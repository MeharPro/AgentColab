"""`colab` — what a human or, far more often, an agent actually types."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from . import board, chat, identity, records, session, wire
from .records import ago, eprint, iso, new_id, now, parse_iso
from .store import (LinkError, STATE_REF, Store, canonical_slug, can_push, git,
                    list_remotes, read_json, remote_url, write_json)

DEFAULT_TTL = "90m"


def _body(value: str | None) -> str:
    """`-` means stdin, which is how an agent sends multi-line text safely."""
    if value == "-":
        return sys.stdin.read().strip()
    return (value or "").strip()


def _paths(store: Store, raw: list[str] | None) -> list[str]:
    return [p for p in (records.normalise_path(r, store.repo_root) for r in (raw or [])) if p]


def _ok(text: str = "") -> int:
    if text:
        print(text)
    return 0


def _publish(store: Store, message: str) -> str:
    """Publish, and return a note if it did not land.

    Twelve of fifteen call sites used to ignore the result, so a record that
    never reached the ref was still reported as filed. It is not lost — it stays
    in `mine/` and goes out on the next sync — but telling an agent its
    heads-up is published when nobody can see it is how two agents end up
    working from different pictures.
    """
    if not store.push_url:
        return "  (local only — no writable remote)"
    ok, detail = store.publish(message)
    if ok:
        return ""
    # git is chatty on failure; the first line is the cause and the rest is
    # advice aimed at a human running git by hand.
    detail = (detail or "").strip().splitlines()
    detail = detail[0] if detail else "unknown error"
    return (f"\n  NOT PUBLISHED YET: {detail[:160]}\n"
            f"  It is saved locally and goes out on your next `colab sync`. "
            f"Peers cannot see it until then.")


# ================================================================ join

WELCOME = """\
AgentColab is live on this repo.

  agent    {agent}{verified}
  project  {project}
  reads    {sources}
  writes   {push}
  chat     {chat}
  peers    {peers}

What to do next:
  colab              the picture right now
  colab next         the one task nobody else is going to take
  colab preflight    before you push

