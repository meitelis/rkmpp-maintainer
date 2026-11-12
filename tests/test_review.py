import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from agent import AgentBlocked, AgentError, completed_review, run_agent
from repair_fixture import BROKEN, REFERENCE, create_fixture


class ReviewTests(unittest.TestCase):
    def test_observed_false_finish_is_rejected_even_with_complete_flag(self):
        summary = ("The BinExt.php path does not exist in the local repository. "
                   "Reporting pause pending investigation of the BinExt.php discrepancy and version target. "
                   "I will continue next turn once I read upstream-version.json.")
        with self.assertRaisesRegex(AgentBlocked, "Incomplete review"):
            completed_review({"summary": summary, "review_complete": True, "blockers": []})

    def test_absent_or_incomplete_structured_review_fails_closed(self):
        for extra in [{}, {"review_complete": False, "blockers": []},
                      {"review_complete": "true", "blockers": []},
                      {"review_complete": True, "blockers": ["protocol mismatch"]}]:
            with self.subTest(extra=extra), self.assertRaises(AgentBlocked):
                completed_review({"summary": "Review report", **extra})

    def test_finish_requires_observations_in_the_correct_repository(self):
        with tempfile.TemporaryDirectory() as temp:
            root, upstream = Path(temp)/"local", Path(temp)/"official"
            root.mkdir(); upstream.mkdir()
            (root/"source.py").write_text("local source")
            (upstream/"protocol.py").write_text("official protocol")
            finish = {"action":"finish", "summary":"Review complete", "review_complete":True, "blockers":[]}
            actions = iter([{"action":"read", "path":"source.py"}, finish,
                            {"action":"upstream", "path":"protocol.py"}, finish])
            calls = []
            def respond(endpoint, messages):
                calls.append(messages)
                return json.dumps(next(actions))
            with redirect_stdout(io.StringIO()):
                review = run_agent(root, upstream, "review", lambda _: "", "local", "memories",
                    root/"agent.jsonl", responder=respond, turns=8,
                    required_observations={"read":["source.py"], "upstream":["protocol.py"]})
            self.assertIn("Cannot finish", calls[2][-1]["content"])
            self.assertIn(("upstream", "protocol.py"), review["observations"])
            self.assertEqual(review["assessment"], "model-self-report")

    def test_repair_exercise_cannot_finish_without_a_real_edit(self):
        with tempfile.TemporaryDirectory() as temp:
            root, upstream = Path(temp)/"local", Path(temp)/"official"
            source = create_fixture(root, upstream)
            actions = iter([{"action":"read", "path":"go-vod/http_options.py"},
                {"action":"finish", "summary":"Reviewed", "review_complete":True, "blockers":[]},
                {"action":"block", "reason":"Unable to repair"}])
            calls = []
            def respond(endpoint, messages):
                calls.append(messages)
                return json.dumps(next(actions))
            with redirect_stdout(io.StringIO()), self.assertRaises(AgentBlocked):
                run_agent(root, upstream, "repair", lambda _: "", "local", "memories",
                    root/"agent.jsonl", responder=respond, require_edit=True)
            self.assertIn("required source edit", calls[-1][-1]["content"])
            self.assertEqual(source.read_text(), BROKEN)

    def test_behavioral_checks_fail_on_seed_and_accept_a_correct_repair(self):
        with tempfile.TemporaryDirectory() as temp:
            root, upstream = Path(temp)/"local", Path(temp)/"official"
            source = create_fixture(root, upstream)
            def check():
                return subprocess.run([sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests", "-v"],
                    cwd=root, capture_output=True, text=True)
            broken = check()
            self.assertNotEqual(broken.returncode, 0)
            self.assertIn("FAIL: test_option_must_exist_in_both_binaries", broken.stderr)
            self.assertIn("FAIL: test_missing_help_from_either_binary_rejects_options", broken.stderr)
            source.write_text(REFERENCE)
            corrected = check()
            self.assertEqual(corrected.returncode, 0, corrected.stderr)
            self.assertIn("Ran 5 tests", corrected.stderr)


if __name__ == "__main__":
    unittest.main()
