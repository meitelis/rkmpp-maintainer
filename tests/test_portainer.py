import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import maintainer


class PortainerTests(unittest.TestCase):
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
