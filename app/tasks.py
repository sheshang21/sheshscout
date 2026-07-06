"""
app/tasks.py — Celery task definitions.

Deliberately thin: run_scan_job() in app/scan_runner.py holds all the
actual logic (fetch, analyze, write results, update job status) and was
built in step 4 specifically so this step could swap the *caller* without
touching that logic. This file is that swap.
"""
from .celery_app import celery_app
from .scan_runner import run_scan_job


@celery_app.task(name="scans.run_scan_job")
def run_scan_job_task(job_id: str, symbols: list[str]) -> None:
    run_scan_job(job_id, symbols)
