"""A WebSocket client small enough to read: RFC 6455 text frames over one socket.

The wake listener (`agentcolab/wake.py`) holds a connection to the relay's
agent stream for hours, and the hosted relay speaks WebSocket only. The
standard library has no client and a dependency is not on the table, so this
is the subset the contract needs (docs/canvas-contract.md §5 and §10.5): the
opening handshake with its accept-key check, masked text frames out, text
frames in whether or not the server fragmented them, a `ping` answered with a
`pong`, and a `close` answered and reported as `EOFError`. Binary frames are
dropped -- the relay never sends one -- and a message over `MAX_MESSAGE`
closes the connection rather than growing a buffer without bound.

Lifted from the probe in tests/canvas_live.py and hardened: that one trusted
the server's length field, never checked `Sec-WebSocket-Accept`, and had no
timeout on a read, which is fine for a sixty-second probe and not for a
process that is meant to survive a laptop's afternoon.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import os
import socket
import ssl
import urllib.parse
from typing import Any

USER_AGENT = "AgentColab/1 (+https://github.com/AgentColab/AgentColab)"
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"       # RFC 6455 §1.3, fixed by the spec
MAX_MESSAGE = 16 * 1024 * 1024
MAX_HEADER = 64 * 1024

OP_CONTINUATION, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


class HandshakeError(OSError):
    """The server did not upgrade: `status` is what it answered instead."""

    def __init__(self, status: int, detail: str = "") -> None:
        super().__init__(f"WebSocket upgrade answered {status}: {detail}"[:300])
        self.status = status
        self.detail = detail


def accept_key(key: str) -> str:
    """What the server must echo for a given `Sec-WebSocket-Key` (RFC 6455 §4.2.2)."""
    digest = hashlib.sha1((key + GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def mask(payload: bytes, key: bytes) -> bytes:
    """XOR with a 4-byte key; as bytes-to-int arithmetic because a per-byte
    comprehension is thirty times slower and this runs on every frame."""
    if not payload:
        return b""
    repeats = len(payload) // 4 + 1
    stream = (key * repeats)[:len(payload)]
    return (int.from_bytes(payload, "big") ^ int.from_bytes(stream, "big")).to_bytes(len(payload), "big")


def encode_frame(opcode: int, payload: bytes, *, masked: bool = True, key: bytes | None = None) -> bytes:
    """One frame, FIN set. Clients MUST mask (§5.3) so a proxy never sees a
    repeating plaintext pattern; `masked=False` exists so a test can build the
    server's side of a conversation."""
    head = bytes([0x80 | (opcode & 0x0F)])
    length = len(payload)
    flag = 0x80 if masked else 0x00
    if length < 126:
        head += bytes([flag | length])
    elif length < 65536:
        head += bytes([flag | 126]) + length.to_bytes(2, "big")
    else:
        head += bytes([flag | 127]) + length.to_bytes(8, "big")
    if not masked:
        return head + payload
    key = key if key is not None and len(key) == 4 else os.urandom(4)
    return head + key + mask(payload, key)


def decode_frame(data: bytes) -> tuple[int, bool, bytes, int]:
    """(opcode, fin, payload, bytes consumed) for the frame at the start of `data`.

    Raises `IncompleteFrame` when more bytes are needed and `ValueError` on a
    frame the protocol forbids (reserved bits, an oversize length).
    """
    if len(data) < 2:
        raise IncompleteFrame(2)
    b0, b1 = data[0], data[1]
    if b0 & 0x70:
        raise ValueError("reserved bits set without an extension")
    opcode, fin = b0 & 0x0F, bool(b0 & 0x80)
    length = b1 & 0x7F
    offset = 2
    if length == 126:
        if len(data) < 4:
            raise IncompleteFrame(4)
        length, offset = int.from_bytes(data[2:4], "big"), 4
    elif length == 127:
        if len(data) < 10:
            raise IncompleteFrame(10)
        length, offset = int.from_bytes(data[2:10], "big"), 10
    if length > MAX_MESSAGE:
        raise ValueError(f"frame of {length} bytes is over the {MAX_MESSAGE} cap")
    key = None
    if b1 & 0x80:
        if len(data) < offset + 4:
            raise IncompleteFrame(offset + 4)
        key, offset = data[offset:offset + 4], offset + 4
    end = offset + length
    if len(data) < end:
        raise IncompleteFrame(end)
    payload = data[offset:end]
    if key is not None:
        payload = mask(payload, key)
    return opcode, fin, payload, end


