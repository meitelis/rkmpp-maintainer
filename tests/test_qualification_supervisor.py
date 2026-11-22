"""Crash/restart qualification invariants, without a model or Docker daemon."""
import contextlib
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from qualification_queue import run_suite, supervise


class QualificationSupervisorTests(unittest.TestCase):
    def test_disabled_is_noop(self):
        with patch("qualification_queue.qualification_once") as worker, \
             patch("qualification_queue.time.sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                supervise({}, Path("/unused"), enabled=False)
        worker.assert_not_called()

    def test_conflicting_manual_worker_is_not_killed_or_overlapped(self):
        with patch("qualification_queue.qualification_once", side_effect=[
                RuntimeError("Another maintainer job is running"),
                {"status":"qualified-for-candidate-preparation", "report":"/workspace/report.json"}]) as work, \
             patch("qualification_queue.time.sleep", side_effect=[None, KeyboardInterrupt]) as sleeper:
            with self.assertRaises(KeyboardInterrupt):
                supervise({}, Path("/unused"), enabled=True)
        self.assertEqual(work.call_count, 2)
        self.assertEqual([c.args for c in sleeper.call_args_list], [(60,), (3600,)])

    def test_failed_stage_is_terminal_not_a_restart_loop(self):
        with patch("qualification_queue.qualification_once",
                   return_value={"status":"failed", "report":"/workspace/failed.json"}) as work, \
             patch("qualification_queue.time.sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                supervise({}, Path("/unused"), enabled=True)
        work.assert_called_once()

    def test_corrupt_checkpoint_does_not_trigger_docker_restart_loop(self):
        with patch("qualification_queue.qualification_once",
                   side_effect=ValueError("Invalid result.json")) as work, \
             patch("qualification_queue.time.sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                supervise({}, Path("/unused"), enabled=True)
        work.assert_called_once()

    def test_unexpected_preflight_error_does_not_start_endless_retries(self):
        with patch("qualification_queue.qualification_once",
                   side_effect=RuntimeError("isolated Docker unavailable")) as work, \
             patch("qualification_queue.time.sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                supervise({}, Path("/unused"), enabled=True)
        work.assert_called_once()

    def persisted(self, root, steps, status="running", retries=None):
        identity = {"external_model_endpoint":False,"revision":"supervisor-test"}
        key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:12]
        report = root / "qualifications" / key / "result.json"
        report.parent.mkdir(parents=True,exist_ok=True)
        report.write_text(json.dumps({"status":status,"steps":steps,
                                     "restart_retries":retries or {},"identity":identity}))
        return identity, report

    def test_restart_keeps_passed_step_and_retries_only_unfinished_stage_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            identity, report = self.persisted(root,{
                "done":{"status":"passed","result":{"status":"passed"}},
                "unfinished":{"status":"running","job":"/workspace/jobs/old"}})
            done = Mock()
            unfinished = Mock(return_value={"status":"passed"})
            with patch("qualification_queue.manifest",return_value=identity), \
                 patch("qualification_queue.wait_for_window",return_value={"allowed":True}), \
                 patch("qualification_queue.locked",side_effect=lambda _:contextlib.nullcontext()), \
                 patch("qualification_queue.qualified_model_memory",return_value=123):
                result = run_suite({},root,cases=[
                    ("done",done,"passed"), ("unfinished",unfinished,"passed")])
            done.assert_not_called()
            unfinished.assert_called_once()
            self.assertEqual(result["status"],"qualified-for-candidate-preparation")
            self.assertEqual(result["restart_retries"]["unfinished"],1)
            self.assertEqual(result["interruption_history"][0]["step"],"unfinished")
            self.assertEqual(json.loads(report.read_text())["status"],result["status"])

    def test_second_crash_requires_review_no_repeat(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            identity, report=self.persisted(root,{"step":{"status":"running"}},
                                            retries={"step":1})
            work=Mock()
            with patch("qualification_queue.manifest",return_value=identity):
                result=run_suite({},root,cases=[("step",work,"passed")])
            self.assertEqual(result["status"],"interrupted")
            work.assert_not_called()
            self.assertEqual(json.loads(report.read_text())["status"],"interrupted")

    def test_partially_persisted_failed_stage_cannot_be_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            identity,_=self.persisted(root,{"step":{"status":"failed"}})
            work=Mock()
            with patch("qualification_queue.manifest",return_value=identity):
                result=run_suite({},root,cases=[("step",work,"passed")])
            self.assertEqual(result["status"],"failed")
            work.assert_not_called()

    def test_successful_revision_is_immutable_on_reboot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            identity,_=self.persisted(root,{"step":{"status":"passed"}},
                status="qualified-for-candidate-preparation")
            work=Mock()
            with patch("qualification_queue.manifest",return_value=identity):
                result=run_suite({},root,cases=[("step",work,"passed")])
            self.assertEqual(result["status"],"qualified-for-candidate-preparation")
            work.assert_not_called()


if __name__ == "__main__":
    unittest.main()
