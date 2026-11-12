"""Credential scanning distinguishes narrowly evidenced examples and regexes."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from seampact.source.inspection import withheld_source


class CredentialPatterns(unittest.TestCase):
    def check(self, text):
        return withheld_source("example.py", text.encode())

    def test_regex_argument_and_explicit_example_are_readable(self):
        text = '''class Detector:
    # e.g. api_key: "foo"
    password = r"([a-f0-9]{48}(?![a-f0-9]))"
    matcher = build_assignment_regex(secret_regex=password)
'''
        self.assertEqual(self.check(text), [])

    def test_real_literals_remain_withheld_even_in_comments_or_regex_calls(self):
        for text in ('api_key = "foo"', '# api_key: "foo"',
                     '# e.g. api_key: "high-entropy-actual-value-12345"',
                     'password = r"high-entropy-actual-value-12345"\nmatcher(secret_regex=password)',
                     'password = "[a-z]{48}"; api_key = "high-entropy-actual-value-12345"\nmatcher(secret_regex=password)',
                     'password = "[a-z]{48}"\ndef other():\n    matcher(secret_regex=password)'):
            with self.subTest(text=text):
                self.assertTrue(self.check(text))


if __name__ == "__main__":
    unittest.main()
