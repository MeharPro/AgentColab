"""The work board: how many agents divide one repository without negotiating.

The failure this exists to prevent is the expensive one. Two agents that both
notice the same broken test both fix it, both open a pull request, and a human
throws one away — having paid for both. Chat does not solve it, because by the
time an agent says "I'll take this" the other has already started.

Three mechanisms, in order of how much they save:

**Deterministic assignment.** `owner_of(task)` hashes the task id over the
sorted list of currently live agents. Every machine computes the same answer
with no round trip, no lease, and no message. An agent asking "what should I do
next" gets an answer nobody else will also get. When a machine goes quiet it
drops out of the roster and its share is redistributed automatically — work
never stalls waiting for an absent participant.

**Conflict-free taking.** A `take` is a record under the taker's own directory,
so two simultaneous takes cannot conflict on the git ref. When both land, both
machines resolve the winner identically — earliest timestamp, then lexically
smaller agent name — so the loser stands down on its own at the next sync
without anyone sending a message about it.

**Leases, not locks.** A taken task carries a TTL. An agent that dies mid-task
releases it by simply not renewing, and it returns to the pool. Nothing is ever
permanently owned by a machine that is no longer there.
"""

from __future__ import annotations

import hashlib
from typing import Any, Iterable

from . import records
from .records import ago, in_seconds, iso, now, parse_iso
from .store import Store

STATES = ("open", "taken", "review", "done", "dropped", "blocked")
DEFAULT_LEASE = "4h"
STALE_AGENT_HOURS = 6

# Rough size bands. Deliberately coarse: precision here is fake, and the only
# decision it feeds is "can one agent finish this in one session".
SIZES = {"xs": "minutes", "s": "under an hour", "m": "a session",
         "l": "several sessions", "xl": "split it first"}

PRIORITIES = {"p0": 0, "p1": 1, "p2": 2, "p3": 3}


def live_agents(store: Store, hours: int = STALE_AGENT_HOURS) -> list[str]:
    """Everyone whose heartbeat is recent, in an order every machine agrees on."""
    live: set[str] = set()
    for peer in store.agents():
        stamp = parse_iso(peer.get("updated_at"))
        if stamp and (now() - stamp).total_seconds() < hours * 3600:
            name = str(peer.get("agent") or "")
            if name:
                live.add(name)
    with_me = live | {store.agent}
    return sorted(with_me)


def owner_of(key: str, roster: list[str]) -> str:
    """Which agent owns a piece of work, computed identically everywhere.

    sha256 rather than Python's hash(): the built-in is salted per process, so
    two machines would disagree and the whole point would be lost.
    """
    if not roster:
        return ""
    digest = hashlib.sha256(str(key).encode()).digest()
    return roster[int.from_bytes(digest[:8], "big") % len(roster)]


# ---------------------------------------------------------------- records


def task_record(store: Store, title: str, *, body: str = "", paths: Iterable[str] = (),
                priority: str = "p2", size: str = "m", tags: Iterable[str] = (),
                deps: Iterable[str] = (), task_id: str | None = None) -> dict[str, Any]:
    return {
        "id": task_id or records.new_id("t"),
        "kind": "task",
        "agent": store.agent,
        "title": records.scrub(title)[:200],
        "body": records.scrub(body)[:4000],
        "paths": list(paths)[:40],
        "priority": priority if priority in PRIORITIES else "p2",
        "size": size if size in SIZES else "m",
        "tags": [records.slug(t, limit=24) for t in tags][:8],
        "deps": [str(d) for d in deps][:16],
        "state": "open",
        "created_at": iso(),
        "updated_at": iso(),
    }


def claim_record(store: Store, task_id: str, *, lease: str = DEFAULT_LEASE,
                 note: str = "") -> dict[str, Any]:
    return {
        "id": f"take-{task_id}",
        "kind": "take",
        "task": str(task_id),
        "agent": store.agent,
        "note": records.scrub(note)[:400],
        "created_at": iso(),
        "expires_at": in_seconds(records.parse_duration(lease)),
        "branch": records.fingerprint(store.repo_root).get("git_branch", ""),
    }


# ---------------------------------------------------------------- reading


