import contextlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from qualification_queue import run_suite


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

    def test_a_failed_revision_is_not_retried_implicitly(self):
        with tempfile.TemporaryDirectory() as temp:
            cases=[("first",lambda:{"status":"failed"},"passed")]
            with patch("qualification_queue.wait_for_window",return_value={"allowed":True}), \
                 patch("qualification_queue.locked",side_effect=lambda _:contextlib.nullcontext()):
                run_suite({},Path(temp),cases=cases)
                with self.assertRaisesRegex(RuntimeError,"review its failure"):
                    run_suite({},Path(temp),cases=cases)
