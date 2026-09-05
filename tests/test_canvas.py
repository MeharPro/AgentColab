"""Canvas client: parsers, sanitise, schema, offsets, poster, daemon, structure.

Everything is stdlib unittest. The relay is a fake `http.server` in a thread
inside this file; the real relay has its own suite. Fixtures are dict literals
written to temp files at runtime, and every credential-shaped string is
assembled by concatenation so a secret scanner never sees a literal.

Mutation checks (run by hand on a temp copy; each must turn a test red):
delete the `scrub_deep` call in `sanitise`; delete the `looks_like_secret`
gate; change `* 256` to `* 16` in `_Tailer._event`; remove the `BACKLOG_MAX`
branch in `poll`; make `ack` commit before the relay answers; drop the
`start_new_session` flag from `ensure_tailer`.
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentcolab import canvas, records                 # noqa: E402
from agentcolab.store import Store                     # noqa: E402


def _shape(prefix: str, body: str, length: int = 0) -> str:
    filler = (body * 64)[:length] if length else body
    return prefix + filler


FAKE_GITHUB_TOKEN = _shape("gh" + "p_", "C", 36)
FAKE_BLOB = "Zk3Lm9Qp2Rt5Vx8Yb1Ec4Hg7Jn0Ks3Mv6Pw9Sz2Ad5Fh8"

SID = "8c5053f6-ba4d-4d25-9c34-43ebe9694c18"
THREAD = "01a036ec-d9d5-7752-9053-73b96bff7e5c"
TS = "2026-09-04T10:00:00.123Z"


def _write_jsonl(path: Path, entries: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry) + "\n")


def _append_jsonl(path: Path, entries: list) -> None:
    with open(path, "a", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry) + "\n")


# ---------------------------------------------------------------- fixtures


def _claude_entry(kind: str, uuid: str, **extra):
    base = {"parentUuid": None, "isSidechain": False, "type": kind, "uuid": uuid, "timestamp": TS,
            "sessionId": SID, "cwd": "/repo", "version": "2.1.255", "gitBranch": "main"}
    base.update(extra)
    return base


def _assistant(uuid: str, block: dict, index: int, stop: str = "tool_use", **extra):
    return _claude_entry("assistant", uuid, apiBlockIndex=index,
                         message={"model": "claude-fable-5-1", "id": "msg_1", "type": "message",
                                  "role": "assistant", "content": [block], "stop_reason": stop,
                                  "usage": {"input_tokens": 1}}, **extra)


def claude_fixture(repo: str) -> list:
    edit_args = {"file_path": f"{repo}/agentcolab/hooks.py", "old_string": "a\nb\nc",
                 "new_string": f"token {FAKE_GITHUB_TOKEN}"}
    twenty = [{"type": "tool_result", "tool_use_id": f"toolu_many{i}", "content": f"out {i}", "is_error": False}
              for i in range(20)]
    return [
        _claude_entry("user", "u1", promptId="p1", origin={"kind": "human"},
                      message={"role": "user", "content": "Fix the failing test in tests/test_units.py"}),
        _claude_entry("user", "u2", isMeta=True, promptId="p1",
                      message={"role": "user", "content": [{"type": "text", "text": "skill body, never shown"}]}),
        _assistant("a1", {"type": "thinking", "thinking": "The hook must not import http.", "signature": "CAIS"}, 0),
        _assistant("a2", {"type": "text", "text": f"I'll edit {repo}/agentcolab/hooks.py now."}, 1),
        _assistant("a3", {"type": "tool_use", "id": "toolu_edit1", "name": "Edit", "input": edit_args,
                          "caller": {"type": "direct"}}, 2),
        _claude_entry("user", "u3", promptId="p1", sourceToolAssistantUUID="a3",
                      toolUseResult={"filePath": f"{repo}/agentcolab/hooks.py", "oldString": "a", "newString": "b"},
                      message={"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_edit1",
                                                            "content": "The file has been updated.", "is_error": False}]}),
        _assistant("a4", {"type": "tool_use", "id": "toolu_bash1", "name": "Bash",
                          "input": {"command": "git status --short", "description": "Show status"}}, 0),
        _claude_entry("user", "u4", promptId="p1",
                      toolUseResult={"stdout": "M x.py\n", "stderr": "", "interrupted": False, "isImage": False},
                      message={"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_bash1",
                                                            "content": f"M x.py\nsecret {FAKE_BLOB}\n", "is_error": False}]}),
        _claude_entry("user", "u5", promptId="p1", message={"role": "user", "content": twenty}),
        _claude_entry("user", "u6", promptId="p1",
                      message={"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_img",
                                                            "content": [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA" * 200}}]}]}),
        {"type": "ai-title", "aiTitle": "Canvas design", "sessionId": SID},
        {"type": "queue-operation", "operation": "enqueue", "timestamp": TS, "sessionId": SID, "content": "x"},
        {"type": "bridge-session", "sessionId": SID, "bridgeSessionId": "cse_1", "lastSequenceNum": 1,
         "ownerAccountUuid": "acct", "ownerOrganizationUuid": "org"},
        _claude_entry("system", "s1", subtype="compact_boundary", content="Conversation compacted",
                      compactMetadata={"trigger": "auto", "preTokens": 1}, level="info", isMeta=True),
        _assistant("a5", {"type": "text", "text": "Rate limited"}, 0, isApiErrorMessage=True, error="rate_limit",
                   apiErrorStatus=429),
        _assistant("a6", {"type": "tool_use", "id": "toolu_ask", "name": "AskUserQuestion",
                          "input": {"questions": [{"question": "Which?"}]}}, 0),
        _assistant("a7", {"type": "text", "text": "Done. Tests pass."}, 0, stop="end_turn"),
    ]


def codex_fixture(repo: str) -> list:
    def line(kind, payload, ordinal):
        return {"timestamp": TS, "type": kind, "payload": payload, "ordinal": ordinal}
    return [
        line("session_meta", {"id": THREAD, "session_id": THREAD, "timestamp": TS, "cwd": repo,
                              "originator": "codex_exec", "cli_version": "0.149.0", "source": "exec"}, 1),
        line("turn_context", {"turn_id": "t1", "cwd": repo, "model": "gpt-5.6", "effort": "high"}, 2),
        line("event_msg", {"type": "task_started", "turn_id": "t1", "started_at": 1}, 3),
        line("event_msg", {"type": "user_message", "message": "port the hooks", "images": []}, 4),
        line("response_item", {"type": "reasoning", "id": "rs_1", "summary": [{"type": "summary_text", "text": "**Planning**"}],
                               "encrypted_content": "gAAAA"}, 5),
        line("response_item", {"type": "message", "id": "msg_1", "role": "assistant", "phase": "commentary",
                               "content": [{"type": "output_text", "text": "Looking at the hooks."}]}, 6),
        line("response_item", {"type": "custom_tool_call", "id": "ctc_1", "status": "completed", "call_id": "call_1",
                               "name": "exec", "input": "await tools.exec_command({cmd: 'ls'})"}, 7),
        line("event_msg", {"type": "exec_command_end", "call_id": "call_1", "turn_id": "t1", "command": ["ls"],
                           "stdout": "a\nb\n", "stderr": "", "aggregated_output": "a\nb\n", "exit_code": 0}, 8),
        line("response_item", {"type": "custom_tool_call_output", "id": "ctco_1", "call_id": "call_1",
                               "output": [{"type": "input_text", "text": "a\nb\n"}]}, 9),
        line("response_item", {"type": "function_call", "id": "fc_1", "name": "write_stdin", "call_id": "call_2",
                               "arguments": json.dumps({"cell_id": "24", "path": f"{repo}/x.py"})}, 10),
        line("response_item", {"type": "function_call_output", "id": "fco_1", "call_id": "call_2", "output": "ok"}, 11),
        line("response_item", {"type": "custom_tool_call", "id": "ctc_2", "status": "completed", "call_id": "call_3",
                               "name": "apply_patch", "input": "*** Begin Patch\n*** Update File: x.py\n"}, 12),
        line("event_msg", {"type": "patch_apply_end", "call_id": "call_3", "turn_id": "t1", "stdout": "", "stderr": "",
                           "success": True, "changes": {f"{repo}/x.py": {"type": "update", "unified_diff": "-a\n+b"}}}, 13),
        line("response_item", {"type": "custom_tool_call_output", "id": "ctco_2", "call_id": "call_3",
                               "output": [{"type": "input_text", "text": "Success"}]}, 14),
        line("response_item", {"type": "message", "id": "msg_2", "role": "assistant", "phase": "final_answer",
                               "content": [{"type": "output_text", "text": "Ported."}]}, 15),
        line("event_msg", {"type": "task_complete", "turn_id": "t1", "last_agent_message": "Ported."}, 16),
        line("compacted", {"message": "", "replacement_history": [], "window_id": "w2"}, 17),
        line("event_msg", {"type": "turn_aborted", "turn_id": "t2", "reason": "interrupted"}, 18),
        line("response_item", {"type": "message", "id": "msg_3", "role": "user",
                               "content": [{"type": "input_text", "text": "<environment_context>injected</environment_context>"}]}, 19),
    ]


# ---------------------------------------------------------------- fake relay


class FakeRelay(BaseHTTPRequestHandler):
    """Answers like the contract says a relay does, and records what it saw."""

    room = "k7mq-p3xw-4h"
    join_code = room + "." + "b6hj3kx9w2mrp4tq7vy8zn5c"
    token = "at-" + "7x2kq9mw4hp3vb8nt5rj6zc2yd4fg7hk"
    batches: list = []
    bodies: list = []
    fail_next: list = []            # status codes to answer before behaving
    reject: dict = {}               # id -> why
    oversize_ids: set = set()
    max_events = 200
    lock = threading.Lock()

    @classmethod
    def reset(cls):
        with cls.lock:
            cls.batches, cls.bodies, cls.fail_next = [], [], []
            cls.reject, cls.oversize_ids, cls.max_events = {}, set(), 200

    def log_message(self, *args):
        pass

    def _json(self, code, payload, headers=None):
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(raw)

    def _read(self):
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _auth(self):
        header = self.headers.get("Authorization") or ""
        return header[len("Bearer "):] if header.startswith("Bearer ") else ""

    def do_POST(self):
        path = self.path.split("?")[0]
        body = self._read()
        if path == "/rooms":
            self._json(201, {"room": self.room, "join_code": self.join_code,
                             "policy": {"max_stream": "tools", "retention_min": 120, "ticket_ttl_s": 600, "ask_ttl_s": 86400},
                             "relay": "http://relay", "url": "http://relay/#" + self.room})
            return
        if path.startswith(f"/r/{self.room}/agents/"):
            if self._auth() != self.join_code:
                self._json(401, {"error": "auth", "hint": "join code unknown"})
                return
            self._json(200, {"token": self.token, "rseq": 7, "effective_stream": "tools",
                             "policy": {"max_stream": "tools", "retention_min": 120, "ticket_ttl_s": 600, "ask_ttl_s": 86400}})
            return
        if path == f"/r/{self.room}/events":
            with self.lock:
                if FakeRelay.fail_next:
                    code = FakeRelay.fail_next.pop(0)
                    headers = {"Retry-After": "0"} if code == 503 else ({"Retry-After": "2"} if code == 429 else {})
                    self._json(code, {"error": str(code), "hint": "as asked"}, headers)
                    return
            if self._auth() != self.token:
                self._json(401, {"error": "auth", "hint": "token unknown or rotated"})
                return
            lines = [l for l in body.decode("utf-8").split("\n") if l.strip()]
            if len(body) > 65536 or len(lines) > 200 or len(lines) > FakeRelay.max_events:
                self._json(413, {"error": "oversize", "hint": "split the batch"})
                return
            events = [json.loads(l) for l in lines]
            if any(e.get("id") in FakeRelay.oversize_ids for e in events):
                self._json(413, {"error": "oversize", "hint": "one event is too big"})
                return
            rejected = [{"id": e["id"], "why": FakeRelay.reject[e["id"]]} for e in events if e.get("id") in FakeRelay.reject]
            with self.lock:
                FakeRelay.batches.append(events)
                FakeRelay.bodies.append(body)
            self._json(202, {"rseq": len(FakeRelay.batches), "accepted": len(events) - len(rejected),
                             "dup": 0, "rejected": rejected})
            return
        self._json(404, {"error": "room", "hint": "no such route"})

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == f"/r/{self.room}/inbox":
            if self._auth() != self.token:
                self._json(401, {"error": "auth", "hint": "token unknown"})
                return
            self._json(200, {"rseq": 9, "role": {"role": "reviewer", "viewer": "mehar", "set_seq": 5, "ts": TS},
                             "asks": [{"id": "ca-k7mq-8", "seq": 8, "viewer": "sam", "text": "why?", "ts": TS}]})
            return
        self._json(404, {"error": "room", "hint": "no such route"})

    def do_PUT(self):
        self._read()
        if self.path.startswith(f"/r/{self.room}/roles/") and self._auth() == self.token:
            self._json(200, {"set_seq": 11})
            return
        self._json(404, {"error": "room", "hint": "no such route"})

    def do_DELETE(self):
        if self.path.startswith(f"/r/{self.room}/agents/") and self._auth() == self.token:
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._json(404, {"error": "room", "hint": "no such route"})


class RelayCase(unittest.TestCase):
    """A fake relay for the whole class; a fresh store per test."""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeRelay)
        cls.server.daemon_threads = True
        cls.relay = f"http://127.0.0.1:{cls.server.server_port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        FakeRelay.reset()
        self.work = Path(tempfile.mkdtemp(prefix="agentcolab-canvas-"))
        self.addCleanup(shutil.rmtree, self.work, ignore_errors=True)
        os.environ["AGENTCOLAB_HOME"] = str(self.work / "home")
        self.addCleanup(os.environ.pop, "AGENTCOLAB_HOME", None)
        self.repo = self.work / "repo"
        self.repo.mkdir()
        for cmd in (["git", "init", "-q", "-b", "main"],
                    ["git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q", "--allow-empty", "-m", "init"]):
            subprocess.run(cmd, cwd=self.repo, check=True, capture_output=True)
        self.store = Store(root=self.repo)
        self.store.save_config({"agent": "alice-claude-code", "human": "alice", "harness": "claude-code",
                                "model": "claude-fable-5-1", "role": "contributor",
                                "canvas": {"relay": self.relay, "room": FakeRelay.room, "token": FakeRelay.token,
                                           "name": "alice-claude-code", "stream": "tools"}})
        self.transcript = self.work / "claude" / f"{SID}.jsonl"
        _write_jsonl(self.transcript, claude_fixture(str(self.repo)))

    def events(self):
        return [e for batch in FakeRelay.batches for e in batch]


# ---------------------------------------------------------------- parsers


class ClaudeParser(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="agentcolab-canvas-"))
        self.addCleanup(shutil.rmtree, self.work, ignore_errors=True)
        self.repo = self.work / "repo"
        self.repo.mkdir()
        self.path = self.work / f"{SID}.jsonl"
        _write_jsonl(self.path, claude_fixture(str(self.repo)))

    def tail(self, **kwargs):
        tailer = canvas.ClaudeTailer(self.path, session=SID, repo_root=self.repo, **kwargs)
        return tailer, tailer.poll()

    def test_every_kind_is_produced_and_meta_is_skipped(self):
        tailer, events = self.tail()
        kinds = [e["kind"] for e in events]
        for kind in ("prompt", "thinking", "text", "tool_call", "tool_result", "session"):
            self.assertIn(kind, kinds)
        self.assertNotIn("skill body, never shown", json.dumps(events))
        self.assertEqual(tailer.model, "claude-fable-5-1")
        self.assertEqual(tailer.title, "Canvas design")
        prompt = next(e for e in events if e["kind"] == "prompt")
        self.assertEqual(prompt["body"]["text"], "Fix the failing test in tests/test_units.py")
        self.assertEqual(prompt["seq"], 1 * 256)
        self.assertEqual(prompt["id"], records.content_id("ev", SID, "u1", "0"))

    def test_block_index_comes_from_the_api_block_index(self):
        _, events = self.tail()
        edit = next(e for e in events if e["kind"] == "tool_call" and e["body"]["name"] == "Edit")
        self.assertEqual(edit["seq"] % 256, 2)
        self.assertEqual(edit["ref"], "toolu_edit1")
        self.assertEqual(edit["body"]["paths"], ["agentcolab/hooks.py"])
        result = next(e for e in events if e["kind"] == "tool_result" and e["ref"] == "toolu_edit1")
        self.assertEqual(result["body"]["paths"], ["agentcolab/hooks.py"])
        self.assertTrue(result["body"]["ok"])
        self.assertEqual(result["body"]["exit"], 0)

    def test_twenty_tool_results_on_one_line_get_twenty_seqs(self):
        _, events = self.tail()
        many = [e for e in events if e["kind"] == "tool_result" and e["ref"].startswith("toolu_many")]
        self.assertEqual(len(many), 20)
        self.assertEqual(len({e["seq"] for e in many}), 20)
        self.assertEqual(len({e["id"] for e in many}), 20)
        self.assertTrue(all(e["seq"] >> 8 == many[0]["seq"] >> 8 for e in many))

    def test_final_error_abort_compact_and_ask(self):
        _, events = self.tail()
        finals = [e for e in events if e["kind"] == "text" and e["body"]["final"]]
        self.assertEqual(len(finals), 1)
        states = [e["body"]["state"] for e in events if e["kind"] == "session"]
        self.assertIn("compact", states)
        self.assertIn("error", states)
        self.assertNotIn("Rate limited", [e["body"].get("text") for e in events if e["kind"] == "text"])
        image = next(e for e in events if e["kind"] == "tool_result" and e["ref"] == "toolu_img")
        self.assertTrue(image["body"]["image"])
        self.assertEqual(image["body"]["media_type"], "image/png")
        self.assertNotIn("AAAA", json.dumps(image))

    def test_two_tailers_agree_on_ids_and_positions(self):
        one = canvas.ClaudeTailer(self.path, session=SID)
        two = canvas.ClaudeTailer(self.path, session=SID)
        first = [(e["id"], e["epoch"], e["seq"]) for e in one.poll()]
        second = [(e["id"], e["epoch"], e["seq"]) for e in two.poll()]
        self.assertEqual(first, second)
        self.assertGreater(len(first), 20)

    def test_a_partial_trailing_line_is_held_until_its_newline(self):
        tailer, events = self.tail()
        tailer.ack()
        before = tailer.line
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(_assistant("a9", {"type": "text", "text": "half"}, 0))[:40])
        self.assertEqual(tailer.poll(), [])
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(_assistant("a9", {"type": "text", "text": "half"}, 0))[40:] + "\n")
        more = tailer.poll()
        self.assertEqual(len(more), 1)
        self.assertEqual(more[0]["body"]["text"], "half")
        self.assertEqual(more[0]["seq"] >> 8, tailer.line)
        self.assertGreater(more[0]["seq"] >> 8, before)

    def test_a_line_over_four_mebibytes_becomes_a_gap(self):
        _append_jsonl(self.path, [_assistant("big", {"type": "text", "text": "x" * (5 * 1024 * 1024)}, 0),
                                  _assistant("after", {"type": "text", "text": "after"}, 0)])
        tailer = canvas.ClaudeTailer(self.path, session=SID, backlog_max=1 << 40)
        events = []
        while True:
            got = tailer.poll(budget=1 << 30)
            if not got:
                break
            events.extend(got)
            tailer.ack()
        gaps = [e for e in events if e["kind"] == "gap"]
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["body"]["reason"], "oversize")
        self.assertEqual(events[-1]["body"]["text"], "after")
        self.assertEqual(events[-1]["seq"] >> 8, (gaps[0]["seq"] >> 8) + 1)

    def test_an_inode_change_bumps_the_epoch_and_says_rewrite(self):
        tailer, events = self.tail()
        tailer.ack()
        self.assertEqual(tailer.state["epoch"], 0)
        replacement = self.work / "new.jsonl"
        _write_jsonl(replacement, claude_fixture(str(self.repo))[:3])
        os.replace(replacement, self.path)
        again = tailer.poll()
        self.assertEqual(again[0]["kind"], "gap")
        self.assertEqual(again[0]["body"]["reason"], "rewrite")
        self.assertTrue(all(e["epoch"] == 1 for e in again))
        tailer.ack()
        self.assertEqual(tailer.state["epoch"], 1)

    def test_subagent_lane_comes_from_the_file_name(self):
        side = self.work / SID / "subagents" / "agent-a0e7d2988770c5f32.jsonl"
        _write_jsonl(side, [_claude_entry("user", "su1", isSidechain=True, agentId="a0e7d2988770c5f32",
                                          message={"role": "user", "content": "child prompt"}),
                            _assistant("sa1", {"type": "text", "text": "child text"}, 0, isSidechain=True,
                                       agentId="a0e7d2988770c5f32")])
        tailer = canvas.ClaudeTailer(side, session=SID, lane=canvas._lane_of(side))
        events = tailer.poll()
        self.assertEqual({e["lane"] for e in events}, {"a0e7d2988770c5f32"})
        self.assertEqual(events[0]["kind"], "prompt")
        self.assertEqual(events[0]["body"]["source"], "parent")


class CodexParser(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="agentcolab-canvas-"))
        self.addCleanup(shutil.rmtree, self.work, ignore_errors=True)
        self.repo = self.work / "repo"
        self.repo.mkdir()
        self.path = self.work / f"rollout-2026-09-04T10-00-00-{THREAD}.jsonl"
        _write_jsonl(self.path, codex_fixture(str(self.repo)))

    def test_mapping(self):
        tailer = canvas.make_tailer(self.path, session=THREAD, repo_root=self.repo)
        self.assertIsInstance(tailer, canvas.CodexTailer)
        events = tailer.poll()
        kinds = [e["kind"] for e in events]
        self.assertEqual(kinds.count("prompt"), 1)
        self.assertEqual(kinds.count("thinking"), 1)
        self.assertEqual(kinds.count("text"), 2)
        self.assertEqual(kinds.count("tool_call"), 3)
        self.assertEqual(kinds.count("tool_result"), 3)
        self.assertEqual(tailer.cwd, str(self.repo))
        self.assertEqual(tailer.model, "gpt-5.6")
        self.assertNotIn("injected", json.dumps(events))
        texts = [e for e in events if e["kind"] == "text"]
        self.assertFalse(texts[0]["body"]["final"])
        self.assertTrue(texts[1]["body"]["final"])
        exec_call = next(e for e in events if e["kind"] == "tool_call" and e["body"]["name"] == "exec")
        self.assertEqual(exec_call["ref"], "call_1")
        exec_out = next(e for e in events if e["kind"] == "tool_result" and e["ref"] == "call_1")
        self.assertEqual(exec_out["body"]["exit"], 0)
        self.assertEqual(exec_out["body"]["lines"], 3)
        fn = next(e for e in events if e["kind"] == "tool_call" and e["body"]["name"] == "write_stdin")
        self.assertEqual(fn["body"]["args"]["cell_id"], "24")
        self.assertEqual(fn["body"]["paths"], ["x.py"])
        patch_out = next(e for e in events if e["kind"] == "tool_result" and e["ref"] == "call_3")
        self.assertEqual(patch_out["body"]["paths"], ["x.py"])
        states = [e["body"]["state"] for e in events if e["kind"] == "session"]
        for state in ("start", "idle", "compact", "abort"):
            self.assertIn(state, states)
        self.assertEqual(events[0]["id"], records.content_id("ev", THREAD, "line:1", "0"))
        self.assertTrue(all(e["harness"] == "codex" for e in events))

    def test_a_subagent_rollout_knows_its_parent(self):
        entries = codex_fixture(str(self.repo))
        entries[0]["payload"]["source"] = {"subagent": {"thread_spawn": {"parent_thread_id": "parent-1", "depth": 1}}}
        _write_jsonl(self.path, entries)
        tailer = canvas.CodexTailer(self.path, session=THREAD)
        tailer.poll()
        self.assertEqual(tailer.parent, "parent-1")


# ---------------------------------------------------------------- sanitise


class Sanitise(unittest.TestCase):
    repo = Path("/repo/checkout")

    def event(self, kind, body, **kw):
        return canvas.build(kind, body, session=SID, harness="claude-code", **kw)

    def test_images_are_dropped_and_marked(self):
        event = self.event("tool_result", {"ok": True, "exit": 0, "bytes": 0, "lines": 0, "paths": [], "image": True,
                                           "media_type": "image/png", "text": "",
                                           "extra": [{"type": "image", "source": {"data": "AAAA" * 100, "media_type": "image/png"}}]})
        clean = canvas.sanitise(event, "full", self.repo)
        self.assertNotIn("AAAA", json.dumps(clean))
        self.assertEqual(clean["body"]["extra"], [{"image": True, "media_type": "image/png"}])

    def test_repo_and_home_are_rewritten(self):
        home = str(Path.home())
        event = self.event("text", {"text": f"see /repo/checkout/a.py and {home}/.ssh/id", "final": True})
        clean = canvas.sanitise(event, "tools", self.repo)
        self.assertEqual(clean["body"]["text"], "see ./a.py and ~/.ssh/id")

    def test_head_and_tail_truncation_records_the_size(self):
        event = self.event("text", {"text": "H" * 40000 + "T" * 40000, "final": False})
        clean = canvas.sanitise(event, "tools", self.repo)
        body = clean["body"]
        self.assertTrue(body["truncated"])
        self.assertEqual(body["bytes"], 80000)
        self.assertTrue(body["text"].startswith("H"))
        self.assertTrue(body["text"].endswith("T"))
        self.assertIn("bytes cut", body["text"])
        self.assertLessEqual(len(body["text"].encode("utf-8")), canvas.CAPS["text"])
        self.assertEqual(canvas.validate(clean), [])

    def test_scrub_reaches_nested_strings(self):
        event = self.event("tool_call", {"name": "Bash", "args": {"command": f"curl -H 'x: {FAKE_GITHUB_TOKEN}'",
                                                                  "env": {"nested": {"deep": FAKE_GITHUB_TOKEN}}},
                                         "paths": [], "omitted": {}})
        clean = canvas.sanitise(event, "full", self.repo)
        self.assertNotIn(FAKE_GITHUB_TOKEN, json.dumps(clean))
        self.assertIn("REDACTED", clean["body"]["args"]["env"]["nested"]["deep"])

    def test_credential_shaped_blobs_are_withheld(self):
        event = self.event("tool_result", {"ok": True, "exit": 0, "bytes": 1, "lines": 1, "paths": [], "image": False,
                                           "text": f"value {FAKE_BLOB} end"})
        clean = canvas.sanitise(event, "full", self.repo)
        self.assertNotIn(FAKE_BLOB, clean["body"]["text"])
        self.assertIn("[WITHHELD]", clean["body"]["text"])

    def test_control_characters_go_but_newlines_stay(self):
        event = self.event("text", {"text": "a\x00b\x07c\nd\te", "final": True})
        self.assertEqual(canvas.sanitise(event, "tools", self.repo)["body"]["text"], "abc\nd\te")

    def test_policy_at_every_level(self):
        edit = self.event("tool_call", {"name": "Edit", "args": {"file_path": "/repo/checkout/a.py",
                                                                 "old_string": "one\ntwo", "new_string": "three"},
                                        "paths": ["/repo/checkout/a.py"], "omitted": {}}, ref="toolu_1")
        thinking = self.event("thinking", {"text": "hmm"})
        result = self.event("tool_result", {"ok": True, "exit": 0, "bytes": 3, "lines": 1, "paths": [], "image": False,
                                            "text": "out"})
        text = self.event("text", {"text": "hello", "final": True})
        prompt = self.event("prompt", {"text": "p" * 500})
        self.assertIsNone(canvas.sanitise(edit, "off", self.repo))
        # summary
        body = canvas.sanitise(edit, "summary", self.repo)["body"]
        self.assertEqual(body["args"], {})
        self.assertEqual(body["paths"], ["a.py"])
        self.assertIn("old_string", body["omitted"])
        self.assertIsNone(canvas.sanitise(thinking, "summary", self.repo))
        self.assertIsNone(canvas.sanitise(result, "summary", self.repo))
        self.assertIsNone(canvas.sanitise(text, "summary", self.repo))
        short = canvas.sanitise(prompt, "summary", self.repo)["body"]
        self.assertEqual(len(short["text"]), 200)
        self.assertTrue(short["truncated"])
        # tools
        body = canvas.sanitise(edit, "tools", self.repo)["body"]
        self.assertEqual(body["args"], {"file_path": "./a.py"})
        self.assertEqual(body["omitted"], {"old_string": {"bytes": 7, "lines": 2}, "new_string": {"bytes": 5, "lines": 1}})
        self.assertIsNone(canvas.sanitise(thinking, "tools", self.repo))
        tools_result = canvas.sanitise(result, "tools", self.repo)["body"]
        self.assertNotIn("text", tools_result)
        self.assertEqual(tools_result["bytes"], 3)
        self.assertEqual(canvas.sanitise(text, "tools", self.repo)["body"]["text"], "hello")
        self.assertEqual(len(canvas.sanitise(prompt, "tools", self.repo)["body"]["text"]), 500)
        # full
        body = canvas.sanitise(edit, "full", self.repo)["body"]
        self.assertEqual(body["args"]["new_string"], "three")
        self.assertEqual(canvas.sanitise(thinking, "full", self.repo)["body"]["text"], "hmm")
        self.assertEqual(canvas.sanitise(result, "full", self.repo)["body"]["text"], "out")

    def test_content_keys_are_capped_at_two_kib_at_full(self):
        write = self.event("tool_call", {"name": "Write", "args": {"file_path": "x", "content": "c" * 10000},
                                         "paths": [], "omitted": {}})
        body = canvas.sanitise(write, "full", self.repo)["body"]
        self.assertLessEqual(len(body["args"]["content"].encode("utf-8")), canvas.CONTENT_KEY_MAX + 64)
        self.assertEqual(body["omitted"]["content"]["bytes"], 10000)

    def test_effective_level_is_the_minimum_of_three(self):
        self.assertEqual(canvas.effective_level("full", "tools", "full"), "tools")
        self.assertEqual(canvas.effective_level("full", None, "summary"), "summary")
        self.assertEqual(canvas.effective_level("tools", "full", "full"), "tools")
        self.assertEqual(canvas.effective_level(None, None, None), "tools")
        self.assertEqual(canvas.effective_level("off", "full", "full"), "off")


# ---------------------------------------------------------------- schema


class Schema(unittest.TestCase):
    def test_validate_finds_what_the_relay_would(self):
        good = canvas.build("text", {"text": "x", "final": True}, session=SID, harness="claude-code")
        self.assertEqual(canvas.validate(good), [])
        oversize = dict(good, body={"text": "x" * 50000, "final": True})
        self.assertTrue(any(p.startswith("oversize") for p in canvas.validate(oversize)))
        self.assertTrue(any(p.startswith("kind") for p in canvas.validate(dict(good, kind="bogus"))))
        missing = dict(good)
        del missing["id"]
        self.assertTrue(any(p.startswith("id") for p in canvas.validate(missing)))
        self.assertTrue(canvas.validate(dict(good, lane="")))
        self.assertTrue(canvas.validate(dict(good, seq=-1)))
        gap = canvas.gap("backlog", 1, 2, 1, session=SID)
        self.assertEqual(canvas.validate(gap), [])
        self.assertTrue(canvas.validate(canvas.gap("nope", 1, 2, 1, session=SID)))

    def test_batches_respect_both_limits(self):
        small = [canvas.build("text", {"text": str(i), "final": True}, session=SID) for i in range(450)]
        split = canvas.batches(small)
        self.assertEqual([len(b) for b in split], [200, 200, 50])
        big = [canvas.build("text", {"text": "y" * 30000, "final": True}, session=SID) for _ in range(5)]
        split = canvas.batches(big)
        self.assertTrue(all(sum(len(line.encode("utf-8")) + 1 for _, line in b) <= canvas.BATCH_BYTES for b in split))
        self.assertEqual(sum(len(b) for b in split), 5)

    def test_iso_ms_has_millisecond_precision(self):
        stamp = canvas.iso_ms()
        self.assertRegex(stamp, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")

    def test_derive_state(self):
        def ev(kind, **body):
            ref = body.pop("ref", None)
            return {"kind": kind, "ref": ref, "ts": TS, "body": body}
        self.assertEqual(canvas.derive_state([])[0], "idle")
        self.assertEqual(canvas.derive_state([ev("prompt")])[0], "working")
        self.assertEqual(canvas.derive_state([ev("text", final=False)])[0], "working")
        self.assertEqual(canvas.derive_state([ev("text", final=True)])[0], "idle")
        state, tool = canvas.derive_state([ev("tool_call", name="Bash", ref="t1")])
        self.assertEqual((state, tool["name"], tool["ref"]), ("tool", "Bash", "t1"))
        self.assertEqual(canvas.derive_state([ev("tool_call", name="Bash", ref="t1"), ev("tool_result", ref="t1")])[0], "idle")
        self.assertEqual(canvas.derive_state([ev("tool_call", name="AskUserQuestion", ref="t2")])[0], "waiting")
        self.assertEqual(canvas.derive_state([ev("tool_result", ref="t3", denied=True)])[0], "waiting")
        self.assertEqual(canvas.derive_state([ev("text", final=True), ev("session", state="end")])[0], "gone")


# ---------------------------------------------------------------- offsets, spool


class Offsets(RelayCase):
    def daemon(self):
        return canvas._Daemon(self.store, SID, self.transcript)

    def test_offset_moves_only_on_ack(self):
        FakeRelay.fail_next[:] = [503, 503]
        daemon = self.daemon()
        for _ in range(2):
            daemon.step()
            saved = canvas.Offsets(self.store, SID)
            self.assertEqual(saved.state_for(self.transcript)["offset"], 0, "advanced without an ack")
        self.assertEqual(FakeRelay.batches, [])
        daemon.step()
        saved = canvas.Offsets(self.store, SID)
        self.assertEqual(saved.state_for(self.transcript)["offset"], self.transcript.stat().st_size)
        self.assertGreater(saved.state_for(self.transcript)["seq"], 0)
        self.assertTrue(FakeRelay.batches)

    def test_a_backlog_over_one_mebibyte_is_skipped_with_a_gap(self):
        entries = [_assistant(f"bl{i}", {"type": "text", "text": "z" * 1000}, 0) for i in range(1200)]
        _append_jsonl(self.transcript, entries)
        total = len(claude_fixture(str(self.repo))) + 1200
        tailer = canvas.ClaudeTailer(self.transcript, session=SID)
        events = tailer.poll()
        self.assertEqual([e["kind"] for e in events], ["gap"])
        self.assertEqual(events[0]["body"]["reason"], "backlog")
        self.assertEqual(events[0]["body"]["count"], total)
        self.assertEqual(events[0]["seq"], total * 256)
        tailer.ack()
        _append_jsonl(self.transcript, [_assistant("tail1", {"type": "text", "text": "now"}, 0)])
        more = tailer.poll()
        self.assertEqual(more[0]["body"]["text"], "now")
        self.assertEqual(more[0]["seq"] >> 8, total + 1)

    def test_pending_file_drops_its_head_with_a_gap(self):
        payload = {"family": "msg", "kind": "note", "rid": "m-1", "from": "a", "to": "*", "subject": "s" * 900,
                   "paths": [], "reply_to": None, "task": None, "state": None, "owner": None, "blocked_by": [],
                   "trust": "unverified", "ts": TS}
        for index in range(700):
            event = canvas.build("record", payload, session=SID, id=f"rec-{index:016x}", seq=index)
            canvas.spool(self.store, SID, event)
        path = canvas._pending_path(self.store, SID)
        self.assertLessEqual(path.stat().st_size, canvas.PENDING_MAX + 2048)
        _, events = canvas.read_pending(self.store, SID)
        self.assertEqual(events[0]["kind"], "gap")
        self.assertEqual(events[0]["body"]["reason"], "spool")
        self.assertGreater(events[0]["body"]["count"], 0)
        self.assertEqual(events[-1]["id"], f"rec-{699:016x}")

    def test_consume_pending_keeps_a_concurrent_append(self):
        first = canvas.build("answer", {"ask": "ca-k7mq-1", "text": "yes"}, session=SID)
        canvas.spool(self.store, SID, first)
        data, events = canvas.read_pending(self.store, SID)
        second = canvas.build("answer", {"ask": "ca-k7mq-2", "text": "no"}, session=SID)
        canvas.spool(self.store, SID, second)
        canvas.consume_pending(self.store, SID, data)
        _, left = canvas.read_pending(self.store, SID)
        self.assertEqual([e["id"] for e in left], [second["id"]])

    def test_429_records_retry_after_and_the_next_flush_does_not_sleep(self):
        FakeRelay.fail_next[:] = [429]
        daemon = self.daemon()
        daemon.step()
        self.assertEqual(FakeRelay.batches, [])
        saved = canvas.Offsets(self.store, SID)
        self.assertIsNotNone(saved.retry_after)
        self.assertGreater(records.parse_iso(saved.retry_after).timestamp(), time.time() - 1)
        start = time.time()
        daemon.step()
        self.assertLess(time.time() - start, 0.05)
        self.assertEqual(FakeRelay.batches, [])
        fresh = canvas._Daemon(self.store, SID, self.transcript)
        self.assertFalse(fresh.poster.ready(), "retry_after did not survive a restart")


# ---------------------------------------------------------------- poster


class PosterAgainstFakeRelay(RelayCase):
    def poster(self, token=None):
        return canvas.Poster(self.relay, FakeRelay.room, token or FakeRelay.token, timeout=3)

    def events(self, count, size=10):
        # Explicit ids: synthetic ids hash the timestamp, and a loop mints several
        # events in one millisecond.
        return [canvas.build("text", {"text": "e" * size + str(i), "final": True}, session=SID, seq=i, harness="x",
                             id=records.content_id("ev", SID, "test", str(i), str(size)))
                for i in range(count)]

    def test_202_acks_and_rejected_ids_become_gaps(self):
        events = self.events(3)
        FakeRelay.reject[events[1]["id"]] = "policy"
        gaps = []
        acked = self.poster().send(events, gaps)
        self.assertEqual(acked, {e["id"] for e in events})
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["body"]["reason"], "policy")
        self.assertEqual(gaps[0]["body"]["from_seq"], events[1]["seq"])
        self.assertEqual(FakeRelay.bodies[0].count(b"\n"), 3)
        self.assertTrue(FakeRelay.bodies[0].endswith(b"\n"))

    def test_413_splits_the_batch_in_half(self):
        FakeRelay.max_events = 3
        events = self.events(10)
        acked = self.poster().send(events, [])
        self.assertEqual(acked, {e["id"] for e in events})
        self.assertTrue(all(len(batch) <= 3 for batch in FakeRelay.batches))
        self.assertEqual([e["id"] for b in FakeRelay.batches for e in b], [e["id"] for e in events])

    def test_413_on_a_single_event_substitutes_a_gap(self):
        events = self.events(1)
        FakeRelay.oversize_ids.add(events[0]["id"])
        acked = self.poster().send(events, [])
        self.assertEqual(acked, {events[0]["id"]})
        self.assertEqual(FakeRelay.batches[0][0]["kind"], "gap")
        self.assertEqual(FakeRelay.batches[0][0]["body"]["reason"], "oversize")

    def test_429_sets_retry_after_without_sleeping(self):
        FakeRelay.fail_next[:] = [429]
        poster = self.poster()
        start = time.time()
        acked = poster.send(self.events(2), [])
        self.assertEqual(acked, set())
        self.assertLess(time.time() - start, 1.0)
        self.assertFalse(poster.ready())
        self.assertLessEqual(poster.retry_after - time.time(), 2.1)
        again = time.time()
        self.assertEqual(poster.send(self.events(2), []), set())
        self.assertLess(time.time() - again, 0.05)

    def test_three_auth_failures_mark_the_room_gone(self):
        poster = self.poster(token="at-" + "wrong" * 6)
        for _ in range(3):
            poster.retry_after = 0
            poster.send(self.events(1), [])
        self.assertTrue(poster.gone)
        self.assertEqual(FakeRelay.batches, [])

    def test_a_network_error_keeps_everything_and_backs_off(self):
        poster = canvas.Poster("http://127.0.0.1:9", FakeRelay.room, FakeRelay.token, timeout=1)
        acked = poster.send(self.events(2), [])
        self.assertEqual(acked, set())
        self.assertFalse(poster.ready())

    def test_register_inbox_role_leave(self):
        room = canvas.new_room(self.relay, "agentcolab")
        self.assertEqual(room["room"], FakeRelay.room)
        reg = canvas.register(self.relay, room["join_code"], "alice-claude-code", harness="claude-code",
                              human="alice", model="m", stream="tools")
        self.assertEqual(reg["token"], FakeRelay.token)
        self.assertEqual(reg["room"], FakeRelay.room)
        inbox = canvas.pull_inbox_raw(self.relay, FakeRelay.room, FakeRelay.token, 4)
        self.assertEqual(inbox["asks"][0]["id"], "ca-k7mq-8")
        self.assertEqual(canvas.put_role(self.relay, FakeRelay.room, FakeRelay.token, "alice-claude-code", "reviewer")["set_seq"], 11)
        self.assertTrue(canvas.leave(self.relay, FakeRelay.room, FakeRelay.token, "alice-claude-code"))
        with self.assertRaises(RuntimeError):
            canvas.register(self.relay, FakeRelay.room + ".bad", "x")

    def test_answer_posts_one_event(self):
        target = {"id": "ca-k7mq-4791", "source": "canvas", "session": SID}
        self.assertTrue(canvas.answer(self.store, target, "The hook path changed."))
        self.assertEqual(FakeRelay.batches[0][0]["kind"], "answer")
        self.assertEqual(FakeRelay.batches[0][0]["ref"], "ca-k7mq-4791")
        self.assertEqual(FakeRelay.batches[0][0]["body"]["ask"], "ca-k7mq-4791")


# ---------------------------------------------------------------- records, integration


class Mirroring(RelayCase):
    def test_records_are_mirrored_once_per_content_id(self):
        me = self.store.agent
        self.store.put(f"msgs/{me}/m-1.json", {"id": "m-1", "kind": "question", "agent": me, "to": "bob-codex",
                                               "subject": "does the hook run under Windows?", "paths": ["agentcolab/hooks.py"],
                                               "needs_reply": True, "ts": "2026-09-04T10:15:00Z"})
        self.store.put(f"claims/{me}/c-1.json", {"id": "c-1", "kind": "claim", "agent": me, "paths": ["src/pay.py"],
                                                 "reason": "rounding", "ts": "2026-09-04T10:16:00Z",
                                                 "expires_at": "2099-01-01T00:00:00Z"})
        self.store.put(f"tasks/{me}/t-1.json", {"id": "t-1", "kind": "task", "agent": me, "title": "port hooks",
                                                "state": "open", "created_at": "2026-09-04T10:00:00Z",
                                                "updated_at": "2026-09-04T10:00:00Z", "deps": []})
        self.store.put(f"tasks/{me}/t-2.json", {"id": "t-2", "kind": "task", "agent": me, "title": "docs",
                                                "state": "open", "created_at": "2026-09-04T10:01:00Z",
                                                "updated_at": "2026-09-04T10:01:00Z", "deps": ["t-1"]})
        self.store.put(f"tasks/{me}/take-t-1.json", {"id": "take-t-1", "kind": "take", "task": "t-1", "agent": me,
                                                     "created_at": "2026-09-04T10:02:00Z",
                                                     "expires_at": "2099-01-01T00:00:00Z"})
        events, seen = canvas.mirror_records(self.store, {}, session=SID, harness="claude-code", agent=me)
        by_rid = {e["body"]["rid"]: e for e in events}
        self.assertEqual(set(by_rid), {"m-1", "c-1", "t-1", "t-2"})
        self.assertEqual(by_rid["m-1"]["body"]["family"], "msg")
        self.assertEqual(by_rid["m-1"]["body"]["to"], "bob-codex")
        self.assertEqual(by_rid["m-1"]["ref"], "m-1")
        self.assertEqual(by_rid["t-1"]["body"]["state"], "taken")
        self.assertEqual(by_rid["t-1"]["body"]["owner"], me)
        self.assertEqual(by_rid["t-2"]["body"]["blocked_by"], ["t-1"])
        self.assertEqual(by_rid["c-1"]["body"]["state"], "active")
        self.assertTrue(all(e["id"].startswith("rec-") for e in events))
        self.assertTrue(all(canvas.validate(e) == [] for e in events))
        again, _ = canvas.mirror_records(self.store, seen, session=SID, harness="claude-code", agent=me)
        self.assertEqual(again, [])
        self.store.put(f"claims/{me}/c-1.json", {"id": "c-1", "kind": "claim", "agent": me, "paths": ["src/pay.py"],
                                                 "reason": "rounding", "ts": "2026-09-04T10:16:00Z",
                                                 "released_at": "2026-09-04T11:00:00Z"})
        changed, _ = canvas.mirror_records(self.store, seen, session=SID, harness="claude-code", agent=me)
        self.assertEqual([e["body"]["rid"] for e in changed], ["c-1"])
        self.assertEqual(changed[0]["body"]["state"], "released")

    def test_tail_once_posts_exactly_what_the_fixture_implies_at_tools(self):
        sent = canvas.tail_once(self.store, SID, self.transcript)
        self.assertGreater(sent, 0)
        posted = self.events()
        kinds = {e["kind"] for e in posted}
        self.assertNotIn("thinking", kinds)
        self.assertIn("agent", kinds)
        for event in posted:
            if event["kind"] == "tool_result":
                self.assertNotIn("text", event["body"])
        expected = set()
        tailer = canvas.ClaudeTailer(self.transcript, session=SID, repo_root=self.repo)
        for event in tailer.poll():
            if canvas.sanitise(event, "tools", self.repo) is not None:
                expected.add(event["id"])
        transcript_ids = {e["id"] for e in posted if e["kind"] in canvas.POSITIONAL}
        self.assertEqual(transcript_ids, expected)
        self.assertNotIn(FAKE_GITHUB_TOKEN, b"".join(FakeRelay.bodies).decode("utf-8"))
        self.assertNotIn(FAKE_BLOB, b"".join(FakeRelay.bodies).decode("utf-8"))
        snap = next(e for e in posted if e["kind"] == "agent")
        body = snap["body"]
        self.assertEqual(body["self_role"], "contributor")
        self.assertEqual(body["alive"], "daemon")
        self.assertEqual(body["stream"], "tools")
        self.assertEqual(body["title"], "Canvas design")
        self.assertIn(body["state"], ("idle", "working", "tool", "waiting"))
        for key in ("host", "machine_id", "fingerprint", "bio", "github", "sig", "sig_by"):
            self.assertNotIn(key, body)
        self.assertEqual(set(body["surface"]), {"base", "files", "count", "truncated"})
        saved = canvas.Offsets(self.store, SID)
        self.assertEqual(saved.state_for(self.transcript)["offset"], self.transcript.stat().st_size)
        again = canvas.tail_once(self.store, SID, self.transcript)
        self.assertEqual([e for b in FakeRelay.batches[1:] for e in b if e["kind"] in canvas.POSITIONAL], [])
        self.assertGreaterEqual(again, 0)

    def test_a_policy_rejection_lowers_the_level(self):
        daemon = canvas._Daemon(self.store, SID, self.transcript)
        tailer = canvas.ClaudeTailer(self.transcript, session=SID, repo_root=self.repo)
        text = next(e for e in tailer.poll() if e["kind"] == "text")
        FakeRelay.reject[text["id"]] = "policy"
        daemon.step()
        self.assertEqual(daemon.level, "summary")
        self.assertTrue(daemon.gaps or any(e["kind"] == "gap" for e in self.events()))


# ---------------------------------------------------------------- daemon


class Daemon(RelayCase):
    def _tail_argv(self):
        probe = subprocess.run([sys.executable, "-m", "agentcolab", "canvas", "--help"], cwd=str(ROOT),
                               capture_output=True, timeout=20)
        if probe.returncode == 0:
            return None
        return [sys.executable, "-m", "agentcolab.canvas"]

    def test_spawn_returns_fast_leaves_a_live_child_and_the_child_obeys_paused(self):
        argv = self._tail_argv()
        patch = f"canvas.TAIL_ARGV = {argv!r} + ['tail']\n" if argv else ""
        code = (
            "import sys, json\n"
            f"sys.path.insert(0, {str(ROOT)!r})\n"
            "from agentcolab import canvas\n"
            "from agentcolab.store import Store\n"
            f"{patch}"
            f"store = Store(root={str(self.repo)!r})\n"
            f"print(canvas.ensure_tailer(store, {SID!r}, {str(self.transcript)!r}))\n"
        )
        env = {**os.environ, "PYTHONPATH": str(ROOT)}
        start = time.time()
        parent = subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=10, env=env, text=True)
        elapsed = time.time() - start
        self.assertEqual(parent.stdout.strip(), "spawned", parent.stderr)
        self.assertLess(elapsed, 2.0, "the parent waited on its child")
        pidfile = canvas._pidfile(self.store, SID)
        pid = int(pidfile.read_text(encoding="utf-8"))
        self.addCleanup(lambda: subprocess.run(["kill", "-9", str(pid)], capture_output=True))
        self.assertTrue(canvas._pid_alive(pid), "the child died at once: " + self._log())
        self.assertEqual(canvas.ensure_tailer(self.store, SID, self.transcript), "running")
        deadline = time.time() + 8
        while time.time() < deadline and not FakeRelay.batches:
            time.sleep(0.1)
        self.assertTrue(FakeRelay.batches, "the child never posted: " + self._log())
        self.assertIn("agent", {e["kind"] for b in FakeRelay.batches for e in b})
        config = self.store.config()
        config["paused"] = True
        self.store.save_config(config)
        deadline = time.time() + 8
        while time.time() < deadline and canvas._pid_alive(pid):
            time.sleep(0.1)
        self.assertFalse(canvas._pid_alive(pid), "the child ignored paused: " + self._log())
        self.assertFalse(pidfile.exists())
        self.assertFalse(canvas._marker(self.store, f"daemon-failed-{SID}").exists())

    def _log(self):
        log = canvas._marker(self.store, "tail.log")
        return log.read_text(encoding="utf-8")[-800:] if log.exists() else "(no log)"

    def test_a_live_pid_is_respected_and_a_dead_one_replaced(self):
        pidfile = canvas._pidfile(self.store, SID)
        pidfile.write_text(str(os.getpid()), encoding="utf-8")
        self.assertEqual(canvas.ensure_tailer(self.store, SID, self.transcript), "running")
        pidfile.write_text("999999", encoding="utf-8")
        self.assertTrue(canvas._claim_pidfile(pidfile))
        self.assertEqual(pidfile.read_text(encoding="utf-8"), "")

    def test_ensure_tailer_is_off_without_a_room(self):
        config = self.store.config()
        del config["canvas"]
        self.store.save_config(config)
        self.assertEqual(canvas.ensure_tailer(self.store, SID, self.transcript), "off")
        self.assertFalse(canvas._pidfile(self.store, SID).exists())

    def test_flush_if_orphaned_needs_the_marker_and_no_live_pid(self):
        self.assertEqual(canvas.flush_if_orphaned(self.store, SID, self.transcript, budget=3), 0)
        canvas._touch(canvas._marker(self.store, f"daemon-failed-{SID}"), "OSError: no such interpreter")
        sent = canvas.flush_if_orphaned(self.store, SID, self.transcript, budget=3)
        self.assertGreater(sent, 0)
        snap = next(e for e in self.events() if e["kind"] == "agent")
        self.assertEqual(snap["body"]["alive"], "hook")
        self.assertEqual(snap["body"]["daemon"], {"state": "failed", "reason": "OSError: no such interpreter"})
        self.assertIn("prompt", {e["kind"] for e in self.events()})
        canvas._pidfile(self.store, SID).write_text(str(os.getpid()), encoding="utf-8")
        before = len(FakeRelay.batches)
        self.assertEqual(canvas.flush_if_orphaned(self.store, SID, self.transcript, budget=3), 0)
        self.assertEqual(len(FakeRelay.batches), before)

    def test_stop_marker_ends_the_session(self):
        canvas.write_stop_marker(self.store, SID)
        daemon = canvas._Daemon(self.store, SID, self.transcript)
        reason, _ = daemon.step()
        self.assertEqual(reason, "stopped")
        ends = [e for e in self.events() if e["kind"] == "session" and e["body"]["state"] == "end"]
        self.assertEqual(len(ends), 1)

    def test_discovery_uses_the_slug_and_the_cwd_field(self):
        projects = self.work / "projects"
        canvas.CLAUDE_PROJECTS, saved = projects, canvas.CLAUDE_PROJECTS
        self.addCleanup(setattr, canvas, "CLAUDE_PROJECTS", saved)
        slug = "".join(c if c.isalnum() else "-" for c in str(self.repo))
        self.assertEqual(canvas.claude_project_dir("/Users/meharkhanna/AgentColab").name, "-Users-meharkhanna-AgentColab")
        here = projects / slug / f"{SID}.jsonl"
        _write_jsonl(here, [_claude_entry("user", "u1", origin={"kind": "human"}, cwd=str(self.repo),
                                          message={"role": "user", "content": "hi"})])
        elsewhere = projects / slug / "other-session.jsonl"
        _write_jsonl(elsewhere, [_claude_entry("user", "u1", origin={"kind": "human"}, cwd="/somewhere/else",
                                               message={"role": "user", "content": "hi"})])
        found = canvas.discover_claude_transcripts(self.repo)
        self.assertEqual([sid for sid, _ in found], [SID])
        sessions = self.work / "codex"
        canvas.CODEX_SESSIONS, saved_codex = sessions, canvas.CODEX_SESSIONS
        self.addCleanup(setattr, canvas, "CODEX_SESSIONS", saved_codex)
        today = time.strftime("%Y/%m/%d", time.localtime())
        rollout = sessions / today / f"rollout-2026-09-04T10-00-00-{THREAD}.jsonl"
        _write_jsonl(rollout, codex_fixture(str(self.repo)))
        other = sessions / today / "rollout-2026-09-04T10-00-01-11111111-2222-3333-4444-555555555555.jsonl"
        entries = codex_fixture("/somewhere/else")
        _write_jsonl(other, entries)
        self.assertEqual([sid for sid, _ in canvas.discover_rollouts(self.repo)], [THREAD])

    def test_markers_and_config_helpers(self):
        canvas.write_idle_marker(self.store, SID)
        self.assertTrue(canvas._marker(self.store, f"idle-{SID}").exists())
        canvas.clear_idle_marker(self.store, SID)
        self.assertFalse(canvas._marker(self.store, f"idle-{SID}").exists())
        self.assertTrue(canvas.is_on(self.store))
        project = self.repo / ".agentcolab"
        project.mkdir()
        (project / "agentcolab.json").write_text(json.dumps({"canvas": {"relay": "http://other", "max_stream": "summary",
                                                                         "token": "must-not-leak"}}), encoding="utf-8")
        cfg = canvas.canvas_config(self.store)
        self.assertEqual(cfg["relay"], self.relay, "the machine's relay lost to the repo's")
        self.assertEqual(cfg["max_stream"], "summary")
        self.assertEqual(cfg["token"], FakeRelay.token)
        daemon = canvas._Daemon(self.store, SID, self.transcript)
        self.assertEqual(daemon.level, "summary")


# ---------------------------------------------------------------- structural


class Structural(unittest.TestCase):
    source = (ROOT / "agentcolab" / "canvas.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    def test_no_server_or_thread_imports(self):
        names = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                names.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and not node.level:
                names.add((node.module or "").split(".")[0])
        for banned in ("http", "socketserver", "threading", "selectors", "asyncio"):
            self.assertNotIn(banned, names)
        for banned in ("hooks", "session", "cli", "mcp", "chat"):
            self.assertNotIn(f"from . import {banned}\n", self.source)
            self.assertNotIn(f"from .{banned} import", self.source)

    def test_time_sleep_only_in_the_loops(self):
        allowed = {"tail_loop", "tail_discover"}
        for node in ast.walk(self.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for call in ast.walk(node):
                    if (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                            and call.func.attr == "sleep"):
                        self.assertIn(node.name, allowed, f"time.sleep in {node.name}")
        self.assertIn("time.sleep(", self.source)

    def test_every_open_names_an_encoding_or_is_binary(self):
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if name not in ("read_text", "write_text", "open"):
                continue
            if any(k.arg == "encoding" for k in node.keywords):
                continue
            mode = ""
            if name == "open":
                if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
                    mode = str(node.args[1].value)
                for k in node.keywords:
                    if k.arg == "mode" and isinstance(k.value, ast.Constant):
                        mode = str(k.value.value)
            self.assertIn("b", mode, f"canvas.py:{node.lineno} {name}() without an encoding")

    def test_the_spawn_is_detached_and_quiet(self):
        body = self.source[self.source.index("def ensure_tailer("):self.source.index("def _lane_of(")]
        for needle in ("stdin=subprocess.DEVNULL", "stdout=subprocess.DEVNULL", "start_new_session",
                       "close_fds=True", "AGENTCOLAB_PROFILE", "creationflags"):
            self.assertIn(needle, body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
