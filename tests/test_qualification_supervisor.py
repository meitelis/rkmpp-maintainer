"""Crash/restart qualification invariants, without a model or Docker daemon."""
import contextlib
import subprocess
import sys
import time
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from qualification_queue import run_suite, supervise, supervisor_health


class QualificationSupervisorTests(unittest.TestCase):
    def test_disabled_is_noop(self):
        with patch("qualification_queue.write_supervisor_status") as report, \
             patch("qualification_queue.qualification_once") as worker, \
             patch("qualification_queue.time.sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                supervise({}, Path("/unused"), enabled=False)
        worker.assert_not_called()

    def test_conflicting_manual_worker_is_not_killed_or_overlapped(self):
        with patch("qualification_queue.write_supervisor_status") as report, \
             patch("qualification_queue.qualification_once", side_effect=[
                RuntimeError("Another maintainer job is running"),
                {"status":"qualified-for-candidate-preparation", "report":"/workspace/report.json"}]) as work, \
             patch("qualification_queue.time.sleep", side_effect=[None, KeyboardInterrupt]) as sleeper:
            with self.assertRaises(KeyboardInterrupt):
                supervise({}, Path("/unused"), enabled=True)
        self.assertEqual(work.call_count, 2)
        self.assertEqual([c.args for c in sleeper.call_args_list], [(60,), (3600,)])

    def test_failed_stage_is_terminal_not_a_restart_loop(self):
        with patch("qualification_queue.write_supervisor_status") as report, \
             patch("qualification_queue.qualification_once",
                   return_value={"status":"failed", "report":"/workspace/failed.json"}) as work, \
             patch("qualification_queue.time.sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                supervise({}, Path("/unused"), enabled=True)
        work.assert_called_once()

    def test_corrupt_checkpoint_does_not_trigger_docker_restart_loop(self):
        with patch("qualification_queue.write_supervisor_status") as report, \
             patch("qualification_queue.qualification_once",
                   side_effect=ValueError("Invalid result.json")) as work, \
             patch("qualification_queue.time.sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                supervise({}, Path("/unused"), enabled=True)
        work.assert_called_once()

    def test_unexpected_preflight_error_does_not_start_endless_retries(self):
        with patch("qualification_queue.write_supervisor_status") as report, \
             patch("qualification_queue.qualification_once",
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



    def test_real_sigkill_during_a_stage_recovers_exactly_once(self):
        # This crosses a real OS process boundary: not a mocked running-step
        # status. The first worker is killed while holding the stage lock.
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            identity={"external_model_endpoint":False,"test":"real-process-kill"}
            child = """
import sys,time
from pathlib import Path
from unittest.mock import patch
from qualification_queue import run_suite
root=Path(sys.argv[1])
identity={"external_model_endpoint":False,"test":"real-process-kill"}
def completed():
    (root/"completed.marker").write_text("exactly-once")
    return {"status":"passed"}
def hanging():
    (root/"hanging.marker").write_text("started")
    time.sleep(120)
    return {"status":"passed"}
with patch("qualification_queue.manifest",return_value=identity), \\
     patch("qualification_queue.wait_for_window",return_value={"allowed":True}), \\
     patch("qualification_queue.qualified_model_memory",return_value=1):
    run_suite({},root,cases=[("completed",completed,"passed"),
                             ("interrupted",hanging,"passed")])
"""
            worker=subprocess.Popen([sys.executable,"-c",child,str(root)],
                                    stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,
                                    cwd=str(Path(__file__).resolve().parents[1]))
            try:
                for attempt in range(100):
                    if (root/"hanging.marker").is_file():
                        break
                    if worker.poll() is not None:
                        raise AssertionError("First qualifier process exited before interruption")
                    time.sleep(0.05)
                else:
                    self.fail("First worker never entered the interrupted stage")
                worker.kill()
                worker.communicate(timeout=5)
                self.assertNotEqual(worker.returncode,0)
                completed=Mock(side_effect=AssertionError("Completed stage was replayed"))
                rerun=Mock(return_value={"status":"passed"})
                with patch("qualification_queue.manifest",return_value=identity), \
                     patch("qualification_queue.wait_for_window",return_value={"allowed":True}), \
                     patch("qualification_queue.qualified_model_memory",return_value=1):
                    result=run_suite({},root,cases=[
                        ("completed",completed,"passed"),("interrupted",rerun,"passed")])
                completed.assert_not_called()
                rerun.assert_called_once()
                self.assertEqual(result["status"],"qualified-for-candidate-preparation")
                self.assertEqual(result["restart_retries"]["interrupted"],1)
            finally:
                if worker.poll() is None:
                    worker.kill()
                    worker.communicate(timeout=5)

    def test_supervisor_failure_is_unhealthy_while_process_remains_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("qualification_queue.qualification_once",
                       return_value={"status":"failed","report":"/workspace/failure.json"}), \
                 patch("qualification_queue.time.sleep", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    supervise({}, Path(tmp), enabled=True)
            self.assertEqual(supervisor_health(tmp)["status"],"blocked")
            self.assertFalse(supervisor_health(tmp)["healthy"])

    def test_completed_qualification_has_healthy_immutable_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("qualification_queue.qualification_once",
                       return_value={"status":"qualified-for-candidate-preparation",
                                     "report":"/workspace/result.json"}), \
                 patch("qualification_queue.time.sleep", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    supervise({}, Path(tmp), enabled=True)
            self.assertEqual(supervisor_health(tmp)["status"],"completed")
            self.assertTrue(supervisor_health(tmp)["healthy"])

    def test_corrupt_or_absent_supervisor_status_is_unhealthy(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(supervisor_health(tmp)["healthy"])
            (Path(tmp)/"qualifier-status.json").write_text("not json")
            self.assertFalse(supervisor_health(tmp)["healthy"])

if __name__ == "__main__":
    unittest.main()
