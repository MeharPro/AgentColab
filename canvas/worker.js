// AgentColab Canvas relay: one Worker in front of one Durable Object per room.
//
// The Worker only routes: it mints room and join codes (the only place the
// plaintext join code exists), rate-limits creation per IP, refuses every path
// that is not in the contract's route table, and hands the rest to the room's
// object. The object holds the room in SQLite and serves viewers over
// hibernating WebSockets, so an idle room costs nothing while tabs stay open.
// docs/canvas-contract.md v1.2 is binding; section numbers below refer to it.
// Deliberately no imports beyond the runtime and no build step: the file that
// is deployed is the file in the repository.

import { DurableObject } from "cloudflare:workers";

const VERSION = "1";
const ALPHABET = "23456789abcdefghjkmnpqrstvwxyz";
const SYM = "[23456789abcdefghjkmnpqrstvwxyz]";
const RE_ROOM = new RegExp(`^${SYM}{4}-${SYM}{4}-${SYM}{2}$`);
const RE_JOIN = new RegExp(`^${SYM}{4}-${SYM}{4}-${SYM}{2}\\.${SYM}{24}$`);
const RE_TOKEN = new RegExp(`^at-${SYM}{32}$`);
const RE_TICKET = new RegExp(`^vt-${SYM}{32}$`);
const RE_NAME = /^[a-z0-9][a-z0-9-]{0,47}$/;
const RE_UINT = /^\d+$/;

const BATCH_BYTES = 65536;
const BATCH_LINES = 200;
const EVENT_BYTES = 40960;
const AGENT_BYTES = 1024 * 1024;
const ROOM_BYTES = 8 * 1024 * 1024;
const TAIL_BYTES = 256 * 1024;
const TAIL_EVENTS = 80;
const REPLAY_BYTES = 2 * 1024 * 1024;
const MAX_AGENTS = 12;
const MAX_VIEWERS = 25;
const MAX_OPEN_ASKS = 40;
const MAX_RECORDS = 2000;
const RECORDS_PER_FRAME = 200;
const BUCKET_RATE = 2;
const BUCKET_BURST = 10;
const ASK_GAP_MS = 5000;
const ROLE_GAP_MS = 30000;
const ALARM_MS = 15 * 60 * 1000;
const IDLE_WIPE_MS = 7 * 24 * 60 * 60 * 1000;
const JSON_BODY_BYTES = 65536;

const STREAMS = ["summary", "tools", "full"];
const KINDS = new Set(["agent", "text", "thinking", "tool_call", "tool_result",
  "prompt", "session", "record", "answer", "gap"]);
const POSITIONAL = new Set(["text", "thinking", "tool_call", "tool_result",
  "prompt", "session", "gap"]);
const CONTENT_KEYS = ["content", "new_string", "old_string", "edits", "cells",
  "patch", "new_source"];
const POLICY_RANGES = {
  retention_min: [5, 720], ticket_ttl_s: [1, 3600], ask_ttl_s: [1, 604800],
};
const DEFAULT_POLICY = {
  max_stream: "tools", retention_min: 120, ticket_ttl_s: 600, ask_ttl_s: 86400,
};

const ENC = new TextEncoder();
const DEC = new TextDecoder("utf-8");

// ---------------------------------------------------------------- helpers

function json(status, obj, headers) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", ...(headers || {}) },
  });
}

function err(status, code, hint, headers) {
  return json(status, { error: code, hint }, headers);
}

function noContent() {
  return new Response(null, { status: 204 });
}

function isObj(v) {
  return v !== null && typeof v === "object" && !Array.isArray(v);
}

function hasOwn(o, k) {
  return Object.prototype.hasOwnProperty.call(o, k);
}

function nowIso() {
  return new Date().toISOString();
}

function cpLen(s) {
  let n = 0;
  for (const _ of s) n += 1;
  return n;
}

function byteLen(s) {
  return ENC.encode(s).length;
}

// Single-line human fields: tab and newline go too, plus C1 controls (§0).
function stripControls(s) {
  return s.replace(/[\u0000-\u001f\u007f-\u009f]/g, "");
}

// Returns the cleaned string, or null when it is not a string or breaks the
// code-point cap (the byte cap is 4x, §2).
function humanField(v, cap) {
  if (typeof v !== "string") return null;
  const s = stripControls(v);
  if (cpLen(s) > cap || byteLen(s) > cap * 4) return null;
  return s;
}

async function sha256hex(data) {
  const bytes = typeof data === "string" ? ENC.encode(data) : data;
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  let out = "";
  for (const b of new Uint8Array(digest)) out += b.toString(16).padStart(2, "0");
  return out;
}

// Both sides are hex digests of the same length, so a leak here would be of a
// hash, not a credential; still, the platform offers a constant-time compare.
function safeEqual(a, b) {
  const x = ENC.encode(a);
  const y = ENC.encode(b);
  if (x.length !== y.length) return false;
  if (typeof crypto.subtle.timingSafeEqual === "function") {
    return crypto.subtle.timingSafeEqual(x, y);
  }
  let r = 0;
  for (let i = 0; i < x.length; i += 1) r |= x[i] ^ y[i];
  return r === 0;
}

// 30 symbols do not divide 256, so bytes are rejected above the largest
// multiple rather than reduced modulo 30, which would bias the low symbols.
function randomSymbols(n) {
  let out = "";
  while (out.length < n) {
    const buf = crypto.getRandomValues(new Uint8Array(n * 2));
    for (const b of buf) {
      if (b < 240 && out.length < n) out += ALPHABET[b % 30];
    }
  }
  return out;
}

function newRoomCode() {
  const s = randomSymbols(10);
  return `${s.slice(0, 4)}-${s.slice(4, 8)}-${s.slice(8, 10)}`;
}

function classifyBearer(header) {
  if (!header) return null;
  const m = /^Bearer\s+(.+?)\s*$/i.exec(header);
  if (!m) return null;
  const v = m[1];
  if (RE_ROOM.test(v)) return { cls: "room", value: v };
  if (RE_JOIN.test(v)) return { cls: "join", value: v };
  if (RE_TOKEN.test(v)) return { cls: "token", value: v };
  if (RE_TICKET.test(v)) return { cls: "ticket", value: v };
  return null;
}

// Validates a policy object; `partial` allows a subset (PUT /policy).
// Returns the merged policy or a string naming the offending field.
function mergePolicy(base, patch, partial) {
  if (!isObj(patch)) return "policy";
  const out = { ...base };
  let touched = 0;
  if (hasOwn(patch, "max_stream")) {
    if (!STREAMS.includes(patch.max_stream)) return "max_stream";
    out.max_stream = patch.max_stream;
    touched += 1;
  }
  for (const k of Object.keys(POLICY_RANGES)) {
    if (!hasOwn(patch, k)) continue;
    const [lo, hi] = POLICY_RANGES[k];
    if (!Number.isInteger(patch[k]) || patch[k] < lo || patch[k] > hi) return k;
    out[k] = patch[k];
    touched += 1;
  }
  if (partial && touched === 0) return "policy";
  return out;
}

