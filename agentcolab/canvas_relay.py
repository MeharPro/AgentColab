"""The reference canvas relay: rooms, credentials, the room log, SSE.

Stdlib only, in memory, one lock. It exists so docs/canvas-contract.md has an
implementation the Worker, the client and the frontend are held to, and so
`colab canvas serve` runs on a host you own without an account. Everything a
viewer sees is a frame from the room log; the relay validates, rejects or stores
an event and never edits one (contract §0, §6.3), because two backends that
alter the same input differently are two contracts.

v1.3 (§10) adds the message table that replaced asks, owner tokens, the wake
settings on every agent object, wake-acks, and a second SSE stream -- the agent
stream -- that a machine's listener holds so a ping can start a session there.
"""

from __future__ import annotations

import argparse
import collections
import contextlib
import hashlib
import hmac
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterable

from . import records

# ---------------------------------------------------------------- limits (§2, §10)

ALPHABET = "23456789abcdefghjkmnpqrstvwxyz"
_SYM = "[" + ALPHABET + "]"
ROOM_RE = re.compile(f"^{_SYM}{{4}}-{_SYM}{{4}}-{_SYM}{{2}}$")
JOIN_RE = re.compile(f"^{_SYM}{{4}}-{_SYM}{{4}}-{_SYM}{{2}}\\.{_SYM}{{24}}$")
TOKEN_RE = re.compile(f"^at-{_SYM}{{32}}$")
OWNER_RE = re.compile(f"^ot-{_SYM}{{32}}$")
TICKET_RE = re.compile(f"^vt-{_SYM}{{32}}$")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
# Single-line human fields: everything below 0x20 (tab and newline included)
# plus the C1 range, stripped before any cap is measured (§0).
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")

BATCH_BYTES = 65_536
BATCH_LINES = 200
EVENT_BYTES = 40_960
AGENT_BYTES = 1 << 20
ROOM_BYTES = 8 << 20
TAIL_EVENTS = 80
TAIL_BYTES = 256 << 10
REPLAY_BYTES = 2 << 20
MAX_AGENTS = 12
MAX_VIEWERS = 25
MAX_AGENT_STREAMS = 4
MAX_OPEN_ASKS = 200
MESSAGES_SHOWN = 100
MAX_RECORDS = 2_000
RECORDS_PER_FRAME = 200
VIEWER_MESSAGE_GAP_S = 5
AGENT_MESSAGES_PER_MIN = 10
MESSAGE_TTL_S = 7 * 86_400
ROLE_GAP_S = 30
BUCKET_RATE = 2.0
BUCKET_BURST = 10.0
KEEPALIVE_S = 15
PASS_S = 60
IDLE_WIPE_S = 7 * 86_400
# A request body larger than this is refused unread; the batch cap is 64 KiB
# and every other body is a few hundred bytes, so nothing legitimate gets near.
READ_CAP = 4 << 20

KINDS = frozenset({"agent", "text", "thinking", "tool_call", "tool_result", "prompt",
                   "session", "record", "answer", "gap"})
POSITIONAL = frozenset({"text", "thinking", "tool_call", "tool_result", "prompt", "session", "gap"})
CONTENT_KEYS = frozenset({"content", "new_string", "old_string", "edits", "cells", "patch",
                          "new_source"})
STREAMS = ("summary", "tools", "full")
POLICY_RANGES = {"retention_min": (5, 720), "ticket_ttl_s": (1, 3600), "ask_ttl_s": (1, 604_800)}
POLICY_KEYS = ("max_stream", "retention_min", "ticket_ttl_s", "ask_ttl_s")
DEFAULT_POLICY = {"max_stream": "tools", "retention_min": 120, "ticket_ttl_s": 600,
                  "ask_ttl_s": 86_400}
MESSAGE_KINDS = ("ask", "ping", "say")
WAKE_KEYS = ("enabled", "from", "max_per_hour")
WAKE_FROM = ("agents", "room")
WAKE_RESULTS = ("woke", "busy", "declined", "off")
DEFAULT_WAKE = {"enabled": False, "from": "agents", "max_per_hour": 4}

_CLASS_NAMES = {"room": "a room code", "join": "a join code", "token": "an agent token",
                "owner": "an owner token", "ticket": "a viewer ticket"}

PLACEHOLDER_HTML = (b"<!doctype html><meta charset=utf-8><title>AgentColab canvas</title>"
                    b"<p>The relay is up; the canvas frontend (canvas/web/index.html) is not"
                    b" built yet.</p>")


class _Reply(Exception):
    """An HTTP answer raised from anywhere inside a route; the handler writes it."""

    def __init__(self, status: int, code: str, hint: str,
                 headers: dict[str, str] | None = None) -> None:
        super().__init__(hint)
        self.status, self.code, self.hint = status, code, hint
        self.headers = headers or {}


# ---------------------------------------------------------------- helpers


def _symbols(n: int) -> str:
    out: list[str] = []
    while len(out) < n:
        for byte in os.urandom(n):
            # 240 = 8 * 30: rejecting the top 16 values keeps every symbol equally
            # likely, which a plain modulo over 256 does not.
            if byte < 240:
                out.append(ALPHABET[byte % 30])
                if len(out) == n:
                    break
    return "".join(out)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _classify(value: str) -> str | None:
    if ROOM_RE.match(value):
        return "room"
    if JOIN_RE.match(value):
        return "join"
    if TOKEN_RE.match(value):
        return "token"
    if OWNER_RE.match(value):
        return "owner"
    if TICKET_RE.match(value):
        return "ticket"
    return None


def _clean(text: str) -> str:
    return _CONTROL.sub("", text)


def _over(text: str, code_points: int) -> bool:
    return len(text) > code_points or len(text.encode("utf-8")) > 4 * code_points


def _blen(text: str) -> int:
    return len(text.encode("utf-8", "surrogatepass"))


