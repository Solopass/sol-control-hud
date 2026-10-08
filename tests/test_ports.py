"""The Localhost card: what counts as a dev server, what is never closable, and the open/closed history.

The point of the guard rails here is that a click on this card must not be able to stop a model mid-run, a Windows
service, or the dashboard itself - so most of these tests are about what `close_listener` refuses.
"""
import json
import time
from collections import namedtuple

import psutil
import pytest

from sol_control_hud import hub
from sol_control_hud.data.collectors import ports

Addr = namedtuple("Addr", "ip port")
DENIED = object()          # a username we are not allowed to read (every Windows service)


class FakeProc:
    def __init__(self, pid, name, cmdline=(), user="SOL\\cohen", started=1000.0, kids=()):
        self.pid, self._name, self._cmd, self._user, self._started = pid, name, tuple(cmdline), user, started
        self._kids = list(kids)
        self.terminated = self.killed = False

    def name(self):
        return self._name

    def cmdline(self):
        return list(self._cmd)

    def username(self):
        if self._user is DENIED:
            raise psutil.AccessDenied(self.pid)
        return self._user

    def create_time(self):
        return self._started

    def children(self, recursive=False):
        return list(self._kids)

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


def fake_machine(monkeypatch, listening, procs):
    """`listening` is [(pid, ip, port)]; `procs` is {pid: FakeProc}. Nothing real is touched."""
    conns = [type("C", (), {"status": psutil.CONN_LISTEN, "pid": pid, "laddr": Addr(ip, port)})()
             for pid, ip, port in listening]
    monkeypatch.setattr(ports.psutil, "net_connections", lambda kind="inet": conns)

    def process(pid):
        if pid not in procs:
            raise psutil.NoSuchProcess(pid)
        return procs[pid]

    monkeypatch.setattr(ports.psutil, "Process", process)
    monkeypatch.setattr(ports.psutil, "wait_procs", lambda ps, timeout=None: (list(ps), []))
    monkeypatch.setattr(ports, "_info", {})            # each fake machine starts without what an earlier one cached
    return procs


# ---- what a listener is

def test_the_ai_stack_windows_and_we_ourselves_are_never_dev_servers():
    vite = FakeProc(10, "node.exe", ["node", r"C:\p\busy-oppenheimer\node_modules\vite\bin\vite.js"])
    router = FakeProc(11, "llama-server.exe", [r"D:\AI\bin\llama\vulkan\llama-server.exe", "--models-preset"])
    shim = FakeProc(12, "python.exe", ["python", r"D:\OBVLT\tools\ollama-shim.py"])
    svc = FakeProc(13, "svchost.exe", ["svchost.exe", "-k", "netsvcs"], user=DENIED)
    me = FakeProc(14, "pythonw.exe", ["pythonw.exe", "-m", "sol_control_hud"])
    discord = FakeProc(15, "Discord.exe", ["Discord.exe", "--type=renderer", "/prefetch:1"])

    assert ports.classify(vite, 5173, own_pid=99) == "dev"
    assert ports.classify(router, 11440, own_pid=99) == "ai"
    assert ports.classify(shim, 11434, own_pid=99) == "ai"
    assert ports.classify(svc, 135, own_pid=99) == "system"
    assert ports.classify(me, 7900, own_pid=99) == "self"        # by its command line, not only by pid
    assert ports.classify(me, 7900, own_pid=14) == "self"        # and by pid when it is this very process
    assert ports.classify(discord, 6463, own_pid=99) == "app"


def test_an_unknown_port_in_the_ai_range_stays_locked():
    """The router hands out 11434-11449; anything there is treated as the AI stack even under a plain name."""
    anon = FakeProc(20, "server.exe", ["server.exe"])
    assert ports.classify(anon, 11445, own_pid=99) == "ai"
    assert ports.classify(anon, 5173, own_pid=99) == "app"


def test_label_names_the_project_not_the_tools_own_folder():
    vite = FakeProc(10, "node.exe", ["node", r"C:\p\busy-oppenheimer\node_modules\vite\bin\vite.js"])
    module = FakeProc(11, "pythonw.exe", ["pythonw.exe", "-m", "vocal_savior", "watch"])
    switches = FakeProc(12, "Discord.exe", ["Discord.exe", "--type=renderer", "/prefetch:1"])
    assert ports.label(vite) == "vite \u00b7 busy-oppenheimer"
    assert ports.label(module) == "vocal_savior"                  # `-m module`, no path to read
    assert ports.label(switches) == ""                            # switches are not scripts


