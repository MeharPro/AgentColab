# Architecture

Every decision here follows from one constraint: **an open-source project has
no server, no budget, and no appetite for another account.** So the transport
has to be something every participant already has, already authenticates
against, and already audits. That is git. Coordination has no server. The one
optional piece that does — the canvas, a live view of each agent's transcript —
has a relay you can host yourself, is off until you join a room, and is
described in [canvas.md](canvas.md); nothing below depends on it.

---

## The transport

State lives at **`refs/agentcolab/state`** — a custom ref, not a branch.

This matters more than it sounds:

- GitHub Actions fires on `refs/heads/*` and pull requests. A custom ref runs
  no CI.
- Vercel, Netlify, Render and friends build every branch push. A heartbeat on a
  branch would produce a failed preview deployment every few minutes.
- `git branch -a`, `git log`, every PR picker and every branch dropdown
  enumerate `refs/heads/*`. None of them will ever show this.
- `git clone` does not fetch it. **A maintainer who does nothing sees nothing.**

That last property is the single most important adoption requirement. A
coordination tool that shows up in someone's branch list is a coordination tool
they delete.

### Your checkout is never touched

Everything runs in `~/.agentcolab/<project>/`:

```
~/.agentcolab/
  MeharPro-AgentColab/
    state/                  a git repo of its own, driven by plumbing
    pins.json               first-seen signing keys, like known_hosts
    keys/                   cached github.com/<user>.keys
    checkouts.json          which checkout speaks as which agent
    lock
    p/
      fable-arch/           one profile per agent
        config.json         identity, sources, chat credentials (mode 600)
        local.json          cursors, read state, budgets
        mine/               records this agent has written
        cache/view.json     the merged picture, for the hot path
```

A hook can never disturb your index, your stash, or a rebase in progress,
because it never runs a command against your repository that writes anything.
Deleting `~/.agentcolab` is always safe: it is a cache plus an identity, and
`colab join` rebuilds both.

The state repo has a work tree, and it stays empty. That is a concession to
git: `update-index` and `ls-files` refuse to run without one, and they fail
*quietly*, which is worse than failing loudly — an earlier version of this used
a bare repo and silently never removed anything, so a renamed agent haunted the
roster forever. Nothing is ever checked out into it. Reads go through `ls-tree`
and `cat-file` against fetched refs; writes go through an index file named
explicitly.

### Profiles

Identity is per **profile**, not per project. Two agents on one machine — Claude
Code in one checkout and Codex in another, or two harnesses in one directory —
would otherwise resolve to the same project and silently share one identity,
each overwriting the other's name and records.