def _iso_ms(t: float) -> str:
    dt = datetime.fromtimestamp(t, timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _instant(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    dt = records.parse_iso(value)
    return dt.timestamp() if dt else None


def _is_int(value: Any) -> bool:
    # bool is an int subclass; `true` must not pass as 1. An integral float
    # does pass: JSON has one number type and the Worker's Number.isInteger
    # accepts `1.0`, so a client whose serialiser writes it must get the same
    # answer from both backends.
    if isinstance(value, bool):
        return False
    return isinstance(value, int) or (isinstance(value, float) and value.is_integer())


def _nonneg(text: str | None) -> int | None:
    if text is None or not re.fullmatch(r"\d+", text.strip()):
        return None
    return int(text)


def _ceil(x: float) -> int:
    return int(-(-x // 1))


def _json_bytes(obj: Any) -> bytes:
    try:
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except UnicodeEncodeError:
        # A lone surrogate is legal JSON that UTF-8 cannot carry; escaping keeps
        # the parsed value identical, which is the only promise made.
        return json.dumps(obj, ensure_ascii=True, separators=(",", ":")).encode("ascii")


def _sse(frame: dict[str, Any]) -> bytes:
    head = b"id: %d\n" % frame["rseq"] if "rseq" in frame else b""
    return head + b"data: " + _json_bytes(frame) + b"\n\n"


def _validate_policy(body: Any, current: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise _Reply(400, "schema", "policy must be a JSON object with any of "
                     + ", ".join(POLICY_KEYS))
    policy = dict(current)
    for key, value in body.items():
        if key == "max_stream":
            if value not in STREAMS:
                raise _Reply(400, "schema", "max_stream must be summary, tools or full")
            policy[key] = value
        elif key in POLICY_RANGES:
            lo, hi = POLICY_RANGES[key]
            if not _is_int(value) or not lo <= value <= hi:
                raise _Reply(400, "schema", f"{key} must be an integer from {lo} to {hi}")
            policy[key] = int(value)
    return policy


def _envelope_ok(ev: dict[str, Any]) -> bool:
    """§4.1's relay validation, exhaustively; anything else is the client's rule."""
    def text(value: Any, cap: int, *, minimum: int = 0) -> bool:
        return isinstance(value, str) and minimum <= _blen(value) <= cap

    def text_or_null(value: Any, cap: int) -> bool:
        return value is None or text(value, cap)

    return (_is_int(ev.get("v")) and ev["v"] == 1
            and text(ev.get("id"), 64, minimum=1)
            and isinstance(ev.get("kind"), str)
            and isinstance(ev.get("body"), dict)
            and text(ev.get("session"), 64)
            and text(ev.get("lane"), 64, minimum=1)
            and _is_int(ev.get("epoch")) and ev["epoch"] >= 0
            and _is_int(ev.get("seq")) and ev["seq"] >= 0
            and text(ev.get("ts"), 40)
            and text(ev.get("harness"), 32)
            and text_or_null(ev.get("model"), 64)
            and text_or_null(ev.get("ref"), 128))


def _breaks_policy(kind: str, body: dict[str, Any], level: str) -> bool:
    """§4.4: what a room ceiling refuses, checked on every batch."""
    if level == "full":
        return False
    if kind == "thinking":
        return True
    if kind == "tool_result":
        return "text" in body
    if kind == "tool_call":
        args = body.get("args")
        if not isinstance(args, dict):
            return False
        if level == "summary":
            return len(args) > 0
        # Top-level keys only: a nested object is not inspected (§4.4).
        return any(key in CONTENT_KEYS for key in args)
    if level == "summary":
        if kind == "text":
            return True
        if kind == "prompt":
            text = body.get("text")
            return isinstance(text, str) and (len(text) > 200 or _blen(text) > 800)
    return False


# ---------------------------------------------------------------- state


class _Row:
    """One log row: the frame as delivered, plus what retention needs to know."""

    __slots__ = ("rseq", "t", "agent", "frame", "bytes", "digest", "ts", "positional")

    def __init__(self, rseq: int, t: str, frame: dict[str, Any], *, agent: str | None,
                 nbytes: int, digest: str | None, ts: float, positional: int) -> None:
        self.rseq, self.t, self.frame, self.agent = rseq, t, frame, agent
        self.bytes, self.digest, self.ts, self.positional = nbytes, digest, ts, positional

    def to_json(self) -> dict[str, Any]:
        return {"rseq": self.rseq, "t": self.t, "agent": self.agent, "frame": self.frame,
                "bytes": self.bytes, "digest": self.digest, "ts": self.ts,
                "positional": self.positional}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "_Row":
        return cls(int(data["rseq"]), str(data["t"]), data["frame"], agent=data.get("agent"),
                   nbytes=int(data.get("bytes", 0)), digest=data.get("digest"),
                   ts=float(data.get("ts", 0)), positional=int(data.get("positional", 0)))


class _Viewer:
    """One open SSE connection -- a viewer's `/stream` or an agent's `/agent-stream`."""

    __slots__ = ("queue", "cond", "closed")

    def __init__(self, cond: threading.Condition) -> None:
        self.queue: collections.deque = collections.deque()
        self.cond = cond
        self.closed = False


class _Room:
    def __init__(self, code: str, name: str, join_hash: str, policy: dict[str, Any],
                 now: float) -> None:
        self.code, self.name, self.join_hash, self.policy = code, name, join_hash, policy
        self.created_at = now
        self.last_batch_at = now
        self.rseq = 0
        self.horizon = 0
        self.log: list[_Row] = []
        self.digests: dict[str, int] = {}
        self.agent_bytes: dict[str, int] = {}
        self.total_bytes = 0
        self.agents: dict[str, dict[str, Any]] = {}
        self.roles: dict[str, dict[str, Any] | None] = {}
        self.role_seq: dict[str, int] = {}
        self.role_at: dict[str, float] = {}
        self.messages: dict[str, dict[str, Any]] = {}
        self.viewer_at: dict[str, float] = {}
        self.agent_message_at: dict[str, list[float]] = {}
        self.tickets: dict[str, float] = {}
        self.records: dict[str, dict[str, Any]] = {}
        self.rids: dict[str, str] = {}
        self.buckets: dict[str, list[float]] = {}
        self.viewers: list[_Viewer] = []
        self.agent_streams: dict[str, list[_Viewer]] = {}

    @property
    def room4(self) -> str:
        return self.code[:4]

    def to_json(self) -> dict[str, Any]:
        return {"code": self.code, "name": self.name, "join_hash": self.join_hash,
                "policy": self.policy, "created_at": self.created_at,
                "last_batch_at": self.last_batch_at, "rseq": self.rseq,
                "horizon": self.horizon, "log": [row.to_json() for row in self.log],
                "agents": self.agents, "roles": self.roles, "role_seq": self.role_seq,
                "messages": list(self.messages.values()), "tickets": self.tickets,
                "records": list(self.records.values())}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "_Room":
        room = cls(str(data["code"]), str(data["name"]), str(data["join_hash"]),
                   dict(DEFAULT_POLICY, **data.get("policy", {})), float(data["created_at"]))
        room.last_batch_at = float(data.get("last_batch_at", room.created_at))
        room.rseq = int(data.get("rseq", 0))
        room.horizon = int(data.get("horizon", 0))
        room.agents = data.get("agents", {})
        room.roles = data.get("roles", {})
        room.role_seq = {k: int(v) for k, v in data.get("role_seq", {}).items()}
        # A v1.2 state file carries `asks`; they have no place in the message table
        # and are dropped rather than guessed at.
        for message in data.get("messages", []):
            room.messages[message["id"]] = message
        room.tickets = {k: float(v) for k, v in data.get("tickets", {}).items()}
        for row_data in data.get("log", []):
            row = _Row.from_json(row_data)
            room.log.append(row)
            if row.t == "batch" and row.agent is not None:
                room.agent_bytes[row.agent] = room.agent_bytes.get(row.agent, 0) + row.bytes
                room.total_bytes += row.bytes
                if row.digest:
                    room.digests[row.digest] = row.rseq
        for ev in data.get("records", []):
            room.records[ev["id"]] = ev
            rid = ev.get("body", {}).get("rid") if isinstance(ev.get("body"), dict) else None
            if isinstance(rid, str):
                room.rids[rid] = ev["id"]
        return room


# ---------------------------------------------------------------- the relay


class Relay:
    """Bind, serve, shut down. All room state lives here under one lock."""

    def __init__(self, address: tuple[str, int], *, web: str | Path | None = None,
                 public_url: str | None = None, state_dir: str | Path | None = None) -> None:
        # Wall clock as a hook so a test can move time rather than sleep through
        # a 30 s role cap or a 120 min retention window.
        self.clock: Callable[[], float] = time.time
        self.web = Path(web) if web else Path(__file__).resolve().parents[1] / "canvas" / "web" / "index.html"
        self.public_url = public_url.rstrip("/") if public_url else None
        self.state_dir = Path(state_dir) if state_dir else None
        self.rooms: dict[str, _Room] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._serving = False
        self._server = _Server(address, _Handler)
        self._server.relay = self
        if self.state_dir:
            self._load()

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def serve_forever(self) -> None:
        self._serving = True
        threading.Thread(target=self._housekeeping, name="canvas-relay-pass", daemon=True).start()
        self._server.serve_forever()

    def shutdown(self) -> None:
        self._stop.set()
        with self._lock:
            for room in self.rooms.values():
                for stream in self._streams(room):
                    stream.closed = True
                    stream.cond.notify_all()
        # HTTPServer.shutdown() waits for a serve_forever() loop to notice; on a
        # relay that never served it would wait forever.
        if self._serving:
            self._server.shutdown()
        self._server.server_close()
        self._dump()

    # -- background ------------------------------------------------------

    def _housekeeping(self) -> None:
        while not self._stop.wait(PASS_S):
            with contextlib.suppress(Exception):
                self._pass_all()
            with contextlib.suppress(Exception):
                self._dump()

    def _pass_all(self) -> None:
        with self._lock:
            now = self.clock()
            for room in list(self.rooms.values()):
                self._prune(room, now)

    # -- state file ------------------------------------------------------

    def _dump(self) -> None:
        if not self.state_dir:
            return
        with self._lock:
            payload = {"version": 1, "rooms": {code: room.to_json()
                                               for code, room in self.rooms.items()}}
            data = _json_bytes(payload)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        target = self.state_dir / "rooms.json"
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, target)

    def _load(self) -> None:
        assert self.state_dir is not None
        target = self.state_dir / "rooms.json"
        if not target.exists():
            return
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
            for code, data in payload.get("rooms", {}).items():
                self.rooms[code] = _Room.from_json(data)
        except (ValueError, KeyError, TypeError) as exc:
            records.eprint(f"canvas relay: {target} is not a state file this relay wrote "
                           f"({exc}); starting empty -- move it aside to silence this")
            self.rooms = {}

    # -- lookups ---------------------------------------------------------

    def _room(self, code: str) -> _Room:
        room = self.rooms.get(code)
        if room is None:
            raise _Reply(404, "room", "no room by that code; rooms are wiped a week after "
                         "the last batch -- create one with POST /rooms")
        return room

    def _agent_by_hash(self, room: _Room, digest: str, key: str) -> str | None:
        for name, agent in room.agents.items():
            stored = agent.get(key)
            if stored and hmac.compare_digest(stored, digest):
                return name
        return None

    def _ticket_valid(self, room: _Room, value: str) -> bool:
        if _classify(value) != "ticket":
            return False
        digest = _sha(value)
        now = self.clock()
        return any(hmac.compare_digest(stored, digest) and expires > now
                   for stored, expires in room.tickets.items())

    def _authenticate(self, room: _Room, header: str | None,
                      allowed: tuple[str, ...], wants: str) -> tuple[str, str | None]:
        """(class, agent name) for a credential the route accepts, else 401/403."""
        if not header or not header[:7].lower() == "bearer ":
            raise _Reply(401, "auth", f"send Authorization: Bearer <{wants}>")
        value = header[7:].strip()
        cls = _classify(value)
        if cls is None:
            raise _Reply(401, "auth", "the bearer value matches no credential shape; "
                         f"this route takes {wants}")
        if cls not in allowed:
            raise _Reply(403, "forbidden", f"this route takes {wants}; you sent "
                         f"{_CLASS_NAMES[cls]}")
        if cls == "room":
            if not hmac.compare_digest(value, room.code):
                raise _Reply(401, "auth", "that room code is not this room's")
            return cls, None
        if cls == "join":
            if not hmac.compare_digest(_sha(value), room.join_hash):
                raise _Reply(401, "auth", "unknown join code for this room")
            return cls, None
        if cls == "token":
            name = self._agent_by_hash(room, _sha(value), "token_hash")
            if name is None:
                raise _Reply(401, "auth", "unknown, rotated or kicked agent token; "
                             "re-register with the join code")
            return cls, name
        if cls == "owner":
            name = self._agent_by_hash(room, _sha(value), "owner_hash")
            if name is None:
                raise _Reply(401, "auth", "unknown, rotated or kicked owner token; "
                             "re-registering the agent prints a new owner link")
            return cls, name
        if not self._ticket_valid(room, value):
            raise _Reply(401, "auth", "unknown or expired viewer ticket; mint another "
                         "with POST /r/{room}/ticket")
        return cls, None

    # -- shapes ----------------------------------------------------------

    def _wake_used(self, agent: dict[str, Any]) -> int:
        # The hourly window is the relay's wall clock, reset on the hour (§10.4).
        hour = int(self.clock() // 3600)
        return int(agent.get("wake_used", 0)) if agent.get("wake_hour") == hour else 0

    def _count_wake(self, agent: dict[str, Any]) -> None:
        agent["wake_used"] = self._wake_used(agent) + 1
        agent["wake_hour"] = int(self.clock() // 3600)

    def _wake_object(self, room: _Room, name: str) -> dict[str, Any]:
        agent = room.agents[name]
        settings = agent.get("wake") or {}
        connected = any(not s.closed for s in room.agent_streams.get(name, ()))
        return {"enabled": bool(settings.get("enabled", DEFAULT_WAKE["enabled"])),
                "from": settings.get("from", DEFAULT_WAKE["from"]),
                "max_per_hour": settings.get("max_per_hour", DEFAULT_WAKE["max_per_hour"]),
                "used_this_hour": self._wake_used(agent),
                "listener": "connected" if connected else "absent",
                "set_by": settings.get("set_by"), "ts": settings.get("ts")}

    def _agent_object(self, room: _Room, name: str) -> dict[str, Any]:
        agent = room.agents[name]
        return {"name": name, "harness": agent["harness"], "human": agent["human"],
                "model": agent["model"], "stream": agent["stream"],
                "registered_at": agent["registered_at"], "last_seen": agent["last_seen"],
                "kicked": bool(agent["kicked"]), "role": room.roles.get(name),
                "snapshot": agent.get("snapshot"), "wake": self._wake_object(room, name)}

    @staticmethod
    def _message_public(message: dict[str, Any]) -> dict[str, Any]:
        # Fresh nested objects: a frame stored in a row must keep the state it
        # announced after a later ack or answer mutates the message.
        return {"id": message["id"], "seq": message["seq"], "kind": message["kind"],
                "to": message["to"], "from": dict(message["from"]), "text": message["text"],
                "ts": message["ts"], "state": message["state"],
                "answer": dict(message["answer"]) if message["answer"] else None,
                "wake": dict(message["wake"])}

    def _message_list(self, room: _Room) -> list[dict[str, Any]]:
        newest = sorted(room.messages.values(), key=lambda m: m["seq"])[-MESSAGES_SHOWN:]
        return [self._message_public(m) for m in newest]

    def _snapshot(self, room: _Room) -> dict[str, Any]:
        return {"room": room.code, "name": room.name, "rseq": room.rseq,
                "policy": dict(room.policy),
                "agents": [self._agent_object(room, name) for name in sorted(room.agents)],
                "messages": self._message_list(room)}

    # -- the log ---------------------------------------------------------

    @staticmethod
    def _streams(room: _Room) -> list[_Viewer]:
        out = list(room.viewers)
        for streams in room.agent_streams.values():
            out.extend(streams)
        return out

    def _broadcast(self, room: _Room, frame: dict[str, Any], *,
                   agents: str | None = None) -> None:
        """To every viewer, plus the agent streams of `agents`: a name, or "*" for all."""
        targets = list(room.viewers)
        if agents == "*":
            for streams in room.agent_streams.values():
                targets.extend(streams)
        elif agents is not None:
            targets.extend(room.agent_streams.get(agents, ()))
        for viewer in targets:
            if not viewer.closed:
                viewer.queue.append(frame)
                viewer.cond.notify()

    def _push_agent(self, room: _Room, name: str, frame: dict[str, Any]) -> None:
        """To that agent's streams only; viewers never receive a wake frame (§10.5)."""
        for stream in room.agent_streams.get(name, ()):
            if not stream.closed:
                stream.queue.append(frame)
                stream.cond.notify()

    def _emit_agent(self, room: _Room, name: str) -> None:
        self._broadcast(room, {"t": "agent", "agent": self._agent_object(room, name)}, agents=name)

    def _append(self, room: _Room, t: str, payload: dict[str, Any], *, agent: str | None = None,
                nbytes: int = 0, digest: str | None = None, positional: int = 0,
                agents: str | None = None) -> _Row:
        room.rseq += 1
        frame = {"t": t, "rseq": room.rseq, **payload}
        row = _Row(room.rseq, t, frame, agent=agent, nbytes=nbytes, digest=digest,
                   ts=self.clock(), positional=positional)
        room.log.append(row)
        if t == "batch" and agent is not None:
            room.agent_bytes[agent] = room.agent_bytes.get(agent, 0) + nbytes
            room.total_bytes += nbytes
            if digest:
                room.digests[digest] = row.rseq
        self._broadcast(room, frame, agents=agents)
        return row

    def _message_row(self, room: _Room, message: dict[str, Any], *, nbytes: int) -> _Row:
        """One row per creation and per state or wake change, to viewers and the recipient."""
        return self._append(room, "message", {"message": self._message_public(message)},
                            nbytes=nbytes, agents=message["to"])

    def _delete_rows(self, room: _Room, doomed: set[_Row]) -> None:
        if not doomed:
            return
        room.log = [row for row in room.log if row not in doomed]
        for row in doomed:
            room.horizon = max(room.horizon, row.rseq)
            if row.t == "batch" and row.agent is not None:
                room.agent_bytes[row.agent] = room.agent_bytes.get(row.agent, 0) - row.bytes
                room.total_bytes -= row.bytes
                if row.digest and room.digests.get(row.digest) == row.rseq:
                    del room.digests[row.digest]

    def _enforce_bytes(self, room: _Room, name: str) -> None:
        """1 MiB per agent, then 8 MiB per room, oldest batch rows first (§3.8)."""
        doomed: set[_Row] = set()
        agent_left = room.agent_bytes.get(name, 0)
        total_left = room.total_bytes
        for row in room.log:
            if agent_left <= AGENT_BYTES:
                break
            if row.t == "batch" and row.agent == name:
                doomed.add(row)
                agent_left -= row.bytes
                total_left -= row.bytes
        for row in room.log:
            if total_left <= ROOM_BYTES:
                break
            if row.t == "batch" and row not in doomed:
                doomed.add(row)
                total_left -= row.bytes
        self._delete_rows(room, doomed)

    def _tail(self, room: _Room, name: str) -> list[dict[str, Any]]:
        picked: list[_Row] = []
        events = nbytes = 0
        for row in reversed(room.log):
            if row.t != "batch" or row.agent != name:
                continue
            picked.append(row)
            events += row.positional
            nbytes += row.bytes
            if events >= TAIL_EVENTS or nbytes >= TAIL_BYTES:
                break
        return [row.frame for row in reversed(picked)]

    def _replay(self, room: _Room, after: int) -> tuple[list[_Row], dict[str, int] | None]:
        start = max(after, room.horizon)
        rows = [row for row in room.log if row.rseq > start]
        gap = {"before_rseq": room.horizon + 1} if after < room.horizon else None
        if sum(row.bytes for row in rows) > REPLAY_BYTES:
            kept: list[_Row] = []
            budget = REPLAY_BYTES
            for row in reversed(rows):
                if kept and row.bytes > budget:
                    break
                kept.append(row)
                budget -= row.bytes
            rows = list(reversed(kept))
            gap = {"before_rseq": rows[0].rseq}
        return rows, gap

    def _backfill(self, room: _Room, after: int | None) -> list[dict[str, Any]]:
        snapshot = self._snapshot(room)
        frames: list[dict[str, Any]]
        if after is None:
            frames = [{"t": "hello", "transport": "sse", "backfill": "tail", "gap": None,
                       "room": snapshot}]
            held = list(room.records.values())
            for i in range(0, len(held), RECORDS_PER_FRAME):
                frames.append({"t": "records", "events": held[i:i + RECORDS_PER_FRAME]})
            for name in sorted(room.agents):
                frames.extend(self._tail(room, name))
        else:
            rows, gap = self._replay(room, after)
            frames = [{"t": "hello", "transport": "sse",
                       "backfill": "replay" if rows else "none", "gap": gap, "room": snapshot}]
            frames.extend(row.frame for row in rows)
        frames.append({"t": "live"})
        return frames

    # -- retention -------------------------------------------------------

    def _trim_records(self, room: _Room) -> None:
        surplus = len(room.records) - MAX_RECORDS
        if surplus <= 0:
            return
        ordered = sorted(enumerate(room.records.items()),
                         key=lambda item: (_instant(item[1][1].get("body", {}).get("ts"))
                                           or float("-inf"), item[0]))
        for _, (held_id, ev) in ordered[:surplus]:
            del room.records[held_id]
            rid = ev.get("body", {}).get("rid")
            if isinstance(rid, str) and room.rids.get(rid) == held_id:
                del room.rids[rid]

    def _hold_record(self, room: _Room, ev: dict[str, Any]) -> bool:
        """Hold the record, or report False for an older version of a held rid (§6.4)."""
        rid = ev["body"].get("rid")
        if isinstance(rid, str) and rid in room.rids:
            held = room.records[room.rids[rid]]
            new_ts = _instant(ev["body"].get("ts")) or float("-inf")
            old_ts = _instant(held["body"].get("ts")) or float("-inf")
            if new_ts < old_ts:
                return False
            del room.records[room.rids[rid]]
        room.records[ev["id"]] = ev
        if isinstance(rid, str):
            room.rids[rid] = ev["id"]
        return True

    def _wipe(self, room: _Room) -> None:
        for stream in self._streams(room):
            stream.queue.append({"t": "gone"})
            stream.closed = True
            stream.cond.notify()
        self.rooms.pop(room.code, None)

    def _prune(self, room: _Room, now: float) -> None:
        cutoff = now - room.policy["retention_min"] * 60
        self._delete_rows(room, {row for row in room.log if row.ts < cutoff})
        for name in list(room.agent_bytes):
            self._enforce_bytes(room, name)
        room.tickets = {digest: expires for digest, expires in room.tickets.items()
                        if expires > now}
        # Messages live seven days from their ts whatever their kind or state
        # (§10.1); an ask past ask_ttl_s expires but stays until then.
        for mid in [m["id"] for m in room.messages.values()
                    if now - m["created_at"] >= MESSAGE_TTL_S]:
            del room.messages[mid]
        expiring = sorted((m for m in room.messages.values()
                           if m["kind"] == "ask" and m["state"] == "open"
                           and m["expires_at"] <= now),
                          key=lambda m: m["seq"])
        for message in expiring:
            message["state"] = "expired"
            self._message_row(room, message,
                              nbytes=len(_json_bytes(self._message_public(message))))
        self._trim_records(room)
        if now - room.last_batch_at > IDLE_WIPE_S:
            self._wipe(room)

    def _take_token(self, room: _Room, name: str) -> None:
        now = self.clock()
        bucket = room.buckets.setdefault(name, [BUCKET_BURST, now])
        bucket[0] = min(BUCKET_BURST, bucket[0] + max(0.0, now - bucket[1]) * BUCKET_RATE)
        bucket[1] = now
        if bucket[0] < 1.0:
            wait = max(1, _ceil((1.0 - bucket[0]) / BUCKET_RATE))
            raise _Reply(429, "rate", f"this agent is over 2 batches/s; wait {wait} s and "
                         "resend the same batch -- nothing was consumed",
                         {"Retry-After": str(wait)})
        bucket[0] -= 1.0

    # -- events ----------------------------------------------------------

    def _check_event(self, room: _Room, name: str, line: bytes,
                     level: str) -> tuple[dict[str, Any] | None, str | None, str | None]:
        """(event, id, why): oversize -> schema -> kind -> policy, first failure wins."""
        parsed: Any = None
        try:
            parsed = json.loads(line)
        except ValueError:
            parsed = None
        ident = parsed.get("id") if isinstance(parsed, dict) else None
        if not isinstance(ident, str):
            ident = None
        if len(line) > EVENT_BYTES:
            return None, ident, "oversize"
        if not isinstance(parsed, dict):
            return None, None, "schema"
        parsed["agent"] = name
        if not _envelope_ok(parsed):
            return None, ident, "schema"
        kind = parsed["kind"]
        if kind not in KINDS:
            return None, ident, "kind"
        if _breaks_policy(kind, parsed["body"], level):
            return None, ident, "policy"
        if kind == "answer":
            # Only an open ask addressed to this agent resolves (§10.1); anything
            # else is the client's mistake, reported like a missing field.
            mid = parsed["body"].get("ask")
            message = room.messages.get(mid) if isinstance(mid, str) else None
            if (message is None or message["kind"] != "ask" or message["to"] != name
                    or message["state"] != "open"):
                return None, ident, "schema"
        return parsed, ident, None

    def _post_events(self, room: _Room, name: str, raw: bytes) -> dict[str, Any]:
        lines = [line for line in raw.split(b"\n") if line.strip()]
        if len(raw) > BATCH_BYTES or len(lines) > BATCH_LINES:
            raise _Reply(413, "oversize", "a batch is at most 65,536 bytes and 200 events; "
                         "split it and resend")
        self._take_token(room, name)
        now = self.clock()
        agent = room.agents[name]
        agent["last_seen"] = _iso_ms(now)
        # Digest before any overwrite: two identical bodies must match whatever
        # name their tokens carry, and the body on the wire is the only stable thing.
        digest = hashlib.sha256(raw).hexdigest()
        if not lines:
            return {"rseq": room.rseq, "accepted": 0, "dup": 0, "rejected": []}
        if digest in room.digests:
            return {"rseq": room.digests[digest], "accepted": 0, "dup": len(lines),
                    "rejected": []}
        level = room.policy["max_stream"]
        stored: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        answered: list[dict[str, Any]] = []
        accepted = dup = 0
        for line in lines:
            ev, ident, why = self._check_event(room, name, line, level)
            if why or ev is None:
                rejected.append({"id": ident, "why": why})
                continue
            kind = ev["kind"]
            if kind == "agent":
                agent["snapshot"] = ev["body"]
                accepted += 1
                # One frame per snapshot, carrying the snapshot as of that event
                # and sent ahead of the batch row, as the Worker does: two
                # snapshots in one batch must reach a viewer as two frames.
                self._emit_agent(room, name)
                continue
            if kind == "record":
                # The id is already held, or this is an older version of a held
                # rid: dup either way, and neither rides in the batch row (§6.4).
                if ev["id"] in room.records or not self._hold_record(room, ev):
                    dup += 1
                    continue
            elif kind == "answer":
                message = room.messages[ev["body"]["ask"]]
                message["state"] = "answered"
                message["answer"] = {"text": ev["body"].get("text"), "ts": ev["ts"]}
                answered.append(message)
            stored.append(ev)
            accepted += 1
        self._trim_records(room)
        rseq = room.rseq
        if stored:
            row = self._append(room, "batch", {"agent": name, "events": stored}, agent=name,
                               nbytes=len(raw), digest=digest,
                               positional=sum(1 for ev in stored if ev["kind"] in POSITIONAL))
            rseq = row.rseq
            room.last_batch_at = now
            self._enforce_bytes(room, name)
            for message in sorted(answered, key=lambda m: m["seq"]):
                self._message_row(room, message,
                                  nbytes=len(_json_bytes(self._message_public(message))))
        return {"rseq": rseq, "accepted": accepted, "dup": dup, "rejected": rejected}

    # -- routes ----------------------------------------------------------

    def _handle(self, h: "_Handler", method: str) -> None:
        path, _, query = h.path.partition("?")
        match = _match(method, path)
        if match is None:
            if path.startswith(("/r/", "/rooms", "/healthz")):
                raise _Reply(404, "room", "no such route")
            self._serve_index(h)
            return
        name, params = match
        q = {key: values[0] for key, values in
             urllib.parse.parse_qs(query, keep_blank_values=True).items()}
        auth = h.headers.get("Authorization")
        if name == "healthz":
            h.reply_json(200, {"ok": True, "backend": "python", "transports": ["sse"],
                               "version": "1"})
            return
        if name == "stream":
            self._stream(h, params["room"], q, auth)
            return
        if name == "agent_stream":
            self._agent_stream(h, params["room"], auth)
            return
        route = getattr(self, "_route_" + name)
        with self._lock:
            status, payload, headers = route(h, params, q, auth)
        if payload is None:
            h.reply_empty(status, headers)
        else:
            h.reply_json(status, payload, headers)

    def _serve_index(self, h: "_Handler") -> None:
        try:
            data = self.web.read_bytes()
        except OSError:
            data = PLACEHOLDER_HTML
        h.send_response(200)
        h.send_header("Content-Type", "text/html; charset=utf-8")
        h.send_header("Content-Length", str(len(data)))
        h.end_headers()
        if h.command != "HEAD":
            h.wfile.write(data)

    @staticmethod
    def _body_json(h: "_Handler") -> dict[str, Any]:
        if h.body_error:
            raise h.body_error
        if not h.body.strip():
            return {}
        try:
            body = json.loads(h.body)
        except ValueError:
            raise _Reply(400, "schema", "the body must be a JSON object")
        if not isinstance(body, dict):
            raise _Reply(400, "schema", "the body must be a JSON object")
        return body

    @staticmethod
    def _after(q: dict[str, str]) -> int:
        if "after" not in q:
            return 0
        parsed = _nonneg(q["after"])
        if parsed is None:
            raise _Reply(400, "schema", "after must be a non-negative integer")
        return parsed

    @staticmethod
    def _limit(q: dict[str, str]) -> int:
        if "limit" not in q:
            return 50
        parsed = _nonneg(q["limit"])
        if parsed is None or not 1 <= parsed <= 200:
            raise _Reply(400, "schema", "limit must be an integer from 1 to 200")
        return parsed

    def _route_create_room(self, h: "_Handler", params: dict[str, str], q: dict[str, str],
                           auth: str | None) -> tuple[int, Any, dict[str, str]]:
        body = self._body_json(h)
        name = body.get("name")
        if name is None:
            name = ""
        if not isinstance(name, str):
            raise _Reply(400, "schema", "name must be a string")
        name = _clean(name) or "room"
        if _over(name, 60):
            raise _Reply(400, "schema", "name is at most 60 code points and 240 bytes")
        # Only an absent key means "the defaults": `null`, `0`, `""` and `false`
        # are wrong types, and the Worker answers them 400 (§3.1).
        policy = _validate_policy(body.get("policy", {}), DEFAULT_POLICY)
        code = _symbols(10)
        code = f"{code[:4]}-{code[4:8]}-{code[8:]}"
        while code in self.rooms:
            code = _symbols(10)
            code = f"{code[:4]}-{code[4:8]}-{code[8:]}"
        join = f"{code}.{_symbols(24)}"
        self.rooms[code] = _Room(code, name, _sha(join), policy, self.clock())
        base = self.public_url or f"http://{h.headers.get('Host') or 'localhost'}"
        return 201, {"room": code, "join_code": join, "policy": dict(policy), "relay": base,
                     "url": f"{base}/#{code}"}, {}

    def _route_room_snapshot(self, h, params, q, auth):
        room = self._room(params["room"])
        self._authenticate(room, auth, ("room",), "the room code")
        return 200, self._snapshot(room), {}

    def _route_records(self, h, params, q, auth):
        room = self._room(params["room"])
        self._authenticate(room, auth, ("room",), "the room code")
        return 200, {"rseq": room.rseq, "events": list(room.records.values())}, {}

    def _route_ticket(self, h, params, q, auth):
        room = self._room(params["room"])
        self._authenticate(room, auth, ("room",), "the room code")
        self._body_json(h)
        ticket = "vt-" + _symbols(32)
        ttl = room.policy["ticket_ttl_s"]
        room.tickets[_sha(ticket)] = self.clock() + ttl
        return 201, {"ticket": ticket, "ttl": ttl}, {}

    def _route_events_get(self, h, params, q, auth):
        room = self._room(params["room"])
        self._authenticate(room, auth, ("room",), "the room code")
        after, before = q.get("after"), q.get("before")
        if (after is None) == (before is None):
            raise _Reply(400, "schema", "pass exactly one of after=N or before=N")
        position = _nonneg(after if after is not None else before)
        if position is None:
            raise _Reply(400, "schema", "after and before must be non-negative integers")
        limit = self._limit(q)
        agent = q.get("agent")
        if agent is not None:
            if agent not in room.agents or room.agents[agent]["kicked"]:
                return 200, {"rseq": room.rseq, "frames": [], "more": False}, {}
            rows = [row for row in room.log if row.t == "batch" and row.agent == agent]
        else:
            rows = room.log
        if after is not None:
            matching = [row for row in rows if row.rseq > position]
            page = matching[:limit]
        else:
            matching = [row for row in rows if row.rseq < position]
            page = matching[-limit:]
        return 200, {"rseq": room.rseq, "frames": [row.frame for row in page],
                     "more": len(matching) > limit}, {}

    def _route_register(self, h, params, q, auth):
        room = self._room(params["room"])
        self._authenticate(room, auth, ("join",), "the join code")
        body = self._body_json(h)
        name = params["name"]
        if not NAME_RE.match(name):
            raise _Reply(400, "schema", "an agent name matches [a-z0-9][a-z0-9-]{0,47}")
        fields: dict[str, Any] = {}
        for key in ("harness", "human", "model"):
            value = body.get(key)
            if value is not None and not isinstance(value, str):
                raise _Reply(400, "schema", f"{key} must be a string or null")
            if isinstance(value, str):
                value = _clean(value)
                if _over(value, 120):
                    raise _Reply(400, "schema", f"{key} is at most 120 code points and 480 bytes")
            fields[key] = value
        stream = body.get("stream", "tools")
        if stream not in STREAMS:
            raise _Reply(400, "schema", "stream must be summary, tools or full (off is not a "
                         "level; leave the room instead)")
        existing = room.agents.get(name)
        active = sum(1 for a in room.agents.values() if not a["kicked"])
        if (existing is None or existing["kicked"]) and active >= MAX_AGENTS:
            raise _Reply(503, "full", f"this room already has {MAX_AGENTS} agents; kick one "
                         "with DELETE /r/{room}/agents/{name} or open another room")
        now = self.clock()
        token = "at-" + _symbols(32)
        # The owner token rotates with the agent token (§10.2); the wake settings
        # are the owner's switches and survive a re-registration like the role.
        owner = "ot-" + _symbols(32)
        agent = existing or {"registered_at": _iso_ms(now), "snapshot": None}
        agent.update(fields)
        agent.update({"stream": stream, "token_hash": _sha(token), "owner_hash": _sha(owner),
                      "last_seen": _iso_ms(now), "kicked": False})
        room.agents[name] = agent
        self._emit_agent(room, name)
        effective = STREAMS[min(STREAMS.index(stream), STREAMS.index(room.policy["max_stream"]))]
        return 200, {"token": token, "owner_token": owner, "rseq": room.rseq,
                     "policy": dict(room.policy), "effective_stream": effective}, {}

    def _route_kick(self, h, params, q, auth):
        room = self._room(params["room"])
        cls, own = self._authenticate(room, auth, ("join", "token", "owner"),
                                      "the join code, or the agent's own agent or owner token")
        name = params["name"]
        if cls != "join" and own != name:
            raise _Reply(403, "forbidden", "an agent or owner token may only remove its own name")
        if h.body_error:
            raise h.body_error
        agent = room.agents.get(name)
        if agent is None:
            raise _Reply(404, "agent", "no agent by that name was ever registered here")
        if agent["kicked"]:
            return 204, None, {}
        agent["kicked"] = True
        agent["token_hash"] = None
        agent["owner_hash"] = None
        self._emit_agent(room, name)
        # Its token is dead, so its listener's stream ends too; a reconnect gets 401.
        for stream in room.agent_streams.get(name, ()):
            stream.closed = True
            stream.cond.notify()
        return 204, None, {}

    def _route_post_events(self, h, params, q, auth):
        room = self._room(params["room"])
        _, name = self._authenticate(room, auth, ("token",), "an agent token")
        if h.body_error:
            raise h.body_error
        assert name is not None
        return 202, self._post_events(room, name, h.body), {}

    def _route_inbox(self, h, params, q, auth):
        room = self._room(params["room"])
        _, name = self._authenticate(room, auth, ("token",), "an agent token")
        after = self._after(q)
        room.agents[name]["last_seen"] = _iso_ms(self.clock())
        messages = sorted((m for m in room.messages.values()
                           if m["seq"] > after and m["to"] in (name, "*")),
                          key=lambda m: m["seq"])
        return 200, {"rseq": room.rseq, "role": room.roles.get(name),
                     "messages": [self._message_public(m) for m in messages]}, {}

    def _route_messages_get(self, h, params, q, auth):
        room = self._room(params["room"])
        self._authenticate(room, auth, ("room",), "the room code")
        after = self._after(q)
        limit = self._limit(q)
        matching = sorted((m for m in room.messages.values() if m["seq"] > after),
                          key=lambda m: m["seq"])
        return 200, {"rseq": room.rseq,
                     "messages": [self._message_public(m) for m in matching[:limit]],
                     "more": len(matching) > limit}, {}

    def _wake_decision(self, room: _Room, to: str, requested: bool,
                       sender: dict[str, str]) -> dict[str, Any]:
        """The wake object at creation (§10.1, §10.4); every later change is an ack."""
        if not requested:
            return {"requested": False, "state": "none", "reason": None, "ts": None}
        settings = self._wake_object(room, to)
        if not settings["enabled"]:
            state, reason = "off", None
        elif settings["from"] == "agents" and sender["kind"] != "agent":
            # `from: agents` means only a token-authenticated sender may wake this
            # agent (§10.3); a viewer's request is answered here rather than
            # pushed to a machine that would only decline it.
            state, reason = "declined", "sender not allowed"
        elif settings["listener"] != "connected":
            state, reason = "nobody", None
        elif settings["used_this_hour"] >= settings["max_per_hour"]:
            state, reason = "busy", "hourly cap"
        else:
            state, reason = "pending", None
        return {"requested": True, "state": state, "reason": reason, "ts": None}

    def _route_post_message(self, h, params, q, auth):
        room = self._room(params["room"])
        cls, own = self._authenticate(room, auth, ("room", "token"),
                                      "the room code (with a viewer name) or an agent token")
        body = self._body_json(h)
        to, text = body.get("to"), body.get("text")
        if not isinstance(to, str) or not to:
            raise _Reply(400, "schema", "to must name an agent in the room, or be * for everyone")
        if not isinstance(text, str):
            raise _Reply(400, "schema", "text must be a string")
        text = _clean(text)
        if not text or _over(text, 2_000):
            raise _Reply(400, "schema", "text is 1 to 2,000 code points and at most 8,000 bytes")
        kind = body.get("kind")
        if kind is None:
            kind = "say" if to == "*" else "ask"
        if kind not in MESSAGE_KINDS:
            raise _Reply(400, "schema", "kind must be ask, ping or say")
        wake = body.get("wake", False)
        if not isinstance(wake, bool):
            raise _Reply(400, "schema", "wake must be true or false")
        if cls == "token":
            sender = {"kind": "agent", "name": own}
        else:
            viewer = body.get("viewer")
            if not isinstance(viewer, str):
                raise _Reply(400, "schema", "viewer must be a non-empty string")
            viewer = _clean(viewer)
            if not viewer or _over(viewer, 40):
                raise _Reply(400, "schema", "viewer must be 1 to 40 code points and at most "
                             "160 bytes")
            sender = {"kind": "viewer", "name": viewer}
        if to != "*":
            target = room.agents.get(to)
            if target is None or target["kicked"]:
                raise _Reply(404, "agent", "no such agent in this room, or it was kicked; "
                             "GET /r/{room} lists them")
        now = self.clock()
        if cls == "token":
            stamps = room.agent_message_at.setdefault(own, [])
            stamps[:] = [t for t in stamps if now - t < 60]
            if len(stamps) >= AGENT_MESSAGES_PER_MIN:
                wait = max(1, _ceil(60 - (now - stamps[0])))
                raise _Reply(429, "rate", f"an agent posts at most {AGENT_MESSAGES_PER_MIN} "
                             f"messages a minute; wait {wait} s", {"Retry-After": str(wait)})
        else:
            last = room.viewer_at.get(sender["name"])
            if last is not None and now - last < VIEWER_MESSAGE_GAP_S:
                wait = max(1, _ceil(VIEWER_MESSAGE_GAP_S - (now - last)))
                raise _Reply(429, "rate", f"one message per viewer name every "
                             f"{VIEWER_MESSAGE_GAP_S} s; wait {wait} s", {"Retry-After": str(wait)})
        if kind == "ask" and sum(1 for m in room.messages.values()
                                 if m["kind"] == "ask" and m["state"] == "open") >= MAX_OPEN_ASKS:
            raise _Reply(503, "full", f"{MAX_OPEN_ASKS} asks are already open in this room; "
                         "wait for answers or expiry, or send a ping or a say")
        # Nothing above had a side effect; everything below is the message.
        if cls == "token":
            stamps.append(now)
        else:
            room.viewer_at[sender["name"]] = now
        seq = room.rseq + 1
        message = {"id": f"cm-{room.room4}-{seq}", "seq": seq, "kind": kind, "to": to,
                   "from": sender, "text": text, "ts": _iso_ms(now),
                   "state": "open" if kind == "ask" else "sent", "answer": None,
                   # `wake` is ignored for the whole room (§10.1): stored false.
                   "wake": self._wake_decision(room, to, wake and to != "*", sender),
                   "created_at": now,
                   "expires_at": now + room.policy["ask_ttl_s"] if kind == "ask" else None}
        room.messages[message["id"]] = message
        row = self._message_row(room, message, nbytes=len(h.body))
        if message["wake"]["state"] == "pending":
            # Exactly once, right after the message frame, to the recipient only.
            self._push_agent(room, to, {"t": "wake", "rseq": row.rseq,
                                        "message": self._message_public(message),
                                        "settings": self._wake_object(room, to)})
        return 201, {"id": message["id"], "seq": seq}, {}

    def _route_wake_ack(self, h, params, q, auth):
        room = self._room(params["room"])
        _, name = self._authenticate(room, auth, ("token",), "an agent token")
        body = self._body_json(h)
        mid, result, reason = body.get("message"), body.get("result"), body.get("reason")
        if not isinstance(mid, str):
            raise _Reply(400, "schema", "message must be the id of the message that woke you")
        if result not in WAKE_RESULTS:
            raise _Reply(400, "schema", "result must be woke, busy, declined or off")
        if reason is not None and not isinstance(reason, str):
            raise _Reply(400, "schema", "reason must be a string of at most 200 code points, or null")
        if isinstance(reason, str):
            reason = _clean(reason)
            if _over(reason, 200):
                raise _Reply(400, "schema", "reason is at most 200 code points and 800 bytes")
        message = room.messages.get(mid)
        # pending -> any result; busy -> any result (a second ack may upgrade to
        # woke); everything else is final (§10.4).
        if (message is None or message["to"] != name
                or message["wake"]["state"] not in ("pending", "busy")):
            raise _Reply(400, "schema", "that message is not addressed to you or is not awaiting "
                         "a wake-ack (its wake.state must be pending or busy)")
        now = self.clock()
        message["wake"].update({"state": result, "reason": reason, "ts": _iso_ms(now)})
        if result == "woke":
            self._count_wake(room.agents[name])
        self._message_row(room, message, nbytes=len(h.body))
        if result == "woke":
            # used_this_hour changed, and that lives on the agent object.
            self._emit_agent(room, name)
        return 200, {"message": self._message_public(message)}, {}

    def _route_wake_settings(self, h, params, q, auth):
        room = self._room(params["room"])
        cls, own = self._authenticate(room, auth, ("token", "owner"),
                                      "this agent's owner token or agent token")
        name = params["name"]
        if own != name:
            raise _Reply(403, "forbidden", "an owner or agent token flips only its own agent's "
                         "wake switches")
        body = self._body_json(h)
        if not any(key in body for key in WAKE_KEYS):
            raise _Reply(400, "schema", "send at least one of " + ", ".join(WAKE_KEYS))
        settings = dict(room.agents[name].get("wake") or {})
        if "enabled" in body:
            if not isinstance(body["enabled"], bool):
                raise _Reply(400, "schema", "enabled must be true or false")
            settings["enabled"] = body["enabled"]
        if "from" in body:
            if body["from"] not in WAKE_FROM:
                raise _Reply(400, "schema", "from must be agents or room (there is no anyone; "
                             "the room code is the outer wall)")
            settings["from"] = body["from"]
        if "max_per_hour" in body:
            value = body["max_per_hour"]
            if not _is_int(value) or not 1 <= value <= 60:
                raise _Reply(400, "schema", "max_per_hour must be an integer from 1 to 60")
            settings["max_per_hour"] = int(value)
        settings["set_by"] = "owner" if cls == "owner" else "agent"
        settings["ts"] = _iso_ms(self.clock())
        room.agents[name]["wake"] = settings
        wake = self._wake_object(room, name)
        self._emit_agent(room, name)
        # The listener applies the last wake frame it received to its own config
        # (§10.3); a settings change carries no message.
        self._push_agent(room, name, {"t": "wake", "message": None, "settings": wake})
        return 200, {"wake": wake}, {}

    def _route_role(self, h, params, q, auth):
        room = self._room(params["room"])
        cls, own = self._authenticate(room, auth, ("room", "token", "owner"),
                                      "the room code, or the agent's own agent or owner token")
        name = params["name"]
        if cls != "room" and own != name:
            raise _Reply(403, "forbidden", "an agent or owner token may only set its own role")
        body = self._body_json(h)
        role = body.get("role")
        if role is not None and not isinstance(role, str):
            raise _Reply(400, "schema", "role must be a string, an empty string or null")
        role = _clean(role) if isinstance(role, str) else ""
        if _over(role, 60):
            raise _Reply(400, "schema", "role is at most 60 code points and 240 bytes")
        if cls != "room":
            viewer = "owner"
        else:
            viewer = body.get("viewer")
            if not isinstance(viewer, str):
                raise _Reply(400, "schema", "viewer must be a non-empty string")
            viewer = _clean(viewer)
            if not viewer or _over(viewer, 40):
                raise _Reply(400, "schema", "viewer must be 1 to 40 code points and at most "
                             "160 bytes")
        agent = room.agents.get(name)
        if agent is None or agent["kicked"]:
            raise _Reply(404, "agent", "no such agent in this room, or it was kicked")
        current = room.roles.get(name)
        if not role and current is None:
            return 200, {"set_seq": room.role_seq.get(name, 0)}, {}
        if current is not None and role == current["role"]:
            return 200, {"set_seq": current["set_seq"]}, {}
        now = self.clock()
        last = room.role_at.get(name)
        if last is not None and now - last < ROLE_GAP_S:
            wait = max(1, _ceil(ROLE_GAP_S - (now - last)))
            raise _Reply(429, "rate", f"one role change per agent every {ROLE_GAP_S} s; "
                         f"wait {wait} s", {"Retry-After": str(wait)})
        room.role_at[name] = now
        seq = room.rseq + 1
        new_role = ({"role": role, "viewer": viewer, "set_seq": seq, "ts": _iso_ms(now)}
                    if role else None)
        room.roles[name] = new_role
        room.role_seq[name] = seq
        self._append(room, "role", {"agent": name, "role": new_role}, nbytes=len(h.body),
                     agents=name)
        return 200, {"set_seq": seq}, {}

    def _route_policy(self, h, params, q, auth):
        room = self._room(params["room"])
        self._authenticate(room, auth, ("join",), "the join code")
        body = self._body_json(h)
        if not any(key in body for key in POLICY_KEYS):
            raise _Reply(400, "schema", "send at least one of " + ", ".join(POLICY_KEYS))
        room.policy = _validate_policy(body, room.policy)
        self._append(room, "policy", {"policy": dict(room.policy)}, nbytes=len(h.body),
                     agents="*")
        return 200, {"policy": dict(room.policy)}, {}

    def _route_delete_room(self, h, params, q, auth):
        room = self._room(params["room"])
        self._authenticate(room, auth, ("join",), "the join code")
        self._wipe(room)
        return 204, None, {}

    def _route_prune(self, h, params, q, auth):
        room = self._room(params["room"])
        self._authenticate(room, auth, ("join",), "the join code")
        self._body_json(h)
        self._prune(room, self.clock())
        return 204, None, {}

    # -- SSE -------------------------------------------------------------

    @staticmethod
    def _sse_headers(h: "_Handler") -> None:
        h.send_response(200)
        h.send_header("Content-Type", "text/event-stream; charset=utf-8")
        h.send_header("Cache-Control", "no-cache")
        h.send_header("X-Accel-Buffering", "no")
        h.end_headers()

    def _watch_eof(self, conn: socket.socket, stream: _Viewer) -> None:
        """Mark the stream closed the moment the peer hangs up.

        The writer only learns of a dead socket when it writes, which on an idle
        stream is the next keepalive up to 15 s away, and often the write after
        that; `listener` (§10.3) and the viewer cap both need the slot back now.
        Reads go to the raw socket because the buffered rfile refuses every read
        after one timeout, and the handler's 30 s socket timeout would trip it.
        """
        while True:
            try:
                data = conn.recv(1)
            except socket.timeout:
                continue
            except (OSError, ValueError):
                data = b""
            if not data:
                break
        with self._lock:
            if not stream.closed:
                stream.closed = True
                stream.cond.notify()

    def _pump(self, h: "_Handler", stream: _Viewer) -> None:
        """Write the queue as SSE frames until either side closes the connection."""
        threading.Thread(target=self._watch_eof, args=(h.connection, stream),
                         name="canvas-relay-eof", daemon=True).start()
        while True:
            with self._lock:
                if not stream.queue and not stream.closed:
                    stream.cond.wait(KEEPALIVE_S)
                frames = list(stream.queue)
                stream.queue.clear()
                closed = stream.closed
            if frames:
                h.wfile.write(b"".join(_sse(frame) for frame in frames))
            elif not closed:
                h.wfile.write(b": keepalive\n\n")
            if closed:
                return

    def _stream(self, h: "_Handler", code: str, q: dict[str, str], auth: str | None) -> None:
        viewer: _Viewer | None = None
        with self._lock:
            room = self._room(code)
            ticket = q.get("ticket")
            if ticket is not None:
                # The query form is the browser's; when present it is the credential,
                # and a header beside it is not consulted (§3.4).
                if not self._ticket_valid(room, ticket):
                    raise _Reply(401, "auth", "unknown or expired viewer ticket; mint another "
                                 "with POST /r/{room}/ticket")
            else:
                self._authenticate(room, auth, ("room",), "a viewer ticket (?ticket=) or the "
                                   "room code")
            after = _nonneg(h.headers.get("Last-Event-ID"))
            if after is None and "after" in q:
                after = _nonneg(q["after"])
                if after is None:
                    raise _Reply(400, "schema", "after must be a non-negative integer")
            if len(room.viewers) < MAX_VIEWERS:
                viewer = _Viewer(threading.Condition(self._lock))
                # Registered before the snapshot is read (§6.6): with the whole
                # backfill chosen inside this critical section nothing can land
                # both in the tail and in the live queue.
                room.viewers.append(viewer)
                viewer.queue.extend(self._backfill(room, after))
        self._sse_headers(h)
        if viewer is None:
            h.wfile.write(_sse({"t": "full"}))
            return
        try:
            self._pump(h, viewer)
        finally:
            with self._lock:
                with contextlib.suppress(ValueError):
                    room.viewers.remove(viewer)

    def _agent_stream(self, h: "_Handler", code: str, auth: str | None) -> None:
        """§10.5: the listener's stream -- hello, then only what concerns this agent."""
        stream: _Viewer | None = None
        with self._lock:
            room = self._room(code)
            # Header only: there is no query form for this stream, so a ?ticket=
            # beside no header is simply no credential.
            _, name = self._authenticate(room, auth, ("token",), "an agent token")
            assert name is not None
            streams = room.agent_streams.setdefault(name, [])
            if sum(1 for s in streams if not s.closed) < MAX_AGENT_STREAMS:
                stream = _Viewer(threading.Condition(self._lock))
                streams.append(stream)
                stream.queue.append({"t": "hello", "transport": "sse",
                                     "agent": self._agent_object(room, name),
                                     "policy": dict(room.policy)})
                if sum(1 for s in streams if not s.closed) == 1:
                    # The first open socket flips `listener` (§10.3); this stream
                    # sees the frame too, after its hello.
                    self._emit_agent(room, name)
        self._sse_headers(h)
        if stream is None:
            h.wfile.write(_sse({"t": "full"}))
            return
        try:
            self._pump(h, stream)
        finally:
            with self._lock:
                with contextlib.suppress(ValueError):
                    room.agent_streams.get(name, []).remove(stream)
                if (self.rooms.get(code) is room and name in room.agents
                        and not any(not s.closed for s in room.agent_streams.get(name, ()))):
                    self._emit_agent(room, name)       # the last socket closed: absent


# ---------------------------------------------------------------- HTTP

_ROUTES: list[tuple[str, re.Pattern[str], str]] = [
    ("POST", re.compile(r"^/rooms$"), "create_room"),
    ("GET", re.compile(r"^/healthz$"), "healthz"),
    ("GET", re.compile(r"^/r/(?P<room>[^/]+)$"), "room_snapshot"),
    ("DELETE", re.compile(r"^/r/(?P<room>[^/]+)$"), "delete_room"),
    ("POST", re.compile(r"^/r/(?P<room>[^/]+)/ticket$"), "ticket"),
    ("GET", re.compile(r"^/r/(?P<room>[^/]+)/stream$"), "stream"),
    ("GET", re.compile(r"^/r/(?P<room>[^/]+)/agent-stream$"), "agent_stream"),
    ("GET", re.compile(r"^/r/(?P<room>[^/]+)/events$"), "events_get"),
    ("POST", re.compile(r"^/r/(?P<room>[^/]+)/events$"), "post_events"),
    ("POST", re.compile(r"^/r/(?P<room>[^/]+)/agents/(?P<name>[^/]+)$"), "register"),
    ("DELETE", re.compile(r"^/r/(?P<room>[^/]+)/agents/(?P<name>[^/]+)$"), "kick"),
    ("PUT", re.compile(r"^/r/(?P<room>[^/]+)/agents/(?P<name>[^/]+)/wake$"), "wake_settings"),
    ("GET", re.compile(r"^/r/(?P<room>[^/]+)/inbox$"), "inbox"),
    ("POST", re.compile(r"^/r/(?P<room>[^/]+)/messages$"), "post_message"),
    ("GET", re.compile(r"^/r/(?P<room>[^/]+)/messages$"), "messages_get"),
    ("POST", re.compile(r"^/r/(?P<room>[^/]+)/wake-ack$"), "wake_ack"),
    ("PUT", re.compile(r"^/r/(?P<room>[^/]+)/roles/(?P<name>[^/]+)$"), "role"),
    ("PUT", re.compile(r"^/r/(?P<room>[^/]+)/policy$"), "policy"),
    ("POST", re.compile(r"^/r/(?P<room>[^/]+)/prune$"), "prune"),
    ("GET", re.compile(r"^/r/(?P<room>[^/]+)/records$"), "records"),
]


def _match(method: str, path: str) -> tuple[str, dict[str, str]] | None:
    for route_method, pattern, name in _ROUTES:
        found = pattern.match(path)
        if found and route_method == method:
            return name, found.groupdict()
    return None


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64
    relay: Relay

    def handle_error(self, request, client_address) -> None:
        # A viewer that closed its tab mid-write is the normal case, not a traceback.
        if os.environ.get("AGENTCOLAB_DEBUG"):
            super().handle_error(request, client_address)


class _Handler(BaseHTTPRequestHandler):
    server_version = "AgentColabCanvas/1"
    sys_version = ""
    # HTTP/1.0 on purpose: every response closes the connection, so nothing here
    # has to get keep-alive right around a streaming response or an unread body.
    protocol_version = "HTTP/1.0"
    # Bounds a client that connects and never speaks, and a viewer that stops
    # reading; the SSE loop itself waits on a condition, not on the socket.
    timeout = 30
    body: bytes = b""
    body_error: _Reply | None = None

    def log_message(self, fmt: str, *args: Any) -> None:
        if os.environ.get("AGENTCOLAB_DEBUG"):
            sys.stderr.write(f"canvas relay: {self.address_string()} {fmt % args}\n")

    def do_GET(self) -> None:
        self._serve("GET")

    def do_POST(self) -> None:
        self._serve("POST")

    def do_PUT(self) -> None:
        self._serve("PUT")

    def do_DELETE(self) -> None:
        self._serve("DELETE")

    def do_HEAD(self) -> None:
        self._serve("HEAD")

    def do_OPTIONS(self) -> None:
        self._serve("OPTIONS")

    def do_PATCH(self) -> None:
        self._serve("PATCH")

    def _serve(self, method: str) -> None:
        try:
            self.body, self.body_error = self._read_body()
            self.server.relay._handle(self, method)
        except _Reply as reply:
            self.reply_error(reply)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        except Exception as exc:       # a bug in a route must not take the thread down silently
            with contextlib.suppress(Exception):
                self.reply_error(_Reply(500, "internal", f"relay bug ({exc!r}); report it "
                                        "with the request that caused it"))

    def _read_body(self) -> tuple[bytes, _Reply | None]:
        """Read the body eagerly, Content-Length only, so a refused request still
        drains its bytes and the client sees the status instead of a reset."""
        length = self.headers.get("Content-Length")
        if length is None:
            if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
                self._drain_chunked()
                return b"", _Reply(400, "schema", "send Content-Length; this relay does not "
                                   "read chunked request bodies")
            return b"", None
        try:
            n = int(length)
        except ValueError:
            n = -1
        if n < 0:
            return b"", _Reply(400, "schema", "Content-Length must be a non-negative integer")
        if n > READ_CAP:
            return b"", _Reply(413, "oversize", f"the body is {n} bytes; a batch is at most "
                               "65,536")
        return self.rfile.read(n), None

    def _drain_chunked(self) -> None:
        total = 0
        while total <= READ_CAP:
            size_line = self.rfile.readline(1024)
            try:
                size = int(size_line.split(b";")[0].strip() or b"0", 16)
            except ValueError:
                return
            if size == 0:
                self.rfile.readline(1024)
                return
            self.rfile.read(size + 2)
            total += size

    def reply_json(self, status: int, payload: Any, headers: dict[str, str] | None = None) -> None:
        data = _json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def reply_empty(self, status: int, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()

    def reply_error(self, reply: _Reply) -> None:
        self.reply_json(reply.status, {"error": reply.code, "hint": reply.hint}, reply.headers)


# ---------------------------------------------------------------- entry point


def main(argv: Iterable[str] | None = None) -> int:
    records.force_utf8()
    parser = argparse.ArgumentParser(
        prog="colab canvas serve",
        description="Run the canvas relay on a host you own. State is in memory unless "
                    "--state names a directory.")
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address (default 127.0.0.1; 0.0.0.0 exposes it)")
    parser.add_argument("--port", type=int, default=8765, help="0 picks a free port")
    parser.add_argument("--port-file", help="write the bound port here after binding")
    parser.add_argument("--public-url",
                        help="what POST /rooms reports as the relay URL (default: the Host header)")
    parser.add_argument("--web", help="the index.html to serve (default canvas/web/index.html)")
    parser.add_argument("--state", help="directory for rooms.json, dumped every 60 s and on exit")
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        relay = Relay((args.host, args.port), web=args.web, public_url=args.public_url,
                      state_dir=args.state)
    except OSError as exc:
        records.eprint(f"canvas relay: cannot bind {args.host}:{args.port} ({exc}); pick another "
                       "--port, or --port 0 for a free one")
        return 1
    if args.port_file:
        Path(args.port_file).write_text(str(relay.port), encoding="utf-8")
    print(f"canvas relay listening on http://{args.host}:{relay.port}", flush=True)
    try:
        relay.serve_forever()
    except KeyboardInterrupt:
        relay.shutdown()
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
