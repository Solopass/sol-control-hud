import json
import time

from fastapi.testclient import TestClient

from sol_control_hud.views.web.app import Cached, create_app
from sol_control_hud.data.collectors import gpu, vram
from sol_control_hud.doctor import FAIL, OK, WARN, checks

DGPU = "luid_0x00000000_0x0000d1a2_phys_0"
IGPU = "luid_0x00000000_0x0000beef_phys_0"
GB = 1024**3


def pm(pid, luid=DGPU):
    return rf"\GPU Process Memory(pid_{pid}_{luid})\Dedicated Usage"


def ps(pid, luid=DGPU):
    return rf"\GPU Process Memory(pid_{pid}_{luid})\Shared Usage"


NAMES = {100: "llama-server", 200: "dwm", 300: "brave", 400: "brave", 500: "Discord", 600: "vivaldi"}


def gpu_block(dedicated: dict, shared: dict, total=15.9, sampled_at=None, used=None):
    if used is None:  # default: no double counting, card total = sum of per-process on the dGPU
        used = sum(v for k, v in dedicated.items() if DGPU in k)
    block = gpu.summarize(
        util={rf"\GPU Engine(pid_100_{DGPU}_eng_0_engtype_Compute)\Utilization Percentage": 97.0},
        adapter_mem={rf"\GPU Adapter Memory({DGPU})\Dedicated Usage": used * GB,
                     rf"\GPU Adapter Memory({IGPU})\Dedicated Usage": 0.2 * GB},
        proc_dedicated={k: v * GB for k, v in dedicated.items()},
        proc_shared={k: v * GB for k, v in shared.items()},
        name_of=lambda pid: NAMES.get(pid, f"pid {pid}"),
        info={"name": "AMD Radeon RX 9070 XT", "vram_total_gb": total},
    )
    if sampled_at is not None:
        block["sampled_at"] = sampled_at
    return block


# the real 2026-09-16 13:25 reading
EVICTED_READING = dict(
    dedicated={pm(100): 10.47, pm(200): 2.12, pm(300): 0.6, pm(400): 0.45, pm(500): 0.36, pm(600): 0.39, pm(600, IGPU): 3.0},
    shared={ps(100): 1.76, ps(200): 0.05, ps(300): 0.02},
)
OLLAMA_LOADED = {"up": True, "loaded": [{"name": "sol-fast:latest", "context": 16384, "gpu_percent": 100}]}


def test_summarize_sums_per_pid_and_ignores_other_adapters():
    g = gpu_block(**EVICTED_READING)
    by_pid = {p["pid"]: p for p in g["processes"]}
    assert by_pid[100] == {"pid": 100, "name": "llama-server", "dedicated_gb": 10.47, "shared_gb": 1.76}
    assert by_pid[600]["dedicated_gb"] == 0.39           # 3 GB on the iGPU must not count against the card
    assert g["processes"][0]["pid"] == 100               # sorted by dedicated
    assert g["load_percent"] == 97.0 and g["vram_used_gb"] == 14.39


def test_budget_uses_the_card_total_not_the_double_counted_process_sum(tmp_path):
    # real 2026-09-16 16:02 reading: processes summed to 18.95 GB on a card holding 14.86 GB
    ded = {pm(100): 11.31, pm(200): 4.39, pm(300): 0.76, pm(600): 0.44, pm(500): 0.31, pm(400): 1.74}
    v = guard(tmp_path, confirm=1).update(gpu_block(ded, {ps(100): 0.92}, used=14.86), OLLAMA_LOADED)
    assert v["others_gb"] == 3.55 and v["used_gb"] == 14.86
    assert v["spare_gb"] == 1.04 and v["evicted"] is True
    assert abs(sum(t["gb"] for t in v["top_consumers"]) - 3.55) < 0.1     # apportioned, adds up to the truth


