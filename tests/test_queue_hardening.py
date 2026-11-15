"""Hardening checks for resumable RK3588 qualification."""
import contextlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from qualification_queue import run_suite, qualified_model_memory


STAGES=("model-smoke","memories-replay","real-go-repair","immich-upgrade")


class QueueHardeningTests(unittest.TestCase):
    def test_busy_maintainer_lock_leaves_stage_waiting_not_failed(self):
        with tempfile.TemporaryDirectory() as temp:
            execution=Mock()
            with patch("qualification_queue.wait_for_window",return_value={"allowed":True}), \
                 patch("qualification_queue.locked",side_effect=RuntimeError(
                     "Another maintainer job is running")), \
                 patch("qualification_queue.time.sleep",side_effect=KeyboardInterrupt) as sleep:
                with self.assertRaises(KeyboardInterrupt):
                    run_suite({},Path(temp),cases=[("unattempted",execution,"passed")])
            report=next((Path(temp)/"qualifications").glob("*/result.json"))
            saved=json.loads(report.read_text())
            self.assertEqual(saved["status"],"waiting")
            self.assertEqual(saved["next_step"],"unattempted")
            self.assertNotIn("unattempted",saved["steps"])
            execution.assert_not_called()
            sleep.assert_called_once()

    def test_busy_lock_retries_and_then_allows_real_failure_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            attempts=iter([RuntimeError("Another maintainer job is running"),contextlib.nullcontext()])
            do_work=Mock(return_value={"status":"failed","error":"real codec failure"})
            with patch("qualification_queue.wait_for_window",return_value={"allowed":True}), \
                 patch("qualification_queue.locked",side_effect=lambda _:next(attempts)), \
                 patch("qualification_queue.time.sleep") as sleep:
                result=run_suite({},Path(temp),cases=[("actual",do_work,"passed")])
            self.assertEqual(result["status"],"failed")
            self.assertEqual(result["steps"]["actual"]["result"]["error"],"real codec failure")
            do_work.assert_called_once()
            sleep.assert_called_once()

    def _stage_reports(self,root):
        result={"steps":{}}
        for stage in STAGES:
            job=root/stage
            job.mkdir()
            (job/"run-manifest.json").write_text(json.dumps({"model_identity_verified":True}))
            (job/"model-runtime.json").write_text(json.dumps({
                "state":{"OOMKilled":False},"cgroup_memory":{"memory.peak":"123456"},
                "sampling_errors":[]}))
            result["steps"][stage]={"result":{"job":str(job)}}
        return result

    def test_memory_gate_checks_every_model_backed_stage(self):
        with tempfile.TemporaryDirectory() as temp:
            state=self._stage_reports(Path(temp))
            with patch("qualification_queue.settings",return_value=({},{"memory":"12g"},{})):
                self.assertEqual(qualified_model_memory(state,{},"coding"),123456)
                target=Path(state["steps"]["immich-upgrade"]["result"]["job"])/"model-runtime.json"
                target.unlink()
                with self.assertRaisesRegex(RuntimeError,"Missing model memory evidence for immich-upgrade"):
                    qualified_model_memory(state,{},"coding")

    def test_missing_identity_or_sampling_errors_block_qualification(self):
        with tempfile.TemporaryDirectory() as temp:
            state=self._stage_reports(Path(temp))
            job=Path(state["steps"]["memories-replay"]["result"]["job"])
            runtime=job/"model-runtime.json"
            with patch("qualification_queue.settings",return_value=({},{"memory":"12g"},{})):
                (job/"run-manifest.json").write_text(json.dumps({"model_identity_verified":False}))
                with self.assertRaisesRegex(RuntimeError,"identity"):
                    qualified_model_memory(state,{},"coding")
                (job/"run-manifest.json").write_text(json.dumps({"model_identity_verified":True}))
                runtime.write_text(json.dumps({
                    "state":{"OOMKilled":False},"cgroup_memory":{"memory.peak":123456},
                    "sampling_errors":["container stats unavailable"]}))
                with self.assertRaisesRegex(RuntimeError,"sampling"):
                    qualified_model_memory(state,{},"coding")
                runtime.write_text(json.dumps({
                    "state":{"OOMKilled":True},"cgroup_memory":{"memory.peak":123456},
                    "sampling_errors":[]}))
                with self.assertRaisesRegex(RuntimeError,"OOM"):
                    qualified_model_memory(state,{},"coding")


if __name__=="__main__":
    unittest.main()
