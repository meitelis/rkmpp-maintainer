"""An isolated Docker daemon is used for models, checks and candidate builds."""
import contextlib
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from core import command, download_model


class Docker:
    def __init__(self, config, workspace):
        self.config, self.workspace = config, Path(workspace).resolve()

    def verify(self):
        info = json.loads(command(["docker", "info", "--format", "{{json .}}"], timeout=30))
        if "rkmpp.maintainer=isolated" not in info.get("Labels", []):
            raise RuntimeError("Refusing a Docker daemon without the isolated maintainer label")

    @contextlib.contextmanager
    def model(self, smoke=False):
        if endpoint := os.environ.get("MODEL_ENDPOINT"):
            yield endpoint.rstrip("/")
            return
        self.verify()
        spec = self.config["models"]["smoke" if smoke else "coding"]
        print(f"Validating/downloading {spec["filename"]} ({spec["bytes"]} bytes)", flush=True)
        path = download_model(spec, self.workspace / "models" / spec["filename"])
        name = "rkmpp-maintainer-model"
        # Only touch this stack's transient model in its own daemon.
        with contextlib.suppress(RuntimeError):
            command(["docker", "rm", "-f", name], timeout=30)
        image = self.config["llama_image"]
        command(["docker", "pull", image], timeout=600)
        command(["docker", "run", "-d", "--name", name, "--cpus", "4", "--memory", "8g",
                 "--read-only", "--tmpfs", "/tmp:rw,size=128m", "-p", "8080:8080",
                 "-v", str(path.parent) + ":/models:ro", image,
                 "-m", "/models/" + path.name, "--host", "0.0.0.0", "--port", "8080",
                 "--alias", "local-maintainer", "--ctx-size", "16384", "--threads", "4",
                 "--parallel", "1", "--jinja"], timeout=60)
        endpoint = "http://builder:8080"
        try:
            deadline = time.monotonic() + 600
            while time.monotonic() < deadline:
                try:
                    with urllib.request.urlopen(endpoint + "/health", timeout=5) as response:
                        if response.status == 200:
                            break
                except (OSError, urllib.error.URLError):
                    if command(["docker", "inspect", name, "--format", "{{.State.Running}}"], timeout=15) != "true":
                        raise RuntimeError("Local model exited during startup; see model.log")
                    time.sleep(2)
            else:
                raise RuntimeError("Local model did not become ready in ten minutes")
            yield endpoint
        finally:
            command(["docker", "logs", name], timeout=30, log=self.workspace / "model.log")
            command(["docker", "rm", "-f", name], timeout=30)

    def run(self, image, root, args, log, timeout=1800):
        self.verify()
        return command(["docker", "run", "--rm", "--cpus", "4", "--memory", "8g",
                        "-v", str(root) + ":/src", "-w", "/src", image, *args],
                       timeout=timeout, log=log)

    def checks(self, project, root, log):
        if project == "fixture":
            self.run(self.config["python_image"], root, ["python", "-m", "unittest", "discover", "-s", "tests", "-v"], log)
            return
        node = self.config["node_image"]
        self.run(node, root, ["sh", "-ec", "npm ci --ignore-scripts --no-audit --no-fund && npm test"], log)
        if project == "memories":
            self.run(self.config["go_image"], root, ["sh", "-ec",
                'cd go-vod && test -z "$(gofmt -l .)" && go test -race ./... && go vet ./... && go build ./...'], log)
            self.run(self.config["php_image"], root, ["sh", "-ec",
                "find memories_rkmpp_v11 -name '*.php' -exec php -l {} \\; && php scripts/test-v11-compat.php"], log)
            command(["sh", str(root / "scripts/test-v11-deploy-preflight.sh")], cwd=root, timeout=60, log=log)
        else:
            self.run(node, root, ["node", "--check", "scripts/prepare-source.mjs"], log)

    def build(self, project, root, tag, log):
        self.verify()
        if project == "fixture":
            command(["docker", "build", "-t", tag, str(root)], timeout=900, log=log)
        elif project == "memories":
            command(["sh", "scripts/build-v11.sh"], cwd=root, timeout=7200, log=log,
                    env={"MEMORIES_RKMPP_IMAGE": tag})
        else:
            command(["sh", "scripts/build.sh", "--image", tag], cwd=root, timeout=10800, log=log)
            # Exercise real FFmpeg fragments using the built candidate, not host FFmpeg.
            scratch = root.parent / "fragments"
            scratch.mkdir(exist_ok=True)
            command(["docker", "run", "--rm", "--entrypoint", "node", "--memory", "8g", "--cpus", "4",
                "-v", str(root) + ":/source:ro", "-v", str(scratch) + ":/scratch", "-w", "/source",
                "-e", "IMMICH_FFMPEG_BIN=ffmpeg", "-e", "IMMICH_FFPROBE_BIN=ffprobe",
                "-e", "IMMICH_PLAYBACK_SCRATCH=/scratch",
                "-e", "IMMICH_COMPILED_MEDIA=/usr/src/app/server/dist/utils/media.js",
                tag, "--test", "tests/playback-fragments.test.mjs"], timeout=1800, log=log)

    def export(self, tag, destination):
        self.verify()
        command(["docker", "save", "-o", str(destination), tag], timeout=1800)
        return command(["docker", "image", "inspect", tag, "--format", "{{.Id}}"], timeout=30)
