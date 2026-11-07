"""The home network: is Pi-hole answering and blocking, and which devices are on the LAN (2026-11-07, sol-net plan P1
and idea 7). Read by the dashboard's Network card and the ticker's attention line.

Why: DNS for this PC is one Pi-hole box (192.168.0.249 and .248 are the same machine, one MAC), so when it is down
the whole internet looks dead. Saying "Pi-hole down" plainly saves hunting the wrong problem.

How, with no password and no new process:
- Up: Pi-hole v6's public `GET /api/info/login` ({"dns": true} = its DNS service runs), every 30 s per address.
- Through this PC's own resolver (what every app here really gets): a real domain every 5 min (Pi-hole up but its
  upstream broken looks like "internet down") and a known ad domain every 10 min (rarely, so SOL doesn't pad
  Pi-hole's "blocked" numbers).
- Direct DNS: a raw query for `pi.hole` straight to each address. Found 2026-11-07: Pi-hole answered Windows' DNS
  service but ignored direct queries from this PC (nslookup, dig, this probe), most likely its per-client rate
  limit. The card shows that as a note; it is not a ticker alert.
- Devices: the Windows ARP table (GetIpNetTable, no process started), names from Pi-hole reverse DNS, plus the names
  sol-deck's watchdog already gave in its known_devices.json (read only). Seen devices are remembered in
  data/lan-devices.json; a MAC never seen before is a "new device" for a day.
- Stats (queries today, % blocked, pause/resume blocking) come through sol-deck (127.0.0.1:5000), which already
  holds the Pi-hole app password in its own .env, so the HUD stores no Pi-hole secret. Once a minute at most.
"""
from __future__ import annotations

import ctypes
import json
import os
import random
import socket
import struct
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from ...paths import DATA_DIR
from ...swallow import note as _swallowed

PIHOLE_HOSTS = tuple(h.strip() for h in os.environ.get("SOL_PIHOLE_HOSTS", "192.168.0.249,192.168.0.248").split(",") if h.strip())
SUBNET_PREFIX = os.environ.get("SOL_LAN_PREFIX", "192.168.0.")
UP_EVERY, UPSTREAM_EVERY, BLOCK_EVERY = 30.0, 300.0, 600.0
UPSTREAM_NAME, BLOCKED_NAME = "example.com", "doubleclick.net"
DECK_URL = os.environ.get("SOL_DECK_URL", "http://127.0.0.1:5000")
STATS_EVERY = 60.0
SLOW_MS = 150.0
DEVICES_FILE = DATA_DIR / "lan-devices.json"
DECK_DEVICES = Path(r"D:\Workspace\sol-deck\data\known_devices.json")
NEW_FOR_S = 24 * 3600.0
FORGET_AFTER_S = 30 * 24 * 3600.0     # a device unseen for 30 days drops off the list
NAME_EVERY = 3600.0
NAMES_PER_SCAN = 2
NEVER = float("-inf")       # "not checked yet": due on any clock

# ---------------------------------------------------------------------------------------------------- DNS (stdlib)
QTYPE_A, QTYPE_PTR = 1, 12


def _qname(name: str) -> bytes:
    return b"".join(bytes([len(p)]) + p.encode("ascii") for p in name.rstrip(".").split(".")) + b"\0"


def build_query(name: str, qtype: int, qid: int) -> bytes:
    return struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0) + _qname(name) + struct.pack(">HH", qtype, 1)


def _skip_name(buf: bytes, i: int) -> int:
    while True:
        n = buf[i]
        if n == 0:
            return i + 1
        if n & 0xC0 == 0xC0:
            return i + 2
        i += n + 1


def _read_name(buf: bytes, i: int, depth: int = 0) -> str:
    parts = []
    while depth < 10:
        n = buf[i]
        if n == 0:
            break
        if n & 0xC0 == 0xC0:
            parts.append(_read_name(buf, ((n & 0x3F) << 8) | buf[i + 1], depth + 1))
            break
        parts.append(buf[i + 1:i + 1 + n].decode("ascii", "replace"))
        i += n + 1
    return ".".join(p for p in parts if p)


