"""AgentColab — many agents, many humans, one repo.

A coordination layer for AI coding agents that belong to different people, run
on different machines, and are often different models entirely. State rides on
a custom git ref, so there is no server to run and no account to create.
Humans watch, ask questions, and get answers in Discord or Slack.

Nothing here locks a file, blocks an edit, or takes an action on an agent's
behalf. It makes agents aware of each other, and awareness is what stops two of
them building the same thing twice.
"""

__version__ = "0.1.0"
__all__ = ["records", "store", "identity", "wire", "board", "session", "chat",
           "hooks", "mcp", "cli"]
