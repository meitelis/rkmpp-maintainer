"""Regression checks for the Qwen3-Coder RAM calibration on ROCK 5B+."""
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from model_settings import memory_bytes, require_model_memory, settings
from schedule import host_idle

GIB = 1024 ** 3
# Host MemAvailable observed at the blocked Qwen preflight on 2026-11-16.
BOARD_AVAILABLE = 20224466944


class QwenBoardMemoryTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((Path(__file__).resolve().parents[1] / "config.json").read_text())
        self.spec, self.runtime, _ = settings(self.config, "coding")

    def test_coding_limit_and_existing_protections(self):
        self.assertEqual(self.runtime["memory"], "17g")
        self.assertTrue(self.runtime["no_swap"])
        stack = (Path(__file__).resolve().parents[1] / "portainer-stack.yml").read_text()
        builder = stack.split("  builder:\n", 1)[1].split("  maintainer:\n", 1)[0]
        self.assertIn("    mem_limit: 19g\n", builder)
        self.assertIn("    memswap_limit: 19g\n", builder)
        self.assertEqual(self.config["work_window"]["memory_headroom_bytes"], GIB)
        self.assertEqual(memory_bytes(self.runtime["memory"]) + GIB, 18 * GIB)
        self.assertLess(self.spec["bytes"] + 1536 * 1024**2, memory_bytes(self.runtime["memory"]))

    def test_preflight_allows_exactly_model_plus_reserve(self):
        meminfo = f"MemAvailable: {18 * GIB // 1024} kB\n"
        with patch("pathlib.Path.read_text", return_value=meminfo):
            result = require_model_memory(self.config, self.spec, self.runtime)
        self.assertEqual(result["estimated_model_and_reserve_bytes"], 18 * GIB)
        self.assertEqual(result["available_before_model_bytes"], 18 * GIB)

    def test_preflight_still_rejects_ram_below_model_plus_reserve(self):
        meminfo = f"MemAvailable: {(18 * GIB - 1024) // 1024} kB\n"
        with patch("pathlib.Path.read_text", return_value=meminfo):
            with self.assertRaisesRegex(RuntimeError, "Host RAM reserved"):
                require_model_memory(self.config, self.spec, self.runtime)

    def test_historical_board_sample_is_admitted_with_safe_headroom(self):
        meminfo = f"MemAvailable: {BOARD_AVAILABLE // 1024} kB\n"
        with patch("schedule.cpu_sample", side_effect=[(100, 80), (200, 180)]), \
             patch("schedule.time.sleep"), \
             patch("schedule.os.getloadavg", return_value=(0.5, 0, 0)), \
             patch("schedule.os.cpu_count", return_value=8), \
             patch("schedule.Path.read_text", return_value=meminfo):
            result = host_idle(self.config)
        self.assertTrue(result["allowed"])
        self.assertEqual(result["required_memory_bytes"], 18 * GIB)
