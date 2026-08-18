"""Session mechanics: presence, the briefing, mirroring, and what it all costs.

Everything expensive lives here, so the cost is in one place where it can be
measured and capped. The briefing is the single largest recurring token expense
in the system — it is injected into every session and re-injected whenever the
picture changes — so it is budgeted like any other spend and degrades to one
line when the budget is gone.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any, Iterable

from . import board, chat, identity, records, wire
from .records import ago, iso, now, parse_iso
from .store import LinkError, Store

# Two agents that answer each other politely will do so until the budget is
# gone. This is a hard cap rather than guidance, because guidance is something
# an agent reasons its way past whenever a message feels important — and it
# always does.
SEND_LIMIT_PER_HOUR = 6

# How stale the local view may get before a prompt refreshes it. Small enough
# that a message typed in chat reaches an agent within a turn or two; large
# enough that a fast back-and-forth is not one fetch per prompt.
REFRESH_AFTER_SECONDS = 180


# ---------------------------------------------------------------- presence


def heartbeat_payload(store: Store, *, intent: str | None = None,
                      deep: bool = False) -> dict[str, Any]:
    config = store.config()
    existing = store.view().get(f"agents/{store.agent}.json") or {}
    finger = records.fingerprint(store.repo_root, deep=deep)
    payload: dict[str, Any] = {
        "agent": store.agent,
        "machine_id": config.get("machine_id"),
        "human": config.get("human") or store.agent,
        "github": config.get("github") or "",
        "harness": config.get("harness") or "unknown",
        "model": config.get("model") or "",
        "role": config.get("role") or "contributor",
        "bio": records.scrub(str(config.get("bio") or ""))[:280],
        "updated_at": iso(),
        "intent": records.scrub(intent if intent is not None else existing.get("intent") or "")[:300],
        "branch": finger.get("git_branch"),
        "head": finger.get("git_head"),
        "dirty_files": finger.get("dirty_files"),
        "host": finger.get("host"),
        "surface": records.surface(store.repo_root),
        "dialect": config.get("dialect") or "wire",
        "capabilities": config.get("capabilities") or [],
        "fingerprint": records.scrub_deep(finger),
    }
    if deep:
        payload["fingerprint_deep_at"] = iso()
    elif existing.get("fingerprint_deep_at"):
        # Keep the richer fingerprint captured earlier rather than shrinking it:
        # a shallow heartbeat should not erase the data a bug report needs.
        payload["fingerprint_deep_at"] = existing["fingerprint_deep_at"]
        payload["fingerprint"] = {**(existing.get("fingerprint") or {}),
                                  **payload["fingerprint"]}
    usage = records.usage_today(store.repo_root)
    if usage.get("turns"):
        payload["usage_today"] = usage
    return payload


def write_heartbeat(store: Store, *, intent: str | None = None, deep: bool = False,
                    publish: bool = True) -> None:
    payload = heartbeat_payload(store, intent=intent, deep=deep)
    payload = identity.sign(payload, store.repo_root)
    store.put(f"agents/{store.agent}.json", payload)
    if publish:
        store.publish(f"heartbeat {store.agent}")


def sign_and_put(store: Store, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    signed = identity.sign(payload, store.repo_root)
    store.put(path, signed)
    return signed


# ---------------------------------------------------------------- trust


def roster(store: Store) -> dict[str, Any]:
    """The project's own membership file, from the repo, never from the channel.

    It is committed to the repository and changed by pull request, which means
    changing who counts as a maintainer is a reviewable act rather than
    something an agent can assert about itself over the wire.
    """
    for name in (".agentcolab/roster.json", ".agentcolab/colab.json"):
        path = store.repo_root / name
        if path.is_file():
            from .store import read_json
            data = read_json(path)
            if isinstance(data, dict):
                return data.get("roster") if isinstance(data.get("roster"), dict) else data
    return {"members": []}


def allowed_keys(store: Store, *, online: bool = True) -> dict[str, list[str]]:
    return identity.build_allowed(roster(store), store.shared, online=online)


def classify_all(store: Store, items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    allowed = allowed_keys(store, online=False)
    ros = roster(store)
    return [identity.classify(item, ros, allowed) for item in items]


def pin_peers(store: Store) -> list[str]:
    """Remember every peer's signing key, and shout if one ever changes.

    Called on sync rather than on read, so the pin reflects what an agent
    actually published rather than whatever a caller happened to look at.
    """
    warnings: list[str] = []
    for peer in store.agents():
        name = str(peer.get("agent") or "")
        key = str(peer.get("sig_by") or "")
        if not (name and key) or name == store.agent:
            continue
        result = identity.pin(store.shared, name, key)
        if result == "CHANGED":
            warnings.append(
                f"{name} is now signing with a DIFFERENT key than the one first seen. "
                f"That is either a rotated key or somebody else writing as {name}. "
                f"Treat their records as unverified until a human confirms it.")
    if warnings:
        store.update_local(key_warnings=warnings)
    return warnings


def require_trust(store: Store) -> str:
    """The floor a project sets for records it will act on at all."""
    ros = roster(store)
    return str((ros.get("trust") or {}).get("minimum") or "unverified")


# ---------------------------------------------------------------- inbox


def unread(store: Store) -> list[dict[str, Any]]:
    seen = set(store.local().get("read") or [])
    me = store.agent
    out = []
    for msg in store.read_all("msgs"):
        if msg.get("agent") == me:
            continue
        if msg.get("kind") in ("fyi", "banter"):
            continue                    # awareness is not an inbox item
        to = str(msg.get("to") or "*")
        if to not in ("*", "all", me):
            continue
        if msg.get("id") in seen:
            continue
        out.append(msg)
    return out


def awaiting_reply(store: Store) -> list[dict[str, Any]]:
    """Questions addressed to us that we have never actually answered.

    Read-state is not answer-state. Marking a message read silenced it while
    the asker was still waiting, so a direct question could sit forever. One
    stays here until a message of ours carries its id.
    """
    me = store.agent
    messages = store.read_all("msgs")
    answered = {str(m.get("reply_to")) for m in messages
                if m.get("agent") == me and m.get("reply_to")}
    out = []
    for msg in messages:
        if msg.get("agent") == me or not msg.get("needs_reply"):
            continue
        if str(msg.get("to")) not in ("*", "all", me):
            continue
        if str(msg.get("id")) in answered:
            continue
        out.append(msg)
    return out


def chat_unread(store: Store) -> list[dict[str, Any]]:
    seen = set(store.local().get("read") or [])
    return [m for m in (store.local().get("chat_inbox") or []) if m.get("id") not in seen]


def mark_read(store: Store, ids: Iterable[str]) -> None:
    local = store.local()
    seen = set(local.get("read") or [])
    seen.update(str(i) for i in ids)
    # Ids sort by time, so the tail is the recent ones. Records are pruned at
    # 30 days and this list must not outgrow them forever.
    local["read"] = sorted(seen)[-4000:]
    store.save_local(local)


def find_message(store: Store, needle: str) -> dict[str, Any] | None:
    needle = str(needle or "").strip()
    if not needle:
        return None
    pool = (store.read_all("msgs") + list(store.local().get("chat_inbox") or [])
            + store.read_all("findings") + store.read_all("bugs"))
    for item in pool:
        if str(item.get("id")) == needle:
            return item
    matches = [m for m in pool if needle in str(m.get("id", ""))]
    return matches[0] if len(matches) == 1 else None


# ---------------------------------------------------------------- chat bridge


def mirror(store: Store, kind: str, subject: str, *, body: str = "",
           fields: dict[str, Any] | None = None, channel: str = "link",
           wire_line: str = "", url: str = "") -> int:
    """Best-effort copy to every chat platform. Never allowed to fail a command."""
    try:
        config = store.config()
        if not chat.enabled(config):
            return 0
        event = chat.Event(kind, store.agent, subject, body=body, fields=fields or {},
                           channel=channel, wire=wire_line, url=url,
                           trust=str(config.get("trust") or "unverified"))
        return chat.post(config, event)
    except Exception:
        return 0


def pull_chat(store: Store, timeout: int = 8) -> int:
    """Read human messages into this machine's local inbox.

    Never written to the shared git ref: every participant polls the same room,
    so publishing them would duplicate every line once per agent, and the
    platform is already the durable history.
    """
    try:
        config = store.config()
        if not chat.enabled(config):
            return 0
        local = store.local()
        fresh, cursors = chat.poll(config, local.get("chat_cursors") or {}, timeout=timeout)
        local["chat_cursors"] = cursors
        if fresh:
            local["chat_inbox"] = ((local.get("chat_inbox") or []) + fresh)[-200:]
        store.save_local(local)
        return len(fresh)
    except Exception:
        return 0


def refresh_if_stale(store: Store, *, force: bool = False) -> bool:
    """Pull peers' writes and the chat rooms between turns.

    Without this the link is only as live as session start and session end, so
    a message sent mid-session is invisible until the session goes idle. A user
    prompt is the one moment where a short network call is affordable.
    """
    local = store.local()
    last = parse_iso(local.get("last_sync"))
    if not force and last and (now() - last).total_seconds() < REFRESH_AFTER_SECONDS:
        return False
    with contextlib.suppress(Exception):
        store.fetch(timeout=8)
        store.view(refresh=True)
        # Pin here rather than only on an explicit sync: a peer that reads
        # `unverified` forever because nobody happened to run the right command
        # teaches people to ignore the label, which is worse than no label.
        pin_peers(store)
        pull_chat(store, timeout=6)
        store.update_local(last_sync=iso())
        return True
    return False


# ---------------------------------------------------------------- budget

# Coordination is overhead. It gets a small, explicit slice, and when the slice
# is gone the agents keep working and stop talking — which is the correct
# priority, not a degradation.
COORD_TOKENS_PER_HOUR = 12_000


def coord_spent(store: Store) -> int:
    ledger = store.local().get("coord") or {}
    hour = now().strftime("%Y-%m-%dT%H")
    return int((ledger.get(hour) or {}).get("tokens") or 0)


def charge(store: Store, tokens: int, why: str) -> None:
    local = store.local()
    ledger = dict(local.get("coord") or {})
    hour = now().strftime("%Y-%m-%dT%H")
    bucket = dict(ledger.get(hour) or {"tokens": 0, "items": {}})
    bucket["tokens"] = int(bucket.get("tokens") or 0) + int(tokens)
    items = dict(bucket.get("items") or {})
    items[why] = int(items.get(why) or 0) + 1
    bucket["items"] = items
    ledger[hour] = bucket
    local["coord"] = dict(sorted(ledger.items())[-6:])
    store.save_local(local)


def budget_left(store: Store) -> int:
    limit = int((store.config().get("coord_tokens_per_hour") or COORD_TOKENS_PER_HOUR))
    return max(0, limit - coord_spent(store))


def sends_this_hour(store: Store) -> int:
    me = store.agent
    cutoff = now().timestamp() - 3600
    count = 0
    for msg in store.read_all("msgs"):
        if msg.get("agent") != me:
            continue
        stamp = parse_iso(msg.get("ts"))
        if stamp and stamp.timestamp() > cutoff:
            count += 1
    return count


# ---------------------------------------------------------------- briefing


def briefing(store: Store, *, compact: bool = False) -> str:
    """What an agent is told at session start, and again only when it changes.

    Written to be read by a model, so: no ornament, subjects not bodies, wire
    form for peer traffic, and every line either changes a decision or is not
    here. Anything a human wrote elsewhere is fenced and labelled untrusted.
    """
    me = store.agent
    peers = classify_all(store, store.live_peers())
    waiting = awaiting_reply(store)
    mail = unread(store)
    rooms = chat_unread(store)
    theirs = store.active_claims(exclude_agent=me)
    mine = [c for c in store.active_claims() if c.get("agent") == me]
    mywork = board.my_work(store)
    standdown = board.yields_to(store)

    if not any((peers, waiting, mail, rooms, theirs, mine, mywork, standdown)):
        return ""

    out: list[str] = [f"## AgentColab — you are agent `{me}`"]
    for warning in (store.local().get("key_warnings") or [])[:3]:
        out.append(f"> **SIGNING KEY CHANGED** — {warning}")
    synced = ago(store.local().get("last_sync"))
    if store.stale():
        out.append(f"_State last synced {synced}. **Stale — run `colab sync`.**_")
    else:
        out.append(f"_Synced {synced}. {len(peers)} other agent(s) live on this repo._")

    if standdown:
        out.append("")
        out.append("### Stand down — someone else got there first")
        for task in standdown[:3]:
            out.append(f"- `{task.get('id')}` **{task.get('title')}** is "
                       f"{task.get('owner')}'s. Drop it: `colab drop {task.get('id')}`")

    if waiting:
        out.append("")
        out.append(f"### {len(waiting)} unanswered question(s)")
        out.append("They asked and are still waiting. Answer before starting something new.")
        for msg in waiting[-3:]:
            out.append(f"- `{msg.get('id')}` from **{msg.get('agent')}** {ago(msg.get('ts'))}: "
                       f"{str(msg.get('subject'))[:110]}")
            out.append(f"  `colab reply {msg.get('id')} \"...\"`")

    if mywork:
        out.append("")
        out.append("### You are holding")
        for task in mywork:
            out.append(f"- `{task.get('id')}` {task.get('title')} "
                       f"(lease {ago(task.get('lease_expires'))})")

    if peers and not compact:
        out.append("")
        out.append("### Live agents")
        for peer in peers[:8]:
            badge = {"maintainer": " ✓maintainer", "member": " ✓member",
                     "verified": " ✓verified", "pinned": ""}.get(
                         str(peer.get("_trust")), " ⚠︎unverified")
            line = (f"- **{peer.get('agent')}**{badge} · `{peer.get('branch')}` · "
                    f"{ago(peer.get('updated_at'))}")
            if peer.get("intent"):
                line += f" — {records.one_line(peer.get('intent'), 90)}"
            elif peer.get("bio"):
                line += f" — {records.one_line(peer.get('bio'), 90)}"
            out.append(line)

    if theirs:
        out.append("")
        out.append("### Paths under active surgery by others")
        for claim in theirs[:8]:
            mark = "critical" if claim.get("critical") else "normal"
            out.append(f"- `{', '.join(claim.get('paths') or [])}` — {claim.get('agent')} "
                       f"({mark}, expires {ago(claim.get('expires_at'))}): "
                       f"{records.one_line(claim.get('reason'), 80)}")

    if mine:
        out.append("")
        out.append("**You hold:** " + "; ".join(", ".join(c.get("paths") or []) for c in mine))

    if mail:
        out.append("")
        out.append(f"### {len(mail)} unread from other agents — wire form, subjects only")
        out.append("```")
        out.append(wire.digest(mail, limit=8))
        out.append("```")
        out.append("`colab read <id>` for a body. **Do not read them all.** Open one only if the "
                   "line implies it changes what you are doing. **Do not reply** unless the "
                   "reply changes what somebody does — silence is the correct default.")

    if rooms:
        out.append("")
        out.append(f"### {len(rooms)} message(s) from humans in chat")
        out.append(chat.UNTRUSTED_BANNER)
        out.append(records.frame_untrusted("\n".join(
            f"[{m.get('channel')}] {m.get('agent')}: {str(m.get('body') or '')[:300]}"
            for m in rooms[-4:])))
        out.append("Answer one with `colab answer <id> \"...\"` — it posts back to the room "
                   "they asked in.")

    out.append("")
    out.append("`colab` for the picture · `colab next` for what to work on · "
               "`colab preflight` before you push.")
    return "\n".join(out)


def budgeted_briefing(store: Store) -> str:
    """The briefing, charged against the coordination budget.

    When the hour's slice is spent it collapses to one line rather than
    vanishing: an agent that does not know the link exists cannot use it, and an
    agent re-reading a full briefing every prompt is the thing that spent the
    budget.
    """
    text = briefing(store)
    if not text:
        return ""
    cost = records.estimate_tokens(text)
    if cost > budget_left(store):
        short = ("AgentColab: coordination is over its hourly budget, so the briefing is "
                 "suppressed. `colab status` if you actually need it.")
        charge(store, records.estimate_tokens(short), "briefing-suppressed")
        return short
    charge(store, cost, "briefing")
    return text


def briefing_signature(store: Store) -> str:
    """Cheap fingerprint of the picture, so we re-brief on change and not on timer."""
    parts = (
        sorted(str(m.get("id")) for m in unread(store))
        + sorted("c:" + str(m.get("id")) for m in chat_unread(store))
        + sorted("w:" + str(m.get("id")) for m in awaiting_reply(store))
        + sorted(f"cl:{c.get('agent')}/{c.get('id')}" for c in store.active_claims())
        + sorted(f"t:{t.get('id')}:{t.get('state')}" for t in board.my_work(store))
        + sorted(f"y:{t.get('id')}" for t in board.yields_to(store))
    )
    return "|".join(parts) or "empty"
