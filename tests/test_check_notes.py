import tempfile
import unittest
from pathlib import Path

from scripts.check_notes import check_file


class CheckNotesTests(unittest.TestCase):
    def check_bytes(self, content):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "note.md"
            path.write_bytes(content)
            return check_file(path)

    def test_valid_markdown(self):
        result = self.check_bytes("# 正常笔记\n".encode("utf-8"))
        self.assertEqual(result, [])

    def test_empty_file(self):
        result = self.check_bytes(b"")
        self.assertEqual(result, ["empty Markdown file"])

    def test_whitespace_only(self):
        result = self.check_bytes(b" \n\t")
        self.assertEqual(result, ["empty Markdown file"])

    def test_invalid_utf8(self):
        result = self.check_bytes(b"\xff")
        self.assertEqual(result, ["invalid UTF-8"])

    def test_nul_character(self):
        result = self.check_bytes(b"# Note\n\0")
        self.assertEqual(result, ["contains a NUL character"])
