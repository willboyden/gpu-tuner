"""The safety envelope — pure functions, no NVML, no I/O, so every limit here is unit-tested.

The daemon imports this from its ROOT-OWNED install dir (/usr/local/lib/gpu-tuner), so nothing
running as the desktop user — the web UI included — can widen these limits.

Three layers, innermost first:
  1. The card's own range as NVML reports it live (never hardcoded as the authority).
  2. The known-good profile for that exact product, INTERSECTED with (1). A card we have no
     profile for may be lowered but never raised above its factory default.
  3. A combined GPU budget across all cards in ONE machine (they share its PSU and wall circuit).
     The per-card maximum is not the safe maximum for a machine: on the workstation this was
     built on, 325 + 600 = 925 W of caps is ~1,730 W at the wall, over a 15 A circuit's
     continuous rating. Each machine has its own budget; there is no fleet-wide one.

A card whose power limit NVML won't report (GB10 and other SoC parts, where firmware owns power)
has no settable range at all, not a 0-0 W one.
"""
from __future__ import annotations

import math


class SafetyError(ValueError):
    """A request outside the envelope. The message is shown to the user verbatim."""


class BudgetExceeded(SafetyError):
    """Raised instead of a plain SafetyError when a request is otherwise valid but pushes the
    combined GPU power over budget. Carries enough detail for a caller to offer an explicit
    override rather than a dead end (see check_power's confirm_override)."""

    def __init__(self, message, total_w, budget_w, others_w):
        super().__init__(message)
        self.total_w = total_w
        self.budget_w = budget_w
        self.others_w = others_w


# ── thermals ────────────────────────────────────────────────────────────────────────────────
# NVML thresholds probed on this lab's two cards (driver 595.84): T.Limit 92 C (Max-Q) / 93 C
# (Workstation), slowdown 95 C, shutdown 98 C. Every fan path is at 100% by 85 C, 10 C under
# slowdown, whatever the user drew. FAN_FULL_BY_C is the fallback for a card whose slowdown
# threshold NVML won't report; every other card gets its own, via safety_floor_for() below.
FAN_FULL_BY_C = 85
FAN_FLOOR_MARGIN_C = 10   # "100% by" sits this many degrees under a card's OWN slowdown threshold

# The shape of the floor under every curve and every fixed speed: effective =
# max(requested, floor(temp)). Measured on this lab's cards (not lazier than the stock driver
# curve at the points found there: Max-Q 61 C @ 43% and 80 C @ 59%, Workstation 78 C @ 46%), then
# climbing hard to full speed at its last point. safety_floor_for() rescales this shape's
# temperatures (never its percentages) so the last point lands at THAT card's own full-by
# temperature instead of always 85 C — on this lab's own cards (slowdown 95 C on both) that
# rescaling is a no-op: 95 - 10 == 85, identical to the curve below.
_SAFETY_FLOOR_SHAPE = ((50, 30), (60, 43), (70, 52), (80, 65), (FAN_FULL_BY_C, 100))


def safety_floor_for(slowdown_c=None):
    """(curve, full_by_c) for one card: the lab-measured floor shape, rescaled to that card's
    own slowdown threshold. Falls back to the fixed lab curve if the threshold is unknown."""
    if not slowdown_c:
        return _SAFETY_FLOOR_SHAPE, FAN_FULL_BY_C
    full_by = slowdown_c - FAN_FLOOR_MARGIN_C
    ref_full = _SAFETY_FLOOR_SHAPE[-1][0]
    scale = full_by / ref_full
    return tuple((t * scale, pct) for t, pct in _SAFETY_FLOOR_SHAPE), full_by

# The curve ops/gpu-fan-curve.py has run since 2026-09-07 (-11 C under load on both cards).
LAB_CURVE = ((30, 30), (45, 45), (55, 60), (65, 75), (72, 88), (80, 100))

