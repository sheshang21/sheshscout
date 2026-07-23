"""
app/routers/intraday_scans.py — API for the intraday long/short screeners.

Endpoint shapes deliberately mirror app/routers/scans.py (same job
lifecycle: create -> poll/SSE -> results -> resume/cancel -> clear
history) so the frontend can reuse ScanProgress/History/ResultsTable
almost unchanged -- only ScanForm-equivalent and the API base path differ.
Lives at /intraday-scans (its own prefix) rather than overloading /scans
with a scan_type query param, so routing/URLs/permissions stay simple and
each router file stays focused on one pipeline.
"""
import json
import time
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from core.intraday_scanner import DEFAULT_PARAMS
from core.universe import load_universe
from db.models import ScanJob, ScanJobStatus, ScanResult, ScanType, User
from db.session import SessionLocal, get_db

from ..deps import get_current_user
from ..schemas import IntradayScanCreateRequest, ScanJobOut, ScanResultOut
from ..intraday_scan_runner import run_intraday_scan_job, get_debug_log

router = APIRouter(prefix="/intraday-scans", tags=["intraday-scans"])

_SCAN_TYPE_BY_DIRECTION = {"long": ScanType.intraday_long, "short": ScanType.intraday_short}
_DIRECTION_BY_SCAN_TYPE = {v: k for k, v in _SCAN_TYPE_BY_DIRECTION.items()}
_INTRADAY_SCAN_TYPES = list(_SCAN_TYPE_BY_DIRECTION.values())


def _normalize_symbols(symbols: list[str]) -> list[str]:
    """Bare tickers -> .NS by default, same as the frontend's ScanForm
    parseCustomList() does for the positional scanner's custom-list mode."""
    out = []
    for s in symbols:
        s = s.strip().upper()
        if not s:
            continue
        if not (s.endswith(".NS") or s.endswith(".BO")):
            s = f"{s}.NS"
        out.append(s)
    return out


def _resolve_symbols(payload: IntradayScanCreateRequest) -> list[str]:
    if payload.symbols:
        return _normalize_symbols(payload.symbols)

    if payload.range:
        symbols: list[str] = []
        for exch, bounds in payload.range.items():
            if len(bounds) != 2:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"Range for {exch} must be [from, to]")
            frm, to = bounds
            exch_universe = load_universe([exch])
            if frm < 1 or frm > to:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"Invalid range for {exch}: {frm}-{to}")
            symbols.extend(exch_universe[frm - 1:to])
        return symbols

    return load_universe(payload.exchanges)


def _get_owned_job(db: Session, job_id: UUID, user: User) -> ScanJob:
    """404 (not 403) if missing/not-owned/not-intraday -- same reasoning as
    scans.py's _get_owned_job, plus scoped to intraday scan_types only so
    this router can never touch a positional job by guessing its UUID."""
    job = (
        db.query(ScanJob)
        .filter(ScanJob.id == job_id, ScanJob.user_id == user.id, ScanJob.scan_type.in_(_INTRADAY_SCAN_TYPES))
        .first()
    )
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Intraday scan job not found")
    return job


