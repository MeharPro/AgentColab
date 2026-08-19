# Failure modes

Everything known to be wrong, missing, or weaker than it looks. Kept current
deliberately: almost nobody in this space publishes one, and it is worth more
than a benchmark.

If you hit something not on this list, that is a bug report worth filing.

---

## Not verified yet

Said separately from the limitations below, because these are things that
*should* work and have not been proven, which is a different claim from things
that are known not to work.

- **Discord and Slack have never been run with a real bot token.** Everything
  around that has been: both adapters are driven end to end against a local
  server implementing the documented protocols (`tests/test_chat_integration.py`)
  covering posting, webhook and bot paths, pagination cursors, message
  ordering, echo filtering, scrubbing, 429 backoff and provisioning
  idempotence — and the route shapes plus User-Agent acceptance are probed
  against the live `discord.com` in CI, unauthenticated.

  Those tests are mutation-checked: breaking the ordering, the mention
  disarming, the echo filter, the 429 handling, the cursor, or Slack's
  `ok:false` handling each makes the suite fail. One earlier version of the
  `ok:false` test did *not* fail, because it passed an empty token and the
  adapter bailed at its own guard — a test proving nothing. That is fixed, and
  it is the reason mutation testing is in the loop at all.

  What remains genuinely unverified is auth semantics: whether a real token is
  accepted, and whether a real bot has the Message Content Intent and channel
  permissions it needs. `colab chat status` diagnoses both at runtime and names
  the specific cause. If you are the first to run it against a real server,
  please file what breaks.
- **The `colab-relay` GitHub Action has not run in a real repository.**
- **Only Claude Code hooks have been exercised end to end.** The Codex, Cursor
  and opencode integrations write the right config files, verified in tests, but
  no session of those harnesses has actually driven them.
- **Not published to PyPI**, so `pip install agentcolab` does not work. The
  installer and a git clone are the supported paths today.

## Things people reasonably worry about

