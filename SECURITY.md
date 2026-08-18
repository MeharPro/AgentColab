# Security

Full threat model: **[docs/security.md](docs/security.md)**.

## Reporting

Open a [security advisory](https://github.com/MeharPro/AgentColab/security/advisories/new).
Please do not open a public issue first.

Highest severity, and what we most want to hear about:

1. **A scrubber bypass** — any credential shape that reaches a git ref or a chat
   channel. Include the shape with an obviously fake value; a failing test is
   the ideal report.
2. **A signature forgery** — any way to make a record verify under an identity
   you do not control.
3. **A hook escape** — any way record content reaches a shell.
4. **A path escape** — any way an agent writes outside its own directory.

## What is out of scope

These are documented limitations, not vulnerabilities:

- A malicious agent that already has push access to the repository.
- Exfiltration through a public channel by an agent that can read secrets. We
  narrow that pipe; we do not close it. Do not attach a private repo to a public
  server.
- Model-level susceptibility to prompt injection. AgentColab never grants an
  agent a capability it did not already have.
- Semantic conflicts between changes that merge cleanly.

## Supported versions

The latest release. This is a young project; there is no LTS to promise yet and
saying so is more useful than a table implying otherwise.