@router.post("", response_model=ScanJobOut, status_code=status.HTTP_201_CREATED)
def start_intraday_scan(
    payload: IntradayScanCreateRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    symbols = _resolve_symbols(payload)
    if not symbols:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Resolved symbol list is empty")

    params = dict(DEFAULT_PARAMS[payload.direction])
    if payload.params:
        params.update({k: v for k, v in payload.params.items() if k in params})

    job = ScanJob(
        user_id=current_user.id,
        status=ScanJobStatus.pending,
        scan_type=_SCAN_TYPE_BY_DIRECTION[payload.direction],
        # Store the *resolved, normalized* symbol list (not payload.symbols
        # verbatim) when an explicit list was given -- resume_intraday_scan
        # below diffs this against ScanResult.symbol (always normalized,
        # e.g. "FAKE1.NS") to find what's left to scan; storing the raw
        # "FAKE1" would never match and everything would look unscanned.
        universe={
            "exchanges": payload.exchanges,
            "range": payload.range,
            "symbols": symbols if payload.symbols else None,
        },
        thresholds=params,
        min_market_cap=0,
        total_stocks=len(symbols),
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    background_tasks.add_task(run_intraday_scan_job, str(job.id), symbols, payload.direction, params)

    return job


@router.get("", response_model=list[ScanJobOut])
def intraday_scan_history(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return (
        db.query(ScanJob)
        .filter(ScanJob.user_id == current_user.id, ScanJob.scan_type.in_(_INTRADAY_SCAN_TYPES))
        .order_by(ScanJob.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )


@router.get("/{job_id}", response_model=ScanJobOut)
def intraday_scan_status(
    job_id: UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return _get_owned_job(db, job_id, current_user)


@router.get("/{job_id}/results", response_model=list[ScanResultOut])
def intraday_scan_results(
    job_id: UUID,
    qualified_only: bool = Query(default=False, description="STRONG signals only"),
    detailed: bool = Query(default=False, description="Include the full analyze_intraday() breakdown per result"),
    limit: int = Query(default=100, ge=1, le=10000),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _get_owned_job(db, job_id, current_user)

    q = db.query(ScanResult).filter(ScanResult.scan_job_id == job_id)
    if qualified_only:
        q = q.filter(ScanResult.qualified.is_(True))
    rows = q.order_by(ScanResult.score.desc()).offset(offset).limit(limit).all()

    out = [ScanResultOut.model_validate(r) for r in rows]
    if not detailed:
        for r in out:
            r.raw_result = None
    return out


@router.post("/{job_id}/resume", response_model=ScanJobOut)
def resume_intraday_scan(
    job_id: UUID,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job = _get_owned_job(db, job_id, current_user)

    if job.status == ScanJobStatus.running and not job.is_stale:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Scan is already running")

    if job.is_stale:
        job.status = ScanJobStatus.failed
        job.error_message = (job.error_message or "") + " [orphaned: server restarted mid-scan]"
        db.commit()

    full_universe = (job.universe or {}).get("symbols") or load_universe((job.universe or {}).get("exchanges"))
    already_scanned = {r.symbol for r in db.query(ScanResult.symbol).filter(ScanResult.scan_job_id == job.id).all()}
    remaining = [s for s in full_universe if s not in already_scanned]

    if not remaining:
        job.status = ScanJobStatus.completed
        db.commit()
        return job

    job.status = ScanJobStatus.pending
    job.error_message = None
    db.commit()

    direction = _DIRECTION_BY_SCAN_TYPE[job.scan_type]
    params = job.thresholds or DEFAULT_PARAMS[direction]

    background_tasks.add_task(run_intraday_scan_job, str(job.id), remaining, direction, params)

    return job


@router.post("/{job_id}/cancel", response_model=ScanJobOut)
def cancel_intraday_scan(
    job_id: UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job = _get_owned_job(db, job_id, current_user)

    if job.status not in (ScanJobStatus.pending, ScanJobStatus.running):
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Scan is not running")

    job.status = ScanJobStatus.cancelled
    job.completed_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(job)
    return job


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
def clear_intraday_history(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Deletes only this user's intraday jobs -- positional jobs (and other
    users' jobs) are untouched, since both the running-job guard and the
    delete itself filter on scan_type.in_(_INTRADAY_SCAN_TYPES)."""
    running_jobs = (
        db.query(ScanJob)
        .filter(
            ScanJob.user_id == current_user.id,
            ScanJob.scan_type.in_(_INTRADAY_SCAN_TYPES),
            ScanJob.status == ScanJobStatus.running,
        )
        .all()
    )
    genuinely_running = [j for j in running_jobs if not j.is_stale]
    if genuinely_running:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Stop the running scan before clearing history")

    db.query(ScanJob).filter(
        ScanJob.user_id == current_user.id,
        ScanJob.scan_type.in_(_INTRADAY_SCAN_TYPES),
    ).delete(synchronize_session=False)
    db.commit()


@router.get("/{job_id}/debug")
def intraday_scan_debug(
    job_id: UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job = _get_owned_job(db, job_id, current_user)
    return {
        "status": job.status,
        "is_stale": job.is_stale,
        "last_heartbeat": job.last_heartbeat,
        "scanned_count": job.scanned_count,
        "total_stocks": job.total_stocks,
        "failed_count": job.failed_count,
        "log": get_debug_log(str(job.id)),
    }


@router.get("/{job_id}/events")
def intraday_scan_events(
    job_id: UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _get_owned_job(db, job_id, current_user)

    def event_stream():
        stream_db = SessionLocal()
        last_snapshot = None
        try:
            while True:
                stream_db.expire_all()
                job = stream_db.query(ScanJob).filter(ScanJob.id == job_id).first()
                if job is None:
                    break

                snapshot = {
                    "status": job.status.value,
                    "total_stocks": job.total_stocks,
                    "scanned_count": job.scanned_count,
                    "failed_count": job.failed_count,
                    "is_stale": job.is_stale,
                }
                if snapshot != last_snapshot:
                    yield f"data: {json.dumps(snapshot)}\n\n"
                    last_snapshot = snapshot

                if job.status in (ScanJobStatus.completed, ScanJobStatus.failed, ScanJobStatus.cancelled):
                    break
                time.sleep(1)
        finally:
            stream_db.close()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
