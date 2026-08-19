"""Drive the Discord and Slack adapters against a local server that speaks
their documented protocols.

This is the closest thing to a live test that does not need somebody's bot
token. It exercises the parts that actually break an adapter — request shape,
auth header, pagination cursors, message ordering, echo filtering, rate-limit
backoff, provisioning idempotence, and error reporting — against a server that
answers exactly as the real API documents.

What it deliberately cannot cover: whether Discord accepts a real token, and
whether a real bot has the intents and channel permissions it needs. Those are
what `colab chat status` diagnoses at runtime, and they are listed as unverified
in FAILURE-MODES.md.

Route shapes and edge acceptance ARE verified against the live API separately —
see the probe in this file's `LiveRouteShapes` case, which sends no credentials.
"""

from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentcolab.chat import base, discord, slack     # noqa: E402


class FakeAPI(BaseHTTPRequestHandler):
    """Answers as Discord and Slack document. State lives on the server class."""

    posted: list = []
    created: list = []
    webhooks: list = []
    rate_limit_once = False
    messages: list = []

    def log_message(self, *a):            # keep the test output clean
        pass

    # -- helpers ------------------------------------------------------
    def _json(self, code, payload, headers=None):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _read(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw)
        except ValueError:
            return {}

    # -- routing ------------------------------------------------------
    def do_GET(self):
        path = self.path.split("?")[0]
        query = dict(p.split("=", 1) for p in self.path.split("?")[1].split("&")
                     if "=" in p) if "?" in self.path else {}

        if path.startswith("/api/v10/channels/") and path.endswith("/messages"):
            if not self.headers.get("Authorization", "").startswith("Bot "):
                return self._json(401, {"message": "401: Unauthorized"})
            after = query.get("after")
            msgs = [m for m in FakeAPI.messages
                    if not after or str(m["id"]) > str(after)]
            # Discord returns newest first. Getting this backwards silently
            # reverses every conversation, which is why it is asserted below.
            return self._json(200, list(reversed(msgs)))

        if path.startswith("/api/v10/channels/"):
            if not self.headers.get("Authorization", "").startswith("Bot "):
                return self._json(401, {"message": "401: Unauthorized"})
            return self._json(200, {"id": path.rsplit("/", 1)[-1], "name": "ask"})

        if path.startswith("/api/v10/guilds/") and path.endswith("/channels"):
            return self._json(200, list(FakeAPI.created))

        if path.startswith("/api/conversations.history"):
            if not self.headers.get("Authorization", "").startswith("Bearer "):
                return self._json(200, {"ok": False, "error": "invalid_auth"})
            oldest = query.get("oldest")
            msgs = [m for m in FakeAPI.messages
                    if not oldest or str(m["ts"]) > str(oldest)]
            return self._json(200, {"ok": True, "messages": list(reversed(msgs))})

        if path.startswith("/api/conversations.info"):
            if not self.headers.get("Authorization", "").startswith("Bearer "):
                return self._json(200, {"ok": False, "error": "invalid_auth"})
            return self._json(200, {"ok": True, "channel": {"name": "ask"}})

        if path.startswith("/api/conversations.list"):
            return self._json(200, {"ok": True,
                                    "channels": [{"name": c["name"], "id": c["id"]}
                                                 for c in FakeAPI.created],
                                    "response_metadata": {"next_cursor": ""}})
        return self._json(404, {"message": "404: Not Found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        payload = self._read()

        if FakeAPI.rate_limit_once:
            FakeAPI.rate_limit_once = False
            return self._json(429, {"retry_after": 0.05}, {"Retry-After": "1"})

        if path.startswith("/webhook/"):
            FakeAPI.posted.append(("webhook", payload))
            return self._json(204, {})

        if path.startswith("/api/v10/channels/") and path.endswith("/webhooks"):
            hook = {"id": "hook1", "token": "tok1"}
            FakeAPI.webhooks.append(path)
            return self._json(200, hook)

        if path.startswith("/api/v10/channels/") and path.endswith("/messages"):
            FakeAPI.posted.append(("bot", payload))
            return self._json(200, {"id": "999"})

        if path.startswith("/api/v10/guilds/") and path.endswith("/channels"):
            entry = {"id": f"c{len(FakeAPI.created)}", "name": payload.get("name"),
                     "type": payload.get("type", 0)}
            FakeAPI.created.append(entry)
            return self._json(200, entry)

        if path == "/api/chat.postMessage":
            if not self.headers.get("Authorization", "").startswith("Bearer "):
                return self._json(200, {"ok": False, "error": "invalid_auth"})
            if payload.get("channel") == "NOT_A_MEMBER":
                # Slack's real failure shape: HTTP 200 with ok:false. An adapter
                # that trusts the status code drops every message while
                # reporting success.
                return self._json(200, {"ok": False, "error": "not_in_channel"})
            FakeAPI.posted.append(("slack", payload))
            return self._json(200, {"ok": True})

        if path == "/api/conversations.create":
            entry = {"id": f"s{len(FakeAPI.created)}", "name": payload.get("name")}
            FakeAPI.created.append(entry)
            return self._json(200, {"ok": True, "channel": entry})

        if path == "/api/conversations.setTopic":
            return self._json(200, {"ok": True})
        return self._json(404, {"message": "404"})


class ChatAdapters(unittest.TestCase):
    server = None
    base_url = ""

    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), FakeAPI)
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        discord.API = f"{cls.base_url}/api/v10"
        slack.API = f"{cls.base_url}/api"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        FakeAPI.posted, FakeAPI.created = [], []
        FakeAPI.webhooks, FakeAPI.messages = [], []
        FakeAPI.rate_limit_once = False

    # -- discord ------------------------------------------------------

    def _discord(self, **over):
        conf = {"token": "bot-token",
                "channels": {"link": {"id": "100", "webhook": f"{self.base_url}/webhook/1"},
                             "ask": {"id": "200"}}}
        conf.update(over)
        return discord.Discord(conf)

    def test_posts_through_a_webhook_when_one_exists(self):
        ok = self._discord().post(base.Event("note", "alice", "hello", body="world"))
        self.assertTrue(ok)
        kind, payload = FakeAPI.posted[0]
        self.assertEqual(kind, "webhook")
        self.assertIn("hello", payload["content"])

    def test_falls_back_to_the_bot_when_a_channel_has_no_webhook(self):
        ok = self._discord().post(base.Event("note", "alice", "hi", channel="ask"))
        self.assertTrue(ok)
        self.assertEqual(FakeAPI.posted[0][0], "bot")

    def test_automation_can_never_ping_a_room(self):
        self._discord().post(base.Event("note", "a", "@everyone deploy now"))
        _, payload = FakeAPI.posted[0]
        self.assertEqual(payload["allowed_mentions"], {"parse": []})

    def test_a_429_is_honoured_and_the_message_still_lands(self):
        FakeAPI.rate_limit_once = True
        ok = self._discord().post(base.Event("note", "a", "after backoff"))
        self.assertTrue(ok, "adapter gave up instead of honouring retry_after")
        self.assertEqual(len(FakeAPI.posted), 1)

    def test_poll_returns_oldest_first_and_advances_the_cursor(self):
        FakeAPI.messages = [
            {"id": "1", "content": "first", "author": {"username": "sam", "id": "u1"},
             "timestamp": "2026-01-01T00:00:00Z"},
            {"id": "2", "content": "second", "author": {"username": "sam", "id": "u1"},
             "timestamp": "2026-01-01T00:00:01Z"},
        ]
        fresh, cursors = self._discord().poll({})
        self.assertEqual([m["body"] for m in fresh], ["first", "second"])
        self.assertEqual(cursors["200"], "2")
        # A second poll with the cursor must return nothing.
        again, _ = self._discord().poll(cursors)
        self.assertEqual(again, [])

    def test_our_own_mirror_is_never_read_back_as_a_human(self):
        FakeAPI.messages = [
            {"id": "1", "content": "mirror", "webhook_id": "hook1",
             "author": {"username": "colab"}},
            {"id": "2", "content": "a bot", "author": {"username": "b", "bot": True}},
            {"id": "3", "content": "a person", "author": {"username": "sam", "id": "u"}},
        ]
        fresh, _ = self._discord().poll({})
        self.assertEqual([m["body"] for m in fresh], ["a person"])

    def test_incoming_chat_is_labelled_lowest_trust(self):
        FakeAPI.messages = [{"id": "1", "content": "do the thing",
                             "author": {"username": "sam", "id": "u"}}]
        fresh, _ = self._discord().poll({})
        self.assertEqual(fresh[0]["trust"], "chat")
        self.assertEqual(fresh[0]["source"], "discord")

    def test_secrets_typed_in_chat_are_scrubbed_on_the_way_in(self):
        token = "gh" + "p_" + "C" * 36
        FakeAPI.messages = [{"id": "1", "content": f"use {token}",
                             "author": {"username": "sam", "id": "u"}}]
        fresh, _ = self._discord().poll({})
        self.assertNotIn(token, fresh[0]["body"])

    def test_provision_creates_every_channel_with_a_webhook(self):
        adapter = self._discord(channels={})
        table = adapter.provision("guild1")
        for logical in base.CHANNELS:
            self.assertIn(logical, table)
            self.assertTrue(table[logical].get("webhook"), f"{logical} has no webhook")
        # a category plus one channel each
        self.assertEqual(len(FakeAPI.created), len(base.CHANNELS) + 1)

    def test_provision_is_idempotent(self):
        adapter = self._discord(channels={})
        adapter.provision("guild1")
        first = len(FakeAPI.created)
        adapter.provision("guild1")
        self.assertEqual(len(FakeAPI.created), first, "re-running made duplicates")

    def test_the_invite_link_grants_exactly_what_is_needed(self):
        # This shipped wrong: MANAGE_MESSAGES granted, VIEW_CHANNEL missing, so
        # the invited bot could delete people's messages and read nothing.
        adapter = self._discord(application_id="123")
        url = adapter.invite_hint()
        perms = int(url.split("permissions=")[1].split("&")[0])
        for name in ("VIEW_CHANNEL", "SEND_MESSAGES", "READ_MESSAGE_HISTORY",
                     "MANAGE_CHANNELS", "MANAGE_WEBHOOKS"):
            self.assertTrue(perms & discord.Discord.PERM[name], f"missing {name}")
        self.assertFalse(perms & (1 << 13), "asks for MANAGE_MESSAGES, which is never used")
        self.assertFalse(perms & (1 << 3), "asks for ADMINISTRATOR")
        self.assertIn("scope=bot", url)

    def test_no_application_id_means_no_invite_link_rather_than_a_broken_one(self):
        self.assertEqual(self._discord().invite_hint(), "")

    def test_the_minimal_invite_drops_the_setup_permissions(self):
        adapter = self._discord(application_id="123")
        perms = adapter.permissions(provision=False)
        self.assertTrue(perms & discord.Discord.PERM["VIEW_CHANNEL"])
        self.assertFalse(perms & discord.Discord.PERM["MANAGE_CHANNELS"])
        self.assertFalse(perms & discord.Discord.PERM["MANAGE_WEBHOOKS"])

    def test_verify_distinguishes_a_bad_token_from_a_quiet_channel(self):
        ok, detail = self._discord(token="").verify()
        self.assertFalse(ok)
        self.assertIn("no bot token", detail)
        ok, detail = self._discord().verify()
        self.assertTrue(ok)
        self.assertIn("ask", detail)

    # -- slack --------------------------------------------------------

    def _slack(self, **over):
        conf = {"token": "xoxb-test",
                "channels": {"link": {"id": "C1"}, "ask": {"id": "C2"}}}
        conf.update(over)
        return slack.Slack(conf)

    def test_slack_posts_and_does_not_page_the_workspace(self):
        self.assertTrue(self._slack().post(base.Event("note", "a", "hi")))
        _, payload = FakeAPI.posted[0]
        self.assertFalse(payload["link_names"])
        self.assertFalse(payload["unfurl_links"])

    def test_slack_ok_false_is_treated_as_failure_not_success(self):
        # Slack answers HTTP 200 with {"ok": false}. This must reach the server
        # with a real token and get a real ok:false back -- an earlier version
        # of this test passed an empty token, so the adapter bailed at its own
        # guard and the test proved nothing. Mutation testing caught that.
        adapter = self._slack(channels={"link": {"id": "NOT_A_MEMBER"}})
        self.assertFalse(adapter.post(base.Event("note", "a", "hi")),
                         "adapter reported success on an ok:false response")
        self.assertEqual(FakeAPI.posted, [], "nothing should have been recorded")

    def test_slack_missing_token_is_refused_before_any_request(self):
        self.assertFalse(self._slack(token="").post(base.Event("note", "a", "hi")))

    def test_slack_poll_orders_and_advances(self):
        FakeAPI.messages = [{"ts": "1.1", "text": "first", "user": "U1"},
                            {"ts": "2.2", "text": "second", "user": "U1"}]
        fresh, cursors = self._slack().poll({})
        self.assertEqual([m["body"] for m in fresh], ["first", "second"])
        self.assertEqual(cursors["C2"], "2.2")

    def test_slack_skips_bots_and_joins(self):
        FakeAPI.messages = [{"ts": "1.1", "text": "mirror", "bot_id": "B1"},
                            {"ts": "1.2", "text": "joined", "subtype": "channel_join",
                             "user": "U1"},
                            {"ts": "1.3", "text": "a person", "user": "U1"}]
        fresh, _ = self._slack().poll({})
        self.assertEqual([m["body"] for m in fresh], ["a person"])

    def test_slack_verify_explains_the_actual_error(self):
        ok, detail = self._slack(token="").verify()
        self.assertFalse(ok)
        self.assertIn("no bot token", detail)

    def test_slack_provision_is_idempotent(self):
        adapter = self._slack(channels={})
        adapter.provision()
        first = len(FakeAPI.created)
        adapter.provision()
        self.assertEqual(len(FakeAPI.created), first)