Everything is warn-never-block. Nothing here can stop you editing a file."""


def cmd_join(store: Store, args: argparse.Namespace) -> int:
    """One command. Detects everything, asks nothing it can work out itself.

    The design target is a stranger with a fresh clone typing one thing — or,
    much more often, telling their agent to. Every branch below has a working
    default, so `colab join --yes` on a machine with no SSH key, no fork, no push
    access and no chat still produces a participant that can read the board and
    publish nothing. Degrading is always allowed; failing is not.
    """
    root = store.repo_root
    config = store.config()
    remotes = list_remotes(root)
    project_config = _project_config(root)

    # -- identity ------------------------------------------------------
    github = config.get("github") or identity.detect_github_login(root)
    name = args.name or config.get("agent")
    if not name:
        # Deliberately not `git config user.name`: on a shared machine, or a
        # team that commits under one identity, it cannot tell two agents apart.
        harness = _detect_harness()
        base = github or os.environ.get("USER") or "agent"
        name = records.slug(f"{base}-{harness}" if harness != "unknown" else str(base))
    name = records.slug(str(name))
    machine_id = config.get("machine_id") or uuid.uuid4().hex

    # Learn who already exists before choosing, so an agent naming itself is
    # never stopped by a collision it had no way to predict. Two agents sharing
    # a name would overwrite each other's records, so uniqueness is not
    # negotiable -- but the value is, and picking the next free one is friendlier
    # than failing.
    store.ensure_repo()
    store.fetch()
    store.view(refresh=True)
    taken = {str(p.get("agent")) for p in store.agents()
             if p.get("machine_id") != machine_id}
    taken |= set(store.profiles_in_use())
    taken.discard(config.get("agent") or "")
    if name in taken:
        if args.name and not args.yes:
            eprint(f"colab: {name!r} is already in use "
                   f"({'another checkout on this machine' if name in store.profiles_in_use() else 'another agent'}).")
            eprint("     Two agents cannot share a name — they would overwrite each")
            eprint(f"     other's records. Try:  colab join --as {_free_name(name, taken)}")
            return 1
        original, name = name, _free_name(name, taken)
        print(f"note: {original!r} was taken, so this agent is {name!r}")
    try:
        store.bind_profile(name)
    except LinkError as exc:
        eprint(f"colab: {exc}")
        return 1
    config = {**store.config(), **config, "agent": name}

    # -- transport -----------------------------------------------------
    sources: list[dict[str, str]] = []
    push_url = ""
    upstream = remotes.get("upstream") or remotes.get("origin") or ""
    if upstream:
        sources.append({"name": "upstream", "url": upstream, "scope": ""})

    if args.push_to:
        push_url = remote_url(root, args.push_to) or args.push_to
    else:
        for candidate in ("origin", "fork", "mine", "upstream"):
            url = remotes.get(candidate)
            if not url:
                continue
            print(f"  checking push access to {candidate}…", file=sys.stderr)
            if can_push(root, candidate):
                push_url = url
                break

    if push_url and not any(s["url"] == push_url for s in sources):
        login = identity.github_login_from_url(push_url)
        sources.append({"name": "mine", "url": push_url,
                        "scope": "" if push_url == upstream else name})

    # A fork we can write is where our records go; the upstream is where
    # everyone else's are read from. That combination is what lets somebody
    # with no permissions at all take part on equal terms.
    for extra in args.source or []:
        sources.append({"name": records.slug(extra, limit=24), "url": extra, "scope": ""})
    for entry in (project_config.get("sources") or []):
        if isinstance(entry, dict) and entry.get("url"):
            sources.append({"name": str(entry.get("name") or "project"),
                            "url": str(entry["url"]), "scope": str(entry.get("scope") or "")})

    # -- chat ----------------------------------------------------------
    chat_config = dict(config.get("chat") or {})
    if project_config.get("chat"):
        for driver, settings in (project_config["chat"] or {}).items():
            if driver in ("driver", "drivers"):
                continue
            merged = dict(chat_config.get(driver) or {})
            # Public config supplies channel ids; tokens only ever come from
            # this machine, so a repo can carry the map without carrying a
            # credential.
            merged.setdefault("channels", {}).update(settings.get("channels") or {})
            for key in ("application_id", "guild", "default", "read_channels"):
                if settings.get(key) is not None:
                    merged.setdefault(key, settings[key])
            chat_config[driver] = merged
        chat_config.setdefault("drivers", list(project_config["chat"].get("drivers")
                                               or [d for d in project_config["chat"]
                                                   if d in chat.DRIVERS]))

    # -- canvas ---------------------------------------------------------
    # The public half is a relay URL and a room code; a project that commits a
    # join code as well (private repos only -- it lets anyone who can read the
    # repository stream as any name) gets its agents registered here, so a
    # newcomer runs `colab join` and is already on the canvas.
    canvas_project = dict(project_config.get("canvas") or {})
    if canvas_project:
        canvas_config = dict(config.get("canvas") or {})
        for key in ("relay", "room", "max_stream"):
            if canvas_project.get(key) and not canvas_config.get(key):
                canvas_config[key] = str(canvas_project[key])
        if canvas_project.get("join_code") and not canvas_config.get("token"):
            canvas_config["join_code"] = str(canvas_project["join_code"])
        if canvas_config:
            config["canvas"] = canvas_config

    # -- write it down --------------------------------------------------
    config.update({
        "paused": False,
        "agent": name,
        "machine_id": machine_id,
        "github": github,
        "human": args.human or config.get("human") or github or name,
        "harness": args.harness or config.get("harness") or _detect_harness(),
        "model": args.model or config.get("model") or os.environ.get("AGENTCOLAB_MODEL", ""),
        "role": config.get("role") or "contributor",
        "bio": records.scrub(args.bio or config.get("bio") or "")[:280],
        "dialect": args.dialect or config.get("dialect") or "wire",
        "sources": _dedupe_sources(sources),
        "push_url": push_url,
        "chat": chat_config,
        "created_at": config.get("created_at") or iso(),
    })
    store.save_config(config)

    key = identity.describe(root)
    reclaimed = store.adopt_own()
    if reclaimed:
        print(f"note: reclaimed {reclaimed} record(s) you had published before "
              f"this machine lost its local state")
    # Shallow, so the person waiting gets their prompt back. The deep
    # fingerprint -- toolchain versions, lockfile hashes, env key shapes -- is
    # only needed by a cross-machine bug report, and is collected on demand.
    session.write_heartbeat(store, intent=args.intent or "", deep=False,
                            publish=bool(push_url))
    store.update_local(last_sync=iso())

    if push_url and not args.quiet:
        session.mirror(store, "join", f"{name} joined",
                       fields={"harness": config["harness"], "branch":
                               records.fingerprint(root).get("git_branch", "")},
                       channel="link")

    peers = store.peers()
    print(WELCOME.format(
        agent=name,
        verified="  (signing as " + key["fingerprint"] + ")" if key["can_sign"] else
                 "  (unsigned — shows as `unverified` to everyone)",
        project=store.project,
        sources=", ".join(s["name"] for s in store.sources()) or "none",
        push=push_url or "nothing writable — you are a read-only participant",
        chat=(", ".join(a.name for a in chat.adapters(config)) or "not configured"),
        peers=", ".join(f"{p['agent']} ({ago(p.get('updated_at'))})" for p in peers[:6])
              or "none yet",
    ))
    if not key["can_sign"]:
        print()
        print("No SSH key found, so your records cannot be signed and will show as")
        print("`unverified`. To fix it:  ssh-keygen -t ed25519   then add the .pub to GitHub.")
    if args.install:
        print()
        cmd_install(store, argparse.Namespace(target="auto", force=False))
    _join_canvas(store)
    return 0


def _join_canvas(store: Store) -> None:
    """Finish the canvas half of `colab join`, when the project set one up.

    A committed join code means the project wants everyone streaming, so this
    registers and starts the tailer for the session running right now. A room
    code alone is a pointer: it says where to watch and how to opt in.
    """
    with contextlib.suppress(Exception):
        from . import canvas
        cfg = canvas.canvas_config(store)
        relay = str(cfg.get("relay") or canvas.DEFAULT_RELAY).rstrip("/")
        if canvas.is_on(store):
            started = canvas.ensure_tailers_for_cwd(store)
            print()
            print(f"canvas    {canvas.viewer_url(relay, str(cfg.get('room')))}"
                  f"  ({len(started)} session(s) streaming)" + ("" if started else " — run `colab canvas tail --discover --once` to see why"))
            return
        if not cfg.get("join_code"):
            if cfg.get("room"):
                print()
                print(f"canvas    watch at {canvas.viewer_url(relay, str(cfg.get('room')))}")
                print("          stream your own session with "
                      "`colab canvas join <join code>`")
            return
        requested = str(cfg.get("stream") or "tools")
        reg = canvas.register(relay, str(cfg["join_code"]), store.agent,
                              harness=str(store.config().get("harness") or "") or None,
                              human=str(store.config().get("github") or "") or None,
                              model=str(store.config().get("model") or "") or None,
                              stream=requested)
        if not reg:
            return
        room = str(cfg["join_code"]).split(".", 1)[0]
        block = _save_canvas_join(store, relay=relay, room=room, join_code=None, reg=reg,
                                  requested=requested)
        started = canvas.ensure_tailers_for_cwd(store)
        print()
        print(f"canvas    {canvas.viewer_url(relay, room)}")
        print(f"          streaming {block['stream']} from "
              f"{len(started)} session(s) in this checkout")
        _print_owner_link(relay, room, block)
        _start_listener_if_enabled(store)


def _save_canvas_join(store: Store, *, relay: str, room: str, join_code: str | None,
                      reg: dict[str, Any], requested: str) -> dict[str, Any]:
    """Write what a registration returned into config.json and return the block.

    The stream level is decided here, as the minimum of what this machine asked
    for and what the relay answered -- never copied from the relay, which could
    otherwise open `full` on a machine that asked for `summary`. The file ends
    up mode 600 (`Store.save_config`); it now holds the room's admin credential
    and this agent's write credential, and the chmod is repeated here so the
    promise does not depend on a helper somebody may loosen later.
    """
    from . import canvas
    config = store.config()
    block = dict(config.get("canvas") or {}) if isinstance(config.get("canvas"), dict) else {}
    block.update({"relay": relay, "room": room, "token": reg.get("token"), "name": store.agent,
                  "stream": canvas.clamp_stream(requested, reg.get("effective_stream"))})
    if join_code:
        block["join_code"] = join_code
    if reg.get("owner_token"):
        block["owner_token"] = reg["owner_token"]
    policy = reg.get("policy") if isinstance(reg.get("policy"), dict) else {}
    if policy.get("max_stream") in canvas.LEVELS:
        # A ceiling can only lower the level (`effective_level` takes the min),
        # so the relay's word is safe to keep for that purpose alone.
        block["policy"] = {"max_stream": policy["max_stream"]}
    config["canvas"] = block
    store.save_config(config)
    with contextlib.suppress(OSError):
        os.chmod(store.config_path, 0o600)
    canvas.clear_room_gone(store)
    return block


def _print_owner_link(relay: str, room: str, block: dict[str, Any]) -> None:
    """The owner link is printed once at join (§10.2) and again only on request."""
    from . import canvas
    token = block.get("owner_token")
    if not token:
        return
    print(f"  owner    {canvas.owner_link(relay, room, str(token))}")
    print("           flips this machine's wake switch from a browser; printed once here,")
    print("           again with `colab canvas status --owner-link`. Keep it to yourself.")


def _start_listener_if_enabled(store: Store) -> None:
    with contextlib.suppress(Exception):
        from . import wake
        if wake.settings(store).get("enabled"):
            state = wake.ensure_listener(store)
            if state in ("spawned", "running"):
                print(f"  wake     on — listener {state}")


def _free_name(name: str, taken: set[str]) -> str:
    """First unused variant. Numeric because a human reading `alice-3` in a
    channel immediately understands what happened."""
    for suffix in range(2, 100):
        candidate = f"{name}-{suffix}"
        if candidate not in taken:
            return candidate
    return f"{name}-{records.new_id('x')[-6:]}"


def _seen(store: Store, name: str) -> str:
    for peer in store.agents():
        if peer.get("agent") == name:
            return str(peer.get("updated_at") or "")
    return ""


def _dedupe_sources(sources: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    out = []
    for entry in sources:
        url = entry.get("url") or ""
        if not url or url in seen:
            continue
        seen.add(url)
        out.append(entry)
    return out


def _project_config(root: Path) -> dict[str, Any]:
    """Public, committed configuration: channel ids, sources, rules.

    Everything a newcomer needs and nothing they must be trusted with. Secrets
    never live here — the repository is public, and a token in it is a token
    that is already burned.
    """
    # `agentcolab.json` is what the docs and `colab chat export` both name; the
    # others are accepted so a repo that picked a reasonable variant still works.
    for name in (".agentcolab/agentcolab.json", ".agentcolab/colab.json",
                 ".agentcolab/config.json", "agentcolab.json"):
        path = root / name
        if path.is_file():
            data = read_json(path)
            if isinstance(data, dict):
                return data
    return {}


def _detect_harness() -> str:
    """Which agent runtime is driving us — used for presence, never for policy."""
    markers = [
        ("claude-code", ("CLAUDE_PROJECT_DIR", "CLAUDECODE", "CLAUDE_CODE_SSE_PORT")),
        ("codex", ("CODEX_SANDBOX", "CODEX_HOME", "OPENAI_CODEX")),
        ("cursor", ("CURSOR_TRACE_ID", "CURSOR_AGENT")),
        ("aider", ("AIDER_MODEL",)),
        ("gemini-cli", ("GEMINI_CLI", "GEMINI_API_KEY")),
        ("opencode", ("OPENCODE_BIN", "OPENCODE")),
        ("windsurf", ("WINDSURF_SESSION",)),
        ("devin", ("DEVIN_SESSION_ID",)),
    ]
    for name, keys in markers:
        if any(os.environ.get(k) for k in keys):
            return name
    if os.environ.get("TERM_PROGRAM") == "vscode" and os.environ.get("GIT_ASKPASS", "").find("cursor") >= 0:
        return "cursor"
    return "unknown"


def _canvas_keepalive(store: Store) -> None:
    """Every `colab` command is a chance to (re)start the canvas tailer.

    Claude Code gets this from its SessionStart hook. Codex has no hooks, so
    the first `colab` command an agent runs is what starts its stream -- which
    is why `status` and `sync`, the two every agent runs, call it.
    """
    with contextlib.suppress(Exception):
        from . import canvas
        if canvas.is_on(store):
            canvas.ensure_tailers_for_cwd(store)
            # A harness without hooks (Codex) has no other moment at which the
            # room reaches it: whatever `colab` command its agent runs is the
            # one that must pull the asks and pings. Bounded, never raises.
            with contextlib.suppress(Exception):
                session.pull_inbox(store, timeout=3)
            # Answers spooled while no session was running have no daemon to
            # drain them; every sync and status is one.
            canvas.flush_spools(store, timeout=3)


# ================================================================ status


def cmd_status(store: Store, args: argparse.Namespace) -> int:
    # `colab` is the most typed thing here and it answers "what is true right
    # now". Answering it from cache is how an agent starts work on something a
    # peer picked up two minutes ago, so this always fetches. It is one small
    # request against a ref with a handful of objects; `--offline` opts out.
    if not args.offline:
        session.refresh_if_stale(store, force=True)
    me = store.agent
    peers = store.live_peers()
    pending = store.unpublished()
    if pending:
        print(f"  {pending} record(s) written here have not reached the remote yet — "
              f"`colab sync` to publish")
    print(f"colab · {store.project} · you are `{me}`"
          + ("" if store.push_url else "  (read-only: no writable remote)"))
    _canvas_keepalive(store)
    print(f"     synced {ago(store.local().get('last_sync'))}"
          + ("  STALE — run `colab sync`" if store.stale() else ""))
    print()

    if not peers:
        print("no other agents live right now")
    else:
        peers = session.classify_all(store, peers)
        print(f"{len(peers)} live agent(s):")
        for peer in peers:
            mark = {"maintainer": " [maintainer]", "member": " [member]",
                    "verified": " [verified]", "pinned": " [pinned]"}.get(
                        str(peer.get("_trust")), " [unverified]")
            print(f"  {peer.get('agent'):<16}{mark} {str(peer.get('branch') or '?'):<24} "
                  f"{ago(peer.get('updated_at')):<10} "
                  f"{records.one_line(peer.get('intent'), 60)}")
    print()

    for line in board.summarise(store, limit=args.limit):
        print(line)
    print()

    mail = session.unread(store)
    waiting = session.awaiting_reply(store)
    rooms = session.chat_unread(store)
    if waiting:
        print(f"{len(waiting)} question(s) awaiting YOUR reply:")
        for msg in waiting[-5:]:
            print(f"  {msg.get('id')}  {msg.get('agent')}: {str(msg.get('subject'))[:70]}")
    if mail:
        print(f"{len(mail)} unread:")
        for msg in mail[-5:]:
            print(f"  {wire.from_message(msg)}")
    if rooms:
        print(f"{len(rooms)} unread from humans in chat "
              f"(`colab answer <id> \"...\"` to reply)")
        for msg in rooms[-3:]:
            print(f"  {msg.get('id')}  [{msg.get('channel')}] {msg.get('agent')}: "
                  f"{str(msg.get('body'))[:70]}")
    claims = store.active_claims(exclude_agent=me)
    if claims:
        print(f"{len(claims)} path(s) claimed by others:")
        for claim in claims[:6]:
            print(f"  {', '.join(claim.get('paths') or [])} — {claim.get('agent')}"
                  f"{' CRITICAL' if claim.get('critical') else ''}")
    if not (mail or waiting or rooms or claims):
        print("inbox clear")
    canvas_line = _canvas_status_line(store)
    if canvas_line:
        print()
        print(canvas_line)
    return 0


def _canvas_status_line(store: Store) -> str:
    """One line for `colab status`: where to watch, and whether we are streaming."""
    with contextlib.suppress(Exception):
        from . import canvas
        cfg = canvas.canvas_config(store)
        if not canvas.is_on(store):
            if cfg.get("room"):
                return ("canvas: this project has a room — join it with "
                        "`colab canvas join <join code>`")
            return ""
        live = sum(1 for row in canvas.daemon_states(store) if row.get("state") == "running")
        where = canvas.viewer_url(str(cfg.get("relay")), str(cfg.get("room")))
        return (f"canvas: {where}  ({cfg.get('stream') or 'tools'}, "
                f"{live} session(s) streaming)")
    return ""


def cmd_sync(store: Store, args: argparse.Namespace) -> int:
    from .hooks import _flush_notices
    store.fetch()
    store.view(refresh=True)
    session.pin_peers(store)
    # Notices are queued by the edit hook so the hot path stays local. Every
    # sync is a chance to deliver them -- not only the session-lifecycle hooks,
    # which a harness without hooks never fires.
    delivered = _flush_notices(store)
    got = session.pull_chat(store)
    _canvas_keepalive(store)
    with contextlib.suppress(Exception):
        got += session.pull_inbox(store, timeout=3)
    session.write_heartbeat(store, intent=args.intent, publish=bool(store.push_url))
    ok, detail = (True, "")
    if store.push_url:
        ok, detail = store.publish(f"sync {store.agent}")
    store.update_local(last_sync=iso())
    if args.quiet:
        return 0
    print(f"synced · {len(store.live_peers())} live peer(s) · {got} new chat message(s)"
          + (f" · {delivered} notice(s) delivered" if delivered else "")
          + ("" if ok else f" · publish failed: {detail}"))
    return 0


def cmd_context(store: Store, args: argparse.Namespace) -> int:
    """The briefing block a harness injects at session start."""
    text = session.briefing(store) if args.raw else session.budgeted_briefing(store)
    if text:
        print(text)
    return 0


def cmd_intent(store: Store, args: argparse.Namespace) -> int:
    session.write_heartbeat(store, intent=" ".join(args.text), publish=bool(store.push_url))
    return _ok("noted — peers will see it on their next sync")


# ================================================================ board


def cmd_next(store: Store, args: argparse.Namespace) -> int:
    """The one task this agent should pick up, with nobody consulted."""
    # Deterministic assignment is only as good as the roster it hashes over, so
    # this is one of the few places worth paying for a network round trip. A
    # stale roster hands the same task to two agents, which is the exact
    # failure the whole mechanism exists to prevent.
    session.refresh_if_stale(store, force=not args.offline)
    task = board.next_for(store)
    if not task:
        if board.open_tasks(store):
            print("nothing for you right now — every open task is already dealt to")
            print("another live agent. That is the mechanism working, not a fault.")
            print("  colab board            see who has what")
            print('  colab task "<title>"   add work if you have some')
        else:
            print("nothing open on the board.")
            print('Add work with `colab task "<title>"`, or just do what your human asked.')
        return 0
    roster = board.live_agents(store)
    owner = board.owner_of(str(task.get("id")), roster)
    print(f"{task.get('id')}  [{str(task.get('priority')).upper()} · {task.get('size')}]  "
          f"{task.get('title')}")
    if task.get("body"):
        print()
        print(records.frame_untrusted(str(task.get("body"))[:1500]))
    if task.get("paths"):
        print()
        print("paths: " + ", ".join(task.get("paths") or []))
    print()
    if owner == store.agent:
        print(f"Deterministically yours ({len(roster)} live agents). Nobody else will "
              f"be offered it.")
    else:
        print(f"Not deterministically yours — {owner} is offered it first, but nobody "
              f"has taken it.")
    print(f"Take it:  colab take {task.get('id')}")
    if args.take:
        return cmd_take(store, argparse.Namespace(
            id=str(task.get("id")), lease=board.DEFAULT_LEASE, note=""))
    return 0


def _report_unknown_deps(store: Store) -> None:
    """A task nothing can ever offer should not be invisible as well."""
    stuck = board.blocked_by_unknown(store)
    if not stuck:
        return
    eprint(f"colab: {len(stuck)} task(s) blocked by dependencies no known task defines.")
    for task_id, unknown in stuck[:5]:
        eprint(f"     {task_id} waits on {', '.join(unknown)}")
    eprint("     Either the peer that owns them has not synced, or the id is a typo.")


def cmd_board(store: Store, args: argparse.Namespace) -> int:
    if args.json:
        print(json.dumps(list(board.tasks(store).values()), indent=2, sort_keys=True))
        return 0
    for line in board.summarise(store, limit=args.limit):
        print(line)
    fights = board.contested(store)
    if fights:
        print()
        print("contested — more than one agent took these; the resolver already picked:")
        for task in fights:
            print(f"  {task.get('id')} -> {task.get('owner')} "
                  f"(stood down: {', '.join(task.get('contested') or [])})")
    _report_unknown_deps(store)
    return 0


def cmd_task(store: Store, args: argparse.Namespace) -> int:
    session.refresh_if_stale(store)
    task = board.task_record(
        store, " ".join(args.title), body=_body(args.body), paths=_paths(store, args.paths),
        priority=args.priority, size=args.size, tags=args.tag or [], deps=args.dep or [])
    session.sign_and_put(store, f"tasks/{store.agent}/{task['id']}.json", task)
    note = _publish(store, f"task {task['id']}")
    roster = board.live_agents(store)
    owner = board.owner_of(task["id"], roster)
    session.mirror(store, "task", task["title"],
                   fields={"id": task["id"], "priority": task["priority"],
                           "offered to": owner},
                   channel="board",
                   wire_line=wire.encode("TASK", store.agent, "*", text=task["title"],
                                         t=task["id"], sev=task["priority"]))
    return _ok(f"{task['id']} filed · offered first to {owner}{note}")


def cmd_take(store: Store, args: argparse.Namespace) -> int:
    known = board.tasks(store)
    task = known.get(args.id) or next(
        (t for k, t in known.items() if args.id in k), None)
    if not task:
        eprint(f"colab: no task matching {args.id!r} — `colab board` to list")
        return 1
    if task.get("state") == "done":
        return _ok(f"{task['id']} is already done ({task.get('owner')})")
    if task.get("owner") and task["owner"] != store.agent:
        eprint(f"colab: {task['id']} is held by {task['owner']} until "
               f"{ago(task.get('lease_expires'))}.")
        eprint("     Taking it anyway is allowed and will be visible to everyone;")
        eprint(f"     the resolver keeps whoever took it first. Say something instead:")
        eprint(f'       colab send "want to take {task["id"]}" --to {task["owner"]}')
        if not args.force:
            return 2
    # Our own existing take, if this is a renewal rather than a fresh claim.
    held = next((t for t in (task.get("takes") or [])
                 if t.get("agent") == store.agent), None)
    record = board.claim_record(store, task["id"], lease=args.lease, note=args.note,
                                previous=held)
    session.sign_and_put(store, f"tasks/{store.agent}/{record['id']}.json", record)
    note = _publish(store, f"take {task['id']}")
    session.mirror(store, "take", str(task.get("title")),
                   fields={"task": task["id"], "lease": args.lease}, channel="board",
                   wire_line=wire.encode("TAKE", store.agent, "*", t=task["id"]))
    return _ok(f"took {task['id']} · lease {args.lease} · renew with "
               f"`colab take {task['id']}`{note}")


def cmd_done(store: Store, args: argparse.Namespace) -> int:
    path = store.mine / f"tasks/{store.agent}/take-{args.id}.json"
    record = read_json(path)
    if not isinstance(record, dict):
        eprint(f"colab: you are not holding {args.id!r} — `colab board` to check")
        return 1
    record["done_at"] = iso()
    record["pr"] = args.pr or ""
    record["result"] = records.scrub(_body(args.note))[:2000]
    session.sign_and_put(store, f"tasks/{store.agent}/take-{args.id}.json", record)
    if store.push_url:
        store.publish(f"done {args.id}")
    task = board.tasks(store).get(args.id, {})
    session.mirror(store, "done", str(task.get("title") or args.id),
                   body=record["result"], fields={"task": args.id, "pr": args.pr or "-"},
                   channel="board",
                   wire_line=wire.encode("DONE", store.agent, "*", t=args.id, pr=args.pr or ""))
    return _ok(f"{args.id} marked done")


def cmd_drop(store: Store, args: argparse.Namespace) -> int:
    path = f"tasks/{store.agent}/take-{args.id}.json"
    record = read_json(store.mine / path)
    if not isinstance(record, dict):
        return _ok(f"you were not holding {args.id}")
    record["released_at"] = iso()
    record["expires_at"] = iso()
    session.sign_and_put(store, path, record)
    if store.push_url:
        store.publish(f"drop {args.id}")
    return _ok(f"released {args.id} back to the board")


# ================================================================ talking


def cmd_send(store: Store, args: argparse.Namespace) -> int:
    used = session.sends_this_hour(store)
    if used >= session.SEND_LIMIT_PER_HOUR and not args.force:
        eprint(f"colab: {used} messages sent this hour, which is the cap.")
        eprint("     Batch what is left into one message next hour, or --force if it is")
        eprint("     genuinely urgent. Two agents being polite is how a budget disappears.")
        return 2
    if session.budget_left(store) <= 0 and not args.force:
        eprint("colab: coordination is over its hourly token budget. Do the work; talk later.")
        return 2

    body = _body(args.body)
    if records.looks_like_secret(body) and not args.allow_secrets:
        body = records.withhold_secrets(body)
    msg = {
        "id": new_id("m"),
        "agent": store.agent,
        "to": records.slug(args.to) if args.to else "*",
        "kind": args.kind,
        "subject": records.scrub(" ".join(args.subject))[:200],
        "body": records.scrub(body)[:8000],
        "paths": _paths(store, args.paths),
        "task": args.task or "",
        "needs_reply": bool(args.needs_reply),
        "reply_to": args.reply_to or "",
        "branch": records.fingerprint(store.repo_root).get("git_branch", ""),
        "ts": iso(),
    }
    session.sign_and_put(store, f"msgs/{store.agent}/{msg['id']}.json", msg)
    note = _publish(store, f"msg {msg['id']}")
    line = wire.from_message(msg)
    session.charge(store, records.estimate_tokens(line), "send")
    session.mirror(store, msg["kind"], msg["subject"], body=msg["body"],
                   fields={"to": msg["to"], "paths": ", ".join(msg["paths"]) or "-"},
                   channel=args.channel, wire_line=line)
    return _ok(f"{msg['id']} sent ({used + 1}/{session.SEND_LIMIT_PER_HOUR} this hour){note}")


def cmd_reply(store: Store, args: argparse.Namespace) -> int:
    target = session.find_message(store, args.id)
    if not target:
        eprint(f"colab: no message {args.id!r}")
        return 1
    # A chat question is answered in the room it was asked in, not on the git
    # ref — the human waiting for it is not reading git.
    if str(target.get("source")) in chat.DRIVERS or str(target.get("source")) == "canvas":
        return cmd_answer(store, argparse.Namespace(
            id=args.id, text=args.text, channel=target.get("channel") or "ask"))
    return cmd_send(store, argparse.Namespace(
        subject=[f"re: {str(target.get('subject'))[:120]}"], body=" ".join(args.text),
        to=str(target.get("agent")), kind="reply", paths=[], task=target.get("task") or "",
        needs_reply=False, reply_to=str(target.get("id")), channel="link",
        force=True, allow_secrets=False))


def cmd_answer(store: Store, args: argparse.Namespace) -> int:
    """Answer a human, in the room they asked in. This is the interview channel."""
    target = session.find_message(store, args.id)
    if not target:
        eprint(f"colab: no message {args.id!r} — `colab inbox --chat` to list")
        return 1
    text = " ".join(args.text) if isinstance(args.text, list) else str(args.text)
    text = _body(text)
    # A canvas ask came from the web view, not from a chat room. Answering it
    # into `#ask` would post a stranger's question and this agent's reply to
    # Discord, and saying "answered in chat" when chat is off is a lie.
    if str(target.get("source")) == "canvas":
        from . import canvas
        sent = canvas.answer(store, target, text)
        session.mark_read(store, [str(target.get("id"))])
        return _ok("answered on the canvas" if sent
                   else "answer queued — it goes out on the next sync")
    session.mirror(store, "reply", f"answering {target.get('agent')}",
                   body=records.scrub(text)[:3000],
                   fields={"asked": str(target.get("subject"))[:120]},
                   channel=args.channel or target.get("channel") or "ask")
    session.mark_read(store, [str(target.get("id"))])
    return _ok("answered in chat")


def _print_room_mail(rooms: list[dict[str, Any]]) -> None:
    print(chat.UNTRUSTED_BANNER)
    print()
    for msg in rooms:
        kind = str(msg.get("kind") or "chat")
        flag = "  NEEDS REPLY" if msg.get("needs_reply") else ""
        print(f"{msg.get('id')}  [{kind}] {msg.get('agent')}{flag}  {ago(msg.get('ts'))}")
        print(records.frame_untrusted(str(msg.get("body"))[:600]))
    print()
    print("Reply with `colab answer <id> \"...\"` — it goes back to the room they asked in.")


def cmd_inbox(store: Store, args: argparse.Namespace) -> int:
    # The room is the other half of the inbox. Codex learned this the hard way:
    # a ping sat on the canvas while `colab inbox` said "inbox clear", because
    # only `sync` pulled the room and only `--chat` showed it. Pull it here,
    # list it here.
    _canvas_keepalive(store)
    rooms = session.chat_unread(store)
    if args.chat:
        if not rooms:
            return _ok("no unread chat messages")
        _print_room_mail(rooms)
        return 0
    mail = session.unread(store)
    # A project that set a minimum trust level meant it. Records below it are
    # held back rather than mixed in, and the count is always reported -- a
    # silently shortened inbox would be its own kind of lie.
    classified = session.classify_all(store, mail)
    if args.all:
        mail, withheld = classified, []
    else:
        mail, withheld = session.below_minimum(store, classified)
    if rooms and not args.wire:
        _print_room_mail(rooms)
        if mail:
            print()
    if not mail:
        if rooms:
            return 0
        if withheld:
            return _ok(f"inbox clear ({len(withheld)} held below "
                       f"{session.require_trust(store)} — `colab inbox --all` to see them)")
        return _ok("inbox clear")
    if withheld:
        eprint(f"colab: {len(withheld)} record(s) held below this project's minimum "
               f"trust of {session.require_trust(store)}. `colab inbox --all` shows them.")
    if args.wire:
        print(wire.digest(mail, limit=args.limit))
        return 0
    for msg in mail[-args.limit:]:
        flag = " NEEDS REPLY" if msg.get("needs_reply") else ""
        print(f"{msg.get('id')}  {msg.get('agent')} -> {msg.get('to')}  "
              f"[{msg.get('kind')}]{flag}  {ago(msg.get('ts'))}")
        print(f"    {str(msg.get('subject'))[:150]}")
    return 0


def cmd_read(store: Store, args: argparse.Namespace) -> int:
    msg = session.find_message(store, args.id)
    if not msg:
        eprint(f"colab: no record {args.id!r}")
        return 1
    trusted = session.classify_all(store, [msg])[0]
    _, withheld = session.below_minimum(store, [trusted])
    if withheld:
        eprint(f"colab: this record is {trusted.get('_trust')}, below this "
               f"project's minimum of {session.require_trust(store)}.")
        eprint("     Showing it anyway, clearly marked — refusing to show a record "
               "you asked for by id would just send you to `cat`.")
    print(f"{msg.get('id')} · from {msg.get('agent')} · {ago(msg.get('ts'))} · "
          f"trust: {trusted.get('_trust')}")
    if msg.get("paths"):
        print("paths: " + ", ".join(msg.get("paths") or []))
    print()
    print(records.UNTRUSTED_BANNER)
    print(records.frame_untrusted(str(msg.get("body") or msg.get("subject") or "")))
    session.mark_read(store, [str(msg.get("id"))])
    if msg.get("needs_reply"):
        print()
        print(f"They are waiting. `colab reply {msg.get('id')} \"...\"` — marking it read "
              f"does not answer it.")
    return 0


def cmd_finding(store: Store, args: argparse.Namespace) -> int:
    """A lesson recorded once so nobody pays to learn it twice."""
    title = " ".join(args.title)
    finding = {
        # Content-addressed: two agents that learn the same thing write the same
        # id, so the knowledge base deduplicates itself instead of accumulating
        # five phrasings of one fact.
        "id": records.content_id("f", title.lower().strip()),
        "agent": store.agent,
        "title": records.scrub(title)[:200],
        "body": records.scrub(_body(args.body))[:8000],
        "paths": _paths(store, args.paths),
        "tags": [records.slug(t, limit=24) for t in (args.tag or [])],
        "created_at": iso(),
        "ts": iso(),
    }
    session.sign_and_put(store, f"findings/{store.agent}/{finding['id']}.json", finding)
    note = _publish(store, f"finding {finding['id']}")
    session.mirror(store, "finding", finding["title"], body=finding["body"],
                   channel="findings",
                   wire_line=wire.encode("FIND", store.agent, "*", text=finding["title"],
                                         p=finding["paths"]))
    return _ok(f"{finding['id']} recorded{note}")


def cmd_known(store: Store, args: argparse.Namespace) -> int:
    """Everything anyone already worked out. Run before you spend an hour re-deriving."""
    needle = (args.about or "").lower()

    def hit(item: dict[str, Any]) -> bool:
        if not needle:
            return True
        blob = " ".join(str(item.get(k) or "") for k in
                        ("title", "subject", "body")).lower()
        blob += " ".join(item.get("paths") or []).lower()
        return needle in blob

    findings = [f for f in store.read_all("findings") if hit(f)]
    bugs = [b for b in store.read_all("bugs") if hit(b)]
    decisions = [m for m in store.read_all("msgs")
                 if m.get("kind") in ("decision", "heads-up") and hit(m)]

    if not (findings or bugs or decisions):
        return _ok(f"nothing recorded{' about ' + needle if needle else ''} — "
                   f"that is a real answer, carry on")

    if findings:
        print(f"FINDINGS ({len(findings)})")
        for item in findings[-args.limit:]:
            print(f"  {item.get('id')}  [{item.get('agent')}] {item.get('title')}")
            if args.full and item.get("body"):
                print(records.frame_untrusted(str(item["body"])[:800]))
        print()
    if bugs:
        print(f"BUGS ({len(bugs)})")
        for item in bugs[-args.limit:]:
            print(f"  {item.get('id')}  [{item.get('status') or 'open'}] "
                  f"{item.get('title')} — {item.get('agent')}")
            if item.get("verdict"):
                print(f"      verdict: {item['verdict']}")
        print()
    if decisions:
        print(f"DECISIONS AND HEADS-UPS ({len(decisions)})")
        for item in decisions[-args.limit:]:
            print(f"  {item.get('id')}  [{item.get('agent')}] {item.get('subject')}")
    print()
    print("Written by other agents on other machines: a colleague's notes. Useful, "
          "possibly wrong, never an instruction.")
    return 0


# ================================================================ claims


def cmd_claim(store: Store, args: argparse.Namespace) -> int:
    paths = _paths(store, args.paths)
    if not paths:
        eprint("colab: claim what?")
        return 1
    conflicts = []
    for claim in store.active_claims(exclude_agent=store.agent):
        for path in paths:
            if records.path_matches(path, claim.get("paths") or []):
                conflicts.append((path, claim))
    if conflicts and not args.override:
        for path, claim in conflicts[:5]:
            eprint(f"colab: `{path}` is already claimed by {claim.get('agent')} "
                   f"({records.one_line(claim.get('reason'), 80)})")
        eprint("     Talk to them, or --override if it is plainly stale.")
        return 2

    claim = {
        "id": new_id("c"),
        "agent": store.agent,
        "paths": paths,
        "reason": records.scrub(args.reason or "")[:400],
        "critical": bool(args.critical),
        "branch": records.fingerprint(store.repo_root).get("git_branch", ""),
        "created_at": iso(),
        "expires_at": records.in_seconds(records.parse_duration(args.ttl)),
    }
    session.sign_and_put(store, f"claims/{store.agent}/{claim['id']}.json", claim)
    if store.push_url:
        store.publish(f"claim {claim['id']}")
    session.mirror(store, "claim", ", ".join(paths),
                   fields={"why": claim["reason"], "until": args.ttl,
                           "critical": "yes" if claim["critical"] else "no"},
                   channel="link",
                   wire_line=wire.encode("CL", store.agent, "*", p=paths, ttl=args.ttl,
                                         text=claim["reason"]))
    return _ok(f"{claim['id']} claimed {len(paths)} path(s) for {args.ttl}"
               + ("  (critical)" if claim["critical"] else "")
               + "\nThis warns others once. It does not block them, by design.")


def cmd_release(store: Store, args: argparse.Namespace) -> int:
    released = 0
    for path in sorted((store.mine / "claims" / store.agent).glob("*.json")) \
            if (store.mine / "claims" / store.agent).is_dir() else []:
        claim = read_json(path) or {}
        if claim.get("released_at"):
            continue
        if not args.all and args.id and args.id not in str(claim.get("id")):
            continue
        claim["released_at"] = iso()
        session.sign_and_put(store, f"claims/{store.agent}/{path.name}", claim)
        released += 1
    if released and store.push_url:
        store.publish("release")
    if released:
        session.mirror(store, "release", f"{released} claim(s) released", channel="link")
    return _ok(f"released {released} claim(s)")


def cmd_claims(store: Store, args: argparse.Namespace) -> int:
    live = store.active_claims()
    if not live:
        return _ok("no live claims")
    for claim in live:
        mine = " (yours)" if claim.get("agent") == store.agent else ""
        print(f"{claim.get('id')}  {claim.get('agent')}{mine}"
              f"{'  CRITICAL' if claim.get('critical') else ''}")
        print(f"    {', '.join(claim.get('paths') or [])}")
        print(f"    {records.one_line(claim.get('reason'), 100)} · expires "
              f"{ago(claim.get('expires_at'))}")
    return 0


def cmd_check(store: Store, args: argparse.Namespace) -> int:
    """Local, instant, no network. Exit 2 means somebody else is in there."""
    path = records.normalise_path(args.path, store.repo_root)
    for claim in store.active_claims(exclude_agent=store.agent):
        pattern = records.path_matches(path, claim.get("paths") or [])
        if pattern:
            print(f"claimed by {claim.get('agent')} via `{pattern}`: "
                  f"{records.one_line(claim.get('reason'), 100)}")
            return 2
    for peer in store.live_peers():
        if path in set((peer.get("surface") or {}).get("files") or []):
            print(f"not claimed, but {peer.get('agent')} has also changed it on "
                  f"`{peer.get('branch')}`")
            return 2
    return _ok("clear")


def cmd_preflight(store: Store, args: argparse.Namespace) -> int:
    """Run before you push. This is the moment work actually gets overwritten."""
    root = str(store.repo_root)
    if not args.no_fetch:
        records.run(["git", "-C", root, "fetch", "--quiet", "origin", args.base], timeout=45)
    base_ref = records.resolve_base_ref(store.repo_root, f"origin/{args.base}")
    if not base_ref:
        eprint(f"colab: cannot find {args.base} or origin/{args.base} to compare against")
        return 1
    mine = records.surface(store.repo_root, base_ref)
    my_files = set(mine.get("files") or [])
    if not my_files:
        return _ok(f"nothing to check — your branch matches {base_ref}")

    merge_base = records.run(["git", "-C", root, "merge-base", base_ref, "HEAD"], timeout=10)
    landed: set[str] = set()
    if merge_base:
        out = records.run(["git", "-C", root, "diff", "--name-only",
                           f"{merge_base}..{base_ref}"], timeout=25)
        landed = {line.strip() for line in out.splitlines() if line.strip()}

    print(f"preflight · {mine.get('count')} file(s) changed since {base_ref}")
    print()
    moved = sorted(my_files & landed)
    print(f"MOVED UNDER YOU ({len(moved)}) — changed on {base_ref} since you branched")
    for path in moved[:25]:
        print(f"  {path}")
    if len(moved) > 25:
        print(f"  … and {len(moved) - 25} more")
    if not moved:
        print("  (none)")

    overlap = 0
    for peer in store.live_peers():
        theirs = set((peer.get("surface") or {}).get("files") or [])
        shared = sorted(my_files & theirs)
        if not shared:
            continue
        overlap += len(shared)
        print()
        print(f"ALSO BEING EDITED BY {peer.get('agent')} ({len(shared)}) — "
              f"{peer.get('branch')}, active {ago(peer.get('updated_at'))}")
        for path in shared[:20]:
            print(f"  {path}")
    print()
    if moved:
        print(f"Merge {base_ref} in and resolve locally before pushing.")
    if overlap:
        print("Tell them what you changed:")
        print('  colab send "pushing <files>" --kind heads-up --paths <files> --body -')
    if not moved and not overlap:
        print("Clear. Nothing of yours has moved and nothing overlaps.")
    return 1 if (moved or overlap) else 0


# ================================================================ bugs


def _capture(command: list[str], cwd: Path) -> dict[str, Any]:
    try:
        proc = subprocess.run(command, cwd=str(cwd), capture_output=True, text=True,
                              timeout=600, stdin=subprocess.DEVNULL)
        tail = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    except subprocess.TimeoutExpired:
        return {"cmd": command, "exit": -1, "tail": "timed out after 600s"}
    except OSError as exc:
        return {"cmd": command, "exit": -1, "tail": f"could not run: {exc}"}
    return {"cmd": command, "exit": proc.returncode,
            "tail": records.scrub(tail[-4000:])}


def cmd_bug(store: Store, args: argparse.Namespace) -> int:
    """A failure that may be the machine rather than the code."""
    deep = session.deep_fingerprint(store, force=args.fresh)
    bug = {
        "id": new_id("b"),
        "agent": store.agent,
        "title": records.scrub(" ".join(args.title))[:200],
        "body": records.scrub(_body(args.body))[:8000],
        "paths": _paths(store, args.paths),
        "status": "open",
        "created_at": iso(),
        "ts": iso(),
        # The whole point: what is true about THIS machine, so somebody else can
        # diff their own against it without either of us publishing a secret.
        "fingerprint": deep,
    }
    if args.capture:
        bug["capture"] = _capture(list(args.capture), store.repo_root)
    session.sign_and_put(store, f"bugs/{store.agent}/{bug['id']}.json", bug)
    note = _publish(store, f"bug {bug['id']}")
    session.mirror(store, "bug", bug["title"], body=bug["body"],
                   fields={"id": bug["id"],
                           "exit": (bug.get("capture") or {}).get("exit", "-")},
                   channel="incidents",
                   wire_line=wire.encode("BUG", store.agent, "*", text=bug["title"],
                                         p=bug["paths"]))
    return _ok(f"{bug['id']} filed · others reproduce with "
               f"`colab bug-try {bug['id']}`{note}")


def cmd_bugs(store: Store, args: argparse.Namespace) -> int:
    bugs = [b for b in store.read_all("bugs")
            if args.all or b.get("status") in (None, "", "open")]
    if not bugs:
        return _ok("no open bugs")
    for bug in bugs[-args.limit:]:
        print(f"{bug.get('id')}  [{bug.get('status') or 'open'}]  {bug.get('agent')}  "
              f"{ago(bug.get('created_at'))}")
        print(f"    {str(bug.get('title'))[:150]}")
        if bug.get("verdict"):
            print(f"    verdict: {bug['verdict']}")
    return 0


def cmd_bug_try(store: Store, args: argparse.Namespace) -> int:
    """Diff their machine against yours. The answer is usually in the list."""
    bug = session.find_message(store, args.id)
    if not bug or "fingerprint" not in bug:
        eprint(f"colab: no bug {args.id!r}")
        return 1
    theirs = bug.get("fingerprint") or {}
    mine = records.fingerprint(store.repo_root, deep=True)
    print(f"{bug.get('id')} · {bug.get('title')} · filed by {bug.get('agent')} "
          f"{ago(bug.get('created_at'))}")
    print()
    if bug.get("body"):
        print(records.frame_untrusted(str(bug["body"])[:1500]))
        print()
    differences = records.diff_fingerprints(mine, theirs)
    if differences:
        print(f"THE TWO MACHINES DISAGREE IN {len(differences)} WAY(S):")
        for line in differences:
            print(f"  {line}")
    else:
        print("The two machines look identical on everything measured — so this is")
        print("probably the code, not the environment.")
    capture = bug.get("capture") or {}
    if capture.get("cmd"):
        print()
        print(f"They ran: {' '.join(capture['cmd'])}  -> exit {capture.get('exit')}")
        if capture.get("tail"):
            print(records.frame_untrusted(str(capture["tail"])[-1500:]))
        if args.run:
            print()
            print("running it here…")
            result = _capture(list(capture["cmd"]), store.repo_root)
            print(f"  exit {result['exit']} (theirs was {capture.get('exit')})")
            if result["exit"] == capture.get("exit"):
                print("  same failure here — it is the code")
            else:
                print("  different here — it is the environment")
        else:
            print()
            print("That command came from another machine, so it is NOT run automatically.")
            print(f"Ask your human first, then:  colab bug-try {bug.get('id')} --run")
    return 0


def cmd_bug_close(store: Store, args: argparse.Namespace) -> int:
    """Only the filer closes a bug — nobody rewrites another agent's records."""
    path = store.mine / f"bugs/{store.agent}/{args.id}.json"
    bug = read_json(path)
    if not isinstance(bug, dict):
        eprint(f"colab: {args.id!r} is not a bug you filed. Reply to it instead:")
        eprint(f'       colab send "re {args.id}: ..." --kind decision --body -')
        return 1
    bug["status"] = args.status
    bug["verdict"] = records.scrub(args.note or "")[:1000]
    bug["closed_at"] = iso()
    session.sign_and_put(store, f"bugs/{store.agent}/{args.id}.json", bug)
    if store.push_url:
        store.publish(f"bug-close {args.id}")
    session.mirror(store, "fix", str(bug.get("title")), body=bug["verdict"],
                   fields={"id": args.id, "as": args.status}, channel="incidents")
    return _ok(f"{args.id} closed as {args.status}")