def test_rows_merge_ipv4_and_ipv6_and_flag_ports_open_to_the_network(monkeypatch):
    proc = FakeProc(10, "node.exe", ["node", "serve.js"], started=500.0)
    fake_machine(monkeypatch, [(10, "127.0.0.1", 5173), (10, "::1", 5173)], {10: proc})
    rows = ports._rows(own_pid=99, now=1000.0)
    assert len(rows) == 1 and rows[0]["addrs"] == ["127.0.0.1", "::1"]
    assert rows[0]["up_seconds"] == 500.0 and rows[0]["everyone"] is False

    fake_machine(monkeypatch, [(10, "0.0.0.0", 8080)], {10: proc})
    assert ports._rows(own_pid=99)[0]["everyone"] is True


def test_the_system_process_has_no_start_time(monkeypatch):
    """pid 4 reports the epoch, which would otherwise read as "up for 20000 days"."""
    fake_machine(monkeypatch, [(4, "::", 445)], {4: FakeProc(4, "System", [], user=DENIED, started=0.0)})
    row = ports._rows(own_pid=99)[0]
    assert row["started"] is None and row["up_seconds"] is None and row["can_close"] is False


# ---- closing

@pytest.fixture
def machine(monkeypatch, tmp_path):
    """A dev server, the AI router, a Windows service and this dashboard, all listening."""
    procs = {
        10: FakeProc(10, "node.exe", ["node", r"C:\p\site\node_modules\vite\bin\vite.js"],
                     kids=[FakeProc(101, "esbuild.exe")]),
        11: FakeProc(11, "llama-server.exe", [r"D:\AI\bin\llama\llama-server.exe"]),
        12: FakeProc(12, "svchost.exe", ["svchost.exe"], user=DENIED),
        13: FakeProc(13, "pythonw.exe", ["pythonw.exe", "-m", "sol_control_hud"]),
    }
    fake_machine(monkeypatch, [(10, "127.0.0.1", 5173), (11, "127.0.0.1", 11440),
                               (12, "0.0.0.0", 135), (13, "127.0.0.1", 7900)], procs)
    monkeypatch.setattr(ports, "_history", ports.History(tmp_path / "h.json"))
    return procs


def test_a_forgotten_dev_server_is_asked_to_stop_then_its_workers(machine):
    r = ports.close_listener(5173, own_pid=99)
    assert r["ok"] and ":5173" in r["why"] and "vite \u00b7 site" in r["why"]
    assert machine[10].terminated and not machine[10].killed      # it stopped when asked: no kill needed
    assert machine[10]._kids[0].terminated                        # and esbuild didn't stay behind


def test_a_dev_server_that_ignores_terminate_is_killed(machine, monkeypatch):
    proc = machine[10]
    monkeypatch.setattr(ports.psutil, "wait_procs", lambda ps, timeout=None: ([], list(ps)))
    assert ports.close_listener(5173, own_pid=99)["ok"]
    assert proc.terminated and proc.killed


@pytest.mark.parametrize("port, words", [(11440, "local AI"), (135, "Windows"), (7900, "this dashboard")])
def test_the_card_refuses_the_ai_stack_windows_and_itself(machine, port, words):
    r = ports.close_listener(port, own_pid=99)
    assert r["ok"] is False and words in r["why"]
    assert not any(p.terminated or p.killed for p in machine.values())


def test_the_page_cannot_smuggle_a_pid_past_the_class_check(machine):
    """The page sends port+pid, but the class is decided here from the live port - not from what was sent."""
    assert ports.close_listener(11440, pid=11, own_pid=99)["ok"] is False
    assert ports.close_listener(5173, pid=11, own_pid=99)["ok"] is False    # pid/port mismatch: nothing matches
    assert not machine[11].terminated


def test_closing_a_port_that_is_already_free(machine):
    r = ports.close_listener(4321, own_pid=99)
    assert r["ok"] is False and "nothing listens" in r["why"]
    assert ports.close_listener("not a port", own_pid=99) == {"ok": False, "why": "which port?"}


# ---- the history

def test_history_remembers_what_closed_and_how_long_it_ran(tmp_path):
    h = ports.History(tmp_path / "h.json")
    row = {"port": 5173, "pid": 10, "name": "node", "label": "vite \u00b7 site", "kind": "dev", "started": 100.0}
    assert h.update([row], now=200.0) == []                       # first sample: nothing has closed yet
    closed = h.update([], now=260.0)                              # gone on the next sample
    assert len(closed) == 1
    assert closed[0]["port"] == 5173 and closed[0]["ran_seconds"] == 100.0   # 100.0 started -> 200.0 last seen
    assert closed[0]["closed_at"] == 200.0 and closed[0]["approx"] is False


def test_a_port_that_went_away_while_the_dashboard_was_shut_is_marked_approximate(tmp_path):
    h = ports.History(tmp_path / "h.json")
    row = {"port": 3000, "pid": 7, "name": "node", "label": "", "kind": "dev", "started": 0.0}
    h.update([row], now=100.0)
    closed = h.update([], now=100.0 + ports.STALE_GAP_S + 1)
    assert closed[0]["approx"] is True and closed[0]["closed_at"] == 100.0


