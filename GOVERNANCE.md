# Governance

## Now

Maintained by its original authors. Decisions are made in public issues and pull
requests. There is no private roadmap.

## Licence

MIT, permanently. No relicensing, no dual licence, no open-core split, no
"available source" tier. Copyleft was considered and rejected: this needs to be
vendorable into anything, including commercial harnesses, or it cannot become
the shared layer it is trying to be.

## Contributions

DCO, never a CLA. Contributors keep their copyright.

## The spec, and where it should end up

The **ref layout and record schema** are versioned separately from this tool.
The intent is to donate them to a neutral foundation — the Linux Foundation's
agentic AI work is where MCP, AGENTS.md and goose already live — **once two
independent implementations exist.**

Not before. A specification with one implementation is not a specification, it
is a changelog, and this space is littered with protocol repositories that had
no clients and were abandoned within a week.

The pattern is well established by now: single-vendor open cores get retired or
pivoted; foundation-governed projects do not. Foundation governance is a
legitimate technical selection criterion, and planning for it from the start is
part of being worth adopting.

## Compatibility

- Every record carries a schema version.
- Release lines run in parallel. A major version does not force a migration on
  anybody's timeline.
- A newer agent talking to an older one degrades to "I do not know what that
  field means", never to silence. Unknown fields are preserved and displayed.
- The storage format is JSON on a git ref. If this project dies, `git show` and
  `jq` still read everything, and that is deliberate.

## Becoming a maintainer

Sustained, good-faith contribution — code, review, docs, or triage. Ask in an
issue. There is no committee.

## If this project stops being maintained

The `MAINTAINERS` file will say so plainly, with a date. No pretending. Fork it;
the licence exists for that.
