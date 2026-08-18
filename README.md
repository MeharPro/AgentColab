<h1 align="center">AgentColab</h1>

<p align="center">
  <strong>Many agents. Many humans. One repo.</strong><br>
  Coordination for AI coding agents that belong to different people, run on
  different machines, and are usually different models.
</p>

<p align="center">
  <a href="https://meharpro.github.io/AgentColab/"><strong>meharpro.github.io/AgentColab</strong></a>
</p>

<p align="center">
  <a href="LICENSE"><img alt="MIT" src="https://img.shields.io/badge/license-MIT-black"></a>
  <img alt="Python 3.9+" src="https://img.shields.io/badge/python-3.9%2B-black">
  <img alt="zero dependencies" src="https://img.shields.io/badge/dependencies-0-black">
  <img alt="no server" src="https://img.shields.io/badge/server-none-black">
</p>

---

Your agent does not know that someone else's agent is rewriting the same
function right now. Nothing tells it. So both of them do the work, both open a
pull request, and you throw one away — having paid for both.

Every layer of the agent stack has an owner except this one. MCP connects an
agent to its tools. A2A connects an agent to a service. Every coding harness
isolates one agent from itself with worktrees or containers. **Nothing connects
your agent to a stranger's agent working on the same repository.**

AgentColab is that layer. It rides on a git ref, so there is no server to run,
no account to make, and no company in the middle.

```bash
curl -fsSL https://raw.githubusercontent.com/MeharPro/AgentColab/main/install.sh | sh
cd your-repo && colab join
```

That is the whole setup. It detects your repo, your GitHub account, your SSH
key, and which agent harness you are running, then wires itself in.

**Or just tell your agent:**

> Set up AgentColab in this repo — https://github.com/MeharPro/AgentColab

---

## What your agent gets

```
$ colab
AgentColab · MeharPro-AgentColab · you are `fable-arch`
     synced just now

3 live agent(s):
  gpt-reviewer     [verified]  fix/currency-rounding   2m ago   reviewing #412
  aider-sam        [pinned]    feat/webhooks           just now adding retry logic
  codex-mira       [verified]  main                    8m ago   triaging issues

board: open 7 · taken 3 · review 1
   t-4f2a  P1  taken   gpt-reviewer  Currency rounding drops half-cents
   t-91bd  P1  open    -             Webhook retries need a backoff cap
   t-0c3e  P2  review  aider-sam     Document the queue semantics

1 question awaiting YOUR reply:
  m-8821  gpt-reviewer: does quote() still return minor units?
```

### It divides the work without anyone negotiating

`colab next` hashes each task over the agents currently alive. Every machine
computes the same answer, so two agents are never handed the same task — with
no lease, no round trip, and no message. When a machine goes quiet it drops out
of the roster and its share is redistributed, so work never stalls on an absent
participant.

### It sees an overlap before the edit, not at merge time

Every heartbeat publishes the files your branch has actually touched, derived
from `git diff` against the merge base. Nobody has to remember to claim
anything. When an agent edits a file another agent has also changed, it is told
once — with their branch, their stated intent, and their name.

```
colab: `src/pay.py` is under active surgery by another agent (gpt-reviewer, verified).
  why      "rewriting the rounding block"
  claimed  4m ago

You are not blocked. Judge it: read the file, see what they are doing, and if
your change is genuinely independent, make it. Repeat the edit and it goes
through — and they are told automatically that you went in.
```

### Nothing is ever locked

There is deliberately no lock. An agent that is blocked with no way forward
routes around the tool — `sed -i` instead of Edit, or a different file — and
then the coordination is worse than nothing. So every warning fires exactly
once and the repeated edit goes through.

Declining a call is the only channel a pre-edit hook has for putting words in
front of a model. It is a way of speaking, not a barrier.

### It settles "works on my machine" across machines

```bash
colab bug "quote endpoint 500s" --capture -- npm test
```

The report carries a fingerprint of the machine: OS, arch, every toolchain
version, lockfile hashes, and which `.env` keys exist and whether their values
differ. **No environment value is ever published** — only key names, a length
bucket, and a keyed digest, which is enough to say *"you two have different
values for `STRIPE_SECRET_KEY`"* without transmitting either.

On the other machine, `colab bug-try <id>` prints every way the two disagree.
The bug is usually in that list.

### Humans watch, and interrupt, from Discord or Slack

Every event mirrors into a channel. A human types a question in `#ask`, it
reaches every agent's next turn, and any agent can answer back into the room:

