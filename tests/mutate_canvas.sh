#!/usr/bin/env bash
# Mutation checks for the canvas client: each mutation below removes or bends
# one safety property in a temporary copy of the tree, and the named test must
# go red for it. A test that stays green under a mutation is a test that does
# not test the thing it is named after -- which is how the scrubber could have
# been deleted without CI noticing. Listed in tests/test_canvas.py's header.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/agentcolab-mutate.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n' "$1"; [ $# -gt 1 ] && printf '       %s\n' "$2"; }

# A fresh copy per mutation: they must not see each other.
fresh() {
  rm -rf "$WORK/tree"
  mkdir -p "$WORK/tree"
  cp -R "$ROOT/agentcolab" "$ROOT/tests" "$WORK/tree/"
  rm -rf "$WORK/tree/agentcolab/__pycache__" "$WORK/tree/tests/__pycache__"
}

# mutate <file> <needle> <replacement>: exact-string replacement that must hit
# exactly once, so a refactor that moves the code fails loudly here instead of
# silently leaving the mutation unapplied (and the check vacuously "red").
mutate() {
  python3 - "$WORK/tree/$1" "$2" "$3" <<'PY'
import sys
path, needle, replacement = sys.argv[1], sys.argv[2], sys.argv[3]
src = open(path, encoding="utf-8").read()
count = src.count(needle)
if count != 1:
    sys.exit(f"mutation needle found {count} times in {path}: {needle!r}")
open(path, "w", encoding="utf-8").write(src.replace(needle, replacement))
PY
}

# expect_red <label> <test id>: the test must fail on the mutated copy.
expect_red() {
  local label="$1" test="$2"
  if (cd "$WORK/tree" && timeout 300 python3 tests/test_canvas.py "$test" >"$WORK/out.log" 2>&1); then
    bad "$label" "stayed green under the mutation ($test)"
  else
    if grep -qE "^(FAIL|ERROR):" "$WORK/out.log"; then ok "$label"
    else bad "$label" "did not run: $(tail -3 "$WORK/out.log" | tr '\n' ' ')"; fi
  fi
}

echo "workspace: $WORK"
echo "each mutation must turn its test red"

fresh
mutate agentcolab/canvas.py "    body = records.scrub_deep(body)
" ""
expect_red "1. deleting scrub_deep leaks nested tokens" "Sanitise.test_scrub_reaches_nested_strings"

fresh
mutate agentcolab/canvas.py "return records.withhold_secrets(text) if records.looks_like_secret(text) else text" "return text"
expect_red "2. deleting the looks_like_secret gate leaks high-entropy blobs" "Sanitise.test_credential_shaped_blobs_are_withheld"

fresh
mutate agentcolab/session.py 'return "\n".join(["### Canvas role", ROLE_PREAMBLE, records.frame_untrusted(line),' 'return "\n".join(["### Canvas role", ROLE_PREAMBLE, line,'
expect_red "3. dropping frame_untrusted from the role block lets a role forge structure" "Briefing.test_the_role_block_is_fenced_and_cannot_close_its_own_fence"

fresh
mutate agentcolab/canvas.py "if stat.st_size - self.read_to > self.backlog_max and not self.partial and not self.skipping:" "if False:"
expect_red "4. removing the BACKLOG_MAX branch reads a whole backlog instead of skipping it" "Offsets.test_a_backlog_over_one_mebibyte_is_skipped_with_a_gap"

fresh
mutate agentcolab/canvas.py "        seq = line_no * 256 + block
        self.seq = max(self.seq, seq)" "        seq = line_no * 16 + block
        self.seq = max(self.seq, seq)"
expect_red "5. seq = line * 16 + block no longer matches the contract's positions" "ClaudeParser.test_every_kind_is_produced_and_meta_is_skipped"

fresh
mutate agentcolab/canvas.py "        if self.unacked:
            return False
        self.state.update({\"offset\": self.read_to," "        self.state.update({\"offset\": self.read_to,"
expect_red "6. committing the offset before the relay answers loses events on a 503" "Offsets.test_offset_moves_only_on_ack"

fresh
mutate agentcolab/canvas.py "    return candidate == root or root in candidate.parents" "    return True"
expect_red "7. a scope check that accepts every checkout tails other repositories' sessions" "Daemon.test_scope_is_the_joined_checkout"

echo
printf '%d mutations caught, %d escaped\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
