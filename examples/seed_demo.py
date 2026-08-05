"""Populate a demo database so you can see the layers working.

    python examples/seed_demo.py

Creates ./demo.db with a handful of fictional entries, then prints what an
agent would receive on wake-up.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mnemo import Config, MemoryStore  # noqa: E402

cfg = Config(db_path="./demo.db", token="demo")
store = MemoryStore(cfg)

CORE = [
    ("Prefers concise answers. Dislikes being asked to fill in forms — "
     "collect information from conversation instead.", "preferences"),
    ("Works in UTC+8. All stored timestamps are UTC; convert on display.",
     "environment"),
]

DIARY = [
    "Shipped the tag-based retrieval. Search over 200 entries returns instantly.",
    "Found a timezone bug: entries written after midnight local time were "
    "grouped under the previous day. Cause was slicing the raw UTC string.",
    "Decided against a manual-entry form for mood tracking. Testing showed it "
    "simply never got used. Writes now happen automatically.",
]

SUMMARIES = [
    ("Week 1 summary: schema, layers, first working MCP round-trip.", "summary"),
    ("Week 2 summary: tag retrieval, cold sinking, timezone fixes.", "summary"),
    ("Week 3 summary: removed shell tool, added token auth.", "summary"),
]


def main():
    for content, tags in CORE:
        store.write(content, layer="core", tags=tags)
    for content in DIARY:
        store.write(content, layer="diary", tags="log")
    for content, tags in SUMMARIES:
        store.write(content, layer="core", tags=tags)

    print(store.wakeup(diary_count=3))
    print()
    s = store.stats()
    print(f"total {s['total']}, sunk {s['demoted']} "
          f"(hot_limits: {cfg.hot_limits})")
    print("\nThe first weekly summary has sunk: core keeps only the two most\n"
          "recent entries per tag, so wake-up stays a fixed size as the\n"
          "database grows. Nothing was deleted — read it back with\n"
          "include_inactive=True.")


if __name__ == "__main__":
    main()
