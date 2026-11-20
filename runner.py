"""An isolated Docker daemon is used for models, checks and candidate builds."""
import contextlib
import hashlib
import json
import os
import re
import sys
import time
import uuid
import urllib.error
import urllib.request
from pathlib import Path
from core import command, download_model, save_json
from limits import bounded_timeout, check_budget, cleanup_budget, require_space
from model_settings import require_model_memory, settings, validated_existing_model, hybrid_docker_options
from model_metrics import CGROUP_COMMAND, CgroupSampler, require_enforced_memory_limits

PHP_LINT_CHECK = "find memories_rkmpp_v11 -name '*.php' -exec sh -ec 'for f do php -l \"$f\" || exit; done' sh {} + && php scripts/test-v11-compat.php"


class Docker:
    def __init__(self, config, workspace):
        self.config, self.workspace = config, Path(workspace).resolve()

    def verify(self):
        info = json.loads(command(["docker", "info", "--format", "{{json .}}"], timeout=30))
        if "rkmpp.maintainer=isolated" not in info.get("Labels", []):
            raise RuntimeError("Refusing a Docker daemon without the isolated maintainer label")

    def reap(self):
        """Called only at job start under the exclusive workspace lock."""
        self.verify()
        names = command(["docker", "ps", "-a", "--filter", "label=rkmpp.maintainer.transient=true",
                         "--format", "{{.Names}}"], timeout=30).split()
        if any(not name.startswith("rkmpp-maintainer-") for name in names):
            raise RuntimeError("Unexpected transient container name; refusing recovery")
        if names:
            with cleanup_budget():
                command(["docker", "rm", "-f", *names], timeout=60)
            with (self.workspace / "recovery.jsonl").open("a") as handle:
                handle.write(json.dumps({"time":time.time(), "removed_orphans":names}) + "\n")

    def cleanup(self, name, model_log=None):
        """Always attempt removal; never replace an active exception with log errors."""
        primary_error = sys.exc_info()[0] is not None
        errors = []
        with cleanup_budget():
            if model_log:
                try:
                    command(["docker", "logs", name], timeout=30, log=model_log)
                except Exception as error:
                    errors.append(str(error))
            try:
                ids = command(["docker", "ps", "-aq", "--filter", "name=^/" + name + "$"], timeout=30).split()
                if ids:
                    command(["docker", "rm", "-f", *ids], timeout=30)
            except Exception as error:
                errors.append(str(error))
        if errors:
            try:
                with (self.workspace / "cleanup-errors.jsonl").open("a") as handle:
                    handle.write(json.dumps({"container": name, "time": time.time(), "errors": errors}) + "\n")
            except OSError as error:
                errors.append("Could not persist cleanup error: " + str(error))
                print("; ".join(errors), file=sys.stderr, flush=True)
            if not primary_error:
                raise RuntimeError("Transient container cleanup/logging failed: " + "; ".join(errors))

    def discard_failed_candidate(self, tag):
        """Remove only an unsuccessful candidate tag from the isolated builder.

        Never prune caches, delete unrelated images, or touch host Docker.
        """
        if not re.fullmatch(r"local/(?:memories|immich)-rkmpp:candidate-\d+\.\d+\.\d+-[a-f0-9]{8}", tag):
            raise RuntimeError("Refusing to remove non-candidate Docker image: " + tag)
        self.verify()
        with cleanup_budget():
            try:
                command(["docker", "image", "inspect", tag, "--format", "{{.Id}}"], timeout=30)
            except RuntimeError:
                return  # No image survived the unsuccessful build.
            command(["docker", "image", "rm", tag], timeout=120)

    @contextlib.contextmanager
    def model(self, smoke=False, metrics_path=None, profile=None):
        if endpoint := os.environ.get("MODEL_ENDPOINT"):
            self.model_evidence = {"external":True, "identity_verified":False}
            yield endpoint.rstrip("/")
            return
        self.verify()
        spec, runtime, _ = settings(self.config, profile or ("smoke" if smoke else "coding"))
        memory_preflight = require_model_memory(self.config, spec, runtime)
        print(f"Validating/downloading {spec["filename"]} ({spec["bytes"]} bytes)", flush=True)
        path = validated_existing_model(spec, self.workspace) or download_model(
            spec, self.workspace / "models" / spec["filename"])
        name = "rkmpp-maintainer-model"
        # Only touch this stack's transient model in its own daemon.
        with contextlib.suppress(RuntimeError):
            command(["docker", "rm", "-f", name], timeout=30)
        hybrid = runtime.get("backend") == "rknpu2"
        if runtime.get("backend", "cpu") not in {"cpu", "rknpu2"}:
            raise ValueError("Unknown model backend")
        image = self.config["rknpu_image"] if hybrid else self.config["llama_image"]
        threads = str(runtime.get("threads", 4))
        docker_options = ["--oom-score-adj","1000"]
        npu_docker, npu_flags = hybrid_docker_options(runtime)
        docker_options += npu_docker
        if runtime.get("cpu_set"):
            docker_options += ["--cpuset-cpus", runtime["cpu_set"]]
        if runtime.get("no_swap"):
            docker_options += ["--memory-swap", runtime.get("memory", "12g")]
        model_options = []
        for field, flag in [("threads_batch","--threads-batch"), ("batch_size","--batch-size"),
                            ("ubatch_size","--ubatch-size"), ("flash_attention","--flash-attn"),
                            ("cache_type_k","--cache-type-k"), ("cache_type_v","--cache-type-v"),
                            ("cache_ram","--cache-ram"), ("context_checkpoints","--ctx-checkpoints")]:
            if field in runtime:
                model_options += [flag, str(runtime[field])]
        if hybrid:
            # Build the pinned runtime once in our existing isolated builder.
            try:
                command(["docker", "image", "inspect", image], timeout=20)
            except RuntimeError:
                source = Path(__file__).resolve().parent
                command(["docker", "build", "-f", str(source / "Dockerfile.rknpu2"),
                         "-t", image, str(source)], timeout=3600)
            detected = command([
                "docker", "run", "--rm", "--network", "none", "--device",
                runtime["npu_device"], image, "--list-devices"], timeout=60)
            if not re.search(r"(?i)\bRKNPU\b", detected):
                raise RuntimeError("RKNPU backend/device not detected; refusing CPU-only fallback")
        else:
            command(["docker", "pull", image], timeout=600)
        endpoint = "http://builder:8080"
        sampler = None
        try:
            command(["docker", "run", "-d", "--name", name, "--label", "rkmpp.maintainer.transient=true", "--cpus", threads,
                 "--memory", runtime.get("memory", "12g"), *docker_options,
                 "--read-only", "--tmpfs", "/tmp:rw,size=128m", "-p", "8080:8080",
                 "-v", str(path.parent) + ":/models:ro", image,
                 "-m", "/models/" + path.name, "--host", "0.0.0.0", "--port", "8080",
                 "--alias", "local-maintainer", "--ctx-size", str(runtime.get("context_size", 16384)),
                 "--threads", threads, "--parallel", "1", "--jinja",
                 "--reasoning-format", "deepseek",
                 "--reasoning-budget", str(runtime.get("reasoning_budget", 1024)), *model_options, *npu_flags], timeout=60)
            # Docker's requested limits are not evidence that cgroup v1
            # actually enforces memory+swap. Check the live model container.
            cgroup_text = command(["docker", "exec", name, "sh", "-c",
                                   CGROUP_COMMAND], timeout=15)
            cgroup_values = dict(line.split("=", 1) for line in cgroup_text.splitlines() if "=" in line)
            enforced_limits = require_enforced_memory_limits(
                cgroup_values, runtime.get("memory", "12g"), runtime.get("no_swap", False))
            if metrics_path:
                sampler = CgroupSampler(lambda:command(["docker","exec",name,"sh","-c",CGROUP_COMMAND],timeout=5),
                                        metrics_path,runtime)
                sampler.start()
            deadline = time.monotonic() + 600
            while time.monotonic() < deadline:
                check_budget()
                try:
                    with urllib.request.urlopen(endpoint + "/health", timeout=bounded_timeout(5)) as response:
                        if response.status == 200:
                            break
                except (OSError, urllib.error.URLError):
                    if command(["docker", "inspect", name, "--format", "{{.State.Running}}"], timeout=15) != "true":
                        raise RuntimeError("Local model exited during startup; see model.log")
                    time.sleep(2)
            else:
                raise RuntimeError("Local model did not become ready in ten minutes")
            self.model_evidence = {"container_image_id":command(
                ["docker", "inspect", name, "--format", "{{.Image}}"], timeout=30),
                "model_sha256":spec["sha256"], "filename":spec["filename"],
                "backend": "rknpu2" if hybrid else "cpu",
                "npu_layers_requested": runtime["npu_layers"] if hybrid else 0,
                "npu_device_detected": bool(hybrid),
                "npu_execution_verified": False,
                **memory_preflight, **enforced_limits}
            yield endpoint
        finally:
            try:
                if metrics_path:
                    with cleanup_budget():
                        snapshot = {"time":time.time(), "runtime":runtime, "errors":[]}
                        if sampler:
                            sampler.stop()
                            snapshot.update(cgroup_memory=sampler.last,memory_samples=sampler.samples,
                                            sampling_errors=sampler.errors)
                        try:
                            snapshot["state"] = json.loads(command(["docker", "inspect", name,
                                "--format", "{{json .State}}"], timeout=15))
                            snapshot["state"].pop("Health",None)
                            snapshot["docker_stats"] = json.loads(command(["docker", "stats", "--no-stream",
                                "--format", "{{json .}}", name], timeout=15))
                            text = command(["docker", "exec", name, "sh", "-c",
                                CGROUP_COMMAND], timeout=15)
                            snapshot["cgroup_memory"] = dict(line.split("=",1) for line in text.splitlines() if "=" in line)
                        except Exception as error:
                            snapshot["errors"].append(str(error))
                        try:
                            save_json(metrics_path, snapshot)
                        except OSError as error:
                            print("Could not persist model metrics: " + str(error), file=sys.stderr, flush=True)
            finally:
                self.cleanup(name, Path(metrics_path).parent / "model.log" if metrics_path else self.workspace / "model.log")

    def run(self, image, root, args, log, timeout=1800):
        self.verify()
        name = "rkmpp-maintainer-check-" + uuid.uuid4().hex[:12]
        self.last_check_container = name
        try:
            return command(["docker", "run", "--rm", "--name", name, "--label", "rkmpp.maintainer.transient=true", "--cpus", "4", "--memory", "8g",
                            "-v", str(root) + ":/src", "-w", "/src", image, *args],
                           timeout=timeout, log=log)
        finally:
            self.cleanup(name)

    def checks(self, project, root, log):
        if project == "fixture":
            self.run(self.config["python_image"], root, ["python", "-m", "unittest", "discover", "-s", "tests", "-v"], log)
            return
        node = self.config["node_image"]
        self.run(node, root, ["sh", "-ec", "npm ci --ignore-scripts --no-audit --no-fund && npm test"], log)
        if project == "memories":
            self.run(self.config["go_image"], root, ["sh", "-ec",
                'cd go-vod && test -z "$(gofmt -l .)" && go test -race ./... && go vet ./... && go build ./...'], log)
            self.run(self.config["php_image"], root, ["sh", "-ec", PHP_LINT_CHECK], log)
            command(["sh", str(root / "scripts/test-v11-deploy-preflight.sh")], cwd=root, timeout=60, log=log)
        else:
            self.run(node, root, ["node", "--check", "scripts/prepare-source.mjs"], log)

    def build(self, project, root, tag, log):
        self.verify()
        require_space(self.workspace, self.config.get("build_free_bytes", 8 * 1024**3))
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
            name = "rkmpp-maintainer-fragments-" + uuid.uuid4().hex[:12]
            try:
                command(["docker", "run", "--rm", "--name", name, "--label", "rkmpp.maintainer.transient=true", "--entrypoint", "node", "--memory", "8g", "--cpus", "4",
                "-v", str(root) + ":/source:ro", "-v", str(scratch) + ":/scratch", "-w", "/source",
                "-e", "IMMICH_FFMPEG_BIN=ffmpeg", "-e", "IMMICH_FFPROBE_BIN=ffprobe",
                "-e", "IMMICH_PLAYBACK_SCRATCH=/scratch",
                "-e", "IMMICH_COMPILED_MEDIA=/usr/src/app/server/dist/utils/media.js",
                    tag, "--test", "tests/playback-fragments.test.mjs"], timeout=1800, log=log)
            finally:
                self.cleanup(name)
            report = Path(log).read_text(errors="replace")
            # A zero exit code can hide a skipped Node test; all three
            # original/HEVC/AAC fragment cases are mandatory, with no skips.
            def count(key):
                match = re.search(r"(?m)^\s*[ℹ#]?\s*" + key + r"\s+(\d+)\s*$", report)
                return int(match[1]) if match else None
            if (count("tests"), count("pass"), count("fail"), count("skipped")) != (3, 3, 0, 0):
                raise RuntimeError("Incomplete Immich FFmpeg fragment qualification: " + str(log))

    def hardware(self, project, root, tag, log):
        """Run the built RKMPP image through on-board hardware tests.

        Only the isolated Docker daemon is accepted; production image stores,
        application databases and real media are never mounted.
        """
        self.verify()
        root = Path(root).resolve()
        scratch = root.parent / Path(log).stem
        scratch.mkdir(exist_ok=False)
        image_id = command(["docker", "image", "inspect", tag, "--format", "{{.Id}}"], timeout=30)
        arch = command(["docker", "image", "inspect", tag, "--format", "{{.Architecture}}"], timeout=30)
        if arch != "arm64":
            raise RuntimeError("Hardware qualification requires an ARM64 candidate")
        for path in ("/dev/dri", "/dev/dma_heap", "/dev/mpp_service", "/dev/rga"):
            command(["docker", "run", "--rm", "--network", "none", "--privileged",
                     "--label", "rkmpp.maintainer.transient=true",
                     "--entrypoint", "sh", self.config["node_image"],
                     "-c", f"test -e {path}"], timeout=60)
        if project == "immich":
            from hardware_gate import validate_immich_report
            name = "rkmpp-maintainer-hardware-" + uuid.uuid4().hex[:12]
            label = "candidate-" + root.parent.name[-8:]
            try:
                command(["docker", "run", "--rm", "--privileged", "--network", "none",
                         "--name", name, "--label", "rkmpp.maintainer.transient=true",
                         "--cpus", "4", "--memory", "6g",
                         "-v", str(root / "scripts/verify-hardware.mjs") + ":/opt/verify-hardware.mjs:ro",
                         "-v", str(scratch) + ":/scratch", "--entrypoint", "node", tag,
                         "/opt/verify-hardware.mjs", "--scratch", "/scratch",
                         "--hdr", "--sdr10", "--small", "--audio",
                         "--fallback", "--rotation", "--memories", "--label", label],
                        timeout=3600, log=log)
            finally:
                self.cleanup(name)
            evidence = validate_immich_report(scratch, label)
        elif project == "memories":
            from hardware_gate import validate_memories_matrix
            # The upstream fork's matrix builds its own separate smoke image and
            # invokes its compiled ffmpeg/core/api Go tests on real MPP/RGA.
            command(["bash", "scripts/test-v11-rkmpp.sh", "--matrix"], cwd=root,
                    env={"RKMPP_SMOKE_SKIP_BUILD": "1", "RKMPP_SMOKE_OUTPUT": str(scratch)},
                    timeout=3600, log=log)
            evidence = validate_memories_matrix(scratch)
            # The shipping runtime must contain precisely the smoke-tested engine.
            def extract_hash(image, filename):
                name = "rkmpp-maintainer-hash-" + uuid.uuid4().hex[:12]
                output = scratch / filename
                try:
                    command(["docker", "create", "--name", name,
                             "--label", "rkmpp.maintainer.transient=true",
                             image], timeout=60)
                    command(["docker", "cp", name + ":/app/go-vod", str(output)], timeout=120)
                    with output.open("rb") as stream:
                        return hashlib.file_digest(stream, "sha256").hexdigest()
                finally:
                    self.cleanup(name)
            tested_hash = extract_hash("local/memories-rkmpp:v11-smoke", "tested-go-vod")
            shipping_hash = extract_hash(tag, "shipping-go-vod")
            if tested_hash != shipping_hash:
                raise RuntimeError("Memories shipping go-vod differs from hardware-tested binary")
            evidence["go_vod_sha256"] = shipping_hash
        else:
            raise RuntimeError("No RKMPP hardware qualification for " + project)
        if command(["docker", "image", "inspect", tag, "--format", "{{.Id}}"], timeout=30) != image_id:
            raise RuntimeError("Candidate image identity changed during hardware tests")
        evidence["image_id"] = image_id
        evidence["arch"] = arch
        return evidence

    def export(self, tag, destination):
        self.verify()
        size = int(command(["docker", "image", "inspect", tag, "--format", "{{.Size}}"], timeout=30))
        require_space(self.workspace, int(size * 1.2))
        command(["docker", "save", "-o", str(destination), tag], timeout=1800)
        archive = Path(destination)
        if not archive.is_file() or archive.stat().st_size <= 0:
            raise RuntimeError("Docker export archive missing or empty")
        with archive.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        archive.with_suffix(archive.suffix + ".sha256").write_text(
            digest + "  " + archive.name + "\n")
        return command(["docker", "image", "inspect", tag, "--format", "{{.Id}}"], timeout=30)
