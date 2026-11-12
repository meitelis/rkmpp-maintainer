import tempfile
import unittest
from pathlib import Path
from core import command
from profiles import current_version, git, reconcile, validate


def init(root):
    root.mkdir()
    command(["git", "init", str(root)])
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.test")


def commit(root):
    git(root, "add", ".")
    git(root, "commit", "-m", "fixture")
    return git(root, "rev-parse", "HEAD")


class ReconciliationTests(unittest.TestCase):
    def test_memories_hotfix_replay_preserves_fork_and_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            upstream, fork = Path(temp)/"upstream", Path(temp)/"fork"
            init(upstream)
            (upstream/"go-vod").mkdir()
            base = 'package main\nconst VERSION = "0.7.0"\n' + '\n'.join('// unchanged '+str(i) for i in range(15))+'\n'
            (upstream/"go-vod/main.go").write_text(base)
            old = commit(upstream)
            (upstream/"go-vod/main.go").write_text(base.replace('"0.7.0"','"0.7.1"'))
            (upstream/"go-vod/new.go").write_text('package main\n// official hotfix\n')
            new = commit(upstream)
            init(fork)
            (fork/"go-vod").mkdir()
            (fork/"go-vod/main.go").write_text(base+'\n// RKMPP custom code\n')
            (fork/"README.md").write_text('Nextcloud Memories 11.1.1\nMeasured on 11.1.1\n')
            info = fork/"memories_rkmpp_v11/appinfo"
            info.mkdir(parents=True)
            (info/"info.xml").write_text('<version>1.1.27</version>')
            commit(fork)
            reconcile("memories", fork, upstream, "11.1.1", "11.1.2", old, new)
            self.assertIn("RKMPP custom code", (fork/"go-vod/main.go").read_text())
            self.assertIn('"0.7.1"', (fork/"go-vod/main.go").read_text())
            self.assertTrue((fork/"go-vod/new.go").exists())
            self.assertEqual((fork/"README.md").read_text(), 'Nextcloud Memories 11.1.2\nMeasured on 11.1.1\n')
            self.assertEqual(current_version("memories", fork), "11.1.2")
            self.assertIn("1.1.28", (info/"info.xml").read_text())
            validate(fork)

    def test_unresolved_conflicts_fail_validation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)/"repo"
            init(root)
            (root/"file.go").write_text('<<<<<<< ours\nold\n=======\nnew\n>>>>>>> theirs\n')
            commit(root)
            with self.assertRaisesRegex(RuntimeError, "Unresolved"):
                validate(root)