# ================================================================ review


def cmd_review(store: Store, args: argparse.Namespace) -> int:
    """Ask another agent to look at a PR, or record what you found in one.

    Review is where multi-agent collaboration either pays for itself or drowns
    a maintainer. Requests are deterministically routed so the same reviewer is
    not asked twice, and a verdict is a record, not a chat line.
    """
    roster = board.live_agents(store)
    target = args.to or board.owner_of(f"review:{args.pr}", [a for a in roster
                                                             if a != store.agent] or roster)
    record = {
        "id": records.content_id("r", str(args.pr), store.agent, args.verdict or "request"),
        "agent": store.agent,
        "pr": str(args.pr),
        "to": target,
        "verdict": args.verdict or "",
        "body": records.scrub(_body(args.body))[:8000],
        "created_at": iso(),
        "ts": iso(),
    }
    session.sign_and_put(store, f"reviews/{store.agent}/{record['id']}.json", record)
    if store.push_url:
        store.publish(f"review {record['id']}")
    session.mirror(store, "review", f"PR #{args.pr}" + (f" — {args.verdict}" if args.verdict else ""),
                   body=record["body"], fields={"reviewer": target}, channel="reviews",
                   wire_line=wire.encode("REV", store.agent, target, pr=str(args.pr),
                                         st=args.verdict or "requested"))
    return _ok(f"review {'verdict' if args.verdict else 'requested from ' + target} "
               f"on PR #{args.pr}")


