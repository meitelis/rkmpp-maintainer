import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from core import clean_env, command, latest_release, locked, version
from runner import Docker


class CoreTests(unittest.TestCase):
    def test_stable_release_order_is_semantic(self):
        releases = [{"tag_name": "v3.9.0"}, {"tag_name":"v3.10.0"},
                    {"tag_name":"v4.0.0", "draft":True},
                    {"tag_name":"v5.0.0", "prerelease":True},
                    {"tag_name":"android/1.19"}]
        self.assertEqual(latest_release(releases)["tag_name"], "v3.10.0")
        for tag in ["--help", "../3.4.0", "v3.4.0-rc1", "v3.4.0;evil"]:
            with self.assertRaises(ValueError): version(tag)

    def test_repo_processes_do_not_get_tokens(self):
        with patch.dict(os.environ, {"GITHUB_TOKEN":"secret", "API_KEY":"secret", "DOCKER_HOST":"tcp://builder:2375"}):
            env = clean_env()
        self.assertNotIn("GITHUB_TOKEN", env)
        self.assertNotIn("API_KEY", env)
        self.assertEqual(env["DOCKER_HOST"], "tcp://builder:2375")

    def test_timeout_kills_process_group(self):
        with self.assertRaisesRegex(RuntimeError, "timed out"):
            command(["sh", "-c", "sleep 30"], timeout=0.05)

    def test_daemon_label_prevents_production_daemon_use(self):
        with tempfile.TemporaryDirectory() as temp:
            docker = Docker({}, temp)
            with patch("runner.command", return_value=json.dumps({"Labels":[]})):
                with self.assertRaisesRegex(RuntimeError, "Refusing"):
                    docker.verify()
            with patch("runner.command", return_value=json.dumps({"Labels":["rkmpp.maintainer=isolated"]})):
                docker.verify()

    def test_job_lock_is_exclusive(self):
        with tempfile.TemporaryDirectory() as temp:
            with locked(Path(temp) / "lock"):
                with self.assertRaisesRegex(RuntimeError, "Another"):
                    with locked(Path(temp) / "lock"): pass

    def test_model_lifecycle_applies_memory_and_reasoning_budget(self):
        with tempfile.TemporaryDirectory() as temp:
            config = {"llama_image": "pinned-runtime", "models": {"coding": {"filename": "model.gguf"}},
                      "model_runtime": {"memory": "12g", "context_size": 16384,
                                        "threads": 4, "reasoning_budget": 1024}}
            docker = Docker(config, temp)
            with patch.dict(os.environ, {"MODEL_ENDPOINT": ""}), patch.object(docker, "verify"), \
                 patch("runner.download_model", return_value=Path(temp) / "models/model.gguf"), \
                 patch("runner.command", return_value="") as invoke, \
                 patch("runner.urllib.request.urlopen") as health:
                health.return_value.__enter__.return_value = SimpleNamespace(status=200)
                with docker.model() as endpoint:
                    self.assertEqual(endpoint, "http://builder:8080")
                args = next(call.args[0] for call in invoke.call_args_list
                            if call.args[0][:2] == ["docker", "run"])
                for flag, value in [("--memory", "12g"), ("--ctx-size", "16384"),
                                    ("--reasoning-format", "deepseek"), ("--reasoning-budget", "1024")]:
                    self.assertEqual(args[args.index(flag) + 1], value)
                self.assertEqual(invoke.call_args_list[-1].args[0],
                                 ["docker", "rm", "-f", "rkmpp-maintainer-model"])