// Room name: absent, null or empty means "room"; anything else is a string
// under the human caps (§3.1).
function roomName(v) {
  if (v === undefined || v === null || v === "") return "room";
  const s = humanField(v, 60);
  if (s === null) return null;
  return s === "" ? "room" : s;
}

// The contract's route table (§3.16), matched exactly on method and path.
const ROUTES = [
  ["POST", /^\/rooms$/, "rooms"],
  ["GET", /^\/healthz$/, "healthz"],
  ["GET", /^\/r\/([^/]+)$/, "snapshot"],
  ["DELETE", /^\/r\/([^/]+)$/, "delete_room"],
  ["POST", /^\/r\/([^/]+)\/ticket$/, "ticket"],
  ["GET", /^\/r\/([^/]+)\/stream$/, "stream"],
  ["GET", /^\/r\/([^/]+)\/events$/, "get_events"],
  ["POST", /^\/r\/([^/]+)\/events$/, "post_events"],
  ["POST", /^\/r\/([^/]+)\/agents\/([^/]+)$/, "register"],
  ["DELETE", /^\/r\/([^/]+)\/agents\/([^/]+)$/, "kick"],
  ["GET", /^\/r\/([^/]+)\/inbox$/, "inbox"],
  ["POST", /^\/r\/([^/]+)\/asks$/, "ask"],
  ["PUT", /^\/r\/([^/]+)\/roles\/([^/]+)$/, "role"],
  ["PUT", /^\/r\/([^/]+)\/policy$/, "policy"],
  ["POST", /^\/r\/([^/]+)\/prune$/, "prune"],
  ["GET", /^\/r\/([^/]+)\/records$/, "records"],
];

function matchRoute(method, path) {
  for (const [m, re, name] of ROUTES) {
    const hit = re.exec(path);
    if (hit && m === method) return { name, room: hit[1] || null, arg: hit[2] || null };
  }
  return null;
}

function noSuchRoute() {
  return err(404, "room", "no such route");
}

function healthz() {
  return json(200, { ok: true, backend: "worker", transports: ["ws"], version: VERSION });
}

async function readJsonBody(request) {
  const buf = new Uint8Array(await request.arrayBuffer());
  if (buf.length > JSON_BODY_BYTES) {
    return { error: err(413, "oversize", "keep JSON request bodies under 64 KiB") };
  }
  const text = DEC.decode(buf);
  if (text.trim() === "") return { body: {}, bytes: buf.length };
  try {
    const body = JSON.parse(text);
    if (!isObj(body)) return { error: err(400, "schema", "send a JSON object as the request body") };
    return { body, bytes: buf.length };
  } catch (e) {
    return { error: err(400, "schema", "the request body is not valid JSON") };
  }
}

// ---------------------------------------------------------------- Worker

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const path = url.pathname;
    // A16: the internal creation path is answered here so it can never reach
    // an object from outside, whatever the assets layer does with it.
    if (path === "/__create" || path.startsWith("/__create/")) return noSuchRoute();
    if (path === "/healthz" || path === "/rooms" || path.startsWith("/r/")) {
      const m = matchRoute(request.method, path);
      if (!m) return noSuchRoute();
      if (m.name === "healthz") return healthz();
      if (m.name === "rooms") return createRoom(request, env, url);
      if (!RE_ROOM.test(m.room)) {
        return err(404, "room", "room codes look like k7mq-p3xw-4h; check the code you were given");
      }
      return forward(env, m.room, request);
    }
    if (env.ASSETS) return env.ASSETS.fetch(request);
    return noSuchRoute();
  },
};

async function forward(env, room, request) {
  const stub = env.ROOM.get(env.ROOM.idFromName(room));
  try {
    // The body is buffered here so an object that rejects before reading it
    // (404, 403, 401) never leaves a half-pumped stream behind; bodies are
    // small by contract and the object enforces the 64 KiB caps itself.
    let forwarded = request;
    if (request.method === "POST" || request.method === "PUT") {
      forwarded = new Request(request, { body: await request.arrayBuffer() });
    }
    return await stub.fetch(forwarded);
  } catch (e) {
    // The runtime flags overload on the error itself; that is the one case the
    // contract lets the Worker answer with `busy` (§0).
    if (e && (e.overloaded || /overload/i.test(String(e.message || "")))) {
      return err(503, "busy", "the room is overloaded; retry after the Retry-After seconds",
        { "Retry-After": "2" });
    }
    return err(500, "internal", "the relay hit an unexpected error; retry, then report it with the room code");
  }
}

async function createRoom(request, env, url) {
  const read = await readJsonBody(request);
  if (read.error) return read.error;
  const name = roomName(read.body.name);
  if (name === null) return err(400, "schema", "name must be a string of at most 60 characters");
  const policy = mergePolicy(DEFAULT_POLICY, read.body.policy === undefined ? {} : read.body.policy, false);
  if (typeof policy === "string") {
    return err(400, "schema", `policy.${policy} is out of range or of the wrong type; see the contract's POST /rooms`);
  }
  // Schema before rate so a malformed request does not spend the caller's quota.
  const ip = request.headers.get("CF-Connecting-IP") || "unknown";
  if (env.CREATE && typeof env.CREATE.limit === "function") {
    const { success } = await env.CREATE.limit({ key: ip });
    if (!success) {
      return err(429, "rate", "at most 5 rooms per minute per address; wait a minute or reuse a room",
        { "Retry-After": "60" });
    }
  }
  for (let attempt = 0; attempt < 3; attempt += 1) {
    const room = newRoomCode();
    const joinCode = `${room}.${randomSymbols(24)}`;
    const joinHash = await sha256hex(joinCode);
    const stub = env.ROOM.get(env.ROOM.idFromName(room));
    const init = new Request("https://canvas.internal/__create", {
      method: "POST",
      body: JSON.stringify({ room, name, policy, join_hash: joinHash, created_at: nowIso() }),
    });
    const res = await stub.fetch(init);
    if (res.status === 409) continue;   // the code is in use: astronomically rare, but cheap to handle
    if (res.status !== 201) return res;
    const relay = `${url.protocol}//${url.host}`;
    return json(201, { room, join_code: joinCode, policy, relay, url: `${relay}/#${room}` });
  }
  return err(500, "internal", "could not allocate an unused room code; retry");
}

// ---------------------------------------------------------------- Room

