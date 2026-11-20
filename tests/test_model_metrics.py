import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from model_metrics import CgroupSampler, require_enforced_memory_limits


class ModelMetricsTests(unittest.TestCase):
    def test_an_oom_exit_cannot_erase_the_last_alive_memory_sample(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/"memory.json"
            read=Mock(side_effect=["memory.current=100\nmemory.peak=120\nmemory.max=128\n",
                                   RuntimeError("container stopped after OOM")])
            sampler=CgroupSampler(read,path,{"memory":"128"})
            sampler.sample()
            sampler.sample()
            self.assertEqual(sampler.last["memory.peak"],"120")
            self.assertEqual(json.loads(path.read_text())["cgroup_memory"]["memory.peak"],"120")
            self.assertEqual(sampler.samples,1)
            self.assertIn("OOM",sampler.errors[0])

    def test_cgroup_v1_requires_real_memsw_enforcement(self):
        gib = 1024 ** 3
        good = {"memory/memory.limit_in_bytes": str(17*gib),
                "memory/memory.memsw.limit_in_bytes": str(17*gib)}
        self.assertTrue(require_enforced_memory_limits(good, "17g", True)["no_swap_enforced"])
        unlimited = dict(good, **{"memory/memory.memsw.limit_in_bytes": str(2**63-4096)})
        with self.assertRaisesRegex(RuntimeError, "no-swap limit not enforced"):
            require_enforced_memory_limits(unlimited, "17g", True)
        with self.assertRaisesRegex(RuntimeError, "RAM limit not enforced"):
            require_enforced_memory_limits(good, "16g", True)

    def test_cgroup_v2_swap_zero_and_missing_fields(self):
        self.assertTrue(require_enforced_memory_limits(
            {"memory.max": str(17*1024**3), "memory.swap.max": "0"}, "17g", True
        )["no_swap_enforced"])
        with self.assertRaisesRegex(RuntimeError, "no-swap limit not enforced"):
            require_enforced_memory_limits(
                {"memory.max": str(17*1024**3), "memory.swap.max": "max"}, "17g", True)
        with self.assertRaisesRegex(RuntimeError, "Cannot verify"):
            require_enforced_memory_limits({"memory.max": "max"}, "17g", True)