def default_curve(full_by=FAN_FULL_BY_C):
    """The lab curve, or — for a card whose own "100% by" temperature is below the curve's last
    point (a GB10-class slowdown of 86 C gives 76 C) — the same shape with its temperatures scaled
    down to end exactly there, so a card's default always passes its own validate_curve()."""
    last = LAB_CURVE[-1][0]
    if full_by >= last:
        return LAB_CURVE
    out, prev = [], CURVE_TEMP_MIN_C - 1
    for t, pct in LAB_CURVE:
        t2 = max(prev + 1, CURVE_TEMP_MIN_C, int(t * full_by / last))
        out.append((t2, pct))
        prev = t2
    out[-1] = (full_by, 100)
    return tuple(out) if out[-2][0] < full_by else ((CURVE_TEMP_MIN_C, out[0][1]), (full_by, 100))


CURVE_PRESETS = {
    "lab": {"label": "Lab (cooling-first)", "curve": LAB_CURVE,
            "note": "The curve this lab has run since 2026-09-07. 100% at 80 C."},
    "balanced": {"label": "Balanced", "curve": ((40, 30), (55, 45), (65, 60), (75, 80), (83, 100)),
                 "note": "Quieter at idle and mid-load; still full speed by 83 C."},
    "quiet": {"label": "Quiet", "curve": ((50, 30), (60, 45), (70, 55), (80, 70), (85, 100)),
              "note": "Rides just above the safety floor. Expect temperatures near the stock "
                      "curve's (high 70s to mid 80s under load), which costs 4-8% of clock."},
}

CURVE_TEMP_MIN_C = 20
CURVE_MIN_POINTS = 2
CURVE_MAX_POINTS = 8
FAN_ABS_MIN_PCT = 30      # NVML's reported minimum on both cards; the daemon re-reads it live
HYSTERESIS_C = 2          # ramp up at once; ramp down only after the card has cooled this much

# ── clocks ──────────────────────────────────────────────────────────────────────────────────
# A clock CAP can only slow the card down, so it is safe by construction. The floor is a
# fat-finger guard (a 180 MHz cap is legal and useless), not a safety limit.
CLOCK_CAP_MIN_MHZ = 1000

# ── power ───────────────────────────────────────────────────────────────────────────────────
# Last-resort fallback only, for a config.json that is missing entirely: install.sh seeds the real
# budget from the caps already in force on THIS machine's cards. (750 W was the 2x RTX PRO 6000
# workstation's own 300 + 450 W ceiling.) The wall-power estimate that used to live here is a
# per-machine display setting now — see hosts.py, `wall` in hosts.json.
DEFAULT_GPU_BUDGET_W = 750

PROFILES = (
    {
        "key": "rtx-pro-6000-maxq",
        "match": "RTX PRO 6000 Blackwell Max-Q Workstation Edition",
        "label": "Max-Q",
        "power_min_w": 250, "power_max_w": 325, "baseline_power_w": 300,
        "power_presets": (
            {"w": 250, "label": "250 W floor", "note": "Measured: 1,822 MHz at 71 C under load."},
            {"w": 300, "label": "300 W default", "note": "Factory default and lab baseline. "
                                                          "Measured: 2,145 MHz at 75 C (+17% clock over 250 W)."},
            {"w": 325, "label": "325 W ceiling", "note": "Not yet measured here; the findings note "
                                                          "predicts low-to-mid single-digit % more clock."},
        ),
        "note": "The 600 W GB202 die held to 300 W: under sustained load it sits at its cap by "
                "design, and the cap is its only real performance lever.",
    },
    {
        "key": "rtx-pro-6000-ws",
        "match": "RTX PRO 6000 Blackwell Workstation Edition",
        "label": "Workstation",
        "power_min_w": 150, "power_max_w": 600, "baseline_power_w": 450,
        "power_presets": (
            {"w": 300, "label": "300 W efficient", "note": "Not measured here. Deep in the efficient "
                                                            "part of the voltage/frequency curve."},
            {"w": 450, "label": "450 W lab cap", "note": "Lab baseline. Measured: 1,980 MHz at 73 C; "
                                                          "renders flat-top here by choice."},
            {"w": 525, "label": "525 W", "note": "Measured: 2,130 MHz at 78 C (+7.6% clock for +16.7% "
                                                  "power). Needs budget headroom."},
            {"w": 600, "label": "600 W factory", "note": "Factory default. Needs budget headroom the "
                                                          "documented 750 W ceiling does not have."},
        ),
        "note": "Already well up its voltage/frequency curve at 450 W, so extra watts buy less "
                "clock than they do on the Max-Q.",
    },
)