const SCHEMA = `
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS agents (
  name TEXT PRIMARY KEY, token_hash TEXT, harness TEXT, human TEXT, model TEXT,
  stream TEXT NOT NULL, snapshot TEXT, registered_at TEXT NOT NULL, last_seen TEXT NOT NULL,
  kicked INTEGER NOT NULL DEFAULT 0, bucket REAL NOT NULL DEFAULT 10, bucket_at INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS log (
  rseq INTEGER PRIMARY KEY, t TEXT NOT NULL, agent TEXT, ts TEXT NOT NULL,
  bytes INTEGER NOT NULL, digest TEXT, body TEXT NOT NULL, npos INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS log_agent ON log (t, agent, rseq);
CREATE INDEX IF NOT EXISTS log_digest ON log (digest);
CREATE TABLE IF NOT EXISTS records (
  id TEXT PRIMARY KEY, rid TEXT, ts TEXT, ts_ms INTEGER NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS roles (
  agent TEXT PRIMARY KEY, role TEXT, viewer TEXT, set_seq INTEGER NOT NULL, ts TEXT, changed_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS asks (
  seq INTEGER PRIMARY KEY, id TEXT NOT NULL, to_agent TEXT NOT NULL, viewer TEXT NOT NULL,
  text TEXT NOT NULL, ts TEXT NOT NULL, expires_ms INTEGER NOT NULL, state TEXT NOT NULL, answer TEXT);
CREATE TABLE IF NOT EXISTS tickets (hash TEXT PRIMARY KEY, expires INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS viewer_rate (viewer TEXT PRIMARY KEY, at INTEGER NOT NULL);
`;

export class Room extends DurableObject {
  constructor(ctx, env) {
    super(ctx, env);
    this.sql = ctx.storage.sql;
    this.sql.exec(SCHEMA);
    // Keepalives are answered by the runtime so a tab's ping never wakes the
    // object; that is what makes an idle room free (§5).
    ctx.setWebSocketAutoResponse(new WebSocketRequestResponsePair("ping", "pong"));
  }

  // -- meta ---------------------------------------------------------------

  loadMeta() {
    const rows = this.sql.exec("SELECT k, v FROM meta").toArray();
    if (rows.length === 0) return null;
    const m = {};
    for (const r of rows) m[r.k] = r.v;
    if (!m.room) return null;
    return {
      room: m.room,
      room4: m.room.slice(0, 4),
      name: m.name,
      policy: JSON.parse(m.policy),
      join_hash: m.join_hash,
      rseq: Number(m.rseq),
      horizon: Number(m.horizon),
      created_at: m.created_at,
      last_batch_at: m.last_batch_at || null,
    };
  }

  setMeta(k, v) {
    this.sql.exec("INSERT INTO meta (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v", k, String(v));
  }

  // The counter lives in meta rather than in autoincrement so a deleted row
  // can never hand its number to a later one (§6.1).
  nextRseq(meta) {
    meta.rseq += 1;
    this.setMeta("rseq", meta.rseq);
    return meta.rseq;
  }

  raiseHorizon(meta, rseq) {
    if (rseq > meta.horizon) {
      meta.horizon = rseq;
      this.setMeta("horizon", rseq);
    }
  }

  // -- dispatch -----------------------------------------------------------

  async fetch(request) {
    try {
      return await this.route(request);
    } catch (e) {
      return err(500, "internal", `the room hit an unexpected error (${String(e && e.message || e).slice(0, 120)}); retry, then report it`);
    }
  }

  async route(request) {
    const url = new URL(request.url);
    const path = url.pathname;
    if (path === "/__create") return this.create(request);
    const m = matchRoute(request.method, path);
    if (!m) return noSuchRoute();
    if (m.name === "healthz") return healthz();
    if (m.name === "rooms") return noSuchRoute();
    const meta = this.loadMeta();
    if (!meta || meta.room !== m.room) {
      return err(404, "room", "no such room; create one with POST /rooms or check the code");
    }
    switch (m.name) {
      case "snapshot": return this.snapshot(request, meta);
      case "records": return this.records(request, meta);
      case "ticket": return this.ticket(request, meta);
      case "stream": return this.stream(request, url, meta);
      case "get_events": return this.getEvents(request, url, meta);
      case "register": return this.register(request, meta, m.arg);
      case "kick": return this.kick(request, meta, m.arg);
      case "post_events": return this.postEvents(request, meta);
      case "inbox": return this.inbox(request, url, meta);
      case "ask": return this.ask(request, meta);
      case "role": return this.role(request, meta, m.arg);
      case "policy": return this.policy(request, meta);
      case "delete_room": return this.deleteRoom(request, meta);
      case "prune": return this.prune(request, meta);
      default: return noSuchRoute();
    }
  }

  async create(request) {
    const body = await request.json();
    if (this.loadMeta()) return json(409, { error: "exists" });
    this.ctx.storage.transactionSync(() => {
      this.setMeta("room", body.room);
      this.setMeta("name", body.name);
      this.setMeta("policy", JSON.stringify(body.policy));
      this.setMeta("join_hash", body.join_hash);
      this.setMeta("rseq", 0);
      this.setMeta("horizon", 0);
      this.setMeta("created_at", body.created_at);
    });
    await this.ctx.storage.setAlarm(Date.now() + ALARM_MS);
    return json(201, { room: body.room });
  }

  // -- auth ---------------------------------------------------------------

  // Resolves the bearer credential against the classes a route accepts, in the
  // contract's order: wrong class 403 before unknown value 401 (§0).
  async authorize(request, meta, allowed) {
    const cred = classifyBearer(request.headers.get("Authorization"));
    if (!cred) {
      return { error: err(401, "auth", "send Authorization: Bearer <room code, join code or agent token>") };
    }
    if (!allowed.includes(cred.cls)) {
      return { error: err(403, "forbidden", `this route does not accept a ${cred.cls === "token" ? "agent token" : cred.cls + " code"}; see the contract's route table`) };
    }
    if (cred.cls === "room") {
      if (!safeEqual(cred.value, meta.room)) return { error: err(401, "auth", "that room code does not open this room") };
      return { cls: "room" };
    }
    if (cred.cls === "join") {
      const h = await sha256hex(cred.value);
      if (!safeEqual(h, meta.join_hash)) return { error: err(401, "auth", "unknown join code; ask the room's creator for the current one") };
      return { cls: "join" };
    }
    if (cred.cls === "token") {
      const h = await sha256hex(cred.value);
      // A lookup by hash of a 160-bit random value; timing reveals nothing usable.
      const rows = this.sql.exec("SELECT name FROM agents WHERE token_hash = ? AND kicked = 0", h).toArray();
      if (rows.length === 0) return { error: err(401, "auth", "unknown, rotated or kicked token; re-register with the join code") };
      return { cls: "token", agent: rows[0].name };
    }
    return { error: err(403, "forbidden", "a viewer ticket only opens /stream as ?ticket=") };
  }

  // -- shapes -------------------------------------------------------------