class IncompleteFrame(Exception):
    """`need` is how many bytes the frame occupies at least."""

    def __init__(self, need: int) -> None:
        super().__init__(need)
        self.need = need


class WebSocket:
    """One open connection. `recv_text` blocks up to `timeout` seconds."""

    def __init__(self, sock: socket.socket, buffered: bytes = b"") -> None:
        self.sock = sock
        self.buf = bytearray(buffered)
        self.closed = False

    # -- wire

    def _fill(self, timeout: float | None) -> None:
        self.sock.settimeout(timeout)
        try:
            chunk = self.sock.recv(65536)
        except socket.timeout:
            raise TimeoutError("no frame from the server within the timeout") from None
        if not chunk:
            self.closed = True
            raise EOFError("the server closed the connection")
        self.buf.extend(chunk)

    def _send(self, opcode: int, payload: bytes) -> None:
        if self.closed:
            raise EOFError("connection already closed")
        self.sock.sendall(encode_frame(opcode, payload))

    def send_text(self, text: str) -> None:
        self._send(OP_TEXT, text.encode("utf-8"))

    def ping(self, payload: bytes = b"") -> None:
        self._send(OP_PING, payload[:125])

    def recv_text(self, timeout: float | None = None) -> str:
        """The next complete text message.

        Answers pings, ignores pongs and binary frames, and on a close frame
        replies in kind and raises `EOFError`. Raises `TimeoutError` when no
        frame arrives within `timeout`; the connection is still usable then.
        """
        message = bytearray()
        in_text = False                 # a fragmented text message is under way
        skipping_binary = False         # a fragmented binary message is being dropped
        while True:
            try:
                opcode, fin, payload, used = decode_frame(bytes(self.buf))
            except IncompleteFrame:
                self._fill(timeout)
                continue
            except ValueError as exc:
                self.close(1002, str(exc)[:100])
                raise EOFError(f"protocol error: {exc}") from None
            del self.buf[:used]
            if opcode == OP_PING:
                with contextlib.suppress(OSError):
                    self._send(OP_PONG, payload)
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                with contextlib.suppress(OSError):
                    self._send(OP_CLOSE, payload[:2])
                self.closed = True
                with contextlib.suppress(OSError):
                    self.sock.close()
                raise EOFError("close frame")
            if opcode == OP_BINARY:
                skipping_binary = not fin
                continue
            if opcode == OP_CONTINUATION:
                if skipping_binary:
                    skipping_binary = not fin
                    continue
                if not in_text:
                    self.close(1002, "continuation without a first fragment")
                    raise EOFError("protocol error: stray continuation frame")
            elif opcode == OP_TEXT:
                if in_text:
                    self.close(1002, "text frame inside a fragmented message")
                    raise EOFError("protocol error: interleaved messages")
                in_text = True
            else:
                self.close(1002, f"unknown opcode {opcode}")
                raise EOFError(f"protocol error: opcode {opcode}")
            message.extend(payload)
            if len(message) > MAX_MESSAGE:
                self.close(1009, "message too big")
                raise EOFError("message over the size cap")
            if fin:
                return message.decode("utf-8", "replace")

    def close(self, code: int = 1000, reason: str = "") -> None:
        """Send a close frame and drop the socket. Safe to call twice."""
        if not self.closed:
            self.closed = True
            with contextlib.suppress(OSError):
                self.sock.sendall(encode_frame(OP_CLOSE, code.to_bytes(2, "big") + reason.encode("utf-8")[:120]))
        with contextlib.suppress(OSError):
            self.sock.close()


