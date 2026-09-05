#!/usr/bin/env python3
"""Hold a canvas relay to docs/canvas-contract.md.

Runs against the in-thread Python relay by default and against `CANVAS_RELAY`
when set, which is how any other backend is checked. The stream is read as SSE
through urllib; a backend whose /healthz advertises no `sse` transport skips
the stream cases here (they are probed by tests/canvas_live.py instead), and
the cases that need to move the clock or reach into the process skip with a
reason when the relay is not in this process.

Every test opens its own room, so a rate bucket or a viewer left behind by one
case cannot leak into the next. Credential-shaped strings are assembled at
runtime so a secret scanner never sees one.
"""

from __future__ import annotations

import itertools
import json
import os
import random
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentcolab import canvas_relay          # noqa: E402

REMOTE = os.environ.get("CANVAS_RELAY")
UA = "AgentColab/1 (+https://github.com/AgentColab/AgentColab)"
ALPHABET = "23456789abcdefghjkmnpqrstvwxyz"
ISO_MS = r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$"
NO_WAKE = {"requested": False, "state": "none", "reason": None, "ts": None}
DEFAULT_WAKE = {"enabled": False, "from": "agents", "max_per_hour": 4, "used_this_hour": 0,
                "listener": "absent", "set_by": None, "ts": None}
_counter = itertools.count(1)


def _sym(n: int) -> str:
    return "".join(random.choice(ALPHABET) for _ in range(n))


def _shape(prefix: str, body: str, length: int = 0) -> str:
    filler = (body * 64)[:length] if length else body
    return prefix + filler


def _fake_room() -> str:
    code = _sym(10)
    return f"{code[:4]}-{code[4:8]}-{code[8:]}"


def _ev(kind: str = "text", **over):
    ev = {"v": 1, "id": "ev-%016x" % random.getrandbits(64), "agent": "client-said-so",
          "session": "s-" + "1" * 8, "lane": "main", "epoch": 0, "seq": 256 * next(_counter),
          "ts": "2026-09-04T10:00:00.000Z", "kind": kind, "harness": "claude-code",
          "model": None, "ref": None, "body": {"text": "hello", "final": False}}
    ev.update(over)
    return ev


def _rec(rid: str, ts=None, **over):
    body = {"family": "msg", "kind": "question", "rid": rid, "from": "alice", "to": "*",
            "subject": "s", "paths": [], "reply_to": None, "task": None, "state": None,
            "owner": None, "blocked_by": [], "trust": "verified"}
    if ts is not None:
        body["ts"] = ts
    return _ev("record", id="rec-%016x" % random.getrandbits(64), ref=rid, body=body, **over)


def _batch(events, *, trailing: bool = True) -> bytes:
    lines = [json.dumps(e).encode("utf-8") for e in events]
    return b"\n".join(lines) + (b"\n" if trailing else b"")


def _expected(ev, name):
    out = json.loads(json.dumps(ev))
    out["agent"] = name
    return out


