"""Per-job time and free-space guards shared by subprocess and HTTP operations."""
import contextlib
import contextvars
import shutil
import signal
import threading
import time
from pathlib import Path

_guard = contextvars.ContextVar("job_guard", default=None)


def alarm_supported():
    return hasattr(signal, "setitimer") and threading.current_thread() is threading.main_thread()


def restore_alarm(handler, timer, started):
    signal.setitimer(signal.ITIMER_REAL, 0)
    signal.signal(signal.SIGALRM, handler)
    remaining = timer[0] - (time.monotonic() - started)
    if timer[0] > 0 and remaining > 0:
        signal.setitimer(signal.ITIMER_REAL, remaining, timer[1])


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
    started = time.monotonic()
    previous = _guard.get()
    deadline = min(started + seconds, previous[0]) if previous else started + seconds
    token = _guard.set((deadline, Path(workspace), reserve))
    alarm = alarm_supported()
    if alarm:
        handler, timer = signal.getsignal(signal.SIGALRM), signal.getitimer(signal.ITIMER_REAL)
        def expired(signum, frame):
            raise BudgetExceeded("Job deadline exceeded")
        signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, max(0.001, deadline - started))
    try:
        check_budget()
        yield
        check_budget()
    finally:
        if alarm:
            restore_alarm(handler, timer, started)
        _guard.reset(token)


@contextlib.contextmanager
def cleanup_budget():
    """Cleanup has its own short command timeouts, even after the job expires."""
    token = _guard.set(None)
    alarm = alarm_supported()
    if alarm:
        started = time.monotonic()
        handler, timer = signal.getsignal(signal.SIGALRM), signal.getitimer(signal.ITIMER_REAL)
        signal.setitimer(signal.ITIMER_REAL, 0)
    try:
        yield
    finally:
        if alarm:
            restore_alarm(handler, timer, started)
        _guard.reset(token)
