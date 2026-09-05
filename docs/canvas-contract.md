# AgentColab Canvas — relay contract v1.2

This document is the single source of truth for four independent implementations: the Python client (`agentcolab/canvas.py`), the Python relay (`agentcolab/canvas_relay.py`), the Cloudflare Worker + Durable Object (`canvas/worker.js`), and the frontend (`canvas/web/index.html`). If two of them disagree, the one that matches this document is right. v1.2 supersedes v1.1 and v1; §9 lists every change and the decision behind it.

## 0. Conventions

- All bodies are JSON (UTF-8) except `POST /r/{room}/events`, which is NDJSON (`Content-Type: application/x-ndjson`), one event per line; the trailing `\n` is optional, and blank or whitespace-only lines are skipped (§3.8).
- `Content-Type` is not validated on any route. The Python relay requires `Content-Length` on every request that carries a body (no chunked request bodies); the Worker accepts both.
- Sizes are UTF-8 bytes unless a limit says "code points". Four human-typed fields — ask text, viewer name, role text, room name — are capped in Unicode code points and additionally in bytes at 4× the code-point cap (§2). The relay strips control characters from all four — every code point below 0x20, tab and newline included (these are single-line fields), plus 0x7F–0x9F — before the code-point/byte check and before storage.
- Timestamps are ISO-8601 UTC with millisecond precision: `2026-09-04T10:00:00.123Z`. The `ts` inside a `record` body is the record's own timestamp and may be second-only.
- `MUST`/`MUST NOT` are binding on the named party. Unknown top-level fields in requests are ignored; unknown fields in responses and frames MUST be ignored by readers (forward compatibility).
- Every error response has body `{"error": "<code>", "hint": "<one sentence a program or a person can act on>"}`. Codes: `schema` (400), `auth` (401), `forbidden` (403), `room` (404), `agent` (404), `oversize` (413), `rate` (429, with `Retry-After` seconds), `full` (503), `busy` (503, with `Retry-After`; only the Worker answers it, under Durable Object overload — the Python relay never does, and the conformance suite does not test it). A body that does not parse as JSON, or a query parameter that is not of the documented type, is `400 schema`.
- Auth is `Authorization: Bearer <credential>`. The relay decides which credential class it received by prefix/shape (§1) and answers `403 forbidden` when the class is wrong for the route, `401 auth` when the value is unknown, expired, or rotated. No `Authorization` header, a non-Bearer scheme, or a bearer value matching none of the four shapes → `401 auth`. Check order on every `/r/{room}/…` route: room exists (`404 room`) → credential class (`403 forbidden`) → credential value (`401 auth`) → body size (`413 oversize`) → body and query (`400 schema`) → target agent (`404 agent`) → rate (`429 rate`) → capacity (`503 full`). A request that fails any check has no side effect.
- No CORS headers. The frontend is always served by the relay it talks to.
- Clients send `User-Agent: AgentColab/1 (+https://github.com/AgentColab/AgentColab)`.
- The relay MUST NOT edit any event. It validates, rejects, or stores each event so that `parse(stored)` deep-equals `parse(sent)` after `sent.agent` is set to the token's name (§6.3): string leaves byte-identical, key order irrelevant, no truncation, no re-scrubbing. It writes exactly one envelope field, `agent` (§4.1). It removes from an accepted batch only `agent`-kind snapshots, which it consumes (§4.2), and records whose `id` it already holds (§6.4).

## 1. Credentials

Symbol alphabet for all generated strings: `23456789abcdefghjkmnpqrstvwxyz` (30 symbols; no `0 1 i l o u a e`, so no words form).

| class | shape | example | stored as | grants |
|---|---|---|---|---|
| room code | `xxxx-xxxx-xx` (10 symbols, ~49 bits) | `k7mq-p3xw-4h` | plaintext (it is the room id) | `GET /r/{room}`, `GET /records`, `POST /ticket`, `GET /events`, `POST /asks`, `PUT /roles/{agent}`, `GET /stream` (header form) |
| join code | `<room code>.<24 symbols>` (~118 bits) | `k7mq-p3xw-4h.b6hj3kx9w2mrp4tq7vy8zn5c` | sha256 | `POST /agents/{name}`, `DELETE /agents/{name}`, `PUT /policy`, `POST /prune`, `DELETE /r/{room}` |
| agent token | `at-<32 symbols>` | `at-7x2kq9mw4hp3vb8nt5rj6zc2yd4fg7hk` | sha256, bound to one agent name | `POST /events`, `GET /inbox`, `PUT /roles/{own name}`, `DELETE /agents/{own name}` |
| viewer ticket | `vt-<32 symbols>`, expires `policy.ticket_ttl_s` seconds after minting (600 by default) | `vt-q4z8k7mw2hp9vb3nt6rj5zc8yd2fg4hk` | sha256 + expiry | `GET /stream` (query form) |

`<room4>` is the first four symbols of the room code — `k7mq` for `k7mq-p3xw-4h` — and prefixes every ask id (§3.10), so ask ids never collide across rooms in an agent's local read-state.

Generation MUST use a cryptographic source (`os.urandom`, `crypto.getRandomValues`). Join codes, tokens and tickets are never returned twice. One ticket may open any number of concurrent `/stream` connections until it expires; it survives reconnects within its TTL.

## 2. Limits (identical on both backends)

| limit | value | on breach |
|---|---|---|
| batch | ≤ 200 events, ≤ 65,536 bytes, one agent | `413 oversize` (whole batch) |
| event | ≤ 40,960 bytes | `rejected[{why:"oversize"}]`, rest accepted |
| batches per agent | token bucket: 2/s sustained, burst 10 | `429 rate`, `Retry-After: <s>`, batch not consumed |
| agents per room | 12 not kicked | `POST /agents` → `503 full` |
| concurrent viewers per room | 25 | 26th receives `{"t":"full"}` as its only frame (no `hello`), then close |
| open asks per room | 40 | `POST /asks` → `503 full` |
| asks per viewer name | 1 per 5 s | `429 rate` |
| ask text | 500 code points and ≤ 2,000 bytes; viewer name 40 code points and ≤ 160 bytes; both control-stripped first (§0) | `400 schema` |
| role text | 60 code points and ≤ 240 bytes; control characters stripped first (§0); unchanged text is a no-op | `400 schema` |
| room name | 60 code points and ≤ 240 bytes, control-stripped first (§0); absent or empty → `"room"` | `400 schema` |
| role changes per agent | 1 per 30 s, every caller incl. the owner token; no-ops do not count | `429 rate` |
| records per room | 2,000 in the `records` map; oldest `body.ts` evicted first (compared as instants; missing or unparsable sorts oldest) | — |
| retention | `policy.retention_min` (default 120, 5–720), deleting rows of every kind older than it, **and** ≤ 1 MiB per agent, ≤ 8 MiB per room, counting `batch` rows only (UTF-8 bytes of the raw request body that produced each row); the agent bound deletes that agent's oldest `batch` rows, the room bound the room's oldest `batch` rows of any agent; every deletion raises the prune horizon `H` (§6.7) | — |
| retention pass | byte bounds on insert; everything time-driven on the periodic pass (Worker 15 min, Python relay 60 s) and on `POST /prune` | — |
| replay on connect | `after=N`: rows with `rseq > max(N, H)`, ≤ 2 MiB; fresh connect: per agent, whole batches until ≥ 80 positional events or ≥ 256 KiB (soft cap, §5) | `hello.gap` set (`after=N` only) |
| tickets | `policy.ticket_ttl_s` (default 600, 1–3600) | `401 auth` on `/stream` |
| ask lifetime | `policy.ask_ttl_s` (default 86,400, 1–604,800), then `state:"expired"` on the next periodic pass or `POST /prune` — `open` everywhere until then | — |
| room lifetime | wiped 7 days after the last accepted batch | `404 room` |
| room creation (hosted Worker only) | 5 per minute per IP | `429 rate` |

Expiry instants are fixed at minting/creation (`minted + ticket_ttl_s`, `ts + ask_ttl_s`); a later policy change affects only tickets and asks created after it.

## 3. Routes

### 3.1 `POST /rooms` — create a room

An absent or empty body is treated as `{}`.
Auth: none (rate-limited on the hosted Worker). Body (every field optional):
```json
{"name":"agentcolab","policy":{"max_stream":"tools","retention_min":120,"ticket_ttl_s":600,"ask_ttl_s":86400}}
```
`201`:
```json
{"room":"k7mq-p3xw-4h","join_code":"k7mq-p3xw-4h.b6hj3kx9w2mrp4tq7vy8zn5c",
 "policy":{"max_stream":"tools","retention_min":120,"ticket_ttl_s":600,"ask_ttl_s":86400},
 "relay":"https://canvas.agentcolab.dev","url":"https://canvas.agentcolab.dev/#k7mq-p3xw-4h"}
```
`max_stream` ∈ `summary|tools|full` (default `tools`); `retention_min` 5–720 (default 120); `ticket_ttl_s` 1–3600 (default 600); `ask_ttl_s` 1–604800 (default 86400); `name` ≤ 60 code points and 240 bytes after control stripping (§0); absent or empty → `"room"`. The response `policy` always carries all four fields. The Python relay fills `relay` and `url` from `--public-url` when given, else from `http://<Host header>`.

Errors: `400 schema` (a policy value out of range or of the wrong type, `name` too long); `429 rate` (hosted Worker, per IP).