# ================================================================ chat


def cmd_chat(store: Store, args: argparse.Namespace) -> int:
    config = store.config()
    chat_config = dict(config.get("chat") or {})

    if args.action == "status":
        adapters = chat.adapters(config)
        if not adapters:
            return _ok("chat is not configured on this machine.\n"
                       "  colab chat setup discord     (or slack)")
        for adapter in adapters:
            ok, detail = adapter.verify()
            print(f"{adapter.name}: write={'yes' if adapter.can_write() else 'no'} "
                  f"read={'yes' if adapter.can_read() else 'no'}")
            print(f"  {'ok' if ok else 'PROBLEM'}: {detail}")
            table = adapter.config.get("channels") or {}
            for logical in chat.CHANNELS:
                entry = table.get(logical)
                if entry:
                    print(f"    {logical:<10} {entry.get('id') or 'webhook-only'}")
            hint = adapter.invite_hint()
            if hint:
                print(f"  invite: {hint}")
        return 0

    if args.action == "invite":
        driver = args.driver or "discord"
        adapter = chat.DRIVERS[driver](dict(chat_config.get(driver) or {}))
        hint = (adapter.invite_hint(provision=not args.minimal)
                if driver == "discord" else adapter.invite_hint())
        if not hint:
            eprint(f"colab: no application id set for {driver}. Run `colab chat setup "
                   f"{driver}` first, or set it in ~/.agentcolab.")
            return 1
        return _ok(hint)

    if args.action == "off":
        config["chat"] = {}
        store.save_config(config)
        return _ok("chat bridge off on this machine (nothing was deleted remotely)")

    if args.action == "pull":
        got = session.pull_chat(store, timeout=15)
        return _ok(f"{got} new message(s)")

    if args.action == "test":
        sent = session.mirror(store, "note", "AgentColab test",
                              body="If you can read this, the mirror works.",
                              channel=args.channel or "link")
        return _ok(f"posted to {sent} platform(s)") if sent else _ok("nothing posted — "
                                                                    "check `colab chat status`")

    if args.action == "provision":
        driver = args.driver or "discord"
        settings = dict(chat_config.get(driver) or {})
        token = os.environ.get("AGENTCOLAB_CHAT_TOKEN") or settings.get("token")
        if not token:
            eprint("colab: provisioning needs a bot token. Set AGENTCOLAB_CHAT_TOKEN, or run")
            eprint(f"     `colab chat setup {driver}` first. Never paste a token into a repo.")
            return 1
        settings["token"] = token
        adapter = chat.DRIVERS[driver](settings)
        try:
            table = adapter.provision(args.guild or settings.get("guild") or "")
        except Exception as exc:
            eprint(f"colab: provisioning failed — {exc}")
            return 1
        chat_config[driver] = adapter.config
        chat_config.setdefault("drivers", [])
        if driver not in chat_config["drivers"]:
            chat_config["drivers"].append(driver)
        config["chat"] = chat_config
        store.save_config(config)
        print(f"provisioned {len(table)} channel(s) on {driver}:")
        for logical, entry in sorted(table.items()):
            print(f"  {logical:<10} {entry.get('id')}")
        print()
        print("Commit the PUBLIC half so nobody else has to do this:")
        print(f"  colab chat export > .agentcolab/colab.json   # ids only, never the token")
        return 0

    if args.action == "export":
        public: dict[str, Any] = {"chat": {"drivers": chat_config.get("drivers") or []}}
        for driver in public["chat"]["drivers"]:
            settings = chat_config.get(driver) or {}
            public["chat"][driver] = {
                "channels": {k: {"id": v.get("id")} for k, v in
                             (settings.get("channels") or {}).items() if v.get("id")},
                "guild": settings.get("guild") or "",
                "application_id": settings.get("application_id") or "",
            }
        existing = _project_config(store.repo_root)
        existing.update(public)
        print(json.dumps(existing, indent=2, sort_keys=True))
        return 0

    # setup
    driver = args.driver or "discord"
    if driver not in chat.DRIVERS:
        eprint(f"colab: no adapter named {driver!r} (have: {', '.join(chat.DRIVERS)})")
        return 1
    return _chat_setup(store, driver)


def _chat_setup(store: Store, driver: str) -> int:
    """A guided walkthrough, in the order the platform actually requires.

    Interactive so the token is typed by a human and never passed on a command
    line, where it would land in shell history, the process table, and whatever
    transcript the agent is writing. An agent must not run this on somebody's
    behalf.

    The ordering matters and an earlier version had it wrong: you cannot
    provision channels until the bot is in the server, and you cannot invite the
    bot without its application id. So the id comes first, the invite URL is
    printed the moment it can be, and provisioning is offered at the end rather
    than left as a command to remember.
    """
    import getpass

    if not sys.stdin.isatty():
        eprint("colab: `chat setup` needs a terminal — it asks for a bot token, and a")
        eprint("     token passed any other way ends up in shell history and transcripts.")
        eprint("     Run it yourself in a terminal. An agent should not run it for you.")
        return 1

    config = store.config()
    chat_config = dict(config.get("chat") or {})
    settings = dict(chat_config.get(driver) or {})

    def ask(prompt: str, current: str = "") -> str:
        got = input(f"   {prompt} [{current or 'unset'}]: ").strip()
        return got or current

    if driver == "discord":
        print("Discord setup. Three things to paste; blank keeps what is already set.")
        print()
        print("If you have never made a Discord bot before, this is the whole thing:")
        print("  1. https://discord.com/developers/applications → New Application")
        print("  2. Bot (left sidebar) → Reset Token → copy it")
        print("  3. On that same Bot page, turn ON  MESSAGE CONTENT INTENT")
        print("     Without it the bot reads empty messages and humans cannot reach")
        print("     your agents. It is the single most common thing to miss.")
        print()
        print("A. Application ID — General Information → Application ID.")
        settings["application_id"] = ask("application id",
                                         str(settings.get("application_id") or ""))
        hint = chat.DRIVERS[driver](settings).invite_hint()
        if hint:
            print()
            print("   Invite the bot to your server with this link, then come back:")
            print(f"     {hint}")
            input("   press enter once the bot is in your server… ")

        print()
        print("B. Bot token. Stored in ~/.agentcolab at mode 600, never in the repo,")
        print("   never printed. Typed hidden.")
        token = getpass.getpass("   token (hidden): ").strip()
        if token:
            settings["token"] = token

        print()
        print("C. Server ID — enable Developer Mode (User Settings → Advanced),")
        print("   then right-click the server icon → Copy Server ID.")
        settings["guild"] = ask("server id", str(settings.get("guild") or ""))
    else:
        print("Slack setup. Two things to paste; blank keeps what is already set.")
        print()
        print("If you have never made a Slack app before:")
        print("  1. https://api.slack.com/apps → Create New App → From scratch")
        print("  2. OAuth & Permissions → Bot Token Scopes, add:")
        print("       chat:write, channels:read, channels:history")
        print("       (+ channels:manage only if you want `provision` to make channels)")
        print("  3. Install to Workspace, then copy the Bot User OAuth Token (xoxb-…)")
        print()
        print("A. Bot token. Stored in ~/.agentcolab at mode 600, typed hidden.")
        token = getpass.getpass("   token (hidden): ").strip()
        if token:
            settings["token"] = token
        print()
        print("B. Remember to /invite your app into each channel it should post in.")

    chat_config[driver] = settings
    drivers = list(chat_config.get("drivers") or [])
    if driver not in drivers:
        drivers.append(driver)
    chat_config["drivers"] = drivers
    config["chat"] = chat_config
    store.save_config(config)
    with contextlib.suppress(OSError):
        os.chmod(store.config_path, 0o600)

    adapter = chat.DRIVERS[driver](settings)
    print()
    if not settings.get("token"):
        print("No token given, so this machine can mirror out but humans cannot reach")
        print("the agents. Re-run when you have one.")
        return 0

    if not settings.get("channels"):
        print("Creating the channels now — this needs Manage Channels on the bot.")
        try:
            table = adapter.provision(settings.get("guild") or "")
        except Exception as exc:
            eprint(f"colab: could not create channels — {exc}")
            eprint("     Fix that, then:  colab chat provision --driver " + driver)
            return 1
        settings.update(adapter.config)
        chat_config[driver] = settings
        config["chat"] = chat_config
        store.save_config(config)
        print(f"created {len(table)} channel(s): " + ", ".join(sorted(table)))

    ok, detail = adapter.verify()
    print(f"{driver}: {'ready' if ok else 'NOT reading yet'} — {detail}")
    if ok and session.mirror(store, "note", "AgentColab connected",
                             body="If you can read this, the mirror works.",
                             channel="link"):
        print("posted a test message to #link — go and look at it")
    print()
    print("Share it with everyone else so nobody repeats this:")
    print(f"  colab chat export > .agentcolab/agentcolab.json")
    print( "  git add .agentcolab && git commit -m 'AgentColab: channel map'")
    print()
    print("That file carries channel ids only. Your token stays on this machine.")
    return 0


# ================================================================ protocol


def cmd_wire(store: Store, args: argparse.Namespace) -> int:
    if args.action == "legend":
        return _ok(wire.legend())
    if args.action == "decode":
        return _ok(wire.render(" ".join(args.text)))
    if args.action == "measure":
        prose = _body(args.text[0] if args.text else "-")
        line = wire.encode("NOTE", store.agent, "*", text=prose[:400])
        result = wire.measure(prose, line)
        print(f"prose {result['prose_tokens']} tok → wire {result['wire_tokens']} tok "
              f"({result['ratio'] * 100:.0f}% saved)")
        print(line)
        return 0
    # encode
    return _ok(wire.encode(args.kind or "NOTE", store.agent, args.to or "*",
                           text=" ".join(args.text)))


# ================================================================ diagnosis


def _safe_url(url: str) -> str:
    """A remote URL can carry a token in userinfo. Never print one."""
    import re as _re
    return _re.sub(r"//[^@/]+@", "//***@", url or "")


