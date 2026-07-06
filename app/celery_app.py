"""
app/celery_app.py — Celery configuration.

Broker: Redis (already part of the stack for the rate limiter/cache).
No result backend configured: task state (running/completed/failed,
progress counts) already lives in Postgres via ScanJob, written by
run_scan_job() itself. Celery's own result store would just be a second,
redundant source of truth for the same information -- skipped entirely
(task_ignore_result=True) rather than left half-used.

Run a worker:
    export DATABASE_URL=postgresql+psycopg2://stockscout:stockscout@localhost:5432/stockscout
    export REDIS_URL=redis://localhost:6379/0
    celery -A app.celery_app worker --loglevel=info --concurrency=2

--concurrency=2 here means 2 worker PROCESSES (Celery's default "prefork"
pool), each capable of running one scan job's internal ThreadPoolExecutor
(6 threads) at a time -- i.e. up to 2 scan jobs genuinely in parallel,
each internally fetching up to 6 symbols at once. All of them share the
same Redis-backed rate limiter, so scaling this number up doesn't risk
hammering Yahoo harder -- the gate in core/redis_client.py throttles
everyone globally regardless of how many workers or threads are asking.
"""
import os

from celery import Celery

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

celery_app = Celery("stockscout", broker=REDIS_URL)

celery_app.conf.update(
    task_ignore_result=True,
    task_always_eager=os.environ.get("CELERY_ALWAYS_EAGER", "false").lower() == "true",
    # Eager mode runs tasks synchronously in-process, no broker/worker needed --
    # used by the test suite so tests/test_scans.py doesn't require a live
    # Celery worker just to verify the API contract.
)

celery_app.autodiscover_tasks(["app"], related_name="tasks")