### 3.2 `GET /r/{room}` — snapshot
Auth: room code. `200`:
```json
{"room":"k7mq-p3xw-4h","name":"agentcolab","rseq":4812,
 "policy":{"max_stream":"tools","retention_min":120,"ticket_ttl_s":600,"ask_ttl_s":86400},
 "agents":[{"name":"mehar-claude-code","harness":"claude-code","human":"mehar",
            "model":"claude-fable-5-1","stream":"tools",
            "registered_at":"2026-09-04T09:58:00.000Z","last_seen":"2026-09-04T10:00:00.123Z",
            "kicked":false,
            "role":{"role":"reviewer — read PRs, do not push","viewer":"mehar","set_seq":4790,"ts":"2026-09-04T09:59:10.000Z"},
            "snapshot":{"...":"the body of this agent's newest accepted agent event (§4.2), or null"}}],
 "asks":[{"id":"ca-k7mq-4791","seq":4791,"to":"mehar-claude-code","viewer":"sam",
          "text":"why did the hook path change?","ts":"2026-09-04T09:59:40.000Z",
          "state":"open","answer":null}]}
```
`rseq` is the room's current log position. Agent objects carry `name, harness, human, model, stream, registered_at, last_seen, kicked, role, snapshot`: `stream` is the requested value (the level in force is `min(stream, policy.max_stream)`), `role` the current role object or `null`, `snapshot` the body of the newest accepted `agent` event or `null`. `agents` ascends by `name` and keeps kicked agents (`kicked:true`) until the 7-day wipe. `asks` ascends by `seq` and holds every open ask plus the 40 answered/expired asks with the largest `seq`.

Errors: `401 auth`; `403 forbidden` (join code or agent token); `404 room`.

### 3.3 `POST /r/{room}/ticket` — mint a viewer ticket
Auth: room code. Body: `{}` (an absent or empty body is treated as `{}`). `201`:
```json
{"ticket":"vt-q4z8k7mw2hp9vb3nt6rj5zc8yd2fg4hk","ttl":600}
```
`ttl` is `policy.ticket_ttl_s` at minting. Errors: `401 auth`; `403 forbidden`; `404 room`.

### 3.4 `GET /r/{room}/stream?after=N` — live stream
Auth: `?ticket=vt-…` **or** `Authorization: Bearer <room code>` (non-browser clients, tests). The query `ticket` is checked when present; the `Authorization` header is consulted only when `ticket` is absent. Worker: WebSocket upgrade, `101`. Python relay: `200`, `text/event-stream`. No credential, or an expired or unknown one → `401 auth` before the upgrade and before any frame. `after` omitted → fresh connect; `after=N` → replay of rows with `rseq > max(N, H)` (`H` is the prune horizon, §6.7). On the Python relay the `Last-Event-ID` request header is honoured as `after` and wins over the query parameter; a `Last-Event-ID` that is not a non-negative integer is ignored. Frames in §5. The 26th concurrent viewer is accepted, receives `{"t":"full"}` and is closed.

Errors: `400 schema` (`after` present but not a non-negative integer; absent means `0`); `401 auth`; `403 forbidden` (join code or agent token in the header); `404 room`.

### 3.5 `GET /r/{room}/events` — history and polling fallback
Auth: room code. Query: exactly one of `after=N` (rows with `rseq > N`, ascending) or `before=N` (the `limit` rows with the largest `rseq < N`, returned ascending); neither or both → `400 schema`. Optional `agent=<name>` returns only `batch` rows of that agent; without it every log row type (`batch`, `ask`, `role`, `policy`) is returned; an unknown or kicked `agent` → `{"rseq":<current>,"frames":[],"more":false}`. `limit` 1–200 rows (default 50); outside that range or not an integer → `400 schema`. `200`:
```json
{"rseq":4817,"frames":[{"t":"batch","rseq":4813,"agent":"mehar-claude-code","events":[]}],"more":true}
```
`rseq` is the room's current log position. `more` is `true` when further rows matching the query (row kind and `agent` filter included) exist beyond `limit` in the query direction (older for `before`, newer for `after`). Rows are exactly as delivered on `/stream`; the polling bootstrap (§5) counts positional events — exactly the kinds of §4.1 — across them.

Errors: `400 schema`; `401 auth`; `403 forbidden`; `404 room`.

### 3.6 `POST /r/{room}/agents/{name}` — register (or re-register) an agent
Auth: join code. `name` MUST match `[a-z0-9][a-z0-9-]{0,47}`. Body:
```json
{"harness":"claude-code","human":"mehar","model":"claude-fable-5-1","stream":"tools"}
```
`harness`, `human` and `model` are optional (`null` when absent), each ≤ 120 code points and 480 bytes after control stripping (§0). `stream` ∈ `summary|tools|full`, default `tools`; `off` is not a legal value. `200`:
```json
{"token":"at-7x2kq9mw4hp3vb8nt5rj6zc2yd4fg7hk","rseq":4812,
 "policy":{"max_stream":"tools","retention_min":120,"ticket_ttl_s":600,"ask_ttl_s":86400},
 "effective_stream":"tools"}
```
Re-registering rotates the token (the old one answers `401` from then on), replaces all four body fields and keeps `registered_at`, history, role and snapshot; a kicked name re-registers the same way and `kicked` is cleared. `effective_stream = min(body.stream, policy.max_stream)`. Emits an `agent` frame (no `rseq`, §5). A 13th name among agents not kicked → `503 full`.

Errors: `400 schema` (bad `name`, bad `stream`, ill-typed body); `401 auth`; `403 forbidden` (room code or agent token); `404 room`; `503 full`.

### 3.7 `DELETE /r/{room}/agents/{name}` — leave or kick
Auth: join code, or that agent's own token. `204`, no body. The token is invalidated; history, role and snapshot stay until retention, and the agent object stays in `GET /r/{room}.agents` and `hello.room.agents` with `kicked:true` until the 7-day wipe; emits an `agent` frame with `kicked:true`. On an already-kicked name: `204`, no frame.

Errors: `401 auth`; `403 forbidden` (room code, or the token of a different agent); `404 room`; `404 agent` (never registered).

### 3.8 `POST /r/{room}/events` — post a batch
Auth: agent token. Body: NDJSON of events (§4). `202`:
```json
{"rseq":4813,"accepted":17,"dup":0,"rejected":[{"id":"ev-1a2b3c4d5e6f7a8b","why":"policy"}]}
```
- The relay MUST set every event's `agent` to the token's name before validation, storage and broadcast.
- `agent`-kind events are consumed, not stored: the newest per agent becomes `agents[name].snapshot`, `last_seen` is updated, an `agent` frame is emitted per accepted snapshot; they count in `accepted`.
- `record`-kind events are deduplicated by `id` across the room: an id already in the `records` map counts in `dup` and is not stored again; a new one enters the map **and** stays in the batch row.
- Every remaining accepted event is appended, in the order sent, as one `batch` frame with one `rseq`, delivered to every connected viewer before the response. When nothing remains (all rejected, consumed or duplicate) no row is written and `rseq` is the room's current position — `202 {"rseq":<current>,"accepted":0,"dup":0,"rejected":[…]}` when all were rejected. A dup batch and an all-rejected batch each still consume one rate-bucket token and update `last_seen`.
- A batch containing an accepted `answer` (§4.2) writes the `batch` row first, then one `ask` row per newly answered ask, in ask-`seq` order; the `202` `rseq` is the batch row's. An `answer` to an ask already `answered` or `expired` is stored in the batch row, changes nothing and emits no `ask` frame.
- `rseq` is the log position of this batch, or of the identical earlier batch when `dup` applies to the whole body.
- `why` ∈ `oversize` (raw line > 40,960 bytes), `policy` (§4.4), `kind` (unknown kind), `schema` (missing/ill-typed envelope field, §4.1; a non-blank line that is not a JSON object, reported with `"id":null`; an `answer` whose `body.ask` is missing, not a string or names no ask in the room, or whose token name is not the ask's `to`). Per-event checks run on the raw NDJSON line in the order oversize → schema → kind → policy; the first failure is reported.
- Batch dedup: sha256 over the raw request body bytes, computed before any overwrite; a match with a stored row within retention is not stored again: `202 {"rseq":<original>,"accepted":0,"dup":<n>,"rejected":[]}`, `<n>` = the count of non-blank lines.
- Lines: blank or whitespace-only lines are skipped and count toward nothing; the trailing `\n` is optional; the 200-line cap and `dup:<n>` count non-blank lines; a body with no non-blank line → `202 {"rseq":<current>,"accepted":0,"dup":0,"rejected":[]}`.
- `413 oversize` when the body exceeds 64 KiB or 200 non-blank lines; `429 rate` on the bucket, with `Retry-After`; nothing is stored in either case. `401 auth` on a rotated or kicked token; `404 room` after deletion.
- Byte bounds (§2) are enforced on this insert: while this agent's `batch` bytes exceed 1 MiB delete this agent's oldest `batch` rows; then while the room's `batch` bytes exceed 8 MiB delete the room's oldest `batch` rows of any agent; both raise `H`.

Errors: `401 auth`; `403 forbidden` (room code or join code); `404 room`; `413 oversize`; `429 rate`.

### 3.9 `GET /r/{room}/inbox?after=N` — the agent's sync-time pull
Auth: agent token. `200` for `?after=4790`:
```json
{"rseq":4817,
 "role":{"role":"reviewer — read PRs, do not push","viewer":"mehar","set_seq":4790,"ts":"2026-09-04T09:59:10.000Z"},
 "asks":[{"id":"ca-k7mq-4791","seq":4791,"viewer":"sam","text":"why did the hook path change?","ts":"2026-09-04T09:59:40.000Z"},
         {"id":"ca-k7mq-4814","seq":4814,"viewer":"sam","text":"is the Windows path covered?","ts":"2026-09-04T10:00:20.000Z"}]}
```
`role` is the current role or `null`. `asks` are every open ask addressed to this agent whose `seq > N`, ascending, with no per-pull cap (the room bound of 40 open asks is the bound); an ask's `seq` is the `rseq` of the `ask` frame that announced it. The response `rseq` is the room's current position and is what the client stores as its next `after`. `viewer` is carried as typed. Updates `last_seen`.

Errors: `400 schema` (`after` present but not a non-negative integer; absent means `0`); `401 auth`; `403 forbidden`; `404 room`.

### 3.10 `POST /r/{room}/asks` — a viewer asks an agent
Auth: room code. Body:
```json
{"to":"mehar-claude-code","text":"is the Windows path covered?","viewer":"sam"}
```
`viewer` is required and non-empty; `text` and `viewer` are control-stripped before their caps are checked (§0). `201`:
```json
{"id":"ca-k7mq-4814","seq":4814}
```
The ask is one log row; `seq` is that row's `rseq` and the id is `ca-<room4>-<rseq>` — `k7mq` from `k7mq-p3xw-4h`, row 4814; there is no separate ask counter. Emits an `ask` frame with `state:"open"`. When the agent named in `to` posts an `answer` event whose `body.ask` matches, the relay sets `state:"answered"`, stores `answer:{text, ts}` (`ts` = the event's), and emits an `ask` frame as a new row after the batch row (§3.8); the ask keeps its original `id` and `seq`. `ask_ttl_s` after `ts`, the periodic pass or `POST /prune` sets `state:"expired"` on an ask that is still `open` (never on an answered one) and emits an `ask` frame — until that pass runs, the ask is `open` everywhere (inbox, counts, snapshot).

