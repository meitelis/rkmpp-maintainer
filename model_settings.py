"""Resolve model-specific runtime and sampling without changing the CI control."""
import copy


def settings(config, name="coding"):
    if name not in {"coding", "smoke", "hybrid"}:
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
    required = max(spec["bytes"] + overhead, memory_bytes(runtime["memory"]) if "memory" in runtime else 0) + reserve
    if available < required:
        raise RuntimeError(f"Host RAM reserved for other services: need {required} available bytes, have {available}")
    return {"available_before_model_bytes":available,"estimated_model_and_reserve_bytes":required}


def validated_existing_model(spec, workspace):
    """Reuse exact-hash board GGUF without copying another ~17 GiB file."""
    import hashlib
    from pathlib import Path
    relative = spec.get("existing_path")
    if not relative:
        return None
    root = Path(workspace).resolve()
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root):
        raise RuntimeError("Existing model path escapes the isolated workspace")
    if not candidate.is_file():
        return None
    if candidate.stat().st_size != spec["bytes"]:
        raise RuntimeError("Existing model file size disagrees with pinned GGUF")
    with candidate.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != spec["sha256"]:
        raise RuntimeError("Existing model checksum disagrees with pinned GGUF")
    return candidate


def hybrid_docker_options(runtime):
    """Explicit, bounded CPU+NPU requests; never silently run CPU only."""
    import re
    if runtime.get("backend") != "rknpu2":
        return [], []
    layers = runtime.get("npu_layers")
    if type(layers) is not int or not 1 <= layers <= 4:
        raise ValueError("Hybrid NPU layers must be an integer from 1 to 4 until hardware qualified")
    device = runtime.get("npu_device")
    if not isinstance(device, str) or re.fullmatch(r"/dev/dri/renderD\\d+", device) is None:
        raise ValueError("Hybrid model requires an explicit DRM render device")
    if not runtime.get("no_swap"):
        raise ValueError("Hybrid model requires the verified no-swap memory profile")
    return (["--device", device + ":" + device,
             "--ulimit", "nofile=262144:262144",
             "-e", "RKNPU_HYBRID=W8A8_STANDARD",
             "-e", "RKNPU_GLUE=1"],
            ["-ngl", str(layers), "--no-kv-offload"])
