"""collectors/network.py: Pi-hole status and the LAN device list (2026-11-07). No real network here: fakes only."""
import struct

from sol_control_hud.data.collectors import network as n
from sol_control_hud.data.snapshot_model import Snapshot
from sol_control_hud.data.snapshot_text import attention


# ---- DNS packets

def _reply(qid, name, ips, rcode=0):
    q = n.build_query(name, n.QTYPE_A, qid)
    head = struct.pack(">HHHHHH", qid, 0x8180 | rcode, 1, len(ips), 0, 0)
    ans = b"".join(b"\xc0\x0c" + struct.pack(">HHIH", 1, 1, 60, 4) + bytes(map(int, ip.split("."))) for ip in ips)
    return head + q[12:] + ans


def test_query_and_reply_round_trip():
    assert n.parse_response(_reply(77, "example.com", ["93.184.215.14"]), 77) == (0, ["93.184.215.14"])
    assert n.parse_response(_reply(5, "doubleclick.net", ["0.0.0.0"]), 5) == (0, ["0.0.0.0"])


def test_reply_for_another_query_is_rejected():
    try:
        n.parse_response(_reply(1, "a.b", []), 2)
    except ValueError:
        return
    raise AssertionError("accepted a reply with the wrong id")


def test_blocked_answers():
    assert n.is_blocked_answer(0, ["0.0.0.0"]) and n.is_blocked_answer(3, [])
    assert not n.is_blocked_answer(0, ["142.250.217.6"]) and not n.is_blocked_answer(0, [])


# ---- Pi-hole state

def _host(ip="192.168.0.249", up=True, dns=True, ms=5, direct=True):
    return {"ip": ip, "up": up, "dns": dns, "ms": ms, "direct": direct}


def test_all_good():
    s = n.summarize([_host(), _host("192.168.0.248")], {"upstream": True, "blocking": True})
    assert s["state"] == "ok" and s["alert"] == "" and s["note"] == ""


def test_down_is_loud():
    s = n.summarize([_host(up=False), _host("192.168.0.248", up=False)])
    assert s["state"] == "down" and "no DNS" in s["alert"]


def test_web_up_dns_off():
    assert n.summarize([_host(dns=False)])["state"] == "dns-off"


def test_upstream_and_blocking():
    assert n.summarize([_host()], {"upstream": False, "blocking": True})["state"] == "no-upstream"
    s = n.summarize([_host()], {"upstream": True, "blocking": False})
    assert s["state"] == "not-blocking" and "not blocking" in s["alert"]


def test_one_address_down_is_quiet():
    s = n.summarize([_host(), _host("192.168.0.248", up=False)], {"upstream": True, "blocking": True})
    assert s["state"] == "degraded" and s["alert"] == ""


def test_slow_and_unknown():
    assert n.summarize([_host(ms=400)], {"upstream": True, "blocking": True})["state"] == "slow"
    assert n.summarize([{"ip": "x", "up": None}])["state"] == "unknown"


def test_ignored_direct_queries_are_a_note_not_an_alert():
    s = n.summarize([_host(direct=False), _host("192.168.0.248", direct=False)], {"upstream": True, "blocking": True})
    assert s["state"] == "ok" and s["alert"] == "" and "rate limit" in s["note"]


def test_monitor_probes_on_schedule():
    t = [1000.0]
    calls = []
    m = n.PiholeMonitor(hosts=["h1"], info=lambda h: (calls.append("info") or True, 4.0),
                        query=lambda h, name, timeout=1.0: (calls.append("direct") or True, 0, [], 1.0),
                        resolve=lambda name: (calls.append(name) or True, ["0.0.0.0"] if name == n.BLOCKED_NAME else ["1.2.3.4"]),
                        clock=lambda: t[0])
    m.probe()
    s = m.status()
    assert s["state"] == "ok" and s["pc"] == {"upstream": True, "blocking": True}
    first = len(calls)
    t[0] += 10
    m.probe()
    assert len(calls) == first                     # nothing is due 10 s later
    t[0] += n.UP_EVERY
    m.probe()
    assert calls[first:] == ["info", "direct"]     # only the 30 s check; upstream/blocking wait their turn


def test_monitor_skips_resolver_checks_when_pihole_is_down():
    resolved = []
    m = n.PiholeMonitor(hosts=["h1"], info=lambda h: (None, None), query=lambda *a, **k: (False, -1, [], 0),
                        resolve=lambda name: resolved.append(name) or (True, []), clock=lambda: 1.0)
    m.probe()
    assert m.status()["state"] == "down" and resolved == []


# ---- LAN devices

def test_lan_entry_filters():
    assert n.lan_entry("192.168.0.25", "00-16-3e-00-00-25")
    assert not n.lan_entry("192.168.0.255", "ff-ff-ff-ff-ff-ff")
    assert not n.lan_entry("192.168.0.30", "01-00-5e-00-00-fb")      # multicast MAC
    assert not n.lan_entry("10.0.0.5", "00-16-3e-00-00-25")           # another subnet
    assert not n.lan_entry("192.168.0.31", "00-00-00-00-00-00")


def _inv(tmp_path, arp, clock, deck=None):
    return n.LanInventory(path=tmp_path / "lan.json", arp=lambda: list(arp), resolve=lambda ip: "",
                          deck_names=lambda: deck or {}, clock=lambda: clock[0])


