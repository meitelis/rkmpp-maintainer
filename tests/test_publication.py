import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from maintainer import candidate_tree, git, publish


class PublicationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.job = Path(directory.name) / "immich-3.4.1-12345678"
        self.root = self.job / "source"
        self.root.mkdir(parents=True)
        git(self.root, "init")
        git(self.root, "-c", "user.name=Test", "-c", "user.email=test@example.com",
            "commit", "--allow-empty", "-m", "baseline")
        self.base = git(self.root, "rev-parse", "HEAD")
        (self.job / "release.json").write_text(json.dumps({"fork_base": self.base}))
        self.contents = b"\x00qualified bytes\r\n"
        (self.root / "candidate").write_bytes(self.contents)
        self.tree = candidate_tree(self.root, self.job)
        git(self.root, "update-ref", "refs/maintainer/qualified-tree", self.tree)
        self.repo = "meitelis/immich-rkmpp"
        self.branch = "automation/immich-3.4.1-12345678"
        self.commit = "a" * 40
        self.ref = None
        self.pr = None
        self.lose_response = None
        self.writes = []
        self.entries = []
        self.uploaded = []

    def github(self, path, data=None):
        path = path.removeprefix("repos/" + self.repo)
        if data is not None:
            self.writes.append(path)
        if path == "/git/ref/heads/main":
            return {"object": {"sha": self.base}}
        if path == "/git/ref/heads/" + self.branch:
            if self.ref is None:
                raise HTTPError(path, 404, "missing", {}, None)
            return {"object": {"sha": self.ref}}
        if path == "/git/blobs":
            self.uploaded.append(base64.b64decode(data["content"]))
            return {"sha": git(self.root, "rev-parse", self.tree + ":candidate")}
        if path == "/git/trees":
            self.entries = data["tree"]
            return {"sha": self.tree}
        if path == "/git/commits":
            return {"sha": self.commit}
        if path == "/git/refs":
            self.assertIsNone(self.ref)
            self.ref = data["sha"]
            response = {}
        elif path == "/git/commits/" + self.commit:
            return {"tree": {"sha": self.tree}, "parents": [{"sha": self.base}]}
        elif path.startswith("/pulls?"):
            return [self.pr] if self.pr else []
        elif path == "/pulls":
            self.assertIsNone(self.pr)
            self.pr = {"head": {"ref": self.branch, "sha": self.commit, "repo": {"full_name": self.repo}},
                       "base": {"ref": "main", "sha": self.base}, "state": "open",
                       "html_url": "https://example.org/pr/7", "number": 7}
            response = self.pr
        else:
            self.fail("Unexpected request: " + path)
        if path == self.lose_response:
            raise TimeoutError("GitHub applied the request before the connection closed")
        return response

    def publish(self):
        with patch.dict(os.environ, {"GITHUB_TOKEN": "fixture"}), patch("maintainer.github", self.github):
            return publish("immich", self.root, "3.4.1", self.job, self.tree)

    def test_only_qualified_blobs_are_uploaded(self):
        (self.root / "late-file").write_text("created after qualification")
        (self.root / "candidate").write_text("also changed")
        self.publish()
        self.assertEqual([entry["path"] for entry in self.entries], ["candidate"])
        self.assertEqual(self.uploaded, [self.contents])

    def test_recovers_after_lost_branch_and_pr_responses(self):
        for endpoint in ["/git/refs", "/pulls"]:
            self.lose_response = endpoint
            with self.assertRaises(TimeoutError):
                self.publish()
        self.assertEqual(self.publish()["pull_request_number"], 7)
        self.assertEqual(self.writes.count("/git/refs"), 1)
        self.assertEqual(self.writes.count("/pulls"), 1)

    def test_existing_branch_with_wrong_tree_is_rejected(self):
        self.ref = self.commit
        original = self.github
        def wrong_tree(path, data=None):
            if path.endswith("/git/commits/" + self.commit):
                return {"tree": {"sha": self.base}, "parents": [{"sha": self.base}]}
            return original(path, data)
        with patch.object(self, "github", wrong_tree), self.assertRaisesRegex(RuntimeError, "branch differs"):
            self.publish()
        self.assertEqual(self.writes, [])
