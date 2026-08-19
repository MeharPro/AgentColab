"""ColabWire: the compact form agents talk in, expanded to prose for humans.

The honest engineering problem
------------------------------
"Let the agents invent their own compressed language" is an appealing idea that
does not survive contact with a tokenizer. Models are trained on natural
language and code; a private glyph alphabet tokenizes *worse* (rare bytes
fragment into many tokens), and decoding it costs the model reasoning it would
otherwise spend on your repository. Emergent-protocol experiments produce
strings that are shorter in characters and longer in tokens, understood less
reliably, and impossible for a human to audit — which is fatal here, because
every one of these messages is an untrusted input crossing a machine boundary.

What actually saves tokens, in descending order of effect:

1. **Not sending the message.** Rate limits and a "silence is the default"
   norm beat every encoding trick combined.
2. **Reference instead of restatement.** `#t-4f2a` and `api/pay.py:88` cost a
   handful of tokens and point at something both sides can already read. The
   repository is shared context; re-describing it is the actual waste.
3. **A fixed grammar with short, stable keys.** No JSON punctuation, no field
   names repeated in prose, no preamble, no politeness.
4. **Rendering locally.** Humans need sentences; sentences cost tokens. So the
   sentences are generated on each machine from the wire form, at zero token
   cost to anybody.

So ColabWire is deliberately ASCII, deliberately mnemonic, and deliberately
readable by a human squinting at it. It typically costs 55-75% fewer tokens
than the same content written as prose, and `colab wire --measure` will tell you
the real number for your own traffic rather than asking you to trust that one.

The grammar
-----------
    KIND from>to key=value key=value | free text

* `KIND`  two to four uppercase letters, from the table below.
* `from`  agent name. `to` is an agent, `*` for everyone, or `@human`.
* `key=value` ordered, no spaces inside a value; lists are comma separated.
* `|` separates the structured head from one line of free text.

Anything after a `|` is prose and is treated as untrusted text everywhere.
Unknown keys are preserved and shown, never dropped — a newer agent talking to
an older one degrades to "I do not know what that field means", not to silence.
"""

from __future__ import annotations

import re
import shlex
from typing import Any, Iterable

from . import records

VERSION = "AC/1"

# Kind codes. Short because they appear in every line; mnemonic because a human
# reading the channel at 2am should not need this file open.
KINDS: dict[str, tuple[str, str]] = {
    "HU":   ("heads-up", "an interface, route, column, env var or shape changed"),
    "Q":    ("question", "asks something; the sender may be blocked"),
    "A":    ("answer", "replies to a Q"),
    "D":    ("decision", "records a call that was made, so nobody relitigates it"),
    "CL":   ("claim", "announces paths under active surgery"),
    "RL":   ("release", "gives claimed paths back"),
    "BUG":  ("bug", "a failure, possibly machine-specific"),
    "FIX":  ("fix", "a bug is settled, with the cause"),
    "FIND": ("finding", "a durable lesson worth not rediscovering"),
    "TASK": ("task", "proposes a unit of work on the board"),
    "TAKE": ("take", "assigns a task to the sender"),
    "DONE": ("done", "a task is finished, usually with a PR"),
    "BLK":  ("blocked", "cannot proceed, and on what"),
    "REV":  ("review", "asks for or gives a review"),
    "PING": ("ping", "presence only; carries no content"),
    "NOTE": ("note", "anything that fits none of the above"),
}

# Keys, with the prose fragment each expands to. Ordered for rendering.
KEYS: dict[str, str] = {
    "p":    "paths",
    "t":    "task",
    "re":   "in reply to",
    "pr":   "pull request",
    "sha":  "commit",
    "br":   "branch",
    "sig":  "signature",
    "err":  "error",
    "sev":  "severity",
    "st":   "state",
    "ttl":  "expires in",
    "cost": "estimated tokens",
    "conf": "confidence",
    "need": "needs",
    "ref":  "reference",
}

_HEAD = re.compile(r"^\s*([A-Z]{1,5})\s+([A-Za-z0-9_\-]+)\s*>\s*([@*A-Za-z0-9_\-]+)\s*(.*)$")


class WireError(ValueError):
    pass


def encode(kind: str, sender: str, to: str = "*", *, text: str = "",
           **fields: Any) -> str:
    """Build one wire line. Values are scrubbed; nothing here trusts its input."""
    code = str(kind).upper()
    if code not in KINDS:
        code = "NOTE"
    head = [code, f"{records.slug(sender)}>{_norm_target(to)}"]
    for key, value in fields.items():
        if value in (None, "", [], {}):
            continue
        head.append(f"{key}={_encode_value(value)}")
    line = " ".join(head)
    body = records.scrub(str(text or "")).replace("\n", " ").replace("|", "/").strip()
    return f"{line} | {body}" if body else line


def _norm_target(to: str) -> str:
    text = str(to or "*").strip()
    if text in ("*", "all", ""):
        return "*"
    if text.startswith("@"):
        return "@" + records.slug(text[1:])
    return records.slug(text)


def _encode_value(value: Any) -> str:
    if isinstance(value, (list, tuple, set)):
        joined = ",".join(_encode_value(v) for v in value if v not in (None, ""))
        return joined or "-"
    text = records.scrub(str(value)).replace("\n", " ").strip()
    text = re.sub(r"\s+", "_", text)
    return text or "-"


