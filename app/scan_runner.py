"""
app/scan_runner.py — actually runs a scan job.

IMPORTANT — this is a stand-in, not the final architecture:
  Per the migration plan, step 5 wraps this same fetch-analyze-store logic
  in a Celery task, dispatched to a separate worker pool/process, using
  the Redis-backed rate limiter and cache from step 2. For step 4, there's
  no Celery yet — FastAPI's BackgroundTasks runs this function in a worker
  THREAD of the same web server process, not a separate process.

  That means right now a big scan still competes with the web server for
  CPU/threads in the same container — exactly the failure mode the whole
  redesign exists to avoid. This is fine for exercising the API contract
  (job lifecycle, progress, results, resume) end-to-end today; it is NOT
  the "a slow scan can never make someone else's dashboard unresponsive"
  guarantee. That guarantee only lands with step 5.

  Isolated into this one function on purpose, so step 5 can swap the
  *caller* (a Celery task instead of BackgroundTasks) with minimal churn
  to the logic itself.
"""
import gc
import resource
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import datetime, timezone

from sqlalchemy.dialects.postgresql import insert as pg_insert

from core.scanner import analyze_stock, fetch_stock_data, to_jsonable
from db.models import ScanJob, ScanJobStatus, ScanResult
from db.session import SessionLocal

MAX_WORKERS = 4         # lowered from 6 -- see memory note below
POLL_INTERVAL_S = 3      # how often we check the DB for a cancellation request
                        # and write a heartbeat
MEMORY_CEILING_MB = 420  # Render free web services get 512MB total, shared
                         # with the FastAPI process itself + Python/pandas
                         # import overhead (~100-150MB baseline). If our own
                         # RSS gets this high, stop cleanly (mark the job
                         # failed with a resumable message) rather than let
                         # the OS SIGKILL the whole container -- which is
                         # silent (no traceback, just a restart in the logs)
                         # and was the real cause of scans looking "stuck":
                         # two long-lived, unbounded in-process caches in
                         # core/yf_ratelimit.py (_ticker_registry, _mem_cache)
                         # meant memory grew with every new symbol touched
                         # and never came back down until the process died.
                         # Those are now capped with LRU eviction -- this
                         # ceiling is a safety net in case a run still gets
                         # close, not the primary fix.


def _rss_mb() -> float:
    # ru_maxrss is KB on Linux (Render's containers); bytes on macOS (only
    # relevant for local dev). Values over 10M only make sense as bytes.
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return raw / (1024 * 1024) if raw > 10_000_000 else raw / 1024

# Rolling debug log per job, newest last, capped so it can't grow without
# bound over the process's lifetime. In-memory only (not the DB) -- it's
# for a live "what is this scan doing right now" view, not an audit trail,
# and if the process restarts the log is moot anyway since that's exactly
# the situation last_heartbeat/is_stale (db/models.py) is there to surface.
_DEBUG_LOGS: "OrderedDict[str, deque]" = OrderedDict()
_MAX_TRACKED_JOBS = 25
_MAX_LOG_LINES = 400


def _log(job_id: str, line: str) -> None:
    buf = _DEBUG_LOGS.get(job_id)
    if buf is None:
        buf = deque(maxlen=_MAX_LOG_LINES)
        _DEBUG_LOGS[job_id] = buf
        while len(_DEBUG_LOGS) > _MAX_TRACKED_JOBS:
            _DEBUG_LOGS.popitem(last=False)
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    buf.append(f"[{ts}] {line}")


def get_debug_log(job_id: str) -> list[str]:
    return list(_DEBUG_LOGS.get(job_id, ()))

# Paired with core/yf_ratelimit.py's REQUEST_TIMEOUT_S: that fix stops an
# individual HTTP call from hanging a worker thread forever. This poll loop
# is the second half -- instead of blocking on as_completed() (which only
# ever wakes up when a future finishes), we wake up every POLL_INTERVAL_S
# regardless, so a cancel request is picked up promptly even if a worker
# happens to be mid-retry, and so a scan can never look "stuck" from the
# outside without at least the progress bar/DB being checked.