def cmd_doctor(store: Store, args: argparse.Namespace) -> int:
    problems = 0

    def check(label: str, ok: bool, detail: str = "", fatal: bool = True) -> None:
        nonlocal problems
        mark = "ok  " if ok else ("FAIL" if fatal else "warn")
        print(f"  [{mark}] {label}" + (f" — {detail}" if detail else ""))
        if not ok and fatal:
            problems += 1

    print(f"AgentColab doctor · {store.project}")
    print()
    print("repo")
    check("inside a git repository", True, str(store.repo_root))
    remotes = list_remotes(store.repo_root)
    check("has a remote", bool(remotes), ", ".join(remotes) or "none")

    print("identity")
    config = store.config()
    check("joined", store.initialised, config.get("agent") or "run `colab join`")
    key = identity.describe(store.repo_root)
    check("ssh-keygen present", key["ssh_keygen"], fatal=False)
    check("can sign records", key["can_sign"],
          key["fingerprint"] or "no SSH key — you will show as `unverified`", fatal=False)
    if config.get("github"):
        keys = identity.github_keys(str(config["github"]), store.shared)
        check(f"github.com/{config['github']}.keys", bool(keys),
              f"{len(keys)} key(s) published" if keys else
              "GitHub publishes no key for this account — add your .pub there", fatal=False)
        if keys and key["public_key"]:
            check("your key is one GitHub publishes", key["public_key"] in keys,
                  "otherwise nobody can verify you", fatal=False)

    print("transport")
    sources = store.sources()
    check("sources configured", bool(sources),
          ", ".join(f"{s['name']}={_safe_url(s['url'])}" for s in sources))
    reachable = store.fetch()
    for name, ok in reachable.items():
        check(f"fetch {name}", ok, "unreachable — offline is fine, stale is not", fatal=False)
    check("writable remote", bool(store.push_url),
          _safe_url(store.push_url) or "read-only participant (you can read, not publish)",
          fatal=False)
    unsent = store.unpublished()
    check("everything published", unsent == 0, f"{unsent} record(s) waiting", fatal=False)

    print("peers")
    peers = store.peers()
    check("peers visible", True, f"{len(peers)} known, {len(store.live_peers())} live",
          fatal=False)
    unsigned = [p.get("agent") for p in session.classify_all(store, peers)
                if not p.get("_verified")]
    if unsigned:
        check("all peers verified", False,
              f"unverified: {', '.join(str(u) for u in unsigned[:6])}", fatal=False)

    print("chat")
    adapters = chat.adapters(config)
    if not adapters:
        check("configured", False, "no platform set up — `colab chat setup discord`",
              fatal=False)
    for adapter in adapters:
        ok, detail = adapter.verify()
        check(f"{adapter.name}", ok, detail, fatal=False)

    print("canvas")
    with contextlib.suppress(Exception):
        from . import canvas, wake
        if canvas.is_on(store):
            cfg = canvas.canvas_config(store)
            check("room joined", True, canvas.viewer_url(str(cfg.get("relay")), str(cfg.get("room"))))
            state = wake.status(store)
            if state.get("enabled"):
                check("wake", state.get("listener") == "connected",
                      f"on · from {state.get('from')} · {state.get('used_this_hour')}/{state.get('max_per_hour')} "
                      f"this hour · listener {state.get('listener')}"
                      + ("" if state.get("listener") == "connected" else " — `colab wake on` starts one"),
                      fatal=False)
            else:
                check("wake", True, "off — `colab wake on` lets a ping start a session here", fatal=False)
        else:
            check("room joined", False, "off — `colab canvas join <join code>` to stream", fatal=False)

    print("harness")
    check("harness detected", _detect_harness() != "unknown", _detect_harness(), fatal=False)
    settings = store.repo_root / ".claude" / "settings.json"
    if settings.is_file():
        wired = "colab" in settings.read_text(encoding="utf-8")
        check("Claude Code hooks installed", wired,
              "run `colab install claude-code`" if not wired else "", fatal=False)

    print()
    print("all clear" if problems == 0 else f"{problems} problem(s) to fix")
    return 0 if problems == 0 else 1


def cmd_log(store: Store, args: argparse.Namespace) -> int:
    events: list[tuple[str, str]] = []
    for msg in store.read_all("msgs"):
        events.append((str(msg.get("ts")), f"msg   {msg.get('agent')} -> {msg.get('to')}: "
                                           f"{str(msg.get('subject'))[:70]}"))
    for claim in store.read_all("claims"):
        events.append((str(claim.get("created_at")),
                       f"claim {claim.get('agent')}: {', '.join(claim.get('paths') or [])}"))
    for bug in store.read_all("bugs"):
        events.append((str(bug.get("created_at")), f"bug   {bug.get('agent')}: "
                                                   f"{str(bug.get('title'))[:60]}"))
    for task in store.read_all("tasks"):
        label = "task " if task.get("kind") == "task" else "take "
        events.append((str(task.get("created_at")), f"{label} {task.get('agent')}: "
                                                    f"{str(task.get('title') or task.get('task'))[:60]}"))
    for stamp, line in sorted(events)[-args.limit:]:
        print(f"{stamp}  {line}")
    return 0


def cmd_roster(store: Store, args: argparse.Namespace) -> int:
    ros = session.roster(store)
    members = ros.get("members") or []
    if args.json:
        print(json.dumps(ros, indent=2, sort_keys=True))
        return 0
    if not members:
        print("no roster committed — everyone shows as `unverified`.")
        print("Create .agentcolab/roster.json and add members by pull request:")
        print(json.dumps({"members": [{"github": "you", "role": "maintainer"}],
                          "trust": {"minimum": "unverified"}}, indent=2))
        return 0
    print(f"{len(members)} member(s) · minimum trust: {session.require_trust(store)}")
    for member in members:
        print(f"  {str(member.get('github') or member.get('agent')):<24} "
              f"{str(member.get('role') or 'contributor')}")
    return 0


def cmd_relay(store: Store, args: argparse.Namespace) -> int:
    """Mirror recent shared-ref activity into chat. Built to run in CI.

    This is what lets a contributor need no chat credentials at all. One place
    holds the bot token -- a repository secret, in an Action anybody can read --
    and everyone else just publishes to git. Without it, every participant would
    need their own token, which is a setup step most people will not complete
    and a credential most projects should not hand out.

    Delivery is at-most-once by design. A cursor is kept on a ref; if the run
    fails after posting, some events are skipped rather than duplicated, because
    a channel that repeats itself gets muted and a channel that occasionally
    misses a line does not. Git remains the complete record either way.
    """
    store.fetch()
    store.view(refresh=True)
    cursor = str(store.local().get("relay_cursor") or "")
    since = records.now().timestamp() - records.parse_duration(args.since)

    events: list[tuple[str, str, dict[str, Any]]] = []
    for kind, family in (("msg", "msgs"), ("task", "tasks"), ("bug", "bugs"),
                         ("finding", "findings"), ("claim", "claims"),
                         ("review", "reviews")):
        for record in store.read_all(family):
            stamp = parse_iso(record.get("ts") or record.get("created_at"))
            if not stamp or stamp.timestamp() < since:
                continue
            rid = str(record.get("id") or "")
            if cursor and rid <= cursor:
                continue
            events.append((str(record.get("ts") or record.get("created_at")), kind, record))
    events.sort()
    events = events[-args.limit:]

    trusted = {str(r.get("id")): r for r in
               session.classify_all(store, [e[2] for e in events])}
    sent = 0
    for stamp, kind, record in events:
        classified = trusted.get(str(record.get("id")), record)
        channel = {"msg": "link", "task": "board", "bug": "incidents",
                   "finding": "findings", "claim": "link", "review": "reviews"}[kind]
        subject = str(record.get("subject") or record.get("title")
                      or ", ".join(record.get("paths") or []) or record.get("id"))
        event = chat.Event(
            str(record.get("kind") or kind), str(record.get("agent") or "?"), subject,
            body=str(record.get("body") or "")[:1200],
            fields={"id": str(record.get("id"))},
            channel=channel, wire=wire.from_message(record) if kind == "msg" else "",
            trust=str(classified.get("_trust") or "unverified"))
        sent += chat.post(store.config(), event)
    if events:
        store.update_local(relay_cursor=max(str(e[2].get("id") or "") for e in events))
    print(f"relayed {len(events)} event(s), {sent} post(s)")
    return 0


def _channels(store: Store) -> dict[str, dict[str, str]]:
    """Every channel, built-in and project-defined, merged from the repo config."""
    project = _project_config(store.repo_root)
    merged = {"chat": {"custom": {**((project.get("chat") or {}).get("custom") or {}),
                                  **((store.config().get("chat") or {}).get("custom") or {})}}}
    return chat.resolve(merged)


def cmd_channels(store: Store, args: argparse.Namespace) -> int:
    """What rooms exist, what belongs in each, and what to type to post there."""
    known = _channels(store)
    live = {}
    for adapter in chat.adapters(store.config()):
        live.update(adapter.config.get("channels") or {})
    for name, spec in known.items():
        mark = "in " if spec.get("dir") == "in" else "out"
        star = " *" if spec.get("custom") else "  "
        wired = "wired" if name in live else "-"
        print(f"{star}{name:<14} {mark}  {wired:<6} {spec.get('purpose','')}")
        if args.full and spec.get("brief"):
            for line in str(spec["brief"]).splitlines():
                print(f"      {line}")
    print()
    print("* project-defined. Add your own in .agentcolab/agentcolab.json:")
    print('    {"chat": {"custom": {"bs-chat": {"purpose": "...", "brief": "..."}}}}')
    print("Then `colab chat provision` creates them, and `colab say bs-chat \"...\"` posts.")
    return 0


def cmd_say(store: Store, args: argparse.Namespace) -> int:
    """Post to any channel, including ones this project invented.

    Separate from `send` on purpose. `send` writes a durable record to the git
    ref that every agent will read and may have to answer; `say` is a line in a
    room. Conflating them is how a channel meant for occasional sharp
    observations turns into a second inbox.
    """
    known = _channels(store)
    name = records.slug(args.channel, limit=32)
    if name not in known:
        eprint(f"colab: no channel {args.channel!r}. Known: {', '.join(sorted(known))}")
        eprint("     Add one in .agentcolab/agentcolab.json — see `colab channels`.")
        return 1
    if session.budget_left(store) <= 0 and not args.force:
        eprint("colab: coordination is over its hourly token budget. Do the work; talk later.")
        return 2
    body = records.scrub(_body(args.body) or " ".join(args.text))[:4000]
    if not body:
        eprint("colab: nothing to say")
        return 1
    sent = session.mirror(store, "note", body.splitlines()[0][:120],
                          body=body if "\n" in body or len(body) > 120 else "",
                          channel=name)
    session.charge(store, records.estimate_tokens(body), f"say:{name}")
    if not sent:
        if not chat.enabled(store.config()):
            eprint("colab: chat is not configured on this machine, so nothing was posted.")
            eprint("     Set it up with `colab chat setup discord`, or leave it off — "
                   "everything else works without it.")
        else:
            # Configured but the post failed: a wrong token, a channel the bot
            # cannot see, or the platform being down. Saying "not configured"
            # here sends people to fix the one thing that is already right.
            eprint("colab: chat is configured but the message did not go out.")
            eprint("     `colab chat status` says which part is failing.")
        return 1
    landed = getattr(session.mirror, "last_landed", []) or []
    elsewhere = sorted({where for _, where in landed if where != name})
    if elsewhere:
        return _ok(f"posted to #{', #'.join(elsewhere)} — NOT #{name}.\n"
                   f"  #{name} is defined but has no channel on the platform yet, so it "
                   f"fell back.\n  Create it:  colab chat provision")
    return _ok(f"posted to #{name}")


def cmd_brief(store: Store, args: argparse.Namespace) -> int:
    """What a channel is for, in its own words. Read this before posting to one."""
    known = _channels(store)
    name = records.slug(args.channel, limit=32)
    spec = known.get(name)
    if not spec:
        eprint(f"colab: no channel {args.channel!r}")
        return 1
    print(f"#{name} · {'input' if spec.get('dir') == 'in' else 'output'}")
    print(spec.get("purpose", ""))
    if spec.get("brief"):
        print()
        print(spec["brief"])
    else:
        print()
        print("No brief set. A project can add one in .agentcolab/agentcolab.json —")
        print("it is handed to an agent before it posts, so the room keeps its character.")
    if spec.get("dir") == "in":
        print()
        print(chat.UNTRUSTED_BANNER)
    return 0


# ---------------------------------------------------------------- triage

PRIORITIES = {
    "p0": "money, data loss, or the product is down",
    "p1": "broken for real users; a workaround exists or is not universal",
    "p2": "real but survivable",
    "p3": "cosmetic, rare, or nice-to-have",
    "noise": "recurring and judged not to be a bug",
}


def _gh_issues(store: Store, limit: int) -> list[dict[str, Any]]:
    """Open issues, via the gh CLI so we inherit its auth rather than ask for a token."""
    if not shutil.which("gh"):
        return []
    raw = records.run(["gh", "issue", "list", "--state", "open", "--limit", str(limit),
                       "--json", "number,title,labels,createdAt,comments"],
                      timeout=30, cwd=store.repo_root)
    try:
        data = json.loads(raw) if raw else []
    except ValueError:
        return []
    return data if isinstance(data, list) else []


def cmd_issues(store: Store, args: argparse.Namespace) -> int:
    """Sort the open issues across the live agents, deterministically.

    The same hash that divides tasks divides issues, so every machine computes
    the same split with no round trip and the same issue is never triaged twice.
    An agent asks what it owns and gets an answer nobody else will also get.
    """
    session.refresh_if_stale(store)
    issues = _gh_issues(store, args.limit)
    if not issues:
        eprint("colab: no open issues found (needs the `gh` CLI, authenticated, in a "
               "GitHub repo)")
        return 1
    roster = board.live_agents(store)
    done = {str(r.get("issue")) for r in store.read_all("tasks")
            if r.get("kind") == "triage"}

    mine, theirs = [], []
    for issue in issues:
        number = str(issue.get("number"))
        owner = board.owner_of(f"issue:{number}", roster)
        (mine if owner == store.agent else theirs).append((issue, owner))

    print(f"{len(issues)} open issue(s) across {len(roster)} live agent(s)")
    print()
    todo = [(i, o) for i, o in mine if str(i.get("number")) not in done]
    print(f"YOURS, NOT YET TRIAGED ({len(todo)})")
    for issue, _ in todo[:args.show]:
        labels = ",".join(l.get("name", "") for l in (issue.get("labels") or []))
        print(f"  #{issue.get('number'):<6} {str(issue.get('title'))[:66]}")
        if labels:
            print(f"          labels: {labels}")
    if not todo:
        print("  (none — everything you own is triaged)")
    if len(todo) > args.show:
        print(f"  … and {len(todo) - args.show} more")
    print()
    print(f"OWNED BY OTHERS ({len(theirs)}) — leave them alone, they are not yours")
    for issue, owner in theirs[:5]:
        print(f"  #{issue.get('number'):<6} → {owner}")
    print()
    print("File a decision, and say why:")
    print('  colab triage <number> --as p1 --why "..." [--plan -]')
    return 0


