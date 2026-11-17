import unittest
from unittest.mock import patch
from model_settings import memory_bytes, require_model_memory, settings


class ModelSettingsTests(unittest.TestCase):
    def test_model_start_safety_is_independent_of_a_schedule_override(self):
        config = {"work_window": {"memory_headroom_bytes": 1024}}
        spec = {"bytes": 4096}
        runtime = {"estimated_overhead_bytes": 2048}
        with patch("pathlib.Path.read_text", return_value="MemAvailable: 6 kB\n"):
            with self.assertRaisesRegex(RuntimeError, "Host RAM reserved"):
                require_model_memory(config, spec, runtime)
        with patch("pathlib.Path.read_text", return_value="MemAvailable: 8 kB\n"):
            evidence = require_model_memory(config, spec, runtime)
        self.assertEqual(evidence["available_before_model_bytes"], 8192)
        self.assertEqual(evidence["estimated_model_and_reserve_bytes"], 7168)

    def test_larger_coding_profile_does_not_change_ci_control(self):
        config={"model_runtime":{"memory":"12g","threads":4},
            "generation":{"temperature":0.6,"chat_template_kwargs":{"enable_thinking":True}},
            "models":{"smoke":{"filename":"control"},"coding":{"filename":"larger",
                "runtime":{"memory":"18g","cpu_set":"4-7","cache_type_k":"q8_0"},
                "generation":{"temperature":1.0,"reasoning_effort":"medium"}}}}
        _, control, control_generation=settings(config,"smoke")
        spec, larger, generation=settings(config,"coding")
        self.assertEqual(control,{"memory":"12g","threads":4})
        self.assertEqual(control_generation["temperature"],0.6)
        self.assertEqual(larger["memory"],"18g")
        self.assertEqual(larger["cpu_set"],"4-7")
        self.assertTrue(generation["chat_template_kwargs"]["enable_thinking"])
        self.assertEqual(generation["reasoning_effort"],"medium")
        self.assertEqual(spec,{"filename":"larger"})
        self.assertEqual(config["model_runtime"]["memory"],"12g")

    def test_docker_memory_limit_is_converted_without_using_decimal_gb(self):
        self.assertEqual(memory_bytes("18g"),18*1024**3)
        with self.assertRaises(ValueError):memory_bytes("18g; bad")
