import unittest

from benchmark_timings import summarize


class BenchmarkTimingTests(unittest.TestCase):
    def test_weights_by_phase_duration_and_excludes_replayed_reads(self):
        first = dict(prompt_n=1, prompt_ms=100, predicted_n=5, predicted_ms=500)
        second = dict(prompt_n=11, prompt_ms=900, predicted_n=15, predicted_ms=500)
        report = summarize([{"timings": first}, {"timings": second},
                            {"reused_read": True, "timings": first}, {}])
        self.assertEqual(report, {"recorded_calls": 3, "timed_calls": 2,
                                  "replayed_turns": 1, "prompt_tokens_per_second": 12,
                                  "predicted_tokens_per_second": 20})

    def test_missing_measurements_have_no_rate(self):
        report = summarize([{"timings": {"prompt_n": 20}}, {"timings": None}])
        self.assertEqual(report["timed_calls"], 0)
        self.assertIsNone(report["prompt_tokens_per_second"])
        self.assertIsNone(report["predicted_tokens_per_second"])
