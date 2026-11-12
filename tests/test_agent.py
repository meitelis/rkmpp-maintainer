import json
import tempfile
import unittest
from pathlib import Path
from agent import AgentError, Files, editable, run_agent


class FileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "go-vod").mkdir()
        self.file = self.root / "go-vod/source.go"
        self.file.write_text("return old\n")
        self.files = Files(self.root, lambda p: editable("memories", p))

    def test_path_and_symlink_escape(self):
        for name in ["../secret", "/etc/passwd", ".git/config", ".env"]:
            with self.assertRaises(AgentError): self.files.read(name)
        (self.root / "go-vod/link").symlink_to(self.file)
        with self.assertRaises(AgentError): self.files.read("go-vod/link")

    def test_read_before_edit_and_exact_context(self):
        with self.assertRaises(AgentError): self.files.replace("go-vod/source.go", "old", "new")
        self.files.read("go-vod/source.go")
        with self.assertRaises(AgentError): self.files.replace("go-vod/source.go", "absent", "new")
        self.files.replace("go-vod/source.go", "old", "new")
        self.assertEqual(self.file.read_text(), "return new\n")

    def test_checks_and_maintainer_are_protected(self):
        for name in ["go-vod/source_test.go", "tests/foo.js", ".github/workflows/check.yml",
                     "scripts/build-v11.sh", "go-vod/Dockerfile.rkmpp", "automation/agent.py",
                     "overlays/server/foo.spec.ts"]:
            self.assertFalse(editable("memories", name), name)
            self.assertFalse(editable("immich", name), name)

    def test_real_tool_loop_edits_only_source(self):
        actions = iter([{"action":"read", "path":"go-vod/source.go"},
                        {"action":"replace", "path":"go-vod/source.go", "old":"old", "new":"new"},
                        {"action":"finish", "summary":"Updated source"}])
        summary = run_agent(self.root, self.root, "upgrade", lambda _: "", "http://local",
                            "memories", self.root / "transcript.jsonl",
                            responder=lambda *_: json.dumps(next(actions)))
        self.assertEqual(summary, "Updated source")
        self.assertEqual(self.file.read_text(), "return new\n")

    def test_bad_model_output_is_bounded(self):
        with self.assertRaises(AgentError):
            run_agent(self.root, self.root, "upgrade", lambda _: "", "http://local", "memories",
                      self.root / "transcript.jsonl", turns=2, responder=lambda *_: "not json")
