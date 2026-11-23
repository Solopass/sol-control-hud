"""Tests never touch the live app's data folder (settings, logs, the run history, the running marker).

Since the switch-over (2026-09-26) `data/` in this repo is the live machine's state: a test that saved ticker settings
there turned `docked` off in the real settings. `paths.DATA_DIR` is read once at import, so this has to be set before
anything from `sol_control_hud` is imported.
"""
import os
import tempfile

os.environ["SOL_CONTROL_DATA"] = tempfile.mkdtemp(prefix="sol-control-hud-tests-")
_test_llm_dir = tempfile.mkdtemp(prefix="sol-llm-tests-")
os.environ["SOL_LLM_DIR"] = _test_llm_dir
os.environ["SOL_LLM_THOUGHT"] = os.path.join(_test_llm_dir, "live_thought.json")



import pytest  # noqa: E402


class FakeCollector:
    """Stands in for the real data collector inside TickerApp: no GPU counters, no media PowerShell, no router calls.
    (Tests of the collector itself import it from sol_control_hud.data.snapshot and still get the real one.)"""

    def __init__(self, *a, **k):
        from sol_control_hud.data.snapshot import Snapshot
        self._snap, self.interval = Snapshot(), 2.0
        self._sampler = type("Sampler", (), {"latest": {}, "interval": 2.0})()

    def start(self): pass
    def stop(self): pass
    def get_snapshot(self): return self._snap
    def collect_once(self): return self._snap
    def invalidate_cache(self): pass
    def set_pace(self, seconds): self.interval = seconds


@pytest.fixture(autouse=True)
def no_real_collector_in_the_ticker(monkeypatch):
    import sol_control_hud.views.ticker as ticker
    monkeypatch.setattr(ticker, "TickerCollector", FakeCollector)


@pytest.fixture(autouse=True)
def no_git_hook_environment(monkeypatch):
    """The pre-commit hook runs these tests with GIT_INDEX_FILE (and for some commits GIT_DIR...) exported. A test that
    runs `git` in its own temp repo then wrote into the commit's index: `git commit -- <paths>` failed with "invalid
    object ... public/lib.js" (test_chains_engine's repo, 2026-11-22). Tests never get the hook's git variables."""
    for k in [k for k in os.environ if k.startswith("GIT_") and k not in ("GIT_EDITOR",)]:
        monkeypatch.delenv(k, raising=False)
