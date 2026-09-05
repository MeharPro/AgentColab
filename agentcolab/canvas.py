"""Canvas: mirror an agent's transcript to a relay so humans can watch it live.

The agent side of docs/canvas-contract.md. Everything that leaves the machine
passes through `sanitise` first, and the relay stores what it is sent without
editing it, so this module is the trust boundary: caps, scrubbing and the
stream level are all decided here.

Three things shape the design and are easy to get wrong:

- The transcript file is the spool. Ids and `seq` derive from the file position
  (`content_id` over the entry, `line_no * 256 + block`), so two producers over
  one file agree without talking, and an offset is committed only when the
  relay has acknowledged the events it covers. A hook killed at its timeout
  loses a flush, never an event.
- Nothing here blocks a hook. Every network call has a short timeout and sits
  inside `contextlib.suppress`; a `429` is recorded as a timestamp and skipped,
  never slept on. `time.sleep` appears only in the two daemon loops.
- The module imports nothing that pulls in `session`, `chat` or `identity` at
  import time. `hooks` imports this module inside function bodies, and the
  pre-edit hook must stay cheap; the one place that needs `session` imports it
  lazily.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from . import board, records
from .records import content_id, parse_iso
from .store import Store, read_json, write_json

USER_AGENT = "AgentColab/1 (+https://github.com/AgentColab/AgentColab)"

KINDS = ("agent", "text", "thinking", "tool_call", "tool_result", "prompt",
         "session", "record", "answer", "gap")
POSITIONAL = ("text", "thinking", "tool_call", "tool_result", "prompt", "session", "gap")
LEVELS = ("off", "summary", "tools", "full")
SESSION_STATES = ("start", "end", "compact", "idle", "error", "abort")
GAP_REASONS = ("backlog", "oversize", "policy", "rewrite", "restart", "spool")
CONTENT_KEYS = ("content", "new_string", "old_string", "edits", "cells", "patch", "new_source")

# Per-kind body caps in UTF-8 bytes (contract §4.2). The relay only enforces the
# 40 KiB event cap; these are ours.
CAPS = {"agent": 32768, "text": 32768, "thinking": 8192, "tool_call": 8192,
        "tool_result": 8192, "prompt": 8192, "session": 1024, "record": 4096,
        "answer": 3072, "gap": 256}
TEXT_HEAD, TEXT_TAIL = 28672, 4096
RESULT_HEAD, RESULT_TAIL = 6144, 2048
CONTENT_KEY_MAX = 2048          # content-bearing arg at `full`
ARG_LEAF_MAX = {"summary": 0, "tools": 1024, "full": 4096}
SUMMARY_PROMPT_CHARS, SUMMARY_PROMPT_BYTES = 200, 800
MAX_FILES = 400

EVENT_BYTES = 40960
BATCH_EVENTS = 200
BATCH_BYTES = 65536
LINE_MAX = 4 * 1024 * 1024
CHUNK = 65536
READ_BUDGET = 192 * 1024        # complete-line bytes per poll: a few batches, not a burst
BACKLOG_MAX = 1024 * 1024
PENDING_MAX = 512 * 1024
RETRY_MAX = 60
POLL_MIN, POLL_MAX = 0.5, 5.0
IDLE_EXIT = 30 * 60
SNAPSHOT_EVERY, SNAPSHOT_FORCE = 60, 300
SURFACE_EVERY = 300
RECORDS_EVERY = 60
LOG_MAX = 256 * 1024
DISCOVER_EVERY = 30

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
CODEX_SESSIONS = Path.home() / ".codex" / "sessions"

# `sys.executable -m agentcolab` rather than `colab`: a shim missing from PATH
# must not be able to break streaming. A module constant so a test can point it
# at `-m agentcolab.canvas` until the CLI verb exists.
TAIL_ARGV = [sys.executable, "-m", "agentcolab", "canvas", "tail"]
# `-m agentcolab` resolves the package from the child's sys.path, and a child
# started in somebody's checkout has that checkout on its path, not this one.
# A git-clone install -- the installer's layout, and every friend's machine --
# is importable only through the launcher's own path fix-up, so the child was
# dying with "No module named agentcolab" in every repository but this one.
PACKAGE_PARENT = str(Path(__file__).resolve().parents[1])

# Where `colab canvas new` and `colab canvas join` go when neither --relay nor
# the project's config names one. A placeholder until the maintainer deploys
# canvas/worker.js and replaces it; the CLI says so when it falls back to it.
DEFAULT_RELAY = "https://agentcolab-canvas.dizon-dzn12.workers.dev"

# Control characters other than newline and tab: `withhold_secrets` uses NUL
# placeholders internally, and binary tool output carries real NULs.
_CONTROL_TABLE = {c: None for c in range(0x20) if c not in (0x09, 0x0A)}
_CONTROL_TABLE[0x7F] = None


# ---------------------------------------------------------------- time


def iso_ms(dt: datetime | None = None) -> str:
    """ISO-8601 UTC with milliseconds: the transcript's precision, not `records.iso`'s."""
    stamp = dt or datetime.now(timezone.utc)
    return stamp.strftime("%Y-%m-%dT%H:%M:%S.") + f"{stamp.microsecond // 1000:03d}Z"


# ---------------------------------------------------------------- events


def build(kind: str, body: dict[str, Any], *, session: str, lane: str = "main",
          seq: int = 0, epoch: int = 0, ts: str | None = None, harness: str = "",
          model: str | None = None, ref: str | None = None, agent: str = "",
          id: str | None = None) -> dict[str, Any]:
    """One envelope. Synthetic kinds get a content id over (session, kind, ts)."""
    stamp = ts or iso_ms()
    return {
        "v": 1,
        "id": id or content_id("ev", session, kind, stamp),
        "agent": agent,
        "session": session,
        "lane": lane or "main",
        "epoch": int(epoch),
        "seq": int(seq),
        "ts": stamp,
        "kind": kind,
        "harness": harness,
        "model": model,
        "ref": ref,
        "body": body,
    }


def gap(reason: str, from_seq: int, to_seq: int, count: int, **envelope: Any) -> dict[str, Any]:
    body = {"from_seq": int(from_seq), "to_seq": int(to_seq), "count": int(count), "reason": reason}
    envelope.setdefault("seq", to_seq)
    return build("gap", body, **envelope)


def size_of(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def validate(event: Any) -> list[str]:
    """Every problem the relay would find, so a bad event never leaves.

    Mirrors contract §4.1's exhaustive list plus the 40 KiB event cap and the
    two enumerations the frontend relies on. An empty list means send it.
    """
    problems: list[str] = []
    if not isinstance(event, dict):
        return ["event: not an object"]

    def text(key: str, limit: int, *, optional: bool = False, nonempty: bool = False) -> None:
        value = event.get(key)
        if value is None and optional:
            return
        if not isinstance(value, str):
            problems.append(f"{key}: missing or not a string")
        elif len(value) > limit:
            problems.append(f"{key}: over {limit} characters")
        elif nonempty and not value:
            problems.append(f"{key}: empty")

    if event.get("v") != 1:
        problems.append("v: must be 1")
    text("id", 64, nonempty=True)
    kind = event.get("kind")
    if not isinstance(kind, str):
        problems.append("kind: missing")
    elif kind not in KINDS:
        problems.append(f"kind: unknown {kind!r}")
    body = event.get("body")
    if not isinstance(body, dict):
        problems.append("body: not an object")
    text("session", 64)
    text("lane", 64, nonempty=True)
    for key in ("epoch", "seq"):
        value = event.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            problems.append(f"{key}: not a non-negative integer")
    text("ts", 40)
    text("harness", 32)
    text("model", 64, optional=True)
    text("ref", 128, optional=True)
    if isinstance(body, dict):
        if kind == "session" and body.get("state") not in SESSION_STATES:
            problems.append("body.state: not a session state")
        if kind == "gap" and body.get("reason") not in GAP_REASONS:
            problems.append("body.reason: not a gap reason")
        if kind == "answer" and not isinstance(body.get("ask"), str):
            problems.append("body.ask: missing")
    if not problems:
        total = size_of(event)
        if total > EVENT_BYTES:
            problems.append(f"oversize: {total} > {EVENT_BYTES} bytes")
    return problems


def batches(events: Iterable[dict[str, Any]]) -> list[list[tuple[dict[str, Any], str]]]:
    """Split into wire batches of ≤ 200 events and ≤ 64 KiB, in order."""
    out: list[list[tuple[dict[str, Any], str]]] = []
    current: list[tuple[dict[str, Any], str]] = []
    used = 0
    for event in events:
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        cost = len(line.encode("utf-8")) + 1
        if current and (len(current) >= BATCH_EVENTS or used + cost > BATCH_BYTES):
            out.append(current)
            current, used = [], 0
        current.append((event, line))
        used += cost
    if current:
        out.append(current)
    return out


# ---------------------------------------------------------------- sanitise


def _cut(text: str, head: int, tail: int) -> tuple[str, bool, int]:
    """Head + tail truncation on code-point boundaries; reports the original size."""
    raw = text.encode("utf-8")
    if len(raw) <= head + tail:
        return text, False, len(raw)
    front = raw[:head].decode("utf-8", "ignore")
    back = raw[-tail:].decode("utf-8", "ignore") if tail else ""
    dropped = len(raw) - len(front.encode("utf-8")) - len(back.encode("utf-8"))
    return f"{front}\n… {dropped:,} bytes cut …\n{back}", True, len(raw)


def _cut_points(text: str, points: int, max_bytes: int) -> tuple[str, bool]:
    if len(text) <= points and len(text.encode("utf-8")) <= max_bytes:
        return text, False
    out = text[:points]
    while len(out.encode("utf-8")) > max_bytes:
        out = out[:-1]
    return out, True


def _drop_images(value: Any) -> Any:
    """Image data never leaves; a marker says one was there."""
    if isinstance(value, dict):
        if value.get("type") == "image" or "image_url" in value:
            source = value.get("source") if isinstance(value.get("source"), dict) else {}
            return {"image": True, "media_type": source.get("media_type") or value.get("media_type") or ""}
        return {k: _drop_images(v) for k, v in value.items()
                if k not in ("signature", "encrypted_content", "usage")}
    if isinstance(value, list):
        return [_drop_images(v) for v in value]
    if isinstance(value, str) and value.startswith("data:image"):
        return "[image]"
    return value


def _map_strings(value: Any, fn: Callable[[str], str]) -> Any:
    if isinstance(value, str):
        return fn(value)
    if isinstance(value, list):
        return [_map_strings(v, fn) for v in value]
    if isinstance(value, dict):
        return {k: _map_strings(v, fn) for k, v in value.items()}
    return value


def _strip_controls(text: str) -> str:
    return text.translate(_CONTROL_TABLE)


def _rewriter(repo_root: Path | None) -> Callable[[str], str]:
    root = str(repo_root) if repo_root else ""
    home = str(Path.home())
    roots = [r for r in (root, home) if r and r != "/"]

    def rewrite(text: str) -> str:
        if root and root in text:
            text = text.replace(root, ".")
        if home and home in text:
            text = text.replace(home, "~")
        return text
    return rewrite if roots else (lambda text: text)


def _describe(value: Any) -> dict[str, int]:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return {"bytes": len(text.encode("utf-8")), "lines": text.count("\n") + 1 if text else 0}


def _leaves(value: Any, prefix: tuple = ()) -> Iterable[tuple[tuple, str]]:
    if isinstance(value, str):
        yield prefix, value
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _leaves(item, prefix + (index,))
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _leaves(item, prefix + (key,))


def _set_leaf(value: Any, path: tuple, new: Any) -> None:
    target = value
    for step in path[:-1]:
        target = target[step]
    target[path[-1]] = new


def _fit(body: dict[str, Any], cap: int) -> dict[str, Any]:
    """Whatever the per-kind rules left, the event still has to fit its cap.

    Cuts the longest string first, then the longest list, until it fits or
    nothing is left to cut. Rare in practice; here so a giant paths list or a
    record with a huge subject never turns into a rejected event.
    """
    size = size_of(body)
    if size <= cap:
        return body
    original = body.get("bytes") if isinstance(body.get("bytes"), int) else size
    for _ in range(40):
        leaves = [(path, text) for path, text in _leaves(body) if path[-1] not in ("reason", "kind")]
        if not leaves:
            break
        path, text = max(leaves, key=lambda item: len(item[1].encode("utf-8")))
        raw = len(text.encode("utf-8"))
        if raw < 48:
            break
        keep = max(24, raw - (size - cap) - 24)
        new, _, _ = _cut(text, keep * 3 // 4, keep // 4)
        _set_leaf(body, path, new)
        size = size_of(body)
        if size <= cap:
            break
    while size > cap:
        lists = [(k, v) for k, v in body.items() if isinstance(v, list) and v]
        if not lists:
            break
        key, items = max(lists, key=lambda item: size_of(item[1]))
        body[key] = items[:max(0, len(items) // 2)]
        size = size_of(body)
    body["truncated"] = True
    body["bytes"] = original
    return body


def _cap_args(args: dict[str, Any], omitted: dict[str, Any], level: str) -> dict[str, Any]:
    """Content-bearing keys become size markers below `full`; every leaf is capped."""
    out: dict[str, Any] = {}
    leaf_max = ARG_LEAF_MAX.get(level, 1024)
    for key, value in args.items():
        if key in CONTENT_KEYS:
            omitted[key] = _describe(value)
            if level != "full":
                continue
            if isinstance(value, str):
                value, _, _ = _cut(value, CONTENT_KEY_MAX * 3 // 4, CONTENT_KEY_MAX // 4)
            else:
                text = json.dumps(value, ensure_ascii=False)
                if len(text.encode("utf-8")) > CONTENT_KEY_MAX:
                    value, _, _ = _cut(text, CONTENT_KEY_MAX * 3 // 4, CONTENT_KEY_MAX // 4)
            out[key] = value
            continue
        if level == "summary":
            omitted[key] = _describe(value)
            continue

        def clip(text: str) -> str:
            cut, _, _ = _cut(text, leaf_max * 3 // 4, leaf_max // 4)
            return cut
        out[key] = _map_strings(value, clip)
    return out


def _apply_caps(kind: str, body: dict[str, Any], level: str) -> dict[str, Any]:
    if kind == "text":
        text, cut, size = _cut(str(body.get("text") or ""), TEXT_HEAD, TEXT_TAIL)
        body["text"] = text
        if cut:
            body["truncated"], body["bytes"] = True, size
    elif kind == "thinking":
        text, cut, size = _cut(str(body.get("text") or ""), CAPS["thinking"] * 3 // 4, CAPS["thinking"] // 8)
        body["text"] = text
        if cut:
            body["truncated"], body["bytes"] = True, size
    elif kind == "tool_call":
        args = body.get("args") if isinstance(body.get("args"), dict) else {}
        omitted = dict(body.get("omitted") or {})
        body["args"] = _cap_args(args, omitted, level)
        body["omitted"] = omitted
        body["paths"] = [str(p) for p in (body.get("paths") or [])][:MAX_FILES]
    elif kind == "tool_result":
        if "text" in body:
            text, cut, size = _cut(str(body.get("text") or ""), RESULT_HEAD, RESULT_TAIL)
            body["text"] = text
            body["truncated"] = cut
            if cut:
                body["bytes"] = size
        body["paths"] = [str(p) for p in (body.get("paths") or [])][:MAX_FILES]
    elif kind == "prompt":
        if level == "summary":
            text, cut = _cut_points(str(body.get("text") or ""), SUMMARY_PROMPT_CHARS, SUMMARY_PROMPT_BYTES)
            if cut:
                body["truncated"], body["bytes"] = True, len(str(body.get("text") or "").encode("utf-8"))
            body["text"] = text
        else:
            text, cut, size = _cut(str(body.get("text") or ""), CAPS["prompt"] * 3 // 4, CAPS["prompt"] // 8)
            body["text"] = text
            if cut:
                body["truncated"], body["bytes"] = True, size
    elif kind == "agent":
        surface = body.get("surface") if isinstance(body.get("surface"), dict) else None
        if surface and isinstance(surface.get("files"), list) and len(surface["files"]) > MAX_FILES:
            surface["files"] = surface["files"][:MAX_FILES]
            surface["truncated"] = True
    elif kind == "answer":
        text, cut, size = _cut(str(body.get("text") or ""), CAPS["answer"] * 3 // 4, CAPS["answer"] // 8)
        body["text"] = text
        if cut:
            body["truncated"], body["bytes"] = True, size
    return _fit(body, CAPS.get(kind, EVENT_BYTES))


def _policy(kind: str, body: dict[str, Any], level: str) -> dict[str, Any] | None:
    """What each level lets through; None means the event stays home."""
    if level == "off":
        return None
    if kind == "thinking":
        return body if level == "full" else None
    if kind == "text":
        return body if level in ("tools", "full") else None
    if kind == "tool_result":
        if level == "summary":
            return None
        if level != "full":
            # `bytes` stays: it is the size of the output, not a cut marker.
            body.pop("text", None)
            body.pop("truncated", None)
        return body
    if kind == "tool_call":
        if level == "summary":
            body["args"] = {}
        return body
    return body


def sanitise(event: dict[str, Any], level: str, repo_root: Path | None) -> dict[str, Any] | None:
    """The one gate between the transcript and the wire (design §3.7, in order).

    Returns a new event or None when the level keeps it home. Cut before scrub
    so a truncation never bisects a secret that then escapes the pattern.
    """
    if level not in LEVELS or level == "off":
        return None
    kind = str(event.get("kind") or "")
    if kind not in KINDS:
        return None
    if kind == "thinking" and level != "full":
        return None
    if kind == "text" and level == "summary":
        return None
    if kind == "tool_result" and level == "summary":
        return None
    body = event.get("body") if isinstance(event.get("body"), dict) else {}
    body = _drop_images(json.loads(json.dumps(body, ensure_ascii=False)))
    body = _map_strings(body, _strip_controls)
    body = _map_strings(body, _rewriter(repo_root))
    if repo_root and isinstance(body.get("paths"), list):
        body["paths"] = [records.normalise_path(str(p), repo_root) for p in body["paths"] if str(p)]
    body = _apply_caps(kind, body, level)
    body = records.scrub_deep(body)

    def withhold(text: str) -> str:
        return records.withhold_secrets(text) if records.looks_like_secret(text) else text
    body = _map_strings(body, withhold)
    # Scrubbing grows strings (`Bearer abcd1234` becomes `Bearer
    # [REDACTED:auth-header]`), so a body cut to exactly its cap can leave here
    # over it and be refused by the relay as `policy` or `oversize`. The gate
    # runs again after the scrub; both cuts are no-ops when nothing grew.
    if kind == "prompt" and level == "summary":
        text, cut = _cut_points(str(body.get("text") or ""), SUMMARY_PROMPT_CHARS, SUMMARY_PROMPT_BYTES)
        if cut:
            body["truncated"] = True
            body.setdefault("bytes", len(str(body.get("text") or "").encode("utf-8")))
        body["text"] = text
    body = _fit(body, CAPS.get(kind, EVENT_BYTES))
    body = _policy(kind, body, level)
    if body is None:
        return None
    clean = dict(event)
    clean["body"] = body
    return clean


def effective_level(config_level: str | None, project_max: str | None = None,
                    room_max: str | None = None) -> str:
    """min of the machine's wish, the committed ceiling and the room's ceiling."""
    picks = [config_level or "tools"]
    for ceiling in (project_max, room_max):
        if ceiling:
            picks.append(ceiling)
    ranks = [LEVELS.index(p) if p in LEVELS else LEVELS.index("tools") for p in picks]
    return LEVELS[min(ranks)]


def lower_level(level: str) -> str:
    """One step down, never below `summary`.

    A policy rejection means the room's ceiling is lower than this machine
    thought, and `summary` is the floor a ceiling can have (§3.1). Dropping to
    `off` here silently ended the mirror for the rest of the daemon's life while
    `colab canvas status` kept saying it was streaming.
    """
    index = LEVELS.index(level) if level in LEVELS else 1
    return LEVELS[max(1, index - 1)]


def clamp_stream(requested: str | None, reply: Any) -> str:
    """The level this machine streams at: min of what it asked for and what the relay answered.

    Computed here, never copied from the relay: `effective_stream` can only be
    the requested level or lower (§3.6), so a reply above it is a relay lying,
    and a lie must not open the widest pipe in the project.
    """
    wanted = requested if requested in LEVELS and requested != "off" else "tools"
    if isinstance(reply, str) and reply in LEVELS and reply != "off":
        return LEVELS[min(LEVELS.index(wanted), LEVELS.index(reply))]
    return wanted


# ---------------------------------------------------------------- tailers


def _tool_paths(args: dict[str, Any], repo_root: Path | None) -> list[str]:
    out = []
    for key in ("file_path", "notebook_path", "path", "target_file"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            out.append(records.normalise_path(value, repo_root) if repo_root else value)
    return out


def _text_of(content: Any) -> tuple[str, bool, str]:
    """(text, has_image, media_type) of a tool result's content in either shape."""
    if isinstance(content, str):
        return content, False, ""
    parts: list[str] = []
    image, media = False, ""
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        if block.get("type") in ("text", "input_text", "output_text") and isinstance(block.get("text"), str):
            parts.append(block["text"])
        elif block.get("type") in ("image", "input_image", "output_image"):
            # Claude carries `source.media_type`; Codex carries a data URI in
            # `image_url`. Neither's bytes are copied -- only that one was there.
            image = True
            source = block.get("source") if isinstance(block.get("source"), dict) else {}
            media = media or str(source.get("media_type") or block.get("media_type") or "")
            url = block.get("image_url")
            if not media and isinstance(url, str) and url.startswith("data:"):
                media = url[len("data:"):].split(";", 1)[0].split(",", 1)[0]
    return "\n".join(parts), image, media


