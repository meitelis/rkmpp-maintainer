import contextlib
import json
import stat
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

    def test_hardware_runner_prepares_writable_go_tmpdir_before_tests(self):
        """A missing /cache/tmp must not abort all Go hardware tests."""
        with tempfile.TemporaryDirectory() as temp:
            observed=[]
            def fake_command(args, **kwargs):
                if args[:3] == ["docker", "image", "inspect"]:
                    return "sha256:qualified"
                self.assertEqual(args[:2], ["docker", "run"])
                mount=next(item for item in args if item.startswith("type=bind,src="))
                cache=Path(mount.removeprefix("type=bind,src=").removesuffix(",dst=/cache"))
                temporary=cache/"tmp"
                self.assertTrue(temporary.is_dir())
                self.assertEqual(stat.S_IMODE(temporary.stat().st_mode), 0o1777)
                observed.append(temporary)
                Path(kwargs["log"]).write_text("--- PASS: TestRKMPPHardwareFixture (0.01s)\\n")
                return "--- PASS: TestRKMPPHardwareFixture (0.01s)"
            with patch("qualification_queue.Docker") as docker, \
                 patch("qualification_queue.job_budget", side_effect=lambda *a, **k: contextlib.nullcontext()), \
                 patch("qualification_queue.command", side_effect=fake_command):
                result=hardware_checks({},Path(temp),"sha256:qualified")
                self.assertEqual(result["status"],"passed",result.get("error"))
                self.assertEqual(len(result["tests"]),3)
                self.assertEqual(len(observed),3)
                docker.return_value.verify.assert_called_once()

    def test_immich_qualification_is_an_isolated_historical_replay(self):
        """Never try to upgrade the current 3.4.1 fork to itself."""
        with tempfile.TemporaryDirectory() as temp:
            with patch("qualification_queue.manifest",return_value={"external_model_endpoint":False}), \
                 patch("qualification_queue.wait_for_window",return_value={"allowed":True}), \
                 patch("qualification_queue.locked",side_effect=lambda _:contextlib.nullcontext()), \
                 patch("qualification_queue.runtime_checks",return_value={"status":"passed"}), \
                 patch("qualification_queue.smoke",return_value={"status":"ready"}), \
                 patch("qualification_queue.real_repair",return_value={
                     "status":"passed","hardware_test_image_id":"sha256:fixture"}), \
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
