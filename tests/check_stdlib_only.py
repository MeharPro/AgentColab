#!/usr/bin/env python3
"""Fail if a non-stdlib import ever appears.

Zero dependencies is a security property here, not a preference: this package
runs inside an agent's session on a machine holding source code, and every
dependency is another party that gets to run code there. Parsed with `ast`
rather than grepped, because a docstring beginning "from a hook..." is not an
import and a grep cannot tell.
"""

from __future__ import annotations

import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

ALLOWED = {
    "__future__", "argparse", "ast", "contextlib", "datetime", "fcntl",
    "functools", "getpass", "hashlib", "hmac", "io", "json", "os", "pathlib",
    "platform", "re", "shlex", "shutil", "socket", "subprocess", "sys",
    "tempfile", "time", "typing", "unittest", "urllib", "uuid", "agentcolab",
}


def main() -> int:
    offences: list[str] = []
    for path in sorted((ROOT / "agentcolab").rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [] if node.level else [(node.module or "").split(".")[0]]
            else:
                continue
            for name in names:
                if name and name not in ALLOWED:
                    offences.append(f"{path.relative_to(ROOT)}:{node.lineno}: {name}")
    if offences:
        print("non-stdlib imports found — this package has zero dependencies on purpose:")
        for line in offences:
            print("  " + line)
        return 1
    print(f"stdlib only ({len(list((ROOT / 'agentcolab').rglob('*.py')))} modules)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