def test_first_scan_is_the_baseline_then_new_devices_are_flagged(tmp_path):
    arp, clock = [("192.168.0.25", "00-16-3e-00-00-25")], [1000.0]
    inv = _inv(tmp_path, arp, clock, deck={"00-16-3e-00-00-25": "iPad"})
    inv.scan()
    s = inv.status()
    assert s["new"] == 0 and s["alert"] == "" and s["devices"][0]["name"] == "iPad"
    arp.append(("192.168.0.77", "00-1b-63-dd-ee-ff"))
    clock[0] += 60
    inv.scan()
    s = inv.status()
    assert s["new"] == 1 and s["alert"] == "new device on the network: 192.168.0.77"
    assert s["devices"][0]["ip"] == "192.168.0.77"                   # new ones first
    clock[0] += n.NEW_FOR_S + 1
    assert inv.status()["new"] == 0                                   # "new" for a day


def test_inventory_survives_a_restart(tmp_path):
    arp, clock = [("192.168.0.25", "00-16-3e-00-00-25")], [1000.0]
    _inv(tmp_path, arp, clock).scan()
    again = _inv(tmp_path, arp + [("192.168.0.9", "00-16-3e-00-00-09")], clock)
    again.scan()
    s = again.status()
    assert len(s["devices"]) == 2 and s["new"] == 1                   # the restart isn't a new baseline


def test_offline_after_15_minutes(tmp_path):
    arp, clock = [("192.168.0.25", "00-16-3e-00-00-25")], [1000.0]
    inv = _inv(tmp_path, arp, clock)
    inv.scan()
    arp.clear()
    clock[0] += 16 * 60
    inv.scan()
    s = inv.status()
    assert s["online"] == 0 and len(s["devices"]) == 1


def test_names_are_looked_up_a_few_at_a_time(tmp_path):
    arp = [(f"192.168.0.{i}", f"aa-00-00-00-00-{i:02x}") for i in range(10, 15)]
    looked = []
    inv = n.LanInventory(path=tmp_path / "l.json", arp=lambda: arp, resolve=lambda ip: looked.append(ip) or "",
                         deck_names=lambda: {}, clock=lambda: 1000.0)
    inv.scan()
    assert len(looked) == n.NAMES_PER_SCAN


def test_deck_placeholder_names_are_ignored(tmp_path):
    p = tmp_path / "known.json"
    p.write_text('[{"mac":"AA-BB-CC-00-00-01","name":"Device (192.168.0.48)"},{"mac":"aa-bb-cc-00-00-02","name":"Printer"}]')
    assert n._deck_names(p) == {"aa-bb-cc-00-00-02": "Printer"}


# ---- the ticker

def test_ticker_attention_lines():
    lines = attention(Snapshot(net_alert="Pi-hole down: no DNS (the internet will look dead)",
                               net_new_device="new device on the network: 192.168.0.77"))
    texts = [(c, t, tag) for c, t, tag in lines if tag == "NET"]
    assert len(texts) == 2 and "Pi-hole down" in texts[0][1]
    assert attention(Snapshot()) == [] or all(tag != "NET" for _c, _t, tag in attention(Snapshot()))


# ---- review fixes (2026-11-07)

def test_private_macs_are_listed_but_never_new(tmp_path):
    arp, clock = [("192.168.0.25", "00-16-3e-00-00-25")], [1000.0]
    inv = _inv(tmp_path, arp, clock)
    inv.scan()
    arp.append(("192.168.0.70", "ba-11-22-33-44-55"))                  # 0xba: locally administered
    clock[0] += 60
    inv.scan()
    s = inv.status()
    priv = [d for d in s["devices"] if d["mac"] == "ba-11-22-33-44-55"][0]
    assert priv["private"] and not priv["new"] and s["alert"] == ""


def test_devices_unseen_for_30_days_are_forgotten(tmp_path):
    arp, clock = [("192.168.0.25", "00-16-3e-00-00-25")], [1000.0]
    inv = _inv(tmp_path, arp, clock)
    inv.scan()
    arp.clear()
    clock[0] += n.FORGET_AFTER_S + 1
    inv.scan()
    assert inv.status()["devices"] == []


def test_a_failing_probe_says_unknown_not_the_old_ok(monkeypatch):
    class Boom:
        def probe(self):
            raise RuntimeError("weird reply")
    monkeypatch.setattr(n, "_PIHOLE", Boom())
    monkeypatch.setattr(n, "_LAN", type("L", (), {"scan": lambda self: None,
                                                    "status": lambda self: {"devices": [], "online": 0, "new": 0, "alert": ""}})())
    monkeypatch.setattr(n, "_last", {"pihole": {"state": "ok", "alert": ""}, "lan": {}})
    assert n.network_status(max_age=0)["pihole"]["state"] == "unknown"


def test_pihole_info_survives_odd_replies(monkeypatch):
    import io
    class R(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(n.urllib.request, "urlopen", lambda *a, **k: R(b"[1, 2]"))
    assert n.pihole_info("x") == (None, None)                         # valid JSON, not an object
    def boom(*a, **k):
        import http.client
        raise http.client.BadStatusLine("x")
    monkeypatch.setattr(n.urllib.request, "urlopen", boom)
    assert n.pihole_info("x") == (None, None)


def test_monitor_starts_once():
    import threading
    started = []
    real = threading.Thread
    class T(real):
        def start(self): started.append(1)
    n._thread = None
    try:
        n.threading.Thread = T
        ts = [real(target=n.start_network_monitor) for _ in range(8)]
        for t in ts: t.start()
        for t in ts: t.join()
    finally:
        n.threading.Thread = real
        n._thread = None
    assert started == [1]
