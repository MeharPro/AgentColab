"""Chat is pluggable. Discord is the default; Slack ships in the box.

The rest of the system knows only `Event`, `post`, and `poll`. Adding a
platform means implementing `Adapter` and adding one line to `DRIVERS` — no
other file has to change, and no platform can quietly skip the scrubbing,
routing, or trust labelling that lives in `base`.
"""

from __future__ import annotations

import contextlib
from typing import Any

from .base import (BUILTIN, CHANNELS, INPUT_CHANNELS, UNTRUSTED_BANNER, Adapter,
                   ChatError, Event, inputs, normalise_incoming, resolve)
from .discord import Discord
from .slack import Slack

DRIVERS: dict[str, type[Adapter]] = {
    "discord": Discord,
    "slack": Slack,
}

__all__ = ["BUILTIN", "CHANNELS", "INPUT_CHANNELS", "UNTRUSTED_BANNER", "Adapter",
           "ChatError", "Event", "DRIVERS", "adapters", "post", "poll", "enabled",
           "inputs", "normalise_incoming", "resolve"]


def adapters(config: dict[str, Any]) -> list[Adapter]:
    """Every configured platform, in the order they were configured.

    More than one is a supported, deliberately ordinary case: a project can
    mirror to a public Discord and a company Slack at once, and a human in
    either can reach the agents.
    """
    chat = (config or {}).get("chat") or {}
    out: list[Adapter] = []
    for name in chat.get("drivers") or ([chat["driver"]] if chat.get("driver") else []):
        factory = DRIVERS.get(str(name))
        if factory and isinstance(chat.get(name), dict):
            settings = dict(chat[name])
            # Project-defined channels live beside the drivers, not inside one,
            # so hand them down or an adapter cannot route or provision them.
            settings.setdefault("custom", chat.get("custom") or {})
            out.append(factory(settings))
    return out


def enabled(config: dict[str, Any]) -> bool:
    return any(a.can_write() or a.can_read() for a in adapters(config))


def post(config: dict[str, Any], event: Event) -> int:
    """Mirror one event everywhere. Never raises: a mirror must not fail a command."""
    sent = 0
    for adapter in adapters(config):
        if not adapter.can_write():
            continue
        with contextlib.suppress(Exception):
            if adapter.post(event):
                sent += 1
        # The firehose gets everything; a channel that also routes elsewhere is
        # posted twice on purpose, because muting the firehose is the point.
        if event.channel != "firehose" and (adapter.config.get("channels") or {}).get("firehose"):
            with contextlib.suppress(Exception):
                mirror = Event(event.kind, event.agent, event.subject, body=event.body,
                               fields=event.fields, channel="firehose", wire=event.wire,
                               url=event.url, trust=event.trust)
                adapter.post(mirror)
    return sent


def poll(config: dict[str, Any], cursors: dict[str, Any], timeout: int = 10
         ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Read every input channel on every platform."""
    fresh: list[dict[str, Any]] = []
    moved = dict(cursors or {})
    for adapter in adapters(config):
        if not adapter.can_read():
            continue
        with contextlib.suppress(Exception):
            per_platform = dict(moved.get(adapter.name) or {})
            got, after = adapter.poll(per_platform, timeout=timeout)
            fresh.extend(got)
            moved[adapter.name] = after
    return fresh, moved