class EventContract(unittest.TestCase):
    """Every construction of Event must match its signature.

    `colab relay` shipped passing `wire_line=` where Event takes `wire=`, so it
    raised TypeError on every run that had anything to relay — and that is the
    one path holding the bot token, the reason contributors need no chat
    credentials. Nothing caught it: the scheduled workflow was green because
    without the secret it exits before reaching the call, which is the
    "flagship feature must work in automation" trap.

    Checked structurally rather than by example, so a future call site with a
    typo'd keyword fails here instead of in somebody's CI at 3am.
    """

    def test_no_call_site_passes_an_argument_event_does_not_take(self):
        import ast
        import inspect
        allowed = set(inspect.signature(base.Event.__init__).parameters) - {"self"}
        root = Path(__file__).resolve().parent.parent / "agentcolab"
        offenders = []
        for path in sorted(root.rglob("*.py")):
            for node in ast.walk(ast.parse(path.read_text())):
                if not isinstance(node, ast.Call):
                    continue
                name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
                if name != "Event":
                    continue
                for kw in node.keywords:
                    if kw.arg and kw.arg not in allowed:
                        offenders.append(f"{path.name}:{node.lineno} passes {kw.arg!r}")
        self.assertEqual(offenders, [], "Event called with an argument it does not accept")


