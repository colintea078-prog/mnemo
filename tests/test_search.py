import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mnemo import Config, MemoryStore  # noqa: E402
from mnemo.memory import find_snippets, split_terms  # noqa: E402


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cfg = Config(db_path=os.path.join(self.tmp.name, "t.db"), token="x")
        self.store = MemoryStore(cfg)

    def tearDown(self):
        self.tmp.cleanup()

    def test_all_terms_must_match(self):
        self.store.write("deploy went fine on staging", "daily", "ops")
        self.store.write("deploy to production, no staging mentioned elsewhere", "daily")
        self.store.write("only staging here", "daily")
        ids = {r["id"] for r in self.store.search("deploy staging")}
        self.assertEqual(len(ids), 2)

    def test_tag_only_match_still_returns_a_snippet(self):
        self.store.write("plain text", "daily", "special-tag")
        rows = self.store.search("special-tag")
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["snippets"])

    def test_snippet_has_context_and_ellipsis(self):
        text = "a" * 200 + " NEEDLE " + "b" * 200
        snips = find_snippets(text, ["needle"])
        self.assertEqual(len(snips), 1)
        self.assertIn("NEEDLE", snips[0])
        self.assertTrue(snips[0].startswith("\u2026") and snips[0].endswith("\u2026"))
        self.assertLess(len(snips[0]), 120)

    def test_close_hits_merge_and_far_hits_split(self):
        close = "x needle y needle z"
        self.assertEqual(len(find_snippets(close, ["needle"])), 1)
        far = "needle" + " pad" * 150 + " needle"
        self.assertEqual(len(find_snippets(far, ["needle"])), 2)

    def test_snippet_count_is_capped(self):
        text = (" needle" + " pad" * 150) * 6
        self.assertEqual(len(find_snippets(text, ["needle"])), 3)

    def test_ranking_by_hits(self):
        self.store.write("needle once", "daily")
        self.store.write("needle needle needle", "daily")
        rows = self.store.search("needle")
        self.assertEqual(rows[0]["hits"], 3)

    def test_percent_and_underscore_are_literal(self):
        self.store.write("100% sure", "daily")
        self.store.write("1000 sure", "daily")
        self.assertEqual(len(self.store.search("100%")), 1)
        self.store.write("snake_case name", "daily")
        self.store.write("snakeXcase name", "daily")
        self.assertEqual(len(self.store.search("snake_case")), 1)

    def test_inactive_hidden_on_request(self):
        r = self.store.write("old summary needle", "core", "sum")
        self.store.update(r["id"], active_flag=0)
        self.assertEqual(len(self.store.search("needle")), 1)
        self.assertEqual(len(self.store.search("needle", include_inactive=False)), 0)

    def test_empty_query(self):
        self.assertEqual(self.store.search("   "), [])
        self.assertEqual(split_terms("  a   b "), ["a", "b"])

    def test_cjk(self):
        self.store.write("今天去图书馆看书，图书馆很安静，晚上回家做饭", "daily")
        rows = self.store.search("图书馆 安静")
        self.assertEqual(len(rows), 1)
        self.assertIn("图书馆", rows[0]["snippets"][0])


if __name__ == "__main__":
    unittest.main()