Errors: `400 schema` (`text` over 500 code points or 2,000 bytes, `viewer` absent, empty or over 40 code points, `to` absent); `401 auth`; `403 forbidden`; `404 room`; `404 agent` (`to` unknown or kicked); `429 rate` (same `viewer` within 5 s); `503 full` (40 open asks).

### 3.11 `PUT /r/{room}/roles/{agent}` — suggest a role
Auth: room code (`viewer` required and non-empty), or the agent's own token (`viewer` forced to `"owner"`). Body:
```json
{"role":"reviewer — read PRs, do not push","viewer":"mehar"}
```
`"role": null` or `""` clears. `200`:
```json
{"set_seq":4815}
```
The relay strips control characters from `role` (and `viewer`) before the 60-code-point cap is checked and before comparing. Unchanged text → `200` with the existing `set_seq`, no frame, no rate-cap consumption; clearing an already-null role → `200 {"set_seq": <set_seq of the last role frame for this agent, or 0>}`, no frame, no rate-cap consumption. Otherwise emits a `role` frame (one log row) and `set_seq` is that frame's `rseq`; a cleared role is a `role` frame with `"role":null`. The rate cap (1 per agent per 30 s) applies to every caller, the owner token included.

Errors: `400 schema` (`role` over 60 code points or 240 bytes, `viewer` absent or empty with room-code auth, `viewer` over 40 code points); `401 auth`; `403 forbidden` (join code, or the token of a different agent); `404 room`; `404 agent` (unknown or kicked); `429 rate`.

### 3.12 `PUT /r/{room}/policy` — change the ceiling
Auth: join code. Body — any subset of the four fields:
```json
{"max_stream":"summary","retention_min":60,"ticket_ttl_s":300,"ask_ttl_s":3600}
```
`200`:
```json
{"policy":{"max_stream":"summary","retention_min":60,"ticket_ttl_s":300,"ask_ttl_s":3600}}
```
Emits a `policy` frame (one log row) carrying the full policy, even when nothing changes. Lowering `max_stream` takes effect on the next batch; `retention_min` on the next retention pass; the TTLs per §2.

Errors: `400 schema` (a value out of range, wrong type, or a body with none of the four fields); `401 auth`; `403 forbidden`; `404 room`.

### 3.13 `DELETE /r/{room}` — delete the room
Auth: join code. `204`, no body. All storage wiped; viewers receive `{"t":"gone"}` and are closed; every credential of the room answers `404 room` from then on; the room's `rseq` values are never reused.

Errors: `401 auth`; `403 forbidden`; `404 room`.

### 3.14 `POST /r/{room}/prune` — run the retention pass now
Auth: join code. Body: `{}` (an absent or empty body is treated as `{}`). `204`, no body. Runs synchronously, before responding, the periodic pass: delete rows of every kind older than `retention_min` (raising the prune horizon `H`, §6.7), re-check byte bounds, drop expired tickets, expire asks still `open` past `ts + ask_ttl_s` (one `ask` frame each; answered asks are never expired), wipe a room idle over 7 days (viewers receive `{"t":"gone"}`; still `204`). This is how §6's time-driven obligations are tested.

Errors: `401 auth`; `403 forbidden`; `404 room`.

### 3.15 `GET /healthz`
Auth: none. `200`:
```json
{"ok":true,"backend":"worker","transports":["ws"],"version":"1"}
```
Python relay: `"backend":"python","transports":["sse"]`. No error status.

### 3.16 `GET /` and any non-API path
Serves `canvas/web/index.html`. The room code rides in the URL fragment and is never sent to the server as a path. A path beginning with `/r/`, `/rooms` or `/healthz` that is not exactly a route and method of the table below → `404 room` with hint `no such route`; every other path → `index.html`. The Worker forwards only the paths of this route table to the Durable Object; the internal room-creation stub path `/__create` is answered `404 room` before forwarding, so it is unreachable from outside.

Worker route table:

| method | path | § |
|---|---|---|
| `POST` | `/rooms` | 3.1 |
| `GET` | `/r/{room}` | 3.2 |
| `POST` | `/r/{room}/ticket` | 3.3 |
| `GET` | `/r/{room}/stream` | 3.4 |
| `GET` | `/r/{room}/events` | 3.5 |
| `POST` | `/r/{room}/agents/{name}` | 3.6 |
| `DELETE` | `/r/{room}/agents/{name}` | 3.7 |
| `POST` | `/r/{room}/events` | 3.8 |
| `GET` | `/r/{room}/inbox` | 3.9 |
| `POST` | `/r/{room}/asks` | 3.10 |
| `PUT` | `/r/{room}/roles/{agent}` | 3.11 |
| `PUT` | `/r/{room}/policy` | 3.12 |
| `DELETE` | `/r/{room}` | 3.13 |
| `POST` | `/r/{room}/prune` | 3.14 |
| `GET` | `/healthz` | 3.15 |
| `GET` | `/r/{room}/records` | 3.17 |

### 3.17 `GET /r/{room}/records` — every held record
Auth: room code. No body. Request:
```
GET /r/k7mq-p3xw-4h/records
Authorization: Bearer k7mq-p3xw-4h
```
`200`:
```json
{"rseq":4817,
 "events":[{"v":1,"id":"rec-3f9c2a17b04e6d55","agent":"alice-claude-code",
   "session":"8c5053f6-ba4d-4d25-9c34-43ebe9694c18","lane":"main",
   "epoch":0,"seq":107008,"ts":"2026-09-04T10:15:00.000Z","kind":"record",
   "harness":"claude-code","model":"claude-fable-5-1","ref":"m-20260904T101500-9a1b2c",
   "body":{"family":"msg","kind":"question","rid":"m-20260904T101500-9a1b2c",
    "from":"alice-claude-code","to":"bob-codex","subject":"does the hook run under Windows?",
    "paths":["agentcolab/hooks.py"],"reply_to":null,"task":null,"state":null,"owner":null,
    "blocked_by":[],"trust":"verified","ts":"2026-09-04T10:15:00Z"}}]}
```
`rseq` is the room's current log position. `events` is every record the relay holds (§6.4), in the same order as the `records` frames of a fresh connect (§5), `[]` when the map is empty; not paged, since the map holds at most 2,000. The polling bootstrap calls it once after `GET /r/{room}` (§5); a polling viewer otherwise sees records only as they arrive in new `batch` rows.

Errors: `401 auth`; `403 forbidden` (join code or agent token); `404 room`.

## 4. Events

### 4.1 Envelope
```json
{"v":1,"id":"ev-7c1e0a93b2d44f10","agent":"mehar-claude-code",
 "session":"8c5053f6-ba4d-4d25-9c34-43ebe9694c18","lane":"main",
 "epoch":0,"seq":106752,"ts":"2026-09-04T10:00:00.123Z","kind":"text",
 "harness":"claude-code","model":"claude-fable-5-1","ref":null,"body":{}}
```

| field | type | rule |
|---|---|---|
| `v` | int | always `1` |
| `id` | string ≤ 64 | `ev-<16 hex>` from `records.content_id` — Claude: `content_id("ev", sessionId, uuid, str(block_index))`; Codex: `content_id("ev", thread_id, str(line_no), str(block_index))`; synthetic kinds: `content_id("ev", session, kind, ts)`. `rec-<16 hex>` for records: `content_id("rec", rid, updated_at_or_state)`. Same content → same id. Viewers MUST dedup by id. |
| `agent` | string | **overwritten by the relay** with the token's name; clients fill it anyway |
| `session` | string ≤ 64 | Claude `sessionId`, Codex thread id |
| `lane` | string ≤ 64 | `main` or a subagent id (Claude `agentId`, Codex child thread id) |
| `epoch` | int ≥ 0 | transcript generation; bumped on inode change or shrink |
| `seq` | int ≥ 0 | `line_no * 256 + block_index` (1-based line, block ≤ 255) for transcript-derived kinds; the lane's current high-water seq for the others |
| `ts` | string | harness timestamp verbatim, or the emitter's clock (`canvas.iso_ms()`) for synthetic kinds |
| `kind` | string | one of §4.2 |
| `harness` | string | `claude-code`, `codex`, … |
| `model` | string or null | as reported by the harness |
| `ref` | string or null | `tool_use_id`/`call_id` on tool events; the ask id on `answer`; the record id (`rid`) on `record` |
| `body` | object | per kind |

