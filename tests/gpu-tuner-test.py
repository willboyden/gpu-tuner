#!/usr/bin/env python3
"""Offline tests for gpu-tuner: the safety envelope and the daemon's decisions.

Pure stdlib, no GPU: the daemon gets a fake NVML module shaped like the real pynvml. Every
test here is one that CAN fail — each asserts a specific accept/reject or a specific value.
"""
import json
import os
import socket
import sys
import tempfile
import unittest

GPU_TUNER = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, GPU_TUNER)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gpu_tuner import safety, seed  # noqa: E402
from gpu_tuner import daemon as daemon_mod  # noqa: E402
from gpu_tuner.daemon import Daemon, authorised, load_config  # noqa: E402
from gpu_tuner.nvml import Nvml  # noqa: E402

daemon_mod.log = lambda msg: None      # the daemon narrates every decision; keep test output readable

from fakes import (FakeGB10, FakeLib, FakeOldLib, GB10, MAXQ, WS,  # noqa: E402
                   meminfo_file)


def make_daemon(tmp, budget=750, state=None, lib=None):
    lib = lib or FakeLib()
    nv = Nvml(lib=lib)
    path = os.path.join(tmp, "state.json")
    if state is not None:
        with open(path, "w") as f:
            json.dump(state, f)
    d = Daemon(nv, {"gpu_budget_w": budget, "allowed_uid": 1000, "interval_s": 2.0}, path)
    d.load_state()
    d.apply_startup()
    return d, lib


class SafetyPower(unittest.TestCase):
    def setUp(self):
        self.ranges = {MAXQ: (250, 325), WS: (150, 600)}
        self.current = {MAXQ: 300, WS: 450}

    def test_profile_narrows_to_card_range(self):
        p = safety.profile_for("NVIDIA RTX PRO 6000 Blackwell Max-Q Workstation Edition")
        self.assertEqual(safety.power_range(p, 100, 999, 300), (250, 325))

    def test_unknown_card_can_only_be_lowered(self):
        self.assertEqual(safety.power_range(None, 100, 500, 350), (100, 350))

    def test_in_range_and_in_budget_ok(self):
        self.assertEqual(safety.check_power(MAXQ, 325, self.ranges, {MAXQ: 300, WS: 425}, 750), 325)

    def test_over_card_range_rejected(self):
        with self.assertRaises(safety.SafetyError):
            safety.check_power(MAXQ, 326, self.ranges, self.current, 750)
        with self.assertRaises(safety.SafetyError):
            safety.check_power(MAXQ, 249, self.ranges, self.current, 750)

    def test_over_budget_rejected_by_one_watt(self):
        with self.assertRaises(safety.SafetyError) as cm:
            safety.check_power(WS, 451, self.ranges, self.current, 750)
        self.assertIn("751 W", str(cm.exception))

    def test_over_budget_raises_the_distinguishable_subclass(self):
        with self.assertRaises(safety.BudgetExceeded) as cm:
            safety.check_power(WS, 451, self.ranges, self.current, 750)
        e = cm.exception
        self.assertEqual((e.total_w, e.budget_w, e.others_w), (751, 750, 300))

    def test_confirm_override_allows_exceeding_the_budget(self):
        self.assertEqual(safety.check_power(WS, 451, self.ranges, self.current, 750,
                                            confirm_override=True), 451)

    def test_confirm_override_never_bypasses_the_cards_own_range(self):
        # the override is for the SOFT combined budget only; the hardware range stays absolute
        with self.assertRaises(safety.SafetyError):
            safety.check_power(WS, 601, self.ranges, self.current, 750, confirm_override=True)

    def test_over_budget_message_scales_with_card_count(self):
        # zero, one, and several "other" cards each get their own wording
        with self.assertRaises(safety.BudgetExceeded) as cm:
            safety.check_power(WS, 900, {WS: (150, 900)}, {}, 750)
        self.assertEqual(str(cm.exception), "900 W is 150 W over this machine's 750 W GPU budget. "
                                            "Raise the budget, or confirm to exceed it anyway.")
        with self.assertRaises(safety.BudgetExceeded) as cm:   # an only card that is already over
            safety.check_power(WS, 900, {WS: (150, 900)}, {WS: 800}, 750)
        self.assertNotIn("other card", str(cm.exception))
        with self.assertRaises(safety.BudgetExceeded) as cm:
            safety.check_power(WS, 451, self.ranges, self.current, 750)
        self.assertIn("on the other card is", str(cm.exception))
        ranges3 = dict(self.ranges, THIRD=(100, 900))
        current3 = dict(self.current, THIRD=100)
        with self.assertRaises(safety.BudgetExceeded) as cm:
            safety.check_power(WS, 451, ranges3, current3, 750)
        self.assertIn("across the other 2 cards is", str(cm.exception))

    def test_lowering_allowed_even_when_over_budget(self):
        # an over-budget state must always be walkable back down
        self.assertEqual(safety.check_power(WS, 550, self.ranges, {MAXQ: 325, WS: 600}, 750), 550)

    def test_bool_and_fraction_rejected(self):
        for bad in (True, 300.5, "300", None, float("nan")):
            with self.assertRaises(safety.SafetyError):
                safety.check_power(MAXQ, bad, self.ranges, self.current, 750)

    def test_no_reported_range_is_none_not_zero(self):
        # GB10-style: NVML reports nothing. A (0, 0) range once made a daemon try to write 0 W.
        for args in ((None, None, None), (0, 0, 0), (None, 0, None)):
            self.assertIsNone(safety.power_range(None, *args), args)
        p = safety.profile_for("NVIDIA RTX PRO 6000 Blackwell Max-Q Workstation Edition")
        self.assertIsNone(safety.power_range(p, None, None, None))

    def test_unknown_card_without_a_default_gets_no_range(self):
        # nothing to anchor "lower only" to, so nothing is settable
        self.assertIsNone(safety.power_range(None, 100, 500, None))

    def test_no_budget_means_no_budget_check(self):
        self.assertEqual(safety.check_power(WS, 600, self.ranges, self.current, None), 600)


