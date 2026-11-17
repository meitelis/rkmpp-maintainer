"""The production controller and CI execute on Linux with POSIX alarms."""
import tempfile
import time
import unittest
from limits import BudgetExceeded, cleanup_budget, job_budget


class HardDeadlineTests(unittest.TestCase):
    def test_deadline_interrupts_blocking_non_subprocess_work(self):
        with tempfile.TemporaryDirectory() as temp:
            started = time.monotonic()
            with self.assertRaisesRegex(BudgetExceeded, "deadline"):
                with job_budget({"job_timeout_seconds":0.05, "disk_reserve_bytes":0}, temp):
                    time.sleep(3)
            self.assertLess(time.monotonic() - started, 1.5)

    def test_nested_budget_cannot_extend_parent_deadline(self):
        with tempfile.TemporaryDirectory() as temp:
            started = time.monotonic()
            with self.assertRaisesRegex(BudgetExceeded, "deadline"):
                with job_budget({"job_timeout_seconds":0.05, "disk_reserve_bytes":0}, temp):
                    with job_budget({"job_timeout_seconds":10, "disk_reserve_bytes":0}, temp):
                        time.sleep(3)
            self.assertLess(time.monotonic() - started, 1.5)

    def test_cleanup_is_not_interrupted_by_expiring_job_alarm(self):
        with tempfile.TemporaryDirectory() as temp:
            removed = False
            with self.assertRaisesRegex(BudgetExceeded, "deadline"):
                with job_budget({"job_timeout_seconds":0.05, "disk_reserve_bytes":0}, temp):
                    with cleanup_budget():
                        time.sleep(0.1)
                        removed = True
            self.assertTrue(removed)


if __name__ == "__main__":
    unittest.main()
