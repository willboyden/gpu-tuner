"""gpu-tunerd — the root half of gpu-tuner. The only process that writes to the GPUs.

It supersedes ops/gpu-fan-curve.py (two fan writers would fight: SetFanSpeed pins a fixed speed
until the next write) and gpu-power-limit.service (it persists and re-applies the caps itself).

Failure policy — every path fails TOWARD the driver's own fan control, never toward a pinned fan:
  * SIGTERM/SIGINT        -> finally: hand all fans back to the driver.
  * crash / SIGKILL       -> the unit's ExecStopPost runs `--restore-fans`.
  * hung loop             -> systemd WatchdogSec kills it, then ExecStopPost as above.
  * temperature unreadable or a fan write refused -> that card goes back to driver control,
    and the daemon retries in FAULT_RETRY_S.
Power limits are left in place on exit on purpose: stopping a service must not uncap the
Workstation card to 600 W. They are re-asserted if anything else changes them.

Protocol: one JSON object per connection, newline-terminated, on a root-created Unix socket
owned by the configured desktop user (0600) and re-checked with SO_PEERCRED.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import stat
import struct
import sys
import time

from . import __version__, safety
from .nvml import Nvml

DEFAULT_CONFIG = "/etc/gpu-tuner/config.json"
DEFAULT_STATE = "/var/lib/gpu-tuner/state.json"
DEFAULT_SOCKET = "/run/gpu-tuner/control.sock"
MAX_REQUEST = 64 * 1024
READ_DEADLINE_S = 2.0    # hard wall-clock cap per request: a slow-trickling connection (same uid
                          # as allowed_uid, so it passes SO_PEERCRED) must not stall the fan
                          # safety loop, which shares this thread with the accept/serve loop
FAULT_RETRY_S = 30
FAN_REASSERT_S = 30       # rewrite an unchanged speed this often: resume-from-suspend and GPU
                          # resets silently hand the fans back to the driver
POWER_CHECK_S = 10

_running = True


def _stop(_sig, _frm):
    global _running
    _running = False


def log(msg):
    print(msg, flush=True)


def sd_notify(msg):
    path = os.environ.get("NOTIFY_SOCKET")
    if not path:
        return
    if path.startswith("@"):
        path = "\0" + path[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.sendto(msg.encode(), path)
    except OSError:
        pass


def load_config(path):
    cfg = {"allowed_uid": None, "gpu_budget_w": safety.DEFAULT_GPU_BUDGET_W, "interval_s": 2.0}
    try:
        with open(path) as f:
            raw = json.load(f)
    except FileNotFoundError:
        log(f"config {path} not found: defaults (root-only socket, {cfg['gpu_budget_w']} W budget)")
        return cfg
    if not isinstance(raw, dict):
        sys.exit(f"gpu-tunerd: {path} must be a JSON object")
    uid, budget, interval = raw.get("allowed_uid"), raw.get("gpu_budget_w"), raw.get("interval_s")
    if "gpu_budget_w" in raw and budget is None:
        cfg["gpu_budget_w"] = None    # explicit null: no card here has a settable cap (e.g. GB10)
    if uid is not None:
        if isinstance(uid, bool) or not isinstance(uid, int) or uid < 0:
            sys.exit(f"gpu-tunerd: allowed_uid in {path} must be a uid")
        cfg["allowed_uid"] = uid
    if budget is not None:
        if isinstance(budget, bool) or not isinstance(budget, int) or budget <= 0:
            sys.exit(f"gpu-tunerd: gpu_budget_w in {path} must be a positive whole number of watts")
        cfg["gpu_budget_w"] = budget
    if interval is not None:
        if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not 1 <= interval <= 10:
            sys.exit(f"gpu-tunerd: interval_s in {path} must be 1-10")
        cfg["interval_s"] = float(interval)
    return cfg


def save_config(path, cfg):
    """Persist cfg (allowed_uid, gpu_budget_w, interval_s) back to config.json. A no-op if path
    is falsy, so callers that never had a config file (dry-run tries, tests) can skip it."""
    if not path:
        return
    tmp = path + ".tmp"
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o644)   # world-readable by design (no secrets); don't rely on ambient umask
    os.replace(tmp, path)


class Daemon:
    def __init__(self, nv: Nvml, cfg: dict, state_path: str, config_path: str = None):
        self.nv, self.cfg, self.state_path, self.config_path = nv, cfg, state_path, config_path
        self.budget = cfg["gpu_budget_w"]
        self.warnings = []
        self.profiles = {g.uuid: safety.profile_for(g.name) for g in nv.gpus}
        self.ranges = {g.uuid: safety.power_range(self.profiles[g.uuid], g.power_min_w,
                                                  g.power_max_w, g.power_default_w)
                       for g in nv.gpus}
        self.rt = {g.uuid: self._fresh_rt() for g in nv.gpus}
        self.settings = {}
        self._next_power_check = 0.0

    @staticmethod
    def _fresh_rt():
        return {"prev": None, "fan_pct": None, "floor_active": False, "fault": None,
                "retry_at": 0.0, "auto_applied": False, "temp": None, "written_at": 0.0}

    @staticmethod
    def _full_by(g):
        """This card's own 'must be 100% by' temperature (safety.safety_floor_for), derived
        from ITS slowdown threshold instead of the lab's fixed 85 C."""
        return safety.safety_floor_for(g.thresholds.get("slowdown"))[1]

    def warn(self, msg):
        log(f"WARNING {msg}")
        self.warnings.append(msg)

    # ── settings: load, adopt, persist ─────────────────────────────────────────────────────
    def baseline_power(self, g):
        rng = self.ranges[g.uuid]
        if rng is None:
            return None
        prof = self.profiles[g.uuid]
        want = prof["baseline_power_w"] if prof else g.power_default_w
        return max(rng[0], min(rng[1], want))

    def _load_one(self, g, saved):
        """One card's settings from the state file, each field re-validated. The state file is
        root-owned, but a stale one (budget lowered, card swapped) must not bypass the checks."""
        rng = self.ranges[g.uuid]
        default = [list(p) for p in safety.default_curve(self._full_by(g))]
        s = {"power_w": None, "clock_cap_mhz": None,
             "fan": {"mode": "curve", "curve": default, "manual_pct": 60}}
        saved = saved if isinstance(saved, dict) else {}
        if rng is not None:
            live = self.nv.power_limit_w(g)
            want = saved.get("power_w", live)       # first run: adopt whatever is in force now
            try:
                want = safety._as_int(want, "power limit")
                if not rng[0] <= want <= rng[1]:
                    raise safety.SafetyError(f"{want} W is outside {rng[0]}-{rng[1]} W")
                s["power_w"] = want
            except safety.SafetyError as e:
                s["power_w"] = self.baseline_power(g)
                self.warn(f"{g.name}: saved power limit rejected ({e}); using {s['power_w']} W")
        fan = saved.get("fan") if isinstance(saved.get("fan"), dict) else {}
        if not g.nfans:
            fan = {}                # nothing to drive, so nothing to validate or warn about
        try:
            if fan.get("mode") in ("curve", "manual", "auto"):
                s["fan"]["mode"] = fan["mode"]
            if "curve" in fan:
                s["fan"]["curve"] = safety.validate_curve(fan["curve"], full_by=self._full_by(g),
                                                           fan_min=g.fan_min)
            if "manual_pct" in fan:
                s["fan"]["manual_pct"] = safety.validate_manual(fan["manual_pct"], g.fan_min)
        except safety.SafetyError as e:
            s["fan"] = {"mode": "curve", "curve": default, "manual_pct": 60}
            self.warn(f"{g.name}: saved fan settings rejected ({e}); using this card's default curve")
        try:
            s["clock_cap_mhz"] = safety.validate_clock_cap(saved.get("clock_cap_mhz"), g.clocks)
        except safety.SafetyError as e:
            self.warn(f"{g.name}: saved clock cap rejected ({e}); cleared")
        return s

    def load_state(self):
        saved = {}
        try:
            with open(self.state_path) as f:
                doc = json.load(f)
            if isinstance(doc, dict) and isinstance(doc.get("gpus"), dict):
                saved = doc["gpus"]
        except FileNotFoundError:
            log("no saved state: adopting the power limits currently in force and the lab fan curve")
        except (OSError, ValueError) as e:
            self.warn(f"state file unreadable ({e}); starting from live limits and the lab curve")
        for g in self.nv.gpus:
            self.settings[g.uuid] = self._load_one(g, saved.get(g.uuid))
        total = sum(s["power_w"] or 0 for s in self.settings.values())
        if self.budget is not None and total > self.budget:
            for g in self.nv.gpus:
                if self.ranges[g.uuid] is not None:
                    self.settings[g.uuid]["power_w"] = self.baseline_power(g)
            if sum(s["power_w"] or 0 for s in self.settings.values()) > self.budget:
                for g in self.nv.gpus:
                    if self.ranges[g.uuid] is not None:
                        self.settings[g.uuid]["power_w"] = self.ranges[g.uuid][0]
            now = sum(s["power_w"] or 0 for s in self.settings.values())
            self.warn(f"saved power limits totalled {total} W, over the {self.budget} W budget; "
                      f"fell back to {now} W")

    def save_state(self):
        doc = {"version": 1, "gpus": self.settings}
        tmp = self.state_path + ".tmp"
        os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
        with open(tmp, "w") as f:
            json.dump(doc, f, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)   # matches install.sh's seed; don't rely on ambient umask
        os.replace(tmp, self.state_path)

    # ── applying settings to hardware ──────────────────────────────────────────────────────
    def _apply_power(self, g, watts):
        """Write, then READ BACK. Returns the limit actually in force."""
        self.nv.set_power_limit_w(g, watts)
        live = self.nv.power_limit_w(g)
        if live != watts:
            raise RuntimeError(f"driver accepted {watts} W but reports {live} W in force")
        return live

    def apply_startup(self):
        for g in self.nv.gpus:
            try:
                self.nv.set_persistence(g)   # without it the limits drop when the last client exits
            except self.nv.Error as e:
                if self.nv.not_supported(e):  # e.g. GB10: a fact about the card, not a fault to keep showing
                    log(f"{g.name}: persistence mode not supported by this GPU")
                else:
                    self.warn(f"{g.name}: could not enable persistence mode: {e}")
        # Lower first, raise second: the combined cap never overshoots max(before, after).
        todo = [(g, self.settings[g.uuid]["power_w"]) for g in self.nv.gpus
                if self.settings[g.uuid]["power_w"] is not None]
        todo.sort(key=lambda gw: gw[1] - (self.nv.power_limit_w(gw[0]) or 0))
        for g, watts in todo:
            if self.nv.power_limit_w(g) == watts:
                continue
            try:
                self._apply_power(g, watts)
                log(f"{g.name}: power limit -> {watts} W")
            except (self.nv.Error, RuntimeError) as e:
                self.warn(f"{g.name}: could not apply {watts} W: {e}")
        for g in self.nv.gpus:
            cap = self.settings[g.uuid]["clock_cap_mhz"]
            if cap is not None:
                try:
                    self.nv.set_clock_cap(g, cap)
                    log(f"{g.name}: core clock capped at {cap} MHz")
                except self.nv.Error as e:
                    self.warn(f"{g.name}: could not apply the {cap} MHz clock cap: {e}")

    def _fan_fault(self, g, msg, now):
        rt = self.rt[g.uuid]
        try:
            self.nv.set_fan_auto(g)
            handed = "fans handed back to the driver"
        except self.nv.Error as e:
            handed = f"and handing the fans back to the driver ALSO failed: {e}"
        rt.update(self._fresh_rt(), fault=f"{msg}; {handed}", retry_at=now + FAULT_RETRY_S)
        log(f"FAULT {g.name}: {rt['fault']} (retry in {FAULT_RETRY_S}s)")

    def tick(self, now):
        for g in self.nv.gpus:
            if not g.nfans:
                continue
            fan, rt = self.settings[g.uuid]["fan"], self.rt[g.uuid]
            if rt["retry_at"] > now:
                continue
            if fan["mode"] == "auto":
                if not rt["auto_applied"]:
                    try:
                        self.nv.set_fan_auto(g)
                        rt.update(self._fresh_rt(), auto_applied=True)
                        log(f"{g.name}: fans -> driver default curve")
                    except self.nv.Error as e:
                        self._fan_fault(g, f"could not hand fans to the driver: {e}", now)
                continue
            try:
                temp = self.nv.temp_c(g)
            except self.nv.Error as e:
                self._fan_fault(g, f"temperature unreadable: {e}", now)
                continue
            target, floor_active = safety.fan_target(fan["mode"], temp, fan["curve"],
                                                     fan["manual_pct"],
                                                     slowdown_c=g.thresholds.get("slowdown"),
                                                     fan_min=g.fan_min)
            pct = safety.hysteresis(target, temp, rt["prev"])
            changed = pct != rt["fan_pct"]
            if changed or now - rt["written_at"] >= FAN_REASSERT_S:
                try:
                    self.nv.set_fan_pct(g, pct)
                except self.nv.Error as e:
                    self._fan_fault(g, f"fan write refused: {e}", now)
                    continue
                rt["written_at"] = now
                if changed:
                    log(f"{g.name}: {temp}C -> {pct}%{' (safety floor)' if floor_active else ''}")
                    rt["prev"] = (pct, temp)
            rt.update(fan_pct=pct, floor_active=floor_active, temp=temp, fault=None)
        if now >= self._next_power_check:
            self._next_power_check = now + POWER_CHECK_S
            self._enforce_power()

    def _enforce_power(self):
        for g in self.nv.gpus:
            want = self.settings[g.uuid]["power_w"]
            live = self.nv.power_limit_w(g)
            if want is None or live is None or live == want:
                continue
            try:
                self._apply_power(g, want)
                log(f"{g.name}: power limit had drifted to {live} W (changed outside gpu-tuner); "
                    f"re-applied {want} W")
            except (self.nv.Error, RuntimeError) as e:
                log(f"FAULT {g.name}: limit is {live} W, wanted {want} W, re-apply failed: {e}")

    def restore_fans(self):
        for g in self.nv.gpus:
            if g.nfans:
                try:
                    self.nv.set_fan_auto(g)
                except self.nv.Error as e:
                    log(f"FAULT {g.name}: could not hand fans back to the driver: {e}")
        log("fans handed back to the driver")

    # ── requests ───────────────────────────────────────────────────────────────────────────
    def status(self):
        used = sum(s["power_w"] or 0 for s in self.settings.values())
        gpus = []
        for g in self.nv.gpus:
            prof, rt, rng = self.profiles[g.uuid], self.rt[g.uuid], self.ranges[g.uuid]
            floor, full_by = safety.safety_floor_for(g.thresholds.get("slowdown"))
            gpus.append({
                "uuid": g.uuid, "name": g.name, "fan_min": g.fan_min,
                "profile": None if prof is None else {
                    "label": prof["label"], "note": prof["note"],
                    "power_presets": list(prof["power_presets"])},
                "baseline_power_w": self.baseline_power(g),
                "power_range": None if rng is None else list(rng),
                "clock_range": [safety.CLOCK_CAP_MIN_MHZ, max(g.clocks)] if g.clocks else None,
                "settings": self.settings[g.uuid],
                "fan": {k: rt[k] for k in ("fan_pct", "floor_active", "fault", "temp")},
                # Each card's OWN floor, derived from its own slowdown threshold — not a single
                # machine-wide constant, so a mixed-card box shows the right ceiling per card.
                "full_by_c": full_by, "floor": [list(p) for p in floor],
            })
        warnings = list(self.warnings[-10:])
        if self.budget is not None and used > self.budget:
            warnings.append(f"combined power is {used} W, over the {self.budget} W budget")
        return {"ok": True, "version": __version__, "dry_run": self.nv.dry_run,
                "budget_w": self.budget, "budget_used_w": used,
                "power_settable": any(r is not None for r in self.ranges.values()),
                "curve_temp_min_c": safety.CURVE_TEMP_MIN_C,
                "curve_max_points": safety.CURVE_MAX_POINTS,
                "curve_presets": {k: {"label": v["label"], "note": v["note"],
                                      "curve": [list(p) for p in v["curve"]]}
                                  for k, v in safety.CURVE_PRESETS.items()},
                "warnings": warnings, "gpus": gpus}

    def set_budget(self, watts):
        watts = safety._as_int(watts, "GPU budget")
        if watts <= 0:
            raise safety.SafetyError("the GPU budget must be a positive number of watts")
        hw_max = sum(rng[1] for rng in self.ranges.values() if rng is not None)
        if not hw_max:
            raise safety.SafetyError("no card on this machine has a settable power limit, so there "
                                     "is nothing for a budget to limit")
        if watts > hw_max:
            raise safety.SafetyError(
                f"{watts} W is above {hw_max} W, the sum of every card's own hardware maximum — "
                f"a budget above that can never actually bind on anything")
        self.budget = watts
        self.cfg["gpu_budget_w"] = watts
        save_config(self.config_path, self.cfg)

    def _set_power(self, g, watts, confirm_override=False):
        current = {u: s["power_w"] or 0 for u, s in self.settings.items()}
        watts = safety.check_power(g.uuid, watts, self.ranges, current, self.budget,
                                    confirm_override)
        self._apply_power(g, watts)
        self.settings[g.uuid]["power_w"] = watts

    def _set_fan(self, g, req):
        if not g.nfans:
            raise safety.SafetyError("this card has no controllable fans")
        mode = req.get("mode")
        if mode not in ("curve", "manual", "auto"):
            raise safety.SafetyError("fan mode must be curve, manual or auto")
        fan = dict(self.settings[g.uuid]["fan"])
        if "curve" in req:
            fan["curve"] = safety.validate_curve(req["curve"], full_by=self._full_by(g),
                                                  fan_min=g.fan_min)
        if "manual_pct" in req:
            fan["manual_pct"] = safety.validate_manual(req["manual_pct"], g.fan_min)
        fan["mode"] = mode
        self.settings[g.uuid]["fan"] = fan
        self.rt[g.uuid] = self._fresh_rt()      # drop hysteresis so the change shows at once

    def _set_clock_cap(self, g, mhz):
        cap = safety.validate_clock_cap(mhz, g.clocks)
        if cap is None:
            self.nv.reset_clock_cap(g)
        else:
            self.nv.set_clock_cap(g, cap)
        self.settings[g.uuid]["clock_cap_mhz"] = cap

    def handle(self, req):
        if not isinstance(req, dict):
            return {"ok": False, "error": "request must be a JSON object"}
        op = req.get("op")
        if op == "status":
            return self.status()
        if op == "set_budget":
            try:
                self.set_budget(req.get("watts"))
            except safety.SafetyError as e:
                return {"ok": False, "error": str(e)}
            return self.status()
        if op not in ("set_power", "set_fan", "set_clock_cap", "baseline"):
            return {"ok": False, "error": f"unknown op {op!r}"}
        g = self.nv.by_uuid(req.get("uuid"))
        if g is None:
            return {"ok": False, "error": "no such GPU"}
        err, done, extra = None, [], {}
        try:
            if op == "set_power":
                self._set_power(g, req.get("watts"), req.get("confirm_override") is True)
            elif op == "set_fan":
                self._set_fan(g, req)
            elif op == "set_clock_cap":
                self._set_clock_cap(g, req.get("mhz"))
            else:
                # Always-safe steps first, power last: power is the one the budget can refuse.
                if g.nfans:
                    self._set_fan(g, {"mode": "curve",
                                      "curve": [list(p) for p in safety.default_curve(self._full_by(g))]})
                    done.append("fan curve")
                if self.settings[g.uuid]["clock_cap_mhz"] is not None:
                    self._set_clock_cap(g, None)
                    done.append("clock cap")
                base = self.baseline_power(g)
                if base is not None:
                    self._set_power(g, base)
        except safety.BudgetExceeded as e:
            err = str(e)
            extra = {"over_budget": True, "total_w": e.total_w, "budget_w": e.budget_w,
                     "others_w": e.others_w}
        except safety.SafetyError as e:
            err = str(e)
        except (self.nv.Error, RuntimeError) as e:
            err = f"the driver refused: {e}"
        # Each step mutates settings only after it succeeds, so saving here is right on both
        # paths: a half-done baseline persists exactly the half that happened.
        self.save_state()
        self.tick(time.monotonic())
        if err is not None:
            if done:
                err = f"{' and '.join(done)} reset, but the power limit was not: {err}"
            return {"ok": False, "error": err, **extra}
        return self.status()


