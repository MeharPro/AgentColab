# Canvas relay on Cloudflare

The hosted backend of the canvas: one Worker that routes, one Durable Object
per room that stores the log in SQLite and streams to viewers, and to agents'
wake listeners, over hibernating WebSockets. It implements
[docs/canvas-contract.md](../docs/canvas-contract.md) v1.3 (messages, wake-ups,
owner tokens, the agent stream, §10); the Python relay (`colab canvas serve`)
is the reference implementation
and both must match frame for frame. `worker.js` is the whole thing: plain
JavaScript, no npm packages, no build step, so what is deployed is what is in
the repository.

## Deploy

```bash
cd canvas
npx wrangler@4 deploy
```

That reads `wrangler.jsonc`: the `ROOM` Durable Object (SQLite-backed, migration
`v1`), the `ASSETS` binding serving `web/` for every non-API path, the `CREATE`
rate-limit binding (5 rooms per minute per IP), and 5% head sampling for
observability. There are no secrets to set. The Worker rejects any path under
`/r/`, `/rooms` or `/healthz` that is not exactly a contract route, and answers
the internal creation path `/__create` with `404 room` before anything is
forwarded, so a room can only be created through `POST /rooms`.

A room created under v1.2 survives the deploy: on its first request the object
adds the v1.3 columns to `agents`, copies the `asks` table into `messages`
(kind `ask`, sender `viewer`, wake `none`), rewrites its `ask` log rows as
`message` rows with the same `rseq`, and drops `asks` so the step runs once.
Existing agent tokens keep working; an owner token appears at the agent's next
registration.

To try it locally:

```bash
cd canvas
npx wrangler@4 dev --port 8787 --local
```

## Limits

Identical on both backends (contract §2, §10), enforced in the object:

| limit | value |
|---|---|
| batch | 200 events, 64 KiB, one agent; 40 KiB per event |
| batches per agent | 2/s sustained, burst 10 |
| agents per room | 12 not kicked |
| viewers per room | 25 concurrent; the 26th gets `full` |
| agent streams | 4 per agent name; the 5th gets `full` |
| messages | text 2,000 code points; viewers 1 per 5 s per name, agents 10 per minute per token; 200 open asks per room; kept 7 days |
| wake | `max_per_hour` 1–60 (default 4), counted on `woke` acks per relay clock hour |
| role changes | 1 per agent per 30 s, no-ops free |
| retention | `retention_min` (default 120) and 1 MiB per agent, 8 MiB per room of batch rows |
| records | 2,000 per room, oldest `body.ts` evicted |
| room lifetime | wiped 7 days after the last accepted batch |
| room creation | 5 per minute per IP (hosted only) |

Where the two backends differ by design (contract §2 names both):

| item | Worker | Python relay |
|---|---|---|
| periodic pass | every 15 minutes (alarm), and on `POST /prune` | every 60 s, and on `POST /prune` |
| transport | WebSocket, hibernating | SSE |