  agentObject(row) {
    const role = this.roleObject(row.name);
    return {
      name: row.name,
      harness: row.harness,
      human: row.human,
      model: row.model,
      stream: row.stream,
      registered_at: row.registered_at,
      last_seen: row.last_seen,
      kicked: row.kicked === 1,
      role,
      snapshot: row.snapshot === null ? null : JSON.parse(row.snapshot),
    };
  }

  agentByName(name) {
    const rows = this.sql.exec("SELECT * FROM agents WHERE name = ?", name).toArray();
    return rows.length ? rows[0] : null;
  }

  roleObject(agent) {
    const rows = this.sql.exec("SELECT role, viewer, set_seq, ts FROM roles WHERE agent = ?", agent).toArray();
    if (rows.length === 0 || rows[0].role === null) return null;
    return { role: rows[0].role, viewer: rows[0].viewer, set_seq: rows[0].set_seq, ts: rows[0].ts };
  }

  askObject(row) {
    return {
      id: row.id, seq: row.seq, to: row.to_agent, viewer: row.viewer, text: row.text,
      ts: row.ts, state: row.state, answer: row.answer === null ? null : JSON.parse(row.answer),
    };
  }

  roomObject(meta) {
    const agents = this.sql.exec("SELECT * FROM agents ORDER BY name").toArray().map((r) => this.agentObject(r));
    const open = this.sql.exec("SELECT * FROM asks WHERE state = 'open'").toArray();
    const closed = this.sql.exec("SELECT * FROM asks WHERE state != 'open' ORDER BY seq DESC LIMIT ?", MAX_OPEN_ASKS).toArray();
    const asks = open.concat(closed).sort((a, b) => a.seq - b.seq).map((r) => this.askObject(r));
    return { room: meta.room, name: meta.name, rseq: meta.rseq, policy: meta.policy, agents, asks };
  }

  recordEvents() {
    return this.sql.exec("SELECT body FROM records ORDER BY ts_ms, rowid").toArray().map((r) => JSON.parse(r.body));
  }

  // -- broadcast ----------------------------------------------------------

  broadcast(frames) {
    if (frames.length === 0) return;
    for (const ws of this.ctx.getWebSockets("viewer")) {
      for (const f of frames) {
        try { ws.send(f); } catch (e) { /* a closing socket; the runtime drops it */ }
      }
    }
  }

  emitAgent(name) {
    const row = this.agentByName(name);
    return JSON.stringify({ t: "agent", agent: this.agentObject(row) });
  }

  // Writes one log row and returns its frame text. `bytes` is the raw request
  // body that produced it, the unit of every byte budget (§6.7).
  appendRow(meta, t, agent, frameBody, bytes, npos) {
    const rseq = this.nextRseq(meta);
    const frame = JSON.stringify({ t, rseq, ...frameBody });
    this.sql.exec(
      "INSERT INTO log (rseq, t, agent, ts, bytes, digest, body, npos) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
      rseq, t, agent, nowIso(), bytes, null, frame, npos || 0,
    );
    return { rseq, frame };
  }

  // -- routes: read -------------------------------------------------------

  async snapshot(request, meta) {
    const a = await this.authorize(request, meta, ["room"]);
    if (a.error) return a.error;
    return json(200, this.roomObject(this.loadMeta()));
  }

  async records(request, meta) {
    const a = await this.authorize(request, meta, ["room"]);
    if (a.error) return a.error;
    return json(200, { rseq: this.loadMeta().rseq, events: this.recordEvents() });
  }

  async ticket(request, meta) {
    const a = await this.authorize(request, meta, ["room"]);
    if (a.error) return a.error;
    const read = await readJsonBody(request);
    if (read.error) return read.error;
    const ticket = `vt-${randomSymbols(32)}`;
    const hash = await sha256hex(ticket);
    const ttl = this.loadMeta().policy.ticket_ttl_s;
    this.sql.exec("INSERT INTO tickets (hash, expires) VALUES (?, ?)", hash, Date.now() + ttl * 1000);
    return json(201, { ticket, ttl });
  }

  async getEvents(request, url, meta) {
    const a = await this.authorize(request, meta, ["room"]);
    if (a.error) return a.error;
    const q = url.searchParams;
    const after = q.get("after");
    const before = q.get("before");
    if ((after === null) === (before === null)) {
      return err(400, "schema", "pass exactly one of after=N or before=N");
    }
    const cursor = after !== null ? after : before;
    if (!RE_UINT.test(cursor)) return err(400, "schema", "after and before must be non-negative integers");
    let limit = 50;
    if (q.get("limit") !== null) {
      if (!RE_UINT.test(q.get("limit"))) return err(400, "schema", "limit must be an integer from 1 to 200");
      limit = Number(q.get("limit"));
      if (limit < 1 || limit > 200) return err(400, "schema", "limit must be an integer from 1 to 200");
    }
    const fresh = this.loadMeta();
    const agent = q.get("agent");
    if (agent !== null) {
      const row = this.agentByName(agent);
      if (!row || row.kicked === 1) return json(200, { rseq: fresh.rseq, frames: [], more: false });
    }
    const n = Number(cursor);
    const filter = agent !== null ? " AND t = 'batch' AND agent = ?" : "";
    const args = agent !== null ? [agent] : [];
    let rows;
    if (after !== null) {
      rows = this.sql.exec(`SELECT body FROM log WHERE rseq > ?${filter} ORDER BY rseq LIMIT ?`, n, ...args, limit + 1).toArray();
    } else {
      rows = this.sql.exec(`SELECT body FROM log WHERE rseq < ?${filter} ORDER BY rseq DESC LIMIT ?`, n, ...args, limit + 1).toArray();
    }
    const more = rows.length > limit;
    if (more) rows.length = limit;
    if (before !== null) rows.reverse();
    return json(200, { rseq: fresh.rseq, frames: rows.map((r) => JSON.parse(r.body)), more });
  }

  // -- routes: stream -----------------------------------------------------