*Relay schema validation, exhaustively* (`why:"schema"` on failure): `v` == 1; `id` string 1–64; `kind` string (an unknown value is `why:"kind"`); `body` object; `session` string ≤ 64 (may be empty); `lane` string ≤ 64, non-empty; `epoch` and `seq` integers ≥ 0; `ts` string ≤ 40; `harness` string ≤ 32; `model` string ≤ 64 or `null`; `ref` string ≤ 128 or `null`; `agent` anything (overwritten). Nothing else is validated; unknown envelope fields are stored unchanged. Every other rule in the table is the client's.

**Ordering and dedup are within `(agent, session, lane)`.** Display order inside that key is `(epoch, seq, rseq)`, where `rseq` is that of the enclosing `batch` frame (`0` inside a `records` frame). Positional kinds are, unconditionally, the events whose `kind` is one of `text`, `thinking`, `tool_call`, `tool_result`, `prompt`, `session`, `gap`; the tails of §5 and the polling bootstrap of §3.5/§5 count exactly those. `agent`, `record` and `answer` are non-positional and carry the lane's high-water `seq`. Clients also set `seq` to the lane's high-water for daemon-originated `session`/`gap` events — a client note only; the relay does not tell them from transcript-derived ones. The frontend draws a gap marker only for explicit `gap` events. One agent has one window; each `(session, lane)` is one tab.

### 4.2 Kinds and examples

Every example below is a complete event and sits well inside its cap.

**`agent`** — presence snapshot (≤ 32 KiB; `surface.files` ≤ 400). Not a log row: the relay consumes it (§3.8) and re-broadcasts the agent object in an `agent` frame. `alive` ∈ `daemon|hook`; `state` ∈ `working|tool|idle|waiting|gone`; `role` is the canvas role object as last received (or `null`); `role_seen_seq` is the `set_seq` of the newest role the agent has read (`0` when none); `self_role` is the heartbeat's free-text self-description (`"contributor"` by default), never a canvas role.
```json
{"v":1,"id":"ev-2b9d4e71c0a35f88","agent":"mehar-claude-code",
 "session":"8c5053f6-ba4d-4d25-9c34-43ebe9694c18","lane":"main",
 "epoch":0,"seq":106754,"ts":"2026-09-04T10:00:30.000Z","kind":"agent",
 "harness":"claude-code","model":"claude-fable-5-1","ref":null,
 "body":{"human":"mehar","harness":"claude-code","model":"claude-fable-5-1",
  "branch":"main","head":"b280922","dirty_files":3,"intent":"canvas design","title":"Canvas design",
  "self_role":"contributor",
  "state":"tool","tool":{"name":"Bash","ref":"toolu_01QjGx","since":"2026-09-04T09:59:58.000Z"},
  "role":{"role":"reviewer — read PRs, do not push","viewer":"mehar","set_seq":4790,"ts":"2026-09-04T09:59:10.000Z"},
  "role_seen_seq":4790,
  "surface":{"base":"a1b2c3d4e5f6","files":["agentcolab/hooks.py","docs/canvas.md"],"count":2,"truncated":false},
  "stream":"tools","alive":"daemon","daemon":{"state":"running","reason":null}}}
```
*State derivation (daemon), evaluated over the lane's newest events, first match wins:* `gone` after a `session{state:"end"}`; `waiting` when the newest block is a `tool_call` named `AskUserQuestion` or a permission denial; `tool` when the newest `tool_call` in the lane has no matching `tool_result` (by `ref`); `working` when the newest event is a non-final `text`, a `thinking`, or a `prompt`; `idle` after a `text{final:true}` with nothing newer, or when the Stop hook wrote `idle-<sid>`. `tool.since` is the `ts` of that `tool_call`.

*Cadence (client):* one snapshot every 60 s while anything in the body changed since the last one, else every 5 min; and always within 1 s of a role change being observed (that is what turns the chip solid).

**`text`** — one assistant text block (≤ 32 KiB, head 28 + tail 4).
```json
{"v":1,"id":"ev-7c1e0a93b2d44f10","agent":"mehar-claude-code",
 "session":"8c5053f6-ba4d-4d25-9c34-43ebe9694c18","lane":"main",
 "epoch":0,"seq":106752,"ts":"2026-09-04T10:00:00.123Z","kind":"text",
 "harness":"claude-code","model":"claude-fable-5-1","ref":null,
 "body":{"text":"I'll start by scouting the repo.","final":false}}
```
Truncated form of the body: `{"text":"<head>\n… 61,440 bytes cut …\n<tail>","final":true,"truncated":true,"bytes":94208}`.

**`thinking`** — `full` only (≤ 8 KiB). Empty thinking is `{"text":"","redacted":true}`.
```json
{"v":1,"id":"ev-9a4f2c6e8b1d3057","agent":"mehar-claude-code",
 "session":"8c5053f6-ba4d-4d25-9c34-43ebe9694c18","lane":"main",
 "epoch":0,"seq":106753,"ts":"2026-09-04T10:00:00.456Z","kind":"thinking",
 "harness":"claude-code","model":"claude-fable-5-1","ref":null,
 "body":{"text":"The hook must not import http."}}
```

**`tool_call`** (≤ 8 KiB). `paths` are repo-relative. Below `full`, content-bearing keys (`content`, `new_string`, `old_string`, `edits`, `cells`, `patch`, `new_source`) are removed from `args` and described in `omitted`.
```json
{"v":1,"id":"ev-5e3a1c7f9b2d4680","agent":"mehar-claude-code",
 "session":"8c5053f6-ba4d-4d25-9c34-43ebe9694c18","lane":"main",
 "epoch":0,"seq":106754,"ts":"2026-09-04T10:00:01.000Z","kind":"tool_call",
 "harness":"claude-code","model":"claude-fable-5-1","ref":"toolu_01QjGx7Ab2",
 "body":{"name":"Edit","args":{"file_path":"agentcolab/hooks.py"},"paths":["agentcolab/hooks.py"],
  "omitted":{"old_string":{"bytes":212,"lines":6},"new_string":{"bytes":260,"lines":7}}}}
```
A Bash call at `tools`: `"body":{"name":"Bash","args":{"command":"git status --short","description":"Show working tree status"},"paths":[],"omitted":{}}`.

**`tool_result`** (≤ 8 KiB). `text` only at `full` (head 6 KiB + tail 2 KiB). Images are never sent; an image result is `image:true` plus `media_type`.
```json
{"v":1,"id":"ev-c1d2e3f4a5b6c7d8","agent":"mehar-claude-code",
 "session":"8c5053f6-ba4d-4d25-9c34-43ebe9694c18","lane":"main",
 "epoch":0,"seq":107008,"ts":"2026-09-04T10:00:02.200Z","kind":"tool_result",
 "harness":"claude-code","model":"claude-fable-5-1","ref":"toolu_01Qa9",
 "body":{"ok":true,"exit":0,"bytes":1834,"lines":41,"paths":[],"image":false}}
```
At `full` the body adds `"text":"M agentcolab/hooks.py\n…","truncated":false`.

**`prompt`** — human turns only (≤ 8 KiB; 200 code points and ≤ 800 bytes at `summary`).
```json
{"v":1,"id":"ev-0f1e2d3c4b5a6978","agent":"mehar-claude-code",
 "session":"8c5053f6-ba4d-4d25-9c34-43ebe9694c18","lane":"main",
 "epoch":0,"seq":106496,"ts":"2026-09-04T09:59:50.000Z","kind":"prompt",
 "harness":"claude-code","model":"claude-fable-5-1","ref":null,
 "body":{"text":"Fix the failing test in tests/test_units.py"}}
```

**`session`** (≤ 1 KiB). `state` ∈ `start|end|compact|idle|error|abort`; optional `source`, `reason`, `title`.
```json
{"v":1,"id":"ev-a1b2c3d4e5f60718","agent":"mehar-claude-code",
 "session":"8c5053f6-ba4d-4d25-9c34-43ebe9694c18","lane":"main",
 "epoch":0,"seq":256,"ts":"2026-09-04T09:58:00.000Z","kind":"session",
 "harness":"claude-code","model":"claude-fable-5-1","ref":null,
 "body":{"state":"start","source":"startup","title":null}}
```

**`record`** — a coordination record mirrored from the merged view (≤ 4 KiB). `family` ∈ `msg|claim|task|take|review|bug|finding`; `kind` is the record's own kind (`question`, `reply`, `heads-up`, …); `to` is `*` or a name; `trust` ∈ `maintainer|member|verified|pinned|unverified|chat`. The relay deduplicates records by `id` across the room and holds at most 2,000 (§6.4); tasks are mirrored from `board.tasks(store)` with `_blocked_by` overlaid from `board.open_tasks(store)`, so `state`, `owner` and `blocked_by` are the computed ones.
```json
{"v":1,"id":"rec-3f9c2a17b04e6d55","agent":"alice-claude-code",
 "session":"8c5053f6-ba4d-4d25-9c34-43ebe9694c18","lane":"main",
 "epoch":0,"seq":107008,"ts":"2026-09-04T10:15:00.000Z","kind":"record",
 "harness":"claude-code","model":"claude-fable-5-1","ref":"m-20260904T101500-9a1b2c",
 "body":{"family":"msg","kind":"question","rid":"m-20260904T101500-9a1b2c",
  "from":"alice-claude-code","to":"bob-codex","subject":"does the hook run under Windows?",
  "paths":["agentcolab/hooks.py"],"reply_to":null,"task":null,"state":null,"owner":null,
  "blocked_by":[],"trust":"verified","ts":"2026-09-04T10:15:00Z"}}
```
Task body: `{"family":"task","kind":"task","rid":"t-…","from":"alice-claude-code","to":"*","subject":"port hooks to Windows","paths":[],"reply_to":null,"task":"t-…","state":"blocked","owner":"bob-codex","blocked_by":["t-…"],"trust":"verified","ts":"…"}`.

