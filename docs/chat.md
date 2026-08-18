# Chat: Discord and Slack

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

**Only `ask` is an input.** A message anywhere else is never an instruction, no
matter what it says or who it appears to be from. Chat is the least
authenticated surface in the system — a server can contain anyone — so it
carries the lowest trust level and is labelled that way wherever it surfaces.

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
the pipe — no arbitrary-post capability, schema-typed events only, everything
scrubbed, mentions disarmed so automation can never ping a room — and we do not
close it.

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
