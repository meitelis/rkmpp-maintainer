"""Retain cgroup evidence while a model is alive, including before an OOM exit."""
import threading
import time

from core import save_json

CGROUP_COMMAND = ('for k in memory.current memory.peak memory.max memory.swap.current memory.swap.max '
    'memory/memory.usage_in_bytes memory/memory.max_usage_in_bytes memory/memory.limit_in_bytes memory/memory.memsw.limit_in_bytes; do '
    'if [ -r "/sys/fs/cgroup/$k" ]; then printf "%s=" "$k"; cat "/sys/fs/cgroup/$k"; fi; done')


class CgroupSampler:
    def __init__(self, read, path, runtime):
        self.read, self.path, self.runtime = read, path, runtime
        self.last = {}
        self.samples = 0
        self.errors = []
        self.stop_event = threading.Event()
        self.thread = None

    def sample(self):
        try:
            text = self.read()
            values = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
            if values:
                self.last = values
                self.samples += 1
                save_json(self.path, {"time":time.time(), "runtime":self.runtime,
                    "cgroup_memory":self.last, "memory_samples":self.samples,
                    "sampling_errors":self.errors[-5:]})
        except Exception as error:
            # The last successful sample survives an exited/OOM-killed container.
            self.errors.append(str(error)[:1000])
            self.errors = self.errors[-5:]

    def start(self):
        def observe():
            while not self.stop_event.is_set():
                self.sample()
                self.stop_event.wait(15)
        self.thread = threading.Thread(target=observe, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=7)



def require_enforced_memory_limits(readings, requested_memory, no_swap):
    """Fail closed if the live kernel has not applied the Docker cgroup limits.

    A Docker HostConfig setting alone is not proof, especially with cgroup v1
    when kernel memory+swap accounting is unavailable.
    """
    from model_settings import memory_bytes

    required = memory_bytes(requested_memory)
    try:
        if "memory.max" in readings:  # cgroup v2
            actual = int(readings["memory.max"])
            swap = readings.get("memory.swap.max")
            swap_ok = swap == "0"
            kind = "v2"
        elif "memory/memory.limit_in_bytes" in readings:  # cgroup v1
            actual = int(readings["memory/memory.limit_in_bytes"])
            combined_raw = readings.get("memory/memory.memsw.limit_in_bytes")
            combined = int(combined_raw) if combined_raw is not None else None
            swap = "unavailable" if combined is None else str(combined)
            swap_ok = combined is not None and combined <= actual
            kind = "v1"
        else:
            raise ValueError("no readable memory cgroup limit")
    except (ValueError, TypeError, KeyError) as error:
        raise RuntimeError("Cannot verify effective model RAM/swap cgroup limits") from error
    if actual <= 0 or actual > required:
        raise RuntimeError(
            f"Model RAM limit not enforced: cgroup {actual} bytes, requested {required}")
    if no_swap and not swap_ok:
        raise RuntimeError(
            f"Model no-swap limit not enforced ({kind}: {swap}); "
            "check host cgroup swap-accounting support; model startup refused")
    return {"cgroup": kind, "effective_memory_bytes": actual,
            "effective_swap_limit": swap, "no_swap_enforced": bool(no_swap and swap_ok)}
