import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mnemo import Config, MemoryStore  # noqa: E402


class HotLimitTests(unittest.TestCase):
    """These pin down the claims docs/memory-model.md makes about tags and sinking."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(db_path=os.path.join(self.tmp.name, "t.db"), token="x")
        self.store = MemoryStore(self.cfg)

    def tearDown(self):
        self.tmp.cleanup()

    def hot(self, layer="core"):
        return {r["id"] for r in self.store.read(layer=layer, limit=100)}

    def test_each_first_tag_keeps_its_own_newest_two(self):
        ids = {}
        for tag in ("tenday", "monthly"):
            ids[tag] = [self.store.write(f"{tag} {i}", "core", tag)["id"] for i in range(3)]
        self.assertEqual(self.hot(), set(ids["tenday"][1:]) | set(ids["monthly"][1:]))

    def test_untagged_entries_are_never_capped(self):
        facts = [self.store.write(f"fact {i}", "core")["id"] for i in range(6)]
        self.assertEqual(self.hot(), set(facts))

    def test_overlapping_tags_interfere(self):
        a = [self.store.write(f"w {i}", "core", "digest-weekly")["id"] for i in range(2)]
        b = [self.store.write(f"d {i}", "core", "digest")["id"] for i in range(2)]
        # writing "digest" counted the "digest-weekly" entries too, so the oldest sank
        self.assertLess(len(self.hot()), len(a) + len(b))

    def test_sinking_is_not_special_to_core(self):
        cfg = Config(db_path=os.path.join(self.tmp.name, "d.db"), token="x",
                     hot_limits={"core": 2, "diary": 3})
        store = MemoryStore(cfg)
        ids = [store.write(f"day {i}", "diary", "log")["id"] for i in range(5)]
        hot = {r["id"] for r in store.read(layer="diary", limit=100)}
        self.assertEqual(hot, set(ids[2:]))
        # a layer that is not in hot_limits is never capped
        notes = [store.write(f"n {i}", "note", "log")["id"] for i in range(5)]
        self.assertEqual({r["id"] for r in store.read(layer="note", limit=100)}, set(notes))

    def test_sunk_entries_stay_searchable(self):
        first = self.store.write("old needle", "core", "tenday")["id"]
        for i in range(2):
            self.store.write(f"new {i}", "core", "tenday")
        self.assertNotIn(first, self.hot())
        self.assertIn(first, {r["id"] for r in self.store.search("needle")})


if __name__ == "__main__":
    unittest.main()