def _read_head(sock: socket.socket) -> bytes:
    """Read up to and including the blank line that ends the HTTP response."""
    head = bytearray()
    while b"\r\n\r\n" not in head:
        chunk = sock.recv(4096)
        if not chunk:
            raise HandshakeError(0, "connection closed during the handshake")
        head.extend(chunk)
        if len(head) > MAX_HEADER:
            raise HandshakeError(0, "response headers over 64 KiB")
    return bytes(head)


def _parse_head(head: bytes) -> tuple[int, dict[str, str]]:
    lines = head.decode("iso-8859-1").split("\r\n")
    parts = lines[0].split(" ", 2)
    try:
        status = int(parts[1])
    except (IndexError, ValueError):
        raise HandshakeError(0, f"not an HTTP response: {lines[0][:80]!r}") from None
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            name, value = line.split(":", 1)
            headers[name.strip().lower()] = value.strip()
    return status, headers


def connect(url: str, headers: dict[str, str] | None = None, timeout: float = 10) -> WebSocket:
    """Open `ws://` or `wss://` (`http(s)://` is accepted and mapped) and complete the upgrade.

    `headers` ride on the upgrade request -- the agent stream wants
    `Authorization`. Raises `HandshakeError` on anything but `101`, `OSError`
    on network failure. `timeout` covers the connect and the handshake; reads
    afterwards take their own.
    """
    parts = urllib.parse.urlsplit(url)
    secure = parts.scheme in ("wss", "https")
    if parts.scheme not in ("ws", "wss", "http", "https") or not parts.hostname:
        raise ValueError(f"not a WebSocket URL: {url!r}")
    port = parts.port or (443 if secure else 80)
    sock = socket.create_connection((parts.hostname, port), timeout)
    try:
        if secure:
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=parts.hostname)
        sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        host = parts.hostname if port in (80, 443) else f"{parts.hostname}:{port}"
        request = [f"GET {target} HTTP/1.1", f"Host: {host}", "Upgrade: websocket", "Connection: Upgrade",
                   f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13", f"User-Agent: {USER_AGENT}"]
        for name, value in (headers or {}).items():
            if "\r" in value or "\n" in value:
                raise ValueError(f"header {name!r} contains a line break")
            request.append(f"{name}: {value}")
        sock.sendall(("\r\n".join(request) + "\r\n\r\n").encode("utf-8"))
        head = _read_head(sock)
        raw_head, rest = head.split(b"\r\n\r\n", 1)
        status, response = _parse_head(raw_head)
        if status != 101:
            body = rest[:200].decode("utf-8", "replace")
            raise HandshakeError(status, body or response.get("content-type", ""))
        if response.get("upgrade", "").lower() != "websocket":
            raise HandshakeError(status, "101 without Upgrade: websocket")
        if response.get("sec-websocket-accept") != accept_key(key):
            # The one thing that tells a WebSocket server from a proxy that
            # answered 101 to something else.
            raise HandshakeError(status, "Sec-WebSocket-Accept does not match the key")
        return WebSocket(sock, rest)
    except BaseException:
        with contextlib.suppress(OSError):
            sock.close()
        raise


def frame_text(text: str, *, masked: bool = False) -> bytes:
    """A text frame as a server would send it (unmasked): for tests and fakes."""
    return encode_frame(OP_TEXT, text.encode("utf-8"), masked=masked)


def server_accept(request_head: bytes) -> tuple[dict[str, str], bytes]:
    """For a test's tiny server: parse a client's upgrade request and build the 101.

    Returns (request headers, response bytes). Not a server -- the package
    ships no WebSocket server -- only enough for the client to be tested
    against real frames rather than against itself.
    """
    lines = request_head.decode("iso-8859-1").split("\r\n")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            name, value = line.split(":", 1)
            headers[name.strip().lower()] = value.strip()
    key = headers.get("sec-websocket-key", "")
    response = ("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept_key(key)}\r\n\r\n")
    return headers, response.encode("ascii")


__all__: list[Any] = ["WebSocket", "HandshakeError", "connect", "accept_key", "mask", "encode_frame",
                      "decode_frame", "frame_text", "server_accept", "MAX_MESSAGE"]