class MCPTransportSafety(unittest.TestCase):
    """A tool argument must never be able to consume the JSON-RPC transport.

    The CLI reads a bare "-" as "take the body from stdin", which is correct in
    a terminal and catastrophic in the MCP server, whose stdin *is* the
    protocol stream. A tool called with body="-" would swallow whatever the
    client sent next.
    """

    def test_a_dash_argument_is_neutralised(self):
        from agentcolab import mcp
        self.assertEqual(mcp._arg("-"), "")
        self.assertEqual(mcp._arg(" - "), "")
        self.assertEqual(mcp._arg(None), "")

    def test_ordinary_values_are_untouched(self):
        from agentcolab import mcp
        for value in ("normal", "a-b", "--flag-like", "a - b"):
            self.assertEqual(mcp._arg(value), value)

    def test_no_tool_passes_a_raw_get_into_a_command(self):
        # Every string argument must go through _arg, or the guard is decorative.
        import re
        source = (Path(__file__).resolve().parent.parent / "agentcolab" / "mcp.py").read_text()
        call = source[source.index("def call("):source.index("# ---------------------------------------------------------------- server")]
        raw = re.findall(r'str\(get\("(\w+)"\)', call)
        self.assertEqual(raw, [], f"these bypass _arg: {raw}")


class MCPArgumentContract(unittest.TestCase):
    """Every MCP tool must pass what its command actually reads.

    `colab_next` and `colab_bug` both shipped broken this way: a flag was added
    to the CLI and mcp.py was not updated, so the tool raised AttributeError,
    which `_capture` swallowed into a bland "(no output, exit 1)". The tool
    reported failure without ever saying why, on every single call.

    Checked structurally so the next added flag fails here rather than silently
    disabling a tool.
    """

    def test_every_tool_provides_the_arguments_its_command_reads(self):
        import ast
        import inspect
        import re
        from agentcolab import cli
        source = (Path(__file__).resolve().parent.parent / "agentcolab" / "mcp.py").read_text()
        problems = []
        for node in ast.walk(ast.parse(source)):
            if not (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "_capture"):
                continue
            if len(node.args) < 3 or not isinstance(node.args[0], ast.Attribute):
                continue
            fn = node.args[0].attr
            ns = node.args[2]
            if not (isinstance(ns, ast.Call) and getattr(ns.func, "id", "") == "_ns"):
                continue
            provided = {kw.arg for kw in ns.keywords}
            handler = getattr(cli, fn, None)
            if handler is None:
                problems.append(f"mcp calls cli.{fn}, which does not exist")
                continue
            used = set(re.findall(r"args\.(\w+)", inspect.getsource(handler)))
            missing = used - provided
            if missing:
                problems.append(f"{fn} reads args.{{{', '.join(sorted(missing))}}} "
                                f"but the tool never provides them")
        self.assertEqual(problems, [])