def parse_response(buf: bytes, qid: int) -> tuple[int, list[str]]:
    """(rcode, answers): A records as dotted IPs, PTR records as names. Raises ValueError on a reply that isn't ours."""
    if len(buf) < 12:
        raise ValueError("short reply")
    rid, flags, qd, an = struct.unpack(">HHHH", buf[:8])
    if rid != qid or not flags & 0x8000:
        raise ValueError("not our reply")
    i = 12
    for _ in range(qd):
        i = _skip_name(buf, i) + 4
    out = []
    for _ in range(an):
        i = _skip_name(buf, i)
        rtype, _cls, _ttl, rdlen = struct.unpack(">HHIH", buf[i:i + 10])
        i += 10
        if rtype == QTYPE_A and rdlen == 4:
            out.append(socket.inet_ntoa(buf[i:i + 4]))
        elif rtype == QTYPE_PTR:
            out.append(_read_name(buf, i))
        i += rdlen
    return flags & 0x000F, out


def dns_query(server: str, name: str, qtype: int = QTYPE_A, timeout: float = 1.5) -> tuple[bool, int, list[str], float]:
    """(answered, rcode, answers, ms). answered=False: no reply in time (server down or unreachable)."""
    qid = random.randint(1, 0xFFFF)
    t0 = time.perf_counter()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(timeout)
            s.sendto(build_query(name, qtype, qid), (server, 53))
            while True:
                buf, _ = s.recvfrom(1500)
                try:
                    rcode, answers = parse_response(buf, qid)
                    return True, rcode, answers, (time.perf_counter() - t0) * 1000
                except (ValueError, struct.error, IndexError):
                    continue       # a stray or truncated datagram: keep waiting until the timeout
    except OSError:
        return False, -1, [], (time.perf_counter() - t0) * 1000


def is_blocked_answer(rcode: int, answers: list[str]) -> bool:
    """Pi-hole's default blocking answer is 0.0.0.0 (NULL mode); NXDOMAIN mode answers rcode 3."""
    return rcode == 3 or (bool(answers) and all(a == "0.0.0.0" for a in answers))


# ---------------------------------------------------------------------------------------------------- Pi-hole status
def pihole_info(host: str, timeout: float = 2.0) -> tuple[bool | None, float | None]:
    """Pi-hole v6's public login-info endpoint: (dns service running, ms). (None, None) = no answer at all."""
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(f"http://{host}/api/info/login", timeout=timeout) as r:
            data = json.loads(r.read(2048) or b"{}")
        if not isinstance(data, dict):
            return None, None
        return bool(data.get("dns")), (time.perf_counter() - t0) * 1000
    except Exception:          # any failure to get a sane answer (HTTPException, bad JSON...) = no answer
        return None, None


def os_resolve(name: str) -> tuple[bool, list[str]]:
    """This PC's own resolver (Windows' DNS service, so Pi-hole as every app here sees it): (answered, IPv4 list)."""
    try:
        infos = socket.getaddrinfo(name, None, socket.AF_INET)
        return True, sorted({i[4][0] for i in infos})
    except OSError:
        return False, []


