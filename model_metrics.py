"""Retain cgroup evidence while a model is alive, including before an OOM exit."""
import threading
import time

from core import save_json

CGROUP_COMMAND = ('for k in memory.current memory.peak memory.max memory.swap.current memory.swap.max '
    'memory/memory.usage_in_bytes memory/memory.max_usage_in_bytes; do '
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

