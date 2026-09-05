# AGENTS.md

Instructions for any AI agent working on **AgentColab** itself.

## What this is

A coordination layer for AI coding agents that belong to different people, run
on different machines, and are usually different models. Coordination state
rides on a custom git ref and has no server. The optional canvas — a live view
of each agent's transcript — has a relay, and it is off until a machine joins a
room.

Read [RULES.md](RULES.md) first — it governs behaviour here and it is written
for you. [FAILURE-MODES.md](FAILURE-MODES.md) is what is known to be broken;
check it before reporting something.

## Layout

```
agentcolab/
  records.py     pure logic: scrubbing, path matching, fingerprints, surface
  store.py       transport: the git ref, profiles, publish as compare-and-swap
  identity.py    SSH signing, verification, key pinning, trust levels
  wire.py        ColabWire, the compact agent-to-agent line format
  board.py       deterministic work assignment, leases, contested takes
  session.py     presence, the briefing, chat mirroring, token budget
  chat/          Discord + Slack adapters behind one interface
  canvas.py      transcript tailer, sanitising per level, the daemon, roles and messages
  canvas_relay.py the stdlib canvas relay, reference for the contract in docs/canvas-contract.md
  wake.py        the wake listener: one connection to the room, the wake prompt, starting a session
  wsclient.py    a stdlib WebSocket client, for the listener
  hooks.py       harness integration and the pre-edit warning
  mcp.py         MCP server over stdio, no SDK
  cli.py         every command
```

## Working on it

```bash
python3 tests/test_units.py          # fast, no network, no git
bash tests/test_e2e.sh               # four agents, one repo, real git
python3 tests/test_canvas.py         # tailers, sanitising, the daemon, the wake listener
python3 tests/test_canvas_relay.py   # the stdlib relay against the contract
python3 tests/check_stdlib_only.py   # zero dependencies, enforced
bash tests/run_all.sh                # all of the above, one exit code
```

Run all of these before you propose anything — `run_all.sh` is the short way.
A behaviour change needs a test that fails without it.

## Constraints that are not negotiable

- **Zero dependencies.** Stdlib only. CI fails on any other import.
- **Python 3.9+.** `from __future__ import annotations` at the top of every
  module.
- **Never touch the user's checkout.** Everything lives in `~/.agentcolab`. A
  hook that can disturb an index or a rebase is a hook people uninstall.
- **Never block.** Warnings fire once and the repeated action goes through.
- **Never author.** No PRs, no issue comments, no branch pushes, no merges.
- **A hook must never raise.** Swallow, exit 0. Debug with `AGENTCOLAB_DEBUG=1`.
- **Never put a model in the coordination path.** Overlap detection is
  arithmetic on `git diff`, and that is why it is trustworthy.

## Places the obvious implementation is wrong

Each of these carries a comment in the source saying so. They were all real
bugs.

- `git ls-files` and `update-index` refuse to run without a work tree, and fail
  *silently*. The state repo is non-bare with an empty work tree for this reason.
- Chaining commits on the state ref grows history forever. Publishes are orphan
  snapshots under `--force-with-lease`, which is a compare-and-swap.
- The canonical form for signing must exclude read-time metadata, or records
  verify only on the machine that wrote them.
- Keying identity on the project rather than a profile makes two agents on one
  machine share one identity.
- `git status --porcelain` has a position-dependent prefix that `strip()` eats.
  Use `diff --name-only` and `ls-files --others`.
- The entropy scrubber must mask URLs first or it mangles long paths.
- A canvas event's `seq` must come from its position in the transcript file,
  never from a counter. Two producers over the same file — a daemon and a hook
  flush — have to agree without talking, and a counter makes every overlap a
  duplicate the viewer cannot detect.
- The canvas daemon must never be spawned inside `store.lock()`. The child
  inherits the lock's descriptor and holds it for as long as it lives, so every
  hook on the machine waits on a process that will not exit for thirty minutes.
- A wake-up must never touch the harness's permission settings, and the
  listener's local config — not the relay's flag — decides whether anything
  starts. The relay is a display; a relay that could flip a machine's switch
  would make a leaked owner link a remote-execution credential.
- Transcript discovery must test each candidate's working directory against
  this checkout and never fall back to "the newest file". The newest file on a
  developer's machine is very often a session in a different repository.

## Style

Comments explain *why*, not *what*. Error messages tell an agent what to do
next. Every published number ships with the script that produced it.
