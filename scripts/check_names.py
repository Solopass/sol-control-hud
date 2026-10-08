"""Pre-commit: refuse a commit that adds an undefined name to a Python file.

Why: ticker.py used `subprocess` without importing it from its very first commit here (7b4de81, carried over from
sol-hud); the NameError was swallowed by an `except Exception: pass`, so Desk/Away from the ticker and the NET slide's
click never did anything (fixed in b8aaf32, whose message wrongly blames 2a4c9c4). Tests didn't run that path;
pyflakes sees it in a second.

Only *new* undefined names count: each staged .py file is compared with its committed version, so a file that already
has some (and isn't being fixed in this commit) never blocks unrelated work.

usage: python scripts/check_names.py            (staged files, as the hook runs it)
       python scripts/check_names.py FILE...    (those files as they are on disk, against HEAD)
"""
from __future__ import annotations

import subprocess
import sys
from collections import Counter

from pyflakes import api, messages


class _Collect:
    def __init__(self):
        self.names: Counter[str] = Counter()

    def flake(self, msg):
        if isinstance(msg, messages.UndefinedName):
            self.names[msg.message_args[0]] += 1

    def unexpectedError(self, filename, msg):
        pass

    def syntaxError(self, filename, msg, lineno, offset, text):
        self.names[f"<syntax error line {lineno}: {msg}>"] += 1


def undefined(source: str, filename: str) -> Counter[str]:
    c = _Collect()
    api.check(source, filename, c)
    return c.names


def git(*args: str) -> str | None:
    r = subprocess.run(["git", *args], capture_output=True, text=True, encoding="utf-8", errors="replace")
    return r.stdout if r.returncode == 0 else None


def main(argv: list[str]) -> int:
    if argv:
        files = [(f, open(f, encoding="utf-8-sig").read()) for f in argv if f.endswith(".py")]
    else:
        staged = (git("diff", "--cached", "--name-only", "--diff-filter=ACMR") or "").split()
        files = [(f, git("show", f":{f}") or "") for f in staged if f.endswith(".py")]
    bad = []
    for path, now in files:
        before = undefined(git("show", f"HEAD:{path}") or "", path)
        added = undefined(now.lstrip("﻿"), path) - before
        bad += [f"{path}: undefined name {name!r}" + (f" (x{n})" if n > 1 else "") for name, n in added.items()]
    for b in bad:
        print(b)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