def test_guard_loop_detects_eviction_without_page_polls(tmp_path):
    class Sampler:
        n = 0
        @property
        def latest(self):
            self.n += 1
            return gpu_block(**EVICTED_READING, sampled_at=float(self.n))
    loop = vram.GuardLoop(vram.VramGuard(vram.NeedStore(tmp_path / "n.json")), Sampler(), lambda: OLLAMA_LOADED, interval=0.01)
    loop.start()
    try:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not loop.latest.get("evicted"):
            time.sleep(0.01)
    finally:
        loop.stop()
    assert loop.latest["evicted"] is True


def guard(tmp_path, confirm=2):
    return vram.VramGuard(vram.NeedStore(tmp_path / "needs.json"), confirm=confirm)


def test_eviction_needs_two_distinct_samples(tmp_path):
    gd = guard(tmp_path)
    first = gd.update(gpu_block(**EVICTED_READING, sampled_at=1.0), OLLAMA_LOADED)
    assert first["evicted"] is False
    same_sample_again = gd.update(gpu_block(**EVICTED_READING, sampled_at=1.0), OLLAMA_LOADED)
    assert same_sample_again["evicted"] is False          # a second poll of the same sample doesn't count
    second = gd.update(gpu_block(**EVICTED_READING, sampled_at=2.0), OLLAMA_LOADED)
    assert second["evicted"] is True and second["verdict"] == "EVICTED"
    assert second["ai"] == {"processes": ["llama-server"], "dedicated_gb": 10.47, "shared_gb": 1.76, "models": ["sol-fast:latest"]}
    names = [s["name"] for s in second["suggest_free"]]
    assert names[:2] == ["brave", "vivaldi"] and "dwm" not in names   # brave = two processes summed (1.05 GB)


def test_one_shot_guard_flags_immediately(tmp_path):
    v = guard(tmp_path, confirm=1).update(gpu_block(**EVICTED_READING), OLLAMA_LOADED)
    assert v["evicted"] is True


def test_evicted_run_does_not_learn_a_size(tmp_path):
    gd = guard(tmp_path, confirm=1)
    gd.update(gpu_block(**EVICTED_READING), OLLAMA_LOADED)
    assert gd.needs.learned == {}


def test_healthy_load_learns_size_and_reports_ok(tmp_path):
    gd = guard(tmp_path, confirm=1)
    v = gd.update(gpu_block({pm(100): 12.1, pm(200): 2.1}, {ps(100): 0.02}), OLLAMA_LOADED)
    assert v["verdict"] == "OK" and v["evicted"] is False and v["spare_gb"] == 1.7 and v["others_gb"] == 2.1
    assert gd.needs.learned == {"sol-fast@16384": 12.1}
    assert vram.NeedStore(tmp_path / "needs.json").get("sol-fast", 16384) == (12.1, 16384, "learned")   # persisted


def test_verdict_without_a_loaded_model(tmp_path):
    none_loaded = {"up": True, "loaded": []}
    fits = guard(tmp_path).update(gpu_block({pm(200): 2.1}, {}), none_loaded)
    assert fits["verdict"] == "FITS" and fits["need"]["source"] == "estimate"
    # sol-fast's seed need is 7.9 GB since v2 (Gemma 12B): card 15.9 - others 7.6 - 7.9 = 0.4 spare -> TIGHT
    tight = guard(tmp_path).update(gpu_block({pm(200): 2.1, pm(300): 5.5}, {}), none_loaded)
    assert tight["verdict"] == "TIGHT"
    wont = guard(tmp_path).update(gpu_block({pm(200): 2.3, pm(300): 5.5, pm(500): 0.4, pm(600): 0.4}, {}), none_loaded)
    assert wont["verdict"] == "WONT_FIT" and wont["spare_gb"] < 0


def test_unavailable_gpu(tmp_path):
    assert guard(tmp_path).update({"available": False, "error": "no counters"}, {"up": False})["available"] is False