  async stream(request, url, meta) {
    const ticket = url.searchParams.get("ticket");
    if (ticket !== null) {
      // The query form is authoritative when present; the header is not read (§6.13).
      if (!RE_TICKET.test(ticket)) return err(401, "auth", "that is not a viewer ticket; mint one with POST /r/{room}/ticket");
      const hash = await sha256hex(ticket);
      const rows = this.sql.exec("SELECT expires FROM tickets WHERE hash = ?", hash).toArray();
      if (rows.length === 0 || rows[0].expires <= Date.now()) {
        return err(401, "auth", "unknown or expired ticket; mint a new one with POST /r/{room}/ticket");
      }
    } else {
      const a = await this.authorize(request, meta, ["room"]);
      if (a.error) return a.error;
    }
    const afterRaw = url.searchParams.get("after");
    let after = null;
    if (afterRaw !== null) {
      if (!RE_UINT.test(afterRaw)) return err(400, "schema", "after must be a non-negative integer");
      after = Number(afterRaw);
    }
    if ((request.headers.get("Upgrade") || "").toLowerCase() !== "websocket") {
      return err(400, "schema", "open /stream as a WebSocket; this relay advertises transports [\"ws\"] on /healthz");
    }
    const pair = new WebSocketPair();
    const [client, server] = Object.values(pair);
    if (this.ctx.getWebSockets("viewer").length >= MAX_VIEWERS) {
      this.ctx.acceptWebSocket(server, ["full"]);
      server.send(JSON.stringify({ t: "full" }));
      server.close(1000, "full");
      return new Response(null, { status: 101, webSocket: client });
    }
    // Registered before the snapshot is read (§6.6): a batch landing between
    // hello and live is then seen live rather than lost.
    this.ctx.acceptWebSocket(server, ["viewer"]);
    server.serializeAttachment({ after, at: Date.now() });
    for (const frame of this.backfill(this.loadMeta(), after)) server.send(frame);
    return new Response(null, { status: 101, webSocket: client });
  }

  backfill(meta, after) {
    const out = [];
    if (after === null) {
      out.push(JSON.stringify({ t: "hello", transport: "ws", backfill: "tail", gap: null, room: this.roomObject(meta) }));
      const recs = this.recordEvents();
      for (let i = 0; i < recs.length; i += RECORDS_PER_FRAME) {
        out.push(JSON.stringify({ t: "records", events: recs.slice(i, i + RECORDS_PER_FRAME) }));
      }
      const names = this.sql.exec("SELECT DISTINCT agent FROM log WHERE t = 'batch' ORDER BY agent").toArray();
      for (const { agent } of names) {
        // Newest first until the budget is met, whole batches, at least one;
        // then delivered oldest-first (§5).
        const tail = [];
        let events = 0;
        let bytes = 0;
        const cursor = this.sql.exec("SELECT bytes, npos, body FROM log WHERE t = 'batch' AND agent = ? ORDER BY rseq DESC", agent);
        for (const row of cursor) {
          tail.push(row.body);
          events += row.npos;
          bytes += row.bytes;
          if (events >= TAIL_EVENTS || bytes >= TAIL_BYTES) break;
        }
        tail.reverse();
        for (const f of tail) out.push(f);
      }
      out.push(JSON.stringify({ t: "live" }));
      return out;
    }
    const floor = Math.max(after, meta.horizon);
    let gap = after < meta.horizon ? { before_rseq: meta.horizon + 1 } : null;
    const sizes = this.sql.exec("SELECT rseq, bytes FROM log WHERE rseq > ? ORDER BY rseq", floor).toArray();
    let total = 0;
    let start = sizes.length;
    for (let i = sizes.length - 1; i >= 0; i -= 1) {
      if (total + sizes[i].bytes > REPLAY_BYTES && i < sizes.length - 1) break;
      total += sizes[i].bytes;
      start = i;
    }
    if (start > 0) gap = { before_rseq: sizes[start].rseq };
    const backfill = sizes.length === 0 ? "none" : "replay";
    out.push(JSON.stringify({ t: "hello", transport: "ws", backfill, gap, room: this.roomObject(meta) }));
    if (sizes.length > 0) {
      const rows = this.sql.exec("SELECT body FROM log WHERE rseq >= ? ORDER BY rseq", sizes[start].rseq).toArray();
      for (const r of rows) out.push(r.body);
    }
    out.push(JSON.stringify({ t: "live" }));
    return out;
  }

  // Viewers never send anything the relay acts on; asks and roles are HTTP so
  // both transports stay symmetric. Keepalives never reach here.
  async webSocketMessage() {}
  async webSocketClose() {}
  async webSocketError() {}

  // -- routes: agents -----------------------------------------------------

  async register(request, meta, name) {
    const a = await this.authorize(request, meta, ["join"]);
    if (a.error) return a.error;
    const read = await readJsonBody(request);
    if (read.error) return read.error;
    if (!RE_NAME.test(name)) return err(400, "schema", "agent names match [a-z0-9][a-z0-9-]{0,47}");
    const b = read.body;
    const fields = {};
    for (const k of ["harness", "human", "model"]) {
      if (b[k] === undefined || b[k] === null) { fields[k] = null; continue; }
      const s = humanField(b[k], 120);
      if (s === null) return err(400, "schema", `${k} must be a string of at most 120 characters`);
      fields[k] = s;
    }
    const stream = b.stream === undefined ? "tools" : b.stream;
    if (!STREAMS.includes(stream)) return err(400, "schema", "stream must be one of summary, tools, full");
    const fresh = this.loadMeta();
    const existing = this.agentByName(name);
    if (!existing || existing.kicked === 1) {
      const live = this.sql.exec("SELECT COUNT(*) AS n FROM agents WHERE kicked = 0").one().n;
      if (live >= MAX_AGENTS) return err(503, "full", "the room holds 12 live agents; kick one with DELETE /r/{room}/agents/{name}");
    }
    const token = `at-${randomSymbols(32)}`;
    const hash = await sha256hex(token);
    const now = nowIso();
    if (existing) {
      this.sql.exec(
        "UPDATE agents SET token_hash = ?, harness = ?, human = ?, model = ?, stream = ?, last_seen = ?, kicked = 0 WHERE name = ?",
        hash, fields.harness, fields.human, fields.model, stream, now, name,
      );
    } else {
      this.sql.exec(
        "INSERT INTO agents (name, token_hash, harness, human, model, stream, snapshot, registered_at, last_seen, kicked, bucket, bucket_at) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, 0, ?, ?)",
        name, hash, fields.harness, fields.human, fields.model, stream, now, now, BUCKET_BURST, Date.now(),
      );
    }
    this.broadcast([this.emitAgent(name)]);
    const effective = STREAMS[Math.min(STREAMS.indexOf(stream), STREAMS.indexOf(fresh.policy.max_stream))];
    return json(200, { token, rseq: fresh.rseq, policy: fresh.policy, effective_stream: effective });
  }

  async kick(request, meta, name) {
    const a = await this.authorize(request, meta, ["join", "token"]);
    if (a.error) return a.error;
    if (a.cls === "token" && a.agent !== name) {
      return err(403, "forbidden", "an agent token can only remove its own name");
    }
    const row = this.agentByName(name);
    if (!row) return err(404, "agent", "no agent of that name was ever registered here");
    if (row.kicked === 1) return noContent();
    this.sql.exec("UPDATE agents SET kicked = 1, token_hash = NULL WHERE name = ?", name);
    this.broadcast([this.emitAgent(name)]);
    return noContent();
  }

  // -- routes: events -----------------------------------------------------