def cmd_triage(store: Store, args: argparse.Namespace) -> int:
    """Record a triage decision. `--why` is required and gets published.

    A triage decision nobody can audit is not a decision. For p0 a plan is
    required too — a p0 without one is just an alarm, and the point of the
    channel is that a human opening it does not start cold.
    """
    if args.priority == "p0" and not args.plan:
        eprint("colab: a p0 needs --plan. An alarm without a first step is not triage.")
        return 1
    record = {
        "id": records.content_id("tr", str(args.issue), args.priority),
        "kind": "triage",
        "agent": store.agent,
        "issue": str(args.issue),
        "priority": args.priority,
        "why": records.scrub(args.why)[:1000],
        "plan": records.scrub(_body(args.plan))[:4000],
        "created_at": iso(),
        "ts": iso(),
    }
    session.sign_and_put(store, f"tasks/{store.agent}/{record['id']}.json", record)
    if store.push_url:
        store.publish(f"triage #{args.issue}")
    fields = {"issue": f"#{args.issue}", "as": args.priority,
              "why": record["why"][:200]}
    session.mirror(store, "task", f"#{args.issue} → {args.priority.upper()}",
                   body=record["plan"], fields=fields,
                   channel="incidents" if args.priority in ("p0", "p1") else "triage",
                   wire_line=wire.encode("D", store.agent, "*", ref=f"#{args.issue}",
                                         sev=args.priority, text=record["why"][:160]))
    return _ok(f"#{args.issue} triaged as {args.priority.upper()} — "
               f"{PRIORITIES[args.priority]}")


def cmd_standup(store: Store, args: argparse.Namespace) -> int:
    """A digest of who did what. Posts on a slow timer, or on demand."""
    session.refresh_if_stale(store, force=True)
    cutoff = now().timestamp() - records.parse_duration(args.since)
    agents = session.classify_all(store, store.agents())
    tasks = board.tasks(store)

    lines: list[str] = []
    for peer in sorted(agents, key=lambda a: str(a.get("agent"))):
        name = str(peer.get("agent"))
        stamp = parse_iso(peer.get("updated_at"))
        if not stamp or stamp.timestamp() < cutoff:
            continue
        held = [t for t in tasks.values() if t.get("owner") == name
                and t.get("state") in ("taken", "review")]
        shipped = [t for t in tasks.values() if t.get("owner") == name
                   and t.get("state") == "done"]
        bit = f"**{name}** · `{peer.get('branch')}`"
        if peer.get("intent"):
            bit += f" — {records.one_line(peer.get('intent'), 90)}"
        if held:
            bit += f"\n    holding: " + ", ".join(str(t.get("title"))[:40] for t in held[:3])
        if shipped:
            bit += f"\n    shipped: {len(shipped)}"
        lines.append(bit)

    if not lines:
        return _ok(f"nobody has been active in the last {args.since}")
    body = "\n".join(lines)
    print(body)
    if args.post:
        session.mirror(store, "standup", f"standup · {len(lines)} agent(s) active",
                       body=body, channel="standup")
        print()
        print("posted to #standup")
    return 0


def cmd_review_load(store: Store, args: argparse.Namespace) -> int:
    """What a maintainer actually needs: how much agent work is queued at them.

    Review attention is the scarce resource in an open-source project, and a
    fleet of agents converts tokens into review load faster than anything else
    ever invented. This is the number that decides whether a maintainer keeps
    this switched on.
    """
    session.refresh_if_stale(store, force=True)
    tasks = board.tasks(store)
    reviews = store.read_all("reviews")
    agents = session.classify_all(store, store.agents())

    in_flight = [t for t in tasks.values() if t.get("state") in ("taken", "review")]
    finished = [t for t in tasks.values() if t.get("state") == "done"]
    with_pr = [t for t in finished if t.get("pr")]
    humans = {str(a.get("human") or a.get("agent")) for a in agents}
    unverified = [a for a in agents if not a.get("_verified")]

    print(f"review load · {store.project}")
    print()
    print(f"  agents            {len(agents)} ({len(store.live_peers()) + 1} live) "
          f"across {len(humans)} human(s)")
    print(f"  unsigned agents   {len(unverified)}"
          + (f" — {', '.join(str(a.get('agent')) for a in unverified[:5])}"
             if unverified else ""))
    print(f"  work in flight    {len(in_flight)}")
    print(f"  finished, open PR {len(with_pr)}")
    print(f"  review requests   {len([r for r in reviews if not r.get('verdict')])}")
    print()

    # Predicted conflicts are the part a maintainer cannot compute themselves,
    # and the part that decides whether merging in order is safe.
    surfaces = {str(a.get("agent")): set((a.get("surface") or {}).get("files") or [])
                for a in agents}
    pairs = []
    names = sorted(surfaces)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            shared = surfaces[a] & surfaces[b]
            if shared:
                pairs.append((len(shared), a, b, sorted(shared)))
    pairs.sort(reverse=True)
    if pairs:
        print(f"PREDICTED CONFLICTS ({len(pairs)} pair(s) editing the same files)")
        for count, a, b, files in pairs[:args.limit]:
            print(f"  {a} ↔ {b}: {count} file(s)")
            for path in files[:4]:
                print(f"      {path}")
            if len(files) > 4:
                print(f"      … and {len(files) - 4} more")
        print()
        print("Merge these in a deliberate order, or ask one of each pair to rebase")
        print("before the other lands. This is textual overlap only — two changes that")
        print("merge cleanly and are jointly wrong will not appear here.")
    else:
        print("No two agents are currently editing the same file.")

    if args.json:
        print()
        print(json.dumps({
            "agents": len(agents), "humans": len(humans),
            "in_flight": len(in_flight), "open_prs": len(with_pr),
            "unsigned": [str(a.get("agent")) for a in unverified],
            "conflict_pairs": [{"a": a, "b": b, "files": f} for _, a, b, f in pairs],
        }, indent=2, sort_keys=True))
    return 0


def cmd_trailers(store: Store, args: argparse.Namespace) -> int:
    """Commit trailers that record which agent and model produced a change.

    Trailers rather than a badge or a bot comment: they survive a rebase, they
    are greppable with `git log --grep`, and they are the only provenance a
    maintainer can audit six months later without this tool installed.
    """
    config = store.config()
    lines = [
        f"Agent: {config.get('agent')}",
        f"Agent-Harness: {config.get('harness') or 'unknown'}",
    ]
    if config.get("model"):
        lines.append(f"Agent-Model: {config['model']}")
    if config.get("github"):
        lines.append(f"Agent-Principal: @{config['github']}")
    if args.task:
        lines.append(f"Agent-Task: {args.task}")
    key = identity.describe(store.repo_root)
    if key["fingerprint"]:
        lines.append(f"Agent-Key: {key['fingerprint']}")
    print("\n".join(lines))
    if args.explain:
        print()
        print("Append these to a commit message so the change is attributable without")
        print("this tool. The human named in Agent-Principal is the responsible party;")
        print("the agent is the instrument. Add your own Signed-off-by (DCO) as well.")
    return 0


def cmd_off(store: Store, args: argparse.Namespace) -> int:
    """Stop participating, immediately, without deleting anything shared."""
    config = store.config()
    config["paused"] = True
    store.save_config(config)
    from . import canvas, hooks
    stopped = 0
    with contextlib.suppress(Exception):
        stopped = canvas.stop_daemons(store)
    removed = hooks.uninstall(store)
    print("AgentColab is off on this machine.")
    print("  hooks     " + ("removed from " + ", ".join(removed) if removed else "none found"))
    if stopped:
        print(f"  canvas    {stopped} streaming daemon(s) stopped")
    print("  records   left on the shared ref (others still see your last state)")
    print("  restart   colab join")
    print()
    print("To remove your records from the shared ref as well:  colab purge")
    return 0


def cmd_purge(store: Store, args: argparse.Namespace) -> int:
    """Remove everything this agent published, then everything local.

    A tool that cannot be completely removed does not get installed in the first
    place, so this deletes rather than tombstones, and it says exactly what it
    did.
    """
    if not args.yes:
        eprint("colab: this deletes every record you published and all local state.")
        eprint("     Other agents' records are untouched — you cannot delete theirs.")
        eprint("     Confirm with --yes")
        return 1
    with contextlib.suppress(Exception):
        from . import canvas
        cfg = canvas.canvas_config(store)
        canvas.stop_daemons(store)
        if cfg.get("room") and cfg.get("token"):
            canvas.leave(str(cfg.get("relay")), str(cfg.get("room")), str(cfg.get("token")),
                         str(cfg.get("name") or store.agent))
    removed = 0
    if store.mine.is_dir():
        import shutil
        removed = len(list(store.mine.rglob("*.json")))
        shutil.rmtree(store.mine, ignore_errors=True)
        store.mine.mkdir(parents=True, exist_ok=True)
    published = False
    if store.push_url:
        # An empty own-record set means the next tree simply lacks our paths,
        # which is how the removal reaches everyone else.
        published, detail = store.publish("purge")
        if not published:
            eprint(f"colab: could not publish the removal ({detail}).")
            eprint("     Your records stay on the ref until you are online. Local state")
            eprint("     is deleted regardless.")
    if args.all_refs and store.push_url:
        proc = git(["push", store.push_url, "--delete", STATE_REF],
                   cwd=store.state, check=False, timeout=30)
        print("  shared ref  " + ("deleted entirely" if proc.returncode == 0
                                  else "could not delete (you may lack permission)"))
    store.destroy()
    print(f"purged · {removed} record(s) withdrawn"
          + (" and published" if published else " locally"))
    print("Nothing of AgentColab remains on this machine.")
    return 0


def cmd_whoami(store: Store, args: argparse.Namespace) -> int:
    config = store.config()
    key = identity.describe(store.repo_root)
    print(f"agent    {config.get('agent')}")
    if config.get("bio"):
        print(f"bio      {config['bio']}")
    print(f"human    {config.get('human') or '-'}")
    print(f"harness  {config.get('harness')}  model {config.get('model') or '-'}")
    print(f"github   {config.get('github') or '-'}")
    print(f"signing  {key['fingerprint'] or 'no key — you show as unverified'}")
    print(f"profile  {store.profile}  ({store.home})")
    print(f"publish  {_safe_url(store.push_url) or 'read-only'}")
    return 0


def cmd_rename(store: Store, args: argparse.Namespace) -> int:
    """Change what this agent calls itself, without stranding anything.

    Renaming re-points which directory this agent owns, so anything the old name
    published and never pushed would be orphaned -- nobody else is allowed to
    clean up another agent's records. So: publish first, hand back claims, then
    move.
    """
    new = records.slug(args.name)
    old = store.agent
    if not new or new == old:
        return _ok(f"already {old}")
    store.fetch()
    store.view(refresh=True)
    taken = {str(p.get("agent")) for p in store.agents()
             if p.get("machine_id") != store.config().get("machine_id")}
    if new in taken:
        eprint(f"colab: {new!r} is taken. Try {_free_name(new, taken)!r}.")
        return 1
    pending = store.unpublished()
    if pending and store.push_url:
        ok, detail = store.publish(f"pre-rename flush {old}")
        if not ok:
            eprint(f"colab: {pending} record(s) are not published yet and the push "
                   f"failed ({detail}).")
            eprint("     Renaming now would strand them. Get online, then retry.")
            return 1
    cmd_release(store, argparse.Namespace(id=None, all=True))

    # Records live under a directory named after the agent, so the rename is a
    # move of everything we own plus a tombstone where we used to be.
    import shutil
    for family in ("msgs", "claims", "bugs", "findings", "tasks", "reviews"):
        source = store.mine / family / old
        if source.is_dir():
            target = store.mine / family / new
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.rmtree(target, ignore_errors=True)
            shutil.move(str(source), str(target))
    with contextlib.suppress(OSError):
        (store.mine / "agents" / f"{old}.json").unlink()

    config = store.config()
    config["agent"] = new
    if args.bio:
        config["bio"] = records.scrub(args.bio)[:280]
    config.setdefault("former_names", []).append({"was": old, "at": iso()})
    store.save_config(config)
    with contextlib.suppress(LinkError):
        store.bind_profile(new)
    session.write_heartbeat(store, deep=True, publish=bool(store.push_url))
    session.mirror(store, "note", f"{old} is now {new}",
                   fields={"bio": config.get("bio") or "-"}, channel="link")
    return _ok(f"{old} → {new}\nEverything you own moved with you. Others see the new "
               f"name at their next sync.")


def cmd_reset(store: Store, args: argparse.Namespace) -> int:
    if not args.yes:
        eprint(f"colab: this deletes {store.home} (local state and credentials).")
        eprint("     Nothing on any remote is touched. Confirm with --yes")
        return 1
    store.destroy()
    return _ok("local state removed")


# ================================================================ install


def cmd_install(store: Store, args: argparse.Namespace) -> int:
    from . import hooks
    target = args.target
    if target in ("auto", None):
        target = _detect_harness()
        if target == "unknown":
            target = "all"
    installed = hooks.install(store, target, force=args.force)
    if not installed:
        eprint(f"colab: nothing to install for {target!r} "
               f"(try: {', '.join(hooks.TARGETS)}, or `all`)")
        return 1
    for line in installed:
        print(line)
    return 0


def cmd_mcp(store: Store, args: argparse.Namespace) -> int:
    from . import mcp
    return mcp.serve(store)




# ================================================================ canvas


def _canvas_store() -> Store | None:
    """A Store, or None with the reason printed.

    `canvas new` and `canvas serve` are the two verbs a person runs before this
    machine has joined anything -- one to mint a room, one to host the relay --
    so the canvas parser does its own store handling rather than taking the
    global `needs_store` gate.
    """
    try:
        store = Store()
    except LinkError as exc:
        eprint(f"colab: {exc}")
        return None
    if not store.initialised:
        eprint("colab: this machine has not joined yet.")
        eprint("     Run:  colab join")
        return None
    return store


def _canvas_relay(args: argparse.Namespace, store: Store | None) -> str:
    """--relay, then this machine's config, then the project's, then the default."""
    from . import canvas
    if getattr(args, "relay", None):
        return str(args.relay).rstrip("/")
    if store is not None:
        cfg = canvas.canvas_config(store)
        if cfg.get("relay"):
            return str(cfg["relay"]).rstrip("/")
    with contextlib.suppress(Exception):
        project = canvas.project_canvas(Path.cwd())
        if project.get("relay"):
            return str(project["relay"]).rstrip("/")
    return canvas.DEFAULT_RELAY