class CustomChannels(unittest.TestCase):
    """A project inventing its own rooms is configuration, not a code change."""

    CONF = {"chat": {"custom": {
        "bs-chat": {"dir": "out", "purpose": "Blunt observations.",
                    "brief": "Be specific. Post rarely."},
        "ideas":   {"dir": "in",  "purpose": "Humans drop ideas."},
    }}}

    def test_builtins_survive_alongside_custom_ones(self):
        got = base.resolve(self.CONF)
        for name in base.BUILTIN:
            self.assertIn(name, got)
        self.assertIn("bs-chat", got)

    def test_a_brief_is_carried_through(self):
        self.assertIn("Post rarely", base.resolve(self.CONF)["bs-chat"]["brief"])

    def test_a_custom_channel_can_be_an_input(self):
        self.assertIn("ideas", base.inputs(self.CONF))
        self.assertIn("ask", base.inputs(self.CONF))

    def test_custom_channels_default_to_output(self):
        got = base.resolve({"chat": {"custom": {"notes": {"purpose": "x"}}}})
        self.assertEqual(got["notes"]["dir"], "out")

    def test_a_name_cannot_escape_into_a_path_or_a_route(self):
        got = base.resolve({"chat": {"custom": {"../../etc/passwd": {"purpose": "x"},
                                                "with space!": {"purpose": "y"}}}})
        self.assertNotIn("../../etc/passwd", got)
        self.assertTrue(all(all(c.isalnum() or c in "-_" for c in k) for k in got))

    def test_a_project_may_retune_a_builtin_without_losing_it(self):
        got = base.resolve({"chat": {"custom": {"link": {"brief": "keep it short"}}}})
        self.assertEqual(got["link"]["dir"], "out")
        self.assertIn("keep it short", got["link"]["brief"])
        self.assertIn("Agent-to-agent", got["link"]["purpose"])

    def test_adapters_can_route_and_provision_a_custom_channel(self):
        adapter = discord.Discord({"token": "t", "custom": self.CONF["chat"]["custom"],
                                   "channels": {}})
        self.assertIn("bs-chat", adapter.channels())


