#!/usr/bin/env python3
"""Live probe of a running canvas relay, WebSocket or SSE.

The contract suite runs in-thread against the Python relay; the hosted Worker
cannot be imported, so this script drives whatever `CANVAS_RELAY` points at
over real sockets: create a room, register, post, stream, ask, answer, wake
settings, an agent stream, a ping that wakes, its ack, prune, delete. It is
opt-in, stdlib only, and prints one line per check so a red run
says which promise the relay broke. The WebSocket client is the smallest RFC
6455 subset that reads a text frame and answers a ping, because the standard
library has none and a dependency is not on the table.
"""

from __future__ import annotations

import base64
import json
import os
import re
import socket
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

RELAY = os.environ.get("CANVAS_RELAY", "http://127.0.0.1:8787").rstrip("/")
UA = "AgentColab/1 (+https://github.com/AgentColab/AgentColab)"
TIMEOUT = float(os.environ.get("CANVAS_LIVE_TIMEOUT", "8"))
AGENT = "probe-agent"
PEER = "probe-peer"


class Failed(AssertionError):
    pass


def check(cond: bool, what: str, detail: object = "") -> None:
    if cond:
        print(f"ok   {what}")
        return
    print(f"FAIL {what}: {detail}")
    raise Failed(what)


# ------------------------------------------------------------------ HTTP

def call(method: str, path: str, body=None, auth: str = None, raw: bytes = None):
    """Returns (status, headers, parsed body or None); errors are not raised."""
    data = raw if raw is not None else (None if body is None else json.dumps(body).encode("utf-8"))
    req = urllib.request.Request(RELAY + path, data=data, method=method)
    req.add_header("User-Agent", UA)
    if auth:
        req.add_header("Authorization", "Bearer " + auth)
    if data is not None:
        req.add_header("Content-Type", "application/x-ndjson" if raw is not None else "application/json")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            text = resp.read().decode("utf-8")
            status, headers = resp.status, dict(resp.headers)
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "replace")
        status, headers = e.code, dict(e.headers)
    parsed = None
    if text.strip():
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = text
    return status, headers, parsed


# ------------------------------------------------------------------ WebSocket