def test_a_restart_on_the_same_port_is_a_new_entry(tmp_path):
    h = ports.History(tmp_path / "h.json")
    first = {"port": 5173, "pid": 10, "name": "node", "label": "", "kind": "dev", "started": 100.0}
    again = {**first, "pid": 55, "started": 300.0}
    h.update([first], now=200.0)
    closed = h.update([again], now=300.0)
    assert [c["pid"] for c in closed] == [10]                     # the old pid closed, the new one is open
    assert list(h.state["open"]) == ["55:5173"]


def test_history_survives_a_restart_and_is_pruned(tmp_path):
    path = tmp_path / "h.json"
    h = ports.History(path)
    h.update([{"port": 5173, "pid": 10, "name": "node", "label": "", "kind": "dev", "started": 1.0}], now=10.0)
    assert json.loads(path.read_text(encoding="utf-8"))["open"]    # the open set was written out

    later = ports.History(path)                                   # the HUD restarts; the server stopped meanwhile
    closed = later.update([], now=20.0)
    assert closed[0]["port"] == 5173

    old = ports.History(path, keep=2)
    old.state["closed"] = [{"port": p, "closed_at": 0.0} for p in (1, 2, 3, 4)]
    kept = old.update([], now=10.0)
    assert len(kept) == 2                                         # keep=2 caps the list
    assert ports.History(path).update([], now=ports.KEEP_DAYS * 86400 + 10_000) == []   # and age clears it


def test_history_is_not_rewritten_on_every_sample(tmp_path, monkeypatch):
    """The dashboard samples every few seconds; only a change in what listens is worth a disk write."""
    h = ports.History(tmp_path / "h.json")
    writes = []
    monkeypatch.setattr(ports.History, "_save", lambda self: writes.append(1))
    row = {"port": 5173, "pid": 10, "name": "node", "label": "", "kind": "dev", "started": 1.0}
    h.update([row], now=10.0)
    h.update([row], now=13.0)
    h.update([row], now=16.0)
    assert len(writes) == 1                                       # the first sample only
    h.update([], now=19.0)
    assert len(writes) == 2                                       # it closed: write


def test_a_broken_history_file_does_not_break_the_card(tmp_path):
    path = tmp_path / "h.json"
    path.write_text("{not json", encoding="utf-8")
    assert ports.History(path).state == {"open": {}, "closed": []}


def test_listeners_reports_what_the_card_needs(machine):
    d = ports.listeners(own_pid=99)
    assert d["available"] is True
    assert d["counts"] == {"dev": 1, "app": 0, "ai": 1, "system": 1, "self": 1}
    assert d["closable"] == 1                                     # only the vite server
    assert [r["kind"] for r in d["listening"]][0] == "dev"         # what you might close is listed first


def test_the_card_reuses_what_a_listener_is_but_closing_reads_it_fresh(machine, monkeypatch):
    looks = []
    lookup = ports.psutil.Process
    monkeypatch.setattr(ports.psutil, "Process", lambda pid: looks.append(pid) or lookup(pid))
    first = ports.listeners(own_pid=99)["listening"]
    n = len(looks)
    again = ports.listeners(own_pid=99)["listening"]
    assert len(looks) == n                                        # the second look asks Windows nothing again
    strip = lambda rows: [{k: v for k, v in r.items() if k != "up_seconds"} for r in rows]   # noqa: E731
    assert strip(again) == strip(first)
    machine[10]._cmd = ("node", r"C:\p\other\node_modules\vite\bin\vite.js")
    assert "vite \u00b7 other" in ports.close_listener(5173, own_pid=99)["why"]   # closing decided on a fresh read


def test_a_listener_that_goes_away_is_forgotten(machine, monkeypatch):
    ports.listeners(own_pid=99)
    assert (10, 5173) in ports._info
    monkeypatch.setattr(ports.psutil, "net_connections",
                        lambda kind="inet": [type("C", (), {"status": psutil.CONN_LISTEN, "pid": 11,
                                                            "laddr": Addr("127.0.0.1", 11440)})()])
    ports.listeners(own_pid=99)
    assert (10, 5173) not in ports._info and (11, 11440) in ports._info


# ---- the action behind the button

def test_the_close_port_action_goes_through_the_same_guard(machine):
    stub = type("StubHub", (), {"do_action": hub.Hub.do_action})()
    assert stub.do_action("close_port", "11440")["ok"] is False    # the AI router: refused
    assert machine[11].terminated is False
    r = stub.do_action("close_port", "5173", {"pid": 10})
    assert r["ok"] and machine[10].terminated
    assert stub.do_action("close_port", "")["ok"] is False
