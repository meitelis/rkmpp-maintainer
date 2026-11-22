"""Candidate source fingerprint must block unreviewed source changes."""
import hashlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from evidence import candidate_source_digest


class CandidateSourceDigestTests(unittest.TestCase):
    def repo(self, root):
        subprocess.run(["git","init","-q",str(root)],check=True)
        (root/"source.go").write_text("package main\n")
        subprocess.run(["git","-C",str(root),"add","source.go"],check=True)

    def test_files_bytes_and_file_mode_are_protected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            self.repo(root)
            first=candidate_source_digest(root)
            (root/"source.go").write_text("package changed\n")
            changed=candidate_source_digest(root)
            self.assertNotEqual(first,changed)
            os.chmod(root/"source.go",0o755)
            self.assertNotEqual(changed,candidate_source_digest(root))

    def test_untracked_source_included_but_ignored_cache_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            self.repo(root)
            baseline=candidate_source_digest(root)
            (root/"extra.go").write_text("package main\n")
            self.assertNotEqual(baseline,candidate_source_digest(root))
            (root/".gitignore").write_text("generated/\n")
            subprocess.run(["git","-C",str(root),"add",".gitignore"],check=True)
            current=candidate_source_digest(root)
            (root/"generated").mkdir()
            (root/"generated"/"tmp.bin").write_bytes(b"cache")
            self.assertEqual(current,candidate_source_digest(root))

    def test_symlink_and_removed_tracked_sources_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            self.repo(root)
            (root/"source.go").unlink()
            with self.assertRaisesRegex(RuntimeError,"Unsafe or removed"):
                candidate_source_digest(root)
            (root/"source.go").symlink_to("/etc/passwd")
            with self.assertRaisesRegex(RuntimeError,"Unsafe or removed"):
                candidate_source_digest(root)

    def test_git_unavailable_or_empty_checkout_does_not_qualify(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RuntimeError):
                candidate_source_digest(tmp)


if __name__ == "__main__":
    unittest.main()
