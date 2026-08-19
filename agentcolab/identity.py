"""Who wrote this record, and can we prove it.

Public collaboration turns identity into the load-bearing part. Anyone can
push a file that says `"agent": "maintainer"`. What they cannot do is produce a
valid SSH signature over it from a key that GitHub publishes for that account.

The whole mechanism is `ssh-keygen -Y sign` / `-Y verify`, which is the same
primitive git itself uses for `gpg.format = ssh`. That buys three things with
zero dependencies:

* Every developer already has an SSH key, because that is how they push.
* GitHub publishes every account's public keys at `https://github.com/<u>.keys`,
  so the allowed-signers list builds itself from the fork graph.
* Verification is offline once the keys are cached.

Nothing here is mandatory. An unsigned agent still works; it is labelled
`unverified` everywhere it appears, and a project can require signatures with
`trust.require_signatures`. Degrading is always allowed; lying is not.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterable

from . import records
from .records import iso, now, parse_iso

NAMESPACE = "colab"
USER_AGENT = "AgentColab/1 (+https://github.com/AgentColab/AgentColab)"

# Ordered worst-to-best so `max()` over a set of levels is the effective one.
TRUST_ORDER = ("chat", "unverified", "pinned", "verified", "member", "maintainer")

KEY_CANDIDATES = (
    "id_ed25519", "id_ed25519_sk", "id_ecdsa", "id_ecdsa_sk", "id_rsa",
)


class IdentityError(RuntimeError):
    pass


def have_ssh_keygen() -> bool:
    return shutil.which("ssh-keygen") is not None


def trust_rank(level: str | None) -> int:
    try:
        return TRUST_ORDER.index(str(level or "unverified"))
    except ValueError:
        return 0


def best_trust(*levels: str | None) -> str:
    return max((str(x or "unverified") for x in levels), key=trust_rank)


# ---------------------------------------------------------------- key discovery


def _is_ssh_pubkey(text: str) -> bool:
    return text.strip().startswith(("ssh-", "sk-ssh-", "sk-ecdsa-", "ecdsa-"))


def find_signing_key(repo_root: Path | None = None) -> tuple[Path | None, bool]:
    """(public key path, use_agent). Never reads or copies a private key.

    Preference order: what the user configured for colab, then what git already
    signs commits with, then the conventional key names. Ed25519 first because
    it is what `ssh-keygen -t ed25519` has produced by default for years.
    """
    configured = os.environ.get("AGENTCOLAB_SIGNING_KEY")
    if configured:
        path = Path(configured).expanduser()
        pub = path if path.suffix == ".pub" else path.with_suffix(".pub")
        if pub.is_file():
            return pub, not path.with_suffix("").is_file() and not path.is_file()

    if repo_root:
        raw = records.run(["git", "-C", str(repo_root), "config", "--get", "user.signingkey"])
        if raw:
            candidate = Path(raw).expanduser()
            if candidate.is_file() and _is_ssh_pubkey(_safe_read(candidate)):
                return candidate, False
            pub = candidate.with_suffix(".pub")
            if pub.is_file():
                return pub, False

    ssh_dir = Path.home() / ".ssh"
    for name in KEY_CANDIDATES:
        pub = ssh_dir / f"{name}.pub"
        if pub.is_file():
            return pub, not (ssh_dir / name).is_file()
    return None, False


def _safe_read(path: Path) -> str:
    try:
        return path.read_text(errors="ignore", encoding="utf-8")
    except OSError:
        return ""


def public_key(repo_root: Path | None = None) -> str:
    """This machine's public key, normalised to 'type base64' with no comment."""
    pub, _ = find_signing_key(repo_root)
    if not pub:
        return ""
    return normalise_pubkey(_safe_read(pub))


def normalise_pubkey(line: str) -> str:
    parts = (line or "").strip().split()
    if len(parts) < 2 or not _is_ssh_pubkey(parts[0]):
        return ""
    return f"{parts[0]} {parts[1]}"


def key_fingerprint(pubkey: str) -> str:
    """Short, stable, human-comparable. Not a security boundary on its own."""
    normalised = normalise_pubkey(pubkey)
    if not normalised:
        return ""
    return "SHA256:" + hashlib.sha256(normalised.encode()).hexdigest()[:16]


# ---------------------------------------------------------------- signing