def _count_lines(path: Path, start: int, end: int) -> tuple[int, int]:
    """(newlines in [start, end), offset just after the last one)."""
    count, last = 0, start
    with open(path, "rb") as handle:
        handle.seek(start)
        position = start
        while position < end:
            chunk = handle.read(min(CHUNK, end - position))
            if not chunk:
                break
            count += chunk.count(b"\n")
            index = chunk.rfind(b"\n")
            if index >= 0:
                last = position + index + 1
            position += len(chunk)
    return count, last


class _Tailer:
    """Incremental reader over one JSONL file with file-position ids.

    `poll()` returns the events read since the last acknowledged offset and
    keeps returning the same ones until `ack()` says the relay has them; the
    offset in `state` moves only then. A changed inode or a shorter file is a
    new epoch: the old positions are meaningless, so the reader starts over
    and says so with a `gap{rewrite}`.
    """

    harness = ""

    def __init__(self, path: Path | str, *, session: str, lane: str = "main",
                 state: dict[str, Any] | None = None, model: str | None = None,
                 agent: str = "", repo_root: Path | None = None,
                 backlog_max: int = BACKLOG_MAX) -> None:
        self.path = Path(path)
        self.session = session
        self.lane = lane or "main"
        self.model = model
        self.agent = agent
        self.repo_root = repo_root
        self.backlog_max = backlog_max
        # The caller's dict is kept, not copied: `Offsets` hands one in and
        # expects `ack()` to move it.
        self.state = state if isinstance(state, dict) else {}
        for key in ("offset", "inode", "epoch", "seq", "acked_seq", "line"):
            value = self.state.get(key)
            self.state[key] = int(value) if isinstance(value, int) and not isinstance(value, bool) else 0
        self.read_to = self.state["offset"]
        # The physical line number is persisted on its own. Restoring it from
        # `seq >> 8` looked equivalent and was not: `seq` is the highest seq
        # *emitted*, and trailing lines that emit nothing (Claude `attachment`,
        # `queue-operation`; Codex `token_count`) are invisible to it, so a
        # restored tailer numbered the next line short by that many and its ids
        # and seqs disagreed with the daemon that read the same file live. An
        # offsets file from before this field carries no `line`; `seq >> 8` is
        # then the best available guess rather than a silent zero.
        self.line = self.state["line"] if self.state["line"] else self.state["seq"] >> 8
        self.seq = self.state["seq"]
        self.epoch = self.state["epoch"]
        self.partial = bytearray()
        self.skipping = False           # inside a line already past LINE_MAX
        self.skipped = 0
        self.unacked: list[dict[str, Any]] = []
        self.changed = False
        self.title: str | None = None

    # -- envelope helpers

    def _event(self, kind: str, body: dict[str, Any], line_no: int, block: int, *,
               ts: str | None, ref: str | None = None, key: str | None = None) -> dict[str, Any]:
        seq = line_no * 256 + block
        self.seq = max(self.seq, seq)
        ident = content_id("ev", self.session, key or f"line:{line_no}", str(block))
        return build(kind, body, session=self.session, lane=self.lane, seq=seq,
                     epoch=self.epoch, ts=ts or iso_ms(), harness=self.harness,
                     model=self.model, ref=ref, agent=self.agent, id=ident)

    def _gap(self, reason: str, from_seq: int, to_seq: int, count: int) -> dict[str, Any]:
        """A gap found in the file gets a positional id, so two readers agree on it too."""
        self.seq = max(self.seq, to_seq)
        ident = content_id("ev", self.session, f"gap:{reason}", f"{self.epoch}:{from_seq}:{to_seq}")
        return gap(reason, from_seq, to_seq, count, session=self.session, lane=self.lane,
                   epoch=self.epoch, harness=self.harness, model=self.model, agent=self.agent, id=ident)

    def _blocks(self, line_no: int, items: list[tuple[str, dict[str, Any], str | None, str | None]],
                *, ts: str | None, base: int = 0) -> list[dict[str, Any]]:
        """(kind, body, ref, key) per block → events; block 255 is the last one allowed."""
        out = []
        for index, (kind, body, ref, key) in enumerate(items):
            block = base + index
            if block > 255:
                out.append(self._gap("oversize", line_no * 256 + 255, line_no * 256 + 255,
                                     len(items) - index))
                break
            out.append(self._event(kind, body, line_no, block, ts=ts, ref=ref, key=key))
        return out

    # -- reading

    def _reset(self, inode: int) -> list[dict[str, Any]]:
        previous = self.seq
        self.epoch += 1
        self.state["inode"] = inode
        self.read_to, self.line, self.seq = 0, 0, 0
        self.partial = bytearray()
        self.skipping, self.skipped = False, 0
        self.unacked = []
        return [self._gap("rewrite", previous, 0, 0)]

    def poll(self, budget: int = READ_BUDGET) -> list[dict[str, Any]]:
        self.changed = False
        try:
            stat = os.stat(self.path)
        except OSError:
            return list(self.unacked)
        inode = int(getattr(stat, "st_ino", 0) or 0)
        consumed = self.read_to + len(self.partial) + self.skipped
        fresh: list[dict[str, Any]] = []
        if (self.state.get("inode") and inode and inode != self.state["inode"]) or stat.st_size < consumed:
            fresh.extend(self._reset(inode))
            consumed = 0
            self.changed = True
        elif not self.state.get("inode"):
            self.state["inode"] = inode
        if self.unacked and not fresh:
            return list(self.unacked)
        if stat.st_size - self.read_to > self.backlog_max and not self.partial and not self.skipping:
            count, last = _count_lines(self.path, self.read_to, stat.st_size)
            if count:
                before = self.seq
                self.line += count
                fresh.append(self._gap("backlog", before, self.line * 256, count))
                self.read_to = last
                consumed = last
                self.changed = True
        if stat.st_size > consumed:
            fresh.extend(self._read(consumed, budget))
        self.unacked.extend(fresh)
        return list(self.unacked)

    def _read(self, position: int, budget: int) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        done = 0
        with open(self.path, "rb") as handle:
            handle.seek(position)
            while done < budget:
                chunk = handle.read(CHUNK)
                if not chunk:
                    break
                self.changed = True
                start = 0
                while True:
                    index = chunk.find(b"\n", start)
                    if index < 0:
                        rest = chunk[start:]
                        if self.skipping:
                            self.skipped += len(rest)
                        else:
                            self.partial.extend(rest)
                            if len(self.partial) > LINE_MAX:
                                self.skipping = True
                                self.skipped = len(self.partial)
                                self.partial = bytearray()
                        break
                    piece = chunk[start:index]
                    start = index + 1
                    self.line += 1
                    length = self.skipped + len(self.partial) + len(piece) + 1
                    if self.skipping:
                        events.append(self._gap("oversize", self.seq, self.line * 256, 1))
                    else:
                        line = bytes(self.partial) + piece
                        events.extend(self._line(self.line, line))
                    self.partial = bytearray()
                    self.skipping, self.skipped = False, 0
                    self.read_to += length
                    done += length
                if done >= budget:
                    break
        return events

    def _line(self, line_no: int, raw: bytes) -> list[dict[str, Any]]:
        text = raw.decode("utf-8", "replace").strip()
        if not text:
            return []
        try:
            entry = json.loads(text)
        except ValueError:
            return []
        if not isinstance(entry, dict):
            return []
        try:
            return self.parse(line_no, entry)
        except Exception:
            # One malformed entry must not stall the stream: skip it, keep the
            # position. AGENTCOLAB_DEBUG shows what was skipped.
            if os.environ.get("AGENTCOLAB_DEBUG"):
                records.eprint(f"canvas: skipped line {line_no} of {self.path.name}")
            return []

    def parse(self, line_no: int, entry: dict[str, Any]) -> list[dict[str, Any]]:
        raise NotImplementedError

    # -- acknowledgement

    def ack(self, ids: Iterable[str] | None = None) -> bool:
        """Commit the offset once every unacked event is acknowledged."""
        if ids is not None:
            acked = set(ids)
            self.unacked = [e for e in self.unacked if e["id"] not in acked]
        else:
            self.unacked = []
        if self.unacked:
            return False
        self.state.update({"offset": self.read_to, "epoch": self.epoch,
                           "seq": self.seq, "acked_seq": self.seq, "line": self.line})
        return True