**`answer`** — the agent's reply to an ask (≤ 3 KiB). `ref` and `body.ask` are the ask id; the relay consults only `body.ask`, never `ref`. It rejects with `why:"schema"` when `body.ask` is missing, not a string or names no ask in the room, and when the token's name is not the ask's `to`. An `answer` to an ask already `answered` or `expired` is stored in the batch row, changes nothing and emits no `ask` frame.
```json
{"v":1,"id":"ev-6d5c4b3a29181706","agent":"mehar-claude-code",
 "session":"8c5053f6-ba4d-4d25-9c34-43ebe9694c18","lane":"main",
 "epoch":0,"seq":107008,"ts":"2026-09-04T10:01:00.000Z","kind":"answer",
 "harness":"claude-code","model":"claude-fable-5-1","ref":"ca-k7mq-4791",
 "body":{"ask":"ca-k7mq-4791","text":"The hook path changed so PreToolUse stays local."}}
```

**`gap`** (≤ 256 B). `reason` ∈ `backlog|oversize|policy|rewrite|restart|spool`.
```json
{"v":1,"id":"ev-1122334455667788","agent":"mehar-claude-code",
 "session":"8c5053f6-ba4d-4d25-9c34-43ebe9694c18","lane":"main",
 "epoch":0,"seq":120064,"ts":"2026-09-04T10:02:00.000Z","kind":"gap",
 "harness":"claude-code","model":"claude-fable-5-1","ref":null,
 "body":{"from_seq":106752,"to_seq":120064,"count":52,"reason":"backlog"}}
```

### 4.3 Client-side caps are the contract
The relay enforces only the 40 KiB event cap, the envelope schema, and the policy rules in §4.4. The per-kind caps above are the Python client's obligation (`canvas.sanitise`, §7); a relay that receives a 30 KiB `prompt` stores it unchanged.

### 4.4 Policy enforcement (relay)
Given `policy.max_stream`, the relay MUST reject with `why:"policy"`:
- at `summary`: any `thinking`; any `tool_result` with `text`; any `tool_call` whose `args` is an object with one or more keys (`tool_call.body` may carry `name`, `paths`, `omitted`; `args: {}` is accepted); any `text`; any `prompt` whose `text` is over 200 code points or 800 bytes;
- at `tools`: any `thinking`; any `tool_result` with `text`; any `tool_call.args` with a content-bearing key (`content`, `new_string`, `old_string`, `edits`, `cells`, `patch`, `new_source`) among its top-level keys — nested objects are not inspected;
- at `full`: nothing.

Rejection is per event; the rest of the batch is stored. It applies to every batch, including one from a client registered before the policy was lowered.

## 5. Stream frames

Every frame is one JSON object. On WebSocket: one text message per frame. On SSE: `id: <rseq>\ndata: <json>\n\n` for frames that carry an `rseq`, `data: <json>\n\n` for the others; `: keepalive\n\n` every 15 s.

**Frames with an `rseq`** are exactly the rows of the room log: `batch`, `ask`, `role`, `policy`. Replay, `GET /events` and live delivery use the same shapes. **Frames without an `rseq`**: `hello`, `records`, `agent`, `live`, `full`, `gone`. There is no `{"t":"gap"}` frame; `hello.gap` is the only gap signal.

```json
{"t":"hello","transport":"ws","backfill":"tail","gap":null,
 "room":{"room":"k7mq-p3xw-4h","name":"agentcolab","rseq":4812,
         "policy":{"max_stream":"tools","retention_min":120,"ticket_ttl_s":600,"ask_ttl_s":86400},
         "agents":[{"...":"exactly the objects of GET /r/{room}.agents"}],
         "asks":[{"...":"exactly the objects of GET /r/{room}.asks"}]}}
```
`room` is the object `GET /r/{room}` would return at that instant — always current, kicked agents included; `room.rseq` is the log position when the snapshot was read and the only position `hello` carries (no top-level `rseq`); the frontend seeds `lastRseq` from it. `transport` ∈ `ws|sse`. `backfill` ∈ `tail` (fresh connect: `records` and per-agent tails follow) | `replay` (`after=N`: rows with `rseq > max(N, H)` follow, `H` the prune horizon of §6.7) | `none` (`after=N` and no such row). `gap` is `null` or `{"before_rseq": M}`, rows with `rseq < M` not delivered: when `N < H`, `M = H + 1`; the 2 MiB replay budget applies after that — when the qualifying rows exceed 2 MiB, the relay sends only the newest 2 MiB ascending and `M` becomes the first replayed `rseq`. Always `null` on a fresh connect.

```json
{"t":"records","events":[{"v":1,"id":"rec-3f9c2a17b04e6d55","kind":"record","...":"…"}]}
```
Fresh connect only: every record the relay holds, ≤ 200 events per frame, split into several frames when needed, zero frames when the map is empty; `GET /r/{room}/records` (§3.17) returns the same events in the same order.

```json
{"t":"batch","rseq":4813,"agent":"mehar-claude-code","events":[{"v":1,"id":"ev-7c1e0a93b2d44f10","...":"…"}]}
```
```json
{"t":"ask","rseq":4814,"ask":{"id":"ca-k7mq-4814","seq":4814,"to":"mehar-claude-code","viewer":"sam",
 "text":"is the Windows path covered?","ts":"2026-09-04T10:00:20.000Z","state":"open","answer":null}}
```
In the `open` form `ask.seq` and the id's last part equal the frame's `rseq`. The `answered` form carries `"answer":{"text":"…","ts":"…"}`; the `expired` form carries `"answer":null`. Both are new rows with their own, larger `rseq` and keep the ask's original `id` and `seq`.
```json
{"t":"role","rseq":4815,"agent":"mehar-claude-code",
 "role":{"role":"reviewer — read PRs, do not push","viewer":"mehar","set_seq":4815,"ts":"2026-09-04T10:00:25.000Z"}}
```
`role.set_seq` equals the frame's `rseq`. A cleared role is `"role": null`.
```json
{"t":"agent","agent":{"name":"bob-codex","harness":"codex","human":"bob","model":"gpt-5.6","stream":"tools",
 "registered_at":"2026-09-04T10:00:40.000Z","last_seen":"2026-09-04T10:00:40.000Z","kicked":false,
 "role":null,"snapshot":null}}
```
No `rseq`. Carries the full agent object (the same shape as `GET /r/{room}.agents[]`, `snapshot` included). Emitted on registration, on kick, and on every accepted `agent` snapshot.
```json
{"t":"policy","rseq":4816,"policy":{"max_stream":"summary","retention_min":120,"ticket_ttl_s":600,"ask_ttl_s":86400}}
```
```json
{"t":"live"}
```
Sent once the backfill is complete; carries no `rseq`. The frontend flips the pill on it.
```json
{"t":"full"}
```
Followed by close (viewer cap).
```json
{"t":"gone"}
```
Followed by close (room deleted).

**Order on a fresh connect**: `hello` → `records` (zero or more frames) → per-agent `batch` tails → `live` → live frames. An agent's tail is its most recent `batch` rows, chosen newest-first until they hold ≥ 80 positional events (exactly the kinds of §4.1) or ≥ 256 KiB (raw request-body bytes, §6.7), whole batches only (soft cap), at least the newest, delivered oldest-first; one agent's tail completes before the next begins; agent order is not part of the contract; kicked agents' tails are included; an agent with no batch rows contributes nothing.

**On `after=N`**: `hello` → replayed rows ascending (`batch`, `ask`, `role`, `policy`, `rseq > max(N, H)`, ≤ 2 MiB, `hello.gap` otherwise) → `live` → live frames. The frontend applies replayed `ask`, `role` and `policy` rows in order.

**Registration before snapshot.** The relay adds the viewer to the broadcast set *before* reading the room state for `hello`, so a live frame MAY arrive anywhere after `hello`, and a row may arrive both by replay and live. The frontend MUST apply every frame by content (dedup by event id, order by `(epoch, seq, rseq)`; for rows, the `ask`/`role`/`policy` object from the frame with the highest `rseq` wins; `agent` frames apply in arrival order) whatever its arrival position; a batch posted between `hello` and `live` is seen exactly once. The relay MUST send `hello` as the first frame on every accepted connection (an over-cap connection receives only `{"t":"full"}`) and MUST write frames to a connection in emission order (the Python relay uses one per-connection queue; the Durable Object gets this from `ws.send` ordering).

**WebSocket keepalive**: the client MAY send the text `ping` every 30 s; the server answers `pong` (the Worker uses `setWebSocketAutoResponse` so this never wakes the object). Any other client message is ignored. **SSE** needs no client keepalive.