A profile is resolved in this order: `AGENTCOLAB_PROFILE`, then a remembered
mapping from checkout path, then a stable hash of the path (which `join`
immediately replaces with the agent's chosen name).

The git object store is shared across profiles on a machine, because objects
are objects. Identity is not.

---

## Records

Every record is a JSON file at a path that names its owner:

```
agents/<agent>.json                presence: branch, head, intent, surface, fingerprint
msgs/<agent>/<id>.json             messages
claims/<agent>/<id>.json           advisory path claims
tasks/<agent>/<id>.json            work items, and takes
findings/<agent>/<id>.json         durable lessons
bugs/<agent>/<id>.json             failures, with a machine fingerprint
reviews/<agent>/<id>.json          review requests and verdicts
```

**One agent owns one directory.** Two agents never write the same path, so the
channel cannot produce a merge conflict — "keep mine, take theirs" is a complete
and correct merge, and `put()` refuses to write a path this agent does not own.

This is the mistake most git-native coordination tools make. If your task
ledger is one shared file, concurrent agents produce merge conflicts *in the
coordination layer*, which is precisely the thing you installed it to avoid.

### Publishing is a compare-and-swap

```
fetch the ref  →  read its tree  →  drop every path we own  →  lay ours down
              →  write-tree  →  orphan commit  →  push --force-with-lease
```

Two properties fall out:

**History never accumulates.** Chaining commits would be the obvious thing and
it is wrong: a heartbeat every couple of minutes per agent means the ref grows
forever and nobody ever reads it. The ref is a snapshot of *now*, so it is
exactly one commit, always. Old objects fall out at gc.

Contention is handled by retrying with **jittered** backoff. A fixed delay makes
every racer retry on the same beat, so the agents that collided collide again —
with six publishing at once that reliably starved one out of its attempts.
Losing the race never costs a record (it stays in `mine/` and goes out on the
next sync), but a late write means peers are reading stale state, so it is worth
avoiding rather than merely surviving.

**The lease is the correctness argument.** Dropping the parent makes the push a
non-fast-forward, so `--force-with-lease` carries the weight instead: it
succeeds only if the remote is still at the revision we built from. That is a
compare-and-swap — a stronger guarantee than the fast-forward check it
replaces, not a weaker one. A lost race retries with backoff, and because we
only ever remove our own paths, a retry cannot destroy anyone else's work.

### Reading fans out over sources

A **source** is somewhere state is read from. A **push url** is the one place
this agent writes.

For a maintainer these are the same remote. For an outside contributor they are
not: they read from `upstream` and publish to their own fork. A source carries
a **scope** — records fetched from a fork are honoured only for agents that
fork's owner controls, so a fork cannot publish a record claiming to be the
maintainer and have it believed on the strength of the URL.

The merged view is cached to one JSON file, because the pre-edit hook reads it
on every tool call and cannot afford to shell out to git each time.

### Retention

Each agent prunes its own settled records after 30 days. Nobody prunes anyone
else's — that needs no coordination and cannot be abused. Presence records are
current by definition, and findings never expire, because a lesson does not get
less true.

---

## Trust

Records are signed with `ssh-keygen -Y sign`, the same primitive git uses for
`gpg.format = ssh`. That buys three things with no dependencies:

- every developer already has an SSH key, because that is how they push;
- GitHub publishes every account's public keys at `github.com/<user>.keys`, so
  the allowed-signers list builds itself;
- verification is offline once the keys are cached.

### Levels

| Level | Means |
|---|---|
| `chat` | Arrived from Discord, Slack, or the canvas. Anyone in the server, or anyone holding the room code, could have written it. A canvas message from an *agent* carries a name the relay vouches for, and no more than that. |
| `unverified` | No valid signature, or a key nobody recognises. |
| `pinned` | Signed with the key first seen for this name. Proves continuity, not identity. |
| `verified` | Signed with a key the project's roster lists. |
| `member` / `maintainer` | A roster entry says so. |

**Trust is a property of the signature, never of what a record says about
itself.** An unsigned record claiming `"role": "maintainer"` is `unverified`,
and every surface renders it that way.

Pinning is the SSH `known_hosts` model, and it exists because most projects
will never write a roster. A system where every peer reads `unverified` forever
teaches people to ignore the label, which is worse than not having one. A pin
cannot tell you who somebody is; it tells you that the thing writing as `alice`
today is the same thing that wrote as `alice` yesterday — which is what catches
a takeover. A roster entry always outranks a pin.

### Canonicalisation

The signed bytes exclude every signature field and every field the read path
attaches afterwards (`_source`, `_path`, `_trust`). Getting this wrong produces
the worst possible failure: a record that verifies on the machine that wrote it
and nowhere else, which looks like it works.

---

## Deriving work, not asking for it

### Surface

Every heartbeat publishes the files this branch has actually touched, from
`git diff` against the merge base plus uncommitted and untracked changes.
Claims are a ritual and rituals get skipped; a derived surface is not skippable.

The base ref is resolved by probing — `origin/HEAD`, then `origin/main`,
`origin/master`, `upstream/*`, `main`, `master`, `develop`, `trunk` — because a
fresh sandbox, a fork named `master`, or a detached checkout would otherwise
report an empty surface and silently switch overlap detection off.

### Deterministic ownership

`owner_of(key, roster)` is `sha256(key) mod len(roster)` over the sorted list of
agents seen in the last six hours.

Every machine computes the same answer with no round trip, no lease and no
message. An agent asking "what next" gets an answer nobody else will also get.
When a machine goes quiet it drops out of the roster and its share is
redistributed, so work never stalls on an absent participant.

`sha256` rather than Python's `hash()`, which is salted per process — two
machines would disagree and the whole mechanism would be pointless.

Ownership is only the first pass. Hashing does not *deal* evenly, so with six
tasks and four agents an agent commonly owns none, and if every such agent fell
back to whatever was first they would all pick the same task. So the remainder
is dealt: agents holding nothing are ranked by name, surplus tasks are ranked by
priority then id, and they are handed out in order. Every machine computes the
whole deal from the same inputs, so the result is distinct by construction
rather than by luck — and an agent with nothing left to be dealt is told there
is nothing, which is cheaper than a third agent joining work two others are
already sorting out.

### Contested takes resolve without a message

A take is a record under the taker's own directory, so two simultaneous takes
cannot conflict. When both land, every machine resolves the winner identically:
earliest timestamp, then lexically smaller agent name. The loser stands down on
its own at the next sync, and its briefing tells it to.

---

## Warning without blocking

There is deliberately no lock.

An agent that is blocked with no way forward routes around the tool — `sed -i`
instead of Edit, or a different file — and then the coordination is worse than
nothing. So every warning fires exactly once, per file, per session, and the
repeated edit goes through.

A `PreToolUse` hook has no way to show a model a note and continue, so the tool
declines the call once, purely as a way of speaking. The Bash path watches for
the obvious detours (`sed -i`, `perl -i`, `tee`, `git checkout`, redirects into
a watched path) but only when the command both mutates a file *and* names a
watched path — reading a claimed file is always fine, and flagging `2>&1` as a
file mutation produced false accusations in an earlier version.

If an agent edits into a critical claim after being warned, the holder is told
automatically. The notice is queued locally and delivered on the next sync,
because the edit path must not make a network call.

Warn-never-block is also the security answer to claim-squatting: an agent that
claims every path to starve others achieves nothing, because claims have no
teeth by construction.

---

## Cost

Coordination is overhead, and overhead that is not measured grows.

- The briefing is charged against an hourly token budget and collapses to one
  line when the budget is gone. Agents keep working and stop talking, which is
  the correct priority.
- Messages are capped per hour as a hard limit, not advice — advice is
  something an agent reasons its way past whenever a message feels important,
  and it always does.
- The briefing shows subjects in wire form, never bodies. Bodies are fetched by
  id, and rarely should be.
- Briefings carry paths, shas and counts — never file contents. That is a token
  decision and a security decision at once.
- Re-briefing is triggered by a change signature, not a timer, so it survives a
  context clear without becoming noise.

---

## Harness integration

The CLI is the whole product; everything else is a convenience.

- **MCP server** (`colab mcp`) — 18 tools over stdio, JSON-RPC 2.0, implemented
  directly with no SDK. Any agent that speaks MCP gets coordination as native
  tools rather than something it must remember to shell out to.
- **Hooks** (Claude Code) — session briefing, pre-edit warning, presence on
  idle, and a session-end marker for the canvas. This is the only harness where the warning is delivered *before* the
  write rather than offered as a tool the agent may choose to call. That
  asymmetry is real and worth stating plainly.
- **The wake listener** (`colab wake serve`, opt-in) — the one process here
  that runs *between* sessions. It holds a connection to the canvas room and,
  when a ping arrives for this agent, starts a headless session (`claude -p`,
  `codex exec`) in the joined checkout under the harness's own permission
  settings. It is the owner's standing instruction, not the ping's: the
  machine's local config decides, the relay only displays. Off unless the
  owner ran `colab wake on`; [canvas.md](canvas.md#wake-ups) has the consent
  model in full.
- **`AGENTS.md` / Cursor rules** — one paragraph, because it is prepended to
  every session and paid for on every turn forever.

Every handler swallows its own errors and exits 0. A coordination tool that can
wedge a session gets uninstalled within the hour, and rightly.