# ── socket server ──────────────────────────────────────────────────────────────────────────
def open_socket(path, allowed_uid):
    if len(os.fsencode(path)) > 107:      # sockaddr_un.sun_path; bind() only says "path too long"
        sys.exit(f"gpu-tunerd: socket path is over 107 bytes: {path}")
    try:
        if stat.S_ISSOCK(os.stat(path).st_mode):
            os.unlink(path)               # stale socket from a previous run; never a regular file
    except FileNotFoundError:
        pass
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old = os.umask(0o177)                 # born 0600: no window where it is world-connectable
    try:
        srv.bind(path)
    finally:
        os.umask(old)
    if allowed_uid is not None and os.geteuid() == 0:
        os.chown(path, allowed_uid, -1)
    srv.listen(8)
    return srv, os.stat(path).st_ino


def close_socket(srv, path, ino):
    srv.close()
    # Only unlink the path if it is still OUR socket: a replacement daemon that started while
    # we were shutting down has already re-bound it (seen once during dry-run testing).
    try:
        if os.stat(path).st_ino == ino:
            os.unlink(path)
    except OSError:
        pass


def authorised(peer_uid, allowed_uid, own_euid):
    """root, the configured desktop user, or (dry-run only, where own_euid != 0) whoever runs us."""
    return peer_uid == 0 or peer_uid == own_euid or (allowed_uid is not None and peer_uid == allowed_uid)