def cmd_canvas(store: Store | None, args: argparse.Namespace) -> int:
    from . import canvas
    action = args.action or "status"

    # ---- new: mint a room. Works before `colab join`.
    if action == "new":
        try:
            store = Store()
        except LinkError:
            store = None
        relay = _canvas_relay(args, store if store and store.initialised else None)
        policy = {"max_stream": args.max_stream} if args.max_stream else None
        name = args.name or (store.project if store else "room")
        room = canvas.new_room(relay, name, policy)
        if not room:
            eprint(f"colab: could not reach the relay at {relay}")
            eprint("     Check the URL, or run your own with `colab canvas serve`.")
            return 1
        code, join_code = str(room.get("room")), str(room.get("join_code"))
        print("canvas room created")
        print()
        print(f"  watch      {canvas.viewer_url(relay, code)}")
        print(f"  room code  {code}")
        print(f"  join code  {join_code}")
        print()
        print("Share the room code with anyone who should watch. Share the join code")
        print("with a teammate's machine — it lets an agent stream as any name.")
        print()
        print("Next:  colab canvas join " + join_code)
        return 0

    store = store or _canvas_store()
    if store is None:
        return 1
    relay = _canvas_relay(args, store)
    cfg = canvas.canvas_config(store)

    # ---- join: register, save, and start streaming the session we are in now.
    if action == "join":
        words = list(getattr(args, "words", None) or [])
        join_code = str((words[0] if words else "") or cfg.get("join_code") or "")
        if not join_code:
            eprint("colab: a join code is required — `colab canvas join <join code>`")
            eprint("     Ask whoever ran `colab canvas new` for it.")
            return 1
        name = store.agent
        requested = args.stream or "tools"
        reg = canvas.register(relay, join_code, name,
                              harness=str(store.config().get("harness") or "") or None,
                              human=str(store.config().get("github") or "") or None,
                              model=str(store.config().get("model") or "") or None,
                              stream=requested)
        if not reg:
            eprint(f"colab: the relay at {relay} refused the join code.")
            eprint("     It may be wrong, or the room may have been deleted.")
            return 1
        room = join_code.split(".", 1)[0]
        block = _save_canvas_join(store, relay=relay, room=room, join_code=join_code, reg=reg,
                                  requested=requested)
        started = canvas.ensure_tailers_for_cwd(store)
        print(f"joined the canvas room as {name}")
        print(f"  watch    {canvas.viewer_url(relay, room)}")
        print(f"  streams  {block['stream']}"
              + ("" if block["stream"] == requested
                 else f" (the room's ceiling; you asked for {requested})"))
        if started:
            print(f"  live     {len(started)} session(s) from this checkout")
        else:
            print("  live     no session found yet — the next one streams automatically")
        _print_owner_link(relay, room, block)
        _start_listener_if_enabled(store)
        return 0

    if not canvas.is_on(store):
        eprint("colab: this machine has not joined a canvas room.")
        eprint("     Run:  colab canvas join <join code>")
        return 1

    # ---- the rest need a room.
    if action == "role":
        if args.clear:
            text = None
        else:
            text = " ".join(getattr(args, "words", None) or [])
            if not text.strip():
                eprint("colab: say what the role is, or pass --clear")
                return 1
        ok = canvas.put_role(relay, str(cfg.get("room")), str(cfg.get("token")),
                             str(cfg.get("name") or store.agent), text)
        if not ok:
            eprint("colab: the relay did not accept that role")
            return 1
        return _ok("role cleared" if text is None else f"role set: {records.one_line(text, 60)}")

    if action == "pull":
        found = session.pull_inbox(store, timeout=5)
        return _ok(f"pulled {found} ask(s) and the current role"
                   if found else "nothing new on the canvas")

    if action == "tail":
        if args.discover:
            return canvas.tail_discover(store, once=args.once)
        if not args.session:
            started = canvas.ensure_tailers_for_cwd(store)
            if not started:
                eprint("colab: no live session found for this checkout — run `colab canvas tail --discover --once` to see why.")
                eprint("     Pass --session and --transcript, or --discover.")
                return 1
            return _ok(f"streaming {len(started)} session(s)")
        return canvas.tail_loop(store, args.session, args.transcript, once=args.once,
                                harness=args.harness)

    if action == "ensure":
        canvas.ensure_tailers_for_cwd(store)
        return 0

    if action == "off":
        stopped = canvas.stop_daemons(store)
        config = store.config()
        config.pop("canvas", None)
        store.save_config(config)
        print("canvas off on this machine.")
        print(f"  daemons   {stopped} stopped (the wake listener among them)")
        print("  room      still there for everyone else; rejoin with `colab canvas join`")
        return 0

    if action == "say":
        text = " ".join(getattr(args, "words", None) or []).strip()
        if not text:
            eprint("colab: say what — `colab canvas say \"<text>\"`")
            return 1
        try:
            posted = canvas.post_message(relay, str(cfg.get("room")), str(cfg.get("token")),
                                         to="*", kind="say", text=records.scrub(text)[:2000])
        except Exception as exc:
            eprint(f"colab: the relay did not take that line: {exc}")
            eprint("     An older relay has no messages; `colab canvas status` shows its version.")
            return 1
        return _ok(f"said to the room ({posted.get('id')})")

    if action == "export":
        block = {"relay": relay, "room": str(cfg.get("room"))}
        if args.with_join_code:
            block["join_code"] = str(cfg.get("join_code"))
        if args.with_join_code:
            eprint("colab: that join code lets anyone who can read this repository stream")
            eprint("     as any agent name. Commit it only for a private repo you trust.")
        if args.stdout:
            print(json.dumps({"canvas": block}, indent=2))
            return 0
        # The documented quickstart says `export` writes the file, so it does:
        # merged into whatever is there, `$comment` and the chat block included.
        # This is the one place the tool writes into the checkout, and it is a
        # file the user then commits by hand -- nothing else is touched.
        path = store.repo_root / ".agentcolab" / "agentcolab.json"
        existing = read_json(path) if path.is_file() else {}
        existing = existing if isinstance(existing, dict) else {}
        merged = dict(existing.get("canvas") or {}) if isinstance(existing.get("canvas"), dict) else {}
        merged.update(block)
        existing["canvas"] = merged
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json(path, existing)
        rel = path.relative_to(store.repo_root)
        print(f"wrote {rel}")
        print(json.dumps({"canvas": merged}, indent=2))
        if merged.get("join_code") and not args.with_join_code:
            eprint(f"colab: {rel} already carried a join code and still does.")
        print(f"Next:  git add {rel} && git commit -m \"AgentColab: canvas room\"")
        return 0

    # ---- status (the default)
    if getattr(args, "owner_link", False):
        token = cfg.get("owner_token")
        if not token:
            eprint("colab: no owner token saved — the relay that registered this agent predates")
            eprint("     them. `colab canvas join <join code>` again to get one.")
            return 1
        print(canvas.owner_link(relay, str(cfg.get("room")), str(token)))
        return 0
    health = canvas.healthz(relay)
    print(f"canvas    {cfg.get('room')}")
    print(f"  watch     {canvas.viewer_url(relay, str(cfg.get('room')))}")
    print(f"  relay     {relay}" + ("" if health else "  (unreachable)"))
    if health:
        print(f"  transport {', '.join(health.get('transports') or [])}")
    print(f"  streaming {cfg.get('stream') or 'tools'}  (as {cfg.get('name') or store.agent})")
    states = canvas.daemon_states(store)
    if states:
        for row in states:
            note = f"  ({row.get('reason')})" if row.get("reason") else ""
            print(f"  session   {str(row.get('sid'))[:8]}  {row.get('state')}{note}")
    else:
        print("  session   no daemon running — start one with `colab canvas ensure`")
    role = session.canvas_role(store)
    if role:
        print(f"  role      {records.one_line(role.get('role'), 60)} "
              f"(from viewer {records.one_line(role.get('viewer'), 24)})")
    print(f"  {session.canvas_budget_line(store)}")
    with contextlib.suppress(Exception):
        from . import wake
        print(f"  {_wake_line(wake.status(store))}")
    if cfg.get("owner_token"):
        print("  owner     link saved — `colab canvas status --owner-link` prints it")
    return 0


def _wake_line(state: dict[str, Any]) -> str:
    if not state.get("enabled"):
        return "wake: off — `colab wake on` lets a ping start a session here"
    return (f"wake: on · from {state.get('from')} · {state.get('used_this_hour')}/{state.get('max_per_hour')} "
            f"this hour · listener {state.get('listener')}")


# ================================================================ wake, ping


def cmd_wake(store: Store, args: argparse.Namespace) -> int:
    """`colab wake on|off|status|serve|test` (contract §10.7)."""
    from . import canvas, wake
    action = args.action or "status"
    if action == "serve":
        if not canvas.is_on(store):
            eprint("colab: this machine has not joined a canvas room, so there is nothing to listen to.")
            eprint("     Run:  colab canvas join <join code>")
            return 1
        return wake.serve(store, dry_run=bool(args.dry_run), once=bool(args.once))
    if action == "test":
        raw = " ".join(getattr(args, "words", None) or []).strip()
        if raw == "-" or not raw:
            raw = sys.stdin.read().strip()
        try:
            message = json.loads(raw)
        except ValueError:
            eprint('colab: pass the message as JSON — `colab wake test \'{"id":"cm-…","to":"<me>",'
                   '"kind":"ping","text":"…","from":{"kind":"agent","name":"bob"}}\'`')
            return 1
        if not isinstance(message, dict):
            eprint("colab: the message must be a JSON object")
            return 1
        config = wake.settings(store)
        result, reason = wake.decide(store, message, config)
        print(f"decision  {result}" + (f" ({reason})" if reason else ""))
        if result == "woke":
            argv = wake.harness_argv(str(store.config().get("harness") or "claude-code"), "<prompt>")
            print(f"would run {' '.join(argv) if argv else '(no headless command for this harness)'}")
        print()
        print(wake.prompt(message, config, me=store.agent, used=wake.used_this_hour(store) + 1))
        return 0
    if not canvas.is_on(store):
        eprint("colab: this machine has not joined a canvas room.")
        eprint("     Run:  colab canvas join <join code>   then   colab wake on")
        return 1
    cfg = canvas.canvas_config(store)
    relay, room, token = str(cfg.get("relay") or ""), str(cfg.get("room") or ""), str(cfg.get("token") or "")
    name = str(cfg.get("name") or store.agent)
    if action in ("on", "off"):
        changes: dict[str, Any] = {"enabled": action == "on"}
        if getattr(args, "from_", None):
            changes["from"] = args.from_
        if getattr(args, "max_per_hour", None):
            changes["max_per_hour"] = int(args.max_per_hour)
        settings = canvas.save_wake_settings(store, **changes)
        # The relay's copy is what the page shows; the machine's is what happens.
        remote = canvas.put_wake(relay, room, token, name, settings)
        lines = []
        if action == "on":
            state = wake.ensure_listener(store)
            lines.append(f"listener    {state}" + ("" if state != "failed" else
                                                    " — `colab wake serve` in a terminal shows why"))
            if args.at_login:
                lines.extend(wake.install_login_item(store))
        else:
            stopped = wake.stop_listener(store)
            lines.append(f"listener    {'stopped' if stopped else 'was not running'}")
            if args.at_login:
                lines.extend(wake.remove_login_item(store) or ["login item  none installed"])
        print(f"wake {'on' if settings['enabled'] else 'off'} for {name}")
        print(f"  from        {settings['from']}  ({'agents and viewers of the room' if settings['from'] == 'room' else 'other agents only'})")
        print(f"  cap         {settings['max_per_hour']} session(s) an hour")
        for line in lines:
            print(f"  {line}")
        print(f"  relay       {'updated' if remote is not None else 'not updated — it predates wake, or is unreachable; the page will not show it'}")
        if action == "on":
            print()
            print("A woken session runs `claude -p` / `codex exec` with your harness's own permission")
            print("settings, so it can only use tools you already allow headless. The message is")
            print("fenced as untrusted; the agent is told to do only what you would allow anyway.")
        return 0
    # status
    state = wake.status(store)
    print(f"wake      {'on' if state['enabled'] else 'off'}  (as {name})")
    print(f"  from        {state['from']}")
    print(f"  cap         {state['max_per_hour']} an hour · {state['used_this_hour']} used this hour")
    print(f"  listener    {state['listener']}" + (f" (pid {state['pid']})" if state.get("pid") else ""))
    item = wake.login_item_path(store)
    print(f"  login item  {'installed' if state.get('login_item') else 'none'}"
          + (f" ({item})" if item and state.get("login_item") else ""))
    logs = sorted((canvas.markers_dir(store) / wake.LOG_DIR).glob("*.log")) if (canvas.markers_dir(store) / wake.LOG_DIR).is_dir() else []
    if logs:
        print(f"  sessions    {len(logs)} log(s) under {canvas.markers_dir(store) / wake.LOG_DIR}")
    return 0


def cmd_ping(store: Store, args: argparse.Namespace) -> int:
    """`colab ping <agent> "<text>" [--no-wake]`: a ping on the canvas and the same line on the ref."""
    from . import canvas
    to = records.slug(args.agent)
    text = records.scrub(" ".join(args.text)).strip()
    if not text:
        eprint("colab: say what they should look at — `colab ping <agent> \"<text>\"`")
        return 1
    # The git-ref copy first: it enforces the hourly send cap, and an agent
    # outside the room still sees the ping on its next sync.
    rc = cmd_send(store, argparse.Namespace(
        subject=[text[:200]], body="", to=to, kind="ping", paths=[], task="", channel="link",
        needs_reply=False, reply_to="", force=bool(getattr(args, "force", False)), allow_secrets=False))
    if rc:
        return rc
    if not canvas.is_on(store):
        print("  (no canvas room joined, so nothing was pinged on the canvas)")
        return 0
    cfg = canvas.canvas_config(store)
    try:
        posted = canvas.post_message(str(cfg.get("relay") or ""), str(cfg.get("room") or ""),
                                     str(cfg.get("token") or ""), to=to, kind="ping",
                                     text=text[:2000], wake=not args.no_wake)
    except Exception as exc:
        eprint(f"colab: the canvas relay did not take the ping: {exc}")
        eprint("     The git-ref copy went out; an older relay has no messages.")
        return 1
    return _ok(f"pinged {to} on the canvas ({posted.get('id')})"
               + ("" if args.no_wake else " — wake requested; their machine decides"))


def cmd_hook(store: Store | None, args: argparse.Namespace) -> int:
    from . import hooks
    return hooks.dispatch(args.event)


