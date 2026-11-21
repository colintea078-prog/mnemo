"""MCP server: exposes the memory store to an LLM agent as callable tools.

Speaks JSON-RPC 2.0 over HTTP POST, which is what MCP clients expect.
Stdlib only — no framework, no dependencies.

Security notes, because this is the part people get wrong:
  * Every request must hit /<token>. A wrong or missing token gets 401.
  * There is deliberately no shell-execution tool. An earlier private
    version of this server exposed one; it was removed before release.
    Do not add it back. See docs/design.md, "What was removed".
  * Bind to 127.0.0.1 and put a TLS-terminating reverse proxy in front.
"""

import http.server
import json
import socketserver
from typing import Any, Dict

from .config import Config
from .memory import MemoryStore

PROTOCOL_VERSION = "2024-11-05"


def build_tools(cfg: Config):
    return [
        {
            "name": "wakeup",
            "description": (
                "Load long-term memory at the start of a session. Returns all "
                "hot core entries plus the most recent diary entries."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"diary_count": {"type": "integer"}},
            },
        },
        {
            "name": "write_memory",
            "description": "Store one memory entry.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "content": {"type": "string"},
                    "layer": {"type": "string", "enum": cfg.layers},
                    "tags": {"type": "string",
                             "description": "comma separated"},
                },
                "required": ["content"],
            },
        },
        {
            "name": "read_memory",
            "description": "Read entries, newest first. Demoted entries are hidden "
                           "unless include_inactive is true.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "layer": {"type": "string"},
                    "tags": {"type": "string"},
                    "limit": {"type": "integer"},
                    "include_inactive": {"type": "boolean"},
                },
            },
        },
        {
            "name": "search_memory",
            "description": (
                "Multi-keyword search over content and tags. Terms separated "
                "by spaces must all match. Returns ranked entries with the "
                "matching passages; set full=true to include each whole entry."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"query": {"type": "string"},
                               "limit": {"type": "integer"},
                               "full": {"type": "boolean"}},
                "required": ["query"],
            },
        },
        {
            "name": "update_memory",
            "description": "Update an entry's content, tags, or active flag.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "content": {"type": "string"},
                    "tags": {"type": "string"},
                    "active_flag": {"type": "integer", "enum": [0, 1]},
                },
                "required": ["id"],
            },
        },
        {
            "name": "delete_memory",
            "description": "Permanently delete an entry by id.",
            "inputSchema": {
                "type": "object",
                "properties": {"id": {"type": "integer"}},
                "required": ["id"],
            },
        },
        {
            "name": "stats",
            "description": "Counts by layer, plus how many entries have sunk.",
            "inputSchema": {"type": "object", "properties": {}},
        },
    ]


def make_handlers(store: MemoryStore):
    def wakeup(a):
        return store.wakeup(diary_count=a.get("diary_count", 5))

    def write_memory(a):
        r = store.write(a["content"], a.get("layer"), a.get("tags", ""))
        msg = f"stored id={r['id']} in layer {r['layer']}"
        if r["demoted"]:
            msg += f"; demoted {len(r['demoted'])} older entr" \
                   f"{'y' if len(r['demoted']) == 1 else 'ies'}"
        return msg

    def read_memory(a):
        rows = store.read(a.get("layer"), a.get("tags"), a.get("limit"),
                          a.get("include_inactive", False))
        return _fmt(rows)

    def search_memory(a):
        rows = store.search(a["query"], a.get("limit"))
        return _fmt_search(rows, full=a.get("full", False))

    def update_memory(a):
        ok = store.update(a["id"], a.get("content"), a.get("tags"),
                          a.get("active_flag"))
        return f"updated {a['id']}" if ok else f"no entry {a['id']}"

    def delete_memory(a):
        ok = store.delete(a["id"])
        return f"deleted {a['id']}" if ok else f"no entry {a['id']}"

    def stats(a):
        s = store.stats()
        lines = [f"total: {s['total']}", f"sunk:  {s['demoted']}"]
        lines += [f"  {k}: {v}" for k, v in sorted(s["by_layer"].items())]
        return "\n".join(lines)

    return {
        "wakeup": wakeup,
        "write_memory": write_memory,
        "read_memory": read_memory,
        "search_memory": search_memory,
        "update_memory": update_memory,
        "delete_memory": delete_memory,
        "stats": stats,
    }


def _fmt(rows) -> str:
    if not rows:
        return "(no matching entries)"
    out = []
    for r in rows:
        flag = "" if r.get("active_flag", 1) else " [sunk]"
        out.append(f"[{r['id']}] ({r['layer']}) {r['content']} "
                   f"| tags: {r['tags']} | {r['created_at'][:10]}{flag}")
    return "\n".join(out)


def _fmt_search(rows, full: bool = False) -> str:
    """Search results: a header line per entry, its matching passages, and
    optionally the whole entry underneath."""
    if not rows:
        return "(no matching entries)"
    out = []
    for r in rows:
        flag = "" if r.get("active_flag", 1) else " [sunk]"
        out.append(f"[{r['id']}] ({r['layer']}) {r['created_at'][:10]} "
                   f"| tags: {r['tags']} | {r['hits']} hit(s){flag}")
        out += [f"    {snip}" for snip in r["snippets"]]
        if full:
            out.append("    --- full entry ---")
            out += [f"    {line}" for line in r["content"].splitlines()]
        out.append("")
    return "\n".join(out).rstrip()


def make_request_handler(cfg: Config, tools, handlers):
    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "mnemo"

        def _send(self, code: int, payload: Dict[str, Any] = None):
            body = json.dumps(payload).encode() if payload else b""
            self.send_response(code)
            if body:
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def do_POST(self):
            if self.path.strip("/") != cfg.token:
                self._send(401)
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                req = json.loads(self.rfile.read(length))
            except (ValueError, TypeError):
                self._send(400)
                return

            method, rid = req.get("method"), req.get("id")

            if method == "initialize":
                result = {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "mnemo", "version": "1.0"},
                }
            elif method == "tools/list":
                result = {"tools": tools}
            elif method == "tools/call":
                name = req["params"]["name"]
                args = req["params"].get("arguments", {})
                if name not in handlers:
                    text = f"unknown tool: {name}"
                else:
                    try:
                        text = handlers[name](args)
                    except Exception as exc:            # surfaced to the agent
                        text = f"error: {exc}"
                result = {"content": [{"type": "text", "text": str(text)}]}
            elif method in ("ping", "notifications/initialized"):
                self._send(204)
                return
            else:
                self._send(404)
                return

            self._send(200, {"jsonrpc": "2.0", "id": rid, "result": result})

        def log_message(self, *args):
            pass

    return Handler


class ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def main():
    cfg = Config.load()
    store = MemoryStore(cfg)
    handler = make_request_handler(cfg, build_tools(cfg), make_handlers(store))
    print(f"mnemo listening on {cfg.host}:{cfg.port}  db={cfg.db_path}")
    ThreadingServer((cfg.host, cfg.port), handler).serve_forever()


if __name__ == "__main__":
    main()