  // Token bucket per agent: returns null when a token was taken, else the
  // seconds to wait. Read and written in one synchronous step, after every await.
  takeToken(name) {
    const row = this.agentByName(name);
    const now = Date.now();
    const level = Math.min(BUCKET_BURST, row.bucket + ((now - row.bucket_at) / 1000) * BUCKET_RATE);
    if (level < 1) {
      this.sql.exec("UPDATE agents SET bucket = ?, bucket_at = ? WHERE name = ?", level, now, name);
      return Math.max(1, Math.ceil((1 - level) / BUCKET_RATE));
    }
    this.sql.exec("UPDATE agents SET bucket = ?, bucket_at = ? WHERE name = ?", level - 1, now, name);
    return null;
  }

  async postEvents(request, meta) {
    const a = await this.authorize(request, meta, ["token"]);
    if (a.error) return a.error;
    const name = a.agent;
    const buf = new Uint8Array(await request.arrayBuffer());
    if (buf.length > BATCH_BYTES) {
      return err(413, "oversize", "split the batch: at most 65,536 bytes and 200 events per POST");
    }
    const lines = splitLines(buf);
    if (lines.length > BATCH_LINES) {
      return err(413, "oversize", "split the batch: at most 200 events per POST");
    }
    // The digest is over the raw bytes, before the agent overwrite (§6.4).
    const digest = await sha256hex(buf);
    if (!this.agentByName(name) || this.agentByName(name).kicked === 1) {
      return err(401, "auth", "the token was invalidated while the batch was in flight; re-register");
    }
    const wait = this.takeToken(name);
    if (wait !== null) {
      return err(429, "rate", `wait ${wait}s; an agent may post 2 batches per second with a burst of 10`, { "Retry-After": String(wait) });
    }
    const fresh = this.loadMeta();
    const now = nowIso();
    this.sql.exec("UPDATE agents SET last_seen = ? WHERE name = ?", now, name);
    if (lines.length === 0) return json(202, { rseq: fresh.rseq, accepted: 0, dup: 0, rejected: [] });
    const prior = this.sql.exec("SELECT rseq FROM log WHERE t = 'batch' AND digest = ? LIMIT 1", digest).toArray();
    if (prior.length) return json(202, { rseq: prior[0].rseq, accepted: 0, dup: lines.length, rejected: [] });

    const level = fresh.policy.max_stream;
    const stored = [];
    const rejected = [];
    const frames = [];
    const answered = [];
    let accepted = 0;
    let dup = 0;
    let npos = 0;
    for (const line of lines) {
      if (line.bytes > EVENT_BYTES) { rejected.push({ id: idOf(line.text), why: "oversize" }); continue; }
      let ev;
      try { ev = JSON.parse(line.text); } catch (e) { rejected.push({ id: null, why: "schema" }); continue; }
      if (!isObj(ev)) { rejected.push({ id: null, why: "schema" }); continue; }
      ev.agent = name;
      const id = typeof ev.id === "string" ? ev.id : null;
      if (!envelopeOk(ev)) { rejected.push({ id, why: "schema" }); continue; }
      if (!KINDS.has(ev.kind)) { rejected.push({ id, why: "kind" }); continue; }
      if (violatesPolicy(ev, level)) { rejected.push({ id, why: "policy" }); continue; }
      if (ev.kind === "answer") {
        const askId = ev.body.ask;
        const rows = typeof askId === "string"
          ? this.sql.exec("SELECT * FROM asks WHERE id = ?", askId).toArray() : [];
        if (rows.length === 0 || rows[0].to_agent !== name) { rejected.push({ id, why: "schema" }); continue; }
        if (rows[0].state === "open" && !answered.some((x) => x.row.id === askId)) {
          answered.push({ row: rows[0], text: typeof ev.body.text === "string" ? ev.body.text : "", ts: ev.ts });
        }
      } else if (ev.kind === "record") {
        if (this.sql.exec("SELECT 1 FROM records WHERE id = ?", ev.id).toArray().length) { dup += 1; continue; }
        this.holdRecord(ev);
      } else if (ev.kind === "agent") {
        accepted += 1;
        this.sql.exec("UPDATE agents SET snapshot = ?, last_seen = ? WHERE name = ?", JSON.stringify(ev.body), now, name);
        frames.push(this.emitAgent(name));
        continue;
      }
      accepted += 1;
      if (POSITIONAL.has(ev.kind)) npos += 1;
      stored.push(ev);
    }
    let rseq = fresh.rseq;
    if (stored.length > 0) {
      rseq = this.nextRseq(fresh);
      const frame = JSON.stringify({ t: "batch", rseq, agent: name, events: stored });
      this.sql.exec(
        "INSERT INTO log (rseq, t, agent, ts, bytes, digest, body, npos) VALUES (?, 'batch', ?, ?, ?, ?, ?, ?)",
        rseq, name, now, buf.length, digest, frame, npos,
      );
      this.setMeta("last_batch_at", now);
      frames.push(frame);
      answered.sort((x, y) => x.row.seq - y.row.seq);
      for (const { row, text, ts } of answered) {
        const answer = JSON.stringify({ text, ts });
        this.sql.exec("UPDATE asks SET state = 'answered', answer = ? WHERE seq = ?", answer, row.seq);
        const ask = this.askObject({ ...row, state: "answered", answer });
        frames.push(this.appendRow(fresh, "ask", row.to_agent, { ask }, 0, 0).frame);
      }
      this.enforceBytes(fresh, name);
    }
    this.broadcast(frames);
    return json(202, { rseq, accepted, dup, rejected });
  }

  // Records are keyed by event id for dedup and by rid for replacement: a
  // newer state of the same record supersedes the older one in the map while
  // the older event still rides in its batch row (§6.4).
  holdRecord(ev) {
    const rid = typeof ev.body.rid === "string" ? ev.body.rid : null;
    const ts = typeof ev.body.ts === "string" ? ev.body.ts : null;
    const parsed = ts === null ? NaN : Date.parse(ts);
    const tsMs = Number.isNaN(parsed) ? -1 : parsed;
    if (rid !== null) {
      const held = this.sql.exec("SELECT id, ts_ms FROM records WHERE rid = ?", rid).toArray();
      if (held.some((h) => h.ts_ms > tsMs)) return;
      for (const h of held) this.sql.exec("DELETE FROM records WHERE id = ?", h.id);
    }
    this.sql.exec("INSERT INTO records (id, rid, ts, ts_ms, body) VALUES (?, ?, ?, ?, ?)", ev.id, rid, ts, tsMs, JSON.stringify(ev));
    this.trimRecords();
  }

  trimRecords() {
    const n = this.sql.exec("SELECT COUNT(*) AS n FROM records").one().n;
    if (n > MAX_RECORDS) {
      this.sql.exec("DELETE FROM records WHERE rowid IN (SELECT rowid FROM records ORDER BY ts_ms, rowid LIMIT ?)", n - MAX_RECORDS);
    }
  }

