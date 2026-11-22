"""Reproducible job identity without exporting credentials or model reasoning."""
import hashlib
import json
import os
import platform
from pathlib import Path
from core import command, save_json
from model_settings import settings


def source_digest(directory):
    """Bind qualification to every controller/build/test input, not just Python."""
    root = Path(directory)
    patterns = ("*.py", "*.json", "*.yml", "*.yaml", "Dockerfile*", "*.cpp", "*.sh",
                ".dockerignore", ".github/workflows/*.yml", "scripts/*.sh",
                "tests/*.py")
    files = {p.relative_to(root).as_posix(): p
             for pattern in patterns for p in root.glob(pattern)}
    digest = hashlib.sha256()
    for name, path in sorted(files.items()):
        if path.is_symlink() or not path.is_file():
            raise RuntimeError("Qualification input is not a regular file: " + name)
        digest.update(name.encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def manifest(config, directory, smoke=False):
    canonical = json.dumps(config, sort_keys=True).encode()
    external = bool(os.environ.get("MODEL_ENDPOINT"))
    spec, runtime, generation = settings(config, "smoke" if smoke else "coding")
    return {"schema_version": 1, "controller_source_sha256": source_digest(directory),
            "config_sha256": hashlib.sha256(canonical).hexdigest(),
            "python": platform.python_version(), "architecture": platform.machine(),
            "kernel_release": platform.release(),
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

class CandidateSourceMutation(RuntimeError):
    """Source no longer matches the reviewed baseline; never attempt an LLM retry."""


def candidate_source_digest(root):
    """Hash precisely the Git-visible candidate source bytes and modes.

    A script with the isolated Docker socket can modify a source bind mount;
    reject any changed checkout after checks/build/hardware and before publishing.
    Included: tracked files and every unignored untracked file. Ignored build
    caches stay excluded; binary/image qualification remains independently
    bound to the exported image ID.
    """
    root = Path(root).resolve()
    names = command(["git", "-C", str(root), "ls-files", "-z",
                     "--cached", "--others", "--exclude-standard"], timeout=120)
    if not names:
        raise RuntimeError("Candidate source snapshot requires a Git checkout")
    digest = hashlib.sha256()
    entries = sorted(set(name for name in names.split("\0") if name))
    for name in entries:
        file = root / name
        if (name.startswith("/") or not file.resolve().is_relative_to(root)
                or file.is_symlink() or not file.is_file()):
            raise RuntimeError("Unsafe or removed candidate source path: " + repr(name))
        for parent in file.parents:
            if parent == root:
                break
            if parent.is_symlink():
                raise RuntimeError("Candidate source parent is symlink: " + repr(name))
        mode = "100755" if file.stat().st_mode & 0o111 else "100644"
        digest.update(name.encode("utf-8") + b"\0" + mode.encode() + b"\0")
        with file.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()
