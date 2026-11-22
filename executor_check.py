"""Fast real-Docker regression for the isolated fork-script runner.

Run before any large-model CI stage. The exact known Memories fork revision
contains its own deployment-preflight script, including executable mock Docker.
Do not change or weaken that upstream test to accommodate the executor.
"""
import contextlib
import json
import os
import tempfile
import uuid
from pathlib import Path

from core import command, locked
from maintainer import clone
from profiles import git
from runner import Docker

# Immutable fork baseline also used by the full real Go repair fixture.
FORK_BASE = "c8fa9b5323d396c4d1ba3c38f17f12b912d4d490"


def main():
    workspace = Path(os.environ.get("WORKSPACE", "/workspace"))
    workspace.mkdir(parents=True, exist_ok=True)
    with locked(workspace / "maintainer.lock"):
        docker = Docker({}, workspace)
        docker.verify()
        # Use an isolated disposable copy, never modify the running forks.
        with tempfile.TemporaryDirectory(prefix="script-check-", dir=workspace) as directory:
            # Probe the real BuildKit client-side registry-auth path from
            # inside the same socket-bearing executor as fork build scripts.
            # Docker's --pull forces remote token retrieval even with a cache;
            # a no-network Docker CLI would fail as the actual CI build did.
            probe_root = Path(directory) / "registry-probe"
            probe_script = probe_root / "scripts" / "build-v11.sh"
            probe_script.parent.mkdir(parents=True)
            probe_script.write_text("""#!/bin/sh
set -eu
docker build --pull -t "$MEMORIES_RKMPP_IMAGE" - <<'DOCKERFILE'
FROM docker.io/library/busybox:1.37.0
RUN true
DOCKERFILE
""")
            probe_tag = "local/rkmpp-registry-auth-probe:" + uuid.uuid4().hex[:12]
            probe_log = Path(directory) / "registry-probe.log"
            try:
                docker.source_script(
                    "memories", probe_root, "scripts/build-v11.sh",
                    probe_log, timeout=240, docker_socket=True,
                    env={"MEMORIES_RKMPP_IMAGE": probe_tag})
                built = command(["docker", "image", "inspect", probe_tag,
                                 "--format", "{{.Id}}"], timeout=30)
                if not built.startswith("sha256:"):
                    raise RuntimeError("BuildKit registry probe did not produce an image")
            except Exception:
                if probe_log.is_file():
                    print(probe_log.read_text(errors="replace")[-16000:], flush=True)
                raise
            finally:
                # Only remove the uniquely named disposable test tag, never
                # the user's existing images or reusable Docker build cache.
                with contextlib.suppress(RuntimeError):
                    command(["docker", "image", "rm", probe_tag], timeout=60)
            root = Path(directory) / "source"
            clone("meitelis/memories-rkmpp", root)
            git(root, "checkout", "--detach", FORK_BASE)
            log = Path(directory) / "preflight.log"
            try:
                docker.source_script(
                    "memories", root, "scripts/test-v11-deploy-preflight.sh",
                    log, timeout=180)
                output = log.read_text(errors="replace")
                required = "Version guards, project ownership, build-only behavior and image defaults: passed"
                if required not in output:
                    raise RuntimeError("The real fork's preflight did not report all checks passing")
                print(json.dumps({"status": "passed", "kind": "real-isolated-fork-preflight",
                                  "fork_commit": FORK_BASE}), flush=True)
            except Exception:
                # Keep the unmodified fork script's own output in CI logs.
                if log.is_file():
                    print(log.read_text(errors="replace")[-16000:], flush=True)
                raise


if __name__ == "__main__":
    main()
