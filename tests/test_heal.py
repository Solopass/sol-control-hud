"""Auto-heal decisions (plans/SOL_FAST_VRAM_SPILL_PLAN.md step 4). No GPU: decide() is pure."""
import pytest

from sol_control_hud.heal import COOLDOWN_S, MIN_EVICTED_S, Decision, HealState, decide, heal

OK = dict(model="sol-fast", busy=False, fits=True)


def test_not_evicted_clears_the_clock():
    s = HealState(evicted_since=100.0)
    d = decide(s, now=200.0, evicted=False, **OK)
    assert not d.heal and s.evicted_since is None


def test_waits_until_the_spill_has_held_long_enough():
    s = HealState()
    assert not decide(s, now=0.0, evicted=True, **OK).heal          # first sighting starts the clock
    assert not decide(s, now=MIN_EVICTED_S - 1, evicted=True, **OK).heal
    assert decide(s, now=MIN_EVICTED_S + 1, evicted=True, **OK).heal


def test_never_reloads_while_the_model_is_answering():
    s = HealState(evicted_since=0.0)
    d = decide(s, now=MIN_EVICTED_S + 1, evicted=True, model="sol-fast", busy=True, fits=True)
    assert not d.heal and "generating" in d.why


def test_does_not_reload_when_it_would_spill_again():
    s = HealState(evicted_since=0.0)
    d = decide(s, now=MIN_EVICTED_S + 1, evicted=True, model="sol-fast", busy=False, fits=False)
    assert not d.heal and "not fit" in d.why


def test_waits_for_the_driver_to_settle_after_a_game():
    s = HealState(evicted_since=0.0)
    d = decide(s, now=MIN_EVICTED_S + 1, evicted=True, settled_s=5.0, **OK)
    assert not d.heal and "settle" in d.why


def test_one_heal_per_cooldown_and_no_loop_when_it_did_not_help():
    s = HealState(evicted_since=0.0, last_heal=1000.0, last_model="sol-fast")
    again = decide(s, now=1000.0 + COOLDOWN_S / 2, evicted=True, **OK)
    assert not again.heal and "not looping" in again.why          # same model spilled straight back
    s.last_model = "sol-smart"
    other = decide(s, now=1000.0 + COOLDOWN_S / 2, evicted=True, **OK)
    assert not other.heal and "cooling down" in other.why
    assert decide(s, now=1000.0 + COOLDOWN_S + 1, evicted=True, **OK).heal


def test_heal_reloads_notifies_and_records():
    s, calls, notes = HealState(), [], []
    line = heal(s, "sol-fast", calls.append, now=5.0, notify=lambda t, m: notes.append((t, m)))
    assert calls == ["sol-fast"] and s.heals == 1 and s.last_heal == 5.0 and s.last_model == "sol-fast"
    assert "reloaded sol-fast" in line and notes and "system RAM" in notes[0][1]


def test_a_failing_reload_is_reported_not_raised():
    s = HealState()
    def boom(_):
        raise RuntimeError("router down")
    line = heal(s, "sol-fast", boom, now=5.0)
    assert "heal failed" in line and "router down" in line
    assert s.last_heal == 5.0   # still counts: don't retry in a tight loop


def test_no_model_loaded_is_not_an_error():
    d = decide(HealState(), now=1.0, evicted=True, model=None, busy=False, fits=True)
    assert not d.heal and "no model" in d.why
