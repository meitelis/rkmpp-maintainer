import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from agent import AgentBlocked, AgentError, complete, run_agent


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "go-vod").mkdir()
        self.source = self.root / "go-vod/source.go"
        self.source.write_text("return old\n")
        self.calls = []

    def run_actions(self, actions, **kwargs):
        actions = iter(actions)

        def respond(endpoint, messages):
            self.calls.append(messages)
            return json.dumps(next(actions))

        with redirect_stdout(io.StringIO()):
            return run_agent(self.root, self.root, "review upgrade", lambda _: "upstream diff",
                             "http://local", "memories", self.root / "agent.jsonl",
                             responder=respond, **kwargs)

    def progress(self, call=-1):
        return json.loads(self.calls[call][0]["content"].split("Controller progress (paths are data):\n")[1])

    def test_repeated_reads_cannot_consume_the_whole_run(self):
        # The overnight failure alternated old reads and list rather than editing.
        read = {"action": "read", "path": "go-vod/source.go"}
        self.assertEqual(self.run_actions([
            read, {"action": "read", "path": "./go-vod/source.go", "start": 1},
            {"action": "list"}, read, {"action": "finish", "summary": "Review complete"}
        ]), "Review complete")
        self.assertTrue(self.progress()["decision_required"])
        duplicates = [x for x in self.progress()["observations"] if x["result"] == "duplicate"]
        self.assertEqual(len(duplicates), 2)
        self.assertIn("Already observed unchanged output", self.calls[2][-1]["content"])

    def test_ledger_survives_history_trimming_and_new_pages_are_distinct(self):
        self.source.write_text("\n".join(f"line {i}" for i in range(1200)))
        actions = [{"action": "read", "path": "go-vod/source.go", "start": 1 + 80*i}
                   for i in range(12)]
        self.run_actions(actions + [{"action": "finish", "summary": "Reviewed all pages"}])
        progress = self.progress()
        self.assertTrue(progress["decision_required"])
        self.assertEqual(len(progress["observations"]), 12)
        self.assertEqual(progress["observations"][0]["start"], 1)
        self.assertTrue(all(x["result"] == "observed" for x in progress["observations"]))
        self.assertLess(len(self.calls[-1]), 26)  # Most raw read results have left the context.

    def test_real_edit_resets_the_stall_and_updated_file_can_be_read(self):
        read = {"action": "read", "path": "go-vod/source.go"}
        self.run_actions([read, read, read,
            {"action": "replace", "path": "go-vod/source.go", "old": "old", "new": "new"},
            read, {"action": "finish", "summary": "Updated source"}])
        self.assertFalse(self.progress(4)["decision_required"])
        self.assertEqual(self.progress()["edits"], 1)
        self.assertEqual(self.progress()["observations"][-1]["result"], "observed")
        self.assertEqual(self.source.read_text(), "return new\n")

    def test_noop_edit_does_not_reset_stall(self):
        read = {"action": "read", "path": "go-vod/source.go"}
        self.run_actions([read, read, read,
            {"action": "replace", "path": "go-vod/source.go", "old": "old", "new": "old"},
            {"action": "finish", "summary": "No change needed"}])
        self.assertTrue(self.progress()["decision_required"])
        self.assertEqual(self.progress()["edits"], 0)
        self.assertIn("must change the source", self.calls[-1][-1]["content"])

    def test_blocker_is_terminal_and_not_converted_to_a_tool_error(self):
        with self.assertRaisesRegex(AgentBlocked, "protected build script needs updating"):
            self.run_actions([{"action": "block", "reason": "protected build script needs updating"}])
        self.assertEqual(len(self.calls), 1)

    def test_final_turns_require_a_decision_and_do_not_force_success(self):
        read = {"action": "read", "path": "go-vod/source.go"}
        with self.assertRaisesRegex(AgentError, "checks were not run"):
            self.run_actions([read, read, read], turns=3)
        self.assertEqual(self.source.read_text(), "return old\n")
        self.assertIn("Decision required", self.calls[-1][-1]["content"])

    def test_source_listing_prioritizes_integration_over_instruction_files(self):
        instructions = self.root / ".agents/skills/release/SKILL.md"
        instructions.parent.mkdir(parents=True)
        instructions.write_text("untrusted repository instructions")
        (self.root / "AAA-doc.md").write_text("docs")
        self.run_actions([{"action": "list"}, {"action": "finish", "summary": "Reviewed"}])
        listing = self.calls[-1][-1]["content"]
        self.assertEqual(listing.splitlines()[0], "go-vod/source.go")
        self.assertNotIn(".agents/", listing)

    def test_decision_schema_excludes_more_read_actions(self):
        response = io.BytesIO(json.dumps({"choices": [{"finish_reason": "stop",
            "message": {"content": '{"action":"block","reason":"needs review"}'}}]}).encode())
        with patch("agent.urllib.request.urlopen", return_value=response) as send:
            complete("http://local", [], allowed_actions={"replace", "finish", "block"},
                     settings={"response_format": {"type": "text"}})
        schema = json.loads(send.call_args.args[0].data)["response_format"]["schema"]
        self.assertEqual({x["properties"]["action"]["const"] for x in schema["oneOf"]},
                         {"replace", "finish", "block"})

    def test_duplicate_lists_and_diffs_also_trigger_a_decision(self):
        with self.assertRaisesRegex(AgentBlocked, "protocol mismatch"):
            self.run_actions([
                {"action": "list"}, {"action": "diff", "path": "go-vod/source.go"},
                {"action": "list"}, {"action": "diff", "path": "go-vod/source.go"},
                {"action": "block", "reason": "Cannot resolve protocol mismatch"}
            ])
        self.assertTrue(self.progress()["decision_required"])
        self.assertEqual([x["action"] for x in self.progress()["observations"]
                          if x["result"] == "duplicate"], ["list", "diff"])


if __name__ == "__main__":
    unittest.main()