class SafetyFans(unittest.TestCase):
    def test_lab_curve_validates(self):
        self.assertEqual(safety.validate_curve(safety.LAB_CURVE), [list(p) for p in safety.LAB_CURVE])

    def test_every_preset_validates(self):
        for k, p in safety.CURVE_PRESETS.items():
            safety.validate_curve(p["curve"])

    def test_falling_curve_rejected(self):
        with self.assertRaises(safety.SafetyError):
            safety.validate_curve([[30, 50], [60, 40], [80, 100]])

    def test_must_end_at_100_by_full_by(self):
        with self.assertRaises(safety.SafetyError):
            safety.validate_curve([[30, 30], [80, 90]])
        with self.assertRaises(safety.SafetyError):
            safety.validate_curve([[30, 30], [safety.FAN_FULL_BY_C + 1, 100]])
        safety.validate_curve([[30, 30], [safety.FAN_FULL_BY_C, 100]])

    def test_below_fan_minimum_rejected(self):
        with self.assertRaises(safety.SafetyError):
            safety.validate_curve([[30, 29], [80, 100]], fan_min=30)
        with self.assertRaises(safety.SafetyError):
            safety.validate_manual(29, fan_min=30)
        self.assertEqual(safety.validate_manual(30, fan_min=30), 30)

    def test_point_count_and_ordering(self):
        with self.assertRaises(safety.SafetyError):
            safety.validate_curve([[80, 100]])
        with self.assertRaises(safety.SafetyError):
            safety.validate_curve([[40, 40], [40, 50], [80, 100]])
        with self.assertRaises(safety.SafetyError):
            safety.validate_curve([[20 + i, 30 + i] for i in range(9)])

    def test_floor_is_never_lazier_than_measured_stock(self):
        # stock driver curve points measured on these cards (findings note)
        self.assertGreaterEqual(safety.floor_pct(61), 43)
        self.assertGreaterEqual(safety.floor_pct(78), 46)
        self.assertGreaterEqual(safety.floor_pct(80), 59)
        self.assertEqual(safety.floor_pct(safety.FAN_FULL_BY_C), 100)
        self.assertEqual(safety.floor_pct(99), 100)

    def test_safety_floor_for_reproduces_this_labs_own_curve(self):
        # this lab's cards both have a 95 C slowdown threshold; 95 - FAN_FLOOR_MARGIN_C == 85,
        # the historical FAN_FULL_BY_C, so the rescaling must be a no-op here.
        curve, full_by = safety.safety_floor_for(95)
        self.assertEqual(full_by, safety.FAN_FULL_BY_C)
        self.assertEqual(tuple(curve), safety._SAFETY_FLOOR_SHAPE)

    def test_safety_floor_for_falls_back_without_a_threshold(self):
        curve, full_by = safety.safety_floor_for(None)
        self.assertEqual(full_by, safety.FAN_FULL_BY_C)
        self.assertEqual(tuple(curve), safety._SAFETY_FLOOR_SHAPE)

    def test_safety_floor_for_rescales_for_a_different_card(self):
        # a card with a hotter slowdown threshold gets a proportionally hotter floor, same shape
        curve, full_by = safety.safety_floor_for(105)
        self.assertEqual(full_by, 95)
        self.assertEqual(curve[-1][0], 95)
        self.assertEqual([pct for _, pct in curve], [pct for _, pct in safety._SAFETY_FLOOR_SHAPE])
        self.assertGreater(curve[0][0], safety._SAFETY_FLOOR_SHAPE[0][0])   # scaled up, not just shifted
        # floor_pct/fan_target take the threshold: at 90 C, a card rated to 105 C slowdown isn't
        # forced to 100% yet, while the fallback (fixed 85 C full-by) curve already is.
        self.assertEqual(safety.floor_pct(95, slowdown_c=105), 100)
        self.assertLess(safety.floor_pct(90, slowdown_c=105), safety.floor_pct(90, slowdown_c=None))

    def test_quiet_preset_and_fixed_speed_are_lifted_by_the_floor(self):
        quiet = safety.CURVE_PRESETS["quiet"]["curve"]
        pct, floored = safety.fan_target("curve", 75, quiet, None)
        self.assertGreaterEqual(pct, safety.floor_pct(75))
        pct, floored = safety.fan_target("manual", 84, None, 30)
        self.assertTrue(floored)
        self.assertGreaterEqual(pct, 90)
        pct, floored = safety.fan_target("manual", 40, None, 30)
        self.assertEqual((pct, floored), (30, False))

    def test_hysteresis_holds_on_the_way_down_only(self):
        self.assertEqual(safety.hysteresis(60, 70, None), 60)
        self.assertEqual(safety.hysteresis(70, 72, (60, 70)), 70)           # rising: immediate
        self.assertEqual(safety.hysteresis(58, 69, (60, 70)), 60)           # cooled 1 C: hold
        self.assertEqual(safety.hysteresis(58, 68, (60, 70)), 58)           # cooled 2 C: drop
        self.assertGreaterEqual(safety.hysteresis(55, 69, (60, 70)), 55)   # never below target


