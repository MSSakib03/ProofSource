"""Translations: both languages complete and consistent, and delivered to the page."""
import json
import os
import re
import tempfile
import unittest

import i18n_tools
from test_app import A, Base


class TranslationFilesTests(unittest.TestCase):
    def test_translations_are_consistent(self):
        self.assertEqual(i18n_tools.problems(), [])

    def test_both_languages_have_the_same_keys(self):
        tr = i18n_tools.load_translations()
        self.assertEqual(set(tr["en"]), set(tr["bn"]))
        self.assertGreater(len(tr["en"]), 500)

    def test_the_checker_really_catches_problems(self):
        tr = {"en": {"a": "Hello {n}", "b": "x", "err_server_error": "e"}, "bn": {"a": "হ্যালো", "c": "y", "b": " "}}
        found = " | ".join(i18n_tools.problems(tr))
        self.assertIn("missing key", found)                      # bn lacks err_server_error
        self.assertIn("'c' does not exist in en.json", found)
        self.assertIn("different placeholders", found)           # {n} lost in bn
        self.assertIn("'b' is empty", found)

    def test_a_missing_reference_language_is_an_error(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "bn.json"), "w", encoding="utf-8") as f:
                json.dump({"a": "b"}, f)
            with self.assertRaises(RuntimeError):
                i18n_tools.load_translations(d)

    def test_every_error_code_the_server_sends_has_a_message(self):
        ref = i18n_tools.load_translations()["en"]
        missing = [c for c in i18n_tools.error_codes_of_server() if "err_" + c not in ref]
        self.assertEqual(missing, [])


class DeliveryTests(Base):
    def test_the_page_carries_both_languages_in_readable_form(self):
        html = self.c.get("/").data.decode()
        m = re.search(r"window\.PS_I18N = (\{.*?\});\n", html, re.S)
        self.assertIsNotNone(m)
        tr = json.loads(m.group(1))
        self.assertEqual(set(tr), {"en", "bn"})
        self.assertIn("প্রতিযোগিতা", m.group(1))                  # Bangla is not \u-escaped
        self.assertEqual(tr, A.TRANSLATIONS)

    def test_the_template_no_longer_embeds_the_strings(self):
        with open(i18n_tools.TEMPLATE, encoding="utf-8") as f:
            self.assertIn("const I18N = window.PS_I18N", f.read())

    def test_page_is_not_larger_than_before_the_move(self):
        # was 355 KB with the table inside the template; +2 KB for the Validated leaderboard column
        self.assertLess(len(self.c.get("/").data), 362_000)
