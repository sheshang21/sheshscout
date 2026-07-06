import json
import time
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from core.universe import load_universe
from db.models import ScanJob, ScanJobStatus, ScanResult, User
from db.session import SessionLocal, get_db

from ..deps import get_current_user
from ..schemas import ScanCreateRequest, ScanJobOut, ScanResultOut
from ..tasks import run_scan_job_task

router = APIRouter(prefix="/scans", tags=["scans"])


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
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    symbols = payload.symbols if payload.symbols else load_universe(payload.exchanges)
    if not symbols:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Resolved symbol list is empty")

    job = ScanJob(
        user_id=current_user.id,
        status=ScanJobStatus.pending,
        universe={"exchanges": payload.exchanges, "symbols": payload.symbols},
        thresholds=payload.thresholds,
        min_market_cap=payload.min_market_cap,
        total_stocks=len(symbols),
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    # Dispatched to a real Celery worker process (separate from the web
    # server) as of step 5 -- see app/scan_runner.py's docstring for why
    # this replaced FastAPI's BackgroundTasks from step 4.
    run_scan_job_task.delay(str(job.id), symbols)

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
    limit: int = Query(default=100, ge=1, le=1000),
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
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job = _get_owned_job(db, job_id, current_user)

    if job.status == ScanJobStatus.running:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Scan is already running")

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

    run_scan_job_task.delay(str(job.id), remaining)

    return job


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
