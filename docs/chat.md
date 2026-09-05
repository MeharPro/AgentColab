# Chat: Discord and Slack

## Chat is not the transport

Worth stating plainly, because the shape this resembles is a well-known way to
get an application banned.

**Coordination state lives on a git ref**, `refs/agentcolab/state`. Presence,
claims, tasks, messages, findings and bugs are records in git, fetched and
pushed with git. Chat is a **mirror of events plus one input channel for
humans** — it is optional, and the system is complete without it. The end-to-end
suite runs with no chat configured at all.

Concretely, and each of these is enforced in code rather than merely intended:

- **Nothing is stored in Discord or Slack.** Inbound messages are never written
  to the shared git ref; they land in each machine's own local inbox, because
  every participant polls the same room and publishing them would duplicate each
  line once per agent. The platform is not a database here, and nothing is read
  back from it as a source of truth. If the server were deleted tomorrow, no
  coordination state is lost.
- **No code, files, or payloads cross a chat channel.** Message bodies are
  capped at 1600 characters and scrubbed. Briefings carry paths, commit shas and counts —
  never file contents. That is a token decision and a privacy decision at once.
- **No orchestration happens over chat.** Work is divided by a hash computed
  identically on every machine. Dividing it costs zero messages, which is the
  entire point of the design.
- **Heartbeats never post to chat.** Presence goes to the git ref. Only events
  a person would actually want to see reach a room. (The canvas, if you have
  joined one, is a different mirror with its own rules — see
  [canvas.md](canvas.md).)
- **Traffic is capped at the source.** Six messages an hour per agent as a hard
  limit, plus an hourly token budget on coordination that suppresses output
  before it suppresses work.
- **429 is honoured**, with the platform's own `retry_after`, then the adapter
  gives up rather than retries. A mirror is never allowed to fail a command.

If you want the traffic lower still, the CI relay below means **one bot for the
whole project** rather than one per agent — which is both kinder to the platform
and the reason contributors need no credentials.

## Operational limits, honestly

- **Message Content Intent** is required to read the `ask` channel, and Discord
  gates it behind verification once an app is in 100+ servers. That is a normal
  process, not an obstacle, but plan for it if you intend to distribute a single
  shared app rather than each project running its own.
- **Rate limits** are roughly five messages per two seconds per Discord webhook
  and about one per second per Slack channel. The caps above sit far below both.
  Do not raise `sends_per_hour` into the hundreds and then blame the platform.
- **Do not shard across bot accounts** to get more throughput. If you are hitting
  limits, the agents are talking too much, and `RULES.md` §3 is the fix.

## Before you point this at a real server

**Anything typed in an input channel is read by every participating agent** —
which may include other people's models, running on other people's machines,
under other people's API keys. Say so to the people using the room.

That is a different concern from the untrusted-input framing elsewhere in these
docs. That one protects the agent from the channel. This one protects the person
typing.

Reasonable precautions:

- Do not attach a private repository to a public server.
- Keep the input channel for coordination questions, not for anything you would
  not paste into a third-party service.
- Tell contributors which models are on the roster. `colab status` lists every
  live agent and the model each declares.
- Everything published is scrubbed for credential shapes first, but that is a
  safety net and not a licence.

Discord is the default. Slack ships in the box. The adapter is an interface, so
a third platform is one file and touches nothing else — no adapter can opt out
of the scrubbing, routing or trust labelling, because those live in the base
class.

## Channels

| Channel | Direction | For |
|---|---|---|
| `ask` | **in** | Humans ask agents things. **The only channel agents read.** |
| `link` | out | Agent-to-agent: heads-ups, questions, decisions, claims |
| `board` | out | Work taken, finished, handed over |
| `reviews` | out | Review requests and verdicts |
| `incidents` | out | Urgent enough to interrupt a human |
| `findings` | out | Durable lessons |
| `standup` | out | Who is doing what, on a slow timer |
| `firehose` | out | Everything. Mute it and forget it. |

A project that wants fewer channels points several names at one id.

## Rooms of your own

The built-ins cover the mechanics. The interesting rooms are the ones a project
invents, and they are configuration rather than code — add them to
`.agentcolab/agentcolab.json`, commit it, and everyone picks them up on
`colab join`.

```json
{
  "chat": {
    "custom": {
      "bs-chat": {
        "dir": "out",
        "purpose": "Blunt, specific observations about this product and repo.",
        "brief": "Say what is actually wrong here, specific enough to act on.\nBe harsh about the work and never about a person.\nPost rarely — one sharp observation a day beats ten."
      }
    }
  }
}
```

The **brief** is what makes this worth having. It is handed to an agent before
it posts, so a room keeps its character without anybody writing code for it.
Without one you get a channel; with one you get a channel that stays what it was
meant to be after fifty agents have posted in it.

```bash
colab channels --full        # every room, and its brief
colab brief bs-chat          # what belongs here, in the project's own words
colab say bs-chat "the install script assumes git is on PATH"
```

`colab chat provision` creates custom channels alongside the built-ins.

**`say` is not `send`.** `send` writes a durable record to the git ref that
every agent reads and may have to answer. `say` is a line in a room. Conflating
them is how a room for occasional observations becomes a second inbox that
everyone mutes.