def test_status_and_doctor_report_eviction(tmp_path):
    v = guard(tmp_path, confirm=1).update(gpu_block(**EVICTED_READING), OLLAMA_LOADED)
    client = TestClient(create_app(collectors={"vram": Cached(lambda: v, ttl=0)}))
    assert client.get("/api/status").json()["vram"]["verdict"] == "EVICTED"

    from tests.test_doctor import HEALTHY
    lv = {n.strip(): l for n, l, _ in checks({**HEALTHY, "vram": v})}
    assert lv["Model eviction"] == FAIL and lv["VRAM headroom"] == WARN
    ok = guard(tmp_path, confirm=1).update(gpu_block({pm(100): 12.1, pm(200): 2.1}, {}), OLLAMA_LOADED)
    lv = {n.strip(): l for n, l, _ in checks({**HEALTHY, "vram": ok})}
    assert lv["Model eviction"] == OK and lv["VRAM headroom"] == OK


def test_guard_loop_requests_rescan_on_model_change(tmp_path):
    class FakeSampler:
        latest = {"available": False}
        rescans = 0
        def request_rescan(self): self.rescans += 1

    sampler = FakeSampler()
    models = [{"name": "sol-fast:latest"}]
    loop = vram.GuardLoop(vram.VramGuard(tmp_path / "n.json"), sampler, lambda: {"up": True, "loaded": models}, interval=0.01)
    loop.start()
    try:
        time.sleep(0.03)
        assert sampler.rescans == 0
        models = [{"name": "sol-vision:latest"}]  # model changed
        time.sleep(0.04)
        assert sampler.rescans >= 1
    finally:
        loop.stop()


class FakePdh:
    """Stands in for win32pdh: counter paths come from an expander whose instances change over time."""
    PDH_FMT_DOUBLE, PDH_FMT_LARGE = 1, 2

    def __init__(self, values):
        self.values = values

    def OpenQuery(self): return object()
    def CloseQuery(self, q): pass
    def CollectQueryData(self, q): pass
    def AddCounter(self, q, path): return path
    def GetFormattedCounterValue(self, h, fmt): return (0, self.values[h])


def test_sampler_picks_up_a_process_that_starts_later(monkeypatch):
    values = {rf"\GPU Adapter Memory({DGPU})\Dedicated Usage": 12 * GB, pm(200): 2 * GB, pm(100): 10 * GB}
    monkeypatch.setattr(gpu, "win32pdh", FakePdh(values))
    monkeypatch.setattr(gpu, "adapter_info", lambda: {"name": "RX 9070 XT", "vram_total_gb": 15.9})
    started = time.monotonic()

    def expand(counter):
        late = time.monotonic() - started > 0.15          # llama-server "starts" after the first build
        if counter == gpu.ADAPTER_MEM:
            return [rf"\GPU Adapter Memory({DGPU})\Dedicated Usage"]
        if counter == gpu.PROC_DEDICATED:
            return [pm(200)] + ([pm(100)] if late else [])
        return []

    s = gpu.GpuSampler(interval=0.02, rescan=0.2, expand=expand)
    monkeypatch.setattr(gpu, "process_name", lambda pid, cache: NAMES[pid])
    s.start()
    try:
        deadline = time.monotonic() + 3
        seen_before = seen_after = False
        while time.monotonic() < deadline:
            pids = {p["pid"] for p in s.latest.get("processes", [])}
            seen_before |= pids == {200}
            if 100 in pids:
                seen_after = True
                break
            time.sleep(0.01)
    finally:
        s.stop()
    assert seen_before and seen_after


def test_learned_sizes_are_written_atomically(tmp_path):
    """A crash mid-write must not leave half a JSON file: write to .tmp, then rename (Code review chain, 2026-09-29)."""
    path = tmp_path / "needs.json"
    store = vram.NeedStore(path)
    store.learn("sol-fast", 32768, 7.9)
    assert json.loads(path.read_text(encoding="utf-8")) == {"sol-fast@32768": 7.9}
    assert not list(tmp_path.glob("*.tmp")), "the temporary file must be renamed, not left behind"
    store.learn("sol-smart", 32768, 12.2)          # a second write replaces the file, still valid JSON
    assert len(json.loads(path.read_text(encoding="utf-8"))) == 2
