"""Slack adapter — same contract, different verbs.

One bot token drives everything: `chat.postMessage` out, `conversations.history`
in, `conversations.create` to provision. Incoming webhooks are supported as a
lower-permission outbound path for people who cannot get a bot approved, which
in a lot of workplaces is the difference between using this and not.

Slack's per-channel posting limit is roughly one message a second with short
bursts tolerated, so the same 429-aware backoff applies. Slack answers 200 with
`{"ok": false}` on application errors rather than an HTTP status, so every
response body is checked — a mirror that silently drops every message while
reporting success is worse than one that is plainly off.
"""

from __future__ import annotations

import json
import time
import urllib.error
from typing import Any

from .. import records
from .base import (CHANNELS, Adapter, ChatError, Event, http, inputs,
                   normalise_incoming, resolve)

API = "https://slack.com/api"

TOPICS = {name: text for name, (_, text) in CHANNELS.items()}


class Slack(Adapter):
    name = "slack"

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
        # Every declared input channel, not just the built-in `ask`: a custom
        # `dir: "in"` channel used to be provisioned and then never polled.
        return [str((table.get(name) or {}).get("id")) for name in inputs(self.config)
                if (table.get(name) or {}).get("id")]

    # -- outbound ---------------------------------------------------------

    def post(self, event: Event) -> bool:
        route = self.route(event.channel)
        text = event.text(limit=3800)           # Slack allows more than Discord
        webhook = route.get("webhook")
        if webhook:
            payload = json.dumps({"text": text, "mrkdwn": True}).encode()
            return self._send(webhook, payload, {"Content-Type": "application/json"})
        token = self.config.get("token")
        channel = route.get("id")
        if not (token and channel):
            return False
        payload = json.dumps({
            "channel": channel,
            "text": text,
            # Automation must never page a workspace.
            "link_names": False,
            "unfurl_links": False,
            "unfurl_media": False,
        }).encode()
        return self._send(f"{API}/chat.postMessage", payload, {
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {token}",
        })

    def _send(self, url: str, payload: bytes, headers: dict[str, str],
              attempts: int = 3) -> bool:
        for attempt in range(attempts):
            try:
                result = http(url, data=payload, method="POST", headers=headers)
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and attempt < attempts - 1:
                    time.sleep(min(float(exc.headers.get("Retry-After") or 1), 5.0))
                    continue
                return False
            except (urllib.error.URLError, OSError, ValueError):
                return False
            # An incoming webhook answers with the bare string "ok".
            if result is None or result is True:
                return True
            if isinstance(result, str):
                return result.strip().lower() == "ok"
            if isinstance(result, dict):
                if result.get("ok"):
                    return True
                if result.get("error") == "ratelimited" and attempt < attempts - 1:
                    time.sleep(1.5)
                    continue
                return False
            return True
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
            oldest = cursors.get(channel_id)
            url = f"{API}/conversations.history?channel={channel_id}&limit=50"
            if oldest:
                url += f"&oldest={oldest}"
            try:
                result = http(url, headers={"Authorization": f"Bearer {token}"},
                              timeout=timeout)
            except (urllib.error.URLError, OSError, ValueError):
                continue
            if not isinstance(result, dict) or not result.get("ok"):
                continue
            messages = result.get("messages") or []
            if not messages:
                continue
            moved[channel_id] = max(str(m.get("ts") or "0") for m in messages)
            channel_name = names.get(channel_id, "ask")
            for message in reversed(messages):          # newest first
                if message.get("bot_id") or message.get("subtype"):
                    continue                            # our own mirror, joins, edits
                content = (message.get("text") or "").strip()
                if not content:
                    continue
                fresh.append(normalise_incoming(
                    platform="slack",
                    message_id=str(message.get("ts")),
                    author=str(message.get("user") or "someone"),
                    author_id=str(message.get("user") or ""),
                    body=content,
                    channel=channel_name,
                ))
        return fresh, moved

    # -- diagnosis --------------------------------------------------------

    def verify(self) -> tuple[bool, str]:
        token = self.config.get("token")
        ids = self._read_ids()
        if not token:
            return False, "no bot token — outbound mirror only, humans cannot reach the agents"
        if not ids:
            return False, "no readable channel configured (need an `ask` channel id)"
        try:
            result = http(f"{API}/conversations.info?channel={ids[0]}",
                          headers={"Authorization": f"Bearer {token}"})
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return False, f"could not reach Slack ({exc})"
        if not isinstance(result, dict) or not result.get("ok"):
            error = (result or {}).get("error", "unknown")
            hint = {
                "invalid_auth": "the bot token was rejected",
                "not_in_channel": "invite the bot: /invite @your-app in that channel",
                "channel_not_found": "wrong channel id, or the bot cannot see it",
                "missing_scope": "the app is missing channels:history / channels:read",
            }.get(str(error), f"Slack said {error!r}")
            return False, hint
        return True, f"reading #{(result.get('channel') or {}).get('name', '?')}"

    def invite_hint(self) -> str:
        return ("https://api.slack.com/apps → your app → OAuth & Permissions. "
                "Bot scopes: chat:write, channels:read, channels:history, "
                "channels:manage (only for `colab chat provision`).")

    # -- provisioning -----------------------------------------------------

    def provision(self, workspace: str = "", *, prefix: str = "colab",
                  only: list[str] | None = None) -> dict[str, Any]:
        """Create every channel and invite the bot. Idempotent by name."""
        token = self.config.get("token")
        if not token:
            raise ChatError("provisioning needs a bot token with channels:manage")
        headers = {"Authorization": f"Bearer {token}",
                   "Content-Type": "application/json; charset=utf-8"}

        existing: dict[str, str] = {}
        cursor = ""
        for _ in range(10):                     # bounded: a workspace can be large
            url = f"{API}/conversations.list?limit=200&exclude_archived=true"
            if cursor:
                url += f"&cursor={cursor}"
            try:
                page = http(url, headers=headers, timeout=20)
            except (urllib.error.URLError, OSError, ValueError) as exc:
                raise ChatError(f"could not list channels ({exc})")
            if not isinstance(page, dict) or not page.get("ok"):
                raise ChatError(f"could not list channels: {(page or {}).get('error')}")
            for channel in page.get("channels") or []:
                existing[str(channel.get("name"))] = str(channel.get("id"))
            cursor = ((page.get("response_metadata") or {}).get("next_cursor") or "")
            if not cursor:
                break

        table: dict[str, Any] = dict(self.config.get("channels") or {})
        known = self.channels()
        for logical in (only or list(known)):
            if logical not in known:
                continue
            name = records.slug(f"{prefix}-{logical}")
            channel_id = existing.get(name)
            if not channel_id:
                created = http(f"{API}/conversations.create", method="POST", headers=headers,
                               data=json.dumps({"name": name, "is_private": False}).encode(),
                               timeout=20)
                if not isinstance(created, dict) or not created.get("ok"):
                    raise ChatError(f"could not create #{name}: "
                                    f"{(created or {}).get('error')}")
                channel_id = str((created.get("channel") or {}).get("id"))
                time.sleep(0.6)
            try:
                http(f"{API}/conversations.setTopic", method="POST", headers=headers,
                     data=json.dumps({"channel": channel_id,
                                      "topic": (known[logical].get("purpose") or "")[:250]}).encode(),
                     timeout=15)
            except (urllib.error.URLError, OSError, ValueError):
                pass
            table[logical] = {"id": channel_id, "name": name}
        self.config["channels"] = table
        return table