def run_scan_job(job_id: str, symbols: list[str]) -> None:
    """Fetch + analyze every symbol in `symbols`, writing results as they
    complete. Caller just decides *which* symbols (full universe for a
    fresh scan, remaining-only for a resume) — this function owns the
    running -> completed/failed state transition either way."""
    db = SessionLocal()
    try:
        job = db.query(ScanJob).filter(ScanJob.id == job_id).first()
        if job is None:
            return

        job.status = ScanJobStatus.running
        if job.started_at is None:
            job.started_at = datetime.now(timezone.utc)
        job.last_heartbeat = datetime.now(timezone.utc)
        db.commit()
        _log(job_id, f"Started -- {len(symbols)} symbols queued, {MAX_WORKERS} workers")

        thresholds = job.thresholds
        min_market_cap = job.min_market_cap

        def _scan_one(symbol: str):
            data = fetch_stock_data(symbol)
            if data is None:
                return symbol, "fetch_failed", None
            result = analyze_stock(data, min_market_cap, thresholds)
            # analyze_stock() returns None both for "below min_market_cap"
            # and for an internal exception it swallowed -- either way
            # this symbol was successfully fetched, so it's not a fetch
            # failure. Just nothing to store.
            return symbol, "ok", result

        was_cancelled = False
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            pending = {pool.submit(_scan_one, s) for s in symbols}
            while pending:
                done, pending = wait(pending, timeout=POLL_INTERVAL_S, return_when=FIRST_COMPLETED)

                for future in done:
                    symbol, outcome, result = future.result()

                    if outcome == "fetch_failed":
                        job.failed_count += 1
                        _log(job_id, f"{symbol}: fetch failed")
                    elif result is not None:
                        row = pg_insert(ScanResult).values(
                            scan_job_id=job.id,
                            symbol=symbol,
                            score=result.get("score"),
                            rating=result.get("rating"),
                            qualified=bool(result.get("qualified")),
                            sector=result.get("sector"),
                            raw_result=to_jsonable(result),
                        ).on_conflict_do_nothing(
                            index_elements=["scan_job_id", "symbol"]
                        )
                        db.execute(row)
                        _log(job_id, f"{symbol}: scored {result.get('score')} ({result.get('rating')})")
                    else:
                        _log(job_id, f"{symbol}: fetched, filtered out (below min market cap)")

                    job.scanned_count += 1

                db.commit()

                # Every ~25 symbols, drop references to whatever the last
                # batch of futures were holding (dataframes etc.) and force
                # a collection. Cheap insurance against the 512MB ceiling.
                if job.scanned_count % 25 < len(done):
                    gc.collect()
                    rss = _rss_mb()
                    _log(job_id, f"Progress: {job.scanned_count}/{job.total_stocks} "
                                 f"(failed {job.failed_count}), {len(pending)} in flight, "
                                 f"RSS {rss:.0f}MB")

                    if rss >= MEMORY_CEILING_MB:
                        # Stop cleanly now, on our own terms, with a message
                        # and full progress saved -- instead of waiting for
                        # the OS to SIGKILL the whole container, which wipes
                        # the process with no traceback and leaves the job
                        # stuck at 'running' forever (that's the failure
                        # mode db.models.ScanJob.is_stale exists to catch,
                        # but avoiding it outright is strictly better).
                        _log(job_id, f"Stopping: RSS {rss:.0f}MB hit the {MEMORY_CEILING_MB}MB "
                                     f"safety ceiling. Resume to continue with the remaining symbols.")
                        job.status = ScanJobStatus.failed
                        job.error_message = (
                            f"Stopped at {job.scanned_count}/{job.total_stocks} to stay under "
                            f"Render's free-tier memory limit. Click Resume to continue."
                        )
                        job.completed_at = datetime.now(timezone.utc)
                        db.commit()
                        for f in pending:
                            f.cancel()
                        pool.shutdown(wait=False, cancel_futures=True)
                        return

                # Re-check this job's row every poll tick (not just when a
                # future completes) -- this is what lets a "Stop scan" click
                # actually take effect promptly instead of waiting for
                # whatever's currently in flight to finish on its own.
                db.expire(job)
                job = db.query(ScanJob).filter(ScanJob.id == job_id).first()
                if job is not None and job.status == ScanJobStatus.cancelled:
                    was_cancelled = True
                    _log(job_id, "Cancelled by user request")
                    for f in pending:
                        f.cancel()  # only affects futures that haven't started yet
                    pool.shutdown(wait=False, cancel_futures=True)
                    break

                if job is not None:
                    job.last_heartbeat = datetime.now(timezone.utc)
                    db.commit()

        if was_cancelled:
            # Status/completed_at already set by the /cancel endpoint --
            # don't stomp on it by writing "completed" here.
            return

        job.status = ScanJobStatus.completed
        job.completed_at = datetime.now(timezone.utc)
        db.commit()
        _log(job_id, f"Completed -- {job.scanned_count}/{job.total_stocks} scanned, {job.failed_count} failed")

    except Exception as e:
        db.rollback()
        job = db.query(ScanJob).filter(ScanJob.id == job_id).first()
        if job is not None:
            job.status = ScanJobStatus.failed
            job.error_message = str(e)
            job.completed_at = datetime.now(timezone.utc)
            db.commit()
        _log(job_id, f"FAILED: {e}")
    finally:
        db.close()