class LiveRouteShapes(unittest.TestCase):
    """Against the real discord.com. Sends no credentials; skipped when offline.

    Catches the two things a local fake cannot: a wrong route, and an edge that
    rejects our User-Agent outright — which is a real failure mode, not a
    hypothetical one.
    """

    REAL = "https://discord.com/api/v10"

    def setUp(self):
        import os
        if not os.environ.get("AGENTCOLAB_LIVE"):
            self.skipTest("set AGENTCOLAB_LIVE=1 to probe the real API")

    def _status(self, url, headers):
        try:
            urllib.request.urlopen(urllib.request.Request(url, headers=headers),
                                   timeout=15)
            return 200
        except urllib.error.HTTPError as exc:
            return exc.code
        except Exception:
            self.skipTest("no network")

    def test_our_user_agent_is_not_blocked_at_the_edge(self):
        code = self._status(f"{self.REAL}/channels/1",
                            {"User-Agent": base.USER_AGENT})
        self.assertEqual(code, 401, "401 means we reached the API; 403 means blocked")

    def test_the_route_shapes_are_real(self):
        ua = {"User-Agent": base.USER_AGENT}
        self.assertEqual(self._status(f"{self.REAL}/guilds/1/channels", ua), 401)
        self.assertEqual(self._status(f"{self.REAL}/nonsense/1", ua), 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