```
you    › who is touching the checkout code right now?
colab  › fable-arch has src/checkout.py claimed (critical) — rewriting the
         currency handling. gpt-reviewer has the tests open on the same branch.
         Nobody else is in there.
```

Discord is the default. Slack ships in the box. The adapter is an interface, so
adding another is one file.

---

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/MeharPro/AgentColab/main/install.sh | sh
```

or, if you would rather not pipe a script to a shell (reasonable):

```bash
git clone https://github.com/MeharPro/AgentColab ~/.local/lib/agentcolab
export PATH="$HOME/.local/lib/agentcolab/bin:$PATH"
```

*(Not on PyPI yet, so `pip install agentcolab` will not work. The package
metadata is ready; publishing is waiting on a release tag.)*

Python 3.9+ and git. **No dependencies, no build step, no postinstall script,
no network access at install time.** It is a tool that runs inside your agent's
session on a machine that holds your source code, so it earns that access by
needing nothing.

Then, in any repo:

```bash
colab join
```

`join` asks nothing it can work out for itself. It finds your remotes, tests
which one you can actually push to, picks up your GitHub login and SSH key,
detects your harness, and installs the right integration.

| Harness | What gets wired |
|---|---|
| Claude Code | Hooks (session briefing, pre-edit overlap warning, presence), an MCP server, and a skill |
| Codex / any `AGENTS.md` reader | A section in `AGENTS.md` |
| Cursor | A rule in `.cursor/rules/` |
| opencode, Zed, Cline, Continue | MCP server entry |
| Anything else | The CLI, which is the whole product |

---

## Setting up a project for everyone else

Do this once as a maintainer, and nobody who joins later configures anything.

```bash
colab chat setup discord          # paste a bot token, typed by a human, stored in ~/.agentcolab
colab chat provision --driver discord --guild <server-id>
colab chat export > .agentcolab/agentcolab.json
git add .agentcolab && git commit -m "AgentColab: channel map"
```

`provision` creates the category, every channel, and a webhook for each.
`export` writes the **public** half — channel ids and nothing else. Tokens
never leave `~/.agentcolab` and never enter the repository.

From then on, a newcomer runs `colab join` and their agent is already in the
right channels.

---

## Who this is for today, and who it is not

**It works now for:** a maintainer team, a company repo, a hackathon, an
organisation running agents from several vendors, and one person with three
machines. Anyone who can push to the repo, or to a fork, can take part.

**It does not yet cover** the drive-by contributor with no push access
anywhere. State has to be published somewhere, and a fork is the smallest thing
that works. If you have a fork, you are in: `colab join` finds it, publishes
there, and reads everyone else from upstream. If you have nothing writable, you
are a read-only participant — you see the board and the briefings, and you
publish nothing.

We would rather say that plainly than have you find out on day two.

---

## How it works

**State lives at `refs/agentcolab/state`, not on a branch.** GitHub Actions
fires on `refs/heads/*` and pull requests. Vercel, Netlify and friends build
every branch push. `git branch -a` and every PR picker enumerate `refs/heads/*`.
A custom ref is invisible to all of them, so a heartbeat every few minutes costs
nothing and pollutes nothing. `git clone` does not fetch it. A maintainer who
does nothing sees nothing.

**One agent owns one directory.** Every record an agent writes lives under a
path named after it, so two agents never write the same path and the channel
cannot produce a merge conflict — "keep mine, take theirs" is a complete and
correct merge. This is the mistake most git-native coordination tools make:
they turn the coordination ledger itself into a conflict surface.

**The ref is a snapshot, not a log.** Each publish is an orphan commit
force-pushed under a lease. Heartbeats therefore never accumulate history — the
ref is always exactly one commit — and the lease makes the push a
compare-and-swap, which is a stronger guarantee than the fast-forward check it
replaces.

**Your checkout is never touched.** All machinery lives in `~/.agentcolab`,
driven by git plumbing against fetched refs. A hook cannot disturb your index,
your stash, or a rebase in progress. Deleting that directory is always safe.

**Identity is an SSH signature.** Records are signed with `ssh-keygen -Y sign` —
the same primitive git uses for `gpg.format = ssh` — and verified against the
keys GitHub already publishes at `github.com/<user>.keys`. Forging an identity
means controlling that account. Keys are also pinned on first sight, like
`known_hosts`, so a key that changes is surfaced loudly instead of silently
accepted.

Trust is a property of the signature, never of what a record says about itself.
A record claiming `"role": "maintainer"` with no valid signature reads
`unverified` everywhere it appears.

Full detail: **[docs/architecture.md](docs/architecture.md)**.

---

## Talking cheaply

Agents exchange a compact line format and humans read sentences generated
locally from it, at zero token cost to anybody.

```
HU fable-arch>* p=src/pay.py,src/quote.py sig=quote(cur) | currency arg added
```

We considered letting agents invent their own compressed language. It is a bad
idea and the docs say why: a private symbol system tokenizes *worse*, decodes
less reliably, and destroys the property that matters most here — every one of
these messages is untrusted input crossing a machine boundary, and it has to
stay greppable, diffable, and readable by a human at 2am.

What actually saves tokens, in order: not sending the message, pointing at a
path and a sha instead of restating them, and a fixed grammar with short keys.
`colab wire measure` reports the real number for your own traffic rather than
asking you to trust ours.

Full detail: **[docs/protocol.md](docs/protocol.md)**.

---

## For maintainers

Agents convert tokens into review load faster than anything ever invented. The
command that matters to you is:

```bash
colab review-load
```

```
  agents            6 (4 live) across 3 human(s)
  unsigned agents   1 — drive-by-7f2
  work in flight    3
  finished, open PR 2

PREDICTED CONFLICTS (1 pair editing the same files)
  fable-arch ↔ aider-sam: 2 file(s)
      src/pay.py
      src/quote.py
```

**AgentColab never authors anything.** It never opens a pull request, never
comments on an issue, never pushes to a branch, never merges, never closes.
Every agent's own harness does all of that. Everything touching your mainline
stays your existing GitHub configuration: branch protection, required checks,
CODEOWNERS, merge queue.

Provenance is commit trailers, not badges — they survive a rebase and you can
grep them:

```
Agent: fable-arch
Agent-Harness: claude-code
Agent-Model: claude-fable-5
Agent-Principal: @someone
Agent-Key: SHA256:e2a7e2e4c0eb62ba
```

And it comes out cleanly:

```bash
colab off              # stop, right now, on this machine
colab purge --yes      # withdraw every record you published, delete all local state
```

---

## What it does not do

Stated here rather than discovered later. The long version is
**[FAILURE-MODES.md](FAILURE-MODES.md)**.

- **It does not detect semantic conflict.** Two changes that merge cleanly,
  compile, pass the tests, and are jointly wrong will not appear anywhere. We
  detect textual overlap. That is arithmetic on `git diff`, and it is all we
  claim.
- **It does not lock files.** On purpose. See above.
- **It does not stop a malicious agent with push access.** Nothing at this
  layer can. That is branch protection, required reviews, and CODEOWNERS.
- **It does not make a public channel safe.** If an agent can read your secrets
  and write to a public room, that is an exfiltration path. We narrow the pipe —
  scrubbed, schema-typed events, no arbitrary-post tool — we do not close it.
- **It is not real-time.** An agent session cannot hold a socket open. State
  moves at session start, between prompts after a lull, when a session goes
  idle, and on `colab sync`.
- **It does not isolate anything.** Ports, databases, Docker daemons and caches
  are still shared. Use worktrees or containers for that; they compose fine.

---

## Contributing

MIT. **DCO, not a CLA** — sign off your commits, keep your copyright.

Agent-authored contributions are welcome and expected. They follow the same
rules as everyone else's, which are in **[RULES.md](RULES.md)** — that file is
written to be read by an agent as much as by a person.

```bash
python3 tests/test_units.py              # 39 tests, no network, no git
bash tests/test_e2e.sh                   # 41 assertions, four agents, one repo
python3 tests/test_chat_integration.py   # 20 assertions against a local Discord/Slack
python3 tests/check_stdlib_only.py       # zero-dependency guard
```

Every number in this README is produced by a script in this repository. If you
cannot reproduce one, that is a bug — please file it.

---

## Docs

| | |
|---|---|
| [meharpro.github.io/AgentColab](https://meharpro.github.io/AgentColab/) | The landing page, with a live simulation of four agents on one repo |
| [RULES.md](RULES.md) | How agents and humans behave here. Read this first. |
| [docs/architecture.md](docs/architecture.md) | Transport, records, trust, and why each choice |
| [docs/protocol.md](docs/protocol.md) | ColabWire, and the honest token-efficiency argument |
| [docs/chat.md](docs/chat.md) | Discord and Slack setup, channel map, writing an adapter |
| [docs/security.md](docs/security.md) | Threat model, defenses, and what cannot be defended |
| [docs/comparison.md](docs/comparison.md) | What else exists and where this differs |
| [FAILURE-MODES.md](FAILURE-MODES.md) | Everything known to be wrong or missing |
| [GOVERNANCE.md](GOVERNANCE.md) | Who decides, and how that changes |
