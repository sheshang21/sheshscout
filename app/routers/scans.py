import json
import time
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from core.universe import load_universe
from db.models import ScanJob, ScanJobStatus, ScanResult, User
from db.session import SessionLocal, get_db

from ..deps import get_current_user
from ..schemas import ScanCreateRequest, ScanJobOut, ScanResultOut
from ..scan_runner import run_scan_job, get_debug_log

# NOTE: Render's free tier only supports Web Services -- no Background
# Worker service, so there's nowhere for a Celery worker to run and
# `run_scan_job_task.delay(...)` jobs just sit in Redis forever, never
# consumed (progress stuck at 0). Reverted to step 4's approach: FastAPI's
# BackgroundTasks runs run_scan_job() in a worker thread of this same web
# process. See app/scan_runner.py's docstring for the tradeoff (a big scan
# shares CPU/threads with the web server -- fine for single-user/free-tier
# use, not the "never blocks anyone else" guarantee Celery would give on a
# paid plan with a real worker service).

router = APIRouter(prefix="/scans", tags=["scans"])


@router.get("/universe/counts")
def universe_counts():
    """Symbol counts per exchange, so the frontend can render Range Scan
    bounds (e.g. 'NSE has 2143 stocks, pick rows 1-100') without
    hardcoding numbers that drift as nse.txt/bse.txt change."""
    return {
        "NSE": len(load_universe(["NSE"])),
        "BSE": len(load_universe(["BSE"])),
    }


def _get_owned_job(db: Session, job_id: UUID, user: User) -> ScanJob:
    """404 (not 403) if the job doesn't exist OR belongs to someone else —
    don't reveal that a given job_id exists at all to a non-owner."""
    job = db.query(ScanJob).filter(ScanJob.id == job_id, ScanJob.user_id == user.id).first()
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Scan job not found")
    return job


@router.post("", response_model=ScanJobOut, status_code=status.HTTP_201_CREATED)
def start_scan(
    payload: ScanCreateRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if payload.symbols:
        symbols = payload.symbols
    elif payload.range:
        # Range Scan: slice each named exchange's universe to its 1-based
        # From/To (inclusive), same semantics as the Streamlit app's Range
        # Scan mode. An exchange with no entry in `range` is skipped even
        # if it's in `exchanges`.
        symbols = []
        for exch, bounds in payload.range.items():
            if len(bounds) != 2:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"Range for {exch} must be [from, to]")
            frm, to = bounds
            exch_universe = load_universe([exch])
            if frm < 1 or frm > to:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"Invalid range for {exch}: {frm}-{to}")
            symbols.extend(exch_universe[frm - 1:to])
    else:
        symbols = load_universe(payload.exchanges)

    if not symbols:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Resolved symbol list is empty")

    job = ScanJob(
        user_id=current_user.id,
        status=ScanJobStatus.pending,
        universe={"exchanges": payload.exchanges, "range": payload.range, "symbols": payload.symbols},
        thresholds=payload.thresholds,
        min_market_cap=payload.min_market_cap,
        total_stocks=len(symbols),
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    background_tasks.add_task(run_scan_job, str(job.id), symbols)

    return job


@router.get("", response_model=list[ScanJobOut])
def scan_history(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return (
        db.query(ScanJob)
        .filter(ScanJob.user_id == current_user.id)
        .order_by(ScanJob.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )


@router.get("/{job_id}", response_model=ScanJobOut)
def scan_status(
    job_id: UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return _get_owned_job(db, job_id, current_user)


@router.get("/{job_id}/results", response_model=list[ScanResultOut])
def scan_results(
    job_id: UUID,
    qualified_only: bool = Query(default=False),
    detailed: bool = Query(default=False, description="Include the full analyze_stock() breakdown per result"),
    limit: int = Query(default=100, ge=1, le=10000),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _get_owned_job(db, job_id, current_user)  # ownership check; raises 404 if not found/owned

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
def resume_scan(
    job_id: UUID,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job = _get_owned_job(db, job_id, current_user)

    if job.status == ScanJobStatus.running and not job.is_stale:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Scan is already running")

    if job.is_stale:
        # The process that was running this job is gone (Render can restart
        # a free web service at any time) but nothing ever got the chance to
        # write status='failed'. Recognize that here instead of leaving the
        # job unresumable/unstoppable forever.
        job.status = ScanJobStatus.failed
        job.error_message = (job.error_message or "") + " [orphaned: server restarted mid-scan]"
        db.commit()

    full_universe = (job.universe or {}).get("symbols") or load_universe((job.universe or {}).get("exchanges"))
    already_scanned = {r.symbol for r in db.query(ScanResult.symbol).filter(ScanResult.scan_job_id == job.id).all()}
    remaining = [s for s in full_universe if s not in already_scanned]

    if not remaining:
        # Nothing left to do -- every symbol either has a result or was a
        # confirmed fetch failure already counted. Report as complete
        # rather than dispatching a no-op task.
        job.status = ScanJobStatus.completed
        db.commit()
        return job

    job.status = ScanJobStatus.pending
    job.error_message = None
    db.commit()

    background_tasks.add_task(run_scan_job, str(job.id), remaining)

    return job


@router.post("/{job_id}/cancel", response_model=ScanJobOut)
def cancel_scan(
    job_id: UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Request that a running/pending scan stop. Marks the job cancelled
    immediately; scan_runner's poll loop (see app/scan_runner.py) notices
    within POLL_INTERVAL_S and stops submitting/waiting on new work. Results
    already written for symbols scanned so far are kept."""
    job = _get_owned_job(db, job_id, current_user)

    if job.status not in (ScanJobStatus.pending, ScanJobStatus.running):
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Scan is not running")

    job.status = ScanJobStatus.cancelled
    job.completed_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(job)
    return job


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
def clear_history(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Delete every scan job (and cascaded results) owned by the current
    user. Does not touch other users' jobs or the global dead_symbols table."""
    running_jobs = (
        db.query(ScanJob)
        .filter(ScanJob.user_id == current_user.id, ScanJob.status == ScanJobStatus.running)
        .all()
    )
    # A job stuck at status='running' with a dead/stale heartbeat isn't
    # actually active -- it's exactly the orphaned-by-a-restart case
    # is_stale exists to catch (see db/models.py). Don't let it block
    # clearing history forever; only a genuinely-live scan should.
    genuinely_running = [j for j in running_jobs if not j.is_stale]
    if genuinely_running:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Stop the running scan before clearing history")

    db.query(ScanJob).filter(ScanJob.user_id == current_user.id).delete(synchronize_session=False)
    db.commit()


@router.get("/{job_id}/debug")
def scan_debug(
    job_id: UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Live 'what is this scan doing right now' feed for the collapsible
    debug panel -- per-symbol outcomes, progress snapshots, and enough to
    tell a genuinely-running scan apart from an orphaned one."""
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
def scan_events(
    job_id: UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Server-Sent Events stream of scan progress. Polls the job row every
    second and emits a new event only when something changed, until the
    job reaches a terminal state."""
    _get_owned_job(db, job_id, current_user)  # ownership check up front

    def event_stream():
        # Own session, not the request-scoped `db` above -- this generator
        # keeps running long after the endpoint function itself has
        # returned, so it shouldn't share a session whose lifecycle is
        # tied to the request/response cycle.
        stream_db = SessionLocal()
        last_snapshot = None
        try:
            while True:
                stream_db.expire_all()  # force a fresh read; other sessions have committed since our last poll
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
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},  # disable nginx buffering later
    )