def _deck_json(path: str, body: dict | None = None, timeout: float = 4.0) -> dict | None:
    """GET (or POST `body`) sol-deck over loopback, which needs no PIN. None = sol-deck unreachable."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(DECK_URL + path, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8", "replace"))
        except Exception:
            _swallowed("network._deck_json.http_error")
            return {"ok": False, "error": f"HTTP {e.code}"}
    except Exception:
        _swallowed("network._deck_json")
        return None


def deck_pihole_stats() -> dict | None:
    return _deck_json("/api/pihole/stats")


def stats_from_deck(d: dict | None) -> dict:
    """sol-deck's /api/pihole/stats -> the card's numbers. state: ok | no-password | no-deck | error."""
    if d is None:
        return {"state": "no-deck", "why": "sol-deck isn't running"}
    if not d.get("ok"):
        err = str(d.get("error") or "unknown error")
        if "PIHOLE_APP_PASSWORD" in err:
            return {"state": "no-password", "why": "needs a Pi-hole app password in sol-deck's .env"}
        return {"state": "error", "why": err[:120]}
    s = d.get("summary") or {}
    q = s.get("queries") or {}
    pct = q.get("percent_blocked")
    return {
        "state": "ok",
        "blocking": d.get("blocking"),
        "timer": int(d.get("timer") or 0),
        "queries": q.get("total"),
        "blocked": q.get("blocked"),
        "percent": round(pct, 1) if isinstance(pct, (int, float)) else None,
        "domains": (s.get("gravity") or {}).get("domains_being_blocked"),
        "clients": (s.get("clients") or {}).get("active"),
        "top_blocked": [x.get("domain") for x in (d.get("top_blocked") or []) if isinstance(x, dict) and x.get("domain")][:5],
    }


def pihole_pause(seconds: int = 300) -> dict:
    """Pause Pi-hole blocking for `seconds` (through sol-deck)."""
    r = _deck_json("/api/pihole/toggle", {"action": "snooze", "duration": int(seconds)})
    if r is None:
        return {"ok": False, "why": "sol-deck isn't running"}
    if r.get("ok"):
        _PIHOLE.refresh_stats()
        return {"ok": True, "why": f"Pi-hole blocking paused for {int(seconds) // 60} min"}
    return {"ok": False, "why": stats_from_deck(r).get("why") or "Pi-hole refused"}


def pihole_resume() -> dict:
    """Turn Pi-hole blocking back on now (through sol-deck)."""
    r = _deck_json("/api/pihole/toggle", {"action": "enable"})
    if r is None:
        return {"ok": False, "why": "sol-deck isn't running"}
    if r.get("ok"):
        _PIHOLE.refresh_stats()
        return {"ok": True, "why": "Pi-hole blocking is back on"}
    return {"ok": False, "why": stats_from_deck(r).get("why") or "Pi-hole refused"}


class PiholeMonitor:
    """Probes each Pi-hole address on its own schedule; `status()` only reads what the probes found.
    `stats` (sol-deck's Pi-hole numbers) is off unless given, so tests never make real HTTP calls."""

    def __init__(self, hosts=PIHOLE_HOSTS, info=pihole_info, query=dns_query, resolve=os_resolve, clock=time.time,
                 stats=None):
        self.hosts, self._info, self._query, self._resolve, self._clock = tuple(hosts), info, query, resolve, clock
        self._stats_fn, self._stats, self._stats_at = stats, {"state": "off"}, NEVER
        self._lock = threading.Lock()
        self._hosts = {h: {"ip": h, "up": None, "dns": None, "ms": None, "direct": None, "checked": 0.0, "_at": NEVER}
                       for h in self.hosts}
        self._pc = {"upstream": None, "blocking": None, "_upstream_at": NEVER, "_block_at": NEVER}

    def probe(self) -> None:
        now = self._clock()
        for h, st in self._hosts.items():
            if now - st["_at"] < UP_EVERY:
                continue
            dns, ms = self._info(h)
            direct = self._query(h, "pi.hole", timeout=1.0)[0] if dns else None
            with self._lock:
                st.update(up=dns is not None, dns=dns, ms=round(ms) if ms is not None else None, direct=direct,
                          checked=now, _at=now)
        if not any(st["up"] for st in self._hosts.values()):
            return
        if self._stats_fn is not None and now - self._stats_at >= STATS_EVERY:
            st = stats_from_deck(self._stats_fn())
            with self._lock:
                self._stats, self._stats_at = st, now
        if now - self._pc["_upstream_at"] >= UPSTREAM_EVERY:
            ok, ips = self._resolve(UPSTREAM_NAME)
            with self._lock:
                self._pc.update(upstream=ok and bool(ips) and not is_blocked_answer(0, ips), _upstream_at=now)
        if now - self._pc["_block_at"] >= BLOCK_EVERY:
            ok, ips = self._resolve(BLOCKED_NAME)
            with self._lock:
                self._pc.update(blocking=(not ok) or is_blocked_answer(0, ips), _block_at=now)   # NXDOMAIN counts

    def status(self) -> dict:
        with self._lock:
            hosts = [{k: v for k, v in st.items() if not k.startswith("_")} for st in self._hosts.values()]
            pc = {k: v for k, v in self._pc.items() if not k.startswith("_")}
            stats = dict(self._stats)
        out = summarize(hosts, pc)
        out["stats"] = stats
        return out

    def refresh_stats(self) -> None:
        """Fetch the stats on the next probe (after a pause/resume)."""
        with self._lock:
            self._stats_at = NEVER


def summarize(hosts: list[dict], pc: dict | None = None) -> dict:
    """state: ok | slow | degraded (one address down) | dns-off | no-upstream | not-blocking | down | unknown.
    alert = the ticker's line ("" = nothing to say); note = a quieter line for the card only."""
    pc = pc or {}
    known = [h for h in hosts if h.get("up") is not None]
    up = [h for h in known if h["up"]]
    serving = [h for h in up if h.get("dns")]
    state, alert, note = "ok", "", ""
    if not known:
        state = "unknown"
    elif not up:
        state, alert = "down", "Pi-hole down: no DNS (the internet will look dead)"
    elif not serving:
        state, alert = "dns-off", "Pi-hole's DNS service is off (its web page still answers)"
    elif pc.get("upstream") is False:
        state, alert = "no-upstream", "Pi-hole up but its upstream DNS fails"
    elif pc.get("blocking") is False:
        state, alert = "not-blocking", "Pi-hole is not blocking ads for this PC"
    elif len(serving) < len(known):
        state = "degraded"
    elif any((h.get("ms") or 0) > SLOW_MS for h in serving):
        state = "slow"
    if serving and all(h.get("direct") is False for h in serving):
        note = ("answers Windows' DNS but ignores direct queries from this PC (nslookup, dig): "
                "check Pi-hole's rate limit / diagnosis page for this PC's address")
    return {"state": state, "alert": alert, "note": note, "hosts": hosts, "pc": pc}


# ---------------------------------------------------------------------------------------------------- LAN devices
class _MIB_IPNETROW(ctypes.Structure):
    _fields_ = [("dwIndex", ctypes.c_uint32), ("dwPhysAddrLen", ctypes.c_uint32), ("bPhysAddr", ctypes.c_ubyte * 8),
                ("dwAddr", ctypes.c_uint32), ("dwType", ctypes.c_uint32)]


def arp_table() -> list[tuple[str, str]]:
    """(ip, mac) from the Windows ARP cache (GetIpNetTable: no process, no packets sent). Invalid entries skipped."""
    if os.name != "nt":
        return []
    iphlp = ctypes.windll.iphlpapi
    size = ctypes.c_ulong(0)
    iphlp.GetIpNetTable(None, ctypes.byref(size), False)
    if size.value < 4:                                       # ERROR_NO_DATA: an empty table
        return []
    buf = ctypes.create_string_buffer(size.value)
    if iphlp.GetIpNetTable(buf, ctypes.byref(size), False) != 0:
        return []
    count = ctypes.c_uint32.from_buffer(buf).value
    if count == 0 or 4 + count * ctypes.sizeof(_MIB_IPNETROW) > len(buf):
        return []
    rows = (_MIB_IPNETROW * count).from_buffer(buf, 4)
    out = []
    for r in rows:
        if r.dwType == 2 or r.dwPhysAddrLen != 6:           # 2 = invalid
            continue
        ip = socket.inet_ntoa(struct.pack("<I", r.dwAddr))
        mac = "-".join(f"{b:02x}" for b in r.bPhysAddr[:6])
        out.append((ip, mac))
    return out


def lan_entry(ip: str, mac: str, prefix: str = SUBNET_PREFIX) -> bool:
    """A real device on our subnet: not broadcast, multicast or an empty MAC."""
    if not ip.startswith(prefix) or ip.endswith(".255") or ip.endswith(".0"):
        return False
    first = int(mac.split("-")[0], 16)
    return mac not in ("ff-ff-ff-ff-ff-ff", "00-00-00-00-00-00") and not first & 1


def private_mac(mac: str) -> bool:
    """A locally administered (randomised) MAC, as phones use for "private Wi-Fi address": it can change, so a new
    one isn't news. Bit 0x02 of the first byte."""
    try:
        return bool(int(mac.split("-")[0], 16) & 0x02)
    except ValueError:
        return False


def _deck_names(path: Path = DECK_DEVICES) -> dict[str, str]:
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out = {}
    for r in rows if isinstance(rows, list) else []:
        name, mac = (r.get("name") or "").strip(), (r.get("mac") or "").lower()
        if mac and name and not name.startswith(("Device (", "LAN Device (")):   # its placeholders say nothing new
            out[mac] = name
    return out


class LanInventory:
    def __init__(self, path: Path = DEVICES_FILE, arp=arp_table, resolve=None, deck_names=_deck_names, clock=time.time):
        self.path, self._arp, self._deck, self._clock = path, arp, deck_names, clock
        self._resolve = resolve or self._ptr
        self._lock = threading.Lock()
        self._names_at: dict[str, float] = {}
        self.devices: dict[str, dict] = self._load()
        self._baseline = not self.devices                    # first run ever: what's there now isn't "new"

    def _load(self) -> dict[str, dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.devices, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            _swallowed("network.LanInventory._save")

    @staticmethod
    def _ptr(ip: str) -> str:
        """Reverse name through Windows' resolver (Pi-hole answers it, from DHCP/local records). A miss can take a few
        seconds (NetBIOS fallback), hence at most NAMES_PER_SCAN per scan."""
        try:
            name = socket.gethostbyaddr(ip)[0]
        except OSError:
            return ""
        return "" if name == ip else name.removesuffix(".lan").removesuffix(".local")

    def scan(self) -> None:
        now = self._clock()
        deck = self._deck()
        changed, lookups = False, 0
        for ip, mac in self._arp():
            if not lan_entry(ip, mac):
                continue
            with self._lock:
                d = self.devices.get(mac)
                if d is None:
                    d = self.devices[mac] = {"ip": ip, "name": "", "first_seen": now, "last_seen": now,
                                             "baseline": self._baseline}
                d["ip"], d["last_seen"] = ip, now
            changed = True
            name = deck.get(mac, "")
            if not name and lookups < NAMES_PER_SCAN and now - self._names_at.get(mac, NEVER) >= NAME_EVERY:
                self._names_at[mac] = now
                lookups += 1
                name = self._resolve(ip)
            if name:
                with self._lock:
                    d["name"] = name
        self._baseline = False
        with self._lock:                                     # private (rotating) phone MACs would pile up forever
            stale = [m for m, d in self.devices.items() if now - d.get("last_seen", 0) > FORGET_AFTER_S]
            for m in stale:
                del self.devices[m]
        if changed or stale:
            self._save()

    def status(self) -> dict:
        now = self._clock()
        with self._lock:
            rows = []
            for mac, d in self.devices.items():
                private = private_mac(mac)
                new = not d.get("baseline") and not private and now - d.get("first_seen", 0) < NEW_FOR_S
                rows.append({"mac": mac, "ip": d.get("ip", ""), "name": d.get("name", ""), "new": new, "private": private,
                             "online": now - d.get("last_seen", 0) < 15 * 60,
                             "first_seen": datetime.fromtimestamp(d.get("first_seen", 0)).isoformat(timespec="minutes"),
                             "last_seen": datetime.fromtimestamp(d.get("last_seen", 0)).isoformat(timespec="minutes")})
        rows.sort(key=lambda r: (not r["new"], not r["online"], [int(x) for x in r["ip"].split(".") if x.isdigit()]))
        new = [r for r in rows if r["new"]]
        alert = ""
        if new:
            who = new[0]["name"] or new[0]["ip"]
            alert = f"new device on the network: {who}" + (f" (+{len(new) - 1} more)" if len(new) > 1 else "")
        return {"devices": rows, "online": sum(r["online"] for r in rows), "new": len(new), "alert": alert}


# ---------------------------------------------------------------------------------------------------- one shared state
_PIHOLE = PiholeMonitor(stats=deck_pihole_stats)
_LAN: LanInventory | None = None
_last: dict = {}
_last_at = 0.0
_state_lock = threading.Lock()


def network_status(max_age: float = 30.0) -> dict:
    """Pi-hole + LAN for the dashboard (Cached in the hub) and the ticker; probes at most every `max_age` s."""
    global _LAN, _last, _last_at
    with _state_lock:
        if _last and time.time() - _last_at < max_age:
            return _last
        # apart, so a failing device scan never hides a good Pi-hole result (or the other way round), and a failed
        # probe says "unknown" instead of keeping an old "ok"
        try:
            _PIHOLE.probe()
            pihole = _PIHOLE.status()
        except Exception:
            _swallowed("network.network_status.pihole")
            pihole = {"state": "unknown", "alert": "", "note": "", "hosts": [], "pc": {}}
        try:
            if _LAN is None:
                _LAN = LanInventory()
            _LAN.scan()
            lan = _LAN.status()
        except Exception:
            _swallowed("network.network_status.lan")
            lan = (_last or {}).get("lan") or {"devices": [], "online": 0, "new": 0, "alert": ""}
        _last = {"pihole": pihole, "lan": lan}
        _last_at = time.time()
        return _last


def last_status() -> dict:
    """What the last probe found, without probing (the ticker and the dashboard read this)."""
    return _last


_thread: threading.Thread | None = None
_start_lock = threading.Lock()


def start_network_monitor() -> None:
    """One daemon thread probes every 30 s, so a Pi-hole timeout never stalls the ticker or the page."""
    global _thread
    with _start_lock:                  # the hub and the ticker both call this: one thread, not two
        if _thread is not None:
            return
        _thread = threading.Thread(target=_monitor_loop, name="network-monitor", daemon=True)
        _thread.start()


def _monitor_loop() -> None:
    while True:
        network_status(max_age=0)
        time.sleep(UP_EVERY)
