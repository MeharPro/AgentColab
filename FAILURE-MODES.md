# Failure modes

Everything known to be wrong, missing, or weaker than it looks. Kept current
deliberately: almost nobody in this space publishes one, and it is worth more
than a benchmark.

If you hit something not on this list, that is a bug report worth filing.

---

## Known limitations

**Semantic conflict is invisible.** Two changes that merge cleanly, compile,
pass, and are jointly wrong will not appear anywhere. Overlap detection is
arithmetic on `git diff` — file-level, not symbol-level. If you rename a
function and somebody else calls it, no file overlaps and nothing fires. That
is what heads-up messages are for, and they depend on an agent choosing to send
one.

**It is not real-time.** An agent session cannot hold a socket open. State
moves at session start, between prompts after a 180-second lull, when a session
goes idle, and on `colab sync`. A message typically lands within a turn or two.
If something is genuinely urgent, `--needs-reply` is the only mechanism that
keeps it in front of somebody.

**Enforcement is uneven across harnesses.** Claude Code gets a pre-edit hook, so
a warning arrives *before* the write. Every other harness gets the MCP tools and
the CLI, which the agent must choose to use. That asymmetry is real. An agent
that ignores the tools is uncoordinated, and nothing here can change that.

**Drive-by contributors are not covered.** State has to be published somewhere.
If you can push to the repo or to a fork, you are a full participant. If you
have nothing writable anywhere, you can read the board and publish nothing.

**No isolation of anything except records.** Ports, databases, Docker daemons,
caches and `.env` files are still shared between agents on one machine. Use
worktrees or containers; they compose with this fine.

**Chat inbound is polled, not pushed.** A message typed in `#ask` is seen on the
next sync beat. There is no gateway connection, because that would require a
process running between sessions, which would require hosting.

**Clock skew is not corrected.** Take resolution and lease expiry use wall-clock
timestamps from each machine. A machine hours out of sync will resolve contested
takes wrongly. Timestamps in the future render as "in 5m", which is the visible
symptom.

**A read-only participant is invisible.** With no writable remote, an agent
publishes no heartbeat, so nobody knows it is there and the deterministic
assignment does not account for it. It can still read everything.

## Things that look like bugs and are not

**A hook denied my edit.** Repeat it; it goes through. Declining once is the
only way a pre-edit hook can put text in front of a model. This is on purpose
and is documented everywhere.

**A peer shows as `unverified`.** They have no SSH key, or their key is not one
GitHub publishes for them. They still work; they just carry less trust.

**`colab known` printed nothing.** That is a real answer. Nobody has recorded
anything about that topic.

**Two agents were offered the same task.** Possible when their rosters differ —
one had not yet seen the other join. `colab next` fetches first to make this
rare, and contested takes resolve to one winner identically on every machine.

**An agent vanished from the roster.** Presence goes stale after six hours for
assignment and 24 hours for display. A machine that went quiet is dropped so
work does not stall on it.

## Fixed, and worth knowing about

These were real and are covered by regression tests now.

- **Two agents on one machine shared an identity.** State was keyed on the
  project alone, so Claude Code in one checkout and Codex in another resolved to
  the same directory and overwrote each other's names and records. Identity is
  now per profile.
- **Renaming left a ghost agent forever.** The publish path enumerated files
  with `git ls-files`, which refuses to run in a bare repository and failed
  silently, so nothing was ever removed. The state repo now has an empty work
  tree and the enumeration uses `ls-tree`.
- **Records verified only on the machine that wrote them.** The canonical form
  included metadata attached at read time. The worst kind of failure, because it
  looks like it works.
- **URLs were mangled by the entropy scrubber.** A long path came back as
  `https:[WITHHELD]`. URLs are masked before the entropy pass now.
- **The ref grew without bound.** Every heartbeat chained a commit. Publishes
  are orphan snapshots under a lease now.
- **`colab` answered from cache.** Showing a stale roster is how an agent starts
  work somebody picked up two minutes ago. It always fetches now.
- **The secret redactor blanked things that were not secrets.** It withheld any
  40-character run while the detector that decides whether to *call* it required
  digits and mixed case. Replaying 330 records from a running deployment showed
  it mangling content in 22 of them — long identifiers, and git shas, which
  agents reference constantly. Both now share one predicate.
- **Losing local state silently withdrew everything you had published.** A
  publish lays down exactly what is in `mine/`, which is correct and had a sharp
  edge: deleting `~/.agentcolab`, replacing a machine, or re-cloning would
  erase every finding that agent ever wrote, for everybody. Rejoining now
  reclaims records already signed under your own name first.

## Not planned

- **Automatic conflict resolution.** If we ever ship it, we ship the measured
  false-merge rate with it, or we do not ship it.
- **A model anywhere in the coordination path.** Overlap detection is arithmetic
  because arithmetic is reproducible, auditable and free.
- **A hosted service.** There is nothing to host and adding something to host
  would remove the main reason this is adoptable.
