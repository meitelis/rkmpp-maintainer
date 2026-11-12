"""A broken HTTP-option helper with independent, protected behavioral checks."""
from pathlib import Path


BROKEN = """def supported_http_options(requested, ffmpeg_help, ffprobe_help):
    supported = set(ffprobe_help.split())
    return [option for option in requested if option in supported]
"""

REFERENCE = """def supported_http_options(requested, ffmpeg_help, ffprobe_help):
    supported = set(ffmpeg_help.split()) & set(ffprobe_help.split())
    return [option for option in requested if option in supported]
"""

CHECKS = """import sys, unittest
sys.path.insert(0, 'go-vod')
from http_options import supported_http_options

class HTTPOptionsTests(unittest.TestCase):
    def test_option_must_exist_in_both_binaries(self):
        self.assertEqual(supported_http_options(
            ['-reconnect', '-reconnect_at_eof'], '-reconnect',
            '-reconnect -reconnect_at_eof'), ['-reconnect'])

    def test_opposite_support_mismatch(self):
        self.assertEqual(supported_http_options(
            ['-reconnect', '-reconnect_at_eof'], '-reconnect -reconnect_at_eof',
            '-reconnect'), ['-reconnect'])

    def test_missing_help_from_either_binary_rejects_options(self):
        for left, right in [('', '-reconnect'), ('-reconnect', '')]:
            self.assertEqual(supported_http_options(['-reconnect'], left, right), [])

    def test_exact_tokens_not_substrings(self):
        self.assertEqual(supported_http_options(['-reconnect'],
            '-reconnect_at_eof', '-reconnect'), [])

    def test_requested_order_and_whitespace(self):
        self.assertEqual(supported_http_options(['-b', '-a', '-unknown'],
            '-a\\t-b\\n', '-b  -a'), ['-b', '-a'])
"""


def create_fixture(root, upstream):
    root, upstream = Path(root), Path(upstream)
    (root / "go-vod").mkdir(parents=True)
    (root / "tests").mkdir()
    (upstream / "go-vod").mkdir(parents=True)
    source = root / "go-vod/http_options.py"
    source.write_text(BROKEN)
    (upstream / "go-vod/http_options.py").write_text(REFERENCE)
    (root / "tests/test_http_options.py").write_text(CHECKS)
    return source
