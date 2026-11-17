"""The HUD's process scans must stay cheap: one shared scan for /api/top-processes, a remembered pid for sol-deck."""
from types import SimpleNamespace

import pytest
from sol_control_hud.data.collectors import security
from sol_control_hud.views.web.app import Cached


class FakeProc:
    def __init__(self, pid, name, rss=10 * 1024 * 1024, cpu=0.0, create=1000.0):
        self.pid, self._name, self._create = pid, name, create
        self.info = {"pid": pid, "name": name, "memory_info": SimpleNamespace(rss=rss),
                     "cpu_times": SimpleNamespace(user=cpu, system=0.0), "create_time": create}
        self._rss = rss

    def name(self):
        return self._name

    def create_time(self):
        return self._create

    def memory_info(self):
        return SimpleNamespace(rss=self._rss)


@pytest.fixture
def deck(monkeypatch):
    """Fake psutil inside security: counts full scans and Process() lookups."""
    state = SimpleNamespace(procs=[FakeProc(10, "other.exe"), FakeProc(42, "sol-deck.exe")], scans=0, clock=[100.0])
    monkeypatch.setattr(security.psutil, "process_iter", lambda attrs=None: (setattr(state, "scans", state.scans + 1) or iter(state.procs)))

    def process(pid):
        for p in state.procs:
            if p.pid == pid:
                return p
        raise security.psutil.NoSuchProcess(pid)
    monkeypatch.setattr(security.psutil, "Process", process)
    monkeypatch.setattr(security.time, "monotonic", lambda: state.clock[0])
    security.reset_deck_cache()
    yield state
    security.reset_deck_cache()


def test_deck_status_uses_remembered_pid_without_a_scan(deck):
    first = security.get_deck_process_status()
    assert first["running"] and first["pid"] == 42 and first["ram_mb"] == 10.0
    assert deck.scans == 1
    for _ in range(5):
        assert security.get_deck_process_status()["pid"] == 42
    assert deck.scans == 1


def test_deck_status_rescans_at_most_every_30s(deck):
    security.get_deck_process_status()
    assert deck.scans == 1
    deck.procs = [FakeProc(10, "other.exe")]          # sol-deck went away
    deck.clock[0] += 5
    assert security.get_deck_process_status()["running"] is False   # pid gone, last scan 5 s ago: no new scan
    assert deck.scans == 1
    deck.procs.append(FakeProc(77, "sol-deck.exe"))   # it comes back
    deck.clock[0] += 10
    assert security.get_deck_process_status()["running"] is False
    assert deck.scans == 1
    deck.clock[0] += 20                               # 35 s since the scan
    st = security.get_deck_process_status()
    assert st["running"] and st["pid"] == 77 and deck.scans == 2


def test_deck_status_pid_reuse_is_not_still_running(deck):
    security.get_deck_process_status()
    deck.procs = [FakeProc(42, "notepad.exe")]        # same pid, another program
    deck.clock[0] += 31
    assert security.get_deck_process_status()["running"] is False


def test_reset_deck_cache_rescans_immediately(deck):
    deck.procs = [FakeProc(10, "other.exe")]
    assert security.get_deck_process_status()["running"] is False
    deck.procs.append(FakeProc(42, "sol-deck.exe"))
    assert security.get_deck_process_status()["running"] is False   # within 30 s
    security.reset_deck_cache()
    assert security.get_deck_process_status()["running"] is True


def test_top_processes_collector_scans_once_for_two_calls(monkeypatch):
    from sol_control_hud import hub
    scans = []
    procs = [FakeProc(5, "a.exe", rss=50 * 1024 * 1024, cpu=1.0), FakeProc(6, "b.exe", rss=5 * 1024 * 1024, cpu=2.0)]
    monkeypatch.setattr("psutil.process_iter", lambda attrs=None: (scans.append(1) or iter(procs)))
    h = hub.Hub.__new__(hub.Hub)
    h.collectors = {"gpu": Cached(lambda: {"pid_chips": {6: "GPU 0"}}, 0)}
    col = Cached(h._top_processes_payload, 4)
    a, b = col.get(), col.get()
    assert len(scans) == 1 and a is b
    assert a["ok"] is True and a["top_mem"][0]["pid"] == 5
    assert a["top_gpu"] == [{"pid": 6, "name": "b.exe", "cpu": 0.0, "mem_mb": 5.0, "chip": "GPU 0"}]


def test_cached_simultaneous_misses_run_once():
    import threading
    import time
    calls = []

    def slow():
        calls.append(1)
        time.sleep(0.2)
        return 1
    c = Cached(slow, 10)
    ts = [threading.Thread(target=c.get) for _ in range(5)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(calls) == 1
