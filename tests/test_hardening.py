"""Guard regressions that could falsely advertise an unattended candidate."""
import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent import AgentBlocked, page
from core import download_model
from evidence import source_digest
from limits import restore_alarm
from model_settings import require_model_memory
from schedule import admission


class HardeningTests(unittest.TestCase):
    def test_paged_source_cannot_loop_forever_on_boundary_line(self):
        # Number and final newline must both fit, otherwise page_count
        # could return next_start unchanged for a 9,497-character source line.
        with self.assertRaises(AgentBlocked):
            page("x" * 9497)

    def test_aborted_model_download_deletes_partial_file(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "coding.gguf"
            partial = target.with_suffix(".part")
            partial.write_bytes(b"stale")
            spec = {"bytes": 3, "sha256": hashlib.sha256(b"abc").hexdigest(),
                    "url": "https://example.test/model.gguf"}
            with patch("core.require_space"), patch("core.urllib.request.urlopen") as opener:
                opener.return_value.__enter__.return_value.read.side_effect = OSError("connection dropped")
                with self.assertRaisesRegex(OSError, "connection dropped"):
                    download_model(spec, target)
            self.assertFalse(partial.exists())
            self.assertFalse(target.exists())

    def test_oversized_download_deletes_partial_file(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "coding.gguf"
            spec = {"bytes": 3, "sha256": hashlib.sha256(b"abc").hexdigest(),
                    "url": "https://example.test/model.gguf"}
            with patch("core.require_space"), patch("core.urllib.request.urlopen") as opener:
                opener.return_value.__enter__.return_value.read.side_effect = [b"abcd", b""]
                with self.assertRaisesRegex(RuntimeError, "pinned size"):
                    download_model(spec, target)
            self.assertFalse(target.with_suffix(".part").exists())

    def test_qualification_identity_covers_verification_inputs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "worker.py").write_text("value=1\n")
            (root / "Dockerfile").write_text("FROM scratch\n")
            (root / "tests").mkdir()
            (root / "tests/test_worker.py").write_text("assert True\n")
            (root / "scripts").mkdir()
            (root / "scripts/build.sh").write_text("#!/bin/sh\ntrue\n")
            before = source_digest(root)
            (root / "tests/test_worker.py").write_text("assert False\n")
            self.assertNotEqual(before, source_digest(root))
            middle = source_digest(root)
            (root / "Dockerfile").write_text("FROM alpine\n")
            self.assertNotEqual(middle, source_digest(root))
            middle = source_digest(root)
            (root / "scripts/build.sh").write_text("#!/bin/sh\nfalse\n")
            self.assertNotEqual(middle, source_digest(root))
            # Output files are not qualification inputs.
            middle = source_digest(root)
            (root / "data").mkdir()
            (root / "data/result.json").write_text('{"status":"failed"}')
            self.assertEqual(middle, source_digest(root))

    def test_runtime_memory_ceiling_is_not_underbudgeted(self):
        config = {"work_window":{"memory_headroom_bytes":1024}}
        spec = {"bytes":4096}
        runtime = {"memory":"8192","estimated_overhead_bytes":2048}
        with patch("pathlib.Path.read_text",return_value="MemAvailable: 8 kB\n"):
            with self.assertRaisesRegex(RuntimeError,"Host RAM reserved"):
                require_model_memory(config,spec,runtime)
        with patch("pathlib.Path.read_text",return_value="MemAvailable: 10 kB\n"):
            evidence = require_model_memory(config,spec,runtime)
        self.assertEqual(evidence["estimated_model_and_reserve_bytes"],9216)

    def test_expired_prior_alarm_is_rearmed(self):
        with patch("limits.signal.setitimer") as timer, patch("limits.signal.signal"), \
             patch("limits.time.monotonic",return_value=20.0):
            restore_alarm(object(), (5.0,0.0), 10.0)
        self.assertEqual(timer.call_args_list[-1].args[1],0.001)

    def test_schedule_omitted_enabled_key_still_checks_idle(self):
        with patch("schedule.window",return_value={"allowed":True}), \
             patch("schedule.host_idle",return_value={"allowed":False,"reason":"busy"}) as idle:
            result=admission({"work_window":{"idle_only":True}})
        self.assertFalse(result["allowed"])
        idle.assert_called_once()


if __name__ == "__main__":
    unittest.main()
