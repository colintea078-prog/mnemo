# mnemo

**Layered long-term memory for LLM agents.** Python stdlib only, one SQLite file, speaks MCP.

An agent starts every session with an empty head. Anything it learned last time is gone unless
something outside the model wrote it down. `mnemo` is that something: a small memory store the
agent reads on wake-up and writes to before the context window closes.

This is not a research prototype. It is the cleaned-up, de-identified core of a personal AI
assistant that has been running continuously since May 2026 — the design decisions below all
came from things that broke in production.

**[Interactive demo →](https://colintea078-prog.github.io/mnemo/)** — write entries, watch the
oldest summary sink, and see why wake-up cost stays flat while the database grows.

---

## The problem it solves

The obvious approach is "dump everything into a vector database and retrieve on demand". That
fails for long-running personal agents in a specific way: **retrieval only works when the agent
knows it should look**. An agent that has forgotten a person exists will never search for them.

So `mnemo` splits memory into two kinds:

| | loaded when | example |
|---|---|---|
| **Always-on** | every session, unconditionally | who the user is, standing preferences, environment facts |
| **On-demand** | when the agent searches | what happened on a particular day |

The always-on set has to stay small and roughly constant in size, or it eats the context window
as the database grows. That constraint drives most of the design.

---

## Install and run

Requires Python 3.8+. No dependencies.

```bash
git clone https://github.com/colintea078-prog/mnemo.git
cd mnemo
python examples/seed_demo.py
```

That creates `demo.db`, writes a few fictional entries, and prints exactly what an agent would
receive on wake-up.

To run the MCP server:

```bash
cp config.example.json config.json    # then edit it
MNEMO_TOKEN=$(python -c "import secrets;print(secrets.token_urlsafe(32))") python -m mnemo.server
```

The server refuses to start without a token. Bind it to `127.0.0.1` and put a TLS-terminating
reverse proxy in front of it.

---

## How it works

### Layers

Every entry belongs to exactly one layer. Layers are configured, not hard-coded:

```json
{ "layers": ["core", "daily", "memo", "diary", "note"] }
```

Only `core` is loaded automatically on wake-up. Everything else is retrieved by search or by an
explicit read. The layer names above are a starting point — rename them for your domain.

### Cold sinking

The problem: rolling summaries accumulate. After twenty weeks you have twenty weekly summaries,
all of them in `core`, and wake-up no longer fits in the context window.

The fix: a per-layer cap on how many entries stay *hot* for a given tag.

```json
{ "hot_limits": { "core": 2 } }
```

Write a third entry tagged `summary` into `core`, and the oldest one is demoted — `active_flag`
flips to 0. It stops being loaded on wake-up. **It is not deleted.** It stays searchable and can
be read back with `include_inactive`. Wake-up cost stays flat while history keeps growing.

### Wake-up as one call

`wakeup` returns all hot core entries plus the N most recent diary entries in a single response.
This is a deliberate ergonomic choice: a wake-up procedure that takes six tool calls is a
wake-up procedure the agent will skip when it is in a hurry. One call, always the same call.

### Search

Substring `LIKE` over content and tags. At the scale this targets — thousands of entries, one
user — it returns instantly and keeps the dependency list empty. If you outgrow it, SQLite's
FTS5 is a drop-in replacement for `MemoryStore.search`; nothing else needs to change.

---

## Tools exposed over MCP

| tool | purpose |
|---|---|
| `wakeup` | load core + recent diary in one call |
| `write_memory` | store an entry, applying the hot limit |
| `read_memory` | filter by layer / tag, newest first |
| `search_memory` | substring search |
| `update_memory` | edit content, tags, or hot/cold flag |
| `delete_memory` | permanent removal |
| `stats` | counts per layer, how many have sunk |

---

## What was removed before release

The private version this was extracted from had a `exec_vps` tool that ran arbitrary shell
commands on the host. It was genuinely useful for a single-operator deployment and it is
genuinely a remote code execution hole in anything public. It is gone and should not come back.

If your agent needs to run commands, give it a tool that accepts an enum of specific named
operations, not a string that reaches a shell.

Also removed: text-to-speech, a message board, and image storage — useful, but specific to one
deployment rather than to the problem this repository is about.

---

## Things that broke, and what fixed them

**Timestamps grouped under the wrong day.** Entries are stored in UTC. The UI grouped them by
slicing the first 10 characters of the raw timestamp, so anything written after midnight local
time (UTC+8) landed in the previous day. Fix: convert to local time before grouping, never slice
a raw UTC string for display.

**The wrong summary sank.** Three entries written in the same second have identical
`created_at` values, so `ORDER BY created_at DESC` sorted them arbitrarily and demoted the
newest instead of the oldest. Fix: `ORDER BY created_at DESC, id DESC`. Any ordering that
matters needs a tiebreaker.

**Cache made files impossible to update.** Static assets were served with a one-year immutable
cache header, so editing a file in place changed nothing for anyone who had already loaded it.
Fix: treat immutable-cached files as immutable — write a new filename and update the reference.

---

## Design notes

Longer discussion of the trade-offs — why layers instead of pure vector retrieval, why demote
instead of delete, why a feature was cut after testing — is in [docs/design.md](docs/design.md).

## License

MIT
