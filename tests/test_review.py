import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from agent import AgentBlocked, AgentError, completed_review, run_agent
from repair_fixture import BROKEN, REFERENCE, create_fixture


class ReviewTests(unittest.TestCase):
    def test_truncated_views_do_not_complete_required_observations(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            name = "go-vod/main.go"
            (root/"go-vod").mkdir()
            (root/name).write_text("header\n" * 200 + "important tail\n")
            actions = iter([{"action":"read", "path":name}, {"action":"diff", "path":name},
                {"action":"finish", "summary":"Reviewed", "review_complete":True, "blockers":[]},
                {"action":"block", "reason":"Changed sections remain unread"}])
            calls = []
            def respond(endpoint, messages):
                calls.append(messages)
                return json.dumps(next(actions))
            with redirect_stdout(io.StringIO()), self.assertRaises(AgentBlocked):
                run_agent(root, root, "review", lambda _: "diff header\n" * 200 + "changed tail\n",
                    "local", "memories", root/"agent.jsonl", turns=8, responder=respond,
                    required_observations={"read":[name], "diff":[name]})
            self.assertIn("Cannot finish", calls[-1][-2]["content"])

    def test_full_diff_and_file_pagination_can_complete_review(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            name = "go-vod/main.go"
            (root/"go-vod").mkdir()
            (root/name).write_text("header\n" * 200 + "important tail\n")
            actions = [{"action":tool, "path":name, "start":start}
                       for tool in ("read", "diff") for start in (1,80,159)]
            actions.append({"action":"finish", "summary":"Reviewed all sections", "review_complete":True, "blockers":[]})
            actions = iter(actions)
            with redirect_stdout(io.StringIO()):
                review = run_agent(root, root, "review", lambda _: "diff header\n" * 200 + "changed tail\n",
                    "local", "memories", root/"agent.jsonl", turns=8,
                    responder=lambda *_:json.dumps(next(actions)), required_observations={"read":[name], "diff":[name]})
            self.assertTrue(review["review_complete"])
            records = [json.loads(line) for line in (root/"agent.jsonl").read_text().splitlines()]
            self.assertIn("important tail", records[2]["result"])
            self.assertIn("changed tail", records[5]["result"])

    def test_impossible_review_budget_fails_before_generation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root/"long.go").write_text("line\n" * 500)
            responder = __import__("unittest.mock", fromlist=["Mock"]).Mock()
            with self.assertRaisesRegex(AgentBlocked, "at least"):
                run_agent(root, root, "review", lambda _:"", "local", "memories", root/"agent.jsonl",
                    turns=4, responder=responder, required_observations={"read":["long.go"]})
            responder.assert_not_called()

    def test_radxa_action_sequence_can_recover_missing_upstream_read(self):
        with tempfile.TemporaryDirectory() as temp:
            root, upstream = Path(temp)/"local", Path(temp)/"official"
            source = create_fixture(root, upstream)
            name = "go-vod/http_options.py"
            actions = iter([
                {"action":"read", "path":name},
                {"action":"read", "path":"upstream/" + name},
                {"action":"list"},
                {"action":"read", "path":"tests/test_http_options.py"},
                {"action":"replace", "path":name,
                 "old":"set(ffprobe_help.split())", "new":"set(ffmpeg_help.split())"},
                {"action":"replace", "path":name,
                 "old":"set(ffmpeg_help.split())",
                 "new":"set(ffmpeg_help.split()) & set(ffprobe_help.split())"},
                {"action":"upstream", "path":"./" + name},
                {"action":"finish", "summary":"Repaired both-binary capability intersection",
                 "review_complete":True, "blockers":[]},
            ])
            calls = []
            def respond(endpoint, messages, **kwargs):
                calls.append((messages, kwargs["allowed_actions"]))
                return json.dumps(next(actions))
            with redirect_stdout(io.StringIO()), patch("agent.complete", side_effect=respond):
                review = run_agent(root, upstream, "repair", lambda _: "", "local", "memories",
                    root/"agent.jsonl", turns=8, require_edit=True,
                    required_observations={"read":[name], "upstream":[name]})
            self.assertIn('"action": "upstream"', calls[2][0][-2]["content"])
            self.assertIn("upstream", calls[6][1])
            self.assertNotIn("finish", calls[6][1])
            self.assertIn("finish", calls[7][1])
            self.assertIn(("upstream", name), review["observations"])
            self.assertEqual(source.read_text(), REFERENCE)
            checked = subprocess.run([sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests"],
                                     cwd=root, capture_output=True, text=True)
            self.assertEqual(checked.returncode, 0, checked.stderr)
            self.assertIn("Ran 5 tests", checked.stderr)

    def test_recovery_does_not_allow_unrequired_reads_during_decision(self):
        with tempfile.TemporaryDirectory() as temp:
            root, upstream = Path(temp)/"local", Path(temp)/"official"
            create_fixture(root, upstream)
            name = "go-vod/http_options.py"
            (upstream/"other.py").write_text("unrelated")
            actions = iter([{"action":"read", "path":name},
                            {"action":"upstream", "path":"other.py"},
                            {"action":"block", "reason":"Missing required upstream observation"}])
            calls = []
            def respond(*args):
                calls.append(args[1])
                return json.dumps(next(actions))
            with redirect_stdout(io.StringIO()), self.assertRaises(AgentBlocked):
                run_agent(root, upstream, "repair", lambda _: "", "local", "memories",
                    root/"agent.jsonl", turns=3, responder=respond,
                    required_observations={"read":[name], "upstream":[name]})
            self.assertIn("Decision required", calls[-1][-2]["content"])

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
            self.assertIn("Cannot finish", calls[2][-2]["content"])
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
            self.assertIn("required source edit", calls[-1][-2]["content"])
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
