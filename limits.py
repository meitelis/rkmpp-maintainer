"""Per-job time and free-space guards shared by subprocess and HTTP operations."""
import contextlib
import contextvars
import shutil
import time
from pathlib import Path

_guard = contextvars.ContextVar("job_guard", default=None)


class BudgetExceeded(RuntimeError):
    pass


def require_space(path, needed=0, reserve=None):
    if reserve is None:
        guard = _guard.get()
        reserve = guard[2] if guard else 0
    path = Path(path).resolve()
    while not path.exists():
        path = path.parent
    free = shutil.disk_usage(path).free
    if free < needed + reserve:
        raise BudgetExceeded(f"Insufficient free space: need {needed + reserve} bytes, have {free}")
    return free


def check_budget():
    guard = _guard.get()
    if guard:
        deadline, path, reserve = guard
        if time.monotonic() >= deadline:
            raise BudgetExceeded("Job deadline exceeded")
        require_space(path, reserve=reserve)


def bounded_timeout(seconds):
    check_budget()
    guard = _guard.get()
    return max(0.001, min(seconds, guard[0] - time.monotonic())) if guard else seconds


@contextlib.contextmanager
def job_budget(config, workspace):
    seconds = config.get("job_timeout_seconds", 21600)
    reserve = config.get("disk_reserve_bytes", 2 * 1024**3)
    if not isinstance(seconds, (int, float)) or seconds <= 0 or reserve < 0:
        raise ValueError("Invalid job time or storage budget")
    token = _guard.set((time.monotonic() + seconds, Path(workspace), reserve))
    try:
        check_budget()
        yield
    finally:
        _guard.reset(token)


@contextlib.contextmanager
def cleanup_budget():
    """Cleanup has its own short command timeouts, even after the job expires."""
    token = _guard.set(None)
    try:
        yield
    finally:
        _guard.reset(token)
