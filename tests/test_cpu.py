"""The CPU half of the CPU · GPU card: per-core load, P-cores vs E-cores, busiest core, live clock."""
from sol_control_hud.data.collectors import cpu


def test_split_pairs_hyperthreads_into_p_cores():
    threads = [80, 20, 0, 0] + [10, 30, 0]          # 2 P-cores (2 threads each) + 3 E-cores = 7 threads, 5 physical
    got = cpu.split(threads, physical=5)
    assert got["p_cores"] == 2 and got["e_cores"] == 3
    assert got["cores"] == [50.0, 0.0, 10.0, 30.0, 0.0]
    assert got["p_load"] == 25.0 and got["e_load"] == 13.3
    assert got["busiest"] == {"name": "P0", "load": 50.0}


def test_split_without_hyperthreading_is_all_e():
    got = cpu.split([5, 60], physical=2)
    assert got["p_cores"] == 0 and got["p_load"] is None and got["busiest"] == {"name": "E1", "load": 60}


def test_sample_reads_this_pc():
    s = cpu.sample()
    assert s["available"] and s["threads"] >= 2 and 0 <= s["load"] <= 100
    assert len(s["cores"]) == s["p_cores"] + s["e_cores"]


def test_utility_parsed_like_task_manager():
    from sol_control_hud.data.collectors.cpu import parse_utility
    arr = {"0,0": 12.0, "0,1": 140.0, "0,10": 3.0, "0,2": -1.0, "0,_Total": 9.1, "_Total": 9.1}
    total, per = parse_utility(arr)
    assert total == 9.1
    assert per == [12.0, 100.0, 0.0, 3.0]          # ordered by CPU number (0,1,2,10), capped 0..100 like Task Manager
    assert parse_utility({}) is None and parse_utility({"_Total": 5.0}) is None
