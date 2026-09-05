#!/usr/bin/env bash
# Every suite, one exit code. Written after a commit went out green because a
# grep for "Ran" matched a FAILED run just as happily as an OK one.
set -uo pipefail
cd "$(dirname "$0")/.."
fail=0
run() {
  printf '\n=== %s ===\n' "$1"
  shift
  if "$@"; then :; else fail=1; printf '  ^^ FAILED\n'; fi
}
run "stdlib only"      python3 tests/check_stdlib_only.py
run "units"            python3 tests/test_units.py
run "canvas client"    python3 tests/test_canvas.py
run "canvas relay"     python3 tests/test_canvas_relay.py
run "chat integration" python3 tests/test_chat_integration.py
run "end to end"       env ROOT="$PWD" bash tests/test_e2e.sh
printf '\n'
if [ "$fail" -eq 0 ]; then echo "all suites passed"; else echo "SOME SUITES FAILED"; fi
exit "$fail"
