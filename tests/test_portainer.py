import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import maintainer


class PortainerTests(unittest.TestCase):
    def test_new_stack_is_fail_closed_until_explicit_autoupdate_opt_in(self):
        root = Path(__file__).resolve().parents[1]
        stack = (root / "portainer-stack.yml").read_text()
        self.assertIn('AUTO_UPDATE: ${AUTO_UPDATE:-false}', stack)
        self.assertIn('AUTO_PUBLISH: ${AUTO_PUBLISH:-false}', stack)
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
