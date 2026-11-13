import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent import AgentBlocked
from repair_check import real_repair


class IntegrationRepairTests(unittest.TestCase):
    def run_failure(self, source, seed_error=None, model_error=None):
        with tempfile.TemporaryDirectory() as temp:
            def clone(repository, root):
                (root / "go-vod").mkdir(parents=True)
                (root / "go-vod/main.go").write_text(source)
            with patch("repair_check.clone", side_effect=clone), patch("repair_check.git", return_value="fork-base"), \
                 patch("repair_check.Docker") as factory, patch("repair_check.run_agent", side_effect=model_error) as agent:
                docker = factory.return_value
                docker.run.side_effect = seed_error
                result = real_repair({"disk_reserve_bytes":0, "go_image":"fixture"}, Path(temp))
                self.assertEqual(result["status"], "failed")
                self.assertFalse(result["source_edit_verified"])
                self.assertFalse(result["deployment_approved"])
                self.assertEqual(json.loads((Path(result["job"])/"result.json").read_text())["status"], "failed")
                self.assertTrue((Path(result["job"])/"error.txt").exists())
                docker.checks.assert_not_called()
                docker.build.assert_not_called()
                docker.export.assert_not_called()
                return result, agent.call_count, docker.model.call_count

    def test_unrecognized_source_baseline_stops_before_model(self):
        result, calls, models = self.run_failure("package main\n")
        self.assertIn("baseline", result["error"])
        self.assertEqual((calls, models), (0,0))

    def test_unrelated_seed_check_failure_does_not_count_as_proven_fault(self):
        result, calls, models = self.run_failure("package main\n\tffmpeg.DetectHTTPOptions(c.FFmpeg)\n",
            seed_error=RuntimeError("compiler/network failed"))
        self.assertIn("compiler/network", result["error"])
        self.assertEqual((calls, models), (0,0))

    def test_failed_real_model_repair_cannot_check_build_or_export(self):
        result, calls, models = self.run_failure("package main\n\tffmpeg.DetectHTTPOptions(c.FFmpeg)\n",
            seed_error=RuntimeError("STARTUP_HTTP_CONTRACT: FFmpeg must be probed"),
            model_error=AgentBlocked("Could not complete source repair"))
        self.assertIn("Could not complete", result["error"])
        self.assertEqual((calls, models), (1,1))


if __name__ == "__main__":
    unittest.main()
