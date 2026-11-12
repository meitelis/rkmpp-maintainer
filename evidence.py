"""Reproducible job identity without exporting credentials or model reasoning."""
import hashlib
import json
import os
import platform
from pathlib import Path
from core import save_json
from model_settings import settings


def source_digest(directory):
    digest = hashlib.sha256()
    for path in sorted(Path(directory).glob("*.py")):
        digest.update(path.name.encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def manifest(config, directory, smoke=False):
    canonical = json.dumps(config, sort_keys=True).encode()
    external = bool(os.environ.get("MODEL_ENDPOINT"))
    spec, runtime, generation = settings(config, "smoke" if smoke else "coding")
    return {"schema_version": 1, "controller_source_sha256": source_digest(directory),
            "config_sha256": hashlib.sha256(canonical).hexdigest(),
            "python": platform.python_version(), "architecture": platform.machine(),
            "external_model_endpoint": external,
            "model_identity_verified": False,
            "model": None if external else spec,
            "model_runtime": runtime,
            "generation": generation,
            "runtime_images": {k:v for k,v in config.items() if k.endswith("_image")},
            "limits": {k:config.get(k, default) for k,default in {
                "job_timeout_seconds":21600, "disk_reserve_bytes":2*1024**3,
                "build_free_bytes":8*1024**3, "agent_turns":24, "repair_rounds":3}.items()}}


def save_manifest(job, config, directory, smoke=False):
    result = manifest(config, directory, smoke)
    save_json(Path(job) / "run-manifest.json", result)
    return result


def record_runtime(job, docker):
    value = getattr(docker, "model_evidence", None)
    if isinstance(value, dict):
        path = Path(job) / "run-manifest.json"
        result = json.loads(path.read_text())
        result["observed_model_runtime"] = value
        result["model_identity_verified"] = (not result["external_model_endpoint"] and
            isinstance(value.get("container_image_id"), str) and
            value["container_image_id"].startswith("sha256:") and
            value.get("model_sha256") == (result.get("model") or {}).get("sha256"))
        save_json(path, result)