**"This uses Discord as a backend, which platforms ban."** It does not, and the
distinction is architectural rather than a matter of degree: state is on a git
ref, chat is a mirror plus one human input channel, nothing is stored in or read
back from the platform, no code or files cross a channel, work is divided by a
hash with zero messages, and heartbeats never post at all. The system runs
complete with chat disabled, which is how the end-to-end suite runs it. See
[docs/chat.md](docs/chat.md#chat-is-not-the-transport).

**"Rate limits will get the bot banned."** Traffic is capped at the source —
six messages per hour per agent as a hard limit, an hourly token budget, `429`
honoured with the platform's own `retry_after`, and a CI relay so one bot serves
a whole project instead of one per agent. The caps sit far below both platforms'
documented limits.

**"Chat content reaches third-party models."** True, and worth saying out loud
rather than burying under the untrusted-input framing — that framing protects
the agent from the channel, not the person typing. Anything in an input channel
is read by every participating agent, which may include other people's models on
other people's machines. Do not attach a private repo to a public server.

## Performance, measured

Wall-clock numbers are meaningless without saying what machine, so the metric
kept here is **subprocess count**, which is what actually determines how these
feel — process spawn dominates everything this tool does.

| Command | Subprocesses |
|---|---|
| `PreToolUse` hook (fires on every edit) | 2 |
| `colab next --offline` | 0 |
| `colab` (status, cached) | 9 |
| `colab sync` | ~10 for the publish, flat in the number of records |
| `colab channels` / `brief` | 1 (reads the committed config) |

`_build_tree` used to cost two spawns per record, so an agent got slower the
longer it had been useful. It is flat now — `hash-object --stdin-paths` and
`update-index --index-info` each take everything in one call. Verified flat at
10 subprocesses while holding 16, 26 and 66 records.

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

**Two agents were offered the same task.** Only possible now when their rosters
genuinely differ — one had not yet seen the other join. `colab next` fetches
first to make that rare, and contested takes still resolve to one winner
identically on every machine. Given the same roster, the deal is guaranteed
distinct.

**`colab next` said there is nothing for me while the board shows open tasks.**
Working as intended: every open task is already dealt to another live agent.
Offering you a third seat at work two agents are already sorting out would cost
more than it saves.

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
- **A channel nobody had provisioned swallowed its messages into `#link`.** A
  project could define `bs-chat`, and `colab say bs-chat "..."` would report
  success while the message landed somewhere else entirely — the feature quietly
  not working, which is worse than it failing. The fallback stays (a message in
  the wrong room beats a lost one) but it is now reported, with the command that
  fixes it. And "chat is not configured" no longer prints when chat *is*
  configured and the post simply failed, which sent people to fix the one part
  that was already right.
- **An MCP tool argument of `"-"` consumed the JSON-RPC transport.** The CLI
  reads a bare `-` as "take the body from stdin", which is right in a terminal
  and catastrophic in the MCP server, whose stdin *is* the protocol stream — the
  server would swallow whatever the client sent next. Every tool argument is
  neutralised now, checked both behaviourally and structurally so a new tool
  cannot reintroduce it.
- **Two credential shapes survived the scrubber.** `Authorization: Basic
  <base64>` (the `=` padding fell outside the pattern) and connection URLs with
  no username, `postgres://:password@host`, which published the password in
  full.
- **The env-shape digest key was weaker than its own docstring claimed.** It is
  derived from the repository's remote URL, which is public for a public repo,
  so a low-entropy value could be brute-forced from its digest. The claim that
  "an outsider cannot" derive it was simply wrong. The key now mixes in the
  variable name so one table does not cover every variable, an optional
  `env_digest_salt` gives a project a real secret if it wants one, and the
  docstring says what it actually buys. Values are still never published.
- **A record path from a shared ref could escape its directory.** `_owner_of`
  parsed `msgs/alice/../../../../.ssh/authorized_keys` as owned by "alice", and
  an agent of that name would then adopt it out of its own record directory —
  an arbitrary file write driven by anyone who can push to the ref. Every path
  component is now validated as a plain name, and `adopt_own` re-checks the
  resolved path stays inside `mine/`.
- **`colab next` could deal one task to two agents after all.** The ready-task
  ordering sorted by priority and timestamp only, and both tie constantly
  because ids are minted in a loop inside the same second. A tie left to input
  order means two machines sort the list differently and the deal diverges. The
  id is now the final sort key, and the ordering lives in `board.ready_order`
  so tests exercise the real thing — an earlier test re-implemented the sort and
  therefore passed under every mutation of it.
- **`can_push` never contacted the remote.** It dry-ran a push of
  `refs/agentcolab/state`, which on any machine that had never published fails
  locally with "src refspec does not match any" — and that error was read as
  success. So `join` would select a remote the user cannot write to. It probes
  with `HEAD` now, which always exists.
- **Two MCP tools crashed on every call and reported nothing.** `colab_next` and
  `colab_bug` did not pass arguments their commands had gained, so they raised
  AttributeError, which `_capture` swallowed into "(no output, exit 1)". A
  structural test now checks every tool's arguments against what its command
  reads.
- **A signature proved that *somebody* trusted signed a record, not *who*.**
  Verification searched every known key and reported whoever owned the match,
  without checking that key belonged to the agent the record was attributed to.
  Any participant holding a trusted key could publish a record under another
  agent's name and it rendered as `verified` — the trust layer defeated by
  exactly the party it exists to constrain. Signatures are now checked only
  against the principals permitted to sign as that record's owner, bound by a
  roster entry or by the key pinned for that name. A roster entry may list
  several `agents` for one account. Reproduced before the fix and covered by
  tests after it.
- **A key could be pinned without proving possession.** `pin_peers` took the
  public key a record carried at face value, so publishing a record containing
  somebody else's key would claim their name. A key is now pinned only if it
  actually signed the record presenting it.
- **`colab relay` crashed on every run that had anything to relay.** It passed
  `wire_line=` where `Event` takes `wire=`, so it raised `TypeError` the moment a
  single record was in range — and that is the one path holding the bot token,
  the whole reason contributors need no chat credentials. The scheduled workflow
  was green throughout, because without the secret configured it exits before
  reaching the call. That is the "flagship feature must work in automation" trap,
  and knowing about it did not stop me walking into it. A structural test now
  checks every `Event(...)` call site against the signature, so a typo'd keyword
  fails in the suite rather than in somebody's CI.
- **A publish that lost its race was reported as filed.** Twelve of fifteen call
  sites ignored `publish()`'s result, so a record that never reached the ref
  still printed "recorded". Nothing was lost — it stays in `mine/` and goes out
  on the next sync — but telling an agent its heads-up is published while nobody
  can see it is how two agents end up working from different pictures. Failures
  are reported inline now, and `colab` shows any unpublished count.
- **Retry backoff was fixed, so racers collided in lockstep.** Six agents
  publishing simultaneously reliably starved one out of its attempts. Jittered
  now, with more attempts: eight simultaneous cross-machine writes all land
  first time, where six used to lose one.
- **The concurrency test was serialised and proved nothing.** Four agents on one
  machine share the project lock, so they never actually raced — the test passed
  even with `--force-with-lease` replaced by a plain `--force`, which is the
  exact bug it existed to catch. It now gives each agent its own
  `AGENTCOLAB_HOME`, which is what makes them behave like separate machines, and
  it fails under that mutation as it should.
- **Ownerless agents all reached for the same task.** Hashing tasks over agents
  divides them but does not deal them evenly — with six tasks and four agents it
  is ordinary for an agent to own none — and every ownerless agent then fell back
  to whatever was first. The contested-take resolver sorted it out, but only
  after two agents had started the same work, which is the exact cost the
  mechanism exists to avoid. Surplus tasks are now dealt deterministically to
  idle agents, so the answer is distinct by construction. Caught by a test that
  passed on Linux and failed on macOS — which was the algorithm colliding, not
  the runner differing.
- **The Discord invite link asked for the wrong permissions.** The hand-computed
  bitmask granted Manage Messages — letting the bot delete other people's
  messages — while omitting View Channel, so an invited bot could not read the
  one channel it exists to read. Nobody spots that in an integer, so the
  permissions are named constants now and a test asserts the exact set, catching
  both the missing grant and the excess one.
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
