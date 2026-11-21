"""Core memory operations.

The whole point of this module: an LLM agent starts every session with an
empty head. These functions are what it reads on wake-up and what it writes
before the context window closes.
"""

import re
from typing import Any, Dict, List, Optional

from .config import Config
from .db import connect


# --- search tuning -------------------------------------------------------
SNIPPET_RADIUS = 46      # characters of context kept on each side of a hit
SNIPPET_MAX_SPAN = 220   # a merged window never grows past this many characters
SNIPPET_MAX_COUNT = 3    # snippets returned per entry
MAX_HITS_SCANNED = 400   # safety cap for pathological one-letter queries


def split_terms(query: str) -> List[str]:
    """Split a query on whitespace. Every term must match (AND)."""
    return [t for t in query.split() if t]


def _like_escape(term: str) -> str:
    """Make %, _ and \\ literal inside a LIKE pattern."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _fold(text: str) -> str:
    """Lowercase for matching, but never change the string length.

    A few Unicode characters grow when lowercased, which would shift every
    snippet offset. If that happens, fall back to a case-sensitive match.
    """
    low = text.lower()
    return low if len(low) == len(text) else text


def count_hits(text: str, term: str) -> int:
    low, needle = _fold(text), _fold(term)
    count, start = 0, 0
    while count < MAX_HITS_SCANNED:
        i = low.find(needle, start)
        if i == -1:
            break
        count += 1
        start = i + len(needle)
    return count


def find_snippets(
    text: str,
    terms: List[str],
    radius: int = SNIPPET_RADIUS,
    max_span: int = SNIPPET_MAX_SPAN,
    max_count: int = SNIPPET_MAX_COUNT,
) -> List[str]:
    """Return the passages around the hits, with ellipses where text was cut.

    Hits that sit close together are merged into one passage so the same
    sentence is not shown twice. A merged passage is capped at ``max_span``
    characters; past that, a new passage starts.
    """
    low = _fold(text)
    hits = []
    for term in terms:
        needle = _fold(term)
        start = 0
        while len(hits) < MAX_HITS_SCANNED:
            i = low.find(needle, start)
            if i == -1:
                break
            hits.append((i, i + len(needle)))
            start = i + len(needle)
    hits.sort()

    windows: List[List[int]] = []
    for h0, h1 in hits:
        s, e = max(0, h0 - radius), min(len(text), h1 + radius)
        if windows and s <= windows[-1][1] and (e - windows[-1][0]) <= max_span:
            windows[-1][1] = max(windows[-1][1], e)
        else:
            windows.append([s, e])

    out = []
    for s, e in windows[:max_count]:
        piece = re.sub(r"\s*\n\s*", " ", text[s:e])
        out.append(("\u2026" if s > 0 else "") + piece + ("\u2026" if e < len(text) else ""))
    return out


class MemoryStore:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    # ------------------------------------------------------------------ write

    def write(
        self,
        content: str,
        layer: Optional[str] = None,
        tags: str = "",
        source: str = "agent",
    ) -> Dict[str, Any]:
        """Insert one entry, then apply the hot-limit rule for its layer.

        Writing is deliberately cheap and unvalidated beyond the layer name:
        an agent that hesitates about whether something is worth storing
        stores nothing, which is the failure mode this project exists to fix.
        """
        layer = layer or self.cfg.default_layer
        if layer not in self.cfg.layers:
            raise ValueError(f"unknown layer {layer!r}; known layers: {self.cfg.layers}")

        conn = connect(self.cfg.db_path)
        with conn:
            cur = conn.execute(
                "INSERT INTO memories (content, layer, tags, source) VALUES (?,?,?,?)",
                (content, layer, tags, source),
            )
            new_id = cur.lastrowid
            demoted = self._apply_hot_limit(conn, layer, tags)
        conn.close()
        return {"id": new_id, "layer": layer, "demoted": demoted}

    def _apply_hot_limit(self, conn, layer: str, tags: str) -> List[int]:
        """Demote the oldest entries once a tagged group exceeds its cap.

        Demotion sets active_flag = 0. Nothing is ever deleted here: a demoted
        entry stays searchable and can be read back explicitly, it just stops
        being loaded on wake-up. Rolling summaries are the intended use case —
        keep the two most recent, let the older ones sink.
        """
        limit = self.cfg.hot_limits.get(layer)
        if not limit or not tags:
            return []

        primary = tags.split(",")[0].strip()
        if not primary:
            return []

        rows = conn.execute(
            "SELECT id FROM memories "
            "WHERE layer = ? AND tags LIKE ? AND active_flag = 1 "
            # id breaks ties: entries written in the same second would
            # otherwise sort unpredictably and sink the wrong one.
            "ORDER BY created_at DESC, id DESC",
            (layer, f"%{primary}%"),
        ).fetchall()

        stale = [r["id"] for r in rows[limit:]]
        for mid in stale:
            conn.execute("UPDATE memories SET active_flag = 0 WHERE id = ?", (mid,))
        return stale

    # ------------------------------------------------------------------- read

    def read(
        self,
        layer: Optional[str] = None,
        tags: Optional[str] = None,
        limit: Optional[int] = None,
        include_inactive: bool = False,
    ) -> List[Dict[str, Any]]:
        """Read entries newest-first, hiding demoted ones by default."""
        query = ["SELECT id, content, layer, tags, created_at, active_flag",
                 "FROM memories WHERE 1 = 1"]
        params: List[Any] = []

        if layer:
            query.append("AND layer = ?")
            params.append(layer)
        if tags:
            query.append("AND tags LIKE ?")
            params.append(f"%{tags}%")
        if not include_inactive:
            query.append("AND active_flag = 1")

        query.append("ORDER BY created_at DESC, id DESC LIMIT ?")
        params.append(limit or self.cfg.read_limit)

        conn = connect(self.cfg.db_path)
        rows = conn.execute(" ".join(query), params).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def search(
        self,
        query: str,
        limit: Optional[int] = None,
        include_inactive: bool = True,
    ) -> List[Dict[str, Any]]:
        """Multi-keyword search across content and tags.

        Terms are separated by whitespace and ALL must appear (AND), in the
        content or in the tags. Each result carries ``hits`` (total matches)
        and ``snippets`` (the matching passages), and results are ranked by
        hits, newest first among ties.

        LIKE is still the engine: at the scale this runs at (thousands of
        entries, single user) it is instant and the dependency list stays
        empty. Swap in FTS5 if you outgrow it — see docs/design.md.

        Demoted entries stay searchable by default; pass
        ``include_inactive=False`` to hide them.
        """
        terms = split_terms(query)
        if not terms:
            return []

        clauses, params = [], []
        for term in terms:
            clauses.append("(content LIKE ? ESCAPE '\\' OR tags LIKE ? ESCAPE '\\')")
            pattern = f"%{_like_escape(term)}%"
            params += [pattern, pattern]
        where = " AND ".join(clauses)
        if not include_inactive:
            where += " AND active_flag = 1"

        conn = connect(self.cfg.db_path)
        rows = conn.execute(
            "SELECT id, content, layer, tags, created_at, active_flag "
            f"FROM memories WHERE {where} "
            "ORDER BY created_at DESC, id DESC",
            params,
        ).fetchall()
        conn.close()

        results = []
        for r in rows:
            row = dict(r)
            haystack = row["content"] + "\n" + row["tags"]
            counts = [count_hits(haystack, t) for t in terms]
            if not all(counts):          # SQL LIKE is only ASCII-case-insensitive
                continue
            row["hits"] = sum(counts)
            row["snippets"] = (
                find_snippets(row["content"], terms)
                or [row["content"][: SNIPPET_RADIUS * 2]]   # tag-only match
            )
            results.append(row)

        # sorted() is stable, so the newest-first order survives as the tie-break
        results = sorted(results, key=lambda r: -r["hits"])
        return results[: (limit or self.cfg.search_limit)]

    def get(self, memory_id: int) -> Optional[Dict[str, Any]]:
        conn = connect(self.cfg.db_path)
        row = conn.execute(
            "SELECT id, content, layer, tags, created_at, active_flag "
            "FROM memories WHERE id = ?",
            (memory_id,),
        ).fetchone()
        conn.close()
        return dict(row) if row else None

    # ----------------------------------------------------------------- mutate

    def update(
        self,
        memory_id: int,
        content: Optional[str] = None,
        tags: Optional[str] = None,
        active_flag: Optional[int] = None,
    ) -> bool:
        fields, params = [], []
        for name, value in (("content", content), ("tags", tags),
                            ("active_flag", active_flag)):
            if value is not None:
                fields.append(f"{name} = ?")
                params.append(value)
        if not fields:
            return False

        fields.append("updated_at = CURRENT_TIMESTAMP")
        params.append(memory_id)

        conn = connect(self.cfg.db_path)
        with conn:
            cur = conn.execute(
                f"UPDATE memories SET {', '.join(fields)} WHERE id = ?", params
            )
        conn.close()
        return cur.rowcount > 0

    def delete(self, memory_id: int) -> bool:
        conn = connect(self.cfg.db_path)
        with conn:
            cur = conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        conn.close()
        return cur.rowcount > 0

    # ------------------------------------------------------------------ stats

    def stats(self) -> Dict[str, Any]:
        conn = connect(self.cfg.db_path)
        total = conn.execute("SELECT COUNT(*) AS n FROM memories").fetchone()["n"]
        by_layer = {
            r["layer"]: r["n"]
            for r in conn.execute(
                "SELECT layer, COUNT(*) AS n FROM memories GROUP BY layer"
            )
        }
        demoted = conn.execute(
            "SELECT COUNT(*) AS n FROM memories WHERE active_flag = 0"
        ).fetchone()["n"]
        conn.close()
        return {"total": total, "by_layer": by_layer, "demoted": demoted}

    # ----------------------------------------------------------------- wakeup

    def wakeup(self, diary_layer: str = "diary", diary_count: int = 5) -> str:
        """The first call an agent makes in a new session.

        Returns every hot core entry plus the most recent diary entries,
        formatted as plain text for direct injection into context. One call,
        because a wake-up that takes six tool calls will get skipped.
        """
        core = self.read(layer="core", limit=100)
        recent = self.read(layer=diary_layer, limit=diary_count)

        out = [f"=== core ({len(core)} entries) ==="]
        out += [f"[{m['id']}] {m['content']}" for m in core] or ["(empty)"]
        out.append("")
        out.append(f"=== recent {diary_layer} ({len(recent)}) ===")
        out += [f"[{m['id']}] {m['created_at'][:10]} {m['content']}"
                for m in recent] or ["(empty)"]
        return "\n".join(out)