class ClaudeTailer(_Tailer):
    """Claude Code transcript: one JSONL entry per content block (design §3.6)."""

    harness = "claude-code"
    SKIP = ("attachment", "queue-operation", "last-prompt", "bridge-session", "atis-latch",
            "mode", "pr-link", "frame-link", "artifact-comment-monitor",
            "artifact-autoreact-ledger", "summary")

    def parse(self, line_no: int, entry: dict[str, Any]) -> list[dict[str, Any]]:
        kind = entry.get("type")
        if kind in self.SKIP:
            return []
        ts = entry.get("timestamp") if isinstance(entry.get("timestamp"), str) else None
        uuid = entry.get("uuid") if isinstance(entry.get("uuid"), str) else None
        if kind in ("ai-title", "custom-title"):
            title = entry.get("aiTitle") if kind == "ai-title" else entry.get("customTitle")
            if not isinstance(title, str) or not title.strip() or title == self.title:
                return []
            self.title = title.strip()[:200]
            return self._blocks(line_no, [("session", {"state": "start", "source": "title",
                                                       "title": self.title}, None, uuid)], ts=ts)
        if kind == "system":
            if entry.get("subtype") == "compact_boundary":
                meta = entry.get("compactMetadata") if isinstance(entry.get("compactMetadata"), dict) else {}
                body = {"state": "compact", "source": str(meta.get("trigger") or "auto")}
                return self._blocks(line_no, [("session", body, None, uuid)], ts=ts)
            return []
        if kind == "assistant":
            return self._assistant(line_no, entry, ts, uuid)
        if kind == "user":
            return self._user(line_no, entry, ts, uuid)
        return []

    def _assistant(self, line_no: int, entry: dict[str, Any], ts: str | None,
                   uuid: str | None) -> list[dict[str, Any]]:
        message = entry.get("message") if isinstance(entry.get("message"), dict) else {}
        model = message.get("model")
        if isinstance(model, str) and model and not model.startswith("<"):
            self.model = model[:64]
        content = message.get("content")
        blocks = [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []
        base = entry.get("apiBlockIndex")
        base = int(base) if isinstance(base, int) and not isinstance(base, bool) and base >= 0 else 0
        if entry.get("isApiErrorMessage"):
            text, _, _ = _text_of(blocks)
            body = {"state": "error", "reason": (str(entry.get("error") or "") + " " + text).strip()[:300]}
            return self._blocks(line_no, [("session", body, None, uuid)], ts=ts, base=base)
        final = message.get("stop_reason") == "end_turn"
        items: list[tuple[str, dict[str, Any], str | None, str | None]] = []
        for block in blocks:
            kind = block.get("type")
            if kind == "text":
                items.append(("text", {"text": str(block.get("text") or ""), "final": final}, None, uuid))
            elif kind == "thinking":
                text = str(block.get("thinking") or "")
                body = {"text": text} if text else {"text": "", "redacted": True}
                items.append(("thinking", body, None, uuid))
            elif kind == "tool_use":
                args = block.get("input") if isinstance(block.get("input"), dict) else {}
                body = {"name": str(block.get("name") or ""), "args": args,
                        "paths": _tool_paths(args, self.repo_root), "omitted": {}}
                items.append(("tool_call", body, str(block.get("id") or "") or None, uuid))
        if entry.get("isAbortedMidStream"):
            items.append(("session", {"state": "abort", "reason": "interrupted"}, None, uuid))
        return self._blocks(line_no, items, ts=ts, base=base)

    def _user(self, line_no: int, entry: dict[str, Any], ts: str | None,
              uuid: str | None) -> list[dict[str, Any]]:
        if entry.get("isMeta") or entry.get("isCompactSummary"):
            return []
        message = entry.get("message") if isinstance(entry.get("message"), dict) else {}
        content = message.get("content")
        origin = entry.get("origin") if isinstance(entry.get("origin"), dict) else {}
        human = origin.get("kind") == "human"
        # A subagent's first entry is the task its parent gave it: not a human,
        # but it is what the lane is about, so it is shown as a prompt and says so.
        handed = bool(entry.get("isSidechain")) and entry.get("parentUuid") is None
        if isinstance(content, str):
            if not (human or handed) or not content.strip():
                return []
            body = {"text": content} if human else {"text": content, "source": "parent"}
            return self._blocks(line_no, [("prompt", body, None, uuid)], ts=ts)
        blocks = [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []
        if human:
            text = "\n".join(str(b.get("text") or "") for b in blocks if b.get("type") == "text").strip()
            return self._blocks(line_no, [("prompt", {"text": text}, None, uuid)], ts=ts) if text else []
        result = entry.get("toolUseResult")
        items: list[tuple[str, dict[str, Any], str | None, str | None]] = []
        for block in blocks:
            if block.get("type") != "tool_result":
                continue
            text, image, media = _text_of(block.get("content"))
            ok = not bool(block.get("is_error"))
            body: dict[str, Any] = {"ok": ok, "exit": _exit_code(result, ok, text),
                                    "bytes": len(text.encode("utf-8")),
                                    "lines": text.count("\n") + 1 if text else 0,
                                    "paths": [], "image": image}
            if image:
                body["media_type"] = media
            if isinstance(result, dict) and isinstance(result.get("filePath"), str):
                path = result["filePath"]
                body["paths"] = [records.normalise_path(path, self.repo_root) if self.repo_root else path]
            if entry.get("toolDenialKind"):
                body["denied"] = True
            body["text"] = text
            items.append(("tool_result", body, str(block.get("tool_use_id") or "") or None, uuid))
        return self._blocks(line_no, items, ts=ts)


def _exit_code(result: Any, ok: bool, text: str) -> int | None:
    if isinstance(result, dict):
        for key in ("exitCode", "exit_code", "code"):
            if isinstance(result.get(key), int) and not isinstance(result.get(key), bool):
                return int(result[key])
        if result.get("interrupted"):
            return None
    probe = result if isinstance(result, str) else text
    if isinstance(probe, str) and probe.startswith("Error: Exit code "):
        digits = probe[len("Error: Exit code "):].split()[0] if probe[len("Error: Exit code "):].split() else ""
        with contextlib.suppress(ValueError):
            return int(digits)
    return 0 if ok else None


class CodexTailer(_Tailer):
    """Codex rollout: `{timestamp, type, payload}` lines (design §3.6)."""

    harness = "codex"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.cwd: str | None = None
        self.parent: str | None = None
        self.calls: dict[str, dict[str, Any]] = {}     # call_id → exit/paths from *_end events

    def parse(self, line_no: int, entry: dict[str, Any]) -> list[dict[str, Any]]:
        kind = entry.get("type")
        payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
        ts = entry.get("timestamp") if isinstance(entry.get("timestamp"), str) else None
        if kind == "session_meta":
            self.cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else None
            source = payload.get("source")
            if isinstance(source, dict):
                spawn = ((source.get("subagent") or {}).get("thread_spawn") or {})
                parent = spawn.get("parent_thread_id")
                self.parent = str(parent) if parent else None
            body = {"state": "start", "source": str(payload.get("originator") or source or "codex")[:60]}
            return self._blocks(line_no, [("session", body, None, None)], ts=ts)
        if kind == "turn_context":
            model = payload.get("model")
            if isinstance(model, str) and model:
                self.model = model[:64]
            return []
        if kind == "compacted":
            return self._blocks(line_no, [("session", {"state": "compact", "source": "auto"}, None, None)], ts=ts)
        if kind == "event_msg":
            return self._event_msg(line_no, payload, ts)
        if kind == "response_item":
            return self._response_item(line_no, payload, ts)
        return []

    def _event_msg(self, line_no: int, payload: dict[str, Any], ts: str | None) -> list[dict[str, Any]]:
        kind = payload.get("type")
        if kind == "user_message":
            text = payload.get("message")
            if not isinstance(text, str) or not text.strip():
                return []
            return self._blocks(line_no, [("prompt", {"text": text}, None, None)], ts=ts)
        if kind == "task_started":
            return self._blocks(line_no, [("session", {"state": "start", "source": "turn"}, None, None)], ts=ts)
        if kind == "task_complete":
            return self._blocks(line_no, [("session", {"state": "idle", "source": "turn"}, None, None)], ts=ts)
        if kind == "turn_aborted":
            body = {"state": "abort", "reason": str(payload.get("reason") or "interrupted")[:100]}
            return self._blocks(line_no, [("session", body, None, None)], ts=ts)
        if kind == "error":
            body = {"state": "error", "reason": str(payload.get("message") or payload.get("codex_error_info") or "")[:300]}
            return self._blocks(line_no, [("session", body, None, None)], ts=ts)
        if kind == "exec_command_end":
            call = str(payload.get("call_id") or "")
            if call:
                exit_code = payload.get("exit_code")
                self.calls[call] = {"exit": exit_code if isinstance(exit_code, int) else None}
            return []
        if kind == "patch_apply_end":
            call = str(payload.get("call_id") or "")
            changes = payload.get("changes") if isinstance(payload.get("changes"), dict) else {}
            paths = [records.normalise_path(p, self.repo_root) if self.repo_root else p for p in changes]
            if call:
                self.calls[call] = {"exit": 0 if payload.get("success", True) else 1, "paths": paths[:MAX_FILES]}
            return []
        return []

    def _response_item(self, line_no: int, payload: dict[str, Any], ts: str | None) -> list[dict[str, Any]]:
        kind = payload.get("type")
        if kind == "message":
            if payload.get("role") != "assistant":
                return []
            content = payload.get("content") if isinstance(payload.get("content"), list) else []
            text = "\n".join(str(b.get("text") or "") for b in content
                             if isinstance(b, dict) and b.get("type") == "output_text")
            if not text:
                return []
            body = {"text": text, "final": payload.get("phase") == "final_answer"}
            return self._blocks(line_no, [("text", body, None, None)], ts=ts)
        if kind == "reasoning":
            summary = payload.get("summary") if isinstance(payload.get("summary"), list) else []
            text = "\n\n".join(str(s.get("text") or "") for s in summary if isinstance(s, dict)).strip()
            body = {"text": text} if text else {"text": "", "redacted": True}
            return self._blocks(line_no, [("thinking", body, None, None)], ts=ts)
        if kind in ("function_call", "custom_tool_call"):
            name = str(payload.get("name") or kind)
            call = str(payload.get("call_id") or "") or None
            if kind == "function_call":
                raw = payload.get("arguments")
                args: Any = None
                if isinstance(raw, str):
                    with contextlib.suppress(ValueError):
                        args = json.loads(raw)
                if not isinstance(args, dict):
                    args = {"arguments": raw if isinstance(raw, str) else json.dumps(raw)}
            else:
                text = payload.get("input") if isinstance(payload.get("input"), str) else ""
                args = {"patch": text} if name == "apply_patch" else {"input": text}
            body = {"name": name, "args": args, "paths": _tool_paths(args, self.repo_root), "omitted": {}}
            return self._blocks(line_no, [("tool_call", body, call, None)], ts=ts)
        if kind in ("function_call_output", "custom_tool_call_output"):
            call = str(payload.get("call_id") or "")
            text, image, media = _text_of(payload.get("output"))
            known = self.calls.pop(call, {})
            exit_code = known.get("exit")
            ok = exit_code in (0, None) and not text.startswith("Error")
            if exit_code is None and ok:
                exit_code = 0
            body: dict[str, Any] = {"ok": ok, "exit": exit_code, "bytes": len(text.encode("utf-8")),
                                    "lines": text.count("\n") + 1 if text else 0,
                                    "paths": list(known.get("paths") or []), "image": image, "text": text}
            if image:
                body["media_type"] = media
            return self._blocks(line_no, [("tool_result", body, call or None, None)], ts=ts)
        return []


def make_tailer(path: Path | str, *, session: str, lane: str = "main", harness: str | None = None,
                **kwargs: Any) -> _Tailer:
    name = Path(path).name
    codex = harness == "codex" or (harness is None and name.startswith("rollout-"))
    cls = CodexTailer if codex else ClaudeTailer
    return cls(path, session=session, lane=lane, **kwargs)


# ---------------------------------------------------------------- state


def derive_state(lane_events: list[dict[str, Any]], *, idle_marker: bool = False
                 ) -> tuple[str, dict[str, Any] | None]:
    """Contract §4.2 (D14), first match wins, over the lane's newest events."""
    positional = [e for e in lane_events if e.get("kind") in POSITIONAL]
    if not positional:
        return "idle", None
    newest = positional[-1]
    body = newest.get("body") if isinstance(newest.get("body"), dict) else {}
    for event in reversed(positional):
        b = event.get("body") if isinstance(event.get("body"), dict) else {}
        if event.get("kind") == "session" and b.get("state") == "end":
            return "gone", None
        if event.get("kind") in ("text", "prompt", "tool_call", "tool_result", "thinking"):
            break
    if newest.get("kind") == "tool_call" and body.get("name") == "AskUserQuestion":
        return "waiting", None
    if newest.get("kind") == "tool_result" and body.get("denied"):
        return "waiting", None
    answered = {e.get("ref") for e in positional if e.get("kind") == "tool_result"}
    for event in reversed(positional):
        if event.get("kind") == "tool_call":
            if event.get("ref") and event["ref"] not in answered:
                b = event.get("body") if isinstance(event.get("body"), dict) else {}
                return "tool", {"name": str(b.get("name") or ""), "ref": event["ref"],
                                "since": event.get("ts")}
            break
    if idle_marker:
        # The Stop hook has said the turn is over. The transcript alone cannot:
        # a turn that ended on `stop_sequence` leaves a non-final `text` as its
        # newest block, which reads `working` for the whole idle timeout.
        return "idle", None
    kind = newest.get("kind")
    if kind == "text" and not body.get("final"):
        return "working", None
    if kind in ("thinking", "prompt"):
        return "working", None
    return "idle", None


_SNAPSHOT_DROP = ("host", "machine_id", "bio", "github")


def snapshot(store: Store, *, state: str, tool: dict[str, Any] | None = None,
             level: str = "tools", alive: str = "daemon", daemon: dict[str, Any] | None = None,
             role: dict[str, Any] | None = None, role_seen_seq: int = 0, title: str | None = None,
             heartbeat: dict[str, Any] | None = None, session: str = "", lane: str = "main",
             seq: int = 0, epoch: int = 0, harness: str = "", model: str | None = None,
             agent: str = "") -> dict[str, Any]:
    """The `agent` event: the heartbeat minus what a canvas does not need.

    `heartbeat` lets the daemon reuse a payload it computed minutes ago —
    `heartbeat_payload` runs three git commands for the surface. `session` is
    imported here and not at module level so importing this module from a hook
    stays cheap.
    """
    if heartbeat is None:
        from . import session as _session
        heartbeat = _session.heartbeat_payload(store)
    body: dict[str, Any] = {}
    for key, value in heartbeat.items():
        if key in _SNAPSHOT_DROP or key.startswith("sig") or key.startswith("fingerprint"):
            continue
        if key == "role":
            body["self_role"] = value
            continue
        body[key] = value
    surface = body.get("surface") if isinstance(body.get("surface"), dict) else {}
    files = [str(f) for f in (surface.get("files") or [])]
    body["surface"] = {"base": str(surface.get("base") or ""), "files": files[:MAX_FILES],
                       "count": int(surface.get("count") or len(files)),
                       "truncated": bool(surface.get("truncated")) or len(files) > MAX_FILES}
    body["title"] = title
    body["state"] = state
    body["tool"] = tool
    body["role"] = role
    body["role_seen_seq"] = int(role_seen_seq or 0)
    body["stream"] = level
    body["alive"] = alive
    body["daemon"] = daemon or {"state": "running", "reason": None}
    # The machine's own wake settings (§10.7): the relay's copy is what the
    # page shows, this is what the listener will actually do.
    body["wake"] = wake_settings(store)
    return build("agent", body, session=session, lane=lane, seq=seq, epoch=epoch,
                 harness=harness or str(heartbeat.get("harness") or ""),
                 model=model or (str(heartbeat.get("model") or "") or None), agent=agent)


# ---------------------------------------------------------------- records


_FAMILIES = (("msgs", "msg"), ("claims", "claim"), ("reviews", "review"),
             ("findings", "finding"), ("bugs", "bug"))


def _record_body(family: str, record: dict[str, Any], trust: str) -> dict[str, Any]:
    rid = str(record.get("id") or "")
    subject = record.get("subject") or record.get("title") or record.get("reason") or record.get("note") or ""
    to = record.get("to") or "*"
    state = None
    if family in ("task", "bug", "review"):
        state = record.get("state")
    elif family == "claim":
        state = "released" if record.get("released_at") else "active"
    return {
        "family": family,
        "kind": str(record.get("kind") or family),
        "rid": rid,
        "from": str(record.get("agent") or ""),
        "to": str(to) if to else "*",
        "subject": str(subject)[:200],
        "paths": [str(p) for p in (record.get("paths") or [])][:40],
        "reply_to": record.get("reply_to"),
        "task": record.get("task") or (rid if family == "task" else None),
        "state": state,
        "owner": record.get("owner"),
        "blocked_by": [str(d) for d in (record.get("_blocked_by") or [])],
        "trust": trust,
        "ts": str(record.get("ts") or record.get("created_at") or record.get("updated_at") or ""),
    }


def _record_stamp(family: str, record: dict[str, Any]) -> str:
    if family == "task":
        return "|".join([str(record.get("state") or ""), str(record.get("owner") or ""),
                         ",".join(str(d) for d in (record.get("_blocked_by") or [])),
                         str(record.get("updated_at") or "")])
    if family == "claim":
        return str(record.get("released_at") or record.get("renewed_at") or record.get("expires_at") or record.get("ts") or "")
    return str(record.get("updated_at") or record.get("state") or record.get("ts") or record.get("created_at") or "")


def mirror_records(store: Store, seen: dict[str, str], **envelope: Any
                   ) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Record events for everything in the merged view whose content id moved.

    Tasks come from `board.tasks` so state and owner are the computed ones, with
    `_blocked_by` overlaid from `board.open_tasks`. Returns (events, new seen).
    """
    candidates: list[tuple[str, dict[str, Any]]] = []
    for kind, family in _FAMILIES:
        with contextlib.suppress(Exception):
            for record in store.read_all(kind):
                if record.get("id"):
                    candidates.append((family, record))
    with contextlib.suppress(Exception):
        table = board.tasks(store)
        # `open_tasks` marks `_blocked_by` on the tasks it withholds and returns
        # only the ready ones, so the marks never leave it; the same rule is
        # applied here to the same table.
        ready = {str(t.get("id")) for t in board.open_tasks(store)}
        for task in table.values():
            task = dict(task)
            blocked = [dep for dep in task.get("deps") or []
                       if table.get(dep, {}).get("state") != "done"]
            task["_blocked_by"] = blocked if task.get("state") == "open" and str(task.get("id")) not in ready else []
            candidates.append(("task", task))
    changed: list[tuple[str, dict[str, Any], str]] = []
    for family, record in candidates:
        rid = str(record.get("id"))
        ident = content_id("rec", rid, _record_stamp(family, record))
        if seen.get(rid) == ident:
            continue
        changed.append((family, record, ident))
    trust: dict[str, str] = {}
    if changed:
        with contextlib.suppress(Exception):
            from . import session as _session
            for item in _session.classify_all(store, [r for _, r, _ in changed]):
                trust[str(item.get("id"))] = str(item.get("_trust") or "unverified")
    events = []
    updated = dict(seen)
    for family, record, ident in changed:
        rid = str(record.get("id"))
        body = _record_body(family, record, trust.get(rid, "unverified"))
        envelope = dict(envelope)
        envelope["ts"] = envelope.get("ts") or iso_ms()
        events.append(build("record", body, ref=rid, id=ident, **envelope))
        updated[rid] = ident
    if len(updated) > 2000:
        updated = dict(list(updated.items())[-2000:])
    return events, updated


# ---------------------------------------------------------------- http


def _request(method: str, url: str, *, data: bytes | None = None, token: str | None = None,
             timeout: float = 3, content_type: str = "application/json"
             ) -> tuple[int, dict[str, str], bytes]:
    """One request. HTTP errors come back as a status; network errors raise."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if data is not None:
        headers["Content-Type"] = content_type
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, {k.lower(): v for k, v in response.headers.items()}, response.read()
    except urllib.error.HTTPError as exc:
        body = b""
        try:
            with contextlib.suppress(Exception):
                body = exc.read()
        finally:
            with contextlib.suppress(Exception):
                exc.close()
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}, body


def _json_body(raw: bytes) -> dict[str, Any]:
    with contextlib.suppress(ValueError):
        data = json.loads(raw.decode("utf-8", "replace") or "{}")
        if isinstance(data, dict):
            return data
    return {}


def _url(relay: str, path: str) -> str:
    return relay.rstrip("/") + path


def _room_of(join_code: str) -> str:
    return join_code.split(".", 1)[0]


def healthz(relay: str, *, timeout: float = 3) -> dict[str, Any] | None:
    """GET /healthz → the relay's self-description, or None when unreachable."""
    with contextlib.suppress(Exception):
        status, _, raw = _request("GET", _url(relay, "/healthz"), timeout=timeout)
        if status == 200:
            return _json_body(raw)
    return None


def viewer_url(relay: str, room: str) -> str:
    """The page a person opens; the code rides in the fragment, never a path."""
    return f"{str(relay or '').rstrip('/')}/#{room}"


def new_room(relay: str, name: str = "room", policy: dict[str, Any] | None = None,
             *, timeout: float = 5) -> dict[str, Any]:
    """POST /rooms → {room, join_code, policy, relay, url}. Raises on failure."""
    body: dict[str, Any] = {"name": name}
    if policy:
        body["policy"] = policy
    status, _, raw = _request("POST", _url(relay, "/rooms"), data=json.dumps(body).encode("utf-8"), timeout=timeout)
    data = _json_body(raw)
    if status != 201:
        raise RuntimeError(f"relay refused to create a room ({status}): {data.get('hint') or raw[:200]!r}")
    return data


def register(relay: str, join_code: str, name: str, *, harness: str | None = None,
             human: str | None = None, model: str | None = None, stream: str = "tools",
             timeout: float = 5) -> dict[str, Any]:
    """POST /agents/{name} with the join code → {token, owner_token, policy, effective_stream, rseq}.

    `owner_token` (§10.2) is present on a v1.3 relay and `None` on an older one.
    """
    room = _room_of(join_code)
    body = {"harness": harness, "human": human, "model": model, "stream": stream}
    status, _, raw = _request("POST", _url(relay, f"/r/{room}/agents/{urllib.parse.quote(name)}"),
                              data=json.dumps(body).encode("utf-8"), token=join_code, timeout=timeout)
    data = _json_body(raw)
    if status != 200:
        raise RuntimeError(f"relay refused registration ({status}): {data.get('hint') or raw[:200]!r}")
    data["room"] = room
    owner = data.get("owner_token")
    data["owner_token"] = owner if isinstance(owner, str) and owner.startswith("ot-") else None
    return data


def owner_link(relay: str, room: str, owner_token: str) -> str:
    """`<relay>/#<room>/o=<owner token>` (§10.2): the page reads `o=` and rewrites the URL at once."""
    return f"{str(relay or '').rstrip('/')}/#{room}/o={owner_token}"


def post_message(relay: str, room: str, token: str, *, to: str, text: str, kind: str | None = None,
                 wake: bool = False, viewer: str | None = None, timeout: float = 3) -> dict[str, Any]:
    """POST /messages (§10.1) → {id, seq}. Raises on anything but 201.

    With an agent token `from` is the token's name; `viewer` is only for the
    room-code form, which the CLI never uses.
    """
    body: dict[str, Any] = {"to": to, "text": text}
    if kind:
        body["kind"] = kind
    if wake:
        body["wake"] = True
    if viewer:
        body["viewer"] = viewer
    status, _, raw = _request("POST", _url(relay, f"/r/{room}/messages"),
                              data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                              token=token, timeout=timeout)
    data = _json_body(raw)
    if status != 201:
        raise RuntimeError(f"relay refused the message ({status}): {data.get('hint') or raw[:200]!r}")
    return data


def wake_ack(relay: str, room: str, token: str, message_id: str, result: str,
             reason: str | None = None, *, timeout: float = 3) -> bool:
    """POST /wake-ack (§10.4). True on 200; never raises."""
    body = {"message": message_id, "result": result,
            "reason": records.one_line(reason, 200)[1:-1] if reason else None}
    with contextlib.suppress(Exception):
        status, _, _ = _request("POST", _url(relay, f"/r/{room}/wake-ack"),
                                data=json.dumps(body).encode("utf-8"), token=token, timeout=timeout)
        return status == 200
    return False


def put_wake(relay: str, room: str, token: str, name: str, settings: dict[str, Any], *,
             timeout: float = 3) -> dict[str, Any] | None:
    """PUT /agents/{name}/wake (§10.3) with the agent or owner token → the relay's wake object, or None."""
    body = {k: settings[k] for k in ("enabled", "from", "max_per_hour") if k in settings}
    with contextlib.suppress(Exception):
        status, _, raw = _request("PUT", _url(relay, f"/r/{room}/agents/{urllib.parse.quote(name)}/wake"),
                                  data=json.dumps(body).encode("utf-8"), token=token, timeout=timeout)
        if status == 200:
            wake = _json_body(raw).get("wake")
            return wake if isinstance(wake, dict) else {}
    return None


def agent_stream_url(relay: str, room: str, *, websocket: bool = False) -> str:
    """GET /agent-stream (§10.5); `ws(s)://` for the WebSocket form."""
    url = _url(relay, f"/r/{room}/agent-stream")
    if websocket:
        url = "ws" + url[len("http"):] if url.startswith("http") else url
    return url


def pull_inbox_raw(relay: str, room: str, token: str, after: int = 0, *,
                   timeout: float = 3) -> dict[str, Any]:
    """GET /inbox?after=N → {rseq, role, messages} (v1.3) or {rseq, role, asks} (v1.2).

    Raises on anything but 200.
    """
    status, _, raw = _request("GET", _url(relay, f"/r/{room}/inbox?after={int(after)}"),
                              token=token, timeout=timeout)
    data = _json_body(raw)
    if status != 200:
        raise RuntimeError(f"inbox pull failed ({status}): {data.get('hint') or raw[:200]!r}")
    return data


def put_role(relay: str, room: str, token: str, name: str, role: str | None, *,
             timeout: float = 3) -> dict[str, Any]:
    """PUT /roles/{own name} with the agent token; the relay forces viewer "owner"."""
    status, _, raw = _request("PUT", _url(relay, f"/r/{room}/roles/{urllib.parse.quote(name)}"),
                              data=json.dumps({"role": role}).encode("utf-8"), token=token, timeout=timeout)
    data = _json_body(raw)
    if status != 200:
        raise RuntimeError(f"role change failed ({status}): {data.get('hint') or raw[:200]!r}")
    return data


def leave(relay: str, room: str, token: str, name: str, *, timeout: float = 3) -> bool:
    """DELETE /agents/{own name}: best effort, True on 204."""
    with contextlib.suppress(Exception):
        status, _, _ = _request("DELETE", _url(relay, f"/r/{room}/agents/{urllib.parse.quote(name)}"),
                                token=token, timeout=timeout)
        return status == 204
    return False


def post_answer(relay: str, room: str, token: str, event: dict[str, Any], *,
                timeout: float = 3) -> bool:
    """One `answer` event, one POST. True when the relay accepted it."""
    line = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
    status, _, raw = _request("POST", _url(relay, f"/r/{room}/events"), data=line.encode("utf-8"),
                              token=token, timeout=timeout, content_type="application/x-ndjson")
    if status != 202:
        return False
    data = _json_body(raw)
    return not any(r.get("id") == event["id"] for r in (data.get("rejected") or []) if isinstance(r, dict))


class Poster:
    """Posts batches for one agent and remembers what the relay said.

    `send` returns the ids the relay now holds or has refused for good; a
    rejected event is as sent as an accepted one, and a `gap` saying so is
    appended to `gaps` for the next flush. Everything else — `429`, `503`, a
    network error — leaves the ids unacked and sets `retry_after`, which the
    caller persists; nobody sleeps on it.
    """

    def __init__(self, relay: str, room: str, token: str, *, timeout: float = 3,
                 clock: Callable[[], float] = time.time) -> None:
        self.relay, self.room, self.token, self.timeout = relay, room, token, timeout
        self.clock = clock
        self.retry_after: float = 0.0
        self.backoff = 2.0
        self.auth_failures = 0
        self.gone = False
        self.policy_hits = 0
        self.last_status: int | None = None
        self.last_rseq: int | None = None

    def ready(self) -> bool:
        return not self.gone and self.clock() >= self.retry_after

    def _defer(self, seconds: float) -> None:
        self.retry_after = self.clock() + max(0.0, min(float(seconds), RETRY_MAX))

    def send(self, events: list[dict[str, Any]], gaps: list[dict[str, Any]] | None = None, *,
             deadline: float | None = None) -> set[str]:
        """Post `events` in wire batches; `deadline` (a `time.monotonic()` instant) stops
        the loop between batches and caps each request's timeout, for the hook path."""
        acked: set[str] = set()
        if not self.ready():
            return acked
        base_timeout = self.timeout
        try:
            for batch in batches(events):
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining < 0.3:
                        break
                    self.timeout = min(base_timeout, remaining)
                if not self._send_batch(batch, acked, gaps if gaps is not None else []):
                    break
        finally:
            self.timeout = base_timeout
        return acked

    def _send_batch(self, batch: list[tuple[dict[str, Any], str]], acked: set[str],
                    gaps: list[dict[str, Any]]) -> bool:
        body = ("\n".join(line for _, line in batch) + "\n").encode("utf-8")
        try:
            status, headers, raw = _request("POST", _url(self.relay, f"/r/{self.room}/events"), data=body,
                                            token=self.token, timeout=self.timeout,
                                            content_type="application/x-ndjson")
        except Exception:
            self._defer(self.backoff)
            self.backoff = min(RETRY_MAX, self.backoff * 2)
            return False
        self.last_status = status
        data = _json_body(raw)
        if status == 202:
            self.backoff = 2.0
            self.auth_failures = 0
            if isinstance(data.get("rseq"), int):
                self.last_rseq = data["rseq"]
            rejected = {r.get("id"): str(r.get("why") or "schema") for r in (data.get("rejected") or [])
                        if isinstance(r, dict)}
            for event, _ in batch:
                acked.add(event["id"])
                why = rejected.get(event["id"])
                if why is None:
                    continue
                if why == "policy":
                    self.policy_hits += 1
                reason = why if why in GAP_REASONS else "policy"
                gaps.append(gap(reason, event["seq"], event["seq"], 1, session=event["session"],
                                lane=event["lane"], epoch=event["epoch"], harness=event["harness"],
                                model=event.get("model"), agent=event.get("agent") or ""))
            return True
        if status == 413:
            if len(batch) == 1:
                event = batch[0][0]
                replacement = gap("oversize", event["seq"], event["seq"], 1, session=event["session"],
                                  lane=event["lane"], epoch=event["epoch"], harness=event["harness"],
                                  model=event.get("model"), agent=event.get("agent") or "")
                line = json.dumps(replacement, ensure_ascii=False, separators=(",", ":"))
                if self._send_batch([(replacement, line)], set(), gaps):
                    acked.add(event["id"])
                    return True
                return False
            half = len(batch) // 2
            return self._send_batch(batch[:half], acked, gaps) and self._send_batch(batch[half:], acked, gaps)
        if status in (429, 503):
            wait = headers.get("retry-after")
            seconds = float(wait) if wait and wait.replace(".", "", 1).isdigit() else self.backoff
            self._defer(seconds)
            return False
        if status in (401, 404):
            self.auth_failures += 1
            if self.auth_failures >= 3:
                self.gone = True
            self._defer(self.backoff)
            return False
        # 400/403 and anything unexpected: the batch will never be accepted as
        # is, so a retry loop buys nothing. Count it sent and move on.
        for event, _ in batch:
            acked.add(event["id"])
        return True


# ---------------------------------------------------------------- config, markers


def project_canvas(repo_root: Path) -> dict[str, Any]:
    """The committed half: relay, room, join_code (rarely), max_stream. Never a token."""
    for name in (".agentcolab/agentcolab.json", ".agentcolab/colab.json",
                 ".agentcolab/config.json", "agentcolab.json"):
        path = Path(repo_root) / name
        if path.is_file():
            data = read_json(path)
            if isinstance(data, dict):
                block = data.get("canvas")
                return {k: v for k, v in block.items()
                        if k in ("relay", "room", "join_code", "max_stream")} if isinstance(block, dict) else {}
    return {}


def canvas_config(store: Store) -> dict[str, Any]:
    """The machine's block wins; the repo can supply relay/room and lower the level."""
    mine = store.config().get("canvas")
    merged = dict(mine) if isinstance(mine, dict) else {}
    with contextlib.suppress(Exception):
        for key, value in project_canvas(store.repo_root).items():
            if key == "max_stream":
                merged["max_stream"] = value
            else:
                merged.setdefault(key, value)
    return merged


def is_on(store: Store) -> bool:
    try:
        config = store.config()
    except Exception:
        return False
    if config.get("paused"):
        return False
    canvas = canvas_config(store)
    return bool(canvas.get("relay") and canvas.get("room") and canvas.get("token"))


# The machine's half of §10.3: what its listener will do about a wake, whatever
# the relay displays. `from` has no `anyone`; the room code is the outer wall.
WAKE_DEFAULTS: dict[str, Any] = {"enabled": False, "from": "agents", "max_per_hour": 4}
WAKE_FROM = ("agents", "room")


def wake_settings(store: Store) -> dict[str, Any]:
    """`config["canvas"]["wake"]` with defaults filled and every value validated."""
    raw: Any = {}
    with contextlib.suppress(Exception):
        block = store.config().get("canvas")
        raw = block.get("wake") if isinstance(block, dict) else {}
    raw = raw if isinstance(raw, dict) else {}
    per_hour = raw.get("max_per_hour")
    if not isinstance(per_hour, int) or isinstance(per_hour, bool) or not 1 <= per_hour <= 60:
        per_hour = WAKE_DEFAULTS["max_per_hour"]
    return {"enabled": bool(raw.get("enabled", False)),
            "from": raw["from"] if raw.get("from") in WAKE_FROM else WAKE_DEFAULTS["from"],
            "max_per_hour": per_hour}


def save_wake_settings(store: Store, **changes: Any) -> dict[str, Any]:
    """Write `enabled` / `from` / `max_per_hour` into config; returns the settings in force."""
    config = store.config()
    block = dict(config.get("canvas") or {}) if isinstance(config.get("canvas"), dict) else {}
    wake = dict(block.get("wake") or {}) if isinstance(block.get("wake"), dict) else {}
    for key, value in changes.items():
        if key in WAKE_DEFAULTS and value is not None:
            wake[key] = value
    block["wake"] = wake
    config["canvas"] = block
    store.save_config(config)
    return wake_settings(store)


def child_env(store: Store, **extra: str) -> dict[str, str]:
    """The environment for a detached child of ours: the profile, and a sys.path
    that can find this package from any working directory."""
    env = {**os.environ, "AGENTCOLAB_PROFILE": store.profile, **extra}
    prior = env.get("PYTHONPATH")
    env["PYTHONPATH"] = PACKAGE_PARENT + (os.pathsep + prior if prior else "")
    return env


def markers_dir(store: Store) -> Path:
    path = store.home / "canvas"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _marker(store: Store, name: str) -> Path:
    return markers_dir(store) / name


def _touch(path: Path, text: str = "") -> None:
    with contextlib.suppress(OSError):
        path.write_text(text, encoding="utf-8")


def write_stop_marker(store: Store, sid: str) -> None:
    _touch(_marker(store, f"stop-{sid}"), iso_ms())


def write_idle_marker(store: Store, sid: str) -> None:
    _touch(_marker(store, f"idle-{sid}"), iso_ms())


def clear_idle_marker(store: Store, sid: str) -> None:
    with contextlib.suppress(OSError):
        _marker(store, f"idle-{sid}").unlink()


def clear_room_gone(store: Store) -> None:
    with contextlib.suppress(OSError):
        _marker(store, "room-gone").unlink()


# ---------------------------------------------------------------- offsets, pending


class Offsets:
    """`offsets-<sid>.json`: where each transcript is acked to, plus the records cursor."""

    def __init__(self, store: Store, sid: str) -> None:
        self.path = _marker(store, f"offsets-{sid}.json")
        data = read_json(self.path)
        data = data if isinstance(data, dict) else {}
        self.transcripts: dict[str, dict[str, Any]] = dict(data.get("transcripts") or {})
        self.seen: dict[str, str] = dict((data.get("records") or {}).get("seen") or {})
        self.retry_after: str | None = data.get("retry_after") or None
        # The hook path builds a fresh Poster per invocation, so the "three
        # 401/404s and the room is gone" rule (§7) has to count across
        # invocations or it never fires there.
        failures = data.get("auth_failures")
        self.auth_failures: int = int(failures) if isinstance(failures, int) and not isinstance(failures, bool) else 0

    def state_for(self, path: Path | str) -> dict[str, Any]:
        return self.transcripts.setdefault(str(path), {"offset": 0, "inode": 0, "epoch": 0, "seq": 0,
                                                       "acked_seq": 0, "line": 0})

    def save(self) -> None:
        self.transcripts = {p: s for p, s in self.transcripts.items() if Path(p).exists()}
        if len(self.seen) > 2000:
            self.seen = dict(list(self.seen.items())[-2000:])
        write_json(self.path, {"transcripts": self.transcripts, "records": {"seen": self.seen},
                               "retry_after": self.retry_after, "auth_failures": self.auth_failures})


def _pending_path(store: Store, sid: str) -> Path:
    return _marker(store, f"pending-{sid}.ndjson")


def spool(store: Store, sid: str, event: dict[str, Any]) -> None:
    """Append one non-derivable event; over 512 KiB the head goes, with a gap saying so."""
    path = _pending_path(store, sid)
    line = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
    with contextlib.suppress(OSError):
        if path.exists() and path.stat().st_size + len(line.encode("utf-8")) > PENDING_MAX:
            data = path.read_bytes()
            keep = data[len(data) - PENDING_MAX // 2:]
            cut = keep.find(b"\n")
            keep = keep[cut + 1:] if cut >= 0 else b""
            dropped = data[:len(data) - len(keep)].count(b"\n")
            marker = gap("spool", int(event.get("seq") or 0), int(event.get("seq") or 0), dropped,
                         session=str(event.get("session") or ""), lane=str(event.get("lane") or "main"),
                         epoch=int(event.get("epoch") or 0), harness=str(event.get("harness") or ""),
                         model=event.get("model"), agent=str(event.get("agent") or ""))
            head = json.dumps(marker, ensure_ascii=False, separators=(",", ":")) + "\n"
            path.write_bytes(head.encode("utf-8") + keep)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(line)


def read_pending(store: Store, sid: str) -> tuple[bytes, list[dict[str, Any]]]:
    path = _pending_path(store, sid)
    try:
        data = path.read_bytes()
    except OSError:
        return b"", []
    events = []
    # Split on the byte the writer used. `str.splitlines()` also breaks on
    # U+2028/U+2029/U+0085, which `sanitise` lets through and `ensure_ascii=False`
    # writes raw, so an answer containing one was cut into two unparsable halves
    # and silently consumed with the rest of the file.
    for raw in data.split(b"\n"):
        line = raw.decode("utf-8", "replace")
        if not line.strip():
            continue
        with contextlib.suppress(ValueError):
            event = json.loads(line)
            if isinstance(event, dict) and isinstance(event.get("id"), str):
                events.append(event)
    return data, events


def consume_pending(store: Store, sid: str, consumed: bytes) -> None:
    """Remove exactly the bytes that were sent; a concurrent append survives."""
    path = _pending_path(store, sid)
    with contextlib.suppress(OSError):
        current = path.read_bytes()
        if current.startswith(consumed):
            rest = current[len(consumed):]
            if rest.strip():
                path.write_bytes(rest)
            else:
                path.unlink()


# ---------------------------------------------------------------- discovery


def claude_project_dir(cwd: Path | str) -> Path:
    """`~/.claude/projects/<slug>`, slug = the checkout path with non-alphanumerics as `-`."""
    slug = "".join(c if c.isalnum() else "-" for c in str(cwd))
    return CLAUDE_PROJECTS / slug


def _first_cwd(path: Path) -> str | None:
    """The `cwd` in the first entries of a transcript, for the lossy slug's sake."""
    with contextlib.suppress(OSError):
        with open(path, "rb") as handle:
            # A transcript can open with a run of `queue-operation` lines that
            # carry no `cwd`; the first `user` entry does. Forty lines is far
            # past that in every transcript on this machine.
            for _ in range(40):
                line = handle.readline(CHUNK * 4)
                if not line:
                    break
                if b'"cwd"' not in line or not line.endswith(b"\n"):
                    continue
                try:
                    entry = json.loads(line.decode("utf-8", "replace"))
                except ValueError:
                    continue
                if not isinstance(entry, dict):
                    continue
                cwd = entry.get("cwd")
                if not cwd and isinstance(entry.get("payload"), dict):
                    cwd = entry["payload"].get("cwd")
                if isinstance(cwd, str) and cwd:
                    return cwd
    return None


def _beneath(inside: str | None, repo_root: Path | str) -> bool:
    """Is `inside` the checkout, or a directory under it? (Contract §10.7, scope.)

    Resolved on both sides so a symlinked checkout and its real path agree;
    compared by path parts, not string prefix, so `/repo-2` is not under `/repo`.
    """
    if not inside:
        return False
    try:
        candidate = Path(inside).resolve()
        root = Path(repo_root).resolve()
    except OSError:
        return False
    return candidate == root or root in candidate.parents


def _project_dirs_for(repo_root: Path | str) -> list[Path]:
    """Every `~/.claude/projects/<slug>` that could hold a session of this checkout.

    Claude Code slugs the directory a session was *started in*, so a session
    begun in `repo/sub` lives under `<root slug>-sub`, beside the root's own.
    The slug is lossy (`-` stands for `/`, `.`, `_` and every other symbol), so
    a prefix match is only a candidate list: each transcript's `cwd` decides.
    """
    root_dir = claude_project_dir(repo_root)
    prefix = root_dir.name + "-"
    out = []
    with contextlib.suppress(OSError):
        for path in sorted(CLAUDE_PROJECTS.iterdir()):
            if path.is_dir() and (path.name == root_dir.name or path.name.startswith(prefix)):
                out.append(path)
    return out


def discover_claude_transcripts(repo_root: Path | str, hours: float = 6) -> list[tuple[str, Path]]:
    """(session id, path) for every recent Claude Code transcript started inside this checkout.

    Newest first. A transcript is kept when its `cwd` (the first entries that
    carry one) is the repository root or a directory beneath it; a transcript
    with no `cwd` at all is kept only when it sits in the root's own project
    directory, because the slug cannot tell a subdirectory from a sibling. A
    session from another checkout is never tailed, however new it is, and there
    is no fallback to the newest file.
    """
    cutoff = time.time() - hours * 3600
    exact = claude_project_dir(repo_root)
    found = []
    for directory in _project_dirs_for(repo_root):
        for path in directory.glob("*.jsonl"):
            with contextlib.suppress(OSError):
                mtime = path.stat().st_mtime
                if mtime < cutoff:
                    continue
                inside = _first_cwd(path)
                if inside:
                    if not _beneath(inside, repo_root):
                        continue
                elif directory != exact:
                    continue
                found.append((mtime, path.stem, path))
    found.sort(reverse=True)
    return [(sid, path) for _, sid, path in found]


def discover_rollouts(repo_root: Path | str, hours: float = 6) -> list[tuple[str, Path]]:
    """(thread id, path) for recent Codex rollouts whose `session_meta.payload.cwd` is inside this checkout."""
    cutoff = time.time() - hours * 3600
    days = []
    for back in (0, 1):
        stamp = datetime.fromtimestamp(time.time() - back * 86400)
        days.append(CODEX_SESSIONS / f"{stamp.year:04d}" / f"{stamp.month:02d}" / f"{stamp.day:02d}")
    found = []
    for day in days:
        if not day.is_dir():
            continue
        for path in day.glob("rollout-*.jsonl"):
            with contextlib.suppress(OSError):
                mtime = path.stat().st_mtime
                if mtime < cutoff:
                    continue
                if not _beneath(_first_cwd(path), repo_root):
                    continue
                found.append((mtime, _rollout_thread(path), path))
    found.sort(reverse=True)
    return [(sid, path) for _, sid, path in found]


def _rollout_thread(path: Path) -> str:
    stem = path.stem
    return stem[-36:] if len(stem) > 36 else stem


# ---------------------------------------------------------------- daemon


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        # os.kill(pid, 0) terminates on Windows; ask the OS the slow way.
        with contextlib.suppress(Exception):
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                                 capture_output=True, text=True, timeout=5,
                                 **records.quiet_child()).stdout
            # CSV so the pid is a whole field, not a substring of another number
            # or of the "INFO: No tasks" line some locales print.
            for line in out.splitlines():
                cells = [c.strip().strip('"') for c in line.split('","')]
                if len(cells) >= 2 and cells[1] == str(pid):
                    return True
            return False
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _pidfile(store: Store, sid: str) -> Path:
    return _marker(store, f"tail-{sid}.pid")


def _read_pid(path: Path) -> int:
    with contextlib.suppress(OSError, ValueError):
        return int(path.read_text(encoding="utf-8").strip() or 0)
    return 0


def _claim_pidfile(path: Path) -> bool:
    """True when this process now owns the file; a live holder keeps it."""
    for _ in range(2):
        try:
            # Mode "x" is O_CREAT|O_EXCL: two hooks racing here get one winner.
            with open(path, "x", encoding="utf-8"):
                pass
        except FileExistsError:
            pid = _read_pid(path)
            if pid == 0 or _pid_alive(pid):
                if pid == 0:
                    # Claimed but not yet written: a sibling is mid-spawn, or
                    # the file is stale. Old and empty means stale.
                    with contextlib.suppress(OSError):
                        if time.time() - path.stat().st_mtime < 30:
                            return False
                else:
                    return False
            with contextlib.suppress(OSError):
                path.unlink()
            continue
        except OSError:
            return False
        return True
    return False


def tailer_alive(store: Store, sid: str) -> bool:
    return _pid_alive(_read_pid(_pidfile(store, sid)))


def daemon_states(store: Store) -> list[dict[str, Any]]:
    """One row per session the canvas has touched: for `status`, `off`, `doctor`.

    `state` is `running` (a live pid holds the pidfile), `hooks` (the daemon
    could not start and the hooks are flushing instead), or `stopped`.
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    with contextlib.suppress(OSError):
        for path in sorted(markers_dir(store).iterdir()):
            name = path.name
            if name.startswith("tail-") and name.endswith(".pid") and name != "tail-discover.pid":
                sid = name[len("tail-"):-len(".pid")]
            elif name.startswith("daemon-failed-"):
                sid = name[len("daemon-failed-"):]
            elif name.startswith("offsets-") and name.endswith(".json"):
                sid = name[len("offsets-"):-len(".json")]
            else:
                continue
            if sid in seen:
                continue
            seen.add(sid)
            pid = _read_pid(_pidfile(store, sid))
            failed = _marker(store, f"daemon-failed-{sid}")
            reason = ""
            if failed.exists():
                with contextlib.suppress(OSError):
                    reason = failed.read_text(encoding="utf-8").strip()[:120]
            if pid and _pid_alive(pid):
                state = "running"
            elif failed.exists():
                state = "hooks"
            else:
                state = "stopped"
            out.append({"sid": sid, "pid": pid, "state": state, "reason": reason})
    return out


def stop_daemons(store: Store) -> int:
    """Write a stop marker for every live tailer; they exit on their next tick.

    The wake listener (`colab wake serve`) counts as one of them: `colab off`
    and `colab canvas off` mean nothing of ours keeps a socket to the relay.
    """
    count = 0
    for row in daemon_states(store):
        if row["state"] == "running":
            write_stop_marker(store, row["sid"])
            count += 1
    if stop_listener(store):
        count += 1
    return count


WAKE_PIDFILE = "wake.pid"
WAKE_STOP = "wake-stop"


def listener_pid(store: Store) -> int:
    """The live listener's pid, or 0."""
    pid = _read_pid(_marker(store, WAKE_PIDFILE))
    return pid if pid and _pid_alive(pid) else 0


def stop_listener(store: Store) -> bool:
    """Ask the wake listener to exit: a marker it polls, plus SIGTERM where there is one.

    The marker alone would do, but the listener sits in a socket read for up
    to its keepalive interval, and `colab off` should mean off now.
    """
    pid = listener_pid(store)
    if not pid:
        return False
    _touch(_marker(store, WAKE_STOP), iso_ms())
    if os.name != "nt":
        with contextlib.suppress(OSError):
            os.kill(pid, 15)
    return True


CRASH_WINDOW = 60       # a daemon gone within this many seconds of its spawn crashed
CRASH_BACKOFF = 600     # and is not respawned for this long


def _recent_crash(pidfile: Path, marker: Path) -> bool:
    """Did the last daemon die young, and is it too soon to try again?

    A pidfile whose pid is dead but whose mtime is under a minute old means the
    child started and exited straight away (the log has the reason). Record
    that in the failed-marker and hold off; a marker older than the back-off
    clears itself so a fixed install recovers without anyone deleting files.
    """
    now = time.time()
    with contextlib.suppress(OSError):
        if marker.exists():
            text = marker.read_text(encoding="utf-8")
            if text.startswith("crashed") and now - marker.stat().st_mtime < CRASH_BACKOFF:
                return True
    with contextlib.suppress(OSError):
        if pidfile.exists():
            pid = _read_pid(pidfile)
            age = now - pidfile.stat().st_mtime
            if pid and not _pid_alive(pid) and age < CRASH_WINDOW:
                marker.write_text("crashed: the daemon exited within a minute of starting -- "
                                  "see tail.log beside this file; retried in 10 minutes",
                                  encoding="utf-8")
                pidfile.unlink()
                return True
    return False


def ensure_tailer(store: Store, sid: str, transcript_path: Path | str | None = None,
                  harness: str | None = None) -> str:
    """`running` | `spawned` | `failed` | `off` — never raises, never waits.

    Spawned detached with every standard stream pointed away from the hook's:
    stdout is the harness protocol and a child holding it makes the harness
    wait for EOF. `sys.executable -m agentcolab` rather than `colab` so a shim
    missing from PATH cannot break streaming.
    """
    try:
        if not is_on(store):
            return "off"
        if _marker(store, "room-gone").exists():
            return "failed"
        pidfile = _pidfile(store, sid)
        crashed = _marker(store, f"daemon-failed-{sid}")
        if _recent_crash(pidfile, crashed):
            # The last daemon exited within a minute of starting. Respawning it
            # every tick -- every prompt, every `colab` command, every 30 s of
            # discovery -- is a storm of processes that all die the same way;
            # the hooks' bounded flush carries the stream until the marker ages.
            return "failed"
        if not _claim_pidfile(pidfile):
            return "running"
    except Exception:
        return "failed"
    reason = ""
    try:
        log = _marker(store, "tail.log")
        with contextlib.suppress(OSError):
            if log.exists() and log.stat().st_size > LOG_MAX:
                log.write_text("", encoding="utf-8")
        cmd = list(TAIL_ARGV) + ["--session", sid]
        if transcript_path:
            cmd += ["--transcript", str(transcript_path)]
        if harness:
            cmd += ["--harness", harness]
        env = child_env(store)
        extra: dict[str, Any] = {}
        if os.name == "nt":
            extra["creationflags"] = (getattr(subprocess, "DETACHED_PROCESS", 0)
                                      | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        else:
            extra["start_new_session"] = True
        with open(log, "ab") as log_fd:
            child = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=log_fd, close_fds=True, cwd=str(store.repo_root),
                                     env=env, **extra)
        pidfile.write_text(str(child.pid), encoding="utf-8")
        with contextlib.suppress(OSError):
            _marker(store, f"daemon-failed-{sid}").unlink()
        return "spawned"
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"[:300]
    with contextlib.suppress(OSError):
        pidfile.unlink()
    _touch(_marker(store, f"daemon-failed-{sid}"), reason or "spawn failed")
    return "failed"


def _lane_of(path: Path) -> str:
    name = path.name
    if name.startswith("agent-") and name.endswith(".jsonl"):
        return name[len("agent-"):-len(".jsonl")] or "main"
    return "main"


class _Daemon:
    """One session's mirror: tailers, presence, records, one poster."""

    def __init__(self, store: Store, sid: str, transcript_path: Path | str | None, *,
                 harness: str | None = None, clock: Callable[[], float] = time.time,
                 timeout: float = 3) -> None:
        self.store, self.sid, self.clock = store, sid, clock
        self.cfg = canvas_config(store)
        config = store.config()
        self.agent = str(config.get("agent") or "")
        self.transcript = Path(transcript_path) if transcript_path else None
        self.harness = harness or (
            "codex" if self.transcript and self.transcript.name.startswith("rollout-")
            else str(config.get("harness") or "claude-code"))
        self.model = str(config.get("model") or "") or None
        self.level = effective_level(self.cfg.get("stream"), self.cfg.get("max_stream"),
                                     (self.cfg.get("policy") or {}).get("max_stream"))
        self.offsets = Offsets(store, sid)
        self.poster = Poster(str(self.cfg.get("relay") or ""), str(self.cfg.get("room") or ""),
                             str(self.cfg.get("token") or ""), timeout=timeout, clock=clock)
        stamp = parse_iso(self.offsets.retry_after)
        if stamp:
            self.poster.retry_after = stamp.timestamp()
        self.poster.auth_failures = self.offsets.auth_failures
        self.tailers: dict[str, _Tailer] = {}
        self.clean: dict[str, dict[str, Any]] = {}
        self.lanes: dict[str, list[dict[str, Any]]] = {}
        self.gaps: list[dict[str, Any]] = []
        self.session_events: list[dict[str, Any]] = []
        self.last_activity = clock()
        self.last_snapshot = 0.0
        self.last_snapshot_body: dict[str, Any] | None = None
        self.snapshot_due = True
        self.local_changed = False
        self.heartbeat: dict[str, Any] | None = None
        self.heartbeat_at = 0.0
        self.last_records = 0.0
        self.view_mtime = 0.0
        self.local_mtime = self._mtime(store.local_path)
        self.title: str | None = None
        self.interval = POLL_MIN
        self.acked_total = 0

    # -- helpers

    @staticmethod
    def _mtime(path: Path) -> float:
        with contextlib.suppress(OSError):
            return path.stat().st_mtime
        return 0.0

    def _envelope(self) -> dict[str, Any]:
        main = self.tailers.get(str(self.transcript)) if self.transcript else None
        return {"session": self.sid, "lane": "main", "seq": main.seq if main else 0,
                "epoch": main.epoch if main else 0, "harness": self.harness,
                "model": (main.model if main and main.model else self.model), "agent": self.agent}

    def _tailer_for(self, path: Path, lane: str) -> _Tailer:
        key = str(path)
        tailer = self.tailers.get(key)
        if tailer is None:
            tailer = make_tailer(path, session=self.sid, lane=lane, harness=self.harness,
                                 state=self.offsets.state_for(path), model=self.model,
                                 agent=self.agent, repo_root=self.store.repo_root)
            self.tailers[key] = tailer
        return tailer

    def _discover_lanes(self) -> None:
        if self.transcript is None or self.harness != "claude-code":
            return
        sidecar = self.transcript.parent / self.transcript.stem / "subagents"
        if sidecar.is_dir():
            for path in sidecar.glob("**/agent-*.jsonl"):
                self._tailer_for(path, _lane_of(path))

    def _remember(self, event: dict[str, Any]) -> None:
        lane = self.lanes.setdefault(str(event.get("lane") or "main"), [])
        body = event.get("body") if isinstance(event.get("body"), dict) else {}
        slim = {"kind": event.get("kind"), "ref": event.get("ref"), "ts": event.get("ts"),
                "body": {k: body.get(k) for k in ("name", "final", "denied", "state") if k in body}}
        lane.append(slim)
        del lane[:-200]

    # -- one tick

    def exit_reason(self) -> str | None:
        config = self.store.config()
        if config.get("paused"):
            return "paused"
        if not isinstance(config.get("canvas"), dict) or not config["canvas"].get("token"):
            return "off"
        if _marker(self.store, "room-gone").exists() or self.poster.gone:
            return "room-gone"
        if _marker(self.store, f"stop-{self.sid}").exists():
            return "stopped"
        if self.clock() - self.last_activity > IDLE_EXIT:
            return "idle"
        return None

    def collect(self) -> list[dict[str, Any]]:
        """Read every lane, sanitise what is new, note activity and state."""
        if self.transcript is not None:
            self._tailer_for(self.transcript, "main")
        self._discover_lanes()
        out: list[dict[str, Any]] = []
        for tailer in list(self.tailers.values()):
            raw = tailer.poll()
            if tailer.changed:
                self.last_activity = self.clock()
            if tailer.title and tailer.title != self.title:
                self.title = tailer.title
                self.snapshot_due = True
            for event in raw:
                if event["id"] not in self.clean:
                    self._remember(event)
                    if event.get("kind") == "prompt":
                        clear_idle_marker(self.store, self.sid)
                    clean = sanitise(event, self.level, self.store.repo_root)
                    self.clean[event["id"]] = clean if clean is not None else {"id": event["id"], "skip": True}
                    self.snapshot_due = True
                clean = self.clean[event["id"]]
                if not clean.get("skip"):
                    out.append(clean)
        return out

    def records_due(self) -> bool:
        mtime = self._mtime(self.store.cache / "view.json")
        if mtime != self.view_mtime:
            self.view_mtime = mtime
            return True
        return self.clock() - self.last_records >= RECORDS_EVERY

    def make_snapshot(self, *, alive: str = "daemon", daemon: dict[str, Any] | None = None,
                      cheap: bool = False) -> dict[str, Any] | None:
        """`cheap` skips `heartbeat_payload` -- five git subprocesses whose
        timeouts alone sum to over a minute -- and reuses the heartbeat this
        machine last published. The hook path has a budget of seconds."""
        now = self.clock()
        if not cheap and (self.heartbeat is None or now - self.heartbeat_at >= SURFACE_EVERY):
            with contextlib.suppress(Exception):
                records._existing_refs.cache_clear()
                from . import session as _session
                self.heartbeat = _session.heartbeat_payload(self.store)
                self.heartbeat_at = now
        if self.heartbeat is None:
            with contextlib.suppress(Exception):
                self.heartbeat = self.store.view().get(f"agents/{self.store.agent}.json") or {}
        if self.heartbeat is None:
            return None
        local = self.store.local().get("canvas") if isinstance(self.store.local(), dict) else {}
        local = local if isinstance(local, dict) else {}
        role = local.get("role") if isinstance(local.get("role"), dict) else None
        state, tool = derive_state(self.lanes.get("main", []),
                                   idle_marker=_marker(self.store, f"idle-{self.sid}").exists())
        envelope = self._envelope()
        return snapshot(self.store, state=state, tool=tool, level=self.level, alive=alive, daemon=daemon,
                        role=role, role_seen_seq=int((role or {}).get("set_seq") or 0),
                        title=self.title, heartbeat=self.heartbeat, **envelope)

    def snapshot_if_due(self) -> dict[str, Any] | None:
        """Every 60 s while something changed, every 5 min regardless, and
        within the second when local.json moved (that is a role arriving)."""
        now = self.clock()
        local_mtime = self._mtime(self.store.local_path)
        if local_mtime != self.local_mtime:
            self.local_mtime = local_mtime
            self.local_changed = True
        elapsed = now - self.last_snapshot
        if self.last_snapshot and not self.local_changed:
            if elapsed < SNAPSHOT_EVERY:
                return None
            if not self.snapshot_due and elapsed < SNAPSHOT_FORCE:
                return None
        event = self.make_snapshot()
        if event is None:
            return None
        if (self.last_snapshot and elapsed < SNAPSHOT_FORCE and not self.local_changed
                and event["body"] == self.last_snapshot_body):
            self.snapshot_due = False
            return None
        return sanitise(event, self.level, self.store.repo_root)

    def flush(self, transcript_events: list[dict[str, Any]], *, force_snapshot: bool = False) -> int:
        """Post everything ready; commit offsets for what the relay took."""
        if not self.poster.ready():
            return 0
        outgoing: list[dict[str, Any]] = []
        pending_bytes, pending_events = read_pending(self.store, self.sid)
        outgoing.extend(pending_events)
        gaps, self.gaps = self.gaps, []
        outgoing.extend(gaps)
        outgoing.extend(transcript_events)
        record_events: list[dict[str, Any]] = []
        seen = self.offsets.seen
        if self.records_due():
            self.last_records = self.clock()
            with contextlib.suppress(Exception):
                record_events, seen = mirror_records(self.store, self.offsets.seen, **self._envelope())
            for event in record_events:
                clean = sanitise(event, self.level, self.store.repo_root)
                if clean is not None:
                    outgoing.append(clean)
        snap = None
        if force_snapshot:
            snap = self.make_snapshot()
            snap = sanitise(snap, self.level, self.store.repo_root) if snap else None
        else:
            snap = self.snapshot_if_due()
        if snap is not None:
            outgoing.append(snap)
        # An event that would never pass the relay is dropped here and counted as
        # done, or a bad line in the pending file would block every line after it.
        invalid = {e["id"] for e in outgoing if validate(e)}
        outgoing = [e for e in outgoing if e["id"] not in invalid]
        if not outgoing and not invalid:
            return 0
        new_gaps: list[dict[str, Any]] = []
        acked = self.poster.send(outgoing, new_gaps) if outgoing else set()
        if outgoing and not acked:
            invalid = set()             # nothing left the machine: keep the file as is
        acked = acked | invalid
        self.gaps.extend(unsent for unsent in gaps if unsent["id"] not in acked)
        self.gaps.extend(new_gaps)
        if self.poster.policy_hits:
            self.poster.policy_hits = 0
            self.level = lower_level(self.level)
            records.eprint(f"canvas: relay rejected an event by policy; streaming at {self.level} now")
            # Re-sanitise what is still unacked at the new level, but keep what
            # the old level already kept home: a lower level keeps it home too,
            # and forgetting that left those events unacked, so the offset did
            # not commit and the next poll re-served them instead of reading on.
            self.clean = {ident: clean for ident, clean in self.clean.items() if clean.get("skip")}
        # An event the level kept home is as done as an acknowledged one: the
        # offset must move past it or the tailer waits forever.
        kept_home = {ident for ident, clean in self.clean.items() if clean.get("skip")}
        for tailer in self.tailers.values():
            tailer.ack(acked | kept_home)
        for ident in acked:
            self.clean.pop(ident, None)
        for ident in kept_home:
            self.clean.pop(ident, None)
        if pending_events and all(e["id"] in acked for e in pending_events):
            consume_pending(self.store, self.sid, pending_bytes)
        if record_events and all(e["id"] in acked for e in record_events):
            self.offsets.seen = seen
        if snap is not None and snap["id"] in acked:
            self.last_snapshot = self.clock()
            self.last_snapshot_body = snap["body"]
            self.snapshot_due = False
            self.local_changed = False
        if self.poster.gone:
            _touch(_marker(self.store, "room-gone"), iso_ms())
        self.offsets.retry_after = (iso_ms(datetime.fromtimestamp(self.poster.retry_after, timezone.utc))
                                    if self.poster.retry_after > self.clock() else None)
        self.offsets.auth_failures = self.poster.auth_failures
        self.offsets.save()
        self.acked_total += len(acked)
        return len(acked)

    def step(self, *, force_snapshot: bool = False) -> tuple[str | None, int]:
        reason = self.exit_reason()
        if reason == "stopped":
            envelope = self._envelope()
            end = build("session", {"state": "end", "source": "hook"}, **envelope)
            with contextlib.suppress(Exception):
                self.flush(self.collect() + [end], force_snapshot=True)
            return reason, 0
        if reason:
            return reason, 0
        events = self.collect()
        active = any(t.changed for t in self.tailers.values())
        sent = 0
        with contextlib.suppress(Exception):
            sent = self.flush(events, force_snapshot=force_snapshot)
        self.interval = POLL_MIN if active else min(POLL_MAX, self.interval * 2)
        return None, sent


def _finish(store: Store, sid: str, pidfile: Path, *, failed: str | None) -> None:
    with contextlib.suppress(OSError):
        if _read_pid(pidfile) in (os.getpid(), 0):
            pidfile.unlink()
    with contextlib.suppress(OSError):
        _marker(store, f"stop-{sid}").unlink()
    if failed:
        _touch(_marker(store, f"daemon-failed-{sid}"), failed)
    else:
        with contextlib.suppress(OSError):
            _marker(store, f"daemon-failed-{sid}").unlink()


def tail_once(store: Store, sid: str, transcript_path: Path | str | None = None, *,
              harness: str | None = None, timeout: float = 3) -> int:
    """One tick with a snapshot: tests, hooks, and `colab canvas tail --once`."""
    daemon = _Daemon(store, sid, transcript_path, harness=harness, timeout=timeout)
    _, sent = daemon.step(force_snapshot=True)
    return sent


def tail_loop(store: Store, sid: str, transcript_path: Path | str | None = None, *,
              once: bool = False, harness: str | None = None) -> int:
    """`colab canvas tail`: poll, sanitise, post, until told to stop (design §3.2)."""
    pidfile = _pidfile(store, sid)
    holder = _read_pid(pidfile)
    if holder and holder not in (os.getpid(), os.getppid()) and _pid_alive(holder):
        records.eprint(f"canvas: another tailer already holds {pidfile.name}; exiting")
        return 0
    with contextlib.suppress(OSError):
        pidfile.write_text(str(os.getpid()), encoding="utf-8")
    with contextlib.suppress(OSError):
        _marker(store, f"daemon-failed-{sid}").unlink()
    failed: str | None = None
    try:
        if transcript_path is None:
            found = discover_claude_transcripts(store.repo_root, hours=24) + discover_rollouts(store.repo_root, hours=24)
            for found_sid, path in found:
                if found_sid == sid:
                    transcript_path = path
                    break
        daemon = _Daemon(store, sid, transcript_path, harness=harness)
        while True:
            reason, _ = daemon.step(force_snapshot=daemon.last_snapshot == 0)
            if reason or once:
                if reason:
                    records.eprint(f"canvas: tailer for {sid[:8]} exiting: {reason}")
                break
            time.sleep(daemon.interval)
    except Exception as exc:
        failed = f"{type(exc).__name__}: {exc}"[:300]
        records.eprint(f"canvas: tailer for {sid[:8]} failed: {failed}")
    finally:
        _finish(store, sid, pidfile, failed=failed)
    return 1 if failed else 0


# How much transcript one hook flush reads. Small on purpose: everything read
# has to be acked in the same hook for the offset to move, and a hook has a
# second or two. A slice this size is one or two wire batches, so each prompt
# makes progress; the 1 MiB backlog rule bounds how many slices there can be.
HOOK_READ_BUDGET = 48 * 1024


def flush_if_orphaned(store: Store, sid: str, transcript_path: Path | str | None = None, *,
                      budget: float = 1.5, harness: str | None = None) -> int:
    """The bounded fallback when the daemon could not start (design §3.3).

    Does nothing unless `daemon-failed-<sid>` exists, no live pid holds the
    pidfile and the room is not marked gone. Then, inside `budget` -- a deadline
    across everything here, not a per-request timeout -- one bounded read, the
    POSTs that fit, and a snapshot that says `via hooks` built without running
    git. Offsets are persisted whatever happens, so a `429` or a third `401` is
    remembered by the next hook. Never sleeps; never raises.
    """
    deadline = time.monotonic() + max(0.3, float(budget))
    daemon: _Daemon | None = None
    acked: set[str] = set()
    try:
        marker = _marker(store, f"daemon-failed-{sid}")
        if (not marker.exists() or tailer_alive(store, sid) or not is_on(store)
                or _marker(store, "room-gone").exists()):
            return 0
        reason = marker.read_text(encoding="utf-8").strip()[:200] or "spawn failed"
        daemon = _Daemon(store, sid, transcript_path, harness=harness, timeout=max(0.3, budget - 0.2))
        if not daemon.poster.ready():
            return 0
        events: list[dict[str, Any]] = []
        kept_home: set[str] = set()
        if daemon.transcript is not None:
            tailer = daemon._tailer_for(daemon.transcript, "main")
            for event in tailer.poll(budget=HOOK_READ_BUDGET):
                daemon._remember(event)
                clean = sanitise(event, daemon.level, store.repo_root)
                if clean is None:
                    kept_home.add(event["id"])
                else:
                    events.append(clean)
        snap = daemon.make_snapshot(alive="hook", daemon={"state": "failed", "reason": reason}, cheap=True)
        if snap is not None:
            clean = sanitise(snap, daemon.level, store.repo_root)
            if clean is not None:
                events.append(clean)
        pending_bytes, pending_events = read_pending(store, sid)
        candidates = pending_events + events
        # Same rule as the daemon's `flush`: an event the relay would never take
        # is counted done, or one bad spool line would block every line after it.
        invalid = {e["id"] for e in candidates if validate(e)}
        events = [e for e in candidates if e["id"] not in invalid]
        if events:
            acked = daemon.poster.send(events, daemon.gaps, deadline=deadline)
            if not acked:
                invalid = set()             # nothing left the machine: keep the spool as is
        done = acked | invalid
        for tailer in daemon.tailers.values():
            tailer.ack(done | kept_home)
        if pending_events and all(e["id"] in done for e in pending_events):
            consume_pending(store, sid, pending_bytes)
        for extra in daemon.gaps:
            spool(store, sid, extra)
        return len(acked)
    except Exception:
        return len(acked)
    finally:
        if daemon is not None:
            with contextlib.suppress(Exception):
                if daemon.poster.gone:
                    _touch(_marker(store, "room-gone"), iso_ms())
                daemon.offsets.retry_after = (
                    iso_ms(datetime.fromtimestamp(daemon.poster.retry_after, timezone.utc))
                    if daemon.poster.retry_after > time.time() else None)
                daemon.offsets.auth_failures = daemon.poster.auth_failures
                daemon.offsets.save()


def flush_spools(store: Store, *, timeout: float = 3.0) -> int:
    """Post every `pending-*.ndjson` that no live daemon will drain.

    `colab answer` with no session running spools its event under the newest
    offsets file's session, or `cli`; a daemon reads only its own spool and
    the hook fallback only the current session's, so those files were written
    and never read while the CLI reported the answer as queued. `colab sync`
    and `colab status` call this. A spool whose daemon is alive is left to it.
    """
    if not is_on(store):
        return 0
    cfg = canvas_config(store)
    poster = Poster(str(cfg.get("relay") or ""), str(cfg.get("room") or ""),
                    str(cfg.get("token") or ""), timeout=timeout)
    sent = 0
    for path in sorted(markers_dir(store).glob("pending-*.ndjson")):
        sid = path.name[len("pending-"):-len(".ndjson")]
        if tailer_alive(store, sid):
            continue
        data, events = read_pending(store, sid)
        invalid = {e["id"] for e in events if validate(e)}
        good = [e for e in events if e["id"] not in invalid]
        acked = poster.send(good, []) if good else set()
        if good and not acked:
            if not poster.ready():
                break
            continue
        if all(e["id"] in acked or e["id"] in invalid for e in events):
            consume_pending(store, sid, data)
        sent += len(acked)
    return sent


def answer(store: Store, target: dict[str, Any], text: str, *, timeout: float = 3) -> bool:
    """The agent's reply to a canvas message: one POST now, spooled if that fails.

    An `ask` is resolved with an `answer` event. A `ping` or a `say` cannot be
    (the relay resolves only asks, §10.1), so the reply to one is a `say` to the
    room, which is where the person who typed it is looking.
    """
    cfg = canvas_config(store)
    ask = str(target.get("id") or target.get("ask") or "")
    config = store.config()
    if str(target.get("kind") or "") in ("ping", "say"):
        with contextlib.suppress(Exception):
            post_message(str(cfg.get("relay") or ""), str(cfg.get("room") or ""),
                         str(cfg.get("token") or ""), to="*", kind="say",
                         text=f"re {ask}: {records.scrub(text)}"[:2000], timeout=timeout)
            return True
        return False
    body = {"ask": ask, "text": records.scrub(text)[:3000]}
    event = build("answer", body, session=str(target.get("session") or ""), lane="main",
                  harness=str(config.get("harness") or ""), model=str(config.get("model") or "") or None,
                  ref=ask, agent=str(config.get("agent") or ""))
    clean = sanitise(event, "summary", store.repo_root) or event
    sent = False
    with contextlib.suppress(Exception):
        sent = post_answer(str(cfg.get("relay") or ""), str(cfg.get("room") or ""),
                           str(cfg.get("token") or ""), clean, timeout=timeout)
    if not sent:
        with contextlib.suppress(Exception):
            offsets = sorted(markers_dir(store).glob("offsets-*.json"), key=lambda p: p.stat().st_mtime)
            sid = offsets[-1].name[len("offsets-"):-len(".json")] if offsets else "cli"
            spool(store, sid, clean)
    return sent


# ---------------------------------------------------------------- discovery loop


def ensure_tailers_for_cwd(store: Store, hours: float = 6) -> list[tuple[str, str]]:
    """One-shot: a tailer for every recent session of either harness on this checkout."""
    out = []
    with contextlib.suppress(Exception):
        for sid, path in discover_claude_transcripts(store.repo_root, hours):
            out.append((sid, ensure_tailer(store, sid, path, "claude-code")))
    with contextlib.suppress(Exception):
        for sid, path in discover_rollouts(store.repo_root, hours):
            out.append((sid, ensure_tailer(store, sid, path, "codex")))
    return out


def tail_discover(store: Store, *, once: bool = False) -> int:
    """`colab canvas tail --discover`: keep one tailer per discovered session."""
    pidfile = _marker(store, "tail-discover.pid")
    if not _claim_pidfile(pidfile):
        records.eprint("canvas: a discovery loop is already running for this profile")
        return 0
    pidfile.write_text(str(os.getpid()), encoding="utf-8")
    try:
        while True:
            config = store.config()
            if config.get("paused") or not isinstance(config.get("canvas"), dict):
                break
            ensure_tailers_for_cwd(store)
            if once:
                break
            time.sleep(DISCOVER_EVERY)
    finally:
        with contextlib.suppress(OSError):
            pidfile.unlink()
    return 0


# ---------------------------------------------------------------- entry point


def main(argv: list[str] | None = None) -> int:
    """`python -m agentcolab.canvas tail --session SID [--transcript PATH]`.

    The CLI's `colab canvas tail` should call `tail_loop`/`tail_discover`
    directly; this exists so the daemon can be started without the CLI verb.
    """
    import argparse
    records.force_utf8()
    parser = argparse.ArgumentParser(prog="agentcolab.canvas")
    parser.add_argument("action", choices=["tail"])
    parser.add_argument("--session", default="")
    parser.add_argument("--transcript", default=None)
    parser.add_argument("--harness", default=None)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--discover", action="store_true")
    args = parser.parse_args(argv)
    store = Store()
    if args.discover:
        return tail_discover(store, once=args.once)
    if not args.session:
        records.eprint("canvas: --session is required (or pass --discover)")
        return 2
    return tail_loop(store, args.session, args.transcript, once=args.once, harness=args.harness)


if __name__ == "__main__":
    sys.exit(main())