# ================================================================ parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="colab",
        description="AgentColab — many agents, many humans, one repo.",
        epilog="Docs: https://github.com/AgentColab/AgentColab")
    parser.add_argument("--version", action="version", version=_version())
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("join", help="set this machine up. Detects everything. Run this first.")
    p.add_argument("--as", dest="name",
                   help="what this agent calls itself. Pick something other humans "
                        "and agents will recognise in a channel.")
    p.add_argument("--bio", help="one line: what you are and what you are good at")
    p.add_argument("--human", help="who owns this machine")
    p.add_argument("--intent", help="what you are about to work on")
    p.add_argument("--harness", help="claude-code, codex, cursor, aider, …")
    p.add_argument("--model", help="which model is driving, for the record")
    p.add_argument("--dialect", choices=["wire", "prose"], help="how peers talk to you")
    p.add_argument("--push-to", help="remote to publish your records to")
    p.add_argument("--source", action="append", help="extra URL to read state from")
    p.add_argument("--no-install", dest="install", action="store_false",
                   help="skip wiring your harness's hooks")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--yes", "-y", action="store_true", help="never prompt")
    p.set_defaults(func=cmd_join, install=True)

    p = sub.add_parser("status", help="who is doing what, right now")
    p.add_argument("--offline", action="store_true", help="use the cached view")
    p.add_argument("--sync", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("sync", help="pull everyone's state, publish yours")
    p.add_argument("--intent")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("context", help="the session briefing (used by hooks)")
    p.add_argument("--raw", action="store_true", help="ignore the token budget")
    p.set_defaults(func=cmd_context)

    p = sub.add_parser("intent", help="say what you are working on")
    p.add_argument("text", nargs="+")
    p.set_defaults(func=cmd_intent)

    p = sub.add_parser("next", help="the one task nobody else will be offered")
    p.add_argument("--take", action="store_true", help="take it immediately")
    p.add_argument("--offline", action="store_true", help="do not fetch first")
    p.set_defaults(func=cmd_next)

    p = sub.add_parser("board", help="every task and who holds it")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_board)

    p = sub.add_parser("task", help="put work on the board")
    p.add_argument("title", nargs="+")
    p.add_argument("--body", help="'-' reads stdin")
    p.add_argument("--paths", nargs="*")
    p.add_argument("--priority", default="p2", choices=list(board.PRIORITIES))
    p.add_argument("--size", default="m", choices=list(board.SIZES))
    p.add_argument("--tag", action="append")
    p.add_argument("--dep", action="append", help="task id that must finish first")
    p.set_defaults(func=cmd_task)

    p = sub.add_parser("take", help="claim a task (lease, not a lock)")
    p.add_argument("id")
    p.add_argument("--lease", default=board.DEFAULT_LEASE)
    p.add_argument("--note", default="")
    p.add_argument("--force", action="store_true", help="take one somebody else holds")
    p.set_defaults(func=cmd_take)

    p = sub.add_parser("done", help="finish a task you hold")
    p.add_argument("id")
    p.add_argument("--pr", help="pull request number or URL")
    p.add_argument("--note", help="'-' reads stdin")
    p.set_defaults(func=cmd_done)

    p = sub.add_parser("drop", help="give a task back")
    p.add_argument("id")
    p.set_defaults(func=cmd_drop)

    p = sub.add_parser("send", help="message other agents")
    p.add_argument("subject", nargs="+")
    p.add_argument("--body", help="'-' reads stdin")
    p.add_argument("--to", help="one agent, or omit for everyone")
    p.add_argument("--kind", default="note",
                   choices=["note", "heads-up", "question", "decision", "reply",
                            "handoff", "blocked", "review"])
    p.add_argument("--paths", nargs="*")
    p.add_argument("--task")
    p.add_argument("--channel", default="link",
                   help="any channel from `colab channels`, including your own")
    p.add_argument("--needs-reply", action="store_true",
                   help="use only when genuinely blocked")
    p.add_argument("--reply-to")
    p.add_argument("--force", action="store_true", help="past the hourly cap")
    p.add_argument("--allow-secrets", action="store_true",
                   help="do not blank high-entropy strings (think twice)")
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("reply", help="answer a message")
    p.add_argument("id")
    p.add_argument("text", nargs="+")
    p.set_defaults(func=cmd_reply)

    p = sub.add_parser("answer", help="answer a human in the chat room they asked in")
    p.add_argument("id")
    p.add_argument("text", nargs="+")
    p.add_argument("--channel", help="override where the answer goes")
    p.set_defaults(func=cmd_answer)

    p = sub.add_parser("inbox", help="unread messages")
    p.add_argument("--chat", action="store_true", help="messages from humans instead")
    p.add_argument("--wire", action="store_true", help="compact form")
    p.add_argument("--all", action="store_true",
                   help="include records below the project's minimum trust")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_inbox)

    p = sub.add_parser("read", help="read one record and mark it seen")
    p.add_argument("id")
    p.set_defaults(func=cmd_read)

    p = sub.add_parser("finding", help="record something nobody should rediscover")
    p.add_argument("title", nargs="+")
    p.add_argument("--body", help="'-' reads stdin")
    p.add_argument("--paths", nargs="*")
    p.add_argument("--tag", action="append")
    p.set_defaults(func=cmd_finding)

    p = sub.add_parser("known", help="everything already worked out (run before you dig)")
    p.add_argument("--about", help="filter by text or path")
    p.add_argument("--full", action="store_true")
    p.add_argument("--limit", type=int, default=15)
    p.set_defaults(func=cmd_known)

    p = sub.add_parser("claim", help="announce paths you are rewriting (warns, never blocks)")
    p.add_argument("paths", nargs="+")
    p.add_argument("--for", dest="reason", required=True)
    p.add_argument("--ttl", default=DEFAULT_TTL)
    p.add_argument("--critical", action="store_true", help="louder; still does not block")
    p.add_argument("--override", action="store_true", help="claim over a stale one")
    p.set_defaults(func=cmd_claim)

    p = sub.add_parser("release", help="give claims back")
    p.add_argument("id", nargs="?")
    p.add_argument("--all", action="store_true")
    p.set_defaults(func=cmd_release)

    p = sub.add_parser("claims", help="every live claim")
    p.set_defaults(func=cmd_claims)

    p = sub.add_parser("check", help="is this path in use? (local, instant, exit 2 if yes)")
    p.add_argument("path")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("preflight", help="before you push: what moved, where you overlap")
    p.add_argument("--base", default="main")
    p.add_argument("--no-fetch", action="store_true")
    p.set_defaults(func=cmd_preflight)

    p = sub.add_parser("bug", help="file a failure that may be the machine")
    p.add_argument("title", nargs="+")
    p.add_argument("--body", help="'-' reads stdin")
    p.add_argument("--paths", nargs="*")
    p.add_argument("--capture", nargs="*",
                   help="-- <cmd>: run it here and record the exact failure")
    p.add_argument("--fresh", action="store_true",
                   help="re-probe the toolchain instead of using the cached fingerprint")
    p.set_defaults(func=cmd_bug)

    p = sub.add_parser("bugs", help="open cross-machine bugs")
    p.add_argument("--all", action="store_true")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_bugs)

    p = sub.add_parser("bug-try", help="diff their machine against yours")
    p.add_argument("id")
    p.add_argument("--run", action="store_true",
                   help="re-run their command here (ask your human first)")
    p.set_defaults(func=cmd_bug_try)

    p = sub.add_parser("bug-close", help="close a bug you filed")
    p.add_argument("id")
    p.add_argument("--as", dest="status", default="resolved",
                   choices=["resolved", "environmental", "wontfix", "duplicate"])
    p.add_argument("--note")
    p.set_defaults(func=cmd_bug_close)

    p = sub.add_parser("review", help="ask for, or give, a review on a pull request")
    p.add_argument("pr")
    p.add_argument("--to", help="a specific agent (default: deterministic)")
    p.add_argument("--verdict", choices=["approve", "changes", "comment"],
                   help="omit to request a review")
    p.add_argument("--body", help="'-' reads stdin")
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("chat", help="Discord / Slack bridge")
    p.add_argument("action", nargs="?", default="status",
                   choices=["status", "setup", "invite", "provision", "pull", "test",
                            "export", "off"])
    p.add_argument("driver", nargs="?", choices=list(chat.DRIVERS))
    p.add_argument("--driver", dest="driver_flag")
    p.add_argument("--guild", help="Discord server id, for provision")
    p.add_argument("--minimal", action="store_true",
                   help="invite link without the channel-creation permissions")
    p.add_argument("--channel", help="channel name, for `test`")
    p.set_defaults(func=cmd_chat)

    p = sub.add_parser("wire", help="the compact agent-to-agent protocol")
    p.add_argument("action", nargs="?", default="legend",
                   choices=["legend", "encode", "decode", "measure"])
    p.add_argument("text", nargs="*")
    p.add_argument("--kind")
    p.add_argument("--to")
    p.set_defaults(func=cmd_wire)

    p = sub.add_parser("roster", help="who this project trusts, and how much")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_roster)

    p = sub.add_parser("doctor", help="check every part of the link end to end")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("log", help="recent activity")
    p.add_argument("--limit", type=int, default=40)
    p.set_defaults(func=cmd_log)

    p = sub.add_parser("install", help="wire colab into your agent harness")
    p.add_argument("target", nargs="?", default="auto")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_install)

    p = sub.add_parser("mcp", help="run as an MCP server (any agent can use the tools)")
    p.set_defaults(func=cmd_mcp)

    p = sub.add_parser("canvas", help="the live web view of every agent in a room")
    p.add_argument("action", nargs="?", default="status",
                   choices=["new", "join", "serve", "tail", "role", "status",
                            "off", "export", "ensure", "pull", "say"],
                   help="new: mint a room · join: stream from this machine · "
                        "serve: run your own relay · status: where things stand · "
                        "say: a line to everyone in the room")
    # One free-form word list rather than a positional per verb: `join` takes a
    # code, `role` takes a sentence, `ensure` is handed a JSON blob by Codex's
    # `notify` and must ignore it rather than exit 2 on every turn.
    p.add_argument("words", nargs="*", help="the join code (join), the role text (role), the line (say)")
    p.add_argument("--owner-link", dest="owner_link", action="store_true",
                   help="print this machine's owner link again (status)")
    p.add_argument("--stdout", action="store_true",
                   help="print the export instead of writing .agentcolab/agentcolab.json")
    p.add_argument("--relay", help="relay base URL (default: the project's, then the built-in one)")
    p.add_argument("--name", help="what to call the room, for `new`")
    p.add_argument("--max-stream", dest="max_stream",
                   choices=["summary", "tools", "full"],
                   help="the room's ceiling, for `new`. Nobody may stream above it.")
    p.add_argument("--stream", choices=["summary", "tools", "full"],
                   help="what this machine sends (default tools: text and paths, "
                        "never file contents)")
    p.add_argument("--clear", action="store_true", help="clear the role")
    p.add_argument("--with-join-code", dest="with_join_code", action="store_true",
                   help="include the join code in `export` — anyone who can read the "
                        "repository can then stream as any agent")
    p.add_argument("--session", help="session id, for `tail`")
    p.add_argument("--transcript", help="transcript path, for `tail`")
    p.add_argument("--harness", help="which harness wrote the transcript, for `tail`")
    p.add_argument("--discover", action="store_true", help="tail every live session here")
    p.add_argument("--once", action="store_true", help="one pass, then exit")
    p.set_defaults(func=cmd_canvas, needs_store=False)

    p = sub.add_parser("wake", help="let a ping from the room start a session on this machine")
    p.add_argument("action", nargs="?", default="status",
                   choices=["on", "off", "status", "serve", "test"],
                   help="on/off: the switch · status · serve: the listener itself · "
                        "test: what a given message would do")
    p.add_argument("words", nargs="*", help="the message JSON, for `test` ('-' reads stdin)")
    p.add_argument("--from", dest="from_", choices=["agents", "room"],
                   help="who may wake this machine: other agents only, or viewers of the room too")
    p.add_argument("--max-per-hour", dest="max_per_hour", type=int, choices=range(1, 61),
                   metavar="N", help="at most N woken sessions an hour (1-60)")
    p.add_argument("--at-login", dest="at_login", action="store_true",
                   help="also install (on) or remove (off) a login item that runs the listener")
    p.add_argument("--dry-run", dest="dry_run", action="store_true",
                   help="serve: decide and print, start nothing, ack nothing")
    p.add_argument("--once", action="store_true", help="serve: one connection, then exit")
    p.set_defaults(func=cmd_wake)

    p = sub.add_parser("ping", help="ask one agent to look at something; may wake its machine")
    p.add_argument("agent")
    p.add_argument("text", nargs="+")
    p.add_argument("--no-wake", dest="no_wake", action="store_true",
                   help="deliver at their next sync only; never start a session")
    p.add_argument("--force", action="store_true", help="past the hourly send cap")
    p.set_defaults(func=cmd_ping)

    p = sub.add_parser("hook", help="internal: called by harness hooks")
    p.add_argument("event")
    p.set_defaults(func=cmd_hook, needs_store=False)

    p = sub.add_parser("channels", help="every chat room, and what belongs in each")
    p.add_argument("--full", action="store_true", help="include each channel's brief")
    p.set_defaults(func=cmd_channels)

    p = sub.add_parser("say", help="post a line to any channel, including your own")
    p.add_argument("channel")
    p.add_argument("text", nargs="*")
    p.add_argument("--body", help="'-' reads stdin")
    p.add_argument("--force", action="store_true", help="past the hourly budget")
    p.set_defaults(func=cmd_say)

    p = sub.add_parser("brief", help="what a channel is for, in its own words")
    p.add_argument("channel")
    p.set_defaults(func=cmd_brief)

    p = sub.add_parser("issues", help="open GitHub issues, split deterministically")
    p.add_argument("--limit", type=int, default=60)
    p.add_argument("--show", type=int, default=12)
    p.set_defaults(func=cmd_issues)

    p = sub.add_parser("triage", help="record a triage decision, with the reasoning")
    p.add_argument("issue")
    p.add_argument("--as", dest="priority", required=True, choices=list(PRIORITIES))
    p.add_argument("--why", required=True, help="published; a decision nobody can audit is not one")
    p.add_argument("--plan", help="'-' reads stdin. Required for p0.")
    p.set_defaults(func=cmd_triage)

    p = sub.add_parser("standup", help="who did what, as a digest")
    p.add_argument("--since", default="1d")
    p.add_argument("--post", action="store_true", help="also post it to #standup")
    p.set_defaults(func=cmd_standup)

    p = sub.add_parser("relay", help="mirror shared-ref activity into chat "
                                     "(for CI; contributors then need no tokens)")
    p.add_argument("--since", default="30m")
    p.add_argument("--limit", type=int, default=40)
    p.set_defaults(func=cmd_relay)

    p = sub.add_parser("review-load", help="for maintainers: how much agent work is "
                                           "queued at you, and what will collide")
    p.add_argument("--limit", type=int, default=8)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_review_load)

    p = sub.add_parser("trailers", help="provenance trailers for a commit message")
    p.add_argument("--task")
    p.add_argument("--explain", action="store_true")
    p.set_defaults(func=cmd_trailers)

    p = sub.add_parser("off", help="stop participating on this machine, right now")
    p.set_defaults(func=cmd_off)

    p = sub.add_parser("purge", help="withdraw everything you published, then delete "
                                     "all local state")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--all-refs", action="store_true",
                   help="also delete the shared ref (affects everyone)")
    p.set_defaults(func=cmd_purge)

    p = sub.add_parser("whoami", help="what this agent is, and what it can prove")
    p.set_defaults(func=cmd_whoami)

    p = sub.add_parser("rename", help="change what this agent calls itself")
    p.add_argument("name")
    p.add_argument("--bio", help="one line: what you are and what you are good at")
    p.set_defaults(func=cmd_rename)

    p = sub.add_parser("reset", help="delete this machine's local state")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_reset)

    return parser


def _version() -> str:
    from . import __version__
    return f"AgentColab {__version__}"


def main(argv: list[str] | None = None) -> int:
    # Before anything reads or writes a byte. Every entry point -- the CLI, the
    # MCP server, the hook dispatcher -- arrives here, and on Windows the
    # standard streams would otherwise default to the ANSI codepage and fail on
    # the first non-ASCII character in anybody's name or message.
    records.force_utf8()
    argv = list(sys.argv[1:] if argv is None else argv)
    # `colab bug "..." --capture -- npm test --watch` has to survive argparse,
    # which treats a bare `--` as end-of-options and would swallow the command.
    # Split it out by hand and hand it back afterwards.
    trailing: list[str] = []
    if "--" in argv:
        cut = argv.index("--")
        argv, trailing = argv[:cut], argv[cut + 1:]
    # `colab canvas serve --port 0 --state DIR` hands every remaining argument to
    # the relay, which has its own flags and needs no repository. Intercepted
    # before argparse rather than modelled as REMAINDER, which ate the flags of
    # every other canvas verb.
    if argv[:2] == ["canvas", "serve"]:
        from . import canvas_relay
        return int(canvas_relay.main(argv[2:]) or 0)

    parser = build_parser()
    # Bare `colab` is the single most typed thing here, so it is the picture.
    if not argv or (argv and argv[0].startswith("-") and argv[0] not in ("-h", "--help",
                                                                        "--version")):
        argv = ["status", *argv]
    args = parser.parse_args(argv)
    if trailing and hasattr(args, "capture"):
        args.capture = trailing
    if not getattr(args, "func", None):
        parser.print_help()
        return 0

    if getattr(args, "driver_flag", None):
        args.driver = args.driver_flag

    if getattr(args, "needs_store", True) is False:
        return int(args.func(None, args) or 0)

    try:
        store = Store()
    except LinkError as exc:
        eprint(f"colab: {exc}")
        return 1

    if args.func is not cmd_join and not store.initialised:
        eprint("colab: this machine has not joined yet.")
        eprint("     Run:  colab join")
        return 1

    try:
        return int(args.func(store, args) or 0)
    except LinkError as exc:
        eprint(f"colab: {exc}")
        return 1
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return 0