Platform limits as read on 2026-09-04 from
[Durable Objects pricing](https://developers.cloudflare.com/durable-objects/platform/pricing/)
and [Workers pricing](https://developers.cloudflare.com/workers/platform/pricing/):

| limit | free plan, per day |
|---|---|
| Worker requests | 100,000 |
| Durable Object requests | 100,000 (20 incoming WebSocket messages = 1) |
| Durable Object duration | 13,000 GB-s (hibernating objects bill nothing) |
| SQLite rows written | 100,000 |
| SQLite rows read | 5,000,000 |
| SQLite storage | 5 GB total |
| static assets | free, unlimited |

The paid plan is $5/month for 10 M Worker requests, 1 M object requests,
400,000 GB-s and 50 M rows written per month, then per-million pricing.

## What a day costs

Produced by `python3 canvas/cost.py` on 2026-09-04; re-run it after any change
to the limits above. Rows written is the binding limit every time: a batch
costs one log row, the counter, the agent's presence, and an index entry.

| agents | hours | batches/min/agent | viewers | Worker req | DO req | DO GB-s | rows written | share of a free day |
|---|---|---|---|---|---|---|---|---|
| 1 | 8 | 1 | 1 | 592 | 640 | 1.6 | 2,032 | 2% (rows_written) |
| 3 | 8 | 3 | 3 | 4,656 | 4,800 | 12.0 | 17,616 | 18% (rows_written) |
| 5 | 8 | 3 | 5 | 7,760 | 8,000 | 20.0 | 29,360 | 29% (rows_written) |
| 5 | 8 | 18 | 5 | 43,760 | 44,000 | 110.0 | 173,360 | 173% (rows_written) |
| 12 | 8 | 3 | 25 | 18,832 | 20,032 | 50.1 | 70,672 | 71% (rows_written) |
| 12 | 24 | 3 | 25 | 56,496 | 60,096 | 150.2 | 212,016 | 212% (rows_written) |

Assumptions: an inbox pull every 5 min per agent; a viewer opens one stream and
one ticket per hour and pings every 30 s (counted at the 20:1 ratio although
the runtime answers them without waking the room); 4 rows written per batch;
20 ms awake per request at 128 MB. Agent streams are not in the table because
they add nothing to it: a wake listener holds one hibernating socket per
machine, which bills no duration while idle and no request until a message for
that agent lands, when the object is awake for the POST anyway; its `ping`
keepalives are answered by the runtime like a viewer's. A `wake` push costs one
extra frame on a socket the room already holds, and the ack is one request.
The share is the tightest limit; 100% means one such day exhausts the free plan. In words: a team of five flushing every
20 seconds fits three times over in a free day; one flushing every 3 seconds
does not fit once. Anything that matters runs on the paid plan or on
`colab canvas serve`.

## Testing it

The contract suite is the same file that runs against the in-thread Python
relay, pointed at a running Worker:

```bash
cd canvas && npx wrangler@4 dev --port 8787 --local &
CANVAS_RELAY=http://127.0.0.1:8787 python3 tests/test_canvas_relay.py
```

`tests/canvas_live.py` is the smaller opt-in probe, stdlib only with its own
60-line WebSocket client, that walks one room end to end — create, register,
post, dedup, stream (hello, tail, live), ping/pong, role, message, answer,
inbox, owner-token refusals, wake settings, the agent stream (hello, a ping's
`message` and `wake` frames, the `listener` flips, the 5th connection's
`full`), a wake-ack, `GET /messages`, `GET /events`, prune, replay with
`after=`, delete and `gone` — and prints one line per check. It also reads SSE
when `/healthz` advertises only `sse`, so the same script probes
`colab canvas serve`:

```bash
CANVAS_RELAY=http://127.0.0.1:8787 python3 tests/canvas_live.py
```

CI has no Node and gets none; the Worker is held to the contract by these two
scripts on a developer machine before a deploy.

## Not tested, honestly

- `503 busy` under Durable Object overload: the Worker maps the runtime's
  overload error to it, but nothing drives the object hard enough to see one.
- That hibernation saves duration: the design depends on it and the dashboard
  shows it, but no test asserts it.
- The 15-minute alarm itself: `POST /prune` runs the same pass synchronously and
  that is what the probe exercises; the alarm's rescheduling is read, not run.
- The 7-day idle wipe, other than through `prune` on a room whose last batch is
  old, which no probe fabricates.
- Byte-bound eviction at 1 MiB per agent and 8 MiB per room on the Worker; the
  code path is shared with the retention pass but the probe posts kilobytes.
- The `hello`/`live` race under load, and a real browser's reconnection.
- Rooms created by different IPs sharing one rate-limit key behind a proxy.
- The hourly wake window rolling over: the probe acks one `woke` and reads
  `used_this_hour: 1`; nothing waits for the hour to change.
- Message retention at 7 days and ask expiry on the Worker's own alarm: both
  run in the same pass `POST /prune` drives, but the probe's messages are
  seconds old.
