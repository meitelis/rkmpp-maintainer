"""Release selection, bounded processes and credential-free build execution."""
import contextlib
import fcntl
import hashlib
import json
import os
import re
import signal
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path

VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def version(tag):
    match = VERSION.fullmatch(tag)
    if not match:
        raise ValueError("Only stable x.y.z release tags are accepted")
    return tuple(map(int, match.groups()))


def latest_release(releases):
    valid = [r for r in releases if not r.get("draft") and not r.get("prerelease")
             and VERSION.fullmatch(r.get("tag_name", ""))]
    return max(valid, key=lambda r: version(r["tag_name"]), default=None)


def http_json(url, data=None, token=None, method=None):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "rkmpp-maintainer"}
    if token:
        headers["Authorization"] = "Bearer " + token
    encoded = None if data is None else json.dumps(data).encode()
    if encoded is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, encoded, headers, method=method)
    with urllib.request.urlopen(req, timeout=60) as response:
        return json.load(response)


def clean_env(extra=None):
    # Repository scripts and tests never inherit publication/model credentials.
    env = {k: v for k, v in os.environ.items() if k in {
        "PATH", "HOME", "LANG", "LC_ALL", "DOCKER_HOST", "DOCKER_TLS_CERTDIR", "TMPDIR",
        "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
        "http_proxy", "https_proxy", "no_proxy", "all_proxy"}}
    env.update(extra or {})
    return env


def command(args, cwd=None, timeout=1800, log=None, env=None):
    """Bound the entire process group and keep noisy build output on disk."""
    with contextlib.ExitStack() as stack:
        output = stack.enter_context(open(log, "ab")) if log else subprocess.PIPE
        process = subprocess.Popen(args, cwd=cwd, env=clean_env(env), stdin=subprocess.DEVNULL,
                                   stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            data, _ = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            raise RuntimeError(f"Command timed out: {args[0]}") from None
        if log:
            with open(log, "rb") as handle:
                handle.seek(max(0, Path(log).stat().st_size - 20000))
                text = handle.read().decode(errors="replace")
        else:
            text = (data or b"").decode(errors="replace")
        if process.returncode:
            raise RuntimeError(f"Command failed ({process.returncode}): {args[0]}\n{text[-16000:]}")
        return text.strip()


@contextlib.contextmanager
def locked(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another maintainer job is running") from None
        yield


def save_json(path, data):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(path)


def download_model(spec, destination):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    def digest(path):
        with path.open("rb") as handle:
            return hashlib.file_digest(handle, "sha256").hexdigest()
    if destination.exists() and digest(destination) == spec["sha256"]:
        return destination
    temporary = destination.with_suffix(".part")
    req = urllib.request.Request(spec["url"], headers={"User-Agent": "rkmpp-maintainer"})
    with urllib.request.urlopen(req, timeout=120) as response, temporary.open("wb") as target:
        size = 0
        while block := response.read(1024 * 1024):
            size += len(block)
            if size > spec["bytes"]:
                raise RuntimeError("Model download exceeds its pinned size")
            target.write(block)
    if digest(temporary) != spec["sha256"]:
        temporary.unlink(missing_ok=True)
        raise RuntimeError("Model checksum mismatch")
    temporary.replace(destination)
    return destination


def image_digest(repository, tag):
    """Resolve the official GHCR manifest; never substitute an unpinned image."""
    scope = urllib.parse.quote("repository:" + repository + ":pull", safe="")
    token = http_json("https://ghcr.io/token?service=ghcr.io&scope=" + scope)["token"]
    req = urllib.request.Request("https://ghcr.io/v2/" + repository + "/manifests/" + tag,
        headers={"Authorization": "Bearer " + token,
                 "Accept": "application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json"})
    with urllib.request.urlopen(req, timeout=60) as response:
        content = response.read()
        digest = response.headers.get("Docker-Content-Digest", "")
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
        raise RuntimeError("Registry returned no verified digest")
    if hashlib.sha256(content).hexdigest() != digest.removeprefix("sha256:"):
        raise RuntimeError("Registry manifest digest mismatch")
    manifest = json.loads(content)
    if "manifests" in manifest and not any(x.get("platform", {}).get("architecture") == "arm64"
                                            for x in manifest["manifests"]):
        raise RuntimeError("Official runtime has no ARM64 image")
    return "ghcr.io/" + repository + ":" + tag + "@" + digest
