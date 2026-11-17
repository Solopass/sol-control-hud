"""Run `git` for a repo only when the repo changed.

Each project folder used to get fresh `git log` / `git status` / `git config` processes every minute (cards) and every
minute again for the ticker: about 40 short processes per round on this PC. Here a result is kept until the repo's own
bookkeeping changes: modified times of .git/HEAD, .git/index, .git/packed-refs, .git/config, the current branch's ref
file and .git/refs/heads (a commit, checkout, add, fetch, branch or config change touches one of them), plus the files
that were dirty last time (further edits to them, or deleting them, show up). Safety net: MAX_AGE_S, after which it
runs again whatever the fingerprint says.

Known limit: the FIRST edit of a clean tracked file touches none of these, so a repo going from clean to dirty is only
noticed at the next change in the repo or after MAX_AGE_S.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable

MAX_AGE_S = 300.0  # a clean repo whose files you just edited shows as changed within this (the edit touches nothing in .git);
                   # 60 s cost ~2.5% of a core re-checking ~40 repos
_BASE = 6                       # length of the fingerprint's repo part (the rest is the dirty files' times)


def _mtime(p: Path) -> int | None:
    try:
        return p.stat().st_mtime_ns
    except OSError:
        return None


def fingerprint(folder: Path, extra: tuple = ()) -> tuple | None:
    """What identifies the repo's current state, or None when it can't be told (not a plain `.git` folder): then the
    caller always runs git."""
    git = folder / ".git"
    try:
        if not git.is_dir():
            return None
        head = git / "HEAD"
        branch_ref = None
        try:
            text = head.read_text(encoding="utf-8", errors="replace").strip()
            if text.startswith("ref:"):
                branch_ref = git / text[4:].strip()
        except OSError:
            pass
        base = tuple(_mtime(p) for p in (head, git / "index", git / "packed-refs", git / "config", git / "refs" / "heads"))
        if base[0] is None and base[1] is None:
            return None
        return base + (_mtime(branch_ref) if branch_ref else None,) + tuple(_mtime(folder / x) for x in extra)
    except OSError:
        return None


def porcelain_paths(porcelain: str) -> tuple[str, ...]:
    out = []
    for line in (porcelain or "").splitlines():
        path = line[3:].split(" -> ")[-1].strip().strip('"')
        if path:
            out.append(path)
    return tuple(out[:200])          # a huge dirty list isn't worth statting every time


class RepoCache:
    """folder -> (fingerprint, paths, taken_at, value)."""

    def __init__(self, max_age: float = MAX_AGE_S, clock: Callable[[], float] = time.monotonic):
        self.max_age = max_age
        self._clock = clock
        self._items: dict[str, tuple[tuple, tuple, float, object]] = {}
        self._lock = threading.Lock()

    def get(self, folder: Path, compute: Callable[[], object], paths: Callable[[object], tuple] = lambda v: (),
            keep: Callable[[object], bool] = lambda v: True):
        """The cached value if the repo looks unchanged, else compute() it. A value `keep` rejects (e.g. git failed)
        is returned but not remembered."""
        key = str(folder).lower()
        now = self._clock()
        with self._lock:
            hit = self._items.get(key)
        if hit and now - hit[2] < self.max_age and fingerprint(folder, hit[1]) == hit[0]:
            return hit[3]
        before = fingerprint(folder)                  # taken before running git: a change during it re-runs next time
        value = compute()
        if before is not None and keep(value):
            extra = paths(value)
            fp = fingerprint(folder, extra)
            if fp is not None and fp[:_BASE] == before:
                with self._lock:
                    self._items[key] = (fp, extra, now, value)
        return value

    def forget(self, suffix: str | None = None) -> None:
        with self._lock:
            if suffix is None:
                self._items.clear()
            else:
                for k in [k for k in self._items if k.endswith("\\" + suffix.lower())]:
                    self._items.pop(k, None)


PORCELAIN = RepoCache()       # `git status --porcelain` text per repo (the ticker's dirty check)
