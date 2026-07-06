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
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from sqlalchemy.dialects.postgresql import insert as pg_insert

from core.scanner import analyze_stock, fetch_stock_data, to_jsonable
from db.models import ScanJob, ScanJobStatus, ScanResult
from db.session import SessionLocal

MAX_WORKERS = 6  # matches core.scanner's own internal semaphore count


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
        db.commit()

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

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = [pool.submit(_scan_one, s) for s in symbols]
            for future in as_completed(futures):
                symbol, outcome, result = future.result()

                if outcome == "fetch_failed":
                    job.failed_count += 1
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
                # else: fetched fine, but filtered out (below min_market_cap)
                # or analyze_stock hit an internal error -- not a fetch
                # failure, just nothing to store for this symbol.

                job.scanned_count += 1
                db.commit()

        job.status = ScanJobStatus.completed
        job.completed_at = datetime.now(timezone.utc)
        db.commit()

    except Exception as e:
        db.rollback()
        job = db.query(ScanJob).filter(ScanJob.id == job_id).first()
        if job is not None:
            job.status = ScanJobStatus.failed
            job.error_message = str(e)
            job.completed_at = datetime.now(timezone.utc)
            db.commit()
    finally:
        db.close()
