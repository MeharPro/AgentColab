#!/bin/sh
# AgentColab installer.
#
# Downloads a checkout and puts `colab` on your PATH. It runs no build step, no
# postinstall hook, and installs no dependencies, because this is a tool that
# runs inside your agent's session on a machine that holds your source code.
#
#   curl -fsSL https://raw.githubusercontent.com/MeharPro/AgentColab/main/install.sh | sh
#
# Read it first. You should read anything you pipe to a shell.
set -eu

REPO="${AGENTCOLAB_REPO:-https://github.com/MeharPro/AgentColab}"
REF="${AGENTCOLAB_REF:-main}"
PREFIX="${AGENTCOLAB_PREFIX:-$HOME/.local}"
LIB="$PREFIX/lib/agentcolab"
BIN="$PREFIX/bin"

say()  { printf '%s\n' "$*"; }
die()  { printf 'install: %s\n' "$*" >&2; exit 1; }

command -v git >/dev/null 2>&1 || die "git is required"
PY=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1 &&
     "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,9) else 1)' 2>/dev/null; then
    PY="$candidate"; break
  fi
done
[ -n "$PY" ] || die "Python 3.9 or newer is required"

say "AgentColab"
say "  source  $REPO@$REF"
say "  install $LIB"

mkdir -p "$LIB" "$BIN"
if [ -d "$LIB/.git" ]; then
  say "  updating existing checkout"
  git -C "$LIB" fetch --quiet --depth 1 origin "$REF"
  git -C "$LIB" reset --quiet --hard FETCH_HEAD
else
  rm -rf "$LIB"
  git clone --quiet --depth 1 --branch "$REF" "$REPO" "$LIB"
fi

cat > "$BIN/colab" <<LAUNCHER
#!/bin/sh
exec "$PY" "$LIB/bin/colab" "\$@"
LAUNCHER
chmod +x "$BIN/colab"

say ""
if command -v colab >/dev/null 2>&1; then
  say "Installed. Next:"
else
  say "Installed, but $BIN is not on your PATH. Add this to your shell profile:"
  say ""
  say "    export PATH=\"$BIN:\$PATH\""
  say ""
  say "Then:"
fi
say "    cd your-repo && colab join"