def canonical(payload: dict[str, Any]) -> bytes:
    """Exactly the bytes that get signed and verified.

    Signature-carrying fields are excluded, and the rest is serialised with
    sorted keys and no whitespace so two machines producing the same record
    produce the same bytes.
    """
    # Everything the signature itself carries, plus every field the read path
    # attaches after the fact. Reading a record adds `_source`, `_path` and
    # friends; if those reached the canonical form, a record would verify on the
    # machine that wrote it and nowhere else -- which is the worst possible
    # failure, because it looks like it works.
    body = {k: v for k, v in payload.items()
            if k not in ("sig", "sig_by", "sig_alg") and not k.startswith("_")}
    return json.dumps(body, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode()


def sign(payload: dict[str, Any], repo_root: Path | None = None) -> dict[str, Any]:
    """Return payload with `sig` attached, or unchanged if we cannot sign.

    Signing is best-effort by design: a contributor without an SSH key must
    still be able to take part, they just carry less trust. A hard failure here
    would make the tool refuse to work on exactly the machines least able to
    fix it.
    """
    pub, use_agent = find_signing_key(repo_root)
    if not pub or not have_ssh_keygen():
        return payload
    private = pub.with_suffix("")
    key_arg = str(private if (private.is_file() and not use_agent) else pub)
    args = ["ssh-keygen", "-Y", "sign", "-n", NAMESPACE, "-f", key_arg, "-q"]
    if use_agent or not private.is_file():
        args.append("-U")
    try:
        proc = subprocess.run(
            args, input=canonical(payload), capture_output=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return payload
    if proc.returncode != 0 or not proc.stdout:
        return payload
    out = dict(payload)
    out["sig"] = proc.stdout.decode(errors="ignore").strip()
    out["sig_by"] = normalise_pubkey(_safe_read(pub))
    out["sig_alg"] = "ssh"
    return out


def principals_for(owner: str, roster: dict[str, Any],
                   pins: dict[str, Any] | None = None) -> set[str]:
    """Which principals are allowed to sign as this agent.

    This is the binding that makes a signature mean something. Without it,
    verification only answers "did *a* key we know sign this", and any
    participant holding a trusted key can publish a record attributed to
    somebody else and have it render as verified.

    Precedence: an explicit roster binding, then the key pinned for that name on
    first sight. If neither exists the name is unclaimed, and first use pins it.
    """
    name = records.slug(str(owner))
    out: set[str] = set()
    for entry in (roster.get("members") or []):
        if not isinstance(entry, dict):
            continue
        github = str(entry.get("github") or "")
        agent = str(entry.get("agent") or "")
        names = {records.slug(a) for a in (entry.get("agents") or []) if a}
        if agent:
            names.add(records.slug(agent))
        if github and not names:
            names.add(records.slug(github))
        if name in names:
            if github:
                out.add(github)
            if agent:
                out.add(agent)
    if not out and (pins or {}).get(name):
        out.add(name)
    return out


def verify(payload: dict[str, Any], allowed: dict[str, list[str]]) -> tuple[bool, str]:
    """Check a record's signature against a principal -> [pubkeys] table.

    Returns (ok, principal). `allowed` maps a principal — a GitHub login, or an
    agent name for pinned local keys — to the public keys it may sign with.

    Callers must pass only the principals permitted to sign as this record's
    owner; see `principals_for`. Handing it the whole table answers a weaker
    question than it appears to.
    """
    signature = payload.get("sig")
    pubkey = normalise_pubkey(str(payload.get("sig_by") or ""))
    if not signature or not pubkey or not have_ssh_keygen():
        return False, ""

    principal = ""
    for who, keys in (allowed or {}).items():
        if any(normalise_pubkey(k) == pubkey for k in keys or []):
            principal = who
            break
    if not principal:
        return False, ""

    with tempfile.TemporaryDirectory(prefix="colab-verify-") as tmp:
        signers = Path(tmp) / "allowed_signers"
        signers.write_text(f"{principal} {pubkey}\n", encoding="utf-8")
        sig_path = Path(tmp) / "record.sig"
        sig_path.write_text(str(signature).strip() + "\n", encoding="utf-8")
        try:
            proc = subprocess.run(
                ["ssh-keygen", "-Y", "verify", "-f", str(signers), "-I", principal,
                 "-n", NAMESPACE, "-s", str(sig_path)],
                input=canonical(payload), capture_output=True, timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            return False, ""
    return proc.returncode == 0, principal


# ---------------------------------------------------------------- key sources

KEYS_TTL_SECONDS = 6 * 3600


def _fetch(url: str, timeout: int = 10) -> str | None:
    """The body, or None if the request failed.

    The distinction matters: "" is a real answer meaning the account publishes
    no keys, while None means we did not get to ask. Collapsing both into ""
    made a network blip look identical to a revocation.
    """
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode(errors="ignore")
    except (urllib.error.URLError, OSError, ValueError):
        return None


def github_keys(login: str, cache_dir: Path, timeout: int = 10,
                *, allow_fetch: bool = True) -> list[str]:
    """Public SSH keys GitHub publishes for an account, cached on disk.

    This is the entire sybil story: a signature verifies against a key that
    github.com/<login>.keys serves, so forging an identity means controlling
    that account. Cached because it is on the read path of every sync.
    """
    login = records.slug(login, limit=39)
    if not login:
        return []
    cache = cache_dir / "keys" / f"{login}.json"
    blob = _read_json(cache) or {}
    stamp = parse_iso(blob.get("fetched_at"))
    if stamp and (now() - stamp).total_seconds() < KEYS_TTL_SECONDS:
        return list(blob.get("keys") or [])
    if not allow_fetch:
        return list(blob.get("keys") or [])   # stale beats blank

    text = _fetch(f"https://github.com/{login}.keys", timeout=timeout)
    if text is None:
        # The fetch failed, which says nothing about the account. Serve whatever
        # we have and do NOT stamp the cache: writing an empty set here made one
        # unlucky moment suppress that account's keys for the whole TTL, and on a
        # machine with no prior cache that silently unverifies them entirely.
        return list(blob.get("keys") or [])
    keys = [normalise_pubkey(line) for line in text.splitlines()]
    keys = [k for k in keys if k]
    if not keys and blob.get("keys"):
        return list(blob["keys"])   # a blank answer must not revoke anyone
    cache.parent.mkdir(parents=True, exist_ok=True)
    _write_json(cache, {"login": login, "keys": keys, "fetched_at": iso()})
    return keys


# ---------------------------------------------------------------- pinning
#
# Most projects will never write a roster, and a system where every peer reads
# `unverified` forever teaches people to ignore the label -- which is worse than
# not having one. So keys are pinned on first sight, exactly like SSH's
# known_hosts: the first key an agent signs with is remembered, and a later
# record signed with a *different* key for the same name is surfaced loudly
# rather than silently accepted.
#
# Pinning proves continuity, not identity. It cannot tell you who somebody is;
# it can tell you that the thing writing as `alice` today is the same thing that
# wrote as `alice` yesterday, which is what catches a takeover. A roster entry
# always outranks a pin.


def pinned_keys(cache_dir: Path) -> dict[str, Any]:
    return _read_json(cache_dir / "pins.json") or {}


def pin(cache_dir: Path, agent: str, pubkey: str, payload: dict[str, Any] | None = None) -> str:
    """Record or check the key an agent signs with. 'new'|'same'|'CHANGED'|'none'.

    When a payload is given, the key is only pinned if that payload actually
    verifies under it. Otherwise anyone could claim a name by publishing a
    record carrying somebody else's public key in `sig_by`, and the pin — the
    thing every later check is measured against — would be a lie from the start.
    """
    key = normalise_pubkey(pubkey)
    if payload is not None and key:
        ok, _ = verify(payload, {records.slug(str(agent)): [key]})
        if not ok:
            return "none"
    name = records.slug(str(agent))
    if not key or not name:
        return "none"
    pins = pinned_keys(cache_dir)
    entry = pins.get(name)
    if entry and entry.get("key") == key:
        return "same"
    if entry:
        history = list(entry.get("history") or [])
        history.append({"key": entry.get("key"), "until": iso()})
        pins[name] = {"key": key, "first_seen": iso(), "history": history[-5:],
                      "changed": True}
        _write_json(cache_dir / "pins.json", pins)
        return "CHANGED"
    pins[name] = {"key": key, "first_seen": iso(), "history": []}
    _write_json(cache_dir / "pins.json", pins)
    return "new"


def build_allowed(roster: dict[str, Any], cache_dir: Path, *,
                  online: bool = True) -> dict[str, list[str]]:
    """principal -> keys, assembled from the roster plus GitHub's published keys.

    Roster entries may pin keys directly (works offline, and for hosts other
    than GitHub); anything with a `github` login also picks up whatever that
    account currently publishes.
    """
    allowed: dict[str, list[str]] = {}
    for entry in (roster.get("members") or []):
        if not isinstance(entry, dict):
            continue
        principal = str(entry.get("github") or entry.get("agent") or "").strip()
        if not principal:
            continue
        keys = [normalise_pubkey(k) for k in (entry.get("keys") or [])]
        if entry.get("github"):
            # Offline means "do no network I/O", not "forget what we already
            # know". Skipping the cache too made every read-path classification
            # ignore GitHub-published keys, so properly rostered members showed
            # as unverified on every surface that reads without syncing.
            keys += github_keys(str(entry["github"]), cache_dir, allow_fetch=online)
        merged = allowed.setdefault(principal, [])
        for key in keys:
            if key and key not in merged:
                merged.append(key)
    for name, entry in pinned_keys(cache_dir).items():
        key = normalise_pubkey(str((entry or {}).get("key") or ""))
        # A pin never overrides a roster entry: if the project has said which
        # keys an identity may use, that is the answer.
        if key and name not in allowed:
            allowed[name] = [key]
    return allowed


def classify(payload: dict[str, Any], roster: dict[str, Any],
             allowed: dict[str, list[str]],
             cache_dir: Path | None = None) -> dict[str, Any]:
    """Attach `_trust` and `_verified` to a record read off the wire.

    Trust is a property of the *signature*, never of what the record claims
    about itself. An unsigned record that says `"role": "maintainer"` is
    `unverified`, and everything downstream renders it that way.
    """
    # Restrict to the keys permitted to sign as this record's owner. Verifying
    # against every known key would confirm only that somebody we trust signed
    # it -- not that they are who the record says they are.
    owner = str(payload.get("agent") or "")
    permitted = principals_for(owner, roster, pinned_keys(cache_dir) if cache_dir else None)
    scoped = {who: keys for who, keys in (allowed or {}).items() if who in permitted}
    if owner and not scoped:
        # Unclaimed name: nothing has bound it yet, so nothing can be proven
        # about it. First use pins it (see session.pin_peers).
        scoped = {}
    ok, principal = verify(payload, scoped)
    out = dict(payload)
    out["_verified"] = bool(ok)
    out["_principal"] = principal if ok else ""
    if not ok:
        out["_trust"] = "chat" if payload.get("source") in ("discord", "slack") else "unverified"
        return out
    rostered = {str(e.get("github") or e.get("agent") or "")
                for e in (roster.get("members") or []) if isinstance(e, dict)}
    role = "verified" if principal in rostered else "pinned"
    for entry in (roster.get("members") or []):
        if not isinstance(entry, dict):
            continue
        if str(entry.get("github") or entry.get("agent") or "") == principal:
            role = str(entry.get("role") or "verified")
            break
    out["_trust"] = role if role in TRUST_ORDER else "verified"
    return out


# ---------------------------------------------------------------- tiny json io


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------- self-report


def describe(repo_root: Path | None = None) -> dict[str, Any]:
    """What this machine can prove about itself, for `colab doctor` and `join`."""
    pub, use_agent = find_signing_key(repo_root)
    key = public_key(repo_root)
    return {
        "ssh_keygen": have_ssh_keygen(),
        "key_path": str(pub) if pub else "",
        "via_agent": bool(use_agent),
        "public_key": key,
        "fingerprint": key_fingerprint(key),
        "can_sign": bool(key and have_ssh_keygen()),
    }


def detect_github_login(repo_root: Path | None = None) -> str:
    """Best-effort GitHub login for this machine, never fatal.

    Ordered by how likely each source is to be the account that will actually
    push: the CLI's own auth, then the remote URL of a fork the user owns.
    """
    env = os.environ.get("AGENTCOLAB_GITHUB_LOGIN")
    if env:
        return records.slug(env, limit=39)
    # Local sources first. `gh api user` is a network round trip that cost ~3s
    # on the join path, and a fork URL usually answers the same question for
    # free. `gh` is the fallback, not the first resort.
    urls: list[str] = []
    if repo_root:
        out = records.run(["git", "-C", str(repo_root), "remote", "-v"], timeout=5)
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 2:
                urls.append(parts[1])
        for url in urls:
            login = github_login_from_url(url)
            if login:
                return login
    # `gh api user` is a network round trip that cost six seconds on the join
    # path. It is only worth paying when this repository is actually on GitHub
    # and the URL did not already answer the question -- on a GitLab or local
    # remote it can only ever come back empty.
    if any("github.com" in u for u in urls) and shutil.which("gh"):
        raw = records.run(["gh", "api", "user", "--jq", ".login"], timeout=6)
        if raw and "\n" not in raw and not raw.startswith("{"):
            return records.slug(raw, limit=39)
    return ""


def github_login_from_url(url: str) -> str:
    text = (url or "").strip()
    if not text:
        return ""
    for prefix in ("https://", "http://", "ssh://", "git@"):
        text = text.removeprefix(prefix)
    text = text.replace(":", "/")
    parts = [p for p in text.split("/") if p]
    if len(parts) >= 3 and "github.com" in parts[0]:
        return records.slug(parts[1], limit=39)
    return ""