**Reconnect and transport fallback**: the client keeps `lastRseq` (the highest `rseq` seen, seeded from `hello.room.rseq`) and reconnects with `after=lastRseq`, minting a new ticket if the current one is older than half its `ttl` (300 s at the default); backoff 1 s doubling to 30 s. The frontend owns reconnection on both transports — on `EventSource`, an `error` with `readyState === CLOSED` mints a ticket and reopens with `after=lastRseq`; it never relies on the browser's automatic reconnect. Two failed opens before the first `hello` → the next transport advertised by `/healthz` (`ws` → `sse` → short-poll, not advertised, always exists).

**Polling mode**: bootstrap with `GET /r/{room}` (its `rseq` → `lastRseq`; `agents` and `asks` as in `hello.room`), then `GET /r/{room}/records` once (§3.17, standing in for the `records` frames), then per agent `GET /events?agent=<name>&before=<lastRseq+1>&limit=50`, paging with `before=<smallest rseq received>` until ≥ 80 positional events (exactly the kinds of §4.1) or `more:false`, then `GET /events?after=lastRseq` every 5 s, applying every row and advancing `lastRseq`. The pill reads `polling`. Snapshots reach a polling viewer only through `GET /r/{room}`, which the frontend re-fetches every 60 s in this mode; after the bootstrap, records reach a polling viewer only as they arrive in new `batch` rows.

## 6. Relay obligations (both backends)

1. **Room log**: one row per frame of kind `batch`, `ask`, `role`, `policy` (a batch carrying an accepted `answer` writes the `batch` row, then one `ask` row per newly answered ask, §3.8); `rseq` values are consecutive integers — each row's `rseq` is the previous row's + 1, starting at 1 — and are never reused after deletion; the Durable Object keeps its own counter in `meta` and does not rely on autoincrement. `agent` frames and `records` frames are not rows.
2. **`agent` overwrite**: on every event of a batch, from the token, before validation, storage and broadcast.
3. **Equality, not bytes**: for every accepted event, `parse(stored)` deep-equals `parse(sent)` after `sent.agent = <token name>`; string leaves byte-identical; key order irrelevant; no truncation, re-scrubbing or reordering within a batch — as stored and as returned by `GET /events`, replay and tails.
4. **Dedup**: whole batches by sha256 of the raw body bytes, computed before any overwrite, within retention; `record` events across the room, mandatory: the `records` map is keyed by `body.rid` and holds one event per rid (at most 2,000 rids, evicting the oldest `body.ts` first, compared as instants; a missing or unparsable `body.ts` sorts oldest; the cap is enforced synchronously on insert). An arriving record whose `id` equals the held one's → counted in `dup`, not stored. Same rid, different `id`, parsed `body.ts` not older than the held one → accepted, replaces the held event, stays in the batch row. Same rid, older `body.ts` → counted in `dup`, not stored. Other per-event dedup is optional; viewers do it.
5. **Snapshots**: `agent`-kind events are consumed, never stored as rows; `agents[name].snapshot` is the `body` of the newest accepted `agent` event; `last_seen` is updated on every accepted batch and inbox pull; an `agent` frame with the full agent object is emitted on registration, kick and every accepted snapshot.
6. **Broadcast before response**: a viewer is added to the broadcast set before the `hello` snapshot is read; `hello` is the first frame on every connection and frames are written to a connection in emission order; a frame is delivered to every connected viewer before the request that produced it is answered.
7. **Retention and the prune horizon**: byte bounds (1 MiB per agent, 8 MiB per room, `batch` rows only) on every insert — the agent bound deletes that agent's oldest `batch` rows, the room bound deletes the room's oldest `batch` rows of any agent (§3.8); `retention_min` (deleting rows of every kind older than it), ticket and ask expiry and the 7-day idle wipe on the periodic pass (the records cap is enforced on insert, §6.4, and needs no pass) (Worker alarm 15 min, Python relay 60 s) and synchronously on `POST /prune`; ask expiry applies only to asks still `open`, emits an `ask` frame with `state:"expired"`, and changes state only on that pass. Each room keeps `H`, the highest `rseq` of any row it has deleted (0 initially); byte eviction and age retention both raise it. Replay for `after=N` sends rows with `rseq > max(N, H)`; when `N < H`, `hello.gap = {"before_rseq": H + 1}`, else `null`; the 2 MiB replay budget applies after that (§5). Roles, asks, policy and snapshots live in their own tables and are unaffected by row deletion; `hello.room` is always current. All byte accounting (1 MiB per agent, 8 MiB per room, 256 KiB tails, 2 MiB replay) uses the UTF-8 length of the raw request body that produced the row.
8. **Credentials**: stored hashed (join code, tokens, tickets); compared in constant time where the platform allows; a rotated or invalidated credential answers `401` immediately.
9. **Rate limits** per §2; a rejected request has no side effect; the role cap counts every caller including the owner token and does not count no-ops.
10. **`/healthz`** MUST list only transports actually served on `/stream`.
11. **Policy rejection** per §4.4 on every batch, even from a client registered before the policy changed.
12. **Asks**: `answer` → `ask` frame with `state:"answered"` and `answer:{text, ts}`, as its own row after the batch row; an `answer` whose `body.ask` is missing, not a string or names no ask in the room, or from a token that is not the ask's `to` → `rejected[{why:"schema"}]`; an `answer` to an ask already `answered` or `expired` is stored, changes nothing and emits no frame; `ref` is not consulted; asks and roles addressed to a kicked or unknown name → `404 agent`.
13. **`/stream` gate**: the query `ticket` is checked when present, the `Authorization` header only when `ticket` is absent; no ticket and no room code, or an invalid one → `401 auth` before any frame and before the WebSocket upgrade.
14. **SSE resume** (Python relay): the `Last-Event-ID` request header is honoured as `after` and wins over the query parameter; a value that is not a non-negative integer is ignored.
15. **Route table** (Worker): only the public paths of §3 (the table in §3.16) are forwarded to the Durable Object; a path under `/r/`, `/rooms` or `/healthz` that is not exactly a listed route and method → `404 room` with hint `no such route`; `/__create` is answered `404 room` by the Worker before forwarding.
16. **Body handling**: `Content-Type` is not validated; the Python relay requires `Content-Length`; the Worker accepts chunked bodies too.
17. **Untrusted strings**: `viewer` is stored after control stripping and capping, never validated, never trusted; `ask` frames, `role` frames and inbox items carry it that way. The same stripping (§0) applies to `role`, ask `text` and room `name` before their caps are checked.
18. **Public URL** (Python relay): `relay` and `url` in `POST /rooms` come from `--public-url` when given, else `http://<Host header>`.

## 7. Client obligations

**Python client (tailer, `agentcolab/canvas.py`)**
- Send only events that passed `canvas.sanitise(event, level, repo_root)` at the effective level `min(config["canvas"]["stream"], project_config["canvas"]["max_stream"], room.policy.max_stream)`; the per-kind caps of §4.2 are applied there (`records.scrub_deep`, then `records.looks_like_secret` → `records.withhold_secrets` per string, after the cuts).
- Keep the file offset until the relay answers `202`; on `202` with `rejected`, treat rejected ids as sent and emit a `gap{reason:"policy"|"oversize"}` for each; on `413`, split the batch or replace the offending event by a `gap` and retry; on `429`/`503` record `Retry-After` (≤ 60 s) as a timestamp and skip flushes until then — never `sleep` on a hook path; on `401` three times, stop and mark the room gone; on `404`, same; every request `timeout ≤ 3` s from a daemon and ≤ the hook's budget from a hook; never raise.
- Per-session files under `store.home/canvas/`: `offsets-<sid>.json`, `pending-<sid>.ndjson`, `tail-<sid>.pid`, `daemon-failed-<sid>`, `stop-<sid>`. Record mirroring runs in every session daemon (the relay dedups by id, so two daemons are harmless); the `seen` map lives in that session's offsets file.
- Presence: snapshot cadence and `state` derivation as in §4.2; tasks mirrored per §4.2 (`board.tasks`, `board.open_tasks`).
- `ensure_tailer` is called from `colab status`, `colab sync`, `colab canvas join`, `colab canvas ensure` (the documented Codex `notify` one-liner; `nargs="*"`, extras ignored), and from `mcp.serve` when the harness is not `claude-code`. `colab install codex` prints the `notify` snippet. A Codex agent that never runs a `colab` command gets no card until it does.
- Hook budgets: `flush_if_orphaned` runs **before** `session.refresh_if_stale`; `refresh_if_stale` takes one shared deadline of 7 s and `pull_inbox` gets what is left, minimum 1 s; `SessionEnd` is < 300 ms and touches no network. A hook killed at its timeout is a lost flush, never a lost event — the offset advances only on ack.
- Inbox: `pull_inbox` stores `local["canvas"] = {"cursor": rseq, "role": {…}, "seen_at": iso}` and appends each ask to `local["chat_inbox"]` in exactly `chat.base.normalise_incoming` shape with `id` = the ask id (`ca-k7mq-4791`), `source="canvas"`, `channel="ask"`, `trust="chat"`, body `records.one_line(text, 500)`. Roles and asks enter a briefing only through `records.frame_untrusted` under `chat.UNTRUSTED_BANNER`.
- `cmd_answer` branches **before** `session.mirror`: `source == "canvas"` → `canvas.answer(store, target, text)` (spool an `answer` event whose `ref` and `body.ask` are the ask id, one `POST` with `timeout=3`), prints `answered on the canvas`, no chat mirror. `cmd_reply` reroutes to `cmd_answer` on the same condition.
- CLI: `canvas` is registered with `needs_store=False` and builds `Store()` inside the actions that need one, so `colab canvas new` and `serve` work unjoined; `serve --port 0 --port-file PATH` writes the bound port to `PATH` after bind. `colab canvas export` writes `{"canvas": {"relay": url, "room": code}}` into the project's public config; `--with-join-code` adds `join_code` and warns that anyone who can read the repository can then stream as any name. On `colab join`, a `join_code` in the project config auto-registers this agent; a `room` alone makes `colab status` print `canvas: run 'colab canvas join <join code>' to stream`.
- `canvas/cost.py` (stdlib only) prints the free-tier table from `(agents, hours, batches/min, viewers)`; `canvas/README.md` cites its output with the date it was computed.
- Docs that MUST change (D8), each edit with its content:
  - README.md:17 badge → `coordination-server--free`.
  - README.md:31-32, :371-373, AGENTS.md:8-9, docs/security.md:173-177 and docs/chat.md:28: every "no server" / "never posts" / "no arbitrary-post" sentence is narrowed to coordination or chat.
  - README.md:112-114 gains "at the default canvas level; at `full` what the agent read leaves, scrubbed".
  - README.md:146 heading "Humans watch, and interrupt, from Discord or Slack" → "Humans watch, and interrupt, from Discord, Slack, or the canvas".
  - docs/architecture.md:3-4 → "coordination has no server; the optional canvas mirror has a relay you can host yourself".
  - docs/security.md:204 (credentials row) gains the room code, the join code and the agent token, and where each lives: the room code in the browser's URL fragment and `localStorage` and, after `colab canvas export`, in the project's public config; the join code beside the bot tokens in the per-profile `config.json` (mode 600) and in the project config only with `--with-join-code`; the agent token in the per-profile `config.json` (mode 600), never the repo, never printed.
  - docs/security.md:207 (file-contents row) becomes "Never leave the machine unless the canvas mirror is on; then tool output leaves at `full` only, scrubbed, to the relay you chose — see docs/canvas.md".
  - FAILURE-MODES.md:60-64 (third-party model exposure) gains the mirror-image sentence "and every viewer of a canvas room — human or not — reads the agent's transcript at the level the room allows".
  - FAILURE-MODES.md:113-115 names the daemon and its 30-minute idle exit and carries "a hook killed at its timeout is a lost flush, never a lost event — the offset advances only on ack" (D12).
  - The design spec §8 list.

