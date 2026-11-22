"""Fast real-Docker regression for the isolated fork-script runner.

Run before any large-model CI stage. The exact known Memories fork revision
contains its own deployment-preflight script, including executable mock Docker.
Do not change or weaken that upstream test to accommodate the executor.
"""
import json
import os
import tempfile
from pathlib import Path

from core import locked
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
