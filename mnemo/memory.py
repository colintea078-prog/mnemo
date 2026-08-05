"""Core memory operations.

The whole point of this module: an LLM agent starts every session with an
empty head. These functions are what it reads on wake-up and what it writes
before the context window closes.
"""

from typing import Any, Dict, List, Optional

from .config import Config
from .db import connect


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

    def search(self, query: str, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Substring search across content and tags.

        LIKE is intentional: at the scale this runs at (thousands of entries,
        single user) it is instant, and it keeps the dependency list empty.
        Swap in FTS5 if you outgrow it — see docs/design.md.
        """
        conn = connect(self.cfg.db_path)
        rows = conn.execute(
            "SELECT id, content, layer, tags, created_at FROM memories "
            "WHERE content LIKE ? OR tags LIKE ? "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (f"%{query}%", f"%{query}%", limit or self.cfg.search_limit),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

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
