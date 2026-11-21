"""CPU + RK3588 NPU opt-in wiring. No hardware success is inferred from CI."""
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from model_settings import hybrid_docker_options, settings, validated_existing_model, with_smoke_npu_layers
from runner import Docker
from maintainer import inspect_moe_dispatch_log


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

    def test_manual_smoke_can_select_more_npu_layers_without_changing_defaults(self):
        config = json.loads((Path(__file__).resolve().parents[1] / "config.json").read_text())
        original_layers = settings(config, "hybrid")[1]["npu_layers"]
        self.assertEqual(original_layers, 2)
        self.assertIs(with_smoke_npu_layers(config, "smoke", "hybrid", None), config)
        for layers in (1, 2, 3, 4):
            with self.subTest(layers=layers):
                trial = with_smoke_npu_layers(config, "smoke", "hybrid", layers)
                self.assertEqual(settings(trial, "hybrid")[1]["npu_layers"], layers)
                self.assertEqual(hybrid_docker_options(settings(trial, "hybrid")[1])[1],
                                 ["-ngl", str(layers), "--no-kv-offload"])
                self.assertEqual(settings(trial, "hybrid_cpu")[1]["npu_layers"], 0)
                self.assertEqual(settings(trial, "coding")[1], settings(config, "coding")[1])
                self.assertEqual(trial["models"]["hybrid"]["runtime"]["memory"], "18g")
                self.assertTrue(trial["models"]["hybrid"]["runtime"]["no_swap"])
                self.assertEqual(config["models"]["hybrid"]["runtime"]["npu_layers"], 2)
        for action, profile, layers in (
                ("run", "hybrid", 4), ("watch", "hybrid", 4),
                ("smoke", "coding", 4), ("smoke", "hybrid_cpu", 4),
                ("smoke", "hybrid", 5), ("smoke", "hybrid", 8),
                ("smoke", "hybrid", 0), ("smoke", "hybrid", True)):
            with self.subTest(action=action, profile=profile, layers=layers):
                with self.assertRaises(ValueError):
                    with_smoke_npu_layers(config, action, profile, layers)

    def test_moe_experiment_only_in_manual_hybrid_smoke_and_never_default(self):
        config = json.loads((Path(__file__).resolve().parents[1] / "config.json").read_text())
        default = settings(config, "hybrid")[1]
        self.assertFalse(default.get("npu_moe", False))
        self.assertNotIn("RKNPU_MOE=1", hybrid_docker_options(default)[0])
        for action, profile in (("watch", "hybrid"), ("run", "hybrid"),
                                ("smoke", "coding"), ("smoke", "hybrid_cpu")):
            with self.subTest(action=action, profile=profile):
                with self.assertRaises(ValueError):
                    with_smoke_npu_layers(config, action, profile, 1, moe=True)

        # Smallest experimental footprint: one layer, MoE enabled only there.
        experimental = with_smoke_npu_layers(config, "smoke", "hybrid", 1, moe=True)
        runtime = settings(experimental, "hybrid")[1]
        env, flags = hybrid_docker_options(runtime)
        self.assertEqual(runtime["npu_layers"], 1)
        self.assertEqual(runtime["memory"], default["memory"])
        self.assertEqual(runtime["no_swap"], default["no_swap"])
        self.assertEqual(runtime["cpu_set"], default["cpu_set"])
        self.assertEqual(flags, ["-ngl", "1", "--no-kv-offload"])
        self.assertIn("RKNPU_MOE=1", env)
        self.assertIn("RKNPU_MOE_DBG=1", env)
        self.assertIn("RKNPU_GLUE=1", env)
        self.assertNotIn("--privileged", env)
        self.assertEqual(config["models"]["hybrid"]["runtime"]["npu_layers"], 2)
        self.assertNotIn("npu_moe", config["models"]["hybrid"]["runtime"])
        self.assertNotIn("npu_moe", settings(experimental, "hybrid_cpu")[1])

        for bad in (0, -1, 5, "1", True):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    with_smoke_npu_layers(config, "smoke", "hybrid", bad, moe=True)
        with self.assertRaises(ValueError):
            with_smoke_npu_layers(config, "smoke", "hybrid", 1, moe=1)
        with self.assertRaisesRegex(ValueError, "requires at least one"):
            hybrid_docker_options({**runtime, "npu_layers": 0})

    def test_low_memory_moe_experiment_uses_real_one_layer_q4_npu_only(self):
        config = json.loads((Path(__file__).resolve().parents[1] / "config.json").read_text())
        baseline = settings(config, "hybrid")[1]
        trial = with_smoke_npu_layers(config, "smoke", "hybrid", 1,
                                      moe=True, moe_efficient=True)
        runtime = settings(trial, "hybrid")[1]
        docker_opts, server_opts = hybrid_docker_options(runtime)
        self.assertIn("RKNPU_HYBRID=W4A16_HADAMARD", docker_opts)
        self.assertIn("RKNPU_AS_GPU=1", docker_opts)
        self.assertIn("RKNPU_MOE=1", docker_opts)
        self.assertEqual(server_opts,
                         ["-ngl", "1", "--no-kv-offload", "--fit", "off"])
        self.assertEqual(runtime["memory"], "18g")
        self.assertTrue(runtime["no_swap"])
        self.assertEqual(runtime["cpu_set"], baseline["cpu_set"])
        self.assertEqual(baseline["npu_layers"], 2)
        self.assertNotIn("RKNPU_AS_GPU=1", hybrid_docker_options(baseline)[0])
        self.assertNotIn("RKNPU_HYBRID=W4A16_HADAMARD", hybrid_docker_options(baseline)[0])
        self.assertNotIn("npu_moe_efficient", settings(trial, "hybrid_cpu")[1])
        self.assertEqual(config["models"]["hybrid"]["runtime"]["npu_layers"], 2)
        for action, profile, layers, moe in [
                ("watch", "hybrid", 1, True), ("run", "hybrid", 1, True),
                ("smoke", "coding", 1, True), ("smoke", "hybrid_cpu", 1, True),
                ("smoke", "hybrid", 2, True), ("smoke", "hybrid", 1, False)]:
            with self.subTest(action=action, profile=profile, layers=layers, moe=moe):
                with self.assertRaises(ValueError):
                    with_smoke_npu_layers(config, action, profile, layers,
                                           moe=moe, moe_efficient=True)
        for bad in (0, 2, "yes"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    hybrid_docker_options({**runtime, "npu_moe_efficient": bad})

    def test_moe_logs_differentiate_dispatch_evidence_from_requested_flag(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "model.log"
            self.assertEqual(inspect_moe_dispatch_log(path),
                             {"expert_batch_path_observed": False,
                              "expert_batch_log_lines": 0, "model_log_available": False})
            path.write_text("loaded RKNPU backend\nno expert dispatch\n")
            self.assertFalse(inspect_moe_dispatch_log(path)["expert_batch_path_observed"])
            path.write_text("[moe-batch] K=1 N=2\n"
                            "other logging\n"
                            "[moe-batch] K=1 N=2\n")
            evidence = inspect_moe_dispatch_log(path)
            self.assertTrue(evidence["expert_batch_path_observed"])
            self.assertEqual(evidence["expert_batch_log_lines"], 2)
            self.assertTrue(evidence["model_log_available"])

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
                    return "memory.max=19327352832\nmemory.swap.max=0\n"
                return ""
            with patch.dict(os.environ, {"MODEL_ENDPOINT": ""}), \
                 patch.object(Docker, "verify"), \
                 patch("runner.require_model_memory", return_value={}), \
                 patch("runner.download_model", return_value=Path(temp)/"models/q4.gguf"), \
                 patch("runner.command", side_effect=run_command), \
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
            with patch.dict(os.environ, {"MODEL_ENDPOINT": ""}), \
                 patch.object(Docker, "verify"), \
                 patch("runner.require_model_memory", return_value={}), \
                 patch("runner.download_model") as download, \
                 patch("runner.validated_existing_model") as existing, \
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