def profile_for(name: str):
    for p in PROFILES:
        if name.endswith(p["match"]):
            return p
    return None


def _as_int(value, what: str) -> int:
    # bool is an int subclass; True must not pass as 1 W.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SafetyError(f"{what} must be a whole number")
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            raise SafetyError(f"{what} must be a whole number")
        value = int(value)
    return value


def power_range(profile, nvml_min_w, nvml_max_w, nvml_default_w):
    """Settable (lo, hi) watts: the live NVML range, narrowed by the profile. None if empty, or
    if NVML reports no range at all (None/0) — never a 0-0 W range a daemon would try to write."""
    if not nvml_max_w or nvml_max_w <= 0 or nvml_min_w is None:
        return None
    lo, hi = nvml_min_w, nvml_max_w
    if profile is None:
        if not nvml_default_w or nvml_default_w <= 0:
            return None                   # unknown card AND unknown default: nothing to anchor "lower only" to
        hi = min(hi, nvml_default_w)      # unknown card: lowering only
    else:
        lo, hi = max(lo, profile["power_min_w"]), min(hi, profile["power_max_w"])
    return (lo, hi) if lo <= hi else None


def check_power(uuid: str, watts, ranges: dict, current: dict, budget_w: int,
                 confirm_override: bool = False) -> int:
    """Validate one card's new limit against its range and the combined budget.

    ranges/current are keyed by UUID and cover EVERY card, so the budget sees the whole machine.
    Lowering a card is always allowed, even while the total is over budget — otherwise an
    over-budget state could never be walked back down. A card's own NVML range is never
    overridable (that is the real hardware ceiling); the combined budget is a soft, configured
    limit and raises BudgetExceeded instead of SafetyError so a caller can offer
    confirm_override=True to proceed anyway. budget_w None means this machine has no budget.
    """
    watts = _as_int(watts, "power limit")
    rng = ranges.get(uuid)
    if rng is None:
        raise SafetyError("this card has no settable power range")
    lo, hi = rng
    if not lo <= watts <= hi:
        raise SafetyError(f"power limit must be {lo}-{hi} W for this card")
    total = sum(w for u, w in current.items() if u != uuid) + watts
    if budget_w is not None and total > budget_w and watts > current.get(uuid, 0) and not confirm_override:
        others_w = total - watts
        other_uuids = [u for u in current if u != uuid]
        if not other_uuids:
            lead = f"{watts} W is"
        elif len(other_uuids) == 1:
            lead = f"{watts} W here plus {others_w} W on the other card is"
        else:
            lead = f"{watts} W here plus {others_w} W across the other {len(other_uuids)} cards is"
        raise BudgetExceeded(
            f"{lead} {total} W, over the {budget_w} W combined GPU budget. Lower another card "
            f"first, raise the budget, or confirm to exceed it anyway.",
            total_w=total, budget_w=budget_w, others_w=others_w)
    return watts


# ── fan curves ──────────────────────────────────────────────────────────────────────────────
def interp(curve, temp: float) -> float:
    if temp <= curve[0][0]:
        return float(curve[0][1])
    if temp >= curve[-1][0]:
        return float(curve[-1][1])
    for (t0, f0), (t1, f1) in zip(curve, curve[1:]):
        if t0 <= temp <= t1:
            return f0 + (f1 - f0) * ((temp - t0) / (t1 - t0))
    return float(curve[-1][1])


def floor_pct(temp: float, slowdown_c=None, fan_min: int = FAN_ABS_MIN_PCT) -> int:
    curve, _ = safety_floor_for(slowdown_c)
    return max(fan_min, math.ceil(interp(curve, temp)))