**Frontend (`canvas/web/index.html`)**
- State: `agents[name] = {meta, role, roleSeenSeq, lanes, snapshot}` with `lanes` keyed `session + "/" + lane`; one window per agent; one tab per `(session, lane)`, labelled by the session title if known (`session{title}` or snapshot `title`), else `session[:8]`; subagent lanes nested under their session's tab. `asks[id]`; `records[rid]`, replaced when a record for the same `rid` arrives with a `body.ts` not older; `lastRseq`.
- Dedup events by `id` within `(agent, session, lane)`; order by `(epoch, seq, rseq)`; apply every frame by content regardless of arrival position (§5); `hello.room` is authoritative at connect time (`lastRseq` seeded from `hello.room.rseq`), afterwards the `ask`/`role`/`policy` frame with the highest `rseq` and the last-arrived `agent` frame.
- Draw a role chip hollow from the `role` frame until an `agent` frame's `snapshot.role_seen_seq >= set_seq`, then solid.
- Show a dismissible banner when `hello.gap` is not `null`; draw an in-lane gap marker only for explicit `gap` events, never from `seq` arithmetic.
- Never insert foreign text as HTML: every string reaches the DOM through `textContent`; the markdown-lite renderer builds elements, never HTML strings; no `<script src>`; no external URL other than Google Fonts, with system fallbacks.
- Keep the room code only in the URL fragment and `localStorage`; send it only in `Authorization` for `GET /r/{room}`, `GET /records`, `POST /ticket`, `GET /events`, `POST /asks`, `PUT /roles`, and the ticket only as `?ticket=` on `/stream`.
- Reconnection, transport fallback and the polling bootstrap as in §5.
- `#demo` mode: entering `demo` as the room code synthesises three agents and a live stream locally; it is clearly labelled and generates no network request.

## 8. Conformance

`tests/test_canvas.py::RelayContract` runs against the in-thread Python relay and, with `CANVAS_RELAY=<url>`, against any backend. A backend that passes it is a canvas relay. It asserts:

- each numbered obligation in §6 and every status code under every route in §3, in the check order of §0;
- **equality, not bytes**: for every event of a posted batch, `parse(stored)` — from `GET /events`, from replay and from a fresh-connect tail — deep-equals `parse(sent)` after `sent.agent` is set to the token's name, every string leaf byte-identical, key order ignored, batch order kept, nothing truncated or re-scrubbed;
- `agent` overwritten from the token when the client sent another name; a room code cannot `POST /events` (`403`);
- `dup` on an identical body; per-event `dup` on a record already held, posted by a different agent; the records cap and its `body.ts` ordering; `GET /r/{room}/records` returns every held record in `records`-frame order, `[]` on an empty map, `403` to a join code or token;
- a batch of only `agent` events writes no row and emits an `agent` frame without `rseq` carrying `snapshot`; registration and kick emit the same frame;
- the exact frame order on a fresh connect and on `after=N` (§5); which frames carry `rseq`; no `{"t":"gap"}` frame ever; `hello` carries the position only at `hello.room.rseq` and `live` carries none; `hello.gap = {"before_rseq": H + 1}` after a prune with `N < H`, the first replayed `rseq` after a replay over 2 MiB, `null` when `N ≥ H`;
- a batch posted between `hello` and `live` is seen exactly once;
- an ask's `seq` equals its `open` frame's `rseq` and its id is `ca-<room4>-<rseq>`; its `answered` and `expired` frames are new rows with larger `rseq` keeping `id` and `seq`; a batch with an accepted `answer` writes the batch row, then one `ask` row per newly answered ask, and its `202` `rseq` is the batch row's; `GET /inbox?after=N` returns exactly the open asks with `seq > N`, ascending, with no per-pull cap, with the room's `rseq`; a role's `set_seq` equals its `role` frame's `rseq`; an unchanged role and clearing an already-null role emit nothing and consume no rate cap; the role cap applies to the owner token;
- per-event `policy` rejection at each ceiling (`args: {}` accepted and `args` with a key rejected at `summary`; a content-bearing key at the top level of `args` rejected at `tools`), `kind`, `oversize`, and `schema` for an `answer` from the wrong agent, to an unknown ask, or without a string `body.ask`; the per-event check order oversize → schema → kind → policy; blank lines skipped and a body of only blank lines → `202` with `accepted:0`; `413` for the batch; `429` with `Retry-After` and nothing consumed; a dup batch and an all-rejected batch each consume one bucket token;
- `/stream` with no ticket and no code → `401` before any frame; an expired ticket → `401`; ticket expiry, ask expiry (an `ask` frame with `state:"expired"`), `retention_min` and the 7-day wipe, each driven by its policy field plus `POST /prune`; byte bounds on insert;
- `full` on the 26th viewer and `gone` on delete, each followed by close; `Last-Event-ID` honoured by the Python relay and ignored when it is not a non-negative integer; `?ticket=` checked before the header;
- `GET /events` with `limit` outside 1–200 → `400`, with an unknown or kicked `agent` → empty `frames` and `more:false`; `DELETE /agents/{name}` on an already-kicked name → `204` and no frame; a kicked agent stays in `GET /r/{room}.agents` and `hello.room.agents` with `kicked:true` and its tail is still sent; re-registration replaces `harness`, `human`, `model`, `stream` and keeps `registered_at`; `PUT /policy` with none of the four fields → `400`, with unchanged values → a row and a frame; an absent body on `POST /ticket` and `POST /prune` is `{}`; `POST /rooms` without `name` → `"room"`; control characters stripped from `viewer`, `role`, ask `text` and room `name` before the caps; `rseq` values consecutive from 1.

Not asserted, honestly: `503 busy` (Worker only, under Durable Object overload); the `hello`/`live` race under load; two daemons on one profile; time-driven obligations on the Worker beyond the opt-in probe (`tests/canvas_live.py`); `EventSource` reconnection in a real browser; secret-scrubber false positives measured on tool output; that hibernation saves duration; daemon spawn on Windows under a real harness; the frontend beyond self-containment; Codex `notify`.

## 9. Change log

### v1.1 → v1.2

- F1–F6 (lead, after the second check) — records keyed by `body.rid` with the id/ts rules of §6.4; room byte bound deletes any agent's oldest `batch` rows; records cap enforced on insert, not on the pass; the 26th viewer gets `full` as its only frame; only `open` asks expire; `POST /rooms` absent body is `{}` and inbox `after` absent is `0`.

One line per round-2 decision applied, tagged with its id (E = decision stated in full, C = contradiction, M = ambiguity).

