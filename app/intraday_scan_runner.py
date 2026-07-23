"""
app/intraday_scan_runner.py — runs an intraday long/short scan job.

Deliberately a separate file from app/scan_runner.py rather than a branch
inside run_scan_job(): the two pipelines fetch differently (single 2-call
fundamentals fetch + cache vs. two lightweight OHLCV calls, no fundamentals
cache), analyze differently (min_market_cap vs price/volume/RSI/ATR), and
store results differently (rating/sector mean different things per
scan_type — see db/models.py's ScanType docstring). Keeping them separate
means porting the intraday screeners can't regress the positional scanner's
already-hardened heartbeat/memory-ceiling/cancel logic, at the cost of the
control-flow skeleton below being duplicated from scan_runner.py. If that
duplication ever gets annoying, the shared bits (heartbeat/cancel/memory
poll loop) are the obvious candidate to extract into a common helper both
call into.

Same BackgroundTasks caveat as scan_runner.py's docstring: this runs in a
worker thread of the same web server process, not an isolated worker.
"""
import gc
import resource
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import datetime, timezone

from sqlalchemy.dialects.postgresql import insert as pg_insert

from core.intraday_scanner import analyze_intraday, fetch_intraday_data
from core.scanner import to_jsonable
from db.models import ScanJob, ScanJobStatus, ScanResult
from db.session import SessionLocal

MAX_WORKERS = 4          # same as scan_runner.py -- one shared Render free-tier box
POLL_INTERVAL_S = 3
MEMORY_CEILING_MB = 420  # see scan_runner.py's MEMORY_CEILING_MB note; same ceiling,
                         # same reasoning, same Render container.


def _rss_mb() -> float:
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return raw / (1024 * 1024) if raw > 10_000_000 else raw / 1024


# Separate debug-log ring from scan_runner.py's -- job IDs are UUIDs so
# there's no collision risk either way, but keeping the buffers apart means
# a huge positional scan's log spam can't evict an intraday job's log (or
# vice versa) out of the tracked-jobs window.
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


def run_intraday_scan_job(job_id: str, symbols: list[str], direction: str, params: dict) -> None:
    """Fetch + analyze every symbol in `symbols` for intraday long/short
    setups, writing results as they complete. `direction` and `params` are
    passed explicitly by the caller (rather than re-derived from the job
    row) so resume uses exactly the same direction/params the original
    scan started with -- see app/routers/intraday_scans.py's resume
    endpoint."""
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
        _log(job_id, f"Started ({direction}) -- {len(symbols)} symbols queued, {MAX_WORKERS} workers")

        def _scan_one(symbol: str):
            data = fetch_intraday_data(symbol)
            if data is None:
                return symbol, "fetch_failed", None
            result = analyze_intraday(data, symbol, direction, params)
            # None here means "fetched fine, just didn't clear min_price/
            # min_volume/min_conditions/min_score" -- not a fetch failure.
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
                            rating=result.get("signal_strength"),
                            # "qualified" is repurposed for intraday to mean
                            # STRONG (vs MODERATE) -- see ScanType docstring.
                            qualified=(result.get("signal_strength") == "STRONG"),
                            sector=direction,  # repurposed to hold long/short
                            raw_result=to_jsonable(result),
                        ).on_conflict_do_nothing(
                            index_elements=["scan_job_id", "symbol"]
                        )
                        db.execute(row)
                        _log(job_id, f"{symbol}: scored {result.get('score')} ({result.get('signal_strength')})")
                    else:
                        _log(job_id, f"{symbol}: fetched, filtered out (below thresholds)")

                    job.scanned_count += 1

                db.commit()

                if job.scanned_count % 25 < len(done):
                    gc.collect()
                    rss = _rss_mb()
                    _log(job_id, f"Progress: {job.scanned_count}/{job.total_stocks} "
                                 f"(failed {job.failed_count}), {len(pending)} in flight, "
                                 f"RSS {rss:.0f}MB")

                    if rss >= MEMORY_CEILING_MB:
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

                db.expire(job)
                job = db.query(ScanJob).filter(ScanJob.id == job_id).first()
                if job is not None and job.status == ScanJobStatus.cancelled:
                    was_cancelled = True
                    _log(job_id, "Cancelled by user request")
                    for f in pending:
                        f.cancel()
                    pool.shutdown(wait=False, cancel_futures=True)
                    break

                if job is not None:
                    job.last_heartbeat = datetime.now(timezone.utc)
                    db.commit()

        if was_cancelled:
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
