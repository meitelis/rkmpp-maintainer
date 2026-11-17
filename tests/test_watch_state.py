"""Regression coverage for unattended release polling and lock ownership."""
import contextlib
import datetime
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import maintainer


class WatcherTests(unittest.TestCase):
    def test_current_releases_produce_no_images_or_jobs(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace=Path(temp)
            args=SimpleNamespace(action="watch",ignore_window=False,publish=False,no_agent=False)
            releases=[{"project":"memories","current":"11.1.4","latest":"11.1.4","update":False},
                      {"project":"immich","current":"3.4.1","latest":"3.4.1","update":False}]
            with patch.dict(os.environ,{"AUTO_UPDATE":"true","AUTO_PUBLISH":"false"},clear=True), \
                 patch("maintainer.discover",return_value=releases), \
                 patch("maintainer.run_job") as jobs, \
                 patch("maintainer.admission") as admission:
                delay,failed,deferred=maintainer.poll_releases(
                    args,{"poll_seconds":21600},workspace,"coding")
            self.assertEqual((delay,failed,deferred),(21600,False,False))
            jobs.assert_not_called()
            admission.assert_not_called()
            self.assertFalse((workspace/"jobs").exists())
            self.assertFalse((workspace/"state.json").exists())

    def test_new_release_builds_only_its_changed_project(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace=Path(temp)
            args=SimpleNamespace(action="watch",ignore_window=False,publish=False,no_agent=False)
            releases=[{"project":"memories","latest":"11.1.5","update":True},
                      {"project":"immich","latest":"3.4.1","update":False}]
            good_job=workspace/"jobs"/"memories-11.1.5-aabbccdd"
            good_job.mkdir(parents=True)
            response={"status":"built","job":str(good_job),"ready_to_deploy":True,
                      "delivery":{"image_id":"sha256:qualified"}}
            with patch.dict(os.environ,{"AUTO_UPDATE":"true","AUTO_PUBLISH":"false"},clear=True), \
                 patch("maintainer.discover",return_value=releases), \
                 patch("maintainer.admission",return_value={"allowed":True}), \
                 patch("maintainer.run_job",return_value=response) as jobs:
                _,failed,_=maintainer.poll_releases(args,{},workspace,"coding")
            self.assertFalse(failed)
            jobs.assert_called_once()
            self.assertEqual(jobs.call_args.args[0],"memories")
            self.assertEqual(jobs.call_args.args[3],"11.1.5")
            state=json.loads((workspace/"state.json").read_text())
            self.assertIn("memories:11.1.5",state)
            self.assertNotIn("immich:3.4.1",state)

    def test_failed_candidate_retries_but_completed_candidate_does_not(self):
        now = datetime.datetime(2026, 11, 14, 0, 0, tzinfo=datetime.timezone.utc)
        failed = {"status": "failed", "attempts": 1, "last_attempt_at": now.isoformat()}
        self.assertEqual(maintainer.release_attempt_due(failed, now, 3, 3600, watch=True)[1], "cooldown")
        later = now + datetime.timedelta(hours=2)
        self.assertTrue(maintainer.release_attempt_due(failed, later, 3, 3600, watch=True)[0])
        self.assertFalse(maintainer.release_attempt_due(
            {"status": "built", "attempts": 1}, later, 3, 3600, watch=True)[0])
        self.assertEqual(maintainer.release_attempt_due(
            {"status": "failed", "attempts": 3}, later, 3, 3600, watch=True)[1], "retry-limit")

    def test_old_failed_state_is_retryable(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        due, status, attempts = maintainer.release_attempt_due({"status": "failed", "job": "old"},
                                                                now, 3, 3600, watch=True)
        self.assertEqual((due, status, attempts), (True, "retry", 1))

    def test_once_retries_failed_release_then_stops_after_success(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            records = [{"project": "immich", "latest": "3.4.1", "update": True}]
            args = SimpleNamespace(action="once", ignore_window=False, publish=False, no_agent=True)
            result = iter([{"status": "failed", "job": "job1"},
                           {"status": "built", "job": "job2", "ready_to_deploy":True,
                            "delivery":{"image_id":"sha256:qualified"}}])
            with patch("maintainer.discover", return_value=records), \
                 patch("maintainer.admission", return_value={"allowed": True}), \
                 patch("maintainer.run_job", side_effect=lambda *a, **k: next(result)) as run:
                _, failed, _ = maintainer.poll_releases(args, {"poll_seconds": 300}, workspace, "coding")
                self.assertTrue(failed)
                self.assertEqual(json.loads((workspace / "state.json").read_text())["immich:3.4.1"]["attempts"], 1)
                _, failed, _ = maintainer.poll_releases(args, {"poll_seconds": 300}, workspace, "coding")
                self.assertFalse(failed)
                _, failed, _ = maintainer.poll_releases(args, {"poll_seconds": 300}, workspace, "coding")
                self.assertFalse(failed)
                self.assertEqual(run.call_count, 2)
            state = json.loads((workspace / "state.json").read_text())["immich:3.4.1"]
            self.assertEqual((state["status"], state["attempts"]), ("built", 2))

    def test_watch_cooldown_prevents_hot_loop(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            now = datetime.datetime.now(datetime.timezone.utc)
            (workspace / "state.json").write_text(json.dumps({"memories:11.1.4": {
                "status": "failed", "attempts": 1, "last_attempt_at": now.isoformat()}}))
            args = SimpleNamespace(action="watch", ignore_window=False, publish=False, no_agent=True)
            with patch.dict(os.environ, {"AUTO_UPDATE": "true"}), \
                 patch("maintainer.discover", return_value=[{"project": "memories", "latest": "11.1.4", "update": True}]), \
                 patch("maintainer.run_job") as run:
                delay, failed, deferred = maintainer.poll_releases(args, {"poll_seconds": 21600}, workspace, "coding")
                self.assertTrue(deferred)
                self.assertFalse(failed)
                self.assertLessEqual(delay, 3601)
                run.assert_not_called()

    def test_watch_releases_lock_before_sleeping(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            config = workspace / "config.json"
            config.write_text('{"poll_seconds": 300}')
            owning = []
            @contextlib.contextmanager
            def tracked_lock(path):
                owning.append(True)
                try:
                    yield
                finally:
                    owning.pop()
            def sleep(seconds):
                self.assertEqual(owning, [], "watcher retained the lock during sleep")
                raise KeyboardInterrupt
            with patch.object(sys, "argv", ["maintainer", "watch", "--config", str(config),
                                            "--workspace", str(workspace)]), \
                 patch("maintainer.locked", side_effect=tracked_lock), \
                 patch("maintainer.discover", return_value=[]), \
                 patch("maintainer.time.sleep", side_effect=sleep), \
                 patch.dict(os.environ, {"AUTO_UPDATE": "false"}):
                with self.assertRaises(KeyboardInterrupt):
                    maintainer.main()

    def test_failed_release_never_retries_past_limit(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            (workspace / "state.json").write_text(json.dumps({
                "memories:11.1.4": {"status": "failed", "attempts": 3}}))
            args = SimpleNamespace(action="once", ignore_window=False, publish=False, no_agent=True)
            with patch("maintainer.discover", return_value=[
                    {"project": "memories", "latest": "11.1.4", "update": True}]), \
                 patch("maintainer.run_job") as run:
                _, failed, _ = maintainer.poll_releases(args, {"release_max_attempts": 3}, workspace, "coding")
                self.assertTrue(failed)
                run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
