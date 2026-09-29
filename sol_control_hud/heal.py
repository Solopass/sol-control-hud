"""Put a spilled model back on the card by itself (step 4 of plans/SOL_FAST_VRAM_SPILL_PLAN.md).

When the card is briefly short at the moment a model loads - a game just closed, the driver is still freeing - the KV
cache and compute buffers land in system RAM and *stay* there: every token then crosses PCIe and decoding drops from
~135 tok/s to 25-35. Nothing moves them back; only a reload does.

So: when the VRAM guard has said "evicted" for a while, the model isn't answering anything, and it would fit now,
unload it and load it again. The decision is a pure function (`decide`) so it can be tested without a GPU; the caller
supplies what it already samples every 2 s.

Deliberately cautious, because a reload interrupts whatever the user is doing:
- never while the model is generating (a reload would kill the answer),
- never twice in a row: one attempt per COOLDOWN_S, and if a heal didn't help, `why` says so instead of looping,
- only when the guard has been sure for MIN_EVICTED_S (its own verdict already needs two consecutive samples),
- only when there is room for the model now (else the reload spills again).
"""
from __future__ import annotations

from dataclasses import dataclass, field

MIN_EVICTED_S = 120.0    # how long "evicted" must hold before we act (the guard already needs 2 samples to say it)
COOLDOWN_S = 900.0       # at most one heal per 15 min
SETTLE_S = 60.0          # after a game/away hand-back, let the driver finish freeing before judging


@dataclass
class HealState:
    """Carried between ticks by the caller."""
    evicted_since: float | None = None   # when the guard first said evicted (None = not evicted)
    last_heal: float = 0.0               # when we last reloaded
    last_model: str = ""                 # what we reloaded, to notice a heal that didn't help
    heals: int = 0
    log: list[str] = field(default_factory=list)


@dataclass
class Decision:
    heal: bool
    why: str


def decide(state: HealState, *, now: float, evicted: bool, model: str | None, busy: bool, fits: bool,
           settled_s: float = SETTLE_S + 1) -> Decision:
    """Should the caller reload `model` now? Updates `state`'s eviction clock as a side effect.

    evicted: the VRAM guard's verdict (already hysteretic)   busy: the model is generating right now
    fits:    it would fit on the card now (free VRAM >= its size)
    settled_s: seconds since the last big VRAM change (game closed, Away handed back); < SETTLE_S = wait
    """
    if not evicted or not model:
        state.evicted_since = None
        return Decision(False, "not evicted" if model else "no model loaded")
    if state.evicted_since is None:
        state.evicted_since = now
    held = now - state.evicted_since
    if held < MIN_EVICTED_S:
        return Decision(False, f"evicted for {held:.0f}s; waiting until {MIN_EVICTED_S:.0f}s")
    if busy:
        return Decision(False, "model is generating; a reload would kill the answer")
    if settled_s < SETTLE_S:
        return Decision(False, f"VRAM changed {settled_s:.0f}s ago; letting the driver settle")
    if not fits:
        return Decision(False, "it would not fit on the card now; a reload would spill again")
    since_heal = now - state.last_heal
    if state.last_heal and since_heal < COOLDOWN_S:
        if state.last_model == model:
            return Decision(False, f"healed {since_heal:.0f}s ago and it spilled again; not looping "
                                   f"(see plans/SOL_FAST_VRAM_SPILL_PLAN.md)")
        return Decision(False, f"healed {since_heal:.0f}s ago; cooling down")
    return Decision(True, f"{model} has been in system RAM for {held:.0f}s and fits now: reloading")


def heal(state: HealState, model: str, reload_model, *, now: float, notify=None) -> str:
    """Do the reload the decision asked for. `reload_model(model)` unloads and loads it again; both are the caller's.

    Returns a one-line result for the log. Never raises: a failed heal must not take the hub down with it.
    """
    state.last_heal, state.last_model, state.heals = now, model, state.heals + 1
    try:
        reload_model(model)
    except Exception as e:  # noqa: BLE001 - a heal is best-effort; the guard will simply say evicted again
        line = f"heal failed for {model}: {type(e).__name__}: {e}"
        state.log.append(line)
        return line
    line = f"reloaded {model}: it had spilled into system RAM"
    state.log.append(line)
    if notify:
        notify("Local AI healed", f"{model} was running from system RAM and has been reloaded onto the card.")
    return line
