import contextlib
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import maintainer
from evidence import manifest
from limits import BudgetExceeded, bounded_timeout, cleanup_budget, job_budget, require_space
from runner import Docker


class OperationsTests(unittest.TestCase):
    def test_lint_failure_stops_compatibility_and_fails_the_check(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "bin").mkdir()
            (root / "memories_rkmpp_v11").mkdir()
            (root / "memories_rkmpp_v11/broken file.php").write_text("invalid PHP")
            php = root / "bin/php"
            php.write_text('#!/bin/sh\nif [ "$1" = "-l" ]; then echo "lint failed" >&2; exit 1; fi\ntouch compatibility-ran\n')
            php.chmod(0o755)
            docker = Docker({"node_image":"node", "go_image":"go", "php_image":"php"}, root)
            calls = []
            with patch.object(docker, "run", side_effect=lambda image, root, args, *rest: calls.append((image,args))), \
                 patch("runner.command"):
                docker.checks("memories", root, root / "checks.log")
            shell_command = next(args[-1] for image,args in calls if image == "php")
            result = subprocess.run([os.environ.get("TEST_SHELL", "/bin/sh"), "-c",
                'PATH="$PWD/bin:/usr/bin:/bin"; ' + shell_command], cwd=root, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("lint failed", result.stderr)
            self.assertFalse((root / "compatibility-ran").exists())

    def test_logging_failure_still_removes_model_and_preserves_primary_error(self):
        with tempfile.TemporaryDirectory() as temp:
            docker = Docker({}, temp)
            calls = []
            def invoke(args, **kwargs):
                calls.append(args)
                if args[:2] == ["docker", "logs"]:
                    raise RuntimeError("logging failed")
                return "owned-container-id" if args[:3] == ["docker", "ps", "-aq"] else ""
            with patch("runner.command", side_effect=invoke):
                with self.assertRaisesRegex(RuntimeError, "primary error"):
                    try:
                        raise RuntimeError("primary error")
                    finally:
                        docker.cleanup("rkmpp-maintainer-model", Path(temp) / "model.log")
            self.assertIn(["docker", "rm", "-f", "owned-container-id"], calls)
            self.assertIn("logging failed", (Path(temp) / "cleanup-errors.jsonl").read_text())

    def test_timeout_removes_the_named_check_container(self):
        with tempfile.TemporaryDirectory() as temp:
            docker = Docker({}, temp)
            calls = []
            def invoke(args, **kwargs):
                calls.append(args)
                if args[:2] == ["docker", "run"]:
                    raise RuntimeError("timed out")
                return "check-id" if args[:3] == ["docker", "ps", "-aq"] else ""
            with patch.object(docker, "verify"), patch("runner.command", side_effect=invoke):
                with self.assertRaisesRegex(RuntimeError, "timed out"):
                    docker.run("fixture", Path(temp), ["sleep", "100"], Path(temp) / "checks.log")
            self.assertIn("--name", calls[0])
            self.assertIn(["docker", "rm", "-f", "check-id"], calls)

    def test_deadline_applies_across_operations_but_allows_cleanup(self):
        with tempfile.TemporaryDirectory() as temp:
            now = time.monotonic()
            with job_budget({"job_timeout_seconds":10, "disk_reserve_bytes":0}, temp):
                with patch("limits.time.monotonic", return_value=now + 20):
                    with self.assertRaisesRegex(BudgetExceeded, "deadline"):
                        bounded_timeout(1800)
                    with cleanup_budget():
                        self.assertEqual(bounded_timeout(30), 30)

    def test_storage_reserves_space_beyond_the_new_download_or_export(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch("limits.shutil.disk_usage", return_value=SimpleNamespace(free=100)):
                with job_budget({"disk_reserve_bytes":80}, temp):
                    with self.assertRaisesRegex(BudgetExceeded, "Insufficient"):
                        require_space(temp, 25)

    def test_failed_smoke_retains_result_manifest_and_error(self):
        with tempfile.TemporaryDirectory() as temp:
            config = {"python_image":"fixture", "disk_reserve_bytes":0}
            with patch("maintainer.Docker"), patch("maintainer.run_agent", side_effect=RuntimeError("repair failed")):
                result = maintainer.smoke(config, Path(temp))
            job = Path(result["job"])
            self.assertEqual(result["status"], "failed")
            self.assertIn("repair failed", (job / "error.txt").read_text())
            self.assertEqual(json.loads((job / "result.json").read_text())["status"], "failed")
            self.assertTrue((job / "run-manifest.json").is_file())
            self.assertIn("finished_at", result)

    def test_manifest_does_not_claim_external_model_identity_or_save_endpoint_secrets(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"MODEL_ENDPOINT":"https://host/?token=secret"}):
            result = manifest({"models":{"coding":{"sha256":"expected"}}, "generation":{"temperature":0.6}}, temp)
            self.assertFalse(result["model_identity_verified"])
            self.assertIsNone(result["model"])
            self.assertNotIn("secret", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
