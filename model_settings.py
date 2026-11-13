"""Resolve model-specific runtime and sampling without changing the CI control."""
import copy


def settings(config, name="coding"):
    if name not in {"coding", "smoke"}:
        raise ValueError("Unknown model profile")
    spec = copy.deepcopy(config.get("models", {}).get(name, {}))
    runtime = {**config.get("model_runtime", {}), **spec.pop("runtime", {})}
    generation = copy.deepcopy(config.get("generation", {}))
    override = spec.pop("generation", {})
    chat = {**generation.get("chat_template_kwargs", {}), **override.get("chat_template_kwargs", {})}
    generation.update(override)
    if chat:
        generation["chat_template_kwargs"] = chat
    return spec, runtime, generation


def memory_bytes(value):
    import re
    match = re.fullmatch(r"(\d+)([kmg])?", str(value).lower())
    if not match:
        raise ValueError("Memory limit must be an integer with optional k/m/g suffix")
    return int(match[1]) * {None:1, "k":1024, "m":1024**2, "g":1024**3}[match[2]]


def require_model_memory(config, spec, runtime):
    """A manual time-window override never bypasses the host RAM safety check."""
    from pathlib import Path
    available = next((int(line.split()[1])*1024 for line in Path("/proc/meminfo").read_text().splitlines()
                      if line.startswith("MemAvailable:")), 0)
    overhead = runtime.get("estimated_overhead_bytes", 1536*1024**2)
    reserve = config.get("work_window",{}).get("memory_headroom_bytes",1024**3)
    required = spec["bytes"] + overhead + reserve
    if available < required:
        raise RuntimeError(f"Host RAM reserved for other services: need {required} available bytes, have {available}")
    return {"available_before_model_bytes":available,"estimated_model_and_reserve_bytes":required}