def decode(line: str) -> dict[str, Any]:
    """Parse one wire line into a record. Never raises on garbage: worst case
    the whole line becomes a NOTE, because dropping a peer's message silently is
    a worse failure than showing it verbatim."""
    raw = str(line or "").strip()
    if not raw:
        raise WireError("empty line")
    head, _, text = raw.partition("|")
    match = _HEAD.match(head)
    if not match:
        return {"kind": "NOTE", "from": "", "to": "*", "fields": {},
                "text": raw.strip(), "malformed": True}
    code, sender, target, rest = match.groups()
    fields: dict[str, Any] = {}
    try:
        tokens = shlex.split(rest)
    except ValueError:
        tokens = rest.split()
    for token in tokens:
        key, sep, value = token.partition("=")
        if not sep:
            continue
        fields[key.strip()] = _decode_value(value)
    return {
        "kind": code if code in KINDS else "NOTE",
        "from": records.slug(sender),
        "to": _norm_target(target),
        "fields": fields,
        "text": text.strip(),
        "malformed": False,
    }


def _decode_value(value: str) -> Any:
    if value == "-":
        return ""
    if "," in value:
        return [v.replace("_", " ") if " " in v else v for v in value.split(",") if v]
    return value


def render(line: str | dict[str, Any], *, verbose: bool = True) -> str:
    """Wire -> the sentence a human reads. Costs no agent tokens at all."""
    record = decode(line) if isinstance(line, str) else dict(line)
    if record.get("malformed"):
        return f"(unparsed) {record.get('text', '')}"
    label, _ = KINDS.get(record["kind"], ("note", ""))
    target = record["to"]
    who = "everyone" if target == "*" else (
        f"the humans" if target.startswith("@") else target)
    out = [f"**{record['from']}** -> {who} · _{label}_"]
    fields = record.get("fields") or {}
    for key, value in fields.items():
        name = KEYS.get(key, key)
        shown = ", ".join(value) if isinstance(value, list) else str(value)
        out.append(f"  {name}: {shown}")
    if record.get("text"):
        out.append(f"  {record['text']}")
    if verbose and record["kind"] in ("Q", "BLK") and "re" not in fields:
        out.append("  (awaiting an answer)")
    return "\n".join(out)


def render_many(lines: Iterable[str]) -> str:
    return "\n".join(render(line) for line in lines)


# ---------------------------------------------------------------- measurement


def measure(prose: str, wire: str) -> dict[str, Any]:
    """Honest side-by-side. Reported, not asserted."""
    a = records.estimate_tokens(prose)
    b = records.estimate_tokens(wire)
    return {
        "prose_tokens": a,
        "wire_tokens": b,
        "saved": a - b,
        "ratio": round(1 - (b / a), 3) if a else 0.0,
    }


def from_message(msg: dict[str, Any]) -> str:
    """Render a stored message record as a wire line.

    This is how the compact form and the durable form stay one thing: records
    are stored as JSON because JSON is what merges conflict-free on a git ref,
    and projected to wire whenever an agent has to *read* them.
    """
    kind_map = {
        "heads-up": "HU", "question": "Q", "reply": "A", "decision": "D",
        "note": "NOTE", "handoff": "TASK", "blocked": "BLK", "review": "REV",
        "finding": "FIND", "bug-report": "BUG",
    }
    fields: dict[str, Any] = {}
    if msg.get("paths"):
        fields["p"] = msg["paths"]
    if msg.get("reply_to"):
        fields["re"] = msg["reply_to"]
    if msg.get("task"):
        fields["t"] = msg["task"]
    if msg.get("branch"):
        fields["br"] = msg["branch"]
    if msg.get("needs_reply"):
        fields["need"] = "reply"
    text = str(msg.get("subject") or "")
    body = str(msg.get("body") or "")
    if body and body[:60] != text[:60]:
        text = f"{text} — {body}" if text else body
    return encode(kind_map.get(str(msg.get("kind")), "NOTE"),
                  str(msg.get("agent") or "?"), str(msg.get("to") or "*"),
                  text=text[:400], **fields)


def digest(messages: list[dict[str, Any]], limit: int = 12) -> str:
    """The compact block an agent reads instead of N prose messages.

    Subjects only, in wire form, newest last. Bodies are fetched by id when the
    subject actually implies the agent needs one — which is rarely.
    """
    lines = [VERSION]
    for msg in messages[-limit:]:
        # The id comes off the wire from a peer. One newline in it forges an
        # extra line in the block an agent reads as authoritative -- a whole
        # fabricated record, attributed to whoever the forger likes.
        ident = re.sub(r"[^A-Za-z0-9._-]", "", str(msg.get("id") or "?"))[:64] or "?"
        lines.append(f"{ident} {from_message(msg)}")
    return "\n".join(lines)


def legend(kinds: Iterable[str] | None = None) -> str:
    """The decoder ring, emitted once per session rather than per message."""
    wanted = list(kinds) if kinds else list(KINDS)
    rows = [f"  {code:<5} {KINDS[code][0]:<10} {KINDS[code][1]}"
            for code in wanted if code in KINDS]
    return ("ColabWire — `KIND from>to key=value | text`\n" + "\n".join(rows)
            + "\n  keys: " + ", ".join(f"{k}={v}" for k, v in KEYS.items()))
