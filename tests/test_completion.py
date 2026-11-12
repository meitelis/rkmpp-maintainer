import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent import AgentError, SCHEMA, complete, run_agent


class CompletionTests(unittest.TestCase):
    def test_performance_metrics_persist_counters_without_reasoning_text(self):
        data={"choices":[{"finish_reason":"stop","message":{"content":'{"action":"list"}'}}],
              "usage":{"completion_tokens":12,"reasoning_content":"private analysis"},
              "timings":{"predicted_n":12,"predicted_ms":1000,"analysis":"private analysis"}}
        metrics={}
        with patch("agent.urllib.request.urlopen",return_value=io.BytesIO(json.dumps(data).encode())):
            complete("http://local",[],metrics=metrics)
        self.assertEqual(metrics["usage"],{"completion_tokens":12})
        self.assertNotIn("private analysis",json.dumps(metrics))

    def response(self, content, finish_reason="stop", **extra):
        return io.BytesIO(json.dumps({"choices": [{"finish_reason": finish_reason,
                            "message": {"content": content, **extra}}]}).encode())

    def test_reasoning_is_separate_from_the_json_action(self):
        action = '{"action":"finish","summary":"done"}'
        with patch("agent.urllib.request.urlopen", return_value=self.response(
                action, reasoning_content="internal analysis")) as request:
            result = complete("http://local/", [{"role": "user", "content": "fix"}], settings={
                "temperature": 0.6, "max_tokens": 4096, "top_p": 0.95,
                "reasoning_format": "deepseek", "chat_template_kwargs": {"enable_thinking": True},
                "messages": [], "response_format": {"type": "text"}})
        self.assertEqual(result, action)
        payload = json.loads(request.call_args.args[0].data)
        self.assertEqual(payload["messages"], [{"role": "user", "content": "fix"}])
        self.assertEqual(payload["response_format"]["schema"], SCHEMA)
        self.assertEqual(payload["max_tokens"], 4096)
        self.assertEqual(payload["reasoning_format"], "deepseek")
        self.assertTrue(payload["chat_template_kwargs"]["enable_thinking"])
        self.assertNotIn("internal analysis", result)

    def test_truncated_or_reasoning_only_output_cannot_be_an_action(self):
        cases = [(None, "stop", "no action"), ("  ", "stop", "no action"),
                 ('{"action":"finish"}', "length", "truncated"),
                 ('<think>analysis</think>{"action":"finish"}', "stop", "Unparsed reasoning")]
        for content, reason, error in cases:
            with self.subTest(content=content, reason=reason), \
                 patch("agent.urllib.request.urlopen", return_value=self.response(
                     content, reason, reasoning_content="internal analysis")):
                with self.assertRaisesRegex(AgentError, error):
                    complete("http://local", [])

    def test_thinking_tags_inside_a_source_edit_are_allowed(self):
        action = json.dumps({"action": "replace", "path": "go-vod/source.go",
                             "old": "old", "new": "<think>"})
        with patch("agent.urllib.request.urlopen", return_value=self.response(action)):
            self.assertEqual(complete("http://local", []), action)

    def test_tool_loop_uses_configured_generation_and_logs_only_action(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            transcript = root / "agent.jsonl"
            settings = {"temperature": 0.6, "max_tokens": 4096}
            (root / "go-vod").mkdir()
            (root / "go-vod/file.go").write_text("source")
            with patch("agent.complete", side_effect=[
                    '{"action":"read","path":"go-vod/file.go"}',
                    '{"action":"finish","summary":"done","review_complete":true,"blockers":[]}']) as send:
                review = run_agent(root, root, "fix", lambda _: "", "http://local",
                                   "memories", transcript, generation=settings)
                self.assertEqual(review["summary"], "done")
            self.assertEqual(send.call_args.kwargs["settings"], settings)
            record = json.loads(transcript.read_text().splitlines()[-1])
            self.assertTrue({"turn", "response", "timestamp", "result"}.issubset(record))
            self.assertNotIn("reasoning_content", record)
            self.assertEqual(json.loads(record["response"])["action"], "finish")
