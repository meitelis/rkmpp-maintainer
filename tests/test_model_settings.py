import unittest
from model_settings import memory_bytes, settings


class ModelSettingsTests(unittest.TestCase):
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