class RelayContract(unittest.TestCase):
    relay = None
    base = ""
    sse = False

    # -- fixtures -------------------------------------------------------

    @classmethod
    def setUpClass(cls):
        if REMOTE:
            cls.base = REMOTE.rstrip("/")
        else:
            cls.relay = canvas_relay.Relay(("127.0.0.1", 0))
            threading.Thread(target=cls.relay.serve_forever, daemon=True).start()
            cls.base = f"http://127.0.0.1:{cls.relay.port}"
        status, _, health = cls.call("GET", "/healthz")
        cls.sse = status == 200 and "sse" in health.get("transports", [])

    @classmethod
    def tearDownClass(cls):
        if cls.relay is not None:
            cls.relay.shutdown()

    def setUp(self):
        self._offset = 0.0
        if self.relay is not None:
            self.relay.clock = lambda: time.time() + self._offset

    def tearDown(self):
        if self.relay is not None:
            self.relay.clock = time.time

    def _local_only(self, why: str):
        if self.relay is None:
            self.skipTest(f"{why}: only against the in-thread relay")

    def _need_sse(self):
        if not self.sse:
            self.skipTest("this relay serves no SSE; its stream is probed by tests/canvas_live.py")

    def _advance(self, seconds: float, *, sleep_ok: bool = False):
        if self.relay is not None:
            self._offset += seconds
        elif sleep_ok and seconds <= 3:
            time.sleep(seconds)
        else:
            self.skipTest(f"needs the clock moved {seconds} s: only against the in-thread relay")

    @classmethod
    def call(cls, method, path, *, auth=None, body=None, raw=None, headers=None, timeout=5):
        data = raw if raw is not None else (json.dumps(body).encode("utf-8")
                                            if body is not None else None)
        hdrs = {"User-Agent": UA}
        if auth:
            hdrs["Authorization"] = "Bearer " + auth
        hdrs.update(headers or {})
        request = urllib.request.Request(cls.base + path, data=data, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                return resp.status, dict(resp.headers), cls._parse(resp.read())
        except urllib.error.HTTPError as err:
            with err:
                return err.code, dict(err.headers), cls._parse(err.read())

    @staticmethod
    def _parse(raw: bytes):
        try:
            return json.loads(raw)
        except ValueError:
            return raw

    def _room(self, **policy):
        body = {"name": "conformance"}
        if policy:
            body["policy"] = policy
        for _ in range(3):
            status, headers, room = self.call("POST", "/rooms", body=body)
            if status != 429:
                break
            time.sleep(min(65, int(headers.get("Retry-After", "5"))))
        self.assertEqual(status, 201, room)
        room["room4"] = room["room"][:4]
        return room

    def _register_out(self, room, name, **body):
        status, _, out = self.call("POST", f"/r/{room['room']}/agents/{name}",
                                   auth=room["join_code"], body=body)
        self.assertEqual(status, 200, out)
        return out

    def _register(self, room, name, **body):
        return self._register_out(room, name, **body)["token"]

    def _post(self, room, token, events=None, raw=None):
        raw = raw if raw is not None else _batch(events)
        return self.call("POST", f"/r/{room['room']}/events", auth=token, raw=raw)

    def _post_ok(self, room, token, events=None, raw=None):
        raw = raw if raw is not None else _batch(events)
        for _ in range(6):
            status, headers, out = self._post(room, token, raw=raw)
            if status != 429:
                break
            self._advance(int(headers.get("Retry-After", "1")), sleep_ok=True)
        self.assertEqual(status, 202, out)
        return out

    def _message(self, room, auth, **body):
        """POST /messages, asserting 201; returns the {id, seq} answer."""
        status, _, out = self.call("POST", f"/r/{room['room']}/messages", auth=auth, body=body)
        self.assertEqual(status, 201, out)
        return out

    def _messages(self, room):
        """Every message object the room holds, ascending by seq."""
        out, after = [], 0
        while True:
            status, _, page = self.call("GET", f"/r/{room['room']}/messages?after={after}&limit=200",
                                        auth=room["room"])
            self.assertEqual(status, 200, page)
            out.extend(page["messages"])
            if not page["more"]:
                return out
            after = out[-1]["seq"]

    def _find(self, room, message_id):
        return next(m for m in self._messages(room) if m["id"] == message_id)

    def _open(self, room, *, auth=None, ticket=None, after=None, headers=None, timeout=5,
              path="stream"):
        query = []
        if ticket is not None:
            query.append("ticket=" + ticket)
        if after is not None:
            query.append("after=" + str(after))
        path = f"/r/{room['room']}/{path}" + ("?" + "&".join(query) if query else "")
        hdrs = {"User-Agent": UA, "Accept": "text/event-stream"}
        if auth:
            hdrs["Authorization"] = "Bearer " + auth
        hdrs.update(headers or {})
        request = urllib.request.Request(self.base + path, headers=hdrs)
        return urllib.request.urlopen(request, timeout=timeout)

    def _stream(self, room, **kw):
        resp = self._open(room, **kw)
        self.addCleanup(resp.close)
        self.assertEqual(resp.status, 200)
        return resp

    def _stream_error(self, room, **kw):
        try:
            resp = self._open(room, **kw)
        except urllib.error.HTTPError as err:
            with err:
                return err.code, self._parse(err.read())
        resp.close()
        self.fail("the stream opened when it should have been refused")

    def _agent_stream(self, room, token):
        return self._stream(room, auth=token, path="agent-stream")

    @staticmethod
    def _frames(resp, until, cap: int = 100_000):
        """(sse id, frame) pairs up to and including the first frame `until` accepts."""
        out, sse_id, data = [], None, []
        while len(out) < cap:
            line = resp.readline()
            if not line:
                raise AssertionError(f"stream ended before the sentinel; last frames: {out[-3:]}")
            line = line.rstrip(b"\r\n")
            if not line:
                if data:
                    frame = json.loads(b"".join(data))
                    out.append((sse_id, frame))
                    if until(frame):
                        return out
                sse_id, data = None, []
                continue
            if line.startswith(b":"):
                continue
            field, _, value = line.partition(b":")
            if value.startswith(b" "):
                value = value[1:]
            if field == b"id":
                sse_id = int(value)
            elif field == b"data":
                data.append(value)
        raise AssertionError("too many frames before the sentinel")

    def _until_live(self, resp):
        return self._frames(resp, lambda f: f.get("t") == "live")

    def _until(self, resp, t):
        return [f for _, f in self._frames(resp, lambda f: f.get("t") == t)]

    @staticmethod
    def _at_eof(resp) -> bool:
        return resp.readline() == b""

    # -- §3.15, §3.16 -----------------------------------------------------

    def test_healthz_and_route_table(self):
        status, headers, health = self.call("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertIs(health["ok"], True)
        self.assertEqual(health["version"], "1")
        self.assertTrue(set(health["transports"]) <= {"ws", "sse"} and health["transports"])
        if self.relay is not None:
            self.assertEqual(health, {"ok": True, "backend": "python", "transports": ["sse"],
                                      "version": "1"})
        for method, path in (("GET", "/rooms"), ("PATCH", "/r/x"), ("GET", "/r/"),
                             ("GET", "/healthz/x"), ("POST", "/roomsx"),
                             ("GET", f"/r/{_fake_room()}/agents/x"),
                             ("POST", f"/r/{_fake_room()}/asks")):          # removed in v1.3
            status, _, body = self.call(method, path)
            self.assertEqual((status, body["error"], body["hint"]),
                             (404, "room", "no such route"), (method, path))
        for path in ("/", "/anything/else", "/index.html"):
            status, headers, body = self.call("GET", path)
            self.assertEqual(status, 200, path)
            self.assertIn("text/html", headers.get("Content-Type", ""))
            self.assertTrue(body)

    # -- §3.1 ---------------------------------------------------------------

    def test_create_room(self):
        status, _, room = self.call("POST", "/rooms", body={})
        self.assertEqual(status, 201)
        self.assertRegex(room["room"], canvas_relay.ROOM_RE)
        self.assertRegex(room["join_code"], canvas_relay.JOIN_RE)
        self.assertTrue(room["join_code"].startswith(room["room"] + "."))
        self.assertEqual(room["policy"], {"max_stream": "tools", "retention_min": 120,
                                          "ticket_ttl_s": 600, "ask_ttl_s": 86400})
        self.assertTrue(room["relay"].startswith("http"))
        self.assertEqual(room["url"], room["relay"] + "/#" + room["room"])
        _, _, snap = self.call("GET", "/r/" + room["room"], auth=room["room"])
        self.assertEqual(snap["name"], "room")

        status, _, custom = self.call("POST", "/rooms", body={
            "name": "\x07my\tteam\x00 \u0085x", "policy": {"max_stream": "summary",
                                                          "retention_min": 5,
                                                          "ticket_ttl_s": 1, "ask_ttl_s": 1}})
        self.assertEqual(status, 201)
        self.assertEqual(custom["policy"], {"max_stream": "summary", "retention_min": 5,
                                            "ticket_ttl_s": 1, "ask_ttl_s": 1})
        _, _, snap = self.call("GET", "/r/" + custom["room"], auth=custom["room"])
        self.assertEqual(snap["name"], "myteam x")

        status, _, absent = self.call("POST", "/rooms")          # no body at all
        self.assertEqual(status, 201)
        _, _, snap = self.call("GET", "/r/" + absent["room"], auth=absent["room"])
        self.assertEqual(snap["name"], "room")

        # Only an absent `policy` means the defaults; a falsy wrong type is a wrong type.
        for bad in ({"name": "a" * 61}, {"name": 7}, {"policy": {"retention_min": 4}},
                    {"policy": {"retention_min": 721}}, {"policy": {"max_stream": "off"}},
                    {"policy": {"ticket_ttl_s": "600"}}, {"policy": {"ask_ttl_s": True}},
                    {"policy": {"ticket_ttl_s": 3601}}, {"policy": [1]}, [1],
                    {"policy": 0}, {"policy": ""}, {"policy": False}, {"policy": None}):
            status, _, body = self.call("POST", "/rooms", body=bad)
            self.assertEqual((status, body["error"]), (400, "schema"), bad)
        status, _, body = self.call("POST", "/rooms", raw=b"{not json")
        self.assertEqual((status, body["error"]), (400, "schema"))

    # -- §0 check order, §1 classes, §10.2 the owner class ----------------------

    def test_auth_matrix(self):
        room = self._room()
        reg = self._register_out(room, "agent-a")
        token = reg["token"]
        _, _, minted = self.call("POST", f"/r/{room['room']}/ticket", auth=room["room"], body={})
        valid = {"room": room["room"], "join": room["join_code"], "token": token,
                 "owner": reg["owner_token"], "ticket": minted["ticket"]}
        unknown = {"room": _fake_room(), "join": room["room"] + "." + _sym(24),
                   "token": "at-" + _sym(32), "owner": "ot-" + _sym(32), "ticket": "vt-" + _sym(32)}
        routes = [
            ("GET", "/r/{room}", {"room"}), ("POST", "/r/{room}/ticket", {"room"}),
            ("GET", "/r/{room}/events?after=0", {"room"}), ("GET", "/r/{room}/records", {"room"}),
            ("POST", "/r/{room}/messages", {"room", "token"}),
            ("GET", "/r/{room}/messages?after=0", {"room"}),
            ("PUT", "/r/{room}/roles/agent-a", {"room", "token", "owner"}),
            ("GET", "/r/{room}/stream", {"room"}), ("POST", "/r/{room}/agents/newbie", {"join"}),
            ("DELETE", "/r/{room}/agents/agent-a", {"join", "token", "owner"}),
            ("POST", "/r/{room}/events", {"token"}), ("GET", "/r/{room}/inbox?after=0", {"token"}),
            ("GET", "/r/{room}/agent-stream", {"token"}),
            ("PUT", "/r/{room}/agents/agent-a/wake", {"token", "owner"}),
            ("POST", "/r/{room}/wake-ack", {"token"}),
            ("PUT", "/r/{room}/policy", {"join"}), ("POST", "/r/{room}/prune", {"join"}),
            ("DELETE", "/r/{room}", {"join"}),
        ]
        for method, template, allowed in routes:
            path = template.replace("{room}", room["room"])
            body = {} if method in ("POST", "PUT") else None
            for hdrs in (None, {"Authorization": "Basic abc"}, {"Authorization": "Bearer nonsense"},
                         {"Authorization": "Token " + room["room"]}):
                status, _, out = self.call(method, path, body=body, headers=hdrs)
                self.assertEqual((status, out["error"]), (401, "auth"), (method, path, hdrs))
            for cls in ("room", "join", "token", "owner"):
                if cls in allowed:
                    status, _, out = self.call(method, path, auth=unknown[cls], body=body)
                    self.assertEqual((status, out["error"]), (401, "auth"), (method, path, cls))
                else:
                    for value in (valid[cls], unknown[cls]):
                        status, _, out = self.call(method, path, auth=value, body=body)
                        self.assertEqual((status, out["error"]), (403, "forbidden"),
                                         (method, path, cls))
            gone = template.replace("{room}", _fake_room())
            status, _, out = self.call(method, gone, body=body, auth=valid[next(iter(allowed))])
            self.assertEqual((status, out["error"]), (404, "room"), (method, gone))
            status, _, out = self.call(method, gone, body=body)
            self.assertEqual((status, out["error"]), (404, "room"), (method, gone, "no auth"))
        # A viewer ticket opens the stream and nothing else.
        status, _, out = self.call("GET", f"/r/{room['room']}", auth=minted["ticket"])
        self.assertEqual((status, out["error"]), (403, "forbidden"))
        # A room code cannot post events, whatever it carries (§8).
        status, _, out = self.call("POST", f"/r/{room['room']}/events", auth=room["room"],
                                   raw=_batch([_ev()]))
        self.assertEqual((status, out["error"]), (403, "forbidden"))
        _, _, snap = self.call("GET", f"/r/{room['room']}", auth=room["room"])
        self.assertEqual(snap["rseq"], 0, "a refused request must have no side effect")

    # -- §3.2, §10.3 ---------------------------------------------------------

    def test_snapshot_shape(self):
        room = self._room()
        self._register(room, "beta", harness="codex", human="bob", model="gpt-5.6", stream="full")
        self._register(room, "alpha")
        status, _, snap = self.call("GET", f"/r/{room['room']}", auth=room["room"])
        self.assertEqual(status, 200)
        self.assertEqual(set(snap), {"room", "name", "rseq", "policy", "agents", "messages"})
        self.assertEqual((snap["room"], snap["name"], snap["rseq"], snap["messages"]),
                         (room["room"], "conformance", 0, []))
        self.assertEqual([a["name"] for a in snap["agents"]], ["alpha", "beta"])
        beta = snap["agents"][1]
        self.assertEqual(set(beta), {"name", "harness", "human", "model", "stream",
                                     "registered_at", "last_seen", "kicked", "role", "snapshot",
                                     "wake"})
        self.assertEqual((beta["harness"], beta["human"], beta["model"], beta["stream"],
                          beta["kicked"], beta["role"], beta["snapshot"]),
                         ("codex", "bob", "gpt-5.6", "full", False, None, None))
        self.assertEqual(beta["wake"], DEFAULT_WAKE, "wake never set: the §10.3 default")
        alpha = snap["agents"][0]
        self.assertEqual((alpha["harness"], alpha["human"], alpha["model"], alpha["stream"]),
                         (None, None, None, "tools"))
        self.assertRegex(alpha["registered_at"], ISO_MS)

    # -- §3.3, §3.4 gate, §6.13 ------------------------------------------------

    def test_ticket_and_stream_gate(self):
        room = self._room(ticket_ttl_s=1)
        token = self._register(room, "agent-a")
        code = room["room"]
        status, _, minted = self.call("POST", f"/r/{code}/ticket", auth=code)     # absent body
        self.assertEqual(status, 201)
        self.assertRegex(minted["ticket"], canvas_relay.TICKET_RE)
        self.assertEqual(minted["ttl"], 1)
        status, _, body = self.call("POST", f"/r/{code}/ticket", auth=code, raw=b"")
        self.assertEqual(status, 201)

        status, body = self._stream_error(room)
        self.assertEqual((status, body["error"]), (401, "auth"))
        status, body = self._stream_error(room, auth=room["join_code"])
        self.assertEqual((status, body["error"]), (403, "forbidden"))
        status, body = self._stream_error(room, auth=token)
        self.assertEqual((status, body["error"]), (403, "forbidden"))
        status, body = self._stream_error(room, ticket="vt-" + _sym(32), auth=code)
        self.assertEqual((status, body["error"]), (401, "auth"),
                         "the query ticket is checked when present; the header is not consulted")
        status, body = self._stream_error(room, ticket=code)
        self.assertEqual((status, body["error"]), (401, "auth"))
        status, body = self._stream_error(room, auth=code, after="abc")
        self.assertEqual((status, body["error"]), (400, "schema"))
        status, body = self._stream_error(room, auth=code, after="-1")
        self.assertEqual((status, body["error"]), (400, "schema"))
        self._need_sse()
        first = self._stream(room, ticket=minted["ticket"])
        second = self._stream(room, ticket=minted["ticket"])
        for resp in (first, second):
            frames = self._until_live(resp)
            self.assertEqual(frames[0][1]["t"], "hello")
        header = self._stream(room, auth=code)
        self.assertEqual(self._until_live(header)[0][1]["t"], "hello")
        time.sleep(1.2)
        status, body = self._stream_error(room, ticket=minted["ticket"])
        self.assertEqual((status, body["error"]), (401, "auth"), "a ticket expires after its ttl")

    # -- §3.6, §3.7, §10.2 ----------------------------------------------------

    def test_register_and_kick(self):
        room = self._room(max_stream="tools")
        code = room["room"]
        status, _, out = self.call("POST", f"/r/{code}/agents/agent-a", auth=room["join_code"],
                                   body={"harness": "claude-code", "human": "me\x01", "stream": "full"})
        self.assertEqual(status, 200)
        self.assertRegex(out["token"], canvas_relay.TOKEN_RE)
        self.assertRegex(out["owner_token"], canvas_relay.OWNER_RE)
        self.assertEqual((out["rseq"], out["policy"], out["effective_stream"]),
                         (0, room["policy"], "tools"))
        first, first_owner = out["token"], out["owner_token"]
        _, _, snap = self.call("GET", f"/r/{code}", auth=code)
        registered_at = snap["agents"][0]["registered_at"]
        self.assertEqual((snap["agents"][0]["human"], snap["agents"][0]["stream"]), ("me", "full"))

        again = self._register_out(room, "agent-a", harness="codex", model="gpt-5.6", stream="summary")
        second, owner = again["token"], again["owner_token"]
        self.assertNotEqual(first, second)
        self.assertNotEqual(first_owner, owner)
        status, _, out = self._post(room, first, [_ev()])
        self.assertEqual((status, out["error"]), (401, "auth"), "a rotated token answers 401")
        wake_path = f"/r/{code}/agents/agent-a/wake"
        status, _, out = self.call("PUT", wake_path, auth=first_owner, body={"enabled": True})
        self.assertEqual((status, out["error"]), (401, "auth"),
                         "the owner token rotates with the agent token")
        status, _, out = self.call("PUT", wake_path, auth=owner, body={"enabled": True})
        self.assertEqual((status, out["wake"]["set_by"]), (200, "owner"))
        _, _, snap = self.call("GET", f"/r/{code}", auth=code)
        agent = snap["agents"][0]
        self.assertEqual((agent["harness"], agent["human"], agent["model"], agent["stream"],
                          agent["registered_at"]),
                         ("codex", None, "gpt-5.6", "summary", registered_at))

        for name, body in (("Bad", {}), ("-x", {}), ("a" * 49, {}), ("ok", {"stream": "off"}),
                           ("ok", {"stream": 3}), ("ok", {"human": 5}), ("ok", {"harness": "h" * 121})):
            status, _, out = self.call("POST", f"/r/{code}/agents/{name}", auth=room["join_code"],
                                       body=body)
            self.assertEqual((status, out["error"]), (400, "schema"), (name, body))
        status, _, out = self.call("POST", f"/r/{code}/agents/ok", auth=room["join_code"],
                                   raw=b"[1]")
        self.assertEqual((status, out["error"]), (400, "schema"))

        for i in range(2, 13):
            self._register(room, f"agent-{i:02d}")
        status, _, out = self.call("POST", f"/r/{code}/agents/agent-13", auth=room["join_code"],
                                   body={})
        self.assertEqual((status, out["error"]), (503, "full"))

        # kick: the join code, then the agent's own token; a different agent's
        # token or owner token is refused
        status, _, out = self.call("DELETE", f"/r/{code}/agents/agent-02", auth=second)
        self.assertEqual((status, out["error"]), (403, "forbidden"))
        status, _, out = self.call("DELETE", f"/r/{code}/agents/agent-02", auth=owner)
        self.assertEqual((status, out["error"]), (403, "forbidden"))
        status, _, out = self.call("DELETE", f"/r/{code}/agents/agent-02", auth=room["join_code"])
        self.assertEqual((status, out), (204, b""))
        status, _, out = self.call("DELETE", f"/r/{code}/agents/never", auth=room["join_code"])
        self.assertEqual((status, out["error"]), (404, "agent"))
        status, _, out = self.call("DELETE", f"/r/{code}/agents/agent-a", auth=second)
        self.assertEqual(status, 204)
        status, _, out = self._post(room, second, [_ev()])
        self.assertEqual((status, out["error"]), (401, "auth"), "a kicked token answers 401")
        status, _, out = self.call("PUT", wake_path, auth=owner, body={"enabled": True})
        self.assertEqual((status, out["error"]), (401, "auth"), "a kicked owner token answers 401")
        _, _, snap = self.call("GET", f"/r/{code}", auth=code)
        kicked = {a["name"]: a["kicked"] for a in snap["agents"]}
        self.assertTrue(kicked["agent-a"] and kicked["agent-02"] and not kicked["agent-03"])
        self.assertIn("agent-a", kicked, "a kicked agent stays in the snapshot")
        status, _, out = self.call("PUT", f"/r/{code}/roles/agent-a", auth=code,
                                   body={"role": "x", "viewer": "v"})
        self.assertEqual((status, out["error"]), (404, "agent"))
        # kicked names do not count toward twelve; a kicked name re-registers and is cleared
        self._register(room, "agent-13")
        third = self._register(room, "agent-a")
        _, _, snap = self.call("GET", f"/r/{code}", auth=code)
        agent = next(a for a in snap["agents"] if a["name"] == "agent-a")
        self.assertEqual((agent["kicked"], agent["registered_at"]), (False, registered_at))
        self.assertEqual(self._post(room, third, [_ev()])[0], 202)
        # the owner token leaves the room for its own name
        twelve = self._register_out(room, "agent-12")
        status, _, out = self.call("DELETE", f"/r/{code}/agents/agent-12", auth=twelve["owner_token"])
        self.assertEqual((status, out), (204, b""), "an owner token kicks its own name")

        self._need_sse()
        resp = self._stream(room, auth=code)
        self._until_live(resp)
        self._register(room, "agent-new", human="h")
        frames = self._frames(resp, lambda f: f.get("t") == "agent")
        sse_id, frame = frames[-1]
        self.assertIsNone(sse_id)
        self.assertNotIn("rseq", frame)
        self.assertEqual((frame["agent"]["name"], frame["agent"]["human"], frame["agent"]["kicked"],
                          frame["agent"]["snapshot"], frame["agent"]["wake"]),
                         ("agent-new", "h", False, None, DEFAULT_WAKE))
        self.call("DELETE", f"/r/{code}/agents/agent-new", auth=room["join_code"])
        _, frame = self._frames(resp, lambda f: f.get("t") == "agent")[-1]
        self.assertEqual((frame["agent"]["name"], frame["agent"]["kicked"]), ("agent-new", True))
        status, _, _ = self.call("DELETE", f"/r/{code}/agents/agent-new", auth=room["join_code"])
        self.assertEqual(status, 204)
        self.call("PUT", f"/r/{code}/policy", auth=room["join_code"], body={"retention_min": 120})
        between = self._frames(resp, lambda f: f.get("t") == "policy")
        self.assertEqual([f["t"] for _, f in between], ["policy"],
                         "kicking an already-kicked name emits no frame")

    # -- §3.8, §6.2, §6.3 ---------------------------------------------------

    def test_events_equality_and_agent_overwrite(self):
        room = self._room(max_stream="full")
        token = self._register(room, "agent-a")
        code = room["room"]
        e1 = _ev("text", body={"text": "héllo — 日本語 🚀 \"quoted\" back\\slash\nnewline\ttab",
                               "final": False, "n": 2 ** 62, "f": 1.5, "neg": -0.25,
                               "list": [1, "two", None, True, {"deep": {"deeper": "x"}}],
                               "empty": "", "obj": {}}, extra={"unknown": [1, 2]})
        e2 = _ev("tool_call", ref="toolu_01", model="claude-fable-5-1",
                 body={"name": "Bash", "args": {"command": "ls"}, "paths": [], "omitted": {}})
        e3 = _ev("session", body={"state": "start", "source": "startup", "title": None})
        reversed_e3 = {k: e3[k] for k in reversed(list(e3))}
        raw = (json.dumps(e1, ensure_ascii=True).encode() + b"\n"
               + json.dumps(e2, ensure_ascii=False).encode("utf-8") + b"\r\n"
               + json.dumps(reversed_e3).encode())            # trailing newline optional
        status, _, out = self._post(room, token, raw=raw)
        self.assertEqual(status, 202, out)
        self.assertEqual(out, {"rseq": 1, "accepted": 3, "dup": 0, "rejected": []})
        expected = {"t": "batch", "rseq": 1, "agent": "agent-a",
                    "events": [_expected(e1, "agent-a"), _expected(e2, "agent-a"),
                               _expected(e3, "agent-a")]}
        status, _, page = self.call("GET", f"/r/{code}/events?after=0", auth=code)
        self.assertEqual(page, {"rseq": 1, "frames": [expected], "more": False})
        self.assertEqual(page["frames"][0]["events"][0]["agent"], "agent-a",
                         "the relay overwrites agent from the token")
        _, _, page = self.call("GET", f"/r/{code}/events?before=2&agent=agent-a", auth=code)
        self.assertEqual(page["frames"], [expected])
        if not self.sse:
            return
        replay = self._stream(room, auth=code, after=0)
        frames = self._until_live(replay)
        self.assertEqual([f for _, f in frames][1], expected)
        fresh = self._stream(room, auth=code)
        frames = self._until_live(fresh)
        self.assertEqual([f for _, f in frames][1], expected)

    def test_integral_floats_count_as_integers(self):
        # JSON has one number type: `1.0` is what some serialisers write for 1,
        # and the Worker's Number.isInteger accepts it, so both backends must.
        room = self._room(max_stream="full")
        token = self._register(room, "agent-a")
        code = room["room"]
        ev = _ev("text", v=1.0, epoch=0.0, seq=256.0)
        raw = json.dumps(ev).encode() + b"\n"
        self.assertIn(b'"v": 1.0', raw)
        out = self._post_ok(room, token, raw=raw)
        self.assertEqual((out["accepted"], out["rejected"]), (1, []))
        _, _, page = self.call("GET", f"/r/{code}/events?after=0", auth=code)
        self.assertEqual(page["frames"][0]["events"], [_expected(ev, "agent-a")])
        fraction, boolean = _ev("text", seq=1.5), _ev("text", v=True)
        out = self._post_ok(room, token, [fraction, boolean])
        self.assertEqual(out["rejected"], [{"id": fraction["id"], "why": "schema"},
                                           {"id": boolean["id"], "why": "schema"}])
        status, _, made = self.call("POST", "/rooms", body={"policy": {"retention_min": 60.0}})
        self.assertEqual((status, made["policy"]["retention_min"]), (201, 60))

    def test_batch_dedup_blank_lines_and_rseq_from_one(self):
        room = self._room()
        token = self._register(room, "agent-a")
        code = room["room"]
        raw = b"\n  \n" + _batch([_ev(), _ev()]) + b"\t\n\n"
        first = self._post_ok(room, token, raw=raw)
        self.assertEqual(first, {"rseq": 1, "accepted": 2, "dup": 0, "rejected": []})
        again = self._post_ok(room, token, raw=raw)
        self.assertEqual(again, {"rseq": 1, "accepted": 0, "dup": 2, "rejected": []})
        empty = self._post_ok(room, token, raw=b"\n \n\t\r\n")
        self.assertEqual(empty, {"rseq": 1, "accepted": 0, "dup": 0, "rejected": []})
        self.assertEqual(self._post_ok(room, token, raw=b""),
                         {"rseq": 1, "accepted": 0, "dup": 0, "rejected": []})
        bogus = _ev("bogus")
        refused = self._post_ok(room, token, [bogus])
        self.assertEqual(refused, {"rseq": 1, "accepted": 0, "dup": 0,
                                   "rejected": [{"id": bogus["id"], "why": "kind"}]})
        refused_again = self._post_ok(room, token, [bogus])
        self.assertEqual(refused_again["rejected"], [{"id": bogus["id"], "why": "kind"}],
                         "an all-rejected batch writes no row, so it is never a dup")
        second = self._post_ok(room, token, raw=_batch([_ev()], trailing=False))
        self.assertEqual((second["rseq"], second["accepted"]), (2, 1))
        _, _, page = self.call("GET", f"/r/{code}/events?after=0", auth=code)
        self.assertEqual([f["rseq"] for f in page["frames"]], [1, 2])
        _, _, snap = self.call("GET", f"/r/{code}", auth=code)
        self.assertEqual(snap["rseq"], 2)

    def test_per_event_checks_in_order(self):
        room = self._room(max_stream="tools")
        token = self._register(room, "agent-a")
        big_ok = _ev("bogus", body={"text": "x" * 41_000})
        status, _, out = self._post(room, token, raw=b"{" + b" " * 41_000 + b"}\n")   # unparsable
        self.assertEqual((status, out["rejected"]), (202, [{"id": None, "why": "oversize"}]))
        lines = [
            json.dumps(big_ok).encode(),                                   # oversize before kind
            b"not json", b"[1, 2]", b'"a string"',                         # schema, id null
            json.dumps(_ev("bogus", v=2)).encode(),                        # schema before kind
            json.dumps(_ev("text", v=True)).encode(),
            json.dumps(_ev("bogus", body={"text": "thinking-shaped"})).encode(),   # kind
            json.dumps(_ev("thinking", body={"text": "t"})).encode(),      # policy at tools
            json.dumps(_ev("text")).encode(),                              # accepted
        ]
        schema_cases = [_ev("text", lane=""), _ev("text", id=""), _ev("text", id="i" * 65),
                        _ev("text", epoch=-1), _ev("text", seq="1"), _ev("text", body=[]),
                        _ev("text", model="m" * 65), _ev("text", ref="r" * 129),
                        _ev("text", ts="t" * 41), _ev("text", harness="h" * 33),
                        _ev(kind=5), _ev("text", session="s" * 65)]
        missing = _ev("text")
        del missing["session"]
        schema_cases.append(missing)
        lines.extend(json.dumps(e).encode() for e in schema_cases)
        good_tail = _ev("prompt", body={"text": "p"})
        lines.append(json.dumps(good_tail).encode())
        status, _, out = self._post(room, token, raw=b"\n".join(lines) + b"\n")
        self.assertEqual(status, 202, out)
        ids = [json.loads(line).get("id") if line.startswith(b"{") and not line.startswith(b"{ ")
               else None for line in lines]
        expected = [{"id": ids[0], "why": "oversize"},
                    {"id": None, "why": "schema"}, {"id": None, "why": "schema"},
                    {"id": None, "why": "schema"}, {"id": ids[4], "why": "schema"},
                    {"id": ids[5], "why": "schema"}, {"id": ids[6], "why": "kind"},
                    {"id": ids[7], "why": "policy"}]
        # A string id is reported as sent, even an empty one; null is only for a
        # line that is not a JSON object (§3.8).
        expected += [{"id": e["id"], "why": "schema"} for e in schema_cases]
        self.assertEqual(out["rejected"], expected)
        self.assertEqual((out["accepted"], out["dup"], out["rseq"]), (2, 0, 1))
        _, _, page = self.call("GET", f"/r/{room['room']}/events?after=0", auth=room["room"])
        self.assertEqual([e["id"] for e in page["frames"][0]["events"]],
                         [json.loads(lines[8])["id"], good_tail["id"]])

    # -- §4.4, §6.11 ---------------------------------------------------------

    def test_policy_ceiling_per_event(self):
        def whys(room, token, events):
            out = self._post_ok(room, token, events)
            return {e["id"]: None for e in events} | {r["id"]: r["why"] for r in out["rejected"]}

        summary = self._room(max_stream="summary")
        token = self._register(summary, "agent-a", stream="full")
        cases = {
            "thinking": _ev("thinking", body={"text": "t"}),
            "result_text": _ev("tool_result", body={"ok": True, "text": ""}),
            "result_plain": _ev("tool_result", body={"ok": True, "bytes": 3}),
            "call_empty_args": _ev("tool_call", body={"name": "Edit", "args": {}, "paths": ["a"],
                                                      "omitted": {"content": {"bytes": 1}}}),
            "call_args": _ev("tool_call", body={"name": "Bash", "args": {"command": "ls"}}),
            "call_no_args": _ev("tool_call", body={"name": "Bash", "paths": []}),
            "text": _ev("text"),
            "prompt_long": _ev("prompt", body={"text": "p" * 201}),
            "prompt_ok": _ev("prompt", body={"text": "é" * 200}),
            "session": _ev("session", body={"state": "start"}),
            "gap": _ev("gap", body={"from_seq": 1, "to_seq": 2, "count": 1, "reason": "backlog"}),
            "record": _rec("m-1"),
        }
        got = whys(summary, token, list(cases.values()))
        self.assertEqual({k: got[v["id"]] for k, v in cases.items()}, {
            "thinking": "policy", "result_text": "policy", "result_plain": None,
            "call_empty_args": None, "call_args": "policy", "call_no_args": None,
            "text": "policy", "prompt_long": "policy", "prompt_ok": None, "session": None,
            "gap": None, "record": None})

        tools = self._room(max_stream="tools")
        token = self._register(tools, "agent-a", stream="full")
        cases = {
            "thinking": _ev("thinking", body={"text": "t"}),
            "result_text": _ev("tool_result", body={"ok": True, "text": "out"}),
            "content_key": _ev("tool_call", body={"name": "Edit", "args": {"file_path": "x",
                                                                            "old_string": "a"}}),
            "nested_only": _ev("tool_call", body={"name": "X", "args": {"file_path": "x",
                                                                         "nested": {"content": 1}}}),
            "text": _ev("text"),
            "prompt_long": _ev("prompt", body={"text": "p" * 5000}),
        }
        got = whys(tools, token, list(cases.values()))
        self.assertEqual({k: got[v["id"]] for k, v in cases.items()}, {
            "thinking": "policy", "result_text": "policy", "content_key": "policy",
            "nested_only": None, "text": None, "prompt_long": None})
        # Lowering the ceiling applies to the next batch from a client registered before it.
        status, _, _ = self.call("PUT", f"/r/{tools['room']}/policy", auth=tools["join_code"],
                                 body={"max_stream": "summary"})
        self.assertEqual(status, 200)
        text = _ev("text")
        self.assertEqual(whys(tools, token, [text])[text["id"]], "policy")

        full = self._room(max_stream="full")
        token = self._register(full, "agent-a", stream="full")
        cases = [_ev("thinking", body={"text": "t"}), _ev("tool_result", body={"text": "o"}),
                 _ev("tool_call", body={"name": "Write", "args": {"content": "c"}})]
        self.assertEqual(set(whys(full, token, cases).values()), {None})

    # -- §2 batch limits, §6.9 -------------------------------------------------

    def test_oversize_batch_413(self):
        room = self._room(max_stream="full")
        token = self._register(room, "agent-a")
        big = _batch([_ev("text", body={"text": "x" * 30_000}), _ev("text", body={"text": "y" * 30_000}),
                      _ev("text", body={"text": "z" * 6_000})])
        self.assertGreater(len(big), 65_536)
        status, _, out = self._post(room, token, raw=big)
        self.assertEqual((status, out["error"]), (413, "oversize"))
        many = _batch([_ev("gap", body={}) for _ in range(201)])
        self.assertLess(len(many), 65_536)
        status, _, out = self._post(room, token, raw=many)
        self.assertEqual((status, out["error"]), (413, "oversize"))
        exactly = _batch([_ev("gap", body={}) for _ in range(200)]) + b"\n\n"
        self.assertEqual(self._post_ok(room, token, raw=exactly)["accepted"], 200)
        _, _, snap = self.call("GET", f"/r/{room['room']}", auth=room["room"])
        self.assertEqual(snap["rseq"], 1, "a 413 stores nothing")

    def test_rate_bucket_429(self):
        room = self._room()
        token = self._register(room, "agent-a")
        code = room["room"]
        if self.relay is not None:
            frozen = time.time()
            self.relay.clock = lambda: frozen
        seen = None
        for i in range(12):
            status, headers, out = self._post(room, token, [_ev(body={"i": i})])
            if status == 429:
                seen = (i, headers, out)
                break
            self.assertEqual(status, 202, out)
        self.assertIsNotNone(seen, "twelve batches in a burst never hit the bucket")
        i, headers, out = seen
        self.assertEqual(out["error"], "rate")
        self.assertGreaterEqual(int(headers["Retry-After"]), 1)
        if self.relay is not None:
            self.assertEqual(i, 10, "burst is exactly 10")
        _, _, snap = self.call("GET", f"/r/{code}", auth=code)
        self.assertEqual(snap["rseq"], i, "a 429 consumes nothing")
        if self.relay is not None:
            self.relay.clock = lambda: frozen + 0.5       # 2/s sustained: one token back
            status, _, out = self._post(room, token, [_ev(body={"i": "after"})])
            self.assertEqual((status, out["rseq"]), (202, i + 1))
            status, _, out = self._post(room, token, [_ev(body={"i": "again"})])
            self.assertEqual(status, 429)
            # A dup and an all-rejected batch each cost a token (§3.8).
            self.relay.clock = lambda: frozen + 100
            dup = _batch([_ev(body={"d": 1})])
            self.assertEqual(self._post(room, token, raw=dup)[0], 202)
            for _ in range(4):
                self.assertEqual(self._post(room, token, raw=dup)[2]["dup"], 1)
            for _ in range(5):
                self.assertEqual(self._post(room, token, [_ev("bogus")])[2]["accepted"], 0)
            status, _, out = self._post(room, token, [_ev(body={"d": 2})])
            self.assertEqual((status, out["error"]), (429, "rate"))
        else:
            time.sleep(int(headers["Retry-After"]))
            status, _, out = self._post(room, token, [_ev(body={"i": i})])
            self.assertEqual((status, out["dup"]), (202, 0), "the refused batch was not consumed")

    # -- §6.5 snapshots, §6.4 records, §3.17 ---------------------------------

    def test_agent_snapshots_are_consumed(self):
        room = self._room()
        token = self._register(room, "agent-a")
        code = room["room"]
        if self.sse:
            resp = self._stream(room, auth=code)
            self._until_live(resp)
        _, _, before = self.call("GET", f"/r/{code}", auth=code)
        self._advance(2, sleep_ok=True)
        snapshot = {"human": "me", "state": "idle", "self_role": "contributor", "role": None,
                    "role_seen_seq": 0, "surface": {"files": ["a.py"]}}
        out = self._post_ok(room, token, [_ev("agent", body=snapshot)])
        self.assertEqual(out, {"rseq": 0, "accepted": 1, "dup": 0, "rejected": []})
        _, _, snap = self.call("GET", f"/r/{code}", auth=code)
        self.assertEqual(snap["rseq"], 0, "a batch of only agent events writes no row")
        self.assertEqual(snap["agents"][0]["snapshot"], snapshot)
        self.assertGreater(snap["agents"][0]["last_seen"], before["agents"][0]["last_seen"])
        _, _, page = self.call("GET", f"/r/{code}/events?after=0", auth=code)
        self.assertEqual(page["frames"], [])
        if self.sse:
            sse_id, frame = self._frames(resp, lambda f: f.get("t") == "agent")[-1]
            self.assertIsNone(sse_id)
            self.assertNotIn("rseq", frame)
            self.assertEqual(frame["agent"]["snapshot"], snapshot)
        working = dict(snapshot, state="working")
        idle = dict(snapshot, state="idle", n=2)
        out = self._post_ok(room, token, [_ev("agent", body=working), _ev("text"),
                                          _ev("agent", body=idle)])
        self.assertEqual((out["rseq"], out["accepted"]), (1, 3))
        _, _, snap = self.call("GET", f"/r/{code}", auth=code)
        self.assertEqual(snap["agents"][0]["snapshot"], idle, "the newest snapshot wins")
        _, _, page = self.call("GET", f"/r/{code}/events?after=0", auth=code)
        self.assertEqual([e["kind"] for e in page["frames"][0]["events"]], ["text"],
                         "snapshots never enter a batch row")
        if self.sse:
            frames = self._until(resp, "batch")
            self.assertEqual([f["t"] for f in frames], ["agent", "agent", "batch"],
                             "one frame per snapshot, each as of its event, ahead of the batch row")
            self.assertEqual([f["agent"]["snapshot"]["state"] for f in frames[:2]],
                             ["working", "idle"])
        self._advance(2, sleep_ok=True)
        _, _, before = self.call("GET", f"/r/{code}", auth=code)
        self.call("GET", f"/r/{code}/inbox?after=0", auth=token)
        _, _, after = self.call("GET", f"/r/{code}", auth=code)
        self.assertGreater(after["agents"][0]["last_seen"], before["agents"][0]["last_seen"],
                           "an inbox pull updates last_seen")

    def test_records_dedup_order_and_cap(self):
        room = self._room()
        a = self._register(room, "agent-a")
        b = self._register(room, "agent-b")
        code = room["room"]
        status, _, out = self.call("GET", f"/r/{code}/records", auth=code)
        self.assertEqual((status, out), (200, {"rseq": 0, "events": []}))
        for bad in (room["join_code"], a):
            status, _, out = self.call("GET", f"/r/{code}/records", auth=bad)
            self.assertEqual((status, out["error"]), (403, "forbidden"))
        r1, r2 = _rec("m-1", "2026-09-04T10:00:00Z"), _rec("m-2", "2026-09-04T10:01:00Z")
        self.assertEqual(self._post_ok(room, a, [r1, r2])["accepted"], 2)
        r3 = _rec("m-3", "2026-09-04T10:02:00Z")
        out = self._post_ok(room, b, [r1, r3])
        self.assertEqual((out["accepted"], out["dup"], out["rseq"]), (1, 1, 2))
        _, _, page = self.call("GET", f"/r/{code}/events?after=1", auth=code)
        self.assertEqual([e["id"] for e in page["frames"][0]["events"]], [r3["id"]],
                         "a duplicate record is not stored again, even in the batch row")
        expected = [_expected(r1, "agent-a"), _expected(r2, "agent-a"), _expected(r3, "agent-b")]
        _, _, out = self.call("GET", f"/r/{code}/records", auth=code)
        self.assertEqual(out, {"rseq": 2, "events": expected})
        if self.sse:
            resp = self._stream(room, auth=code)
            frames = [f for _, f in self._until_live(resp)]
            self.assertEqual([f["t"] for f in frames], ["hello", "records", "batch", "batch", "live"])
            self.assertEqual(frames[1]["events"], expected)
        # A newer version of a held rid replaces it; an older one is a dup and
        # rides in no batch row (§6.4).
        newer = _rec("m-1", "2026-09-04T10:05:00Z")
        older = _rec("m-2", "2026-09-04T09:00:00Z")
        out = self._post_ok(room, b, [newer, older])
        self.assertEqual((out["accepted"], out["dup"], out["rseq"]), (1, 1, 3))
        _, _, page = self.call("GET", f"/r/{code}/events?after=2", auth=code)
        self.assertEqual([e["id"] for e in page["frames"][0]["events"]], [newer["id"]],
                         "the stale version is not in the batch row")
        _, _, out = self.call("GET", f"/r/{code}/records", auth=code)
        self.assertEqual([e["id"] for e in out["events"]], [r2["id"], r3["id"], newer["id"]])

        # The cap: 2,200 records in batches of 100 (200 records overflow 64 KiB) from
        # three agents so no bucket runs dry; the 200 with the oldest body.ts go.
        cap_room = self._room()
        a = self._register(cap_room, "agent-a")
        b = self._register(cap_room, "agent-b")
        c = self._register(cap_room, "agent-c")
        oldest = [_rec(f"x-{i}") for i in range(200)]           # no ts: sorts oldest
        self.assertEqual(self._post_ok(cap_room, a, oldest[:100])["accepted"], 100)
        self.assertEqual(self._post_ok(cap_room, a, oldest[100:])["accepted"], 100)
        for batch in range(18):
            token = a if batch < 8 else b
            events = [_rec(f"y-{batch}-{i}", f"2026-09-04T10:{batch:02d}:00Z") for i in range(100)]
            self.assertEqual(self._post_ok(cap_room, token, events)["accepted"], 100)
        newest = [_rec(f"z-{i}", "2026-09-05T00:00:00Z") for i in range(200)]
        self.assertEqual(self._post_ok(cap_room, c, newest[:100])["accepted"], 100)
        self.assertEqual(self._post_ok(cap_room, c, newest[100:])["accepted"], 100)
        _, _, out = self.call("GET", f"/r/{cap_room['room']}/records", auth=cap_room["room"])
        held = {e["id"] for e in out["events"]}
        self.assertEqual(len(held), 2000)
        self.assertFalse(held & {e["id"] for e in oldest}, "the oldest body.ts is evicted first")
        self.assertTrue(held >= {e["id"] for e in newest})

    # -- §5 frame order -------------------------------------------------------

    def test_fresh_connect_order_and_tails(self):
        self._need_sse()
        room = self._room(max_stream="full")
        a = self._register(room, "agent-a")
        b = self._register(room, "agent-b")
        code = room["room"]
        a_rows = [self._post_ok(room, a, [_ev("text", body={"text": f"{n}-{i}"}) for i in range(30)])["rseq"]
                  for n in range(4)]
        b_rows = [self._post_ok(room, b, [_ev("prompt", body={"text": str(i)}) for i in range(5)])["rseq"],
                  self._post_ok(room, b, [_rec(f"b-{i}") for i in range(3)])["rseq"]]
        self.call("PUT", f"/r/{code}/roles/agent-a", auth=code, body={"role": "r", "viewer": "v"})
        _, _, snap = self.call("GET", f"/r/{code}", auth=code)
        resp = self._stream(room, auth=code)
        frames = self._until_live(resp)
        kinds = [f["t"] for _, f in frames]
        self.assertEqual(kinds[0], "hello")
        self.assertEqual(kinds[-1], "live")
        self.assertNotIn("gap", kinds)
        hello = frames[0][1]
        self.assertEqual((hello["backfill"], hello["gap"]), ("tail", None))
        self.assertIn(hello["transport"], ("sse", "ws"))
        self.assertNotIn("rseq", hello)
        self.assertNotIn("rseq", frames[-1][1])
        self.assertEqual(hello["room"], snap, "hello.room is exactly GET /r/{room}")
        self.assertEqual(kinds[1], "records")
        self.assertEqual(len(frames[1][1]["events"]), 3)
        batches = [f for _, f in frames if f["t"] == "batch"]
        self.assertEqual(kinds[2:-1], ["batch"] * len(batches))
        per_agent = {}
        for f in batches:
            per_agent.setdefault(f["agent"], []).append(f["rseq"])
        self.assertEqual(per_agent["agent-a"], a_rows[1:], "newest whole batches until >= 80 positional")
        self.assertEqual(per_agent["agent-b"], b_rows, "records are not positional; both batches fit")
        order = [f["agent"] for f in batches]
        self.assertEqual(order, sorted(order, key=order.index),
                         "one agent's tail completes before the next begins")
        for sse_id, f in frames:
            self.assertEqual(sse_id, f.get("rseq"), "id: rides exactly on frames with an rseq")
        # A kicked agent's tail is still sent and it stays in hello.room.agents.
        self.call("DELETE", f"/r/{code}/agents/agent-b", auth=room["join_code"])
        resp = self._stream(room, auth=code)
        frames = [f for _, f in self._until_live(resp)]
        self.assertEqual([f["rseq"] for f in frames if f["t"] == "batch" and f["agent"] == "agent-b"],
                         b_rows)
        self.assertTrue(next(a for a in frames[0]["room"]["agents"] if a["name"] == "agent-b")["kicked"])

    def test_replay_after_and_last_event_id(self):
        self._need_sse()
        room = self._room()
        token = self._register(room, "agent-a")
        code = room["room"]
        self._post_ok(room, token, [_ev()])                                        # 1
        self._message(room, code, to="agent-a", text="q", viewer="sam")            # 2
        self.call("PUT", f"/r/{code}/roles/agent-a", auth=code, body={"role": "r", "viewer": "v"})  # 3
        self.call("PUT", f"/r/{code}/policy", auth=room["join_code"], body={"retention_min": 60})   # 4
        self._post_ok(room, token, [_ev()])                                        # 5
        resp = self._stream(room, auth=code, after=2)
        frames = self._until_live(resp)
        self.assertEqual([(i, f["t"]) for i, f in frames],
                         [(None, "hello"), (3, "role"), (4, "policy"), (5, "batch"), (None, "live")])
        hello = frames[0][1]
        self.assertEqual((hello["backfill"], hello["gap"], hello["room"]["rseq"]), ("replay", None, 5))
        self.assertEqual(frames[1][1]["role"]["set_seq"], 3)
        self.assertEqual(frames[2][1]["policy"]["retention_min"], 60)
        resp = self._stream(room, auth=code, after=1)
        self.assertEqual([f["t"] for _, f in self._until_live(resp)],
                         ["hello", "message", "role", "policy", "batch", "live"])
        resp = self._stream(room, auth=code, after=5)
        frames = self._until_live(resp)
        self.assertEqual([f["t"] for _, f in frames], ["hello", "live"])
        self.assertEqual(frames[0][1]["backfill"], "none")
        resp = self._stream(room, auth=code, after=99)
        self.assertEqual([f["t"] for _, f in self._until_live(resp)], ["hello", "live"])
        self._local_only("Last-Event-ID is an SSE obligation of the Python relay")
        resp = self._stream(room, auth=code, after=0, headers={"Last-Event-ID": "4"})
        self.assertEqual([f.get("rseq") for _, f in self._until_live(resp)], [None, 5, None],
                         "the header wins over the query")
        resp = self._stream(room, auth=code, after=3, headers={"Last-Event-ID": "abc"})
        self.assertEqual([f.get("rseq") for _, f in self._until_live(resp)], [None, 4, 5, None],
                         "a non-integer header is ignored")
        resp = self._stream(room, auth=code, headers={"Last-Event-ID": "-1"})
        self.assertEqual(self._until_live(resp)[0][1]["backfill"], "tail")

    def test_batch_between_hello_and_live_seen_once(self):
        self._need_sse()
        room = self._room()
        token = self._register(room, "agent-a")
        for _ in range(3):
            self._post_ok(room, token, [_ev() for _ in range(20)])
        resp = self._stream(room, auth=room["room"])
        hello = self._frames(resp, lambda f: True)[0][1]
        self.assertEqual(hello["t"], "hello")
        result = {}

        def poster():
            result["out"] = self._post_ok(room, token, [_ev(body={"live": True})])

        worker = threading.Thread(target=poster)
        worker.start()
        worker.join(10)
        self.assertFalse(worker.is_alive())
        live_rseq = result["out"]["rseq"]
        sentinel = self._post_ok(room, token, [_ev(body={"sentinel": True})])["rseq"]
        frames = [f for _, f in self._frames(resp, lambda f: f.get("rseq") == sentinel)]
        kinds = [f["t"] for f in frames]
        self.assertIn("live", kinds)
        self.assertEqual(sum(1 for f in frames if f.get("rseq") == live_rseq), 1)
        self.assertEqual(kinds[-1], "batch")
        rseqs = [f["rseq"] for f in frames if "rseq" in f]
        self.assertEqual(rseqs, sorted(rseqs))

    # -- §10.1 messages, §3.9 inbox, §6.12 answers ------------------------------

    def test_messages_inbox_and_answers(self):
        room = self._room()
        a = self._register(room, "agent-a")
        b = self._register(room, "agent-b")
        code, join = room["room"], room["join_code"]
        self._post_ok(room, a, [_ev()])                                           # row 1
        m1 = self._message(room, code, to="agent-a", text="why?\x00\n", viewer="sam")
        self.assertEqual(m1, {"id": f"cm-{room['room4']}-2", "seq": 2})
        _, _, page = self.call("GET", f"/r/{code}/events?after=1", auth=code)
        frame = page["frames"][0]
        self.assertEqual(frame["t"], "message")
        ask = frame["message"]
        self.assertRegex(ask["ts"], ISO_MS)
        self.assertEqual(ask, {"id": m1["id"], "seq": 2, "kind": "ask", "to": "agent-a",
                               "from": {"kind": "viewer", "name": "sam"}, "text": "why?",
                               "ts": ask["ts"], "state": "open", "answer": None, "wake": NO_WAKE},
                         "a viewer's message to a name defaults to kind ask")
        self.assertEqual(frame["rseq"], ask["seq"])

        status, _, inbox = self.call("GET", f"/r/{code}/inbox?after=0", auth=a)
        self.assertEqual(status, 200)
        self.assertEqual(inbox, {"rseq": 2, "role": None, "messages": [ask]})
        _, _, other = self.call("GET", f"/r/{code}/inbox?after=0", auth=b)
        self.assertEqual(other["messages"], [], "a message to a name reaches only that agent")
        status, _, out = self.call("GET", f"/r/{code}/inbox?after=x", auth=a)
        self.assertEqual((status, out["error"]), (400, "schema"))
        status, _, out = self.call("GET", f"/r/{code}/inbox?after=0", auth=code)
        self.assertEqual((status, out["error"]), (403, "forbidden"))

        # An agent's line to the room: from is the token's name, kind defaults
        # to say, viewer is ignored and wake is stored false for "*".
        m2 = self._message(room, b, to="*", text="hi all", wake=True, viewer="ignored")
        self.assertEqual(m2["seq"], 3)
        say = self._find(room, m2["id"])
        self.assertEqual((say["kind"], say["to"], say["from"], say["state"], say["wake"]),
                         ("say", "*", {"kind": "agent", "name": "agent-b"}, "sent", NO_WAKE))
        m3 = self._message(room, b, to="agent-a", kind="ping", text="look at #4")
        self.assertEqual(m3["seq"], 4)
        _, _, second = self.call("GET", f"/r/{code}/inbox?after={inbox['rseq']}", auth=a)
        self.assertEqual([x["id"] for x in second["messages"]], [m2["id"], m3["id"]],
                         "the cursor is the returned rseq; * and the name both arrive")
        self.assertEqual((second["rseq"], second["messages"][1]["state"]), (4, "sent"))
        _, _, b_inbox = self.call("GET", f"/r/{code}/inbox?after=0", auth=b)
        self.assertEqual([x["id"] for x in b_inbox["messages"]], [m2["id"]],
                         "* reaches everyone, a name only its agent")
        _, _, third = self.call("GET", f"/r/{code}/inbox?after={second['rseq']}", auth=a)
        self.assertEqual(third["messages"], [])
        _, _, snap = self.call("GET", f"/r/{code}", auth=code)
        self.assertEqual([x["id"] for x in snap["messages"]], [m1["id"], m2["id"], m3["id"]])

        # answers: wrong agent, unknown id, non-string, absent, not an ask -> schema
        wrong = _ev("answer", ref=m1["id"], body={"ask": m1["id"], "text": "not mine"})
        unknown = _ev("answer", body={"ask": f"cm-{room['room4']}-999", "text": "?"})
        untyped = _ev("answer", body={"ask": 2, "text": "?"})
        absent = _ev("answer", body={"text": "?"})
        not_an_ask = _ev("answer", body={"ask": m3["id"], "text": "a ping takes no answer"})
        out = self._post_ok(room, b, [wrong])
        self.assertEqual(out["rejected"], [{"id": wrong["id"], "why": "schema"}])
        out = self._post_ok(room, a, [unknown, untyped, absent, not_an_ask])
        self.assertEqual([r["why"] for r in out["rejected"]], ["schema"] * 4)
        answer = _ev("answer", ref=m1["id"], ts="2026-09-04T10:01:00.000Z",
                     body={"ask": m1["id"], "text": "because"})
        out = self._post_ok(room, a, [answer, _ev()])
        self.assertEqual((out["rseq"], out["accepted"]), (5, 2), "the 202 rseq is the batch row's")
        _, _, page = self.call("GET", f"/r/{code}/events?after=4", auth=code)
        self.assertEqual([(f["rseq"], f["t"]) for f in page["frames"]], [(5, "batch"), (6, "message")])
        answered = page["frames"][1]["message"]
        self.assertEqual(answered, dict(ask, state="answered",
                                        answer={"text": "because", "ts": "2026-09-04T10:01:00.000Z"}),
                         "a state change is a new row keeping id and seq")
        self.assertEqual(page["frames"][0]["events"][0]["body"]["ask"], m1["id"],
                         "the answer event itself is stored in the batch row")
        _, _, page = self.call("GET", f"/r/{code}/events?after=1&limit=1", auth=code)
        self.assertEqual(page["frames"][0]["message"]["state"], "open",
                         "the creation row keeps the state it announced")
        _, _, inbox = self.call("GET", f"/r/{code}/inbox?after=0", auth=a)
        self.assertEqual([(x["id"], x["state"]) for x in inbox["messages"]],
                         [(m1["id"], "answered"), (m2["id"], "sent"), (m3["id"], "sent")],
                         "the inbox carries every message, in its current state")
        again = _ev("answer", body={"ask": m1["id"], "text": "twice"})
        out = self._post_ok(room, a, [again])
        self.assertEqual((out["rseq"], out["rejected"]), (6, [{"id": again["id"], "why": "schema"}]),
                         "only an open ask resolves (§10.1)")
        if self.sse:
            resp = self._stream(room, auth=code, after=4)
            frames = [f for _, f in self._until_live(resp)]
            self.assertEqual([(f.get("rseq"), f["t"]) for f in frames],
                             [(None, "hello"), (5, "batch"), (6, "message"), (None, "live")])
            self.assertEqual(frames[2]["message"], answered)
            self.assertEqual(frames[0]["room"]["messages"], self._messages(room))
        # 404 agent for a kicked or unknown target; the join code cannot post; * survives a kick
        self.call("DELETE", f"/r/{code}/agents/agent-b", auth=join)
        for to in ("agent-b", "nobody"):
            status, _, out = self.call("POST", f"/r/{code}/messages", auth=code,
                                       body={"to": to, "text": "t", "viewer": "sam-" + to})
            self.assertEqual((status, out["error"]), (404, "agent"), to)
        status, _, out = self.call("POST", f"/r/{code}/messages", auth=join,
                                   body={"to": "agent-a", "text": "t", "viewer": "sam4"})
        self.assertEqual((status, out["error"]), (403, "forbidden"))
        self._message(room, code, to="*", text="t", viewer="sam5")

    def test_message_limits(self):
        room = self._room()
        a = self._register(room, "agent-a")
        code = room["room"]
        good = {"to": "agent-a", "text": "t", "viewer": "v"}
        for bad in ({"text": "t", "viewer": "v"}, {"to": "agent-a", "viewer": "v"},
                    {"to": "agent-a", "text": "t"}, {"to": "agent-a", "text": "t", "viewer": ""},
                    {"to": "agent-a", "text": "t", "viewer": "\x01\x02"},
                    {"to": "agent-a", "text": "t", "viewer": "v" * 41},
                    {"to": "agent-a", "text": "x" * 2001, "viewer": "v"},
                    {"to": "agent-a", "text": "", "viewer": "v"},
                    {"to": "agent-a", "text": "\x00\t", "viewer": "v"},
                    {"to": 5, "text": "t", "viewer": "v"}, {"to": "agent-a", "text": 5, "viewer": "v"},
                    dict(good, kind="shout"), dict(good, kind=3), dict(good, wake="yes"),
                    dict(good, wake=1)):
            status, _, out = self.call("POST", f"/r/{code}/messages", auth=code, body=bad)
            self.assertEqual((status, out["error"]), (400, "schema"), bad)
        status, _, out = self.call("GET", f"/r/{code}", auth=code)
        self.assertEqual((out["rseq"], out["messages"]), (0, []), "a refused message has no side effect")
        first = self._message(room, code, to="agent-a", text="é" * 2000, viewer="🙂" * 40)
        # say to a named agent is legal and is never open
        fyi = self._message(room, code, to="agent-a", kind="say", text="fyi", viewer="w")
        self.assertEqual((self._find(room, fyi["id"])["kind"], self._find(room, fyi["id"])["state"]),
                         ("say", "sent"))
        status, headers, out = self.call("POST", f"/r/{code}/messages", auth=code,
                                         body=dict(good, viewer="🙂" * 40))
        self.assertEqual((status, out["error"]), (429, "rate"), "one message per viewer name per 5 s")
        self.assertGreaterEqual(int(headers["Retry-After"]), 1)
        # an agent posts ten a minute
        for i in range(10):
            self._message(room, a, to="*", text=str(i))
        status, headers, out = self.call("POST", f"/r/{code}/messages", auth=a,
                                         body={"to": "*", "text": "eleven"})
        self.assertEqual((status, out["error"]), (429, "rate"), "ten messages a minute per agent")
        self.assertGreaterEqual(int(headers["Retry-After"]), 1)
        # 200 open asks fill the room; pings and says are not capped by state
        for i in range(199):
            self._message(room, code, to="agent-a", text="t", viewer=f"viewer-{i}")
        status, _, out = self.call("POST", f"/r/{code}/messages", auth=code,
                                   body=dict(good, viewer="one-too-many"))
        self.assertEqual((status, out["error"]), (503, "full"))
        last = self._message(room, code, to="agent-a", kind="ping", text="still fits", viewer="pinger")
        _, _, snap = self.call("GET", f"/r/{code}", auth=code)
        self.assertEqual(len(snap["messages"]), 100, "the snapshot carries the newest 100")
        seqs = [x["seq"] for x in snap["messages"]]
        self.assertEqual((seqs, seqs[-1]), (sorted(seqs), last["seq"]))
        # GET /messages pages ascending by seq
        total = 1 + 1 + 10 + 199 + 1
        _, _, page = self.call("GET", f"/r/{code}/messages?after=0&limit=200", auth=code)
        self.assertEqual((len(page["messages"]), page["more"], page["rseq"]), (200, True, total))
        self.assertEqual(page["messages"][0]["id"], first["id"])
        _, _, rest = self.call("GET", f"/r/{code}/messages?after={page['messages'][-1]['seq']}", auth=code)
        self.assertEqual(([x["seq"] for x in rest["messages"]], rest["more"]),
                         (list(range(201, total + 1)), False))
        _, _, page = self.call("GET", f"/r/{code}/messages", auth=code)
        self.assertEqual((len(page["messages"]), page["more"]), (50, True), "after defaults to 0, limit to 50")
        for query in ("after=x", "after=-1", "limit=0", "limit=201", "limit=x"):
            status, _, out = self.call("GET", f"/r/{code}/messages?{query}", auth=code)
            self.assertEqual((status, out["error"]), (400, "schema"), query)
        for auth in (room["join_code"], a):
            status, _, out = self.call("GET", f"/r/{code}/messages?after=0", auth=auth)
            self.assertEqual((status, out["error"]), (403, "forbidden"))

    def test_message_expiry_and_retention(self):
        room = self._room(ask_ttl_s=1)
        token = self._register(room, "agent-a")
        code, join = room["room"], room["join_code"]
        ask = self._message(room, code, to="agent-a", text="t", viewer="v")
        ping = self._message(room, token, to="*", kind="ping", text="p")
        _, _, inbox = self.call("GET", f"/r/{code}/inbox?after=0", auth=token)
        self.assertEqual([(x["id"], x["state"]) for x in inbox["messages"]],
                         [(ask["id"], "open"), (ping["id"], "sent")], "open until a pass runs")
        time.sleep(1.2)
        status, _, out = self.call("POST", f"/r/{code}/prune", auth=join)       # absent body
        self.assertEqual((status, out), (204, b""))
        _, _, page = self.call("GET", f"/r/{code}/events?after={ping['seq']}", auth=code)
        self.assertEqual(len(page["frames"]), 1)
        expired = page["frames"][0]
        self.assertEqual((expired["t"], expired["message"]["state"], expired["message"]["id"],
                          expired["message"]["seq"], expired["message"]["answer"]),
                         ("message", "expired", ask["id"], ask["seq"], None))
        self.assertGreater(expired["rseq"], ask["seq"])
        _, _, inbox = self.call("GET", f"/r/{code}/inbox?after=0", auth=token)
        self.assertEqual([(x["id"], x["state"]) for x in inbox["messages"]],
                         [(ask["id"], "expired"), (ping["id"], "sent")],
                         "expiry changes the state and deletes nothing")
        late = _ev("answer", body={"ask": ask["id"], "text": "late"})
        out = self._post_ok(room, token, [late])
        self.assertEqual(out["rejected"], [{"id": late["id"], "why": "schema"}],
                         "an expired ask is not open, so it takes no answer")
        status, _, out = self.call("POST", f"/r/{code}/prune", auth=join, raw=b"")
        self.assertEqual(status, 204)
        self._local_only("seven days of message retention need the clock moved")
        self._advance(6 * 86_400)
        self._post_ok(room, token, [_ev()])                     # keeps the room alive
        self._advance(2 * 86_400)
        status, _, _ = self.call("POST", f"/r/{code}/prune", auth=join)
        self.assertEqual(status, 204)
        status, _, snap = self.call("GET", f"/r/{code}", auth=code)
        self.assertEqual((status, snap["messages"]), (200, []),
                         "seven days from ts, whatever the kind or state")

    # -- §10.2 owner tokens, §10.3 wake settings ------------------------------

    def test_owner_token_and_wake_settings(self):
        room = self._room()
        code = room["room"]
        reg_a, reg_b = self._register_out(room, "agent-a"), self._register_out(room, "agent-b")
        a, owner_a, b, owner_b = reg_a["token"], reg_a["owner_token"], reg_b["token"], reg_b["owner_token"]
        self.assertRegex(owner_a, canvas_relay.OWNER_RE)
        path = f"/r/{code}/agents/agent-a/wake"
        _, _, snap = self.call("GET", f"/r/{code}", auth=code)
        self.assertEqual(snap["agents"][0]["wake"], DEFAULT_WAKE)
        if self.sse:
            resp = self._stream(room, auth=code)
            self._until_live(resp)
        status, _, out = self.call("PUT", path, auth=owner_a, body={"enabled": True, "from": "room"})
        self.assertEqual(status, 200, out)
        self.assertRegex(out["wake"]["ts"], ISO_MS)
        self.assertEqual(out["wake"], dict(DEFAULT_WAKE, enabled=True, **{"from": "room"},
                                           set_by="owner", ts=out["wake"]["ts"]))
        if self.sse:
            _, frame = self._frames(resp, lambda f: f.get("t") == "agent")[-1]
            self.assertNotIn("rseq", frame)
            self.assertEqual((frame["agent"]["name"], frame["agent"]["wake"]), ("agent-a", out["wake"]),
                             "a settings change emits the agent object")
        status, _, out = self.call("PUT", path, auth=a, body={"max_per_hour": 2})
        self.assertEqual(status, 200, out)
        self.assertEqual((out["wake"]["enabled"], out["wake"]["from"], out["wake"]["max_per_hour"],
                          out["wake"]["set_by"]), (True, "room", 2, "agent"))
        _, _, snap = self.call("GET", f"/r/{code}", auth=code)
        self.assertEqual(snap["agents"][0]["wake"], out["wake"])
        for wrong in (code, room["join_code"], b, owner_b):
            status, _, out = self.call("PUT", path, auth=wrong, body={"enabled": False})
            self.assertEqual((status, out["error"]), (403, "forbidden"), wrong[:3])
        for bad in ({}, {"other": 1}, {"enabled": "yes"}, {"enabled": 1}, {"from": "anyone"},
                    {"max_per_hour": 0}, {"max_per_hour": 61}, {"max_per_hour": "4"}, [1]):
            status, _, out = self.call("PUT", path, auth=owner_a, body=bad)
            self.assertEqual((status, out["error"]), (400, "schema"), bad)
        # exactly three routes: wake, roles and leave, for this name only
        for method, route, body in (("GET", "/inbox?after=0", None), ("GET", "", None),
                                    ("GET", "/events?after=0", None), ("GET", "/records", None),
                                    ("GET", "/messages?after=0", None), ("POST", "/ticket", {}),
                                    ("POST", "/events", {}), ("POST", "/wake-ack", {}),
                                    ("POST", "/messages", {"to": "*", "text": "t"}),
                                    ("POST", "/prune", {}), ("PUT", "/policy", {"max_stream": "full"}),
                                    ("POST", "/agents/agent-c", {}), ("DELETE", "", None)):
            status, _, out = self.call(method, f"/r/{code}{route}", auth=owner_a, body=body)
            self.assertEqual((status, out["error"]), (403, "forbidden"), (method, route))
        status, _, out = self.call("PUT", f"/r/{code}/roles/agent-a", auth=owner_a, body={"role": "tester"})
        self.assertEqual((status, out), (200, {"set_seq": 1}))
        _, _, page = self.call("GET", f"/r/{code}/events?after=0", auth=code)
        self.assertEqual(page["frames"][0]["role"]["viewer"], "owner",
                         "the owner token forces viewer to owner")
        status, _, out = self.call("PUT", f"/r/{code}/roles/agent-b", auth=owner_a, body={"role": "x"})
        self.assertEqual((status, out["error"]), (403, "forbidden"))
        # rotated with the agent token; the new one works
        renewed = self._register_out(room, "agent-a")
        status, _, out = self.call("PUT", path, auth=owner_a, body={"enabled": False})
        self.assertEqual((status, out["error"]), (401, "auth"))
        status, _, out = self.call("PUT", path, auth=renewed["owner_token"], body={"enabled": False})
        self.assertEqual((status, out["wake"]["enabled"], out["wake"]["set_by"]), (200, False, "owner"))
        status, _, out = self.call("DELETE", f"/r/{code}/agents/agent-b", auth=renewed["owner_token"])
        self.assertEqual((status, out["error"]), (403, "forbidden"))
        status, _, out = self.call("DELETE", f"/r/{code}/agents/agent-a", auth=renewed["owner_token"])
        self.assertEqual((status, out), (204, b""))
        status, _, out = self.call("PUT", path, auth=renewed["owner_token"], body={"enabled": True})
        self.assertEqual((status, out["error"]), (401, "auth"), "leaving invalidates the owner token")

    # -- §10.1 wake states, §10.4 wake-acks -------------------------------------

    def test_wake_states_and_acks(self):
        room = self._room()
        code = room["room"]
        a = self._register(room, "agent-a")
        b = self._register(room, "agent-b")
        path = f"/r/{code}/agents/agent-a/wake"
        viewers = (f"viewer-{i}" for i in itertools.count())

        def ping(auth, **over):
            body = {"to": "agent-a", "kind": "ping", "text": "look", "wake": True}
            if auth == code:
                body["viewer"] = next(viewers)
            body.update(over)
            return self._find(room, self._message(room, auth, **body)["id"])

        def wake_of(message):
            return (message["wake"]["state"], message["wake"]["reason"])

        self.assertEqual(ping(code)["wake"], {"requested": True, "state": "off", "reason": None,
                                              "ts": None}, "wake never enabled: off")
        self.call("PUT", path, auth=a, body={"enabled": True})
        self.assertEqual(wake_of(ping(code)), ("declined", "sender not allowed"),
                         "from: agents -- a viewer may not wake this agent")
        nobody = ping(b)
        self.assertEqual(wake_of(nobody), ("nobody", None), "enabled, but no listener connected")
        self.call("PUT", path, auth=a, body={"from": "room"})
        self.assertEqual(wake_of(ping(code)), ("nobody", None))
        self.assertEqual(wake_of(ping(code, wake=False)), ("none", None))
        status, _, out = self.call("POST", f"/r/{code}/wake-ack", auth=a,
                                   body={"message": nobody["id"], "result": "woke"})
        self.assertEqual((status, out["error"]), (400, "schema"), "only pending or busy takes an ack")
        self._need_sse()
        listener = self._agent_stream(room, a)
        self._frames(listener, lambda f: f["t"] == "agent")      # hello, then the listener flip
        pending = ping(code)
        self.assertEqual(wake_of(pending), ("pending", None))
        frames = self._until(listener, "wake")
        self.assertEqual([f["t"] for f in frames[-2:]], ["message", "wake"])
        self.assertEqual((frames[-1]["rseq"], frames[-1]["message"], frames[-1]["settings"]["enabled"]),
                         (pending["seq"], pending, True))
        for bad in ({"message": pending["id"], "result": "napped"}, {"message": 5, "result": "woke"},
                    {"result": "woke"}, {"message": pending["id"], "result": "woke", "reason": "r" * 201},
                    {"message": pending["id"], "result": "woke", "reason": 5}):
            status, _, out = self.call("POST", f"/r/{code}/wake-ack", auth=a, body=bad)
            self.assertEqual((status, out["error"]), (400, "schema"), bad)
        status, _, out = self.call("POST", f"/r/{code}/wake-ack", auth=b,
                                   body={"message": pending["id"], "result": "woke"})
        self.assertEqual((status, out["error"]), (400, "schema"), "not addressed to that token")
        self.assertEqual(wake_of(self._find(room, pending["id"])), ("pending", None),
                         "a refused ack changes nothing")
        # busy, then woke: two rows, id and seq kept, used_this_hour counts the woke
        status, _, out = self.call("POST", f"/r/{code}/wake-ack", auth=a,
                                   body={"message": pending["id"], "result": "busy",
                                         "reason": "a session is running"})
        self.assertEqual(status, 200, out)
        busy = self._find(room, pending["id"])
        self.assertEqual(wake_of(busy), ("busy", "a session is running"))
        self.assertRegex(busy["wake"]["ts"], ISO_MS)
        _, _, page = self.call("GET", f"/r/{code}/events?after={pending['seq']}", auth=code)
        row = page["frames"][-1]
        self.assertEqual((row["t"], row["message"]), ("message", busy))
        self.assertGreater(row["rseq"], pending["seq"])
        _, _, snap = self.call("GET", f"/r/{code}", auth=code)
        self.assertEqual(snap["agents"][0]["wake"]["used_this_hour"], 0)
        status, _, out = self.call("POST", f"/r/{code}/wake-ack", auth=a,
                                   body={"message": pending["id"], "result": "woke", "reason": None})
        self.assertEqual(status, 200, out)
        self.assertEqual(wake_of(self._find(room, pending["id"])), ("woke", None))
        _, _, snap = self.call("GET", f"/r/{code}", auth=code)
        self.assertEqual(snap["agents"][0]["wake"]["used_this_hour"], 1)
        status, _, out = self.call("POST", f"/r/{code}/wake-ack", auth=a,
                                   body={"message": pending["id"], "result": "woke"})
        self.assertEqual((status, out["error"]), (400, "schema"), "woke is final")
        # the hourly cap: busy at creation, no wake push
        self.call("PUT", path, auth=a, body={"max_per_hour": 1})
        capped = ping(code)
        self.assertEqual(wake_of(capped), ("busy", "hourly cap"))
        sentinel = self._message(room, b, to="*", text="sentinel")
        frames = [f for _, f in self._frames(listener, lambda f: f.get("rseq") == sentinel["seq"])]
        # Agent frames (used_this_hour, then the settings change) and the settings
        # wake frame are legitimate here; what must not appear is a wake frame
        # carrying the capped message.
        self.assertEqual([f["message"] for f in frames if f["t"] == "wake"], [None],
                         "the capped ping gets no wake push; only the settings change did")
        self.assertEqual([f["message"]["id"] for f in frames if f["t"] == "message"][-4:],
                         [pending["id"], pending["id"], capped["id"], sentinel["id"]],
                         "both acks and the capped ping reach the listener as message rows")
        if self.relay is not None:
            self._advance(3600)
            _, _, snap = self.call("GET", f"/r/{code}", auth=code)
            self.assertEqual(snap["agents"][0]["wake"]["used_this_hour"], 0, "the window resets on the hour")
            self.assertEqual(wake_of(ping(code)), ("pending", None))

    # -- §10.5 the agent stream ---------------------------------------------------

    def test_agent_stream(self):
        self._need_sse()
        room = self._room()
        code, join = room["room"], room["join_code"]
        reg_a = self._register_out(room, "agent-a")
        a = reg_a["token"]
        b = self._register(room, "agent-b")
        _, _, minted = self.call("POST", f"/r/{code}/ticket", auth=code)
        for kw, expected in (({}, 401), ({"ticket": minted["ticket"]}, 401), ({"auth": code}, 403),
                             ({"auth": join}, 403), ({"auth": reg_a["owner_token"]}, 403),
                             ({"auth": "at-" + _sym(32)}, 401)):
            status, body = self._stream_error(room, path="agent-stream", **kw)
            self.assertEqual((status, body["error"]), (expected, "auth" if expected == 401 else "forbidden"),
                             kw)
        viewer = self._stream(room, auth=code)
        self._until_live(viewer)
        listener = self._agent_stream(room, a)
        sse_id, hello = self._frames(listener, lambda f: True)[0]
        self.assertEqual((hello["t"], sse_id), ("hello", None))
        self.assertNotIn("rseq", hello)
        self.assertIn(hello["transport"], ("sse", "ws"))
        self.assertTrue({"agent", "policy"} <= set(hello))
        self.assertEqual((hello["agent"]["name"], hello["policy"]), ("agent-a", room["policy"]))
        self.assertEqual(hello["agent"]["wake"]["listener"], "connected")
        for resp in (viewer, listener):
            _, frame = self._frames(resp, lambda f: f["t"] == "agent")[-1]
            self.assertEqual((frame["agent"]["name"], frame["agent"]["wake"]["listener"]),
                             ("agent-a", "connected"), "the first socket flips listener")
        self.call("PUT", f"/r/{code}/agents/agent-a/wake", auth=a, body={"enabled": True, "from": "room"})
        frames = self._until(listener, "wake")
        self.assertEqual([f["t"] for f in frames], ["agent", "wake"])
        self.assertEqual((frames[1]["message"], frames[1]["settings"]["enabled"]), (None, True),
                         "a settings change reaches the listener as a wake frame with no message")
        ping = self._message(room, code, to="agent-a", kind="ping", text="look", viewer="sam", wake=True)
        say = self._message(room, b, to="*", text="all")
        self._message(room, code, to="agent-b", text="not for a", viewer="pat")
        self.call("PUT", f"/r/{code}/roles/agent-b", auth=code, body={"role": "r", "viewer": "v"})
        _, _, role = self.call("PUT", f"/r/{code}/roles/agent-a", auth=code, body={"role": "r", "viewer": "v"})
        self.call("PUT", f"/r/{code}/policy", auth=join, body={"retention_min": 60})
        frames = [(i, f) for i, f in self._frames(listener, lambda f: f["t"] == "policy")]
        self.assertEqual([(i, f["t"]) for i, f in frames],
                         [(ping["seq"], "message"), (ping["seq"], "wake"), (say["seq"], "message"),
                          (role["set_seq"], "role"), (role["set_seq"] + 1, "policy")],
                         "only what concerns this agent, plus every policy frame")
        pair = [f for _, f in frames[:2]]
        self.assertEqual((pair[0]["message"]["wake"]["state"], pair[1]["message"],
                          pair[1]["settings"]["listener"]), ("pending", pair[0]["message"], "connected"))
        self.assertEqual(frames[3][1]["agent"], "agent-a")
        kinds = [f["t"] for _, f in self._frames(viewer, lambda f: f["t"] == "policy")]
        self.assertEqual(kinds, ["agent", "message", "message", "message", "role", "role", "policy"],
                         "viewers see every row and never a wake frame")
        # a second socket for the same name: both get the pair, no second flip
        second = self._agent_stream(room, a)
        self.assertEqual(self._frames(second, lambda f: True)[0][1]["t"], "hello")
        again = self._message(room, b, to="agent-a", kind="ping", text="again", wake=True)
        for resp in (listener, second):
            frames = self._until(resp, "wake")
            self.assertEqual([(f["t"], f["rseq"]) for f in frames],
                             [("message", again["seq"]), ("wake", again["seq"])])
        second.close()
        time.sleep(0.3)
        marker = self._message(room, code, to="agent-b", text="marker", viewer="marker")
        frames = [f for _, f in self._frames(viewer, lambda f: f.get("rseq") == marker["seq"])]
        self.assertEqual([f["t"] for f in frames], ["message", "message"],
                         "no flip while one socket stays open")
        listener.close()
        _, frame = self._frames(viewer, lambda f: f["t"] == "agent")[-1]
        self.assertEqual((frame["agent"]["name"], frame["agent"]["wake"]["listener"]),
                         ("agent-a", "absent"), "the last socket closing flips listener back")
        self.assertEqual(self._find(room, self._message(room, b, to="agent-a", kind="ping",
                                                         text="anyone?", wake=True)["id"])["wake"]["state"],
                         "nobody")
        # four sockets per name; the fifth is told full and closed
        held = [self._agent_stream(room, a) for _ in range(4)]
        for resp in held:
            self.assertEqual(self._frames(resp, lambda f: True)[0][1]["t"], "hello")
        extra = self._agent_stream(room, a)
        _, frame = self._frames(extra, lambda f: True)[0]
        self.assertEqual(frame, {"t": "full"})
        self.assertTrue(self._at_eof(extra), "full is followed by close")

    # -- §3.11 roles ----------------------------------------------------------

    def test_roles(self):
        room = self._room()
        reg = self._register_out(room, "agent-a")
        token, owner = reg["token"], reg["owner_token"]
        other = self._register(room, "agent-b")
        code = room["room"]
        path = f"/r/{code}/roles/agent-a"
        status, _, out = self.call("PUT", path, auth=code, body={"role": None, "viewer": "v"})
        self.assertEqual((status, out), (200, {"set_seq": 0}), "clearing a null role on a fresh agent")
        for bad in ({"role": "r"}, {"role": "r", "viewer": ""}, {"role": "r", "viewer": "v" * 41},
                    {"role": "r" * 61, "viewer": "v"}, {"role": 5, "viewer": "v"}):
            status, _, out = self.call("PUT", path, auth=code, body=bad)
            self.assertEqual((status, out["error"]), (400, "schema"), bad)
        status, _, out = self.call("PUT", path, auth=code,
                                   body={"role": "re\x00view\ter — read PRs\x9f", "viewer": "me\x1fhar"})
        self.assertEqual((status, out), (200, {"set_seq": 1}), "set_seq is the role frame's rseq")
        _, _, page = self.call("GET", f"/r/{code}/events?after=0", auth=code)
        frame = page["frames"][0]
        self.assertEqual(frame["t"], "role")
        self.assertEqual(frame["agent"], "agent-a")
        self.assertEqual(frame["role"], {"role": "reviewer — read PRs", "viewer": "mehar",
                                         "set_seq": 1, "ts": frame["role"]["ts"]})
        _, _, snap = self.call("GET", f"/r/{code}", auth=code)
        self.assertEqual(snap["agents"][0]["role"], frame["role"])
        _, _, inbox = self.call("GET", f"/r/{code}/inbox?after=0", auth=token)
        self.assertEqual(inbox["role"], frame["role"])
        status, _, out = self.call("PUT", path, auth=code,
                                   body={"role": "reviewer — read PRs", "viewer": "someone-else"})
        self.assertEqual((status, out), (200, {"set_seq": 1}), "unchanged text is a no-op")
        status, headers, out = self.call("PUT", path, auth=code, body={"role": "other", "viewer": "v"})
        self.assertEqual((status, out["error"]), (429, "rate"), "one change per agent per 30 s")
        self.assertGreaterEqual(int(headers["Retry-After"]), 1)
        status, _, out = self.call("PUT", path, auth=token, body={"role": "other"})
        self.assertEqual((status, out["error"]), (429, "rate"), "the agent's own token is capped too")
        status, _, out = self.call("PUT", path, auth=owner, body={"role": "other"})
        self.assertEqual((status, out["error"]), (429, "rate"), "and so is the owner token")
        status, _, out = self.call("PUT", path, auth=other, body={"role": "x"})
        self.assertEqual((status, out["error"]), (403, "forbidden"))
        status, _, out = self.call("PUT", path, auth=room["join_code"], body={"role": "x", "viewer": "v"})
        self.assertEqual((status, out["error"]), (403, "forbidden"))
        status, _, out = self.call("PUT", f"/r/{code}/roles/nobody", auth=code,
                                   body={"role": "x", "viewer": "v"})
        self.assertEqual((status, out["error"]), (404, "agent"))
        _, _, page = self.call("GET", f"/r/{code}/events?after=0", auth=code)
        self.assertEqual(len(page["frames"]), 1, "no-ops and refusals write nothing")
        self._advance(31, sleep_ok=False)
        status, _, out = self.call("PUT", path, auth=owner, body={"role": "tester"})
        self.assertEqual((status, out), (200, {"set_seq": 2}))
        _, _, page = self.call("GET", f"/r/{code}/events?after=1", auth=code)
        self.assertEqual(page["frames"][0]["role"]["viewer"], "owner",
                         "the owner token forces viewer to owner")
        self._advance(31)
        status, _, out = self.call("PUT", path, auth=code, body={"role": "", "viewer": "v"})
        self.assertEqual((status, out), (200, {"set_seq": 3}))
        _, _, page = self.call("GET", f"/r/{code}/events?after=2", auth=code)
        self.assertEqual(page["frames"][0], {"t": "role", "rseq": 3, "agent": "agent-a", "role": None})
        status, _, out = self.call("PUT", path, auth=token, body={"role": None})
        self.assertEqual((status, out), (200, {"set_seq": 3}),
                         "clearing an already-null role reports the last role frame's rseq")
        _, _, snap = self.call("GET", f"/r/{code}", auth=code)
        self.assertIsNone(snap["agents"][0]["role"])
        self._advance(31)
        status, _, out = self.call("PUT", path, auth=token, body={"role": "🙂" * 60})
        self.assertEqual(status, 200, out)
        _, _, page = self.call("GET", f"/r/{code}/events?after=3", auth=code)
        self.assertEqual(page["frames"][0]["role"]["viewer"], "owner",
                         "the agent's own token forces viewer to owner as well")

    # -- §3.12 policy ---------------------------------------------------------

    def test_policy_route(self):
        room = self._room()
        code, join = room["room"], room["join_code"]
        for bad in ({}, {"other": 1}, {"max_stream": "off"}, {"retention_min": 0},
                    {"retention_min": "60"}, {"ask_ttl_s": 604801}, [1]):
            status, _, out = self.call("PUT", f"/r/{code}/policy", auth=join, body=bad)
            self.assertEqual((status, out["error"]), (400, "schema"), bad)
        status, _, out = self.call("PUT", f"/r/{code}/policy", auth=join, body={"max_stream": "tools"})
        self.assertEqual((status, out), (200, {"policy": room["policy"]}))
        _, _, page = self.call("GET", f"/r/{code}/events?after=0", auth=code)
        self.assertEqual(page["frames"], [{"t": "policy", "rseq": 1, "policy": room["policy"]}],
                         "a row and a frame even when nothing changes")
        status, _, out = self.call("PUT", f"/r/{code}/policy", auth=join,
                                   body={"max_stream": "summary", "ticket_ttl_s": 5, "extra": 1})
        expected = dict(room["policy"], max_stream="summary", ticket_ttl_s=5)
        self.assertEqual((status, out), (200, {"policy": expected}))
        _, _, snap = self.call("GET", f"/r/{code}", auth=code)
        self.assertEqual((snap["policy"], snap["rseq"]), (expected, 2))
        _, _, minted = self.call("POST", f"/r/{code}/ticket", auth=code)
        self.assertEqual(minted["ttl"], 5)

    # -- §3.5 ----------------------------------------------------------------

    def test_events_query(self):
        room = self._room()
        a = self._register(room, "agent-a")
        b = self._register(room, "agent-b")
        code = room["room"]
        rows = [self._post_ok(room, a, [_ev()])["rseq"] for _ in range(3)]      # 1 2 3
        self._message(room, code, to="agent-a", text="q", viewer="v")              # 4
        rows.append(self._post_ok(room, b, [_ev()])["rseq"])                       # 5
        rows.append(self._post_ok(room, a, [_ev()])["rseq"])                       # 6
        for query in ("", "after=0&before=1", "after=x", "before=-1", "after=0&limit=0",
                      "after=0&limit=201", "after=0&limit=x"):
            status, _, out = self.call("GET", f"/r/{code}/events?{query}", auth=code)
            self.assertEqual((status, out["error"]), (400, "schema"), query)
        _, _, out = self.call("GET", f"/r/{code}/events?after=0", auth=code)
        self.assertEqual(([f["rseq"] for f in out["frames"]], out["more"], out["rseq"]),
                         ([1, 2, 3, 4, 5, 6], False, 6))
        self.assertEqual(out["frames"][3]["t"], "message")
        _, _, out = self.call("GET", f"/r/{code}/events?after=1&limit=2", auth=code)
        self.assertEqual(([f["rseq"] for f in out["frames"]], out["more"]), ([2, 3], True))
        _, _, out = self.call("GET", f"/r/{code}/events?before=6&limit=2", auth=code)
        self.assertEqual(([f["rseq"] for f in out["frames"]], out["more"]), ([4, 5], True))
        _, _, out = self.call("GET", f"/r/{code}/events?before=3&limit=5", auth=code)
        self.assertEqual(([f["rseq"] for f in out["frames"]], out["more"]), ([1, 2], False))
        _, _, out = self.call("GET", f"/r/{code}/events?after=0&agent=agent-a", auth=code)
        self.assertEqual([f["rseq"] for f in out["frames"]], [1, 2, 3, 6], "batch rows of that agent only")
        _, _, out = self.call("GET", f"/r/{code}/events?before=7&agent=agent-a&limit=1", auth=code)
        self.assertEqual(([f["rseq"] for f in out["frames"]], out["more"]), ([6], True),
                         "more counts only matching rows")
        _, _, out = self.call("GET", f"/r/{code}/events?after=0&agent=nobody", auth=code)
        self.assertEqual(out, {"rseq": 6, "frames": [], "more": False})
        self.call("DELETE", f"/r/{code}/agents/agent-b", auth=room["join_code"])
        _, _, out = self.call("GET", f"/r/{code}/events?after=0&agent=agent-b", auth=code)
        self.assertEqual(out, {"rseq": 6, "frames": [], "more": False}, "a kicked agent filters to nothing")

    # -- §3.13 delete, §2 viewer cap ------------------------------------------

    def test_delete_room_gone(self):
        room = self._room()
        token = self._register(room, "agent-a")
        code, join = room["room"], room["join_code"]
        _, _, minted = self.call("POST", f"/r/{code}/ticket", auth=code)
        resp = listener = None
        if self.sse:
            resp = self._stream(room, auth=code)
            self._until_live(resp)
            listener = self._agent_stream(room, token)
            self._frames(listener, lambda f: f["t"] == "agent")
        status, _, out = self.call("DELETE", f"/r/{code}", auth=join)
        self.assertEqual((status, out), (204, b""))
        for open_stream in (resp, listener):
            if open_stream is not None:
                # The viewer still holds the listener's connect flip (an agent frame).
                frames = [f for _, f in self._frames(open_stream, lambda f: f.get("t") == "gone")]
                self.assertTrue(all(f["t"] == "agent" for f in frames[:-1]), frames)
                self.assertEqual(frames[-1], {"t": "gone"})
                self.assertTrue(self._at_eof(open_stream), "gone is followed by close")
        for method, path, auth in (("GET", f"/r/{code}", code), ("POST", f"/r/{code}/events", token),
                                   ("POST", f"/r/{code}/messages", token),
                                   ("POST", f"/r/{code}/prune", join), ("DELETE", f"/r/{code}", join),
                                   ("GET", f"/r/{code}/stream?ticket={minted['ticket']}", None)):
            status, _, out = self.call(method, path, auth=auth, body={} if method == "POST" else None)
            self.assertEqual((status, out["error"]), (404, "room"), (method, path))

    def test_viewer_cap_full(self):
        self._need_sse()
        room = self._room()
        code = room["room"]
        _, _, minted = self.call("POST", f"/r/{code}/ticket", auth=code)
        held = [self._stream(room, ticket=minted["ticket"]) for _ in range(25)]
        for resp in held:
            self.assertEqual(self._frames(resp, lambda f: True)[0][1]["t"], "hello")
        extra = self._stream(room, ticket=minted["ticket"])
        _, frame = self._frames(extra, lambda f: True)[0]
        self.assertEqual(frame, {"t": "full"})
        self.assertTrue(self._at_eof(extra), "full is followed by close")

    # -- §6.7 retention, horizon, byte bounds, replay budget -------------------

    def test_retention_prune_and_horizon(self):
        self._local_only("retention_min is at least five minutes, so the clock has to move")
        room = self._room(retention_min=5)
        token = self._register(room, "agent-a")
        code, join = room["room"], room["join_code"]
        self._post_ok(room, token, [_ev()])                                                  # 1
        self._message(room, code, to="agent-a", text="q", viewer="v")                        # 2
        self.call("PUT", f"/r/{code}/roles/agent-a", auth=code, body={"role": "r", "viewer": "v"})  # 3
        self.call("PUT", f"/r/{code}/policy", auth=join, body={"max_stream": "full"})             # 4
        self._advance(6 * 60)
        self._post_ok(room, token, [_ev()])                                                  # 5
        status, _, _ = self.call("POST", f"/r/{code}/prune", auth=join)
        self.assertEqual(status, 204)
        _, _, page = self.call("GET", f"/r/{code}/events?after=0", auth=code)
        self.assertEqual([f["rseq"] for f in page["frames"]], [5], "rows of every kind age out")
        _, _, snap = self.call("GET", f"/r/{code}", auth=code)
        self.assertEqual(([x["seq"] for x in snap["messages"]], snap["agents"][0]["role"]["set_seq"],
                          snap["policy"]["max_stream"], snap["rseq"]), ([2], 3, "full", 5),
                         "messages, roles and policy live in their own tables")
        if self.sse:
            for after, gap, replayed in ((0, {"before_rseq": 5}, [5]), (3, {"before_rseq": 5}, [5]),
                                         (4, None, [5]), (5, None, [])):
                resp = self._stream(room, auth=code, after=after)
                frames = [f for _, f in self._until_live(resp)]
                self.assertEqual((frames[0]["gap"], [f["rseq"] for f in frames if "rseq" in f]),
                                 (gap, replayed), after)
            resp = self._stream(room, auth=code)
            self._until_live(resp)
        self._advance(8 * 86_400)
        status, _, _ = self.call("POST", f"/r/{code}/prune", auth=join)
        self.assertIn(status, (204, 404), "the periodic pass may have wiped it first")
        if self.sse:
            # gone is the last frame before close; whether the ask's expiry frame
            # (ask_ttl_s one day) precedes it or the message was already seven
            # days old and deleted depends on the pass's order, which is not fixed.
            frames = [f for _, f in self._frames(resp, lambda f: f.get("t") == "gone")]
            self.assertEqual(frames[-1], {"t": "gone"})
            self.assertTrue(all(f["t"] == "message" and f["message"]["state"] == "expired"
                                for f in frames[:-1]), frames)
            self.assertTrue(self._at_eof(resp))
        status, _, out = self.call("GET", f"/r/{code}", auth=code)
        self.assertEqual((status, out["error"]), (404, "room"), "wiped after seven idle days")

    def _big_batch(self):
        return _batch([_ev("text", body={"text": "x" * 15_000}) for _ in range(4)])

    def test_byte_bounds_on_insert(self):
        self._local_only("over a hundred posts need the bucket refilled by the clock")
        room = self._room(max_stream="full")
        code = room["room"]
        sizes = {}

        def post(token):
            raw = self._big_batch()
            self._advance(1)
            out = self._post_ok(room, token, raw=raw)
            sizes[out["rseq"]] = len(raw)
            return out["rseq"]

        a = self._register(room, "agent-a")
        rows = [post(a) for _ in range(18)]
        _, _, page = self.call("GET", f"/r/{code}/events?after=0&limit=200", auth=code)
        kept = [f["rseq"] for f in page["frames"]]
        self.assertLessEqual(sum(sizes[r] for r in kept), 1 << 20)
        self.assertGreater(kept[0], 1, "the oldest batch rows of the agent went first")
        self.assertEqual(kept, rows[rows.index(kept[0]):], "deletion is oldest-first, contiguous")
        self.assertGreater(sum(sizes[r] for r in kept) + sizes[kept[0] - 1], 1 << 20,
                           "no more than needed is deleted")
        if self.sse:
            resp = self._stream(room, auth=code, after=0)
            frames = [f for _, f in self._until_live(resp)]
            self.assertEqual(frames[0]["gap"], {"before_rseq": kept[0]})
            self.assertEqual(frames[1]["rseq"], kept[0])
        for i in range(2, 10):
            token = self._register(room, f"agent-{i}")
            for _ in range(17):
                post(token)
        _, _, page = self.call("GET", f"/r/{code}/events?after=0&limit=200", auth=code)
        kept = [f["rseq"] for f in page["frames"]]
        self.assertLessEqual(sum(sizes[r] for r in kept), 8 << 20)
        last = max(sizes)
        self.assertEqual(kept, list(range(kept[0], last + 1)), "the room bound drops the oldest rows")
        self.assertGreater(sum(sizes[r] for r in kept) + sizes[kept[0] - 1], 8 << 20)
        per_agent = {}
        for f in page["frames"]:
            per_agent[f["agent"]] = per_agent.get(f["agent"], 0) + sizes[f["rseq"]]
        self.assertTrue(all(v <= 1 << 20 for v in per_agent.values()))

    def test_replay_budget_two_mib(self):
        self._local_only("a few dozen posts need the bucket refilled by the clock")
        self._need_sse()
        room = self._room(max_stream="full")
        code = room["room"]
        sizes = {}
        for name in ("agent-a", "agent-b", "agent-c"):
            token = self._register(room, name)
            for _ in range(12):
                raw = self._big_batch()
                self._advance(1)
                sizes[self._post_ok(room, token, raw=raw)["rseq"]] = len(raw)
        self.assertGreater(sum(sizes.values()), 2 << 20)
        resp = self._stream(room, auth=code, after=0)
        frames = [f for _, f in self._until_live(resp)]
        replayed = [f["rseq"] for f in frames if "rseq" in f]
        self.assertEqual(frames[0]["backfill"], "replay")
        self.assertEqual(frames[0]["gap"], {"before_rseq": replayed[0]})
        self.assertEqual(replayed, list(range(replayed[0], max(sizes) + 1)), "the newest rows, ascending")
        self.assertLessEqual(sum(sizes[r] for r in replayed), 2 << 20)
        self.assertGreater(sum(sizes[r] for r in replayed) + sizes[replayed[0] - 1], 2 << 20)

    # -- §6.16 body handling ---------------------------------------------------

    def test_content_length_required(self):
        self._local_only("the Worker accepts chunked bodies; only the Python relay refuses them")
        room = self._room()
        code = room["room"]
        with socket.create_connection(("127.0.0.1", self.relay.port), timeout=5) as sock:
            sock.sendall((f"POST /r/{code}/ticket HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {code}\r\n"
                          "Transfer-Encoding: chunked\r\n\r\n2\r\n{}\r\n0\r\n\r\n").encode())
            reply = b""
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                reply += chunk
        head, _, body = reply.partition(b"\r\n\r\n")
        self.assertIn(b" 400 ", head.split(b"\r\n")[0])
        self.assertEqual(json.loads(body)["error"], "schema")

    # -- the process: --state, --public-url, main() ------------------------------

    def test_state_dir_and_public_url_survive_restart(self):
        self._local_only("--state and --public-url belong to this process")
        with tempfile.TemporaryDirectory() as tmp:
            first = canvas_relay.Relay(("127.0.0.1", 0), state_dir=tmp,
                                       public_url="https://canvas.example/")
            threading.Thread(target=first.serve_forever, daemon=True).start()
            base = f"http://127.0.0.1:{first.port}"
            request = urllib.request.Request(base + "/rooms", data=b"{}", method="POST")
            with urllib.request.urlopen(request, timeout=5) as resp:
                room = json.loads(resp.read())
            self.assertEqual((room["relay"], room["url"]),
                             ("https://canvas.example", "https://canvas.example/#" + room["room"]))
            saved_base, RelayContract.base = self.base, base
            try:
                reg = self._register_out(room, "agent-a")
                token = reg["token"]
                out = self._post_ok(room, token, [_ev()])
                self.assertEqual(out["rseq"], 1)
                self._message(room, room["room"], to="agent-a", text="q", viewer="v")
                self.call("PUT", f"/r/{room['room']}/agents/agent-a/wake", auth=reg["owner_token"],
                          body={"enabled": True})
                first.shutdown()
                self.assertTrue((Path(tmp) / "rooms.json").exists())
                second = canvas_relay.Relay(("127.0.0.1", 0), state_dir=tmp)
                threading.Thread(target=second.serve_forever, daemon=True).start()
                RelayContract.base = f"http://127.0.0.1:{second.port}"
                status, _, snap = self.call("GET", f"/r/{room['room']}", auth=room["room"])
                self.assertEqual((status, snap["rseq"], snap["agents"][0]["name"],
                                  [x["seq"] for x in snap["messages"]],
                                  snap["agents"][0]["wake"]["enabled"]), (200, 2, "agent-a", [2], True))
                out = self._post_ok(room, token, [_ev()])
                self.assertEqual(out["rseq"], 3, "tokens, rows and the counter survive a restart")
                status, _, out = self.call("PUT", f"/r/{room['room']}/agents/agent-a/wake",
                                           auth=reg["owner_token"], body={"from": "room"})
                self.assertEqual(status, 200, "the owner token survives too")
                _, _, page = self.call("GET", f"/r/{room['room']}/events?after=0", auth=room["room"])
                self.assertEqual([f["rseq"] for f in page["frames"]], [1, 2, 3])
                status, _, _ = self.call("POST", f"/r/{room['room']}/prune", auth=room["join_code"])
                self.assertEqual(status, 204)
                second.shutdown()
            finally:
                RelayContract.base = saved_base

    def test_main_binds_port_zero_and_writes_port_file(self):
        self._local_only("main() is this process's entry point")
        root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as tmp:
            port_file = Path(tmp) / "port"
            proc = subprocess.Popen([sys.executable, "-m", "agentcolab.canvas_relay", "--port", "0",
                                     "--port-file", str(port_file)], cwd=str(root),
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.time() + 10
                while not port_file.exists() and time.time() < deadline:
                    time.sleep(0.05)
                self.assertTrue(port_file.exists(), proc.stderr.read().decode() if proc.poll() else "")
                port = int(port_file.read_text(encoding="utf-8"))
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5) as resp:
                    self.assertEqual(json.loads(resp.read())["backend"], "python")
                proc.send_signal(signal.SIGINT)
                stdout, _ = proc.communicate(timeout=10)
            finally:
                if proc.poll() is None:
                    proc.kill()
            self.assertEqual(proc.returncode, 130)
            self.assertIn(f"http://127.0.0.1:{port}", stdout.decode())

    def test_placeholder_when_web_file_is_missing(self):
        self._local_only("the --web default is this process's file system")
        relay = canvas_relay.Relay(("127.0.0.1", 0), web=Path(tempfile.gettempdir()) / "no-such-canvas.html")
        threading.Thread(target=relay.serve_forever, daemon=True).start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{relay.port}/", timeout=5) as resp:
                self.assertEqual(resp.status, 200)
                self.assertIn("text/html", resp.headers.get("Content-Type", ""))
                self.assertIn(b"canvas", resp.read().lower(), "GET / never 500s: a placeholder page")
        finally:
            relay.shutdown()


if __name__ == "__main__":
    unittest.main(verbosity=2)
