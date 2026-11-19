"""Behavioral acceptance for the README's first task; Python standard library only."""

import subprocess
import sys
import unittest
from pathlib import Path


class SlugCLITest(unittest.TestCase):
    def test_slug_output(self):
        cases = [
            ("Hello, Helmraiser!", "hello-helmraiser"),
            ("  Multiple---separators__here  ", "multiple-separators-here"),
            ("Version 42", "version-42"),
            ("Already-slugged", "already-slugged"),
            ("", ""),
            ("!!!", ""),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                # Keep stderr visible: intake recognizes the missing slug.py diagnostic.
                output = subprocess.check_output(
                    [sys.executable, str(Path(__file__).with_name("slug.py")), text],
                    text=True,
                )
                self.assertEqual(output, expected + "\n")


if __name__ == "__main__":
    unittest.main()
