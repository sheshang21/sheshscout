"""
tests/test_redis_ratelimit_cross_process.py — proves the thing step 5 was
actually for: core/yf_ratelimit.py's throttle/cooldown gate coordinates
across SEPARATE OS PROCESSES via Redis, not just threads in one process.

This is the specific failure mode being fixed: a threading.Lock (the old
implementation) only ever coordinated threads inside one Python process.
Once Celery runs more than one worker process, that lock does nothing —
each process has its own, and one process getting rate-limited would
never tell the others to back off. This test spawns two real, separate
processes to prove that gap is closed.

    export REDIS_URL=redis://localhost:6379/0
    pytest tests/test_redis_ratelimit_cross_process.py -v -s
"""
import os
import subprocess
import sys
import textwrap

import pytest

from core.redis_client import get_redis

_WORKER_SCRIPT = textwrap.dedent("""
    import os, sys, time
    from core.yf_ratelimit import _throttle, _trigger_cooldown

    role = sys.argv[1]
    if role == "trigger":
        time.sleep(0.3)
        _trigger_cooldown(seconds=2)
        print("TRIGGERED", flush=True)
    else:
        t0 = time.time()
        _throttle()
        print(f"FIRST_THROTTLE {time.time() - t0:.2f}", flush=True)
        time.sleep(0.5)
        t0 = time.time()
        _throttle()
        print(f"SECOND_THROTTLE {time.time() - t0:.2f}", flush=True)
""")


@pytest.fixture(autouse=True)
def clean_redis():
    get_redis().flushdb()
    yield


def _run_worker(role: str, repo_root: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", _WORKER_SCRIPT, role],
        cwd=repo_root,
        env={**os.environ},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_cooldown_triggered_by_one_process_blocks_another():
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    worker = _run_worker("worker", repo_root)
    trigger = _run_worker("trigger", repo_root)

    worker_out, worker_err = worker.communicate(timeout=10)
    trigger_out, trigger_err = trigger.communicate(timeout=10)

    assert trigger.returncode == 0, trigger_err
    assert worker.returncode == 0, worker_err
    assert "TRIGGERED" in trigger_out

    lines = {line.split()[0]: float(line.split()[1]) for line in worker_out.strip().splitlines()}

    # First throttle call happens before the other process's cooldown lands —
    # should be near-instant (just the MIN_DELAY_S gate, nothing to wait on yet).
    assert lines["FIRST_THROTTLE"] < 0.5

    # Second throttle call happens AFTER a completely separate process
    # (not a thread -- a different PID) triggered a cooldown. If the two
    # processes weren't actually sharing state via Redis, this would also
    # be near-instant. It isn't -- it's blocked for close to the remaining
    # cooldown window, proving the coordination is real.
    assert lines["SECOND_THROTTLE"] > 1.0, (
        "second _throttle() should have been blocked by the other "
        "process's cooldown -- if this is fast, the two processes aren't "
        "actually sharing rate-limit state"
    )