  // Byte bounds count batch rows only, oldest first, the agent's then the
  // room's; every deletion raises the prune horizon (§6.7).
  enforceBytes(meta, name) {
    const names = name === null
      ? this.sql.exec("SELECT DISTINCT agent FROM log WHERE t = 'batch'").toArray().map((r) => r.agent)
      : [name];
    for (const agent of names) this.trimTo(meta, "SELECT rseq, bytes FROM log WHERE t = 'batch' AND agent = ? ORDER BY rseq", [agent], AGENT_BYTES);
    this.trimTo(meta, "SELECT rseq, bytes FROM log WHERE t = 'batch' ORDER BY rseq", [], ROOM_BYTES);
  }

  trimTo(meta, query, args, cap) {
    const rows = this.sql.exec(query, ...args).toArray();
    let total = 0;
    for (const r of rows) total += r.bytes;
    let i = 0;
    while (total > cap && i < rows.length) {
      this.sql.exec("DELETE FROM log WHERE rseq = ?", rows[i].rseq);
      this.raiseHorizon(meta, rows[i].rseq);
      total -= rows[i].bytes;
      i += 1;
    }
  }

  // -- routes: inbox, asks, roles, policy --------------------------------

  async inbox(request, url, meta) {
    const a = await this.authorize(request, meta, ["token"]);
    if (a.error) return a.error;
    const afterRaw = url.searchParams.get("after");
    let after = 0;
    if (afterRaw !== null) {
      if (!RE_UINT.test(afterRaw)) return err(400, "schema", "after must be a non-negative integer");
      after = Number(afterRaw);
    }
    const fresh = this.loadMeta();
    this.sql.exec("UPDATE agents SET last_seen = ? WHERE name = ?", nowIso(), a.agent);
    const asks = this.sql.exec(
      "SELECT id, seq, viewer, text, ts FROM asks WHERE state = 'open' AND to_agent = ? AND seq > ? ORDER BY seq", a.agent, after,
    ).toArray().map((r) => ({ id: r.id, seq: r.seq, viewer: r.viewer, text: r.text, ts: r.ts }));
    return json(200, { rseq: fresh.rseq, role: this.roleObject(a.agent), asks });
  }

  async ask(request, meta) {
    const a = await this.authorize(request, meta, ["room"]);
    if (a.error) return a.error;
    const read = await readJsonBody(request);
    if (read.error) return read.error;
    const b = read.body;
    if (typeof b.to !== "string") return err(400, "schema", "to must name an agent in the room");
    const viewer = humanField(b.viewer, 40);
    if (viewer === null || viewer === "") return err(400, "schema", "viewer is required: a name of at most 40 characters");
    const text = humanField(b.text, 500);
    if (text === null) return err(400, "schema", "text must be a string of at most 500 characters");
    const target = this.agentByName(b.to);
    if (!target || target.kicked === 1) return err(404, "agent", "no live agent of that name; see GET /r/{room}.agents");
    const now = Date.now();
    const last = this.sql.exec("SELECT at FROM viewer_rate WHERE viewer = ?", viewer).toArray();
    if (last.length && now - last[0].at < ASK_GAP_MS) {
      const wait = Math.max(1, Math.ceil((ASK_GAP_MS - (now - last[0].at)) / 1000));
      return err(429, "rate", `one ask per viewer every 5 s; wait ${wait}s`, { "Retry-After": String(wait) });
    }
    const open = this.sql.exec("SELECT COUNT(*) AS n FROM asks WHERE state = 'open'").one().n;
    if (open >= MAX_OPEN_ASKS) return err(503, "full", "40 asks are already open in this room; wait for answers or expiry");
    const fresh = this.loadMeta();
    this.sql.exec("INSERT INTO viewer_rate (viewer, at) VALUES (?, ?) ON CONFLICT(viewer) DO UPDATE SET at = excluded.at", viewer, now);
    const seq = this.nextRseq(fresh);
    const id = `ca-${fresh.room4}-${seq}`;
    const ts = nowIso();
    this.sql.exec(
      "INSERT INTO asks (seq, id, to_agent, viewer, text, ts, expires_ms, state, answer) VALUES (?, ?, ?, ?, ?, ?, ?, 'open', NULL)",
      seq, id, b.to, viewer, text, ts, now + fresh.policy.ask_ttl_s * 1000,
    );
    const ask = { id, seq, to: b.to, viewer, text, ts, state: "open", answer: null };
    const frame = JSON.stringify({ t: "ask", rseq: seq, ask });
    this.sql.exec("INSERT INTO log (rseq, t, agent, ts, bytes, digest, body, npos) VALUES (?, 'ask', ?, ?, ?, NULL, ?, 0)", seq, b.to, ts, read.bytes, frame);
    this.broadcast([frame]);
    return json(201, { id, seq });
  }

  async role(request, meta, name) {
    const a = await this.authorize(request, meta, ["room", "token"]);
    if (a.error) return a.error;
    if (a.cls === "token" && a.agent !== name) return err(403, "forbidden", "an agent token can only set its own role");
    const read = await readJsonBody(request);
    if (read.error) return read.error;
    const b = read.body;
    let role = null;
    if (b.role !== undefined && b.role !== null && b.role !== "") {
      role = humanField(b.role, 60);
      if (role === null) return err(400, "schema", "role must be a string of at most 60 characters, or null to clear");
      if (role === "") role = null;
    }
    let viewer = "owner";
    if (a.cls === "room") {
      viewer = humanField(b.viewer, 40);
      if (viewer === null || viewer === "") return err(400, "schema", "viewer is required: a name of at most 40 characters");
    }
    const target = this.agentByName(name);
    if (!target || target.kicked === 1) return err(404, "agent", "no live agent of that name; see GET /r/{room}.agents");
    const current = this.sql.exec("SELECT role, set_seq, changed_at FROM roles WHERE agent = ?", name).toArray();
    const currentRole = current.length ? current[0].role : null;
    // A no-op consumes nothing and emits nothing (§3.11).
    if (role === currentRole) return json(200, { set_seq: current.length ? current[0].set_seq : 0 });
    const now = Date.now();
    if (current.length && now - current[0].changed_at < ROLE_GAP_MS) {
      const wait = Math.max(1, Math.ceil((ROLE_GAP_MS - (now - current[0].changed_at)) / 1000));
      return err(429, "rate", `one role change per agent every 30 s; wait ${wait}s`, { "Retry-After": String(wait) });
    }
    const fresh = this.loadMeta();
    const ts = nowIso();
    const seq = this.nextRseq(fresh);
    this.sql.exec(
      "INSERT INTO roles (agent, role, viewer, set_seq, ts, changed_at) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(agent) DO UPDATE SET role = excluded.role, viewer = excluded.viewer, set_seq = excluded.set_seq, ts = excluded.ts, changed_at = excluded.changed_at",
      name, role, viewer, seq, ts, now,
    );
    const obj = role === null ? null : { role, viewer, set_seq: seq, ts };
    const frame = JSON.stringify({ t: "role", rseq: seq, agent: name, role: obj });
    this.sql.exec("INSERT INTO log (rseq, t, agent, ts, bytes, digest, body, npos) VALUES (?, 'role', ?, ?, ?, NULL, ?, 0)", seq, name, ts, read.bytes, frame);
    this.broadcast([frame]);
    return json(200, { set_seq: seq });
  }

