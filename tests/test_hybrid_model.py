"""CPU + RK3588 NPU opt-in wiring. No hardware success is inferred from CI."""
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from model_settings import hybrid_docker_options, settings, validated_existing_model
from runner import Docker


class HybridModelTests(unittest.TestCase):
    def test_default_coding_remains_cpu_and_hybrid_is_pinned_q4_0(self):
        config = json.loads((Path(__file__).resolve().parents[1] / "config.json").read_text())
        _, cpu, _ = settings(config, "coding")
        model, hybrid, _ = settings(config, "hybrid")
        self.assertNotEqual(cpu.get("backend"), "rknpu2")
        self.assertEqual(cpu["memory"], "17g")
        self.assertEqual(hybrid["backend"], "rknpu2")
        self.assertEqual(hybrid["npu_layers"], 2)
        self.assertTrue(hybrid["no_swap"])
        self.assertEqual(model["bytes"], 17304492352)
        self.assertTrue(model["filename"].endswith("Q4_0.gguf"))

    def test_hybrid_options_are_bounded_and_explicit(self):
        runtime = {"backend": "rknpu2", "npu_layers": 2,
                   "npu_device": "/dev/dri/renderD129", "no_swap": True}
        docker, model = hybrid_docker_options(runtime)
        self.assertIn("/dev/dri:/dev/dri", docker)
        self.assertNotIn("/dev/dri/renderD129:/dev/dri/renderD129", docker)
        self.assertIn("nofile=262144:262144", docker)
        self.assertIn("--security-opt", docker)
        self.assertIn("systempaths=unconfined", docker)
        self.assertNotIn("--privileged", docker)
        self.assertEqual(model, ["-ngl", "2", "--no-kv-offload"])
        for invalid in [-1, 5, True, "2"]:
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    hybrid_docker_options({**runtime, "npu_layers": invalid})
        with self.assertRaises(ValueError):
            hybrid_docker_options({**runtime, "npu_device": "/dev/dri/../mem"})
        with self.assertRaises(ValueError):
            hybrid_docker_options({**runtime, "no_swap": False})

    def test_cpu_control_uses_same_q4_model_with_cpu_only_compilation(self):
        config = json.loads((Path(__file__).resolve().parents[1] / "config.json").read_text())
        npu_model, npu_runtime, _ = settings(config, "hybrid")
        cpu_model, cpu_runtime, _ = settings(config, "hybrid_cpu")
        self.assertEqual(cpu_model, npu_model)
        self.assertEqual(cpu_runtime["backend"], "cpu")
        self.assertEqual(cpu_runtime["memory"], npu_runtime["memory"])
        self.assertEqual(cpu_runtime["context_size"], npu_runtime["context_size"])
        self.assertEqual(cpu_runtime["npu_layers"], 0)
        device_opts, flags = hybrid_docker_options(cpu_runtime)
        self.assertEqual(device_opts, [])
        self.assertEqual(flags, [])
        self.assertIn("cpu_control_image", config)

    def test_cpu_control_does_not_request_rknpu_or_enable_backend(self):
        with tempfile.TemporaryDirectory() as temp:
            conf = {"llama_image": "default-cpu-image", "rknpu_image": "hybrid-image",
                    "cpu_control_image": "local/rkmpp-llama-cpu:test",
                    "model_runtime": {"memory": "12g", "threads": 4},
                    "models": {"hybrid": {"filename": "q4.gguf", "bytes": 1,
                        "sha256": "x", "runtime": {"backend": "rknpu2", "memory": "18g",
                        "no_swap": True, "npu_layers": 2, "cpu_set": "4-7",
                        "npu_device": "/dev/dri/renderD129"}}}}
            commands = []
            def run_command(args, **kwargs):
                commands.append(args)
                if args[:3] == ["docker", "image", "inspect"]:
                    raise RuntimeError("no image: build required")
                if args[:2] == ["docker", "run"] and "--list-devices" in args:
                    return "Available devices: CPU"
                if args[:2] == ["docker", "exec"]:
                    return "memory.max=19327352832\\nmemory.swap.max=0\\n"
                return ""
            with patch.dict(os.environ, {"MODEL_ENDPOINT": ""}), \\
                 patch.object(Docker, "verify"), \\
                 patch("runner.require_model_memory", return_value={}), \\
                 patch("runner.download_model", return_value=Path(temp)/"models/q4.gguf"), \\
                 patch("runner.command", side_effect=run_command), \\
                 patch("runner.urllib.request.urlopen") as health:
                health.return_value.__enter__.return_value = SimpleNamespace(status=200)
                with Docker(conf, temp).model(profile="hybrid_cpu") as endpoint:
                    self.assertEqual(endpoint, "http://builder:8080")
            builds = [a for a in commands if a[:2] == ["docker", "build"]]
            self.assertEqual(len(builds), 1)
            self.assertEqual(builds[0][builds[0].index("--build-arg")+1], "LLAMA_RKNPU2=OFF")
            self.assertIn("local/rkmpp-llama-cpu:test", builds[0])
            runs = [a for a in commands if a[:3] == ["docker", "run", "-d"]]
            self.assertEqual(len(runs), 1)
            run = runs[0]
            self.assertIn("local/rkmpp-llama-cpu:test", run)
            self.assertIn("--no-kv-offload", run)
            self.assertEqual(run[run.index("-ngl")+1], "0")
            self.assertNotIn("--device", run)
            self.assertNotIn("--privileged", run)
            self.assertNotIn("RKNPU_GLUE=1", run)
            self.assertFalse(any("/rknpu-probe" in " ".join(a) for a in commands))

    def test_cpu_control_refuses_npu_compiled_image_before_hashing(self):
        with tempfile.TemporaryDirectory() as temp:
            conf = {"llama_image": "default-cpu-image", "rknpu_image": "hybrid-image",
                    "cpu_control_image": "local/cpu-test",
                    "models": {"hybrid": {"filename": "q4.gguf", "bytes": 1,
                        "sha256": "x", "runtime": {"backend": "rknpu2",
                        "memory": "18g", "no_swap": True, "npu_layers": 2}}}}
            def run_command(args, **kwargs):
                if "--list-devices" in args:
                    return "Available devices: RKNPU"
                return ""
            with patch.dict(os.environ, {"MODEL_ENDPOINT": ""}), \\
                 patch.object(Docker, "verify"), \\
                 patch("runner.require_model_memory", return_value={}), \\
                 patch("runner.download_model") as download, \\
                 patch("runner.validated_existing_model") as existing, \\
                 patch("runner.command", side_effect=run_command):
                with self.assertRaisesRegex(RuntimeError, "CPU control image contains RKNPU"):
                    with Docker(conf, temp).model(profile="hybrid_cpu"): pass
                download.assert_not_called()
                existing.assert_not_called()

    def test_reuse_requires_correct_model_bytes_and_sha(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            p = root / "experiments" / "tiny.gguf"
            p.parent.mkdir()
            p.write_bytes(b"validated gguf stand-in")
            spec = {"existing_path": "experiments/tiny.gguf",
                    "bytes": p.stat().st_size,
                    "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
            self.assertEqual(validated_existing_model(spec, root), p)
            with self.assertRaisesRegex(RuntimeError, "checksum"):
                validated_existing_model({**spec, "sha256": "0"*64}, root)
            with self.assertRaisesRegex(RuntimeError, "size"):
                validated_existing_model({**spec, "bytes": 1}, root)
            with self.assertRaisesRegex(RuntimeError, "escapes"):
                validated_existing_model({**spec, "existing_path": "../outside"}, root)
            self.assertIsNone(validated_existing_model({**spec, "existing_path": "missing"}, root))

    def test_hybrid_launch_uses_existing_builder_and_never_silent_cpu(self):
        with tempfile.TemporaryDirectory() as temp:
            conf = {"llama_image": "cpu-image", "rknpu_image": "rknpu-image",
                    "model_runtime": {"memory": "12g", "threads": 4},
                    "models": {"hybrid": {"filename": "q4.gguf", "bytes": 1,
                        "sha256": "x", "runtime": {"backend": "rknpu2", "memory": "18g",
                        "no_swap": True, "npu_layers": 2, "cpu_set": "4-7",
                        "npu_device": "/dev/dri/renderD129"}}}}
            docker = Docker(conf, temp)
            commands = []
            def command(args, **kwargs):
                commands.append(args)
                if args[:2] == ["docker", "exec"]:
                    return "memory.max=19327352832\nmemory.swap.max=0\n"
                if args[:2] == ["docker", "run"] and "--entrypoint" in args:
                    return "RKNPU_PROBE_OK"
                return ""
            with patch.dict(os.environ, {"MODEL_ENDPOINT": ""}), \
                 patch.object(docker, "verify"), \
                 patch("runner.require_model_memory", return_value={}), \
                 patch("runner.download_model", return_value=Path(temp)/"models/q4.gguf"), \
                 patch("runner.command", side_effect=command), \
                 patch("runner.urllib.request.urlopen") as health:
                health.return_value.__enter__.return_value = SimpleNamespace(status=200)
                with docker.model(profile="hybrid") as endpoint:
                    self.assertEqual(endpoint, "http://builder:8080")
            self.assertFalse(any(x[:2] == ["docker", "pull"] for x in commands))
            self.assertTrue(any(x[:3] == ["docker", "image", "inspect"] for x in commands))
            run = next(x for x in commands if x[:3] == ["docker", "run", "-d"])
            self.assertEqual(run[run.index("--memory")+1], "18g")
            self.assertEqual(run[run.index("--memory-swap")+1], "18g")
            self.assertIn("--device", run)
            self.assertEqual(run[run.index("--device") + 1], "/dev/dri:/dev/dri")
            self.assertIn("--security-opt", run)
            self.assertIn("systempaths=unconfined", run)
            probes = [x for x in commands if x[:2] == ["docker", "run"]
                      and "--entrypoint" in x]
            self.assertEqual(len(probes), 1)
            self.assertIn("/opt/llama/bin/rknpu-probe", probes[0])
            self.assertEqual(probes[0][probes[0].index("--device") + 1], "/dev/dri:/dev/dri")
            self.assertIn("--no-kv-offload", run)
            self.assertEqual(run[run.index("-ngl")+1], "2")
            self.assertIn("RKNPU_GLUE=1", run)
            self.assertIn("rknpu-image", run)
            self.assertEqual(docker.model_evidence["npu_execution_verified"], False)

    def test_missing_npu_device_aborts_before_server_starts(self):
        conf = {"rknpu_image": "rknpu-image",
                "models": {"hybrid": {"filename": "q4.gguf", "bytes": 1,
                    "sha256": "x", "runtime": {"backend": "rknpu2", "memory": "18g",
                    "no_swap": True, "npu_layers": 2,
                    "npu_device": "/dev/dri/renderD129"}}}}
        with tempfile.TemporaryDirectory() as temp:
            commands = []
            def run_command(args, **kwargs):
                commands.append(args)
                if "--entrypoint" in args:
                    raise RuntimeError("RKNN failed to open rknpu device")
                return ""
            with patch.dict(os.environ, {"MODEL_ENDPOINT": ""}), \
                 patch.object(Docker, "verify"), \
                 patch("runner.require_model_memory", return_value={}), \
                 patch("runner.download_model", return_value=Path(temp)/"q4.gguf") as download, \
                 patch("runner.validated_existing_model") as existing, \
                 patch("runner.command", side_effect=run_command):
                with self.assertRaisesRegex(RuntimeError, "NPU preflight failed before model loading"):
                    with Docker(conf,temp).model(profile="hybrid"): pass
                download.assert_not_called()
                existing.assert_not_called()
            self.assertFalse(any(x[:3] == ["docker", "run", "-d"] for x in commands))