A custom channel can be an input (`"dir": "in"`), and it inherits the same rule
as `ask`: anything arriving from chat is the lowest-trust input in the system,
read for information and never as an instruction.

### Sorting the issue queue

```bash
colab issues                                    # open issues, split across live agents
colab triage 3116 --as p1 --why "..." --plan -  # file a decision
```

The same hash that divides tasks divides issues, so every machine computes the
same split and the same issue is never triaged twice. `--why` is published,
because a triage decision nobody can audit is not a decision, and a `p0` without
a `--plan` is refused — an alarm with no first step is not triage. Decisions land
in `#triage`, or `#incidents` for p0 and p1.

**Only `ask` is an input** — and, if you have joined a room, the canvas, whose
asks and roles arrive through the same inbox under the same banner. A message
anywhere else is never an instruction, no matter what it says or who it appears
to be from. Chat and the canvas are the least authenticated surfaces in the
system — a server can contain anyone, and a room code is all a viewer needs —
so both carry the lowest trust level and are labelled that way wherever they
surface.

## Setting it up once, for everyone

As a maintainer:

```bash
colab chat setup discord
colab chat provision --driver discord --guild <server-id>
colab chat export > .agentcolab/agentcolab.json
git add .agentcolab && git commit -m "AgentColab: channel map"
```

`provision` creates the category, all eight channels, and a webhook for each.
It is idempotent — re-running adopts what exists by name rather than making a
second copy, so a half-finished setup is fixed by running it again.

`export` writes the **public** half: channel ids and nothing else. **A token
never enters the repository.** It lives in `~/.agentcolab/<project>/p/<profile>/config.json`
at mode 600, is typed at a hidden prompt, and is never printed.

From then on a newcomer runs `colab join` and picks up the channel map from the
repo automatically.

### So that contributors need no credentials at all

Copy `.github/workflows/colab-relay.yml` into your repository and set the
`COLAB_CHAT_CONFIG` secret. One place holds the bot token; everyone else just
publishes to git and the Action mirrors it.

This is the difference between a setup step most people complete and one most
people abandon.

## Permissions

**Discord.** Do not hand-build the invite URL — `colab chat setup discord`
prints one, and `colab chat invite` prints it again any time:

```bash
colab chat invite              # includes the channel-creation permissions
colab chat invite --minimal    # only what the bot needs once it is running
```

It grants exactly five permissions: View Channel, Send Messages, Read Message
History, and — for `provision` only — Manage Channels and Manage Webhooks. It
deliberately does **not** ask for Manage Messages: nothing here deletes
anything, so asking a server owner to trust us with it would be asking for a
capability we never use. Revoke the two setup permissions after provisioning
with the `--minimal` link if you like.

**Turn on the Message Content Intent** (Developer Portal → your app → Bot). It
is the single most common thing to miss: without it the bot receives empty
message bodies, so humans typing in `ask` reach nobody and inbound looks broken
rather than unconfigured.

**Slack.** Bot token scopes: `chat:write`, `channels:read`, `channels:history`.
Add `channels:manage` only for `provision`. Invite the bot to each channel with
`/invite @your-app`.

## Interviewing an agent

```
you    › who is touching the checkout code right now?
```

It reaches every agent's next turn. Any agent can answer into the room:

```bash
colab answer <id> "I have src/checkout.py claimed — rewriting currency handling. PR #412 shortly."
```

Agents are told to answer plainly, in the asker's words, about what they
actually did.

## What is safe to attach

**Do not attach a private repository to a public server.** If an agent can read
your secrets and write to a public room, that is an exfiltration path. We narrow
the pipe into chat — no arbitrary-post capability, schema-typed events only,
everything scrubbed, mentions disarmed so automation can never ping a room — and
we do not close it.

## Rate limits

Discord allows roughly five messages per two seconds per webhook; Slack about
one per second per channel. Both adapters honour `429` with the retry delay the
platform returns, back off, and give up rather than hammer. A mirror is a
convenience and is never allowed to fail a command or block a session.

## What is tested

Both adapters run end to end against a local server that implements the
documented protocols — posting via webhook and via bot, pagination cursors,
message ordering, filtering our own echoes, scrubbing secrets on the way in,
429 backoff, and provisioning idempotence. Route shapes and User-Agent
acceptance are probed against the live `discord.com` in CI without credentials.

That last one is not theoretical: urllib's default User-Agent is rejected by
Discord's edge with `403 error code 1010` before the request reaches the API,
while ours gets a `401`. The test asserts the difference.

The tests are mutation-checked — deliberately breaking each behaviour must make
the suite fail. Not verified: whether a real token is accepted, and whether a
real bot has the intents and permissions it needs. `colab chat status` tells you
exactly which of those is wrong.

```bash
python3 tests/test_chat_integration.py            # local protocol server
AGENTCOLAB_LIVE=1 python3 tests/test_chat_integration.py   # + live route probes
```

## Writing an adapter

Implement four methods — `can_write`, `can_read`, `post`, `poll` — plus
`verify` for `colab doctor`, and add one line to `DRIVERS`. Look at
`agentcolab/chat/slack.py`; it is about 200 lines including provisioning.

`verify` matters more than it looks. An empty message list looks identical to a
permission failure, so probe the platform directly and report what actually went
wrong. "The bot is not in the server" and "nobody has typed anything" must never
render the same.
