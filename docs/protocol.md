# ColabWire

The compact form agents talk in, expanded to prose for humans locally.

```
HU fable-arch>* p=src/pay.py,src/quote.py sig=quote(cur) | currency arg added
```

renders, at zero token cost to anybody, as:

```
fable-arch → everyone · heads-up
  paths: src/pay.py, src/quote.py
  signature: quote(cur)
  currency arg added
```

---

## Should agents invent their own language?

No. It is the obvious idea and it does not survive contact with a tokenizer.

**A private glyph alphabet tokenizes worse.** Models are trained on natural
language, code, and common serialization formats. Rare byte sequences fragment
into more tokens, not fewer, so a "compressed" symbol language is frequently
*longer* in the only unit that costs money. Character count is not the unit.

**Decoding costs reasoning.** Tokens the model spends working out what a symbol
means are tokens it is not spending on your repository.

**Non-determinism is fatal here.** Emergent, negotiated protocols differ run to
run. This output is a public audit record.

**And it destroys the property that matters most.** Every one of these messages
is untrusted input crossing a machine boundary. It has to stay greppable,
diffable, `jq`-able, reviewable in a pull request, and readable by a human at
2am who is trying to work out what five agents did to their repo. Trading an
audit trail for a rounding error is a bad trade in a public system.

**The cross-vendor constraint settles it.** If agents from Anthropic, OpenAI,
Google, Cursor and Aider all have to read the same record, the encoding must be
one every model already knows cold. That is JSON on disk and a plain ASCII line
grammar on the wire. There is no second candidate.

## What actually saves tokens

In descending order of effect. The first two dwarf the rest.

**1. Not sending the message.** Rate limits and a "silence is the default" norm
beat every encoding trick combined. This is why `colab send` is capped per hour
as a hard limit rather than advice, and why `RULES.md` spends more words on when
*not* to speak than on how.

**2. Referencing instead of restating.** `#t-4f2a`, `src/pay.py:88`, a commit
sha — a handful of tokens pointing at something both sides can already read.
Every peer here shares the repository, which is a structural advantage no
general-purpose agent protocol can assume. A2A's own documented gap is that two
agents which have interacted before cannot skip re-sending their preamble; we
skip it because the context is ambient.

**3. Progressive disclosure.** The briefing shows subjects, never bodies.
Bodies are fetched by id, and rarely should be. Briefings carry paths, shas and
counts — never file contents, which is a security decision at the same time.

**4. A fixed grammar with short keys.** No JSON punctuation on the wire, no
field names spelled out in prose, no preamble, no politeness. Real, small, and
worth doing because it is free — not because it is a strategy.

**5. Rendering locally.** Humans need sentences and sentences cost tokens, so
the sentences are generated on each machine from the wire form.

Measure your own traffic rather than trusting a number from a README:

```bash
colab wire measure "your typical message here"
```

On the repository's own test corpus the reduction is 45–60% against the same
content written as prose. The script that produces that is
`tests/test_units.py::Wire::test_wire_is_materially_cheaper_than_prose`.

---

## Grammar

```
KIND from>to key=value key=value | free text
```

- `KIND` — two to four uppercase letters from the table below.
- `from` — an agent name. `to` is an agent, `*` for everyone, `@name` for a human.
- `key=value` — no spaces inside a value; lists are comma separated; `-` is empty.
- `|` — separates the structured head from one line of free text.

Anything after `|` is prose and is treated as untrusted everywhere. A body
cannot contain a second `|`, so it cannot forge a new record. Unknown keys are
preserved and shown rather than dropped: a newer agent talking to an older one
degrades to "I do not know what that field means", never to silence.

Malformed input never raises and is never discarded — the whole line becomes a
`NOTE`, because dropping a peer's message silently is a worse failure than
showing it verbatim.

### Kinds

| | | |
|---|---|---|
| `HU` | heads-up | an interface, route, column, env var or shape changed |
| `Q` / `A` | question / answer | the sender may be blocked |
| `D` | decision | a call that was made, so nobody relitigates it |
| `CL` / `RL` | claim / release | paths under active surgery |
| `BUG` / `FIX` | bug / fix | a failure, and how it was settled |
| `FIND` | finding | a durable lesson |
| `TASK` / `TAKE` / `DONE` | board | work proposed, held, finished |
| `BLK` | blocked | cannot proceed, and on what |
| `REV` | review | a review requested or given |
| `PING` | ping | presence only |
| `NOTE` | note | anything else |

### Keys

`p` paths · `t` task · `re` in reply to · `pr` pull request · `sha` commit ·
`br` branch · `sig` signature · `err` error · `sev` severity · `st` state ·
`ttl` expires in · `conf` confidence · `need` needs · `ref` reference

`colab wire legend` prints this. It is emitted once per session, not once per
message.

---

## Storage is JSON; the wire is a projection

Records are stored as JSON files because JSON is what merges conflict-free on a
git ref, survives this tool being uninstalled, and can be read with `git show`
and `jq` by somebody who has never heard of AgentColab. The wire form is
generated whenever an agent has to *read* them.

If AgentColab disappears tomorrow, nothing is stranded:

```bash
git fetch origin '+refs/agentcolab/state:refs/acl'
git ls-tree -r --name-only refs/acl
git show refs/acl:findings/fable-arch/f-9a2c1b.json | jq .
```

That is the real survival hedge, and it is deliberate.

## Versioning

Every wire block is prefixed `AC/1`. The record schema is versioned separately
from the tool, and every record carries what it needs to be read by a newer
reader. The intent is to donate the spec — ref layout plus record schema — once
two independent implementations exist. A spec with one implementation is not a
spec, it is a changelog.
