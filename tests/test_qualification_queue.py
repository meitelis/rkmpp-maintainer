import contextlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from qualification_queue import hardware_checks, run_suite


class QualificationQueueTests(unittest.TestCase):
    def test_failed_stage_stops_the_queue_and_retains_failure_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            later=Mock()
            with patch("qualification_queue.wait_for_window",return_value={"allowed":True}), \
                 patch("qualification_queue.locked",side_effect=lambda _:contextlib.nullcontext()):
                result=run_suite({},Path(temp),cases=[
                    ("first",lambda:{"status":"failed","error":"source regression"},"passed"),
                    ("later",later,"passed")])
            self.assertEqual(result["status"],"failed")
            later.assert_not_called()
            stored=json.loads(Path(result["report"]).read_text())
            self.assertEqual(stored["steps"]["first"]["result"]["error"],"source regression")
            self.assertFalse(stored["deployment_approved"])

    def test_finished_stage_is_saved_before_waiting_for_the_next_window(self):
        with tempfile.TemporaryDirectory() as temp:
            later=Mock()
            with patch("qualification_queue.wait_for_window",side_effect=[{"allowed":True},KeyboardInterrupt]), \
                 patch("qualification_queue.locked",side_effect=lambda _:contextlib.nullcontext()):
                with self.assertRaises(KeyboardInterrupt):
                    run_suite({},Path(temp),cases=[
                        ("first",lambda:{"status":"passed"},"passed"),("later",later,"passed")])
            report=next((Path(temp)/"qualifications").glob("*/result.json"))
            stored=json.loads(report.read_text())
            self.assertEqual(stored["steps"]["first"]["status"],"passed")
            self.assertEqual(stored["status"],"waiting")
            self.assertEqual(stored["next_step"],"later")
            later.assert_not_called()

    def test_hardware_gate_checks_the_exact_repaired_shipping_image(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            source_job = workspace / "jobs" / "memories-repair-0123abcd"
            (source_job / "source").mkdir(parents=True)
            image_id = "sha256:" + "a" * 64
            tag = "local/memories-rkmpp:repair-qualified-0123abcd"
            repaired = {"status": "passed", "job": str(source_job),
                        "image": tag, "image_id": image_id}
            evidence = {"ok": True, "image_id": image_id,
                        "go_vod_sha256": "b" * 64, "passed": 12}
            with patch("qualification_queue.Docker") as docker_class, \
                 patch("qualification_queue.job_budget", side_effect=lambda *a, **k: contextlib.nullcontext()), \
                 patch("qualification_queue.command", return_value=image_id) as command:
                docker_class.return_value.hardware.return_value = evidence
                result = hardware_checks({}, workspace, repaired)
            self.assertEqual(result["status"], "passed")
            self.assertEqual(result["image_id"], image_id)
            self.assertEqual(result["hardware_qualification"], evidence)
            command.assert_called_once_with(
                ["docker", "image", "inspect", tag, "--format", "{{.Id}}"], timeout=30)
            args = docker_class.return_value.hardware.call_args.args
            self.assertEqual(args[:3], ("memories", source_job / "source", tag))
            self.assertEqual(args[3].name, "hardware.log")
            self.assertNotIn("v11-smoke", str(args))

    def test_hardware_gate_rejects_mismatched_shipped_binary(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            source_job = workspace / "jobs" / "memories-repair-0123abcd"
            (source_job / "source").mkdir(parents=True)
            image_id = "sha256:" + "a" * 64
            repaired = {"status": "passed", "job": str(source_job),
                        "image": "local/memories-rkmpp:repair-qualified-0123abcd",
                        "image_id": image_id}
            with patch("qualification_queue.Docker") as docker_class, \
                 patch("qualification_queue.job_budget", side_effect=lambda *a, **k: contextlib.nullcontext()), \
                 patch("qualification_queue.command", return_value=image_id):
                docker_class.return_value.hardware.return_value = {
                    "ok": True, "image_id": image_id, "passed": 12}
                result = hardware_checks({}, workspace, repaired)
            self.assertEqual(result["status"], "failed")
            self.assertIn("shipping binary identity", result["error"])

    def test_hardware_gate_rejects_stale_shipping_tag(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            source_job = workspace / "jobs" / "memories-repair-0123abcd"
            (source_job / "source").mkdir(parents=True)
            image_id = "sha256:" + "a" * 64
            repaired = {"status": "passed", "job": str(source_job),
                        "image": "local/memories-rkmpp:repair-qualified-0123abcd",
                        "image_id": image_id}
            with patch("qualification_queue.Docker") as docker_class, \
                 patch("qualification_queue.job_budget", side_effect=lambda *a, **k: contextlib.nullcontext()), \
                 patch("qualification_queue.command", return_value="sha256:" + "c" * 64):
                result = hardware_checks({}, workspace, repaired)
            self.assertEqual(result["status"], "failed")
            self.assertIn("changed before", result["error"])
            docker_class.return_value.hardware.assert_not_called()

    def test_immich_qualification_is_an_isolated_historical_replay(self):
        """Never try to upgrade the current 3.4.1 fork to itself."""
        with tempfile.TemporaryDirectory() as temp:
            with patch("qualification_queue.manifest",return_value={"external_model_endpoint":False}), \
                 patch("qualification_queue.wait_for_window",return_value={"allowed":True}), \
                 patch("qualification_queue.locked",side_effect=lambda _:contextlib.nullcontext()), \
                 patch("qualification_queue.runtime_checks",return_value={"status":"passed"}), \
                 patch("qualification_queue.smoke",return_value={"status":"ready"}), \
                 patch("qualification_queue.real_repair",return_value={
                     "status":"passed","image_id":"sha256:fixture"}), \
                 patch("qualification_queue.hardware_checks",return_value={"status":"passed"}), \
                 patch("qualification_queue.qualified_model_memory",return_value=123), \
                 patch("qualification_queue.run_job",return_value={"status":"built"}) as jobs:
                result=run_suite({},Path(temp))
            self.assertEqual(result["status"],"qualified-for-candidate-preparation")
            upgrades=[call for call in jobs.call_args_list if call.args[0]=="immich"]
            self.assertEqual(len(upgrades),1)
            self.assertEqual(upgrades[0].args[3],"3.4.1")
            self.assertEqual(upgrades[0].kwargs["source_version"],"3.4.0")
            self.assertEqual(upgrades[0].kwargs["base_ref"],
                             "3d82c85b3696f384dc39a8a8a13d9df2a563c558")
            self.assertNotIn("publish_code",upgrades[0].kwargs)

    def test_a_failed_revision_is_not_retried_implicitly(self):
        with tempfile.TemporaryDirectory() as temp:
            cases=[("first",lambda:{"status":"failed"},"passed")]
            with patch("qualification_queue.wait_for_window",return_value={"allowed":True}), \
                 patch("qualification_queue.locked",side_effect=lambda _:contextlib.nullcontext()):
                run_suite({},Path(temp),cases=cases)
                with self.assertRaisesRegex(RuntimeError,"review its failure"):
                    run_suite({},Path(temp),cases=cases)
