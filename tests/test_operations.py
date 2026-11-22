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
                 patch.object(docker, "source_script"):
                docker.checks("memories", root, root / "checks.log")
            shell_command = next(args[-1] for image,args in calls if image == "php")
            result = subprocess.run([os.environ.get("TEST_SHELL", "/bin/sh"), "-c",
                'PATH="$PWD/bin:/usr/bin:/bin"; ' + shell_command], cwd=root, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("lint failed", result.stderr)
            self.assertFalse((root / "compatibility-ran").exists())

    def test_repo_scripts_are_isolated_from_controller_token_and_host_daemon(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            script = root / "scripts" / "build-v11.sh"
            script.parent.mkdir()
            script.write_text("#!/bin/sh\nexit 0\n")
            docker = Docker({}, root)
            calls = []
            def invoke(args, **kwargs):
                calls.append(args)
                if args[:3] == ["docker", "image", "inspect"]:
                    return ""
                if args[:2] == ["docker", "ps"]:
                    return ""
                return ""
            with patch.object(docker, "verify"), \
                 patch("runner.command", side_effect=invoke), \
                 patch.dict(os.environ, {"GITHUB_TOKEN": "do-not-expose", "AUTO_PUBLISH": "true"}):
                docker.source_script("memories", root, "scripts/build-v11.sh",
                                     root / "build.log", docker_socket=True,
                                     env={"MEMORIES_RKMPP_IMAGE": "local/memories-rkmpp:test"})
            run = next(args for args in calls if args[:3] == ["docker", "run", "--rm"])
            self.assertIn("--network", run)
            # The inner Docker client must reach registry auth endpoints;
            # the nested daemon socket already confers network capability.
            self.assertEqual(run[run.index("--network") + 1], "bridge")
            self.assertIn("--read-only", run)
            self.assertIn("--cap-drop", run)
            self.assertIn("ALL", run)
            self.assertIn("no-new-privileges", run)
            self.assertIn("type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock", run)
            self.assertNotIn("--privileged", run)
            self.assertNotIn("GITHUB_TOKEN", " ".join(run))
            self.assertNotIn("do-not-expose", " ".join(run))
            self.assertIn("scripts/build-v11.sh", run)
            self.assertFalse(any(a[0] == "sh" for a in calls))

    def test_mock_only_preflight_remains_offline_and_has_exec_tmpfs(self):
        with tempfile.TemporaryDirectory() as temp:
            docker = Docker({}, temp)
            commands = []
            def invoke(args, **kwargs):
                commands.append(args)
                return ""
            with patch.object(docker, "verify"), patch("runner.command", side_effect=invoke):
                docker.source_script("memories", temp,
                                     "scripts/test-v11-deploy-preflight.sh",
                                     Path(temp) / "preflight.log")
            run = next(args for args in commands if args[:3] == ["docker", "run", "--rm"])
            self.assertEqual(run[run.index("--network") + 1], "none")
            self.assertNotIn("type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock", run)
            self.assertIn("/run/preflight-mocks:rw,exec,nosuid,nodev,size=16m,mode=1777", run)
            self.assertIn("TMPDIR=/run/preflight-mocks", run)

    def test_executor_refuses_arbitrary_scripts_and_token_env(self):
        with tempfile.TemporaryDirectory() as temp:
            docker = Docker({}, temp)
            with patch.object(docker, "verify") as verify:
                with self.assertRaisesRegex(RuntimeError, "unreviewed source script"):
                    docker.source_script("memories", temp, "scripts/deploy-v11.sh", Path(temp)/"out.log",
                                         docker_socket=True)
                with self.assertRaisesRegex(RuntimeError, "unexpected executor environment"):
                    docker.source_script("memories", temp, "scripts/build-v11.sh", Path(temp)/"out.log",
                                         env={"GITHUB_TOKEN": "secret"})
                with self.assertRaisesRegex(RuntimeError, "must never receive Docker"):
                    docker.source_script("memories", temp, "scripts/test-v11-deploy-preflight.sh",
                                         Path(temp)/"out.log", docker_socket=True)
                verify.assert_not_called()

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