def tasks(store: Store) -> dict[str, dict[str, Any]]:
    """Every task, with `takes` resolved and state derived rather than trusted.

    A task's state is not read from the record that defines it — the definer
    may be offline while somebody else finishes the work. It is derived from
    the live set of take and done records, so every machine computes the same
    board from the same inputs.
    """
    board: dict[str, dict[str, Any]] = {}
    for record in store.read_all("tasks"):
        if record.get("kind") != "task":
            continue
        task = dict(record)
        task.setdefault("state", "open")
        board[str(task.get("id"))] = task

    takes: dict[str, list[dict[str, Any]]] = {}
    for record in store.read_all("tasks"):
        if record.get("kind") != "take":
            continue
        takes.setdefault(str(record.get("task")), []).append(record)

    for task_id, task in board.items():
        holders = [t for t in takes.get(task_id, [])
                   if not _expired(t) and not t.get("released_at")]
        winner = resolve_take(holders)
        finished = [t for t in takes.get(task_id, []) if t.get("done_at")]
        if finished:
            latest = max(finished, key=lambda t: str(t.get("done_at")))
            task["state"] = "done"
            task["owner"] = latest.get("agent")
            task["pr"] = latest.get("pr")
            task["done_at"] = latest.get("done_at")
        elif winner:
            task["state"] = "review" if winner.get("review_at") else "taken"
            task["owner"] = winner.get("agent")
            task["lease_expires"] = winner.get("expires_at")
            task["branch"] = winner.get("branch")
            task["contested"] = [t.get("agent") for t in holders
                                 if t.get("agent") != winner.get("agent")]
        elif task.get("state") not in ("dropped", "blocked"):
            task["state"] = "open"
    return board


def _expired(take: dict[str, Any]) -> bool:
    stamp = parse_iso(take.get("expires_at"))
    return bool(stamp and stamp < now())


def resolve_take(holders: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Same inputs, same winner, on every machine.

    Earliest wins because it is the one that started; the name breaks a tie
    because two records can carry the same second and something has to be
    total.
    """
    if not holders:
        return None
    return sorted(holders, key=lambda t: (str(t.get("created_at") or ""),
                                          str(t.get("agent") or "")))[0]


def open_tasks(store: Store) -> list[dict[str, Any]]:
    ready = []
    board = tasks(store)
    for task in board.values():
        if task.get("state") != "open":
            continue
        # A task whose dependency is unfinished is not workable yet, and
        # offering it produces an agent that gets stuck and asks a human.
        if any(board.get(dep, {}).get("state") not in ("done",)
               for dep in task.get("deps") or []):
            continue
        ready.append(task)
    ready.sort(key=lambda t: (PRIORITIES.get(str(t.get("priority")), 2),
                             str(t.get("created_at"))))
    return ready


def next_for(store: Store, agent: str | None = None) -> dict[str, Any] | None:
    """The single task this agent should pick up, with nobody consulted.

    Two passes on purpose. First: tasks this agent deterministically owns, so
    parallel agents never collide. Only if it owns none does it fall back to
    the general pool — which is what makes a two-agent project work as well as
    a twenty-agent one.
    """
    me = agent or store.agent
    roster = live_agents(store)
    ready = open_tasks(store)
    mine = [t for t in ready if owner_of(str(t.get("id")), roster) == me]
    return (mine or ready or [None])[0]


def my_work(store: Store) -> list[dict[str, Any]]:
    me = store.agent
    return [t for t in tasks(store).values()
            if t.get("owner") == me and t.get("state") in ("taken", "review")]


def summarise(store: Store, limit: int = 12) -> list[str]:
    board = tasks(store)
    counts: dict[str, int] = {}
    for task in board.values():
        counts[str(task.get("state"))] = counts.get(str(task.get("state")), 0) + 1
    head = " · ".join(f"{state} {counts[state]}" for state in STATES if state in counts)
    lines = [f"board: {head or 'empty'}"]
    for task in sorted(board.values(),
                       key=lambda t: (PRIORITIES.get(str(t.get("priority")), 2),
                                      str(t.get("updated_at"))))[:limit]:
        owner = task.get("owner") or "-"
        flag = "!" if task.get("contested") else " "
        lines.append(f"  {flag}{task.get('id')}  {str(task.get('priority')).upper():<3} "
                     f"{str(task.get('state')):<7} {owner:<12} {str(task.get('title'))[:60]}")
    return lines


def contested(store: Store) -> list[dict[str, Any]]:
    """Tasks more than one agent took. Rendered so a human can see it happening."""
    return [t for t in tasks(store).values() if t.get("contested")]


def yields_to(store: Store) -> list[dict[str, Any]]:
    """Tasks this agent took but deterministically lost — stand down on these."""
    me = store.agent
    out = []
    for task in tasks(store).values():
        if task.get("owner") and task["owner"] != me and me in (task.get("contested") or []):
            out.append(task)
    return out
