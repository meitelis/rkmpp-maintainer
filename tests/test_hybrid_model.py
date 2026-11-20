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
        self.assertIn("/dev/dri/renderD129:/dev/dri/renderD129", docker)
        self.assertIn("nofile=262144:262144", docker)
        self.assertEqual(model, ["-ngl", "2", "--no-kv-offload"])
        for invalid in [0, 5, True, "2"]:
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    hybrid_docker_options({**runtime, "npu_layers": invalid})
        with self.assertRaises(ValueError):
            hybrid_docker_options({**runtime, "npu_device": "/dev/dri/../mem"})
        with self.assertRaises(ValueError):
            hybrid_docker_options({**runtime, "no_swap": False})

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
                    return "memory.max=19327352832\\nmemory.swap.max=0\\n"
                if args[:2] == ["docker", "run"] and "--list-devices" in args:
                    return "Available devices: RKNPU: Rockchip NPU"
                return ""
            with patch.dict(os.environ, {"MODEL_ENDPOINT": ""}), \\
                 patch.object(docker, "verify"), \\
                 patch("runner.require_model_memory", return_value={}), \\
                 patch("runner.download_model", return_value=Path(temp)/"models/q4.gguf"), \\
                 patch("runner.command", side_effect=command), \\
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
                return "Available devices: CPU" if "--list-devices" in args else ""
            with patch.dict(os.environ, {"MODEL_ENDPOINT": ""}), \\
                 patch.object(Docker, "verify"), \\
                 patch("runner.require_model_memory", return_value={}), \\
                 patch("runner.download_model", return_value=Path(temp)/"q4.gguf"), \\
                 patch("runner.command", side_effect=run_command):
                with self.assertRaisesRegex(RuntimeError, "RKNPU backend/device not detected"):
                    with Docker(conf,temp).model(profile="hybrid"): pass
            self.assertFalse(any(x[:3] == ["docker", "run", "-d"] for x in commands))
