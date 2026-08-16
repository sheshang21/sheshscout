"""
app/dispatch.py — single switch point for "how does a scan job actually run".

Two execution paths exist:
  1. Celery — the job is handed to a separate worker process (docker-compose.yml's
     `worker` service, or a Render Background Worker). The web process just enqueues
     it and returns immediately; the scan itself never competes with the web
     server for CPU/threads. This is the real fix for scans going slow/looking
     "stuck" while the API is also trying to serve requests.
  2. BackgroundTasks (FastAPI) — the job runs inline, inside a thread of the
     same process serving HTTP requests. Correct fallback when there's nowhere
     else to run it (Render's free tier has no Background Worker option), but
     under load this is exactly the failure mode described above.

SCAN_USE_CELERY controls which path is taken. Defaults to "false" so a fresh
deploy with no worker configured doesn't silently queue jobs that will never
run (queued-for-a-worker-that-doesn't-exist looks identical to "stuck at
0/N forever" from the user's side, which is worse than just being slow).

docker-compose.yml sets SCAN_USE_CELERY=true by default because it always
provisions a real `worker` service. On Render, set it to true on the Web
Service ONLY after confirming the Background Worker is actually running
(check its logs for "celery@... ready") — see DEPLOY.md.
"""
import logging
import os

from fastapi import BackgroundTasks

logger = logging.getLogger(__name__)


def _env_bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


SCAN_USE_CELERY = _env_bool("SCAN_USE_CELERY", False)

logger.warning("dispatch CONFIG: SCAN_USE_CELERY=%s", SCAN_USE_CELERY)


def dispatch_positional_scan(background_tasks: BackgroundTasks, job_id: str, symbols: list[str]) -> None:
    if SCAN_USE_CELERY:
        from .tasks import run_scan_job_task
        run_scan_job_task.delay(job_id, symbols)
    else:
        from .scan_runner import run_scan_job
        background_tasks.add_task(run_scan_job, job_id, symbols)


def dispatch_intraday_scan(
    background_tasks: BackgroundTasks, job_id: str, symbols: list[str], direction: str, params: dict
) -> None:
    if SCAN_USE_CELERY:
        from .tasks import run_intraday_scan_job_task
        run_intraday_scan_job_task.delay(job_id, symbols, direction, params)
    else:
        from .intraday_scan_runner import run_intraday_scan_job
        background_tasks.add_task(run_intraday_scan_job, job_id, symbols, direction, params)