def serve_one(conn, daemon, allowed_uid):
    conn.settimeout(1.0)
    pid, uid, _gid = struct.unpack("3i", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                                         struct.calcsize("3i")))
    if not authorised(uid, allowed_uid, os.geteuid()):
        log(f"audit uid={uid} pid={pid} REFUSED: not the configured user")
        resp = {"ok": False, "error": "not authorised"}
    else:
        buf, deadline = b"", time.monotonic() + READ_DEADLINE_S
        while b"\n" not in buf and len(buf) <= MAX_REQUEST and time.monotonic() < deadline:
            conn.settimeout(max(0.01, deadline - time.monotonic()))
            try:
                chunk = conn.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            buf += chunk
        req, resp = None, None
        try:
            if len(buf) > MAX_REQUEST:
                raise ValueError("request too large")
            req = json.loads(buf.split(b"\n", 1)[0])
        except ValueError as e:
            resp = {"ok": False, "error": f"bad request: {e}"}
        if resp is None:
            resp = daemon.handle(req)
        if isinstance(req, dict) and req.get("op") != "status":
            detail = {k: v for k, v in req.items()
                      if k in ("op", "uuid", "watts", "mode", "manual_pct", "mhz", "confirm_override")}
            log(f"audit uid={uid} pid={pid} {json.dumps(detail, sort_keys=True)} -> "
                f"{'ok' if resp.get('ok') else 'REJECTED: ' + str(resp.get('error'))}")
    conn.sendall(json.dumps(resp).encode() + b"\n")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="gpu-tunerd", description=__doc__.split("\n")[0])
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--state", default=DEFAULT_STATE)
    ap.add_argument("--socket", default=DEFAULT_SOCKET)
    ap.add_argument("--dry-run", action="store_true",
                    help="validate and log every change but write nothing to the GPUs (no root needed)")
    ap.add_argument("--restore-fans", action="store_true",
                    help="hand every fan back to the driver and exit (the unit's ExecStopPost)")
    args = ap.parse_args(argv)

    if args.restore_fans:
        try:
            nv = Nvml()
        except Exception as e:            # noqa: BLE001 — a stop hook must not fail the unit
            log(f"restore-fans: NVML unavailable ({e}); nothing to do")
            return 0
        Daemon(nv, {"gpu_budget_w": safety.DEFAULT_GPU_BUDGET_W}, args.state).restore_fans()
        nv.close()
        return 0

    if os.geteuid() != 0 and not args.dry_run:
        sys.exit("gpu-tunerd: power and fan writes are root-only on these cards. "
                 "Run under systemd (see install.sh), or pass --dry-run to try it without root.")

    cfg = load_config(args.config)
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    nv = Nvml(dry_run=args.dry_run)
    d = Daemon(nv, cfg, args.state, args.config)
    log(f"gpu-tunerd {__version__} driver {nv.driver}{' DRY-RUN' if args.dry_run else ''} "
        f"budget {'none' if d.budget is None else str(d.budget) + ' W'} interval {cfg['interval_s']}s")
    for g in nv.gpus:
        log(f"  gpu{g.index} {g.name} {g.uuid} fans={g.nfans} settable={d.ranges[g.uuid]} W")
    srv = None
    try:
        d.load_state()
        d.apply_startup()
        d.save_state()
        srv, ino = open_socket(args.socket, cfg["allowed_uid"])
        sd_notify("READY=1")
        next_tick = 0.0
        while _running:
            now = time.monotonic()
            if now >= next_tick:
                d.tick(now)
                next_tick = now + cfg["interval_s"]
                sd_notify("WATCHDOG=1")
            # Python retries accept() after a signal handler returns (PEP 475), so a SIGTERM is
            # only noticed when this timeout expires: keep it short.
            srv.settimeout(max(0.05, min(0.5, next_tick - time.monotonic())))
            try:
                conn, _ = srv.accept()
            except (socket.timeout, InterruptedError):
                continue
            with conn:
                try:
                    serve_one(conn, d, cfg["allowed_uid"])
                except OSError as e:
                    log(f"client error: {e}")
    finally:
        d.restore_fans()
        if srv is not None:
            close_socket(srv, args.socket, ino)
        nv.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
