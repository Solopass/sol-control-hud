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
SAVE_EVERY_S = 30.0             # at most this often to disk (and once more at exit)
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

    def __init__(self, max_age: float = MAX_AGE_S, clock: Callable[[], float] = time.monotonic, background: bool = False):
        """`background`: when an entry is only too old (the repo looks unchanged), return it at once and re-run git on
        one worker thread. Without it every entry taken in the same pass expires together, and the request that finds
        them (a dashboard poll, or the Start / Stop click that forces a fresh list) waited for git in all ~45 repos:
        7-9 s, every 5 minutes (2026-11-19)."""
        self.max_age = max_age
        self._clock = clock
        self._items: dict[str, tuple[tuple, tuple, float, object]] = {}
        self._lock = threading.Lock()
        self.background = background
        self._queue: list[tuple[str, Path, Callable, Callable, Callable]] = []
        self._queued: set[str] = set()
        self._worker: threading.Thread | None = None
        self.persist: Path | None = None          # set by the owner: saved here, loaded on first use
        self._loaded = False
        self._saved_at = -1e9
        self._unsaved = False

    def get(self, folder: Path, compute: Callable[[], object], paths: Callable[[object], tuple] = lambda v: (),
            keep: Callable[[object], bool] = lambda v: True):
        """The cached value if the repo looks unchanged, else compute() it. A value `keep` rejects (e.g. git failed)
        is returned but not remembered."""
        key = str(folder).lower()
        now = self._clock()
        if self.persist and not self._loaded:
            self.load()
        self._maybe_save()
        with self._lock:
            hit = self._items.get(key)
        if hit and fingerprint(folder, hit[1]) == hit[0]:
            if now - hit[2] < self.max_age:
                return hit[3]
            if self.background:
                self._enqueue(key, folder, compute, paths, keep)
                return hit[3]
        return self._run(key, folder, compute, paths, keep, now)

    def _run(self, key: str, folder: Path, compute, paths, keep, now: float):
        before = fingerprint(folder)                  # taken before running git: a change during it re-runs next time
        value = compute()
        if before is not None and keep(value):
            extra = paths(value)
            fp = fingerprint(folder, extra)
            if fp is not None and fp[:_BASE] == before:
                with self._lock:
                    self._items[key] = (fp, extra, now, value)
                self._unsaved = True
                self._maybe_save()
        return value

    def _maybe_save(self) -> None:
        """Save new entries at most every SAVE_EVERY_S; ones that arrive in between go out on a later call."""
        if self.persist and self._unsaved and self._clock() - self._saved_at > SAVE_EVERY_S:
            self.save()

    # ---- kept across restarts: the first Projects card after a restart read git in ~47 repos (~8 s, 2026-11-22)
    def save(self) -> None:
        import json
        if not self.persist:
            return
        now_m, now_w = self._clock(), time.time()
        with self._lock:
            rows = [{"key": k, "fp": list(v[0]), "extra": list(v[1]), "at": now_w - (now_m - v[2]), "value": v[3]}
                    for k, v in self._items.items()]
        try:
            tmp = self.persist.with_suffix(".tmp")
            tmp.write_text(json.dumps({"version": 1, "items": rows}), encoding="utf-8")
            tmp.replace(self.persist)
            self._saved_at, self._unsaved = now_m, False
        except (OSError, TypeError, ValueError):
            from ..swallow import note
            note("gitcache.save")

    def load(self) -> None:
        """Entries come back with their real age; each is still checked against the repo's fingerprint before use."""
        import json
        self._loaded = True
        try:
            data = json.loads(self.persist.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict) or data.get("version") != 1:
            return
        now_m, now_w = self._clock(), time.time()
        with self._lock:
            for r in data.get("items") or []:
                try:
                    self._items.setdefault(r["key"], (tuple(r["fp"]), tuple(r["extra"]), now_m - max(0.0, now_w - float(r["at"])), r["value"]))
                except (KeyError, TypeError, ValueError):
                    continue

    def _enqueue(self, key: str, folder: Path, compute, paths, keep) -> None:
        with self._lock:
            if key in self._queued:
                return
            self._queued.add(key)
            self._queue.append((key, folder, compute, paths, keep))
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._drain, name="gitcache-refresh", daemon=True)
                self._worker.start()

    def _drain(self) -> None:
        while True:
            with self._lock:
                if not self._queue:
                    self._worker = None
                    return
                key, folder, compute, paths, keep = self._queue.pop(0)
            try:
                self._run(key, folder, compute, paths, keep, self._clock())
            except Exception:  # noqa: BLE001 - the stale value stays; the next request queues it again
                from ..swallow import note
                note("gitcache.refresh")
            finally:
                with self._lock:
                    self._queued.discard(key)

    def wait_idle(self, timeout: float = 10.0) -> None:
        """Tests: wait until the background refreshes queued so far are done."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self._lock:
                if not self._queue and not self._queued:
                    return
            time.sleep(0.01)

    def forget(self, suffix: str | None = None) -> None:
        with self._lock:
            if suffix is None:
                self._items.clear()
            else:
                for k in [k for k in self._items if k.endswith("\\" + suffix.lower())]:
                    self._items.pop(k, None)


PORCELAIN = RepoCache()       # `git status --porcelain` text per repo (the ticker's dirty check)
