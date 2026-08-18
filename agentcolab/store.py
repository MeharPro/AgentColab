"""The transport: coordination state carried on git refs, from any number of remotes.

Three rules make this safe, and everything else follows from them.

**1. It never touches your checkout.** All state lives in a private repo under
`~/.agentcolab/<repo>/`, driven entirely by git plumbing with no working tree. A hook
cannot disturb your index, your stash, or a rebase in progress. Deleting that
directory is always safe.

**2. It never uses a branch.** State lives at `refs/agentcolab/state`. GitHub Actions
fires on `refs/heads/*` and pull requests; Vercel, Netlify and friends build
every branch push; `git branch -a` and every PR picker enumerate `refs/heads/*`.
A custom ref is invisible to all of them, so a heartbeat every few minutes costs
nothing and pollutes nothing.

**3. One agent owns one directory.** Every record this agent writes lives under
a path named after it. Two agents never write the same path, so the channel
cannot produce a merge conflict — "keep mine, take theirs" is a complete and
correct merge. A push race is resolved by redoing exactly that.

The generalisation past two people is that reads fan out over many *sources*.
Somebody with push access to the shared remote publishes there; somebody with
only a fork publishes to their own fork and everyone fetches it. A source is
scoped: records fetched from a fork are only honoured for the agents that fork's
owner controls, so a fork cannot impersonate the maintainer.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Iterable, Iterator

from . import records
from .records import iso, now, parse_iso

STATE_REF = "refs/agentcolab/state"
LOCAL_BRANCH = "agentcolab-state"

# Record families. Each is a directory of per-agent subdirectories.
KINDS = ("msgs", "claims", "bugs", "findings", "tasks", "reviews", "events")

# Days after which an agent prunes its own settled records. Nobody ever deletes
# anyone else's, so this needs no coordination.
RETENTION_DAYS = 30

try:                                            # POSIX
    import fcntl
except ImportError:                             # Windows
    fcntl = None  # type: ignore[assignment]


class LinkError(RuntimeError):
    pass


# ---------------------------------------------------------------- git


def git(args: list[str], cwd: Path, *, check: bool = True, timeout: int = 60,
        stdin_bytes: bytes | None = None,
        index_file: Path | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    # A hook that stops on a credential prompt would freeze the whole session.
    # Every interactive path is closed: the terminal prompt, the GUI askpass,
    # ssh's own prompt, and the Git Credential Manager. A non-interactive helper
    # (macOS keychain, gh's credential helper) still works, which is the point.
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = "/usr/bin/false" if os.name != "nt" else "cmd /c exit 1"
    env["SSH_ASKPASS"] = env["GIT_ASKPASS"]
    env["GCM_INTERACTIVE"] = "never"
    env.setdefault("GIT_SSH_COMMAND", "ssh -oBatchMode=yes -oConnectTimeout=8")
    # Inherited git state would silently retarget these commands at the user's
    # own repository.
    for leak in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY"):
        env.pop(leak, None)
    # Inherited index state is stripped above because letting it through is how
    # a hook ends up writing into the developer's own repository. An index we
    # chose ourselves is a different thing, and is set after the strip.
    if index_file is not None:
        env["GIT_INDEX_FILE"] = str(index_file)
    try:
        proc = subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True, timeout=timeout,
            env=env, input=stdin_bytes,
            stdin=None if stdin_bytes is not None else subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        raise LinkError(f"git {' '.join(args[:2])} timed out after {timeout}s")
    except OSError as exc:
        raise LinkError(f"cannot run git: {exc}")
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or b"").decode(errors="ignore").strip()
        raise LinkError(f"git {' '.join(args)} failed: {detail}")
    return proc


def gits(args: list[str], cwd: Path, *, check: bool = True, timeout: int = 60) -> str:
    return git(args, cwd, check=check, timeout=timeout).stdout.decode(errors="ignore").strip()


def repo_root(start: Path | None = None) -> Path:
    start = Path(start or os.environ.get("CLAUDE_PROJECT_DIR") or Path.cwd())
    proc = git(["rev-parse", "--show-toplevel"], cwd=start, check=False)
    if proc.returncode != 0:
        raise LinkError(f"{start} is not inside a git repository")
    return Path(proc.stdout.decode(errors="ignore").strip())


def remote_url(root: Path, remote: str) -> str:
    proc = git(["remote", "get-url", remote], cwd=root, check=False)
    if proc.returncode != 0:
        return ""
    return proc.stdout.decode(errors="ignore").strip()


def list_remotes(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in gits(["remote"], cwd=root, check=False).splitlines():
        name = line.strip()
        if name:
            out[name] = remote_url(root, name)
    return out


def canonical_slug(url: str) -> str:
    """Identify the *project*, not the fork.

    Everyone collaborating on one upstream must land in the same state
    directory, whether they cloned it, forked it, or added it as a second
    remote. Dropping the owner segment is what makes that true.
    """
    text = (url or "").strip()
    for prefix in ("https://", "http://", "ssh://", "git+ssh://", "git://", "git@"):
        text = text.removeprefix(prefix)
    text = text.replace(":", "/").removesuffix(".git").strip("/")
    parts = [p for p in text.split("/") if p]
    if len(parts) >= 3:
        return records.slug(f"{parts[0]}-{parts[-1]}", limit=60)
    return records.slug(text, limit=60)


def home_dir(project: str) -> Path:
    base = Path(os.environ.get("AGENTCOLAB_HOME") or (Path.home() / ".agentcolab"))
    return base / project


def can_push(root: Path, remote: str, timeout: int = 20) -> bool:
    """Ask the remote, do not guess. Advertises refs without writing anything."""
    proc = git(["push", "--dry-run", "--porcelain", remote,
                f"{STATE_REF}:{STATE_REF}"], cwd=root, check=False, timeout=timeout)
    if proc.returncode == 0:
        return True
    text = (proc.stderr or b"").decode(errors="ignore").lower()
    # "src refspec does not match any" means we may push but have nothing yet.
    return "does not match any" in text or "src refspec" in text


# ---------------------------------------------------------------- store


class Store:
    """Local record storage plus fan-out over every configured source."""

    def __init__(self, root: Path | None = None, *, project: str | None = None,
                 profile: str | None = None) -> None:
        # Always resolve to the git toplevel: a hook's cwd may be a subdirectory
        # and every path compared here is repo-relative.
        self.repo_root = repo_root(root)
        self.project = project or self._project_slug()
        # Shared per project: the git object store, and the key material every
        # profile on this machine agrees about.
        self.shared = home_dir(self.project)
        self.state = self.shared / "state"      # git repo, no working tree
        # Per profile: identity, unpublished records, cursors, budgets.
        #
        # Keying this on the project alone was wrong, and wrong in the case that
        # matters most: two agents on one machine -- Claude Code in one checkout
        # and Codex in another, or two harnesses in the same directory -- resolve
        # to the same project and would silently share one identity, each
        # overwriting the other's name and records. A profile is the unit of
        # identity here, and a checkout maps to one.
        self.profile = records.slug(profile or self._resolve_profile(), limit=48)
        self.home = self.shared / "p" / self.profile
        self.mine = self.home / "mine"          # our own records, plain files
        self.cache = self.home / "cache"
        self.config_path = self.home / "config.json"
        self.local_path = self.home / "local.json"
        self.home.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------ profiles

    @property
    def _map_path(self) -> Path:
        return self.shared / "checkouts.json"

    def _resolve_profile(self) -> str:
        """Which identity this checkout speaks as.

        Explicit env wins -- that is how two harnesses share one directory. Then
        a remembered mapping from checkout path, so a profile survives being
        renamed or moved. Then a stable hash of the path, which is a working
        default that `join` immediately replaces with the agent's real name.
        """
        explicit = os.environ.get("AGENTCOLAB_PROFILE") or os.environ.get("AGENTCOLAB_AGENT")
        if explicit:
            return str(explicit)
        try:
            key = str(self.repo_root.resolve())
        except OSError:
            key = str(self.repo_root)
        mapping = read_json(self._map_path) or {}
        if isinstance(mapping, dict) and mapping.get(key):
            return str(mapping[key])
        import hashlib
        return "w-" + hashlib.sha256(key.encode()).hexdigest()[:10]

    def bind_profile(self, name: str) -> None:
        """Remember that this checkout is this agent, and move its files along.

        Called by `join` once the agent has a real name, so that `~/.agentcolab`
        is browsable by a human rather than a directory of hashes.
        """
        name = records.slug(name, limit=48)
        if not name or name == self.profile:
            return
        try:
            key = str(self.repo_root.resolve())
        except OSError:
            key = str(self.repo_root)
        mapping = read_json(self._map_path) or {}
        if not isinstance(mapping, dict):
            mapping = {}
        target = self.shared / "p" / name
        if target.exists() and target != self.home:
            # Somebody already owns this profile on this machine. Do not merge
            # two identities: point at it only if it is this same checkout.
            if mapping.get(key) != name:
                raise LinkError(
                    f"profile {name!r} already exists on this machine for a different "
                    f"checkout. Pick another name, or set AGENTCOLAB_PROFILE.")
        elif self.home.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(OSError):
                self.home.rename(target)
        mapping[key] = name
        write_json(self._map_path, mapping)
        self.profile = name
        self.home = target
        self.mine = self.home / "mine"
        self.cache = self.home / "cache"
        self.config_path = self.home / "config.json"
        self.local_path = self.home / "local.json"
        self.home.mkdir(parents=True, exist_ok=True)

    def _project_slug(self) -> str:
        override = os.environ.get("AGENTCOLAB_PROJECT")
        if override:
            return records.slug(override, limit=60)
        remotes = list_remotes(self.repo_root)
        for name in ("upstream", "origin"):
            if remotes.get(name):
                return canonical_slug(remotes[name])
        if remotes:
            return canonical_slug(next(iter(remotes.values())))
        return records.slug(self.repo_root.name, limit=60)

    # ------------------------------------------------------ config

    def config(self) -> dict[str, Any]:
        return read_json(self.config_path) or {}

    def save_config(self, data: dict[str, Any]) -> None:
        write_json(self.config_path, data)

    def local(self) -> dict[str, Any]:
        return read_json(self.local_path) or {}

    def save_local(self, data: dict[str, Any]) -> None:
        write_json(self.local_path, data)

    def update_local(self, **fields: Any) -> None:
        data = self.local()
        data.update(fields)
        self.save_local(data)

    @property
    def agent(self) -> str:
        name = os.environ.get("AGENTCOLAB_AGENT") or self.config().get("agent")
        if not name:
            raise LinkError("this machine has not joined yet — run: colab join")
        # This becomes a path segment. Unslugged, '../someone' would let this
        # machine write into and prune another agent's records.
        return records.slug(str(name))

    @property
    def initialised(self) -> bool:
        return bool(self.config().get("agent"))

    def owned_names(self) -> set[str]:
        """Every name this machine speaks for, including ones it has retired.

        A rename moves our records to a new directory, but the old directory is
        still ours on the shared ref and nobody else is permitted to delete it.
        Carrying former names here means the next publish tidies up after us --
        otherwise every rename leaves a permanent ghost agent in the roster.
        """
        config = self.config()
        names = {records.slug(str(config.get("agent") or ""))}
        for entry in config.get("former_names") or []:
            if isinstance(entry, dict) and entry.get("was"):
                names.add(records.slug(str(entry["was"])))
        return {n for n in names if n}

    def profiles_in_use(self) -> dict[str, str]:
        """profile -> checkout, for every other checkout on this machine."""
        mapping = read_json(self._map_path) or {}
        if not isinstance(mapping, dict):
            return {}
        try:
            me = str(self.repo_root.resolve())
        except OSError:
            me = str(self.repo_root)
        return {str(v): str(k) for k, v in mapping.items() if str(k) != me}

    def sources(self) -> list[dict[str, str]]:
        """Everywhere we read state from, each with the agents it may speak for.

        `scope` is the security boundary of the fork transport. A source with a
        scope only contributes records written by agents that scope authorises;
        without it, anyone with a fork could publish a record claiming to be the
        maintainer and it would be honoured on trust of the URL alone.
        """
        config = self.config()
        out: list[dict[str, str]] = []
        seen: set[str] = set()
        for entry in config.get("sources") or []:
            if not isinstance(entry, dict):
                continue
            url = str(entry.get("url") or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            out.append({
                "name": str(entry.get("name") or records.slug(url, limit=24)),
                "url": url,
                "scope": str(entry.get("scope") or ""),
            })
        return out

    @property
    def push_url(self) -> str:
        return str(self.config().get("push_url") or "")

    # ------------------------------------------------------ locking

    @contextlib.contextmanager
    def lock(self, timeout: int = 30) -> Iterator[None]:
        """Two sessions on one machine must not drive the state repo at once."""
        self.shared.mkdir(parents=True, exist_ok=True)
        path = self.shared / "lock"
        if fcntl is None:                       # Windows: best-effort lockfile
            deadline = time.time() + timeout
            while path.exists() and time.time() < deadline:
                try:
                    if time.time() - path.stat().st_mtime > 120:
                        break                   # a crashed process left it behind
                except OSError:
                    break
                time.sleep(0.2)
            path.write_text(str(os.getpid()))
            try:
                yield
            finally:
                with contextlib.suppress(OSError):
                    path.unlink()
            return
        handle = open(path, "w")
        deadline = time.time() + timeout
        try:
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.time() > deadline:
                        raise LinkError("another colab command holds the lock; try again")
                    time.sleep(0.2)
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(handle, fcntl.LOCK_UN)
            handle.close()

    # ------------------------------------------------------ repo lifecycle

    def ensure_repo(self) -> None:
        if (self.state / ".git").exists():
            return
        self.state.mkdir(parents=True, exist_ok=True)
        # Not bare. `update-index` and `ls-files` -- the plumbing that builds the
        # tree we push -- both refuse to run without a work tree, and they fail
        # in the worst possible way: quietly, so nothing is ever removed and a
        # renamed agent haunts the roster forever.
        #
        # So there is a work tree, and it stays empty. Nothing is ever checked
        # out into it: reads go through `ls-tree`/`cat-file` against fetched
        # refs, writes go through an index file we name explicitly. It exists
        # only to satisfy git, and it is still nowhere near the user's checkout.
        git(["init", "--quiet", "--initial-branch", LOCAL_BRANCH], cwd=self.state)
        git(["config", "user.name", "AgentColab"], cwd=self.state)
        git(["config", "user.email", "agentcolab@localhost"], cwd=self.state)
        git(["config", "commit.gpgsign", "false"], cwd=self.state)
        git(["config", "gc.auto", "512"], cwd=self.state)
        self.mine.mkdir(parents=True, exist_ok=True)
        self.cache.mkdir(parents=True, exist_ok=True)

    def _source_ref(self, name: str) -> str:
        return f"refs/agentcolab/sources/{records.slug(name, limit=24)}"

    def fetch(self, timeout: int = 25) -> dict[str, bool]:
        """Pull every source's state ref. A failure is offline, never fatal."""
        self.ensure_repo()
        result: dict[str, bool] = {}
        for source in self.sources():
            ref = self._source_ref(source["name"])
            proc = git(["fetch", "--quiet", "--no-tags", "--depth", "1", source["url"],
                        f"+{STATE_REF}:{ref}"],
                       cwd=self.state, check=False, timeout=timeout)
            if proc.returncode == 0:
                result[source["name"]] = True
                continue
            text = (proc.stderr or b"").decode(errors="ignore").lower()
            # The ref simply not existing yet is the normal first-run state, and
            # a shallow fetch against a ref with no history is not an error.
            result[source["name"]] = ("couldn't find remote ref" in text
                                      or "does not match any" in text)
        return result

    # ------------------------------------------------------ reading

    def _tree_records(self, ref: str) -> dict[str, dict[str, Any]]:
        listing = git(["ls-tree", "-r", "--name-only", ref], cwd=self.state, check=False)
        if listing.returncode != 0:
            return {}
        paths = [p for p in listing.stdout.decode(errors="ignore").splitlines()
                 if p.endswith(".json")]
        if not paths:
            return {}
        # One batch call instead of one process per record: a busy project has
        # hundreds of records and a hook cannot afford hundreds of forks.
        spec = "".join(f"{ref}:{path}\n" for path in paths).encode()
        proc = git(["cat-file", "--batch"], cwd=self.state, check=False, stdin_bytes=spec)
        if proc.returncode != 0:
            return {}
        out: dict[str, dict[str, Any]] = {}
        blob = proc.stdout
        cursor = 0
        for path in paths:
            newline = blob.find(b"\n", cursor)
            if newline == -1:
                break
            header = blob[cursor:newline].decode(errors="ignore").split()
            cursor = newline + 1
            if len(header) < 3 or header[1] != "blob":
                continue
            size = int(header[2])
            body = blob[cursor:cursor + size]
            cursor += size + 1              # payload is followed by a newline
            try:
                data = json.loads(body.decode(errors="ignore"))
            except ValueError:
                continue
            if isinstance(data, dict):
                out[path] = data
        return out

    def _own_records(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        if not self.mine.is_dir():
            return out
        for path in self.mine.rglob("*.json"):
            data = read_json(path)
            if isinstance(data, dict):
                out[path.relative_to(self.mine).as_posix()] = data
        return out

    @staticmethod
    def _owner_of(path: str) -> str:
        """`msgs/<agent>/<id>.json` and `agents/<agent>.json` both name an owner."""
        parts = path.split("/")
        if len(parts) >= 3 and parts[0] in KINDS:
            return parts[1]
        if len(parts) == 2 and parts[0] == "agents":
            return parts[1].removesuffix(".json")
        return ""

    def view(self, *, refresh: bool = False) -> dict[str, dict[str, Any]]:
        """Everything everyone has published, merged, path -> record.

        Cached to a single JSON file because the edit hook reads this on every
        tool call and cannot afford to shell out to git each time.
        """
        cache_file = self.cache / "view.json"
        if not refresh:
            cached = read_json(cache_file)
            if isinstance(cached, dict) and cached.get("records") is not None:
                merged = dict(cached["records"])
                merged.update(self._own_records())
                return merged

        merged: dict[str, dict[str, Any]] = {}
        for source in self.sources():
            scope = source.get("scope") or ""
            for path, data in self._tree_records(self._source_ref(source["name"])).items():
                owner = self._owner_of(path)
                if not owner:
                    continue
                # A fork may only speak for the agents its owner controls.
                if scope and owner != scope and not owner.startswith(f"{scope}-"):
                    continue
                existing = merged.get(path)
                if existing is None or _newer(data, existing):
                    data = dict(data)
                    data["_source"] = source["name"]
                    merged[path] = data
        self.cache.mkdir(parents=True, exist_ok=True)
        write_json(cache_file, {"built_at": iso(), "records": merged})
        merged.update(self._own_records())
        return merged

    def read_all(self, kind: str, *, refresh: bool = False) -> list[dict[str, Any]]:
        """Every record of one family, from everyone, oldest first."""
        out: list[dict[str, Any]] = []
        for path, data in self.view(refresh=refresh).items():
            if not path.startswith(f"{kind}/"):
                continue
            item = dict(data)
            item.setdefault("agent", self._owner_of(path))
            item["_path"] = path
            out.append(item)
        out.sort(key=lambda item: str(item.get("ts") or item.get("created_at") or ""))
        return out

    def agents(self, *, refresh: bool = False) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for path, data in self.view(refresh=refresh).items():
            if not path.startswith("agents/") or path.count("/") != 1:
                continue
            item = dict(data)
            item.setdefault("agent", self._owner_of(path))
            out.append(item)
        out.sort(key=lambda item: str(item.get("agent") or ""))
        return out

    def peers(self, *, refresh: bool = False) -> list[dict[str, Any]]:
        try:
            me = self.agent
        except LinkError:
            me = ""
        return [a for a in self.agents(refresh=refresh) if a.get("agent") != me]

    def live_peers(self, max_age_seconds: int = 86400, *,
                   refresh: bool = False) -> list[dict[str, Any]]:
        """Peers seen recently. A branch abandoned days ago should not nag."""
        out = []
        for peer in self.peers(refresh=refresh):
            stamp = parse_iso(peer.get("updated_at"))
            if stamp and (now() - stamp).total_seconds() < max_age_seconds:
                out.append(peer)
        return out

    def active_claims(self, *, exclude_agent: str | None = None) -> list[dict[str, Any]]:
        out = []
        for claim in self.read_all("claims"):
            if claim.get("released_at"):
                continue
            if exclude_agent and claim.get("agent") == exclude_agent:
                continue
            expires = parse_iso(claim.get("expires_at"))
            if expires and expires < now():
                continue
            out.append(claim)
        return out

    def stale(self, max_age_seconds: int = 900) -> bool:
        last = parse_iso(self.local().get("last_sync"))
        return last is None or (now() - last).total_seconds() > max_age_seconds

    # ------------------------------------------------------ writing

    def put(self, path: str, payload: dict[str, Any]) -> Path:
        """Write one of our own records. Local, instant, never blocks on git."""
        owner = self._owner_of(path)
        if owner != self.agent:
            raise LinkError(f"refusing to write {path!r}: this agent only owns "
                            f"paths under its own name ({self.agent})")
        target = self.mine / path
        write_json(target, payload)
        self._invalidate()
        return target

    def drop(self, path: str) -> None:
        if self._owner_of(path) != self.agent:
            raise LinkError(f"refusing to delete {path!r}: not ours")
        with contextlib.suppress(OSError):
            (self.mine / path).unlink()
        self._invalidate()

    def _invalidate(self) -> None:
        with contextlib.suppress(OSError):
            (self.cache / "view.json").unlink()

    def adopt_own(self) -> int:
        """Pull back records published under our name that we no longer hold.

        Local state is the source of truth for our own records: a publish lays
        down exactly what is in `mine/`. That is correct, and it has one sharp
        edge -- if `~/.agentcolab` is deleted, or the machine is replaced, or a
        checkout is re-cloned, the next publish would silently withdraw every
        finding and message that agent ever wrote, for everybody.

        So rejoining reclaims them first. It reads only records already signed
        under our own name, so it cannot be used to adopt anyone else's.
        """
        recovered = 0
        for source in self.sources():
            for path, data in self._tree_records(self._source_ref(source["name"])).items():
                if self._owner_of(path) != self.agent:
                    continue
                if (self.mine / path).exists():
                    continue
                clean = {k: v for k, v in data.items() if not k.startswith("_")}
                write_json(self.mine / path, clean)
                recovered += 1
        if recovered:
            self._invalidate()
        return recovered

    def prune_mine(self) -> int:
        """Drop our own settled, expired records. Nobody prunes anyone else's."""
        cutoff = now().timestamp() - RETENTION_DAYS * 86400
        removed = 0
        for path in list(self.mine.rglob("*.json")):
            rel = path.relative_to(self.mine).as_posix()
            family = rel.split("/")[0]
            if family in ("agents", "findings"):
                continue                        # presence is current; lessons do not expire
            data = read_json(path) or {}
            if family == "claims" and not data.get("released_at"):
                continue
            if family == "bugs" and data.get("status") in (None, "", "open"):
                continue
            if family == "tasks" and data.get("state") not in ("done", "dropped"):
                continue
            stamp = parse_iso(data.get("created_at") or data.get("ts"))
            if stamp and stamp.timestamp() < cutoff:
                with contextlib.suppress(OSError):
                    path.unlink()
                    removed += 1
        if removed:
            self._invalidate()
        return removed

    def publish(self, message: str, *, timeout: int = 45) -> tuple[bool, str]:
        """Push our records onto the shared ref.

        Ownership makes the race trivial: take whatever is on the remote, drop
        the paths we own, lay ours back down, retry. Nothing of anyone else's is
        ever rewritten, so there is no merge to get wrong.
        """
        if not self.push_url:
            return False, "no writable remote configured (read-only participant)"
        self.ensure_repo()
        with self.lock():
            self.prune_mine()
            last = ""
            for attempt in range(5):
                proc = git(["fetch", "--quiet", "--no-tags", "--depth", "1", self.push_url,
                            f"+{STATE_REF}:refs/agentcolab/push-base"],
                           cwd=self.state, check=False, timeout=timeout)
                base = "refs/agentcolab/push-base" if proc.returncode == 0 else ""
                base_sha = gits(["rev-parse", "--verify", "--quiet", base], cwd=self.state,
                                check=False) if base else ""
                tree = self._build_tree(base)

                # An orphan commit, force-pushed under a lease.
                #
                # Chaining commits would be the obvious thing and it is wrong: a
                # heartbeat every couple of minutes per agent means the ref's
                # history grows without bound forever, and nobody ever reads it.
                # The ref is a snapshot of *now*, so it is exactly one commit,
                # always. Old objects fall out at gc.
                #
                # Dropping the parent means the push is not a fast-forward, so
                # correctness rests on the lease instead: it succeeds only if the
                # remote is still at the revision we built from. That is a
                # compare-and-swap, which is a stronger guarantee than the
                # fast-forward check it replaces, not a weaker one.
                commit = gits(["commit-tree", tree, "-m", f"colab: {message}"],
                              cwd=self.state)
                lease = f"--force-with-lease={STATE_REF}:{base_sha}" if base_sha else \
                        f"--force-with-lease={STATE_REF}:"
                push = git(["push", "--quiet", lease, self.push_url,
                            f"{commit}:{STATE_REF}"],
                           cwd=self.state, check=False, timeout=timeout)
                if push.returncode == 0:
                    # The push moved the remote, but nothing moved our view of
                    # it. Without this, every read after a publish is computed
                    # from a pre-publish snapshot -- which silently makes the
                    # live-agent roster stale, and a stale roster is how two
                    # agents get handed the same task.
                    for source in self.sources():
                        if source["url"] == self.push_url:
                            git(["update-ref", self._source_ref(source["name"]), commit],
                                cwd=self.state, check=False)
                    self._invalidate()
                    self.update_local(last_push=iso())
                    return True, ""
                last = (push.stderr or push.stdout or b"").decode(errors="ignore").strip()
                lowered = last.lower()
                if not any(marker in lowered for marker in
                           ("rejected", "non-fast-forward", "fetch first", "stale info")):
                    return False, last
                time.sleep(0.4 * (attempt + 1))
            return False, last or "could not publish after 5 attempts"

    def _build_tree(self, base_ref: str) -> str:
        """Remote tree, minus everything we own, plus everything we own now.

        Enumerated with `ls-tree` rather than `ls-files`: the state repo is bare,
        and `ls-files` refuses to run without a work tree. It failed silently,
        which meant nothing was ever removed -- so a renamed agent stayed in the
        roster forever and a deleted record came back on the next publish.
        """
        # A dedicated index, never the repo's own, so a half-finished publish
        # cannot leave state behind that the next one inherits.
        index = self.home / "build-index"
        with contextlib.suppress(OSError):
            index.unlink()
        if base_ref:
            git(["read-tree", base_ref], cwd=self.state, index_file=index)
            owned = self.owned_names()
            listing = git(["ls-tree", "-r", "--name-only", base_ref], cwd=self.state)
            for path in listing.stdout.decode(errors="ignore").splitlines():
                if path and self._owner_of(path) in owned:
                    git(["update-index", "--force-remove", path], cwd=self.state,
                        index_file=index)
        for path, data in self._own_records().items():
            blob = git(["hash-object", "-w", "--stdin"], cwd=self.state,
                       stdin_bytes=json.dumps(data, sort_keys=True, indent=1).encode())
            sha = blob.stdout.decode().strip()
            git(["update-index", "--add", "--cacheinfo", f"100644,{sha},{path}"],
                cwd=self.state, index_file=index)
        tree = git(["write-tree"], cwd=self.state,
                   index_file=index).stdout.decode(errors="ignore").strip()
        with contextlib.suppress(OSError):
            index.unlink()
        return tree

    def remote_head(self, timeout: int = 12) -> str:
        """The shared ref's current sha, without transferring a single object.

        `ls-remote` is the cheapest question you can ask a git host, which makes
        it the right one for "has anything changed" on a path that runs between
        every prompt.
        """
        if not self.sources():
            return ""
        proc = git(["ls-remote", self.sources()[0]["url"], STATE_REF],
                   cwd=self.state, check=False, timeout=timeout)
        line = proc.stdout.decode(errors="ignore").strip()
        return line.split()[0] if line else ""

    def unpublished(self) -> int:
        """Records written locally that no remote has seen — best effort."""
        marker = self.local().get("last_push")
        if not marker:
            return len(self._own_records())
        stamp = parse_iso(marker)
        if stamp is None:
            return 0
        count = 0
        for path in self.mine.rglob("*.json"):
            try:
                if path.stat().st_mtime > stamp.timestamp():
                    count += 1
            except OSError:
                continue
        return count

    def destroy(self) -> None:
        shutil.rmtree(self.home, ignore_errors=True)


def _newer(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Prefer a signed record, then the newer timestamp.

    Two sources carrying the same path is normal — a fork and the upstream both
    mirror it. Preferring the signed copy means a source that strips or mangles
    signatures loses to one that does not.
    """
    if bool(a.get("sig")) != bool(b.get("sig")):
        return bool(a.get("sig"))
    return str(a.get("updated_at") or a.get("ts") or a.get("created_at") or "") > \
        str(b.get("updated_at") or b.get("ts") or b.get("created_at") or "")


# ---------------------------------------------------------------- json io


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)