- E1 — §7's docs bullet enumerates every D8 edit with its content: security.md credentials row and file-contents row, the README heading, the two FAILURE-MODES passages.
- E2 — Positional is unconditional: `text`, `thinking`, `tool_call`, `tool_result`, `prompt`, `session`, `gap`; tails and the polling bootstrap count exactly those; the transcript-vs-daemon distinction survives only as a client note on `seq`.
- E3 — (v1 correction) §4.4 at `summary`: `tool_call.body` may carry `name`, `paths`, `omitted`; `args` with one or more keys is rejected, `args: {}` is accepted.
- E4 — Control characters (below 0x20 including tab and newline, plus 0x7F–0x9F) stripped from `viewer`, `role`, ask `text` and room `name` before the caps and before storage; §6.17 restated.
- E5 — "A hook killed at its timeout is a lost flush, never a lost event — the offset advances only on ack" in §7 and in the FAILURE-MODES edit.
- C1 — `ask` frames: the `open` form's `ask.seq` and id equal its `rseq`; `answered` and `expired` are new rows with larger `rseq` keeping the original `id` and `seq`.
- C2 — Inbox has no per-pull cap; the 40-open-asks room bound is the bound; the response `rseq` is the room position.
- C3 — A batch with an accepted `answer` writes the `batch` row first, then one `ask` row per newly answered ask in ask-`seq` order; the `202` `rseq` is the batch row's; §6.1 reads "one row per frame of kind `batch`, `ask`, `role`, `policy`".
- C4 — Prune horizon `H`: replay sends `rseq > max(N, H)`; `hello.gap.before_rseq = H + 1` when `N < H`, else `null`; the 2 MiB budget applies after that; age retention deletes rows of every kind; roles, asks, policy and snapshots are unaffected by row deletion; `hello.room` is always current.
- C5 — Same as E4.
- M1 — `answer`: missing, non-string or unknown `body.ask` → `schema`; wrong `to` → `schema`; an `answer` to an `answered`/`expired` ask is stored, changes nothing, emits no frame; `ref` is not consulted.
- M2 — `summary` prompt cap: 200 code points and ≤ 800 bytes.
- M3 — Same as E3.
- M4 — Content-bearing keys are checked as top-level keys of `args` only.
- M5 — Relay schema validation listed exhaustively in §4.1; unknown envelope fields stored unchanged.
- M6 — Per-event check order on the raw line: oversize → schema → kind → policy; first failure reported.
- M7 — Blank lines skipped; trailing `\n` optional; the 200-line cap and `dup:<n>` count non-blank lines; no non-blank line → `202` with zeros; a non-JSON line → `schema` with `"id":null`.
- M8 — Check order `404 room` → `403 forbidden` → `401 auth` → `413 oversize` → `400 schema` → `404 agent` → `429 rate` → `503 full`.
- M9 — No `Authorization`, a non-Bearer scheme, or a value matching no credential shape → `401 auth`.
- M10 — `GET /events`: `limit` outside 1–200 or non-integer → `400`; unknown or kicked `agent` → empty `frames`, `more:false`; `more` counts only matching rows.
- M11 — Register body: `harness`, `human`, `model` optional (`null`), ≤ 120 code points / 480 bytes, control-stripped; `stream` default `tools`; re-registration replaces all four and keeps `registered_at`; `kicked` cleared.
- M12 — `DELETE /agents/{name}` on an already-kicked name → `204`, no frame.
- M13 — Roles: `null` or `""` clears; clearing a null role → `200` with the last `set_seq` (or 0), no frame, no cap; the cap is checked after control stripping.
- M14 — Policy: a body with none of the four fields → `400`; a row and a frame even when nothing changes.
- M15 — `POST /ticket` and `POST /prune`: an absent or empty body is `{}`.
- M16 — Ask expiry changes state only on the periodic pass or `prune`; `open` everywhere until then.
- M17 — `rseq` values are consecutive integers from 1; the Durable Object keeps its own counter in `meta`.
- M18 — All byte accounting uses the UTF-8 length of the raw request body that produced the row.
- M19 — For rows the highest-`rseq` frame wins; `hello` is the first frame on every connection; frames are written in emission order; `agent` frames apply in arrival order.
- M20 — §6.5: `agents[name].snapshot` is the `body` of the newest accepted `agent` event.
- M21 — Kicked agents stay in `hello.room.agents` and `GET /r/{room}` with `kicked:true` until the 7-day wipe; their tails are included on fresh connect.
- M22 — New route `GET /r/{room}/records` (§3.17), in the Worker route table (§3.16), in the polling bootstrap (§5) and in conformance (§8).
- M23 — Records eviction compares `body.ts` parsed to an instant; missing or unparsable sorts oldest; a held record is replaced only by one whose `body.ts` is not older.
- M24 — `503 busy`: Worker only, under Durable Object overload; never the Python relay; not in the conformance suite.
- M25 — A path under `/r/`, `/rooms` or `/healthz` that is not exactly a §3 route and method → `404 room` with hint `no such route`; everything else → `index.html`; the Worker route table is written out in §3.16.
- M26 — `/stream`: `?ticket=` checked when present, the header only when it is absent; a non-integer `Last-Event-ID` is ignored.
- M27 — `asks` arrays ascend by `seq` and hold every open ask plus the 40 answered/expired with the largest `seq`; `agents` arrays ascend by `name`.
- M28 — `POST /rooms` `name` absent or empty → `"room"`.
- M29 — §4.1's "they never affect gap detection" deleted; the frontend draws a gap marker only for explicit `gap` events.
- M30 — A dup batch and an all-rejected batch each consume one rate-bucket token and update `last_seen`.
- M31 — `hello` carries the position only at `hello.room.rseq`; the frontend seeds `lastRseq` from it; `live` carries no `rseq`.

### v1 → v1.1

One line per decision applied, then the v1/spec disagreements no decision covered.

- D1 — Ask `seq` is the `rseq` of its `ask` frame; ids `ca-<room4>-<rseq>` (`ca-k7mq-4814`); inbox cursor restated; `set_seq` is the `role` frame's `rseq`.
- D2 — Ordering and dedup within `(agent, session, lane)`; frontend lanes keyed `session + "/" + lane`, one tab per `(session, lane)`.
- D3 — `agent` events consumed into `snapshot` and re-broadcast as an `agent` frame without `rseq`; `records` map with mandatory dedup and a `records` frame on fresh connect; tails count positional kinds, whole batches, soft cap; retention bytes count `batch` rows only; rseq/no-rseq split of frames; `{"t":"gap"}` removed; snapshot cadence.
- D4 — Storage obligation and §8 reworded from byte-for-byte to deep equality after `agent` overwrite.
- D5 — `ticket_ttl_s` and `ask_ttl_s` in policy; `POST /r/{room}/prune`; byte bounds on insert, time bounds on the periodic pass (15 min / 60 s) and on `prune`; ticket refresh at half of `ttl`.
- D6 — Viewer registered before the snapshot; frames applied by content; exactly-once conformance.
- D7 — `ensure_tailer` callers enumerated; `colab install codex` prints the `notify` snippet.
- D8 — The docs that must change are listed in §7.
- D9 — Snapshot `role` is the canvas role; the heartbeat self-description is `self_role`.
- D10 — Per-session files; record mirroring in every session daemon.
- D11 — Tasks from `board.tasks(store)` with `_blocked_by` from `board.open_tasks(store)`.
- D12 — Hook budgets and the shared 7 s deadline; `SessionEnd` < 300 ms.
- D13 — `cmd_answer` branches before `session.mirror`; `cmd_reply` reroutes.
- D14 — `agent.state` derivation stated under the `agent` kind.
- D15 — `Last-Event-ID` honoured; the frontend owns `EventSource` reconnection.
- D16 — Polling bootstrap; `more` defined; neither/both of `after`/`before` → `400`; `agent=` filters only `batch` rows.
- D17 — `canvas` command `needs_store=False`; `new`/`serve` unjoined; `ensure` takes `nargs="*"`.
- D18 — `--port-file`; `/stream` without credential → `401` before any frame, in conformance.
- D19 — `canvas/cost.py` and its dated citation.
- D20 — "Not asserted" list extended.
- A1 — All-rejected batch: no row, `rseq` current.
- A2 — Batch digest over raw body bytes before overwrite.
- A3 — Tails are whole batches, at least the newest.
- A4 — Registration before snapshot.
- A5 — Retention timing.
- A6 — `hello.gap` semantics; no other gap signal.
- A7 — Kicked names do not count toward 12 and re-register; `404 agent` for kicked/unknown targets; `stream` default `tools`, `off` illegal.
- A8 — One ticket, any number of connections within its TTL.
- A9 — `answer` from the wrong agent → `schema`.
- A10 — Role cap for every caller; `viewer` required with room-code auth; the owner token forces `"owner"`.
- A11 — Code-point caps with 4× byte ceilings.
- A12 — `Content-Type` not validated; the Python relay requires `Content-Length`.
- A13 — `relay`/`url` from `--public-url` else `Host`.
- A14 — `agent` frames carry the full object.
- A15 — Records dedup mandatory.
- A16 — Worker route table; `/__create` unreachable.
- A17 — Room `name` ≤ 60 code points, default `"room"`.
- A18 — `viewer` carried as typed.
- P1 — `colab canvas export` shape, `--with-join-code`, join-time auto-register, `colab status` hint.
- P2 — `#demo` mode.

No decision covered these; v1 kept and closed here so four implementations agree:
- Spec §3.2's `403 policy` for an over-ceiling `stream` does not exist; policy is per event, `why:"policy"`.
- Spec §2 lists `agent` as a log row and spec §4 uses `ca-<seq>` ids; D3 and D1 win.
- v1 `hello`/`live` carried a top-level `rseq`; D3 lists both as frames without one, so the position travels as `hello.room.rseq`.
- A non-JSON NDJSON line → `rejected[{"id":null,"why":"schema"}]`.
- A batch that leaves nothing to store writes no row.
- Check order `404` → `403` → `401` → `400`/`413` → `429`/`503` fixed in §0.
- `hello.backfill:"none"` = `after=N` with nothing to replay.
- A role no-op consumes no rate cap; TTL changes leave existing tickets and asks alone.
- `DELETE /agents/{name}` on a never-registered name → `404 agent`.
- `agents[].stream` is the requested value; the level in force is `min(stream, policy.max_stream)`.
- `/healthz.version` stays `"1"`.
- Polling mode sees snapshots only through the 60 s `GET /r/{room}` refresh; recorded, not extended.
