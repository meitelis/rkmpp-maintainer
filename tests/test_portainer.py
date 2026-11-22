import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import maintainer


class PortainerTests(unittest.TestCase):
    def test_git_managed_stack_has_single_supervised_watcher_and_builder(self):
        root = Path(__file__).resolve().parents[1]
        stack = (root / "portainer-stack.yml").read_text()
        self.assertIn('AUTO_UPDATE: ${RELEASE_WATCH_ENABLED:-true}', stack)
        self.assertIn('AUTO_PUBLISH: ${AUTO_PUBLISH:-false}', stack)
        self.assertNotIn('  qualifier:', stack)
        self.assertIn('  builder:', stack)
        self.assertIn('  maintainer:', stack)
        controller = stack.split('  maintainer:',1)[1].split('\\nnetworks:',1)[0]
        self.assertIn('restart: unless-stopped', controller)
        self.assertIn('pull_policy: never', controller)
        self.assertIn('command: ["${MAINTAINER_ACTION:-watch}"]', controller)
        self.assertNotIn('/var/run/docker.sock', controller)
        conf = json.loads((root / "config.json").read_text())
        self.assertEqual(conf["poll_seconds"], 86400)
        self.assertEqual(conf["work_window"]["timezone"], "Europe/Dublin")
        self.assertEqual(conf["work_window"]["start_hour"], 0)
        self.assertEqual(conf["work_window"]["end_hour"], 8)

    def test_idle_keeps_console_available_without_locking_or_starting_jobs(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "config.json"
            config.write_text(json.dumps({}))
            with patch.object(sys, "argv", ["maintainer", "idle", "--config", str(config),
                                           "--workspace", str(Path(temp) / "workspace")]), \
                 patch("maintainer.time.sleep", side_effect=KeyboardInterrupt), \
                 patch("maintainer.locked") as lock, patch("maintainer.discover") as discover, \
                 patch("maintainer.run_job") as run_job:
                with self.assertRaises(KeyboardInterrupt):
                    maintainer.main()
                lock.assert_not_called()
                discover.assert_not_called()
                run_job.assert_not_called()
