# What else exists

Written to be useful rather than flattering. If something here is out of date or
wrong, that is a bug — please file it, including for our competitors.

## The quadrant

The question that separates these tools is not "does it run agents in
parallel". It is **whose** agents, on **whose** machines.

| | Cross-vendor | Cross-machine | Cross-human | Git-native state | Live presence |
|---|---|---|---|---|---|
| Subagents / worktrees (any harness) | ✗ | ✗ | ✗ | ✗ | ✗ |
| Claude Code agent teams | ✗ | ✗ | ✗ | ✗ | ✓ in-session |
| Claude Code cross-session messaging | ✗ | ✓ your machines | ✗ OS-user scoped | ✗ | ✓ |
| gastown | ✓ | ✗ | ✗ | partial | ✓ |
| container-use | ✓ | ✗ | ✗ | ✓ branches | ✗ |
| GitHub Agent HQ | ✓ | ✓ | ✓ | ✗ | ✗ no shared state |
| Beads / spec-kit / Backlog.md | ✓ | ✓ | ✓ | ✓ | ✗ async ledger |
| A2A | ✓ | ✓ | ✓ | ✗ | ✗ task states |
| **AgentColab** | ✓ | ✓ | ✓ | ✓ | ✓ |

## By category

**Orchestration frameworks** — CrewAI, LangGraph, AG2, MetaGPT, CAMEL, Agno,
OpenAI Agents SDK, Google ADK. These coordinate objects in one process for one
owner. They are not competitors; they are what one participant might be built
with. An agent written with any of them can join.

**Worktree and container managers** — container-use, Claude Squad, conductor,
and the several that have died. They isolate one person's agents on one machine
and they do it better than we ever will. Different problem, and they compose:
run agents in worktrees *and* have them see each other.

**Git-native work ledgers** — Beads, spec-kit, Backlog.md. The market has voted
decisively for plain files in git, and they are right. They coordinate *intent*
— what is planned — and none of them carry presence, which is what is being
typed right now. They also generally make the ledger a shared file, so
concurrent agents produce merge conflicts in the coordination layer, which is
the thing you installed it to avoid. Per-agent write-isolated paths exist for
exactly that reason.

**Cross-vendor dispatch** — GitHub Agent HQ. The only production control plane
that puts several vendors' agents on one repository. It decides *which* agent
gets a ticket; it carries no shared state between the agents it dispatches, so
two of them landing in the same file is exactly as invisible as before.

**Protocols** — MCP owns agent-to-tool. A2A owns agent-to-service. ACP owns
editor-to-agent. All three won their layer and none is used by coding agents for
repository work. We ship a tool and let a spec fall out of it, rather than the
reverse; a protocol with no working client is a document.

**Per-vendor multiplayer** — Amp's multiplayer, Replit's agent. Single-vendor,
usually hosted, usually your-environment-only.

## Where we differ

**We are the only thing in the table with all five columns.** That is the
product. Everything else in this repository is in service of it.

**We never author.** No pull requests, no issue comments, no branch pushes, no
merges. Your branch protection and CODEOWNERS remain the only thing between a
proposal and your mainline. This keeps us structurally out of the bot-spam
category that maintainers are, correctly, banning on sight in 2026.

**Zero repo footprint.** A custom ref, no branch, nothing in the working tree,
nothing in the default branch's history, not fetched by `git clone`. A
maintainer who does nothing sees nothing.

**We do not lock.** Several systems lock the wrong object — a task claim rather
than a file write — which is worse than not locking, because it produces false
confidence while two agents with disjoint tasks edit the same file.

**We say what we do not do.** [FAILURE-MODES.md](../FAILURE-MODES.md).

## Where we are weaker

Said plainly, because you will find out anyway.

- **container-use has strictly better isolation.** It is a container. We isolate
  records, not processes. Use both.
- **gastown's merge queue is the strongest mechanism in this space.** We do not
  have one, and we should not build one — that is GitHub's merge queue's job,
  and we deliberately never touch your mainline.
- **Anything single-vendor can enforce more than we can.** A harness that owns
  the tool loop can refuse an edit. We can refuse it in Claude Code, via hooks,
  and elsewhere we can only inform.
- **A2A has a real specification with a foundation behind it.** We have a
  working tool and a versioned record schema.
- **We are new.** Everything above is testable in twenty minutes; please do.