class WebSocket:
    """Text frames only, client-side masking, ping answered, close honoured."""

    def __init__(self, url: str, headers=None):
        u = urllib.parse.urlsplit(url)
        port = u.port or (443 if u.scheme in ("wss", "https") else 80)
        self.sock = socket.create_connection((u.hostname, port), TIMEOUT)
        if u.scheme in ("wss", "https"):
            self.sock = ssl.create_default_context().wrap_socket(self.sock, server_hostname=u.hostname)
        self.sock.settimeout(TIMEOUT)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        target = u.path + ("?" + u.query if u.query else "")
        request = (
            f"GET {target} HTTP/1.1\r\nHost: {u.hostname}:{port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
            f"User-Agent: {UA}\r\n"
        )
        for name, value in (headers or {}).items():
            request += f"{name}: {value}\r\n"
        request += "\r\n"
        self.sock.sendall(request.encode("ascii"))
        self.buf = b""
        while b"\r\n\r\n" not in self.buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise Failed("connection closed during the WebSocket handshake")
            self.buf += chunk
        head, self.buf = self.buf.split(b"\r\n\r\n", 1)
        self.status = int(head.split(b" ", 2)[1])
        if self.status != 101:
            raise Failed(f"WebSocket upgrade answered {self.status}: {head[:200]!r}")

    def _read(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise EOFError("socket closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _send(self, opcode: int, payload: bytes) -> None:
        # Clients must mask (RFC 6455 §5.3); the key is random so proxies
        # cannot see a repeating plaintext pattern.
        head = bytes([0x80 | opcode])
        n = len(payload)
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + n.to_bytes(2, "big")
        else:
            head += bytes([0x80 | 127]) + n.to_bytes(8, "big")
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(head + mask + masked)

    def send_text(self, text: str) -> None:
        self._send(0x1, text.encode("utf-8"))

    def recv_text(self) -> str:
        """Returns the next text message; raises EOFError on a close frame."""
        message = b""
        while True:
            b0, b1 = self._read(2)
            opcode, fin = b0 & 0x0F, b0 & 0x80
            n = b1 & 0x7F
            if n == 126:
                n = int.from_bytes(self._read(2), "big")
            elif n == 127:
                n = int.from_bytes(self._read(8), "big")
            mask = self._read(4) if b1 & 0x80 else None
            payload = self._read(n)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x9:
                self._send(0xA, payload)
                continue
            if opcode == 0x8:
                raise EOFError("close frame")
            if opcode in (0xA, 0x2):
                continue
            message += payload
            if fin:
                return message.decode("utf-8")

    def close(self) -> None:
        try:
            self._send(0x8, (1000).to_bytes(2, "big"))
        except OSError:
            pass
        self.sock.close()


# ------------------------------------------------------------------ stream

class Stream:
    """One streaming connection: WebSocket when advertised, else SSE.

    `target` is the path with its query; `headers` carries the Authorization
    of an agent stream (a viewer stream authenticates with `?ticket=`)."""

    def __init__(self, target: str, transports, headers=None):
        self.ws = None
        self.resp = None
        if "ws" in transports:
            scheme = "wss" if RELAY.startswith("https") else "ws"
            self.ws = WebSocket(f"{scheme}://{RELAY.split('://', 1)[1]}{target}", headers)
        else:
            req = urllib.request.Request(RELAY + target)
            req.add_header("User-Agent", UA)
            for name, value in (headers or {}).items():
                req.add_header(name, value)
            self.resp = urllib.request.urlopen(req, timeout=TIMEOUT)

    def frames_until(self, kind: str, limit: int = 20) -> list:
        """Frames up to and including the first of kind `kind`."""
        out = []
        for _ in range(limit):
            frame = self.next_frame()
            out.append(frame)
            if frame.get("t") in (kind, "<closed>"):
                break
        return out

    def next_raw(self) -> str:
        """Next message; the empty string once the relay closed the stream."""
        if self.ws:
            try:
                return self.ws.recv_text()
            except EOFError:
                return ""
        data = []
        while True:
            line = self.resp.readline()
            if not line:
                return ""
            line = line.decode("utf-8").rstrip("\r\n")
            if line.startswith("data:"):
                data.append(line[5:].lstrip())
            elif line == "" and data:
                return "\n".join(data)

    def next_frame(self) -> dict:
        raw = self.next_raw()
        if raw == "":
            return {"t": "<closed>"}
        return json.loads(raw)

    def close(self) -> None:
        if self.ws:
            self.ws.close()
        elif self.resp:
            self.resp.close()


# ------------------------------------------------------------------ probe

def event(n: int, kind: str = "text", body=None, ref=None) -> dict:
    return {
        "v": 1, "id": f"ev-{n:016x}", "agent": "not-the-token-name", "session": "probe-session",
        "lane": "main", "epoch": 0, "seq": n * 256, "ts": "2026-09-04T10:00:00.000Z",
        "kind": kind, "harness": "probe", "model": None, "ref": ref,
        "body": body if body is not None else {"text": f"probe line {n}", "final": True},
    }


def ndjson(events) -> bytes:
    return ("\n".join(json.dumps(e) for e in events) + "\n").encode("utf-8")


def main() -> int:
    print(f"relay {RELAY}")
    status, _, health = call("GET", "/healthz")
    check(status == 200 and health.get("ok") is True, "healthz answers ok", (status, health))
    transports = health.get("transports", [])
    check(bool(transports), "healthz advertises a transport", health)
    print(f"     backend={health.get('backend')} transports={transports}")

    status, _, created = call("POST", "/rooms", {"name": "probe"})
    check(status == 201 and re.fullmatch(r"[23456789abcdefghjkmnpqrstvwxyz]{4}-[23456789abcdefghjkmnpqrstvwxyz]{4}-[23456789abcdefghjkmnpqrstvwxyz]{2}", created.get("room", "")) is not None,
          "POST /rooms creates a room", (status, created))
    room, join = created["room"], created["join_code"]
    check(join.startswith(room + "."), "join code is prefixed by the room code", join)
    deleted = False
    stream = None
    try:
        status, _, reg = call("POST", f"/r/{room}/agents/{AGENT}",
                              {"harness": "probe", "human": "probe", "stream": "full"}, auth=join)
        check(status == 200 and reg.get("token", "").startswith("at-"), "register mints an agent token", (status, reg))
        check(reg.get("owner_token", "").startswith("ot-"), "register mints an owner token (v1.3)", reg)
        check(reg.get("effective_stream") == "tools", "effective_stream is min(requested, policy)", reg)
        token, owner = reg["token"], reg["owner_token"]

        batch1 = ndjson([event(1), event(2)])
        status, _, posted = call("POST", f"/r/{room}/events", raw=batch1, auth=token)
        check(status == 202 and posted == {"rseq": 1, "accepted": 2, "dup": 0, "rejected": []},
              "first batch is row 1 with 2 accepted", (status, posted))
        status, _, again = call("POST", f"/r/{room}/events", raw=batch1, auth=token)
        check(status == 202 and again == {"rseq": 1, "accepted": 0, "dup": 2, "rejected": []},
              "identical batch is a dup pointing at row 1", (status, again))
        status, _, _ = call("POST", f"/r/{room}/events", raw=batch1, auth=room)
        check(status == 403, "room code cannot post events", status)

        status, _, minted = call("POST", f"/r/{room}/ticket", {}, auth=room)
        check(status == 201 and minted.get("ticket", "").startswith("vt-"), "ticket minted", (status, minted))

        stream = Stream(f"/r/{room}/stream?ticket={minted['ticket']}", transports)
        hello = stream.next_frame()
        check(hello.get("t") == "hello" and hello.get("backfill") == "tail" and hello.get("gap") is None,
              "first frame is hello with backfill tail", hello)
        check(hello["room"]["rseq"] == 1 and [a["name"] for a in hello["room"]["agents"]] == [AGENT],
              "hello.room carries rseq 1 and the agent", hello["room"])
        check(hello["room"].get("messages") == [] and "asks" not in hello["room"],
              "hello.room carries messages, not asks (v1.3)", hello["room"])
        check(hello["room"]["agents"][0]["wake"] == {
            "enabled": False, "from": "agents", "max_per_hour": 4, "used_this_hour": 0,
            "listener": "absent", "set_by": None, "ts": None}, "agent object carries default wake settings",
            hello["room"]["agents"][0])
        tail = stream.next_frame()
        check(tail.get("t") == "batch" and tail.get("rseq") == 1 and len(tail["events"]) == 2,
              "tail replays batch row 1", tail)
        check(all(e["agent"] == AGENT for e in tail["events"]), "agent overwritten from the token", tail["events"])
        sent = [dict(event(1), agent=AGENT), dict(event(2), agent=AGENT)]
        check(tail["events"] == sent, "stored events deep-equal the sent ones", tail["events"])
        live = stream.next_frame()
        check(live.get("t") == "live", "live follows the tail", live)

        if stream.ws:
            stream.ws.send_text("ping")
            check(stream.next_raw() == "pong", "ping is answered with pong")

        status, _, posted = call("POST", f"/r/{room}/events", raw=ndjson([event(3)]), auth=token)
        check(status == 202 and posted["rseq"] == 2, "second batch is row 2", posted)
        frame = stream.next_frame()
        check(frame.get("t") == "batch" and frame.get("rseq") == 2, "second batch arrives live", frame)

        status, _, inbox = call("GET", f"/r/{room}/inbox?after=0", auth=token)
        check(status == 200 and inbox == {"rseq": 2, "role": None, "messages": []}, "inbox is empty with no role", (status, inbox))
        status, _, body = call("GET", f"/r/{room}/inbox?after=0", auth=owner)
        check(status == 403 and body.get("error") == "forbidden", "owner token cannot read the inbox", (status, body))
        status, _, body = call("POST", f"/r/{room}/events", raw=ndjson([event(9)]), auth=owner)
        check(status == 403 and body.get("error") == "forbidden", "owner token cannot post events", (status, body))

        status, _, role = call("PUT", f"/r/{room}/roles/{AGENT}", {"role": "reviewer", "viewer": "probe"}, auth=room)
        check(status == 200 and role == {"set_seq": 3}, "role set is row 3", (status, role))
        frame = stream.next_frame()
        check(frame.get("t") == "role" and frame.get("rseq") == 3 and frame["role"]["set_seq"] == 3
              and frame["role"]["role"] == "reviewer", "role frame arrives live", frame)
        status, _, same = call("PUT", f"/r/{room}/roles/{AGENT}", {"role": "reviewer", "viewer": "probe"}, auth=room)
        check(status == 200 and same == {"set_seq": 3}, "unchanged role is a no-op", (status, same))

        status, _, body = call("POST", f"/r/{room}/asks", {"to": AGENT, "text": "probe?", "viewer": "probe"}, auth=room)
        check(status == 404 and body.get("hint") == "no such route", "POST /asks is gone (v1.3)", (status, body))
        status, _, body = call("POST", f"/r/{room}/messages", {"to": "nobody-here", "text": "?", "viewer": "probe"}, auth=room)
        check(status == 404 and body.get("error") == "agent", "message to an unknown agent is 404 agent", (status, body))
        status, _, ask = call("POST", f"/r/{room}/messages", {"to": AGENT, "text": "probe?", "viewer": "probe"}, auth=room)
        check(status == 201 and ask == {"id": f"cm-{room[:4]}-4", "seq": 4}, "viewer message is row 4 with a room-prefixed id", (status, ask))
        frame = stream.next_frame()
        check(frame.get("t") == "message" and frame.get("rseq") == 4, "message frame arrives live", frame)
        msg = frame.get("message", {})
        check(msg.get("kind") == "ask" and msg.get("state") == "open" and msg.get("from") == {"kind": "viewer", "name": "probe"}
              and msg.get("wake") == {"requested": False, "state": "none", "reason": None, "ts": None},
              "a viewer's message to an agent defaults to kind ask, wake none", msg)
        status, _, inbox = call("GET", f"/r/{room}/inbox?after=2", auth=token)
        check(status == 200 and inbox["rseq"] == 4 and [m["id"] for m in inbox["messages"]] == [ask["id"]]
              and inbox["role"]["set_seq"] == 3, "inbox carries the message and the role", inbox)

        answer = event(4, "answer", {"ask": ask["id"], "text": "yes"}, ref=ask["id"])
        status, _, posted = call("POST", f"/r/{room}/events", raw=ndjson([answer]), auth=token)
        check(status == 202 and posted["rseq"] == 5 and posted["accepted"] == 1, "answer batch is row 5", posted)
        frame = stream.next_frame()
        check(frame.get("t") == "batch" and frame.get("rseq") == 5, "answer batch arrives live", frame)
        frame = stream.next_frame()
        check(frame.get("t") == "message" and frame.get("rseq") == 6 and frame["message"]["state"] == "answered"
              and frame["message"]["seq"] == 4 and frame["message"]["answer"]["text"] == "yes",
              "answered message is row 6 keeping seq 4", frame)
        again = event(5, "answer", {"ask": ask["id"], "text": "again"}, ref=ask["id"])
        status, _, posted = call("POST", f"/r/{room}/events", raw=ndjson([again]), auth=token)
        check(status == 202 and posted["rseq"] == 6 and posted["rejected"] == [{"id": again["id"], "why": "schema"}],
              "a second answer to a closed ask is rejected as schema", posted)
        status, _, inbox = call("GET", f"/r/{room}/inbox?after=2", auth=token)
        check(status == 200 and [m["state"] for m in inbox["messages"]] == ["answered"],
              "inbox still lists the message, now answered", inbox)

        status, _, snap = call("GET", f"/r/{room}", auth=room)
        check(status == 200 and snap["rseq"] == 6 and snap["messages"][0]["state"] == "answered", "snapshot is current", snap)
        status, _, hist = call("GET", f"/r/{room}/events?after=0", auth=room)
        check(status == 200 and [f["rseq"] for f in hist["frames"]] == [1, 2, 3, 4, 5, 6] and hist["more"] is False,
              "GET /events returns rows 1..6", hist)

        # -- v1.3: owner token, wake settings, the agent stream, a ping that wakes
        status, _, reg2 = call("POST", f"/r/{room}/agents/{PEER}", {"harness": "probe"}, auth=join)
        check(status == 200, "a second agent registers", (status, reg2))
        peer = reg2["token"]
        frame = stream.next_frame()
        check(frame.get("t") == "agent" and frame["agent"]["name"] == PEER, "viewer sees the peer's agent frame", frame)

        wake_path = f"/r/{room}/agents/{AGENT}/wake"
        status, _, body = call("PUT", wake_path, {"enabled": True}, auth=room)
        check(status == 403 and body.get("error") == "forbidden", "room code cannot change wake settings", (status, body))
        status, _, body = call("PUT", wake_path, {"enabled": True}, auth=peer)
        check(status == 403 and body.get("error") == "forbidden", "another agent's token cannot change wake settings", (status, body))
        status, _, body = call("PUT", wake_path, {}, auth=owner)
        check(status == 400 and body.get("error") == "schema", "an empty wake body is 400 schema", (status, body))
        status, _, body = call("PUT", wake_path, {"enabled": True, "from": "room"}, auth=owner)
        check(status == 200 and body.get("wake", {}).get("enabled") is True and body["wake"]["from"] == "room"
              and body["wake"]["set_by"] == "owner" and body["wake"]["listener"] == "absent",
              "owner token enables wake from the room", (status, body))
        frame = stream.next_frame()
        check(frame.get("t") == "agent" and frame["agent"]["name"] == AGENT and frame["agent"]["wake"]["enabled"] is True,
              "viewer sees the agent frame with wake enabled", frame)

        status, _, ping = call("POST", f"/r/{room}/messages",
                               {"to": AGENT, "kind": "ping", "text": "anyone home?", "viewer": "sam", "wake": True}, auth=room)
        check(status == 201 and ping["seq"] == 7, "viewer ping with wake is row 7", (status, ping))
        frame = stream.next_frame()
        check(frame.get("t") == "message" and frame["message"]["wake"]["state"] == "nobody"
              and frame["message"]["state"] == "sent" and frame["message"]["kind"] == "ping",
              "a ping with no listener connected is wake nobody", frame)

        agent_stream = Stream(f"/r/{room}/agent-stream", transports, {"Authorization": "Bearer " + token})
        ahello = agent_stream.next_frame()
        check(ahello.get("t") == "hello" and ahello.get("agent", {}).get("name") == AGENT and "policy" in ahello
              and ahello["agent"]["wake"]["listener"] == "connected", "agent stream opens with hello", ahello)
        frame = stream.next_frame()
        check(frame.get("t") == "agent" and frame["agent"]["name"] == AGENT and frame["agent"]["wake"]["listener"] == "connected",
              "viewer sees listener flip to connected", frame)
        frame = agent_stream.next_frame()
        check(frame.get("t") == "agent" and frame["agent"]["wake"]["listener"] == "connected",
              "agent stream hears its own agent frame", frame)

        status, _, ping = call("POST", f"/r/{room}/messages",
                               {"to": AGENT, "kind": "ping", "text": "look at row 2", "wake": True}, auth=peer)
        check(status == 201 and ping["seq"] == 8, "agent ping with wake is row 8", (status, ping))
        frame = stream.next_frame()
        check(frame.get("t") == "message" and frame.get("rseq") == 8 and frame["message"]["from"] == {"kind": "agent", "name": PEER}
              and frame["message"]["wake"] == {"requested": True, "state": "pending", "reason": None, "ts": None},
              "viewer sees the ping pending, from the peer agent", frame)
        frame = agent_stream.next_frame()
        check(frame.get("t") == "message" and frame.get("rseq") == 8, "agent stream receives the ping", frame)
        frame = agent_stream.next_frame()
        check(frame.get("t") == "wake" and frame.get("rseq") == 8 and frame["message"]["id"] == ping["id"]
              and frame["settings"]["enabled"] is True, "agent stream receives the wake frame after it", frame)

        status, _, body = call("POST", f"/r/{room}/wake-ack", {"message": ping["id"], "result": "woke", "reason": None}, auth=token)
        check(status == 200, "wake-ack woke is accepted", (status, body))
        frame = stream.next_frame()
        check(frame.get("t") == "message" and frame.get("rseq") == 9 and frame["message"]["id"] == ping["id"]
              and frame["message"]["wake"]["state"] == "woke" and frame["message"]["wake"]["ts"],
              "viewer sees the message frame updated to woke as row 9", frame)
        frame = agent_stream.next_frame()
        check(frame.get("t") == "message" and frame.get("rseq") == 9, "agent stream sees the woke update too", frame)
        status, _, body = call("POST", f"/r/{room}/wake-ack", {"message": ping["id"], "result": "woke"}, auth=token)
        check(status == 400 and body.get("error") == "schema", "a second ack on a woke message is 400 schema", (status, body))
        status, _, body = call("POST", f"/r/{room}/wake-ack", {"message": ping["id"], "result": "woke"}, auth=peer)
        check(status == 400 and body.get("error") == "schema", "an ack from the wrong agent is 400 schema", (status, body))
        status, _, snap = call("GET", f"/r/{room}", auth=room)
        me = [a for a in snap["agents"] if a["name"] == AGENT][0]
        check(me["wake"]["used_this_hour"] == 1 and me["wake"]["listener"] == "connected",
              "snapshot counts one wake this hour", me["wake"])

        status, _, say = call("POST", f"/r/{room}/messages", {"to": "*", "text": "hi all", "wake": True}, auth=peer)
        check(status == 201 and say["seq"] == 10, "agent say to everyone is row 10", (status, say))
        frame = stream.next_frame()
        check(frame.get("t") == "message" and frame["message"]["kind"] == "say" and frame["message"]["to"] == "*"
              and frame["message"]["state"] == "sent" and frame["message"]["wake"]["requested"] is False,
              "a say to * defaults kind say and stores wake false", frame)
        frame = agent_stream.next_frame()
        check(frame.get("t") == "message" and frame.get("rseq") == 10, "agent stream receives the room-wide say", frame)

        status, _, inbox = call("GET", f"/r/{room}/inbox?after=6", auth=token)
        check(status == 200 and [m["seq"] for m in inbox["messages"]] == [7, 8, 10]
              and inbox["messages"][1]["wake"]["state"] == "woke",
              "inbox after=6 lists 7, 8 (woke) and the room-wide 10", inbox)
        status, _, page = call("GET", f"/r/{room}/messages?after=6&limit=2", auth=room)
        check(status == 200 and [m["seq"] for m in page["messages"]] == [7, 8] and page["more"] is True,
              "GET /messages pages ascending with more", page)
        status, _, page = call("GET", f"/r/{room}/messages?after=8", auth=room)
        check(status == 200 and [m["seq"] for m in page["messages"]] == [10] and page["more"] is False,
              "GET /messages after=8 is the last page", page)

        agent_stream.close()
        frame = stream.next_frame()
        check(frame.get("t") == "agent" and frame["agent"]["name"] == AGENT and frame["agent"]["wake"]["listener"] == "absent",
              "viewer sees listener flip to absent on close", frame)

        extra = [Stream(f"/r/{room}/agent-stream", transports, {"Authorization": "Bearer " + token}) for _ in range(4)]
        check(all(s.next_frame().get("t") == "hello" for s in extra), "four agent streams open")
        frame = stream.next_frame()
        check(frame.get("t") == "agent" and frame["agent"]["wake"]["listener"] == "connected",
              "the first of four flips the listener once", frame)
        fifth = Stream(f"/r/{room}/agent-stream", transports, {"Authorization": "Bearer " + token})
        check(fifth.next_frame().get("t") == "full", "the fifth agent stream receives full")
        check(fifth.next_frame().get("t") == "<closed>", "and is closed")
        for s in extra:
            s.close()
        frame = stream.next_frame()
        check(frame.get("t") == "agent" and frame["agent"]["wake"]["listener"] == "absent",
              "the last of four closing flips the listener back", frame)

        status, _, _ = call("POST", f"/r/{room}/prune", None, auth=join)
        check(status == 204, "prune answers 204", status)

        replay = Stream(f"/r/{room}/stream?ticket={minted['ticket']}&after=3", transports)
        hello2 = replay.next_frame()
        check(hello2.get("t") == "hello" and hello2.get("backfill") == "replay" and hello2.get("gap") is None,
              "after=3 reconnect says replay", hello2)
        rows = replay.frames_until("live")
        check([r.get("rseq") for r in rows[:-1]] == list(range(4, 11)), "replay sends rows 4..10", rows)
        check(rows[-1].get("t") == "live", "live follows the replay", rows[-1])
        replay.close()

        status, _, _ = call("DELETE", f"/r/{room}", None, auth=join)
        check(status == 204, "delete answers 204", status)
        deleted = True
        frame = stream.next_frame()
        check(frame.get("t") == "gone", "viewer receives gone", frame)
        check(stream.next_frame().get("t") == "<closed>", "stream is closed after gone")
        status, _, body = call("GET", f"/r/{room}", auth=room)
        check(status == 404 and body.get("error") == "room", "room answers 404 after delete", (status, body))
    finally:
        if stream:
            stream.close()
        if not deleted:
            call("DELETE", f"/r/{room}", None, auth=join)
    print("all checks passed")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Failed:
        sys.exit(1)
