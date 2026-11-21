"""Resolve model-specific runtime and sampling without changing the CI control."""
import copy


def settings(config, name="coding"):
    if name not in {"coding", "smoke", "hybrid", "hybrid_cpu"}:
        raise ValueError("Unknown model profile")
    spec = copy.deepcopy(config.get("models", {}).get("hybrid" if name == "hybrid_cpu" else name, {}))
    runtime = {**config.get("model_runtime", {}), **spec.pop("runtime", {})}
    if name == "hybrid_cpu":
        # Use the identical pinned fork and GGUF, but compile without RKNN.
        # The RKNPU-enabled binary still initializes its device with -ngl 0,
        # so disabling layer offload alone is NOT a valid CPU baseline.
        runtime["backend"] = "cpu"
        runtime["npu_layers"] = 0
    generation = copy.deepcopy(config.get("generation", {}))
    override = spec.pop("generation", {})
    chat = {**generation.get("chat_template_kwargs", {}), **override.get("chat_template_kwargs", {})}
    generation.update(override)
    if chat:
        generation["chat_template_kwargs"] = chat
    return spec, runtime, generation


def with_smoke_npu_layers(config, action, profile, layers, *, moe=False):
    """Strictly manual, opt-in NPU smoke experiment; no persisted defaults.

    MoE expert dispatch is riskier than dense offload. Allow the smallest
    experimental one-layer run first, always with existing no-swap limits.
    """
    if layers is None and moe is False:
        return config
    if action != "smoke" or profile != "hybrid":
        raise ValueError("--npu-layers/--npu-moe require smoke --model-profile hybrid")
    if type(moe) is not bool:
        raise ValueError("NPU MoE flag must be boolean")
    if layers is not None and (type(layers) is not int or layers not in (1, 2, 3, 4)):
        raise ValueError("Experimental NPU layer count must be 1, 2, 3 or 4")
    if "hybrid" not in config.get("models", {}):
        raise ValueError("Missing hybrid model configuration")
    updated = copy.deepcopy(config)
    runtime = updated["models"]["hybrid"].setdefault("runtime", {})
    if layers is not None:
        runtime["npu_layers"] = layers
    if moe:
        runtime["npu_moe"] = True
    return updated


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
    if type(layers) is not int or not 0 <= layers <= 4:
        raise ValueError("RKNPU2 layers must be an integer from 0 to 4 until hardware qualified")
    if not runtime.get("no_swap"):
        raise ValueError("Hybrid model requires the verified no-swap memory profile")
    moe = runtime.get("npu_moe", False)
    if type(moe) is not bool:
        raise ValueError("RKNPU MoE enablement must be boolean")
    if layers == 0:
        if moe:
            raise ValueError("NPU MoE requires at least one offloaded layer")
        return [], ["-ngl", "0", "--no-kv-offload"]
    device = runtime.get("npu_device")
    if not isinstance(device, str) or re.fullmatch(r"/dev/dri/renderD\d+", device) is None:
        raise ValueError("Hybrid model requires an explicit DRM render device")
    # Board-verified on ROCK 5B+: granting only renderD129 fails RKNN
    # matmul context creation, while passing the /dev/dri group succeeds
    # without --privileged. Use the same mapping for probe and model.
    # The configured render device is still validated above.
    environment = ["-e", "RKNPU_HYBRID=W8A8_STANDARD", "-e", "RKNPU_GLUE=1"]
    if moe:
        # Only the explicit manual smoke experiment ever enables GGML_OP_MUL_MAT_ID.
        # Leave the original quantization pipeline and IOMMU domains unchanged.
        environment += ["-e", "RKNPU_MOE=1", "-e", "RKNPU_MOE_DBG=1"]
    return (["--device", "/dev/dri:/dev/dri",
             "--security-opt", "systempaths=unconfined",
             "--ulimit", "nofile=262144:262144",
             *environment],
            ["-ngl", str(layers), "--no-kv-offload"])
