# Design notes

Why the thing is shaped the way it is. Written for whoever has to change it later —
including me.

---

## 1. Why layers instead of pure vector retrieval

Retrieval-augmented memory assumes the agent knows enough to ask. For a task-scoped
assistant that holds: the user's question *is* the query.

For an agent that persists across months, it breaks. Consider: the user mentioned six weeks
ago that they are allergic to mango. Today they ask what to order for dessert. Nothing in
"what should I order for dessert" retrieves the mango entry unless the embedding happens to
land close, and it usually does not. **The agent cannot search for what it does not know it
has forgotten.**

So a small set of facts has to be unconditional — loaded every session whether or not anything
in the conversation points at them. That is the `core` layer. Everything else is retrieved on
demand, where vector search would work fine and `LIKE` currently works well enough.

The split is not a technical optimisation. It is an admission that retrieval has a blind spot
and that some facts are too costly to miss.

## 2. Why demote instead of delete

Rolling summaries grow without bound. Twenty weeks of weekly summaries is twenty entries that
all have an equal claim to being in `core`, and wake-up stops fitting.

Deleting the old ones is the obvious fix and it is wrong, for a reason that only shows up
later: **the old summaries are the only record of how something came to be**. Compressing them
away means the agent can tell you the current state of a decision but never why it was made.

Demotion keeps both properties. `active_flag = 0` removes an entry from automatic loading but
leaves it searchable. Wake-up cost stays flat; history stays complete. The cost is one extra
predicate on every read, which is free.

The cap is per-tag, not per-layer, so `core` can hold two rolling summaries *and* an unbounded
number of permanent facts about the user. Tag the summaries; leave the facts untagged.

## 3. Why wake-up is a single call

The first version required the agent to call `read_memory` once per layer, then a separate call
for recent entries — five or six round trips. It worked, and the agent skipped it roughly a
third of the time, particularly when the user's opening message looked urgent.

That is not a prompt problem. Any procedure with six steps and no enforcement will be
shortened under pressure. Collapsing it into one call named `wakeup` fixed the compliance
problem completely, and made the ordering of the returned context something the store controls
rather than something the agent improvises.

General form: if you need an agent to reliably do a multi-step thing, give it one tool that
does the whole thing.

## 4. A feature that was cut after testing

The deployment this came from tracks daily mood. The obvious next step was a manual entry
form — tap a day, pick a mood, save.

It never got used. Not "used less than expected" — zero times, by the only user, over weeks.

The reason turned out to be specific and generalisable: the user does not object to being
*recorded*, she objects to *filling in forms*. Those look identical in a feature spec and are
opposite in practice. The same information, extracted from conversation and written
automatically, was accepted without friction.

The form was removed rather than iterated on. The display it fed stayed.

Worth stating plainly because it is the most transferable lesson here: **a feature that
produces the right data through the wrong interaction is not a feature that needs polish. It
is the wrong feature.** Usage data said so before any amount of UI work would have.

## 5. Security posture

This runs as a network service that an LLM can call. The threat model is not sophisticated
attackers, it is:

- **the agent being talked into something** by content it read — a web page or a document
  containing instructions aimed at the model;
- **the endpoint being found**, since anything reachable over HTTP eventually is.

Two consequences:

**No shell tool.** The private version had one, and it was the single most useful tool in the
set. It is also unconditional remote code execution for anyone holding the token, and a
prompt-injection payload away from being executed on the agent's own initiative. It was removed
before publication. Tools should expose named operations, never a string that reaches a shell.

**Token in the path, not in a header**, because MCP clients vary in what headers they can set,
and a missing header failing open is worse than an ugly URL. The token must be generated
randomly and never committed; `config.json` is gitignored, `config.example.json` is not.

The store itself is unauthenticated below the transport: anything that can reach the port and
holds the token can read and write everything. This is correct for a single-user deployment
and would need rethinking for anything multi-tenant.

## 6. Known limits

- **Single writer.** SQLite with a 10s busy timeout. Fine for one agent, not for a fleet.
- **`LIKE` search does not do semantics.** Searching "allergy" will not find "cannot eat
  mango". This is the largest real gap; FTS5 narrows it, embeddings would close it.
- **No versioning.** `update_memory` overwrites. Entries that get edited lose their history,
  which is a slightly awkward contrast with §2.
- **Hot limits are per-tag-prefix**, matched with `LIKE`. Overlapping tag names will interfere.
  Keep the first tag distinctive.
