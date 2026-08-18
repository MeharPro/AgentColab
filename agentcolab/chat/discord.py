"""Discord adapter — the default room.

Outbound is a webhook per channel: post-only, no bot, no scopes, no gateway
connection, nothing to host. Inbound is a bot token polled over REST on the
beats the git sync already uses. Neither needs a process running between
sessions, which is the whole reason this works for a volunteer project with no
infrastructure budget.

`provision` is the part that matters for adoption: given a bot token with
Manage Channels, it creates the whole category, every channel, and a webhook
for each, then writes the map into the project's public config. A newcomer
never touches any of it — they clone, run `colab join`, and the channel ids are
already in the repo.
"""

from __future__ import annotations

import json
import time
import urllib.error
from typing import Any

from .. import records
from .base import CHANNELS, Adapter, ChatError, Event, http, normalise_incoming, resolve

API = "https://discord.com/api/v10"

# Discord's edge rejects urllib's default agent with a 403 before the request
# ever reaches the API, so every call must carry a real User-Agent. base.http
# sets one; nothing here may bypass it.

TOPICS = {name: text for name, (_, text) in CHANNELS.items()}


class Discord(Adapter):
    name = "discord"

    # -- capability -------------------------------------------------------

    def can_write(self) -> bool:
        table = self.config.get("channels") or {}
        return bool(self.config.get("token") or any(c.get("webhook") for c in table.values()))

    def can_read(self) -> bool:
        return bool(self.config.get("token") and self._read_ids())

    def _read_ids(self) -> list[str]:
        table = self.config.get("channels") or {}
        explicit = self.config.get("read_channels")
        if explicit:
            return [str(x) for x in explicit if x]
        out = []
        for name in ("ask",):
            entry = table.get(name) or {}
            if entry.get("id"):
                out.append(str(entry["id"]))
        return out

    # -- outbound ---------------------------------------------------------

    def post(self, event: Event) -> bool:
        route = self.route(event.channel)
        payload = json.dumps({
            "content": event.text(),
            # Automation must never be able to ping a room. Without this, one
            # peer writing "@everyone" in a commit message notifies the server.
            "allowed_mentions": {"parse": []},
            "flags": 4 if event.channel == "firehose" else 0,   # suppress embeds
        }).encode()

        webhook = route.get("webhook")
        if webhook:
            return self._send(webhook, payload, headers={"Content-Type": "application/json"})
        channel_id = route.get("id")
        token = self.config.get("token")
        if channel_id and token:
            return self._send(f"{API}/channels/{channel_id}/messages", payload,
                              headers={"Content-Type": "application/json",
                                       "Authorization": f"Bot {token}"})
        return False

    def _send(self, url: str, payload: bytes, headers: dict[str, str],
              attempts: int = 3) -> bool:
        """Honour 429 rather than hammering. Never raises: a mirror that fails a
        command is worse than a mirror that misses a line."""
        for attempt in range(attempts):
            try:
                http(url, data=payload, method="POST", headers=headers)
                return True
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and attempt < attempts - 1:
                    try:
                        body = json.loads(exc.read() or b"{}")
                        delay = float(body.get("retry_after") or 1.0)
                    except (ValueError, OSError):
                        delay = 1.0
                    time.sleep(min(delay, 5.0))
                    continue
                return False
            except (urllib.error.URLError, OSError, ValueError):
                return False
        return False

    # -- inbound ----------------------------------------------------------

    def poll(self, cursors: dict[str, str], timeout: int = 10
             ) -> tuple[list[dict[str, Any]], dict[str, str]]:
        if not self.can_read():
            return [], cursors
        token = str(self.config["token"])
        table = self.config.get("channels") or {}
        names = {str(v.get("id")): k for k, v in table.items() if v.get("id")}
        fresh: list[dict[str, Any]] = []
        moved = dict(cursors)

        for channel_id in self._read_ids():
            # Per-channel cursors: one shared cursor skips whichever channel's
            # snowflakes happen to sort lower.
            after = cursors.get(channel_id)
            url = f"{API}/channels/{channel_id}/messages?limit=50"
            if after:
                url += f"&after={after}"
            try:
                messages = http(url, headers={"Authorization": f"Bot {token}"},
                                timeout=timeout)
            except (urllib.error.URLError, OSError, ValueError):
                continue
            if not isinstance(messages, list) or not messages:
                continue
            moved[channel_id] = max(str(m.get("id") or "0") for m in messages)
            channel_name = names.get(channel_id, "ask")
            for message in reversed(messages):          # API returns newest first
                if message.get("webhook_id") or (message.get("author") or {}).get("bot"):
                    continue                            # our own mirror, or another bot
                content = (message.get("content") or "").strip()
                if not content:
                    continue
                author = message.get("author") or {}
                fresh.append(normalise_incoming(
                    platform="discord",
                    message_id=str(message.get("id")),
                    author=str(author.get("global_name") or author.get("username") or "someone"),
                    author_id=str(author.get("id") or ""),
                    body=content,
                    channel=channel_name,
                    sent_at=str(message.get("timestamp") or ""),
                ))
        return fresh, moved

    # -- diagnosis --------------------------------------------------------

    def verify(self) -> tuple[bool, str]:
        """Tell 'the bot cannot see the channel' apart from 'the channel is quiet'.

        An empty message list looks identical to a permission failure, so probe
        the channel endpoint directly and report what actually went wrong.
        """
        token = self.config.get("token")
        ids = self._read_ids()
        if not token:
            return False, "no bot token — outbound mirror only, humans cannot reach the agents"
        if not ids:
            return False, "no readable channel configured (need an `ask` channel id)"
        try:
            channel = http(f"{API}/channels/{ids[0]}",
                           headers={"Authorization": f"Bot {token}"})
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                return False, "the bot token was rejected (wrong, or reset since)"
            if exc.code in (403, 404):
                return False, ("the bot cannot see that channel — it is probably not in "
                               "the server, or lacks View Channel")
            return False, f"Discord returned HTTP {exc.code}"
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return False, f"could not reach Discord ({exc})"
        return True, f"reading #{(channel or {}).get('name', '?')}"

    # Named, because a hand-computed bitmask is unreviewable and this one was
    # wrong: it granted MANAGE_MESSAGES -- letting the bot delete other people's
    # messages -- while omitting VIEW_CHANNEL, so the invited bot could not read
    # the one channel it exists to read. Nobody spots that in a integer.
    PERM = {
        "MANAGE_CHANNELS":      1 << 4,
        "VIEW_CHANNEL":         1 << 10,
        "SEND_MESSAGES":        1 << 11,
        "READ_MESSAGE_HISTORY": 1 << 16,
        "MANAGE_WEBHOOKS":      1 << 29,
    }
    # What the bot needs once it is running. Deliberately no MANAGE_MESSAGES:
    # nothing here ever deletes anything, so asking for it is asking a server
    # owner to trust us with a capability we do not use.
    RUNTIME_PERMS = ("VIEW_CHANNEL", "SEND_MESSAGES", "READ_MESSAGE_HISTORY")
    # Additionally needed only while `provision` creates the channels. Safe to
    # revoke afterwards, and `colab chat invite --minimal` prints the link that
    # does not ask for them.
    SETUP_PERMS = ("MANAGE_CHANNELS", "MANAGE_WEBHOOKS")

    def permissions(self, *, provision: bool = True) -> int:
        wanted = self.RUNTIME_PERMS + (self.SETUP_PERMS if provision else ())
        return sum(self.PERM[name] for name in wanted)

    def invite_hint(self, *, provision: bool = True) -> str:
        app = self.config.get("application_id")
        if not app:
            return ""
        return (f"https://discord.com/oauth2/authorize?client_id={app}"
                f"&scope=bot&permissions={self.permissions(provision=provision)}")

    # -- provisioning -----------------------------------------------------

    def provision(self, guild_id: str, *, category: str = "AgentColab",
                  only: list[str] | None = None) -> dict[str, Any]:
        """Create the category, every channel, and a webhook each. Idempotent.

        Re-running adopts whatever already exists by name rather than making a
        second copy, so a half-finished setup is fixed by running it again.
        """
        token = self.config.get("token")
        if not token:
            raise ChatError("provisioning needs a bot token with Manage Channels")
        headers = {"Authorization": f"Bot {token}", "Content-Type": "application/json"}
        guild_id = str(guild_id).strip()

        try:
            existing = http(f"{API}/guilds/{guild_id}/channels", headers=headers, timeout=20)
        except urllib.error.HTTPError as exc:
            raise ChatError(
                "cannot list channels in that server — check the bot is in it and the "
                f"server id is right (HTTP {exc.code})")
        by_name = {str(c.get("name")): c for c in (existing or [])}

        parent = by_name.get(records.slug(category))
        if parent is None or parent.get("type") != 4:
            parent = http(f"{API}/guilds/{guild_id}/channels", method="POST", headers=headers,
                          data=json.dumps({"name": records.slug(category), "type": 4}).encode(),
                          timeout=20)
            time.sleep(0.4)

        known = self.channels()
        wanted = only or list(known)
        table: dict[str, Any] = dict(self.config.get("channels") or {})
        for logical in wanted:
            if logical not in known:
                continue
            name = records.slug(logical)
            channel = by_name.get(name)
            if channel is None:
                channel = http(
                    f"{API}/guilds/{guild_id}/channels", method="POST", headers=headers,
                    data=json.dumps({
                        "name": name, "type": 0,
                        "parent_id": (parent or {}).get("id"),
                        "topic": (known[logical].get("purpose") or "")[:1024],
                    }).encode(), timeout=20)
                time.sleep(0.4)
            entry: dict[str, Any] = {"id": str(channel.get("id"))}
            try:
                hook = http(f"{API}/channels/{channel['id']}/webhooks", method="POST",
                            headers=headers,
                            data=json.dumps({"name": f"colab-{logical}"}).encode(), timeout=20)
                entry["webhook"] = (f"https://discord.com/api/webhooks/"
                                    f"{hook['id']}/{hook['token']}")
                time.sleep(0.4)
            except (urllib.error.HTTPError, urllib.error.URLError, KeyError, TypeError):
                # No Manage Webhooks: posting still works through the bot token.
                pass
            table[logical] = entry
        self.config["channels"] = table
        self.config["guild"] = guild_id
        return table
