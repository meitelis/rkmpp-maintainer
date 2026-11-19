"""Regression checks for llama.cpp prompt-cache reuse and bounded compaction."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent import AgentBlocked, bounded_messages, run_agent


class PromptCacheTests(unittest.TestCase):
    def test_previous_request_is_exact_prefix_of_next_request(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "go-vod").mkdir()
            (root / "go-vod/source.go").write_text("old source\n")
            requests = []
            actions = iter([
                {"action": "read", "path": "go-vod/source.go"},
                {"action": "upstream", "path": "go-vod/source.go"},
                {"action": "finish", "summary": "Reviewed", "review_complete": True, "blockers": []}
            ])

            def respond(_endpoint, messages):
                requests.append(messages)
                return json.dumps(next(actions))

            review = run_agent(root, root, "upgrade", lambda _: "", "http://local",
                               "memories", root / "agent.jsonl", responder=respond,
                               required_observations={"read": ["go-vod/source.go"],
                                                      "upstream": ["go-vod/source.go"]})
            self.assertTrue(review["review_complete"])
            self.assertEqual(len(requests), 3)
            for earlier, later in zip(requests, requests[1:]):
                self.assertEqual(later[:len(earlier)], earlier,
                                 "KV cache prefix must be byte-for-byte reusable")
                self.assertEqual(later[len(earlier)]["role"], "assistant")
                self.assertEqual(later[len(earlier) + 1]["role"], "user")
                self.assertEqual(later[-1]["role"], "user")
            records = [json.loads(line) for line in (root / "agent.jsonl").read_text().splitlines()]
            self.assertTrue(all(record["compacted_exchanges"] == 0 for record in records))

    def test_compaction_removes_only_whole_old_exchanges(self):
        prefix = [{"role": "system", "content": "System instructions"},
                  {"role": "user", "content": "Review task"}]
        old = []
        for i in range(5):
            old += [{"role": "user", "content": f"Controller progress turn {i}"},
                    {"role": "assistant", "content": f"Action {i}"},
                    {"role": "user", "content": f"Tool result {i}: " + "x" * 700}]
        progress = {"role": "user", "content": "New progress"}
        with patch("agent.MAX_PROMPT_CHARS", 1700):
            messages, removed = bounded_messages(prefix, old, progress)
        self.assertGreater(removed, 0)
        self.assertEqual(len(old) % 3, 0)
        self.assertEqual(messages[:2], prefix)
        self.assertEqual(messages[-1], progress)
        self.assertLessEqual(sum(len(msg["content"]) for msg in messages), 1700)
        retained = old[0]["content"]
        self.assertTrue(retained.startswith("Controller progress turn "))
        # The most recent action/result must survive compaction.
        self.assertTrue(any(m["content"].startswith("Tool result 4:") for m in messages))

    def test_compaction_leaves_room_for_incremental_turns(self):
        def exchange(i):
            return [{"role": "user", "content": str(i)},
                    {"role": "assistant", "content": "read"},
                    {"role": "user", "content": "x" * 3500}]
        history = [m for i in range(10) for m in exchange(i)]
        progress = {"role": "user", "content": "review"}
        messages, dropped = bounded_messages([], history, progress)
        self.assertGreater(dropped, 0)
        self.assertLessEqual(sum(len(m["content"]) for m in messages), 12000)
        self.assertEqual(history[-3:], exchange(9))
        history.extend([progress, *exchange(10)[1:]])
        following, dropped = bounded_messages([], history, progress)
        self.assertEqual(dropped, 0)
        self.assertEqual(following[:len(messages)], messages)

    def test_oversize_single_request_fails_closed(self):
        prefix = [{"role": "system", "content": "x" * 500}]
        with patch("agent.MAX_PROMPT_CHARS", 100):
            with self.assertRaisesRegex(AgentBlocked, "bounded context"):
                bounded_messages(prefix, [], {"role": "user", "content": "progress"})

    def test_incomplete_exchange_cannot_be_compacted(self):
        with self.assertRaisesRegex(ValueError, "incomplete exchange"):
            bounded_messages([], [{"role": "assistant", "content": "dangling"}],
                             {"role": "user", "content": "progress"})


if __name__ == "__main__":
    unittest.main()
