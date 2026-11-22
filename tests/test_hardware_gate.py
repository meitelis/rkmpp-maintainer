import json
import tempfile
import unittest
from pathlib import Path

from hardware_gate import required_immich_cases, validate_immich_report, validate_memories_matrix


class HardwareGateTests(unittest.TestCase):
    def _immich(self, root, cases=None, **kwargs):
        report = Path(root) / "run-123" / "report.json"
        report.parent.mkdir(exist_ok=True)
        cases = [{"name": name, "ok": True} for name in sorted(required_immich_cases())] if cases is None else cases
        report.write_text(json.dumps({"schemaVersion": 1, "label": "candidate-1234",
            "quick": False, "cases": cases, "passed": len(cases), "failed": 0, "ok": True, **kwargs}))
        return report

    def test_exact_38_case_matrix_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            self._immich(directory)
            result = validate_immich_report(directory, "candidate-1234")
            self.assertEqual(result["passed"], 38)
            self.assertEqual(result["required_cases"], 38)
            self.assertTrue(result["ok"])

    def test_missing_or_duplicate_case_rejected(self):
        names = sorted(required_immich_cases())
        for cases in ([{"name": name, "ok": True} for name in names[:-1]],
                      [{"name": name, "ok": True} for name in names[:-1]] +
                      [{"name": names[0], "ok": True}]):
            with tempfile.TemporaryDirectory() as directory:
                self._immich(directory, cases)
                with self.assertRaisesRegex(RuntimeError, "missing|duplicate"):
                    validate_immich_report(directory)

    def test_false_result_and_inconsistent_totals_rejected(self):
        names = sorted(required_immich_cases())
        with tempfile.TemporaryDirectory() as directory:
            cases = [{"name": name, "ok": True} for name in names]
            cases[0]["ok"] = False
            self._immich(directory, cases)
            with self.assertRaisesRegex(RuntimeError, "failing"):
                validate_immich_report(directory)
        with tempfile.TemporaryDirectory() as directory:
            self._immich(directory, passed=37)
            with self.assertRaisesRegex(RuntimeError, "inconsistent"):
                validate_immich_report(directory)

    def test_missing_or_ambiguous_reports_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "exactly one"):
                validate_immich_report(directory)
            self._immich(directory)
            extra = Path(directory) / "run-456"
            extra.mkdir()
            (extra / "report.json").write_text("{}")
            with self.assertRaisesRegex(RuntimeError, "exactly one"):
                validate_immich_report(directory)

    def test_memories_rejects_skip_and_missing_suites(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "environment.txt").write_text("go-vod 0.7.3\n")
            for suite in ("ffmpeg", "core", "api"):
                (root / f"{suite}-tests.log").write_text("--- PASS: TestRKMPPHardwareExample (0.01s)\nPASS\n")
            self.assertEqual(validate_memories_matrix(root)["passed"], 3)
            (root / "ffmpeg-tests.log").write_text("--- SKIP: TestRKMPPHardwareExample\nPASS\n")
            with self.assertRaisesRegex(RuntimeError, "skipped"):
                validate_memories_matrix(root)
            (root / "ffmpeg-tests.log").unlink()
            with self.assertRaisesRegex(RuntimeError, "Missing"):
                validate_memories_matrix(root)


if __name__ == "__main__":
    unittest.main()
