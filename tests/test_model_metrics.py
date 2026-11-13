import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from model_metrics import CgroupSampler


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