class SafetyClocks(unittest.TestCase):
    def test_snaps_down_to_supported(self):
        sup = list(range(180, 3091, 15))
        self.assertEqual(safety.validate_clock_cap(2001, sup), 1995)
        self.assertEqual(safety.validate_clock_cap(3090, sup), 3090)
        self.assertIsNone(safety.validate_clock_cap(None, sup))

    def test_bounds(self):
        sup = list(range(180, 3091, 15))
        with self.assertRaises(safety.SafetyError):
            safety.validate_clock_cap(999, sup)
        with self.assertRaises(safety.SafetyError):
            safety.validate_clock_cap(3091, sup)

    def test_a_card_whose_top_clock_is_under_the_1000_mhz_floor_is_not_locked_out(self):
        # a hypothetical low-clock card (e.g. an older or entry-level GPU): the fixed 1000 MHz
        # floor used to make EVERY cap request fail with an inverted "1000-800 MHz" range.
        sup = [180, 400, 620, 800]
        self.assertEqual(safety.validate_clock_cap(700, sup), 620)
        self.assertEqual(safety.validate_clock_cap(800, sup), 800)
        with self.assertRaises(safety.SafetyError):
            safety.validate_clock_cap(801, sup)


class DaemonBehaviour(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_first_run_adopts_live_limits(self):
        d, lib = make_daemon(self.tmp)
        self.assertEqual(d.settings[MAXQ]["power_w"], 300)
        self.assertEqual(d.settings[WS]["power_w"], 450)
        self.assertEqual(d.settings[WS]["fan"]["mode"], "curve")

    def test_saved_state_over_budget_falls_back(self):
        state = {"version": 1, "gpus": {MAXQ: {"power_w": 325}, WS: {"power_w": 600}}}
        d, lib = make_daemon(self.tmp, state=state)
        self.assertLessEqual(d.settings[MAXQ]["power_w"] + d.settings[WS]["power_w"], 750)
        self.assertTrue(any("over the 750 W budget" in w for w in d.warnings))

    def test_saved_bad_curve_falls_back_to_lab_curve(self):
        state = {"version": 1, "gpus": {WS: {"fan": {"mode": "curve", "curve": [[30, 10], [80, 100]]}}}}
        d, lib = make_daemon(self.tmp, state=state)
        self.assertEqual(d.settings[WS]["fan"]["curve"], [list(p) for p in safety.LAB_CURVE])

    def test_set_power_writes_and_reads_back(self):
        d, lib = make_daemon(self.tmp)
        r = d.handle({"op": "set_power", "uuid": WS, "watts": 425})
        self.assertTrue(r["ok"], r)
        self.assertEqual(lib.cards[1]["limit"], 425000)
        with open(d.state_path) as f:
            self.assertEqual(json.load(f)["gpus"][WS]["power_w"], 425)

    def test_budget_enforced_across_cards(self):
        d, lib = make_daemon(self.tmp)
        self.assertFalse(d.handle({"op": "set_power", "uuid": MAXQ, "watts": 325})["ok"])
        self.assertTrue(d.handle({"op": "set_power", "uuid": WS, "watts": 425})["ok"])
        self.assertTrue(d.handle({"op": "set_power", "uuid": MAXQ, "watts": 325})["ok"])
        self.assertFalse(d.handle({"op": "set_power", "uuid": WS, "watts": 430})["ok"])

    def test_over_budget_response_carries_the_confirm_fields(self):
        d, lib = make_daemon(self.tmp)
        r = d.handle({"op": "set_power", "uuid": MAXQ, "watts": 325})
        self.assertFalse(r["ok"])
        self.assertTrue(r["over_budget"])
        self.assertEqual((r["total_w"], r["budget_w"]), (775, 750))
        self.assertEqual(lib.cards[0]["limit"], 300000)          # rejected: nothing was written

    def test_confirm_override_applies_despite_the_budget(self):
        d, lib = make_daemon(self.tmp)
        r = d.handle({"op": "set_power", "uuid": MAXQ, "watts": 325, "confirm_override": True})
        self.assertTrue(r["ok"], r)
        self.assertNotIn("over_budget", r)
        self.assertEqual(lib.cards[0]["limit"], 325000)
        # status() must keep surfacing the over-budget state even without a fresh write
        self.assertTrue(any("over the 750 W budget" in w for w in r["warnings"]))

    def test_confirm_override_requires_an_actual_true_not_any_truthy_value(self):
        # a loose bool() coercion would let a client send "confirm_override": "false" (a
        # non-empty string) and have it override anyway
        d, lib = make_daemon(self.tmp)
        for sneaky in ("false", "0", 1, [], {}):
            r = d.handle({"op": "set_power", "uuid": MAXQ, "watts": 325, "confirm_override": sneaky})
            self.assertFalse(r["ok"], r)
            self.assertTrue(r["over_budget"])

    def test_set_budget_updates_and_persists(self):
        d, lib = make_daemon(self.tmp)
        d.config_path = os.path.join(self.tmp, "config.json")
        r = d.handle({"op": "set_budget", "watts": 800})
        self.assertTrue(r["ok"], r)
        self.assertEqual(d.budget, 800)
        self.assertEqual(r["budget_w"], 800)
        with open(d.config_path) as f:
            self.assertEqual(json.load(f)["gpu_budget_w"], 800)
        # a raise that fits the NEW 800 W budget succeeds; one watt more is still rejected
        self.assertTrue(d.handle({"op": "set_power", "uuid": WS, "watts": 500})["ok"])
        self.assertFalse(d.handle({"op": "set_power", "uuid": WS, "watts": 501})["ok"])

    def test_set_budget_rejects_nonsense(self):
        d, lib = make_daemon(self.tmp)
        for bad in (0, -100, "800", True, 1.5):
            self.assertFalse(d.handle({"op": "set_budget", "watts": bad})["ok"])
        self.assertEqual(d.budget, 750)

    def test_set_budget_rejects_above_the_hardware_maximum(self):
        # MAXQ tops out at 325, WS at 600 -> 925 W is the most that could ever actually bind
        d, lib = make_daemon(self.tmp)
        self.assertTrue(d.handle({"op": "set_budget", "watts": 925})["ok"])
        r = d.handle({"op": "set_budget", "watts": 926})
        self.assertFalse(r["ok"])
        self.assertIn("925 W", r["error"])
        self.assertEqual(d.budget, 925)   # the earlier valid raise stuck; the bad one didn't move it

    def test_fan_loop_applies_curve_with_floor(self):
        d, lib = make_daemon(self.tmp)
        lib.cards[0]["temp"] = 65
        d.tick(100.0)
        self.assertIn((0, 0, 75), lib.fan_writes)           # lab curve: 65 C -> 75%
        lib.cards[1]["temp"] = 90                             # above FAN_FULL_BY_C
        d.tick(102.0)
        self.assertIn((1, 0, 100), lib.fan_writes)
        self.assertIn((1, 1, 100), lib.fan_writes)           # both fans on the twin-fan card

    def test_fan_write_refused_hands_back_to_driver(self):
        d, lib = make_daemon(self.tmp)
        lib.refuse_fan = True
        d.tick(100.0)
        self.assertIn((0, 0), lib.fan_auto)
        self.assertIsNotNone(d.rt[MAXQ]["fault"])
        self.assertIn("fan write refused", d.status()["gpus"][0]["fan"]["fault"])

    def test_unreadable_temperature_hands_back_to_driver(self):
        d, lib = make_daemon(self.tmp)
        lib.refuse_temp = True
        d.tick(100.0)
        self.assertTrue(lib.fan_auto)
        self.assertIn("temperature unreadable", d.rt[WS]["fault"])

    def test_auto_mode_writes_default_once(self):
        d, lib = make_daemon(self.tmp)
        self.assertTrue(d.handle({"op": "set_fan", "uuid": WS, "mode": "auto"})["ok"])
        n = len(lib.fan_auto)
        d.tick(200.0)
        d.tick(202.0)
        self.assertEqual(len(lib.fan_auto), n)                # handle() already applied it
        self.assertFalse(any(w[0] == 1 for w in lib.fan_writes[-2:]))

    def test_drifted_power_limit_is_reapplied(self):
        d, lib = make_daemon(self.tmp)
        lib.cards[1]["limit"] = 600000                        # something else changed it
        d.tick(1000.0)
        self.assertEqual(lib.cards[1]["limit"], 450000)

    def test_baseline_resets_everything(self):
        d, lib = make_daemon(self.tmp)
        d.handle({"op": "set_power", "uuid": WS, "watts": 400})
        d.handle({"op": "set_fan", "uuid": WS, "mode": "manual", "manual_pct": 70})
        d.handle({"op": "set_clock_cap", "uuid": WS, "mhz": 2000})
        r = d.handle({"op": "baseline", "uuid": WS})
        self.assertTrue(r["ok"], r)
        s = d.settings[WS]
        self.assertEqual((s["power_w"], s["fan"]["mode"], s["clock_cap_mhz"]), (450, "curve", None))
        self.assertEqual(lib.clock_calls[-1], (1, None))

    def test_clock_cap_snaps_and_clears(self):
        d, lib = make_daemon(self.tmp)
        r = d.handle({"op": "set_clock_cap", "uuid": MAXQ, "mhz": 2001})
        self.assertEqual(r["gpus"][0]["settings"]["clock_cap_mhz"], 1995)
        self.assertEqual(lib.clock_calls[-1], (0, 180, 1995))
        self.assertFalse(d.handle({"op": "set_clock_cap", "uuid": MAXQ, "mhz": 500})["ok"])

    def test_unknown_ops_and_gpus_rejected(self):
        d, lib = make_daemon(self.tmp)
        self.assertFalse(d.handle({"op": "reboot"})["ok"])
        self.assertFalse(d.handle({"op": "set_power", "uuid": "GPU-nope", "watts": 300})["ok"])
        self.assertFalse(d.handle(["not", "a", "dict"])["ok"])

    def test_restore_fans_covers_every_fan(self):
        d, lib = make_daemon(self.tmp)
        d.restore_fans()
        self.assertEqual(sorted(lib.fan_auto[-3:]), [(0, 0), (1, 0), (1, 1)])


class Config(unittest.TestCase):
    def test_peer_authorisation(self):
        self.assertTrue(authorised(0, 1000, 0))
        self.assertTrue(authorised(1000, 1000, 0))
        self.assertFalse(authorised(1001, 1000, 0))
        self.assertFalse(authorised(1000, None, 0))          # no allowed_uid configured: root only
        self.assertTrue(authorised(1000, None, 1000))         # dry-run as that user

    def test_config_validation(self):
        tmp = tempfile.mkdtemp()
        p = os.path.join(tmp, "c.json")
        with open(p, "w") as f:
            json.dump({"allowed_uid": 1000, "gpu_budget_w": 800, "interval_s": 3}, f)
        self.assertEqual(load_config(p)["gpu_budget_w"], 800)
        self.assertEqual(load_config(os.path.join(tmp, "missing.json"))["gpu_budget_w"], safety.DEFAULT_GPU_BUDGET_W)
        with open(p, "w") as f:
            json.dump({"allowed_uid": 1000, "gpu_budget_w": None}, f)
        self.assertIsNone(load_config(p)["gpu_budget_w"])      # explicit null: nothing settable here
        for bad in ({"gpu_budget_w": -1}, {"gpu_budget_w": True}, {"allowed_uid": "me"}, {"interval_s": 0.1}, []):
            with open(p, "w") as f:
                json.dump(bad, f)
            with self.assertRaises(SystemExit):
                load_config(p)


class Gb10(unittest.TestCase):
    """A machine whose only GPU reports no power limit, no fans, no memory info, no clock list."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_startup_writes_nothing_and_keeps_no_banner_warning(self):
        lib = FakeGB10()
        d, _ = make_daemon(self.tmp, budget=None, lib=lib)
        self.assertEqual(d.warnings, [])                      # persistence NOT_SUPPORTED is logged, not kept
        self.assertIsNone(d.ranges[GB10])
        self.assertIsNone(d.settings[GB10]["power_w"])
        self.assertEqual(lib.power_writes, [])                # never a 0 W write
        st = d.status()
        self.assertFalse(st["power_settable"])
        self.assertIsNone(st["budget_w"])
        self.assertIsNone(st["gpus"][0]["power_range"])
        self.assertIsNone(st["gpus"][0]["clock_range"])

    def test_power_and_budget_requests_are_refused(self):
        d, lib = make_daemon(self.tmp, budget=None, lib=FakeGB10())
        r = d.handle({"op": "set_power", "uuid": GB10, "watts": 100})
        self.assertFalse(r["ok"])
        self.assertIn("no settable power range", r["error"])
        r = d.handle({"op": "set_budget", "watts": 200})
        self.assertFalse(r["ok"])
        self.assertIn("nothing for a budget to limit", r["error"])
        r = d.handle({"op": "set_fan", "uuid": GB10, "mode": "manual", "manual_pct": 80})
        self.assertFalse(r["ok"])
        self.assertEqual(lib.power_writes, [])

    def test_tick_drives_no_fans(self):
        d, lib = make_daemon(self.tmp, budget=None, lib=FakeGB10())
        d.tick(1000.0)
        self.assertEqual((lib.fan_writes, lib.fan_auto), ([], []))

    def test_baseline_on_a_card_with_nothing_settable_is_a_no_op(self):
        d, lib = make_daemon(self.tmp, budget=None, lib=FakeGB10())
        r = d.handle({"op": "baseline", "uuid": GB10})
        self.assertTrue(r["ok"], r)
        self.assertEqual(lib.power_writes, [])

    def test_unified_memory_reports_system_ram(self):
        nv = Nvml(lib=FakeGB10(), meminfo=meminfo_file(self.tmp, 128 << 20, 96 << 20))
        g = nv.gpus[0]
        self.assertEqual(g.mem_kind, "unified")
        self.assertIsNone(g.power_max_w)
        self.assertEqual(g.static()["power_default_w"], None)
        s = nv.sample(g)
        self.assertEqual((s["vram_total_mib"], s["vram_used_mib"], s["mem_kind"]), (128 << 10, 32 << 10, "unified"))
        self.assertIsNone(s["power_limit_w"])
        self.assertEqual(s["power_w"], 31.0)
        self.assertEqual(s["fans"], [])

    def test_discrete_card_keeps_vram(self):
        nv = Nvml(lib=FakeLib())
        self.assertEqual(nv.gpus[0].mem_kind, "dedicated")
        self.assertEqual(nv.sample(nv.gpus[0])["vram_total_mib"], 96 << 10)


class LowSlowdownCard(FakeLib):
    """A fan-cooled card that slows down at 86 C (as GB10 reports): its "100% by" is 76 C, below the
    lab curve's last point (80 C), so the lab curve itself is not a valid curve for it."""

    def nvmlDeviceGetTemperatureThreshold(self, h, c): return {0: 90, 1: 86, 3: 99}[c]


class DefaultCurves(unittest.TestCase):
    def test_default_curve_always_passes_the_cards_own_rule(self):
        for full_by in range(30, 101):
            curve = [list(p) for p in safety.default_curve(full_by)]
            self.assertEqual(safety.validate_curve(curve, full_by=full_by), curve, full_by)
            self.assertEqual(curve[-1], [min(full_by, 80) if full_by < 80 else 80, 100])
        self.assertEqual(safety.default_curve(85), safety.LAB_CURVE)

    def test_low_slowdown_card_starts_clean_and_resets_to_baseline(self):
        tmp = tempfile.mkdtemp()
        d, lib = make_daemon(tmp, lib=LowSlowdownCard())
        self.assertEqual(d.warnings, [])
        self.assertEqual(d.settings[WS]["fan"]["curve"][-1], [76, 100])
        r = d.handle({"op": "baseline", "uuid": WS})
        self.assertTrue(r["ok"], r)
        self.assertEqual(d.settings[WS]["fan"]["curve"][-1], [76, 100])
        d2 = Daemon(Nvml(lib=LowSlowdownCard()), {"gpu_budget_w": 750, "allowed_uid": 1000, "interval_s": 2.0},
                    d.state_path)
        d2.load_state()                                       # and its saved state reloads without warnings
        self.assertEqual(d2.warnings, [])


class OldPynvml(unittest.TestCase):
    """A distro pynvml without some bindings must degrade to "unsupported", never AttributeError."""

    def test_loads_and_samples(self):
        nv = Nvml(lib=FakeOldLib())
        s = nv.sample(nv.gpus[1])
        self.assertEqual(s["fans"], [None, None])
        self.assertEqual(s["reasons"], [])

    def test_missing_setter_is_an_nvml_error_and_the_fan_loop_survives_it(self):
        tmp = tempfile.mkdtemp()
        d, lib = make_daemon(tmp, lib=FakeOldLib())
        with self.assertRaises(d.nv.Error):
            d.nv.set_fan_pct(d.nv.gpus[0], 60)
        d.tick(1000.0)                                        # must not raise
        self.assertIn("fan write refused", d.rt[MAXQ]["fault"])


class Seed(unittest.TestCase):
    def test_parse_limit(self):
        for text, want in (("450.00", 450), (" 300 ", 300), ("[N/A]", None), ("[Not Supported]", None),
                           ("", None), ("0", None), ("-5", None), ("nan", None), ("inf", None)):
            self.assertEqual(seed.parse_limit(text), want, text)

    def test_mixed_machine(self):
        doc, budget = seed.build([["GPU-a", "325.00"], ["GPU-b", "[N/A]"], ["GPU-c", " 450 "]])
        self.assertEqual(budget, 775)
        self.assertNotIn("power_w", doc["gpus"]["GPU-b"])     # adopt live, or leave power alone
        self.assertEqual(doc["gpus"]["GPU-a"]["power_w"], 325)

    def test_nothing_settable_means_no_budget(self):
        doc, budget = seed.build([["GPU-gb10", "[N/A]"]])
        self.assertIsNone(budget)
        self.assertEqual(list(doc["gpus"]), ["GPU-gb10"])

    def test_fans_start_on_the_driver_curve_unless_asked(self):
        doc, _ = seed.build([["GPU-a", "300"]])
        self.assertEqual(doc["gpus"]["GPU-a"]["fan"]["mode"], "auto")
        doc, _ = seed.build([["GPU-a", "300"]], fan_mode="curve")
        self.assertEqual(doc["gpus"]["GPU-a"]["fan"]["mode"], "curve")
        with self.assertRaises(ValueError):
            seed.build([["GPU-a", "300"]], fan_mode="max")

    def test_blank_rows_skipped_and_cli_round_trip(self):
        tmp = tempfile.mkdtemp()
        csv_in, out, bud = (os.path.join(tmp, n) for n in ("in.csv", "state.json", "budget"))
        with open(csv_in, "w") as f:
            f.write(f"{MAXQ}, 300.00\n\n{WS}, 450.00\n")
        self.assertEqual(seed.main([csv_in, out, bud, "--fan-mode", "curve"]), 0)
        with open(bud) as f:
            self.assertEqual(f.read(), "750")
        with open(out) as f:
            doc = json.load(f)
        self.assertEqual(sorted(doc["gpus"]), sorted([MAXQ, WS]))
        # and the daemon accepts what the seed wrote, without warnings
        d = Daemon(Nvml(lib=FakeLib()), {"gpu_budget_w": 750, "allowed_uid": 1000, "interval_s": 2.0}, out)
        d.load_state()
        self.assertEqual(d.warnings, [])
        self.assertEqual((d.settings[MAXQ]["power_w"], d.settings[WS]["fan"]["mode"]), (300, "curve"))

    def test_only_na_rows_write_a_null_budget(self):
        tmp = tempfile.mkdtemp()
        csv_in, out, bud = (os.path.join(tmp, n) for n in ("in.csv", "state.json", "budget"))
        with open(csv_in, "w") as f:
            f.write(f"{GB10}, [N/A]\n")
        seed.main([csv_in, out, bud])
        with open(bud) as f:
            self.assertEqual(f.read(), "null")
        d = Daemon(Nvml(lib=FakeGB10()), {"gpu_budget_w": None, "allowed_uid": 1000, "interval_s": 2.0}, out)
        d.load_state()
        self.assertEqual((d.warnings, d.settings[GB10]["power_w"]), ([], None))


class HostileRequests(unittest.TestCase):
    """Whatever a client on the socket sends, the daemon answers and keeps running."""

    def ask(self, d, payload):
        a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        b.sendall(payload)
        daemon_mod.serve_one(a, d, os.getuid())       # must not raise
        return b.recv(65536)                          # raw: parsed by the caller

    def test_deep_nesting_is_a_bad_request_not_a_crash(self):
        d, _ = make_daemon(tempfile.mkdtemp())
        real = daemon_mod.json.loads

        def nested_too_deep(_data):                   # what Python <= 3.13 does at ~10,000 levels
            raise RecursionError("maximum recursion depth exceeded while decoding a JSON array")
        daemon_mod.json.loads = nested_too_deep       # (this is the json module itself: restore before parsing)
        try:
            raw = self.ask(d, b"[" * 20000 + b"]" * 20000 + b"\n")
        finally:
            daemon_mod.json.loads = real
        r = json.loads(raw)
        self.assertFalse(r["ok"])
        self.assertIn("bad request", r["error"])
        r = json.loads(self.ask(d, b"[" * 20000 + b"]" * 20000 + b"\n"))   # for real, on this Python
        self.assertFalse(r["ok"])

    def test_an_unexpected_error_inside_a_request_is_answered(self):
        d, _ = make_daemon(tempfile.mkdtemp())
        d.handle = lambda req: (_ for _ in ()).throw(KeyError("boom"))
        r = json.loads(self.ask(d, b'{"op": "status"}\n'))
        self.assertEqual(r, {"ok": False, "error": "internal error; see journalctl -u gpu-tunerd"})


class SocketRace(unittest.TestCase):
    """The restart race, fixed by hand on 2026-09-21: SIGTERM lands, and a replacement daemon
    binds the path before the old one reaches close_socket() in its `finally`. The old daemon
    must recognise the path is no longer its own socket and leave the replacement's alone."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "control.sock")

    def test_outgoing_daemon_does_not_unlink_a_replacements_socket(self):
        srv1, ino1 = daemon_mod.open_socket(self.path, None)
        srv2, ino2 = daemon_mod.open_socket(self.path, None)   # replacement binds first
        self.addCleanup(srv1.close)
        self.assertNotEqual(ino1, ino2)

        daemon_mod.close_socket(srv1, self.path, ino1)         # old daemon's shutdown
        self.assertTrue(os.path.exists(self.path))
        self.assertEqual(os.stat(self.path).st_ino, ino2)      # replacement's socket survives

        daemon_mod.close_socket(srv2, self.path, ino2)
        self.assertFalse(os.path.exists(self.path))

    def test_sole_daemon_still_removes_its_own_socket_on_close(self):
        srv, ino = daemon_mod.open_socket(self.path, None)
        daemon_mod.close_socket(srv, self.path, ino)
        self.assertFalse(os.path.exists(self.path))

    def test_close_socket_tolerates_the_path_already_being_gone(self):
        srv, ino = daemon_mod.open_socket(self.path, None)
        os.unlink(self.path)                                   # e.g. a third daemon already raced both
        daemon_mod.close_socket(srv, self.path, ino)            # must not raise


if __name__ == "__main__":
    unittest.main(verbosity=1)