def validate_curve(points, full_by: int = FAN_FULL_BY_C, fan_min: int = FAN_ABS_MIN_PCT) -> list:
    """Normalise a user curve to [[temp, pct], ...] or raise SafetyError.

    Shape rules only. A curve that dips under the safety floor is NOT rejected: the floor is
    applied when the curve is evaluated, so no accepted curve can ever under-cool the card.
    `full_by` is this card's own "must be 100% by" temperature (see safety_floor_for); it
    defaults to the lab's fixed 85 C for callers that have not looked up a card-specific one.
    """
    if not isinstance(points, (list, tuple)):
        raise SafetyError("curve must be a list of [temperature, fan %] points")
    if not CURVE_MIN_POINTS <= len(points) <= CURVE_MAX_POINTS:
        raise SafetyError(f"curve needs {CURVE_MIN_POINTS}-{CURVE_MAX_POINTS} points")
    out = []
    for p in points:
        if not isinstance(p, (list, tuple)) or len(p) != 2:
            raise SafetyError("each curve point is [temperature, fan %]")
        t, f = _as_int(p[0], "curve temperature"), _as_int(p[1], "curve fan %")
        if not CURVE_TEMP_MIN_C <= t <= full_by:
            raise SafetyError(f"curve temperatures must be {CURVE_TEMP_MIN_C}-{full_by} C")
        if not fan_min <= f <= 100:
            raise SafetyError(f"curve fan speeds must be {fan_min}-100% (the card's fan minimum is {fan_min}%)")
        if out and t <= out[-1][0]:
            raise SafetyError("curve temperatures must strictly increase")
        if out and f < out[-1][1]:
            raise SafetyError("fan speed must never fall as temperature rises")
        out.append([t, f])
    if out[-1][1] != 100:
        raise SafetyError(f"the last curve point must be 100% (at or below {full_by} C)")
    return out


def validate_manual(pct, fan_min: int = FAN_ABS_MIN_PCT) -> int:
    pct = _as_int(pct, "fixed fan speed")
    if not fan_min <= pct <= 100:
        raise SafetyError(f"fixed fan speed must be {fan_min}-100%")
    return pct


def fan_target(mode: str, temp: float, curve, manual_pct, slowdown_c=None,
               fan_min: int = FAN_ABS_MIN_PCT):
    """(percent, floor_active) for the 'curve' and 'manual' modes. 'auto' never reaches here:
    in auto the driver's own curve has the fans and the daemon writes nothing."""
    floor = floor_pct(temp, slowdown_c, fan_min)
    if mode == "curve":
        want = int(round(interp(curve, temp)))
    elif mode == "manual":
        want = int(manual_pct)
    else:
        raise SafetyError(f"unknown fan mode {mode!r}")
    want = max(fan_min, min(100, want))
    return (floor, True) if floor > want else (want, False)


def hysteresis(target: int, temp: float, prev):
    """prev is (pct, temp_when_set) or None. Never returns less than target, so it can hold the
    fans HIGHER than the curve for a moment but can never push them under the floor."""
    if prev is None:
        return target
    prev_pct, prev_temp = prev
    if target >= prev_pct or temp <= prev_temp - HYSTERESIS_C:
        return target
    return prev_pct


# ── clock cap ───────────────────────────────────────────────────────────────────────────────
def validate_clock_cap(mhz, supported):
    """None clears the cap. Otherwise snap DOWN to a clock the card actually supports."""
    if mhz is None:
        return None
    mhz = _as_int(mhz, "clock cap")
    if not supported:
        raise SafetyError("this card reports no supported clock list")
    top = max(supported)
    # A card whose ENTIRE supported range sits under the fat-finger floor (e.g. a low-clock
    # older/entry card) isn't locked out of the whole feature: below `top`, the floor stops
    # applying and the card's own minimum takes over. (Using min(CLOCK_CAP_MIN_MHZ, top) instead
    # would collapse the range to a single point — [top, top] — making every cap below the card's
    # own max illegal, which defeats the point of a "cap" entirely.)
    lo = CLOCK_CAP_MIN_MHZ if top >= CLOCK_CAP_MIN_MHZ else min(supported)
    if not lo <= mhz <= top:
        raise SafetyError(f"clock cap must be {lo}-{top} MHz")
    below = [c for c in supported if c <= mhz]
    return max(below) if below else min(supported)