  async policy(request, meta) {
    const a = await this.authorize(request, meta, ["join"]);
    if (a.error) return a.error;
    const read = await readJsonBody(request);
    if (read.error) return read.error;
    const fresh = this.loadMeta();
    const merged = mergePolicy(fresh.policy, read.body, true);
    if (typeof merged === "string") {
      return err(400, "schema", `send at least one of max_stream, retention_min, ticket_ttl_s, ask_ttl_s in range (${merged})`);
    }
    this.setMeta("policy", JSON.stringify(merged));
    const { frame } = this.appendRow(fresh, "policy", null, { policy: merged }, read.bytes, 0);
    this.broadcast([frame]);
    return json(200, { policy: merged });
  }

  // -- routes: lifecycle --------------------------------------------------

  async deleteRoom(request, meta) {
    const a = await this.authorize(request, meta, ["join"]);
    if (a.error) return a.error;
    await this.wipe();
    return noContent();
  }

  async prune(request, meta) {
    const a = await this.authorize(request, meta, ["join"]);
    if (a.error) return a.error;
    const read = await readJsonBody(request);
    if (read.error) return read.error;
    await this.runPass(this.loadMeta());
    if ((await this.ctx.storage.getAlarm()) === null && this.loadMeta()) {
      await this.ctx.storage.setAlarm(Date.now() + ALARM_MS);
    }
    return noContent();
  }

  async wipe() {
    const gone = JSON.stringify({ t: "gone" });
    for (const ws of this.ctx.getWebSockets()) {
      try { ws.send(gone); ws.close(1000, "gone"); } catch (e) { /* already closing */ }
    }
    await this.ctx.storage.deleteAlarm();
    await this.ctx.storage.deleteAll();
    // deleteAll drops the tables too; this instance may stay resident and must
    // answer 404 from an empty schema rather than a SQL error.
    this.sql.exec(SCHEMA);
  }

  async alarm() {
    const meta = this.loadMeta();
    if (!meta) return;
    const wiped = await this.runPass(meta);
    if (!wiped) await this.ctx.storage.setAlarm(Date.now() + ALARM_MS);
  }

  // The periodic pass (§3.14): everything time-driven, in one place, so the
  // alarm and POST /prune cannot drift apart. Returns true when the room was wiped.
  async runPass(meta) {
    const now = Date.now();
    const last = Date.parse(meta.last_batch_at || meta.created_at);
    if (Number.isFinite(last) && now - last > IDLE_WIPE_MS) {
      await this.wipe();
      return true;
    }
    const cutoff = new Date(now - meta.policy.retention_min * 60 * 1000).toISOString();
    const oldest = this.sql.exec("SELECT MAX(rseq) AS m FROM log WHERE ts < ?", cutoff).one().m;
    if (oldest !== null) {
      this.sql.exec("DELETE FROM log WHERE ts < ?", cutoff);
      this.raiseHorizon(meta, oldest);
    }
    this.enforceBytes(meta, null);
    this.sql.exec("DELETE FROM tickets WHERE expires <= ?", now);
    this.sql.exec("DELETE FROM viewer_rate WHERE at < ?", now - ASK_GAP_MS);
    const frames = [];
    const expired = this.sql.exec("SELECT * FROM asks WHERE state = 'open' AND expires_ms <= ? ORDER BY seq", now).toArray();
    for (const row of expired) {
      this.sql.exec("UPDATE asks SET state = 'expired' WHERE seq = ?", row.seq);
      const ask = this.askObject({ ...row, state: "expired", answer: null });
      frames.push(this.appendRow(meta, "ask", row.to_agent, { ask }, 0, 0).frame);
    }
    this.trimRecords();
    this.broadcast(frames);
    return false;
  }
}

// ---------------------------------------------------------------- events

// Splits the raw body on 0x0A so each line's byte length is exact rather than
// re-encoded; blank and whitespace-only lines are dropped here (§3.8).
function splitLines(buf) {
  const out = [];
  let start = 0;
  for (let i = 0; i <= buf.length; i += 1) {
    if (i === buf.length || buf[i] === 0x0a) {
      if (i > start) {
        const text = DEC.decode(buf.subarray(start, i));
        if (text.trim() !== "") out.push({ bytes: i - start, text });
      }
      start = i + 1;
    }
  }
  return out;
}

function idOf(text) {
  try {
    const v = JSON.parse(text);
    return isObj(v) && typeof v.id === "string" ? v.id : null;
  } catch (e) {
    return null;
  }
}

function strMax(v, max) {
  return typeof v === "string" && byteLen(v) <= max;
}

// Exactly the checks of §4.1; nothing else is validated and unknown fields
// are stored unchanged. `model` and `ref` may be absent (read as null).
function envelopeOk(ev) {
  if (ev.v !== 1) return false;
  if (!strMax(ev.id, 64) || ev.id.length === 0) return false;
  if (typeof ev.kind !== "string") return false;
  if (!isObj(ev.body)) return false;
  if (!strMax(ev.session, 64)) return false;
  if (!strMax(ev.lane, 64) || ev.lane.length === 0) return false;
  if (!Number.isInteger(ev.epoch) || ev.epoch < 0) return false;
  if (!Number.isInteger(ev.seq) || ev.seq < 0) return false;
  if (!strMax(ev.ts, 40)) return false;
  if (!strMax(ev.harness, 32)) return false;
  if (ev.model !== null && ev.model !== undefined && !strMax(ev.model, 64)) return false;
  if (ev.ref !== null && ev.ref !== undefined && !strMax(ev.ref, 128)) return false;
  return true;
}

// §4.4, per event: content-bearing keys are looked for at the top level of
// `args` only, and `args: {}` passes even at summary.
function violatesPolicy(ev, level) {
  if (level === "full") return false;
  const b = ev.body;
  if (ev.kind === "thinking") return true;
  if (ev.kind === "tool_result" && hasOwn(b, "text")) return true;
  if (ev.kind === "tool_call" && isObj(b.args)) {
    const keys = Object.keys(b.args);
    if (level === "summary") return keys.length > 0;
    return keys.some((k) => CONTENT_KEYS.includes(k));
  }
  if (level === "summary") {
    if (ev.kind === "text") return true;
    if (ev.kind === "prompt" && typeof b.text === "string") {
      return cpLen(b.text) > 200 || byteLen(b.text) > 800;
    }
  }
  return false;
}
