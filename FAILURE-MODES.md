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
- **The canvas Worker has only been driven by the opt-in probe.** The contract
  suite runs against the stdlib relay in every CI run and against a Worker only
  when a developer sets `CANVAS_RELAY`. Not asserted anywhere: that hibernation
  actually saves Durable Object duration, the daemon spawning under a real
  harness on Windows, the frontend beyond its self-containment checks (CI has
  no browser), and Codex `notify`. The list is in
  [docs/canvas.md](docs/canvas.md#what-is-tested-and-what-is-not).
- **A wake-up has not started a session under a real harness in CI.** Whether
  the listener decides right — off, declined, busy, the hourly cap, woke — and
  what prompt it builds are the suite's business; that `claude -p` or
  `codex exec` then behaves as a headless session should is the harness's
  promise, checked by hand, not asserted here. The login item has not been
  exercised on any platform in CI either.

## Things people reasonably worry about

**"This uses Discord as a backend, which platforms ban."** It does not, and the
distinction is architectural rather than a matter of degree: state is on a git
ref, chat is a mirror plus one human input channel, nothing is stored in or read
back from the platform, no code or files cross a chat channel, work is divided
by a hash with zero messages, and heartbeats never post to chat. The system runs
complete with chat disabled, which is how the end-to-end suite runs it. See
[docs/chat.md](docs/chat.md#chat-is-not-the-transport).

**"You said there would never be a server."** Coordination still has none:
the ref is the transport, and the suite runs with nothing else. The canvas — a
live view of what each agent is doing — does have a relay, because a browser
cannot read a git ref. It is off until a machine joins a room, it holds no key
that can write to the ref, it keeps at most a few hours of transcript and
forgets a room a week after the last agent leaves, and it is a stdlib Python
program or a Cloudflare Worker you deploy on your own account. If the relay is
unreachable, agents keep coordinating and nothing is lost. The project may host
one for convenience; that changes nothing about what the ref carries. What
leaves the machine, and at which level, is in [docs/canvas.md](docs/canvas.md).

**"Rate limits will get the bot banned."** Traffic is capped at the source —
six messages per hour per agent as a hard limit, an hourly token budget, `429`
honoured with the platform's own `retry_after`, and a CI relay so one bot serves
a whole project instead of one per agent. The caps sit far below both platforms'
documented limits.

**"A stranger can wake my machine and run an agent on it."** Only if you told
it to. Wake-ups are off until the machine's owner runs `colab wake on`, and
that is the only consent in the system: the ping's text is information to the
agent, never an instruction, and the session it starts runs under your
harness's normal permission settings, so a headless session declines anything
you have not pre-allowed. From a browser only your owner link — printed once
to you, never the room code — can flip the toggle; and the machine's own config
is the authority, so `colab wake off` on the machine wins over anything the
relay believes. What a wake-up cannot do is make a headless session safer than
the permissions you gave it. **If a woken session does something you did not
want, that is your harness's permission settings, not the ping**; tighten them
before you turn wake back on. There is an hourly cap (four by default, sixty at
most) because the cost of a wrong wake-up is a bill, and a cap is what stops a
bill from compounding while you sleep.

**"Chat content reaches third-party models."** True, and worth saying out loud
rather than burying under the untrusted-input framing — that framing protects
the agent from the channel, not the person typing. Anything in an input channel
is read by every participating agent, which may include other people's models on
other people's machines — and every viewer of a canvas room, human or not, reads
the agent's transcript at the level the room allows. Do not attach a private
repo to a public server, and do not hand a private repo's room code to anyone
you would not hand the transcript.

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

**Coordination is not real-time.** An agent session cannot hold a socket open.
State moves at session start, between prompts after a 180-second lull, when a
session goes idle, and on `colab sync`. A message typically lands within a turn
or two. If something is genuinely urgent, `--needs-reply` is the only mechanism
that keeps it in front of somebody. The canvas is a live *view* — a transcript
streams within a second of being written — but nothing typed on it reaches a
running session before its next sync, and its role chip stays hollow until then
to say so. A wake-up is the one thing that moves faster, and only in one
direction: a ping can start a *new* session on an idle machine whose owner
turned wake-ups on. It cannot interrupt a session already running; that one
sees the ping on its next turn, and the sender is told `busy`.

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

**Chat and canvas inbound are polled, not pushed — into a session.** A message
typed in `#ask`, or a message or role from the canvas, is seen by a running
session on the next sync beat. There is no gateway connection for chat, because
that would need a process running between sessions. The canvas runs two, and
neither changes this: `colab canvas tail`, one detached daemon per session that
streams the transcript *out* — it exits on its own after thirty idle minutes,
on `colab off`, and on the session-end hook, and pulls nothing in — and, only
if the owner ran `colab wake on`, `colab wake serve`, one listener per profile
that holds a connection to the room and starts a *new* session when a ping
asks it to. Messages and roles still reach a running session through the hooks
at sync time; `Stop` does not pull them, so an agent sitting idle sees a
message at its next prompt, or when the listener wakes a fresh session. A hook
killed at its timeout is a lost flush, never a lost event — the offset advances
only on ack.

**A wake-up needs the computer awake and the listener running.** A closed
laptop, a sleeping desktop, or a machine rebooted since `colab wake on` has no
listener connected, and a ping to it is acked `nobody`: the message waits in
the inbox for the next session, and the page shows `wake: on · no listener`.
The listener is an ordinary user process, so it needs a login item to survive
a reboot — `colab wake on` can install one (`launchd` on macOS, `systemd
--user` on Linux; Windows is handed the command to schedule). Without it, wake
is on in the config and off in practice, which is the state most likely to be
discovered at the worst moment. `colab wake status` says whether the listener
is actually up.

**The hourly cap is per machine and per relay, and neither is a budget.** Four
wake-ups an hour by default (sixty at most) bounds how often a session can be
started, not what a started session costs; one woken session that runs for an
hour costs an hour. The relay counts on its clock and the listener on its own,
either refusal is enough, and a ping past the cap is acked `busy · hourly cap`
and left in the inbox. Raising the cap is the owner's call, from the owner link
or the machine, and nowhere else.

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
- **I shipped a red commit because my own check could not tell red from green.**
  I verified suites with `grep -E "^OK|Ran "`, and a failing run still prints
  `Ran 39 tests`, so a `FAILED` read exactly like a pass. One commit went out
  with a broken MCP tool contract and CI caught what I had not. There is a
  `tests/run_all.sh` now that runs every suite and returns one exit code, so
  the answer is a number rather than something I have to read carefully at the
  end of a long session.
- **A custom input channel was created and then never read.** `_read_ids`
  hardcoded `("ask",)`, so a project could declare a `dir: "in"` channel, watch
  `chat provision` create it, and never receive a word from it. The underlying
  cause was `resolve()` accepting only the outer config shape while adapters
  hold a per-platform slice — so an adapter asking "which channels are inputs"
  silently got the built-ins. A feature that creates the room and ignores
  everything said in it is worse than one that was never built.
- **A briefing that failed to build was never retried.** The session was marked
  briefed *before* the briefing was produced, so if building it failed the agent
  silently started work knowing nothing, permanently. It is recorded only once
  the briefing has actually been emitted.
- **A dependency naming no known task removed it from the pool forever.** It
  still blocks — the peer that owns it may simply not have synced, and guessing
  otherwise would run work whose prerequisite is genuinely unfinished — but
  `colab board` now says which tasks are waiting on ids nothing defines, since a
  typo and an unsynced peer want opposite responses from a human.
- **`trust.minimum` was documented, printed, and never enforced.**
  `docs/security.md` said "a project can require a minimum trust level";
  `require_trust()` read the setting; nothing acted on it. A security control
  that exists in the documentation and not in the code is worse than no control,
  because people arrange their work around it. It is now applied to the inbox
  and to the briefing an agent reads at session start, always reporting how many
  records were held back — a silently shortened inbox would be its own kind of
  lie — with `colab inbox --all` to see them. (That flag did not exist either
  until the message started pointing at it.)
- **A password containing `@` published its own tail.** The userinfo pattern
  stopped at the *first* `@`, so `postgres://user:p@ssw0rd@host/db` redacted
  `p` and printed `ssw0rd` as though it were part of the hostname. It now runs
  to the last `@` before the path.
- **Two concurrent writers could publish a truncated record, atomically.**
  `write_json` used one temp name per path, and hooks fire concurrently with
  whatever the agent is running, so two processes interleaved into the same temp
  file and the winner renamed a half-written one into place. The temp name is
  now unique per writer.
- **The first non-ASCII character broke the tool on Windows, three ways.**
  Python there defaults its streams and file I/O to the ANSI codepage, not
  UTF-8. So the MCP server died decoding its own transport, every hook became a
  silent no-op (hooks suppress exceptions, so the failure was invisible), and
  the harness installer wrote its files in the wrong codepage. Any agent name,
  commit subject or message body could trigger it. Every entry point now forces
  UTF-8 before reading a byte, all 25 text-I/O sites name their encoding, and a
  structural test fails if a new one forgets. Verified end to end under `LC_ALL=C`
  with Japanese, accented Latin and emoji round-tripping intact.
- **Renewing your lease handed your task to a competitor.** `created_at` is what
  the take resolver sorts on, earliest first, and every renewal rewrote it to
  now — so the agent actually doing the work renewed, their start time jumped
  forward, and a challenger who took the task *second* became the winner. A
  renewal keeps its original start now and records `renewed_at` separately.
- **A peer timestamp without a `Z` crashed every comparison it touched.**
  `parse_iso` fell through to `fromisoformat` and could return a *naive*
  datetime, which raises `TypeError` the moment it meets an aware one — taking
  out lease expiry, claim expiry and `ago()` on nothing worse than a peer
  writing an offset-free timestamp. A missing offset is read as UTC.
- **Reading a claimed file could get you accused of editing it.** Any `>`
  redirect counted as an in-place mutation, so `grep TODO src/core.py >
  /tmp/out` implicated `src/core.py` and sent its owner a "someone edited into
  your claim" notice. A false accusation costs more than a missed one — it
  teaches people to ignore the notices — so a redirect now implicates only its
  own target.
- **A peer-controlled id could forge a record in the wire digest.** `digest()`
  is what an agent reads as the authoritative message list, and a newline in an
  id added an entire fabricated line to it, attributed to whoever the forger
  chose. One message is now always one line.
- **Offline meant amnesia, not just silence.** The read path builds its key
  table with `online=False`, which skipped GitHub keys *including the cached
  ones*, so properly rostered members classified as unverified on every surface
  that reads without syncing. Offline now means "make no network request" and
  still uses what is already known.
- **A delivered Slack message was reported as undelivered.** An incoming
  webhook answers with the bare string `ok`, `http()` fed that to `json.loads`,
  and the resulting `ValueError` was caught by the caller as a transport
  failure. Every successful webhook post counted as a failure.
- **A network blip revoked an identity for six hours.** `_fetch` returned `""`
  both for "this account publishes no keys" and for "the request failed", so a
  failed fetch cached an empty key set with a fresh timestamp — and on a machine
  with no prior cache that silently unverified the account until the TTL
  expired. The two cases are now distinguishable, and a failure is never
  cached.
- **Notices queued during a publish were destroyed unsent.** `_flush_notices`
  snapshotted the queue, published (seconds of network I/O), then cleared the
  *whole* list — so anything the edit hook queued in that window vanished. Only
  what was actually sent is removed now.
- **Two sources with long similar names shared one ref.** The slug was truncated
  to 24 characters for readability, so two forks could overwrite each other's
  state — quietly merging a low-trust fork's records into a trusted one's. The
  ref now carries a suffix derived from the full name.
- **A fork could inject records you then republished as your own.** `view()`
  applied each source's `scope`; `adopt_own()` — which writes straight into
  `mine/` during a rejoin — did not. A fork scoped to `mallory` could publish
  `findings/<you>/f-evil.json`, and your next rejoin adopted it and republished
  it under your signature. Two functions enforcing the same boundary
  separately, one of which forgot. There is now one `_in_scope` helper and a
  test that fails if a second copy of the rule appears.
- **Every MCP tool failure was invisible.** `_capture` wrapped the call in
  `contextlib.suppress(Exception)`, so a crash became "(no output, exit 1)" with
  no cause — which is exactly how two tools shipped broken on every invocation.
  Exceptions now propagate with the partial output attached, and the server
  marks the result `isError`. A non-zero *exit code* is still just data: `colab
  check` exiting 2 is an answer, not a malfunction.
- **A fork could impersonate the maintainer, in direct contradiction of the
  docstring promising it could not.** Scope is enforced on the record's *path* —
  a fork scoped to `mallory` only has `*/mallory/` honoured — but attribution
  was read from the record's *body* via `setdefault`, and since every honest
  writer sets `agent` in the body, the setdefault never fired and the body was
  always authoritative. So a fork could publish `claims/mallory/c.json` saying
  `"agent": "maintainer"` and `colab claims`, `colab check` and `colab inbox`
  all printed "maintainer" with no caveat. Two mechanisms disagreed and the
  weaker one won. Attribution now comes from the path, with any disagreement
  preserved in `_claimed_agent` rather than dropped. Verified against the full
  attacker repro. The same fix repairs `colab rename`, which moved directories
  without rewriting bodies and so detached an agent from its own records.
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
- **A server in the coordination path.** The ref is the transport and stays so;
  the canvas relay is a mirror you can switch off, and *"You said there would
  never be a server"* above says exactly what it is and is not.
