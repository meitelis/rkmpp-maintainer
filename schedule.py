"""Admit new heavy jobs overnight when the host is idle; active jobs finish."""
import datetime
import os
import time
from pathlib import Path
from zoneinfo import ZoneInfo
from model_settings import memory_bytes, settings


def window(config, now=None):
    rule = config.get("work_window")
    if not rule or not rule.get("enabled", True):
        return {"allowed":True, "reason":"schedule disabled", "retry_seconds":300}
    if rule.get("finish_current_job", True) is not True:
        raise ValueError("This schedule admits new jobs only; active jobs are allowed to finish")
    zone = ZoneInfo(rule.get("timezone", "Europe/Dublin"))
    now = (now or datetime.datetime.now(datetime.timezone.utc)).astimezone(zone)
    start, end = rule.get("start_hour", 0), rule.get("end_hour", 8)
    if not (0 <= start <= 23 and 0 <= end <= 23 and start != end):
        raise ValueError("Invalid work window")
    hour = now.hour + now.minute / 60 + now.second / 3600
    allowed = start <= hour < end if start < end else hour >= start or hour < end
    next_start = datetime.datetime.combine(now.date(), datetime.time(start), tzinfo=zone)
    if next_start <= now:
        next_start += datetime.timedelta(days=1)
    seconds = max(1, (next_start.astimezone(datetime.timezone.utc) - now.astimezone(datetime.timezone.utc)).total_seconds())
    return {"allowed":allowed, "reason":"night window open" if allowed else "outside night window",
            "local_time":now.isoformat(), "next_start":next_start.isoformat(),
            "retry_seconds":rule.get("idle_retry_seconds",300) if allowed else seconds}


def cpu_sample():
    fields = [int(v) for v in Path("/proc/stat").read_text().splitlines()[0].split()[1:9]]
    return sum(fields), fields[3]  # Count I/O wait as busy for admission purposes.


def host_idle(config, profile="coding"):
    rule = config.get("work_window", {})
    if not rule.get("idle_only", True):
        return {"allowed":True, "reason":"idle gate disabled"}
    before = cpu_sample()
    time.sleep(rule.get("sample_seconds", 1))
    after = cpu_sample()
    total, idle = after[0]-before[0], after[1]-before[1]
    if total <= 0:
        raise RuntimeError("Cannot measure host CPU activity")
    busy = 100 * (1 - idle / total)
    load = os.getloadavg()[0] / (os.cpu_count() or 1)
    memory = {line.split(':',1)[0]:int(line.split()[1])*1024
              for line in Path("/proc/meminfo").read_text().splitlines() if line.startswith("MemAvailable:")}
    _, runtime, _ = settings(config, profile)
    required = memory_bytes(runtime.get("memory", "12g")) + rule.get("memory_headroom_bytes",1024**3)
    available = memory.get("MemAvailable", 0)
    allowed = (busy <= rule.get("max_cpu_percent",25) and load <= rule.get("max_load_per_core",0.3)
               and available >= required)
    return {"allowed":allowed, "reason":"host idle" if allowed else "host busy or memory reserved",
            "cpu_busy_percent":round(busy,2), "load_per_core":round(load,3),
            "available_memory_bytes":available, "required_memory_bytes":required}


def admission(config, profile="coding", now=None):
    result = window(config, now)
    if result["allowed"] and config.get("work_window", {}).get("enabled", False):
        result.update(host_idle(config, profile))
    return result


def wait_for_window(config, profile="coding", wait=False, ignore=False):
    if ignore:
        return {"allowed":True, "reason":"explicit manual override"}
    while True:
        result = admission(config, profile)
        if result["allowed"]:
            return result
        import json
        print(json.dumps({"status":"waiting" if wait else "deferred", **result}), flush=True)
        if not wait:
            return result
        time.sleep(min(result.get("retry_seconds",300), 300))
