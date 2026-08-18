# Contributing

Agent-authored contributions are welcome and expected. They follow the same
rules as everyone else's. Those rules are in [RULES.md](RULES.md), which is
written to be read by an agent.

## Sign off, don't sign away

**DCO, not a CLA.** Add `Signed-off-by:` to every commit (`git commit -s`). You
keep your copyright; you are asserting you have the right to contribute the
code. A CLA is a contribution tax that reads as vendor capture, and this project
will not have one.

If an agent wrote the change, say so in trailers:

```bash
git commit -s -m "$(printf 'fix currency rounding\n\n%s' "$(colab trailers)")"
```

The human in `Agent-Principal` is the responsible party. The agent is the
instrument. Trailers rather than badges because trailers survive a rebase and
`git log --grep` finds them.

## Before you start

```bash
colab                          # who else is live
colab known --about <topic>    # has this already been settled?
colab next                     # the task that is deterministically yours
```

This project uses itself. If that ever feels like friction, that is a bug in the
tool, and reporting it is more valuable than the change you were making.

## Tests

```bash
python3 tests/test_units.py          # 39 assertions, no network, no git
bash tests/test_e2e.sh               # 43 assertions, four agents, one repo
python3 tests/check_stdlib_only.py   # zero-dependency guard
```

All three run in CI on Linux and macOS across Python 3.9, 3.12 and 3.14, and the
end-to-end suite runs a second time with SSH keys removed to prove signing
degrades rather than fails.

**A change to behaviour needs a test that fails without it.** Most of the bugs
found while building this were found by writing the assertion first — including
one where records verified only on the machine that wrote them, which looked
like it worked.

## House style

The code is stdlib-only and stays that way; `check_stdlib_only.py` fails the
build otherwise. This runs inside an agent's session on a machine holding source
code, and every dependency is another party that gets to run code there.

Comments explain **why**, especially where the obvious approach is wrong. There
are several places here where the natural implementation is subtly broken —
bare repositories and `ls-files`, chained commits on a heartbeat ref, a
canonical form that includes read-time metadata — and each carries a note saying
so. If you find another, leave one.

Error messages are for an agent as much as a person: say what happened, why, and
what to do next.

## Things we will not merge

- A dependency, without a very good argument.
- A lock. See [RULES.md](RULES.md) §4 and the architecture doc.
- A model in the coordination path. Overlap detection is arithmetic so it stays
  reproducible, auditable and free.
- Automatic conflict resolution without a measured false-merge rate shipped
  alongside it.
- A published number without the script that produces it.
- Anything that makes AgentColab author on a maintainer's behalf — open a pull
  request, comment on an issue, push a branch, merge, close.

## Reporting

Bugs and features: [issues](https://github.com/MeharPro/AgentColab/issues).
Vulnerabilities: [security advisory](https://github.com/MeharPro/AgentColab/security/advisories/new),
not a public issue. A scrubber bypass is the highest-severity class here.
