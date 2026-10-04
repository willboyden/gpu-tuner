"""The node <-> hub wire protocol: one JSON object per line, over a pipe (local) or ssh (remote).

Pure functions, no I/O, so every rule here is unit-tested.

The hub treats every node as UNTRUSTED. A compromised or buggy machine can lie about its own
readings, but it must not be able to break the page for every other machine, make the hub use
unbounded memory, or put markup on the page. So every message is decoded with a size cap, with
NaN/Infinity rejected (Python's json accepts and re-emits them; one in /api/state would make the
browser's JSON.parse fail and blank every host), and then rebuilt from a whitelist of fields —
anything unknown is dropped, every number must be finite, every string and list is capped.

Node -> hub
  {"type":"hello","proto":1,"version",host:{hostname,arch,driver,pynvml},"forced_cmd","via_ssh","static":[gpu]}
  {"type":"sample","seq","t","gpus":{uuid: sample}}                    every second
  {"type":"daemon","status":{...}|null,"error":null|str}               on change, >= every 10 s, after writes
  {"type":"reply","id","resp":{...}} | {"type":"reply","id","error":str}
  {"type":"pong","id"}
  {"type":"fatal","error"}                                             then the node exits
Hub -> node
  {"id","op":"ping"}  every 10 s, and  {"id","op":"apply","req":{...}}
"""
from __future__ import annotations

import json
import math

PROTO = 1
MAX_LINE = 256 * 1024
# The only daemon requests that may cross the wire, and the only fields of each that are forwarded.
APPLY_OPS = ("set_power", "set_fan", "set_clock_cap", "baseline", "set_budget")
APPLY_KEYS = ("op", "uuid", "watts", "confirm_override", "mode", "curve", "manual_pct", "mhz")
MAX_GPUS = 16
MAX_STR = 256
MAX_ID = 2 ** 31
MAX_UUID = 96          # NVML GPU/MIG UUIDs are ~40 characters
MAX_NUM = 1e15         # no reading or setting comes near this; it keeps sums and averages finite


class ProtoError(ValueError):
    pass


def _reject_constant(name):
    raise ProtoError(f"non-finite number {name} in message")


def _finite_float(text):
    v = float(text)                 # "1e999" is not a NaN/Infinity constant, but parses to inf
    if not math.isfinite(v):
        raise ProtoError(f"non-finite number {text[:20]} in message")
    return v


def encode(obj) -> bytes:
    """One message as a line. allow_nan=False: a NaN is a bug, not something to send."""
    return json.dumps(obj, allow_nan=False, separators=(",", ":")).encode() + b"\n"


def decode_line(line: bytes) -> dict:
    if len(line) > MAX_LINE:
        raise ProtoError(f"line over {MAX_LINE} bytes")
    try:
        obj = json.loads(line, parse_constant=_reject_constant, parse_float=_finite_float)
    except ProtoError:
        raise
    except (ValueError, RecursionError) as e:      # incl. ints over Python's digit limit
        raise ProtoError(f"not JSON: {str(e)[:120]}") from None
    if not isinstance(obj, dict):
        raise ProtoError("message must be a JSON object")
    return obj


# ── field cleaners: invalid -> None, never an exception ──────────────────────────────────────
NUM, INT, STR, BOOL = "num", "int", "str", "bool"


def clean(v, spec):
    """Rebuild v to match spec. Scalars of the wrong type become None; dict specs keep only their
    own keys; ("list", spec, n) and ("map", spec, n) cap their length."""
    if v is None:
        return None
    if spec == NUM:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return None
        if isinstance(v, int) and abs(v) > 2 ** 53:
            return None
        return v if math.isfinite(v) and abs(v) <= MAX_NUM else None
    if spec == INT:
        if isinstance(v, bool) or not isinstance(v, int) or abs(v) > 2 ** 53:
            return None
        return v
    if spec == STR:
        return v[:MAX_STR] if isinstance(v, str) else None
    if spec == BOOL:
        return v if isinstance(v, bool) else None
    if isinstance(spec, dict):
        if not isinstance(v, dict):
            return None
        return {k: clean(v.get(k), sub) for k, sub in spec.items()}
    kind, sub, cap = spec
    if kind == "list":
        if not isinstance(v, list):
            return None
        return [clean(x, sub) for x in v[:cap]]
    if kind == "map":
        if not isinstance(v, dict):
            return None
        out = {}
        for k, x in list(v.items())[:cap]:
            if isinstance(k, str) and 0 < len(k) <= MAX_STR:
                out[k] = clean(x, sub)
        return out
    raise AssertionError(f"bad spec {spec!r}")


POINT = ("list", NUM, 2)
CURVE = ("list", POINT, 8)

STATIC_GPU = {
    "index": INT, "uuid": STR, "name": STR, "pci": STR, "vbios": STR, "nfans": INT,
    "fan_min": NUM, "fan_max": NUM, "power_min_w": NUM, "power_max_w": NUM, "power_default_w": NUM,
    "thresholds": ("map", NUM, 8), "mem_kind": STR, "clock_min_mhz": NUM, "clock_max_mhz": NUM,
}
SAMPLE_GPU = {
    "temp": NUM, "fans": ("list", NUM, 16), "fan_target": NUM, "power_w": NUM, "power_limit_w": NUM,
    "util": NUM, "mem_util": NUM, "vram_used_mib": NUM, "vram_total_mib": NUM, "mem_kind": STR,
    "clock_mhz": NUM, "mem_clock_mhz": NUM, "pstate": NUM, "reasons": ("list", STR, 16),
    "pcie_gen": NUM, "pcie_gen_max": NUM, "pcie_width": NUM, "pcie_width_max": NUM,
    "energy_j": NUM, "persistence": BOOL, "encoder_util": NUM, "decoder_util": NUM,
    "ecc_enabled": BOOL, "ecc_corrected_total": NUM, "ecc_uncorrected_total": NUM,
    "procs": ("list", {"pid": INT, "name": STR, "vram_mib": NUM}, 6),
}
STATUS_GPU = {
    "uuid": STR, "name": STR, "fan_min": NUM,
    "profile": {"label": STR, "note": STR,
                "power_presets": ("list", {"w": NUM, "label": STR, "note": STR}, 8)},
    "baseline_power_w": NUM, "power_range": POINT, "clock_range": POINT,
    "settings": {"power_w": NUM, "clock_cap_mhz": NUM,
                 "fan": {"mode": STR, "curve": CURVE, "manual_pct": NUM}},
    "fan": {"fan_pct": NUM, "floor_active": BOOL, "fault": STR, "temp": NUM},
    "full_by_c": NUM, "floor": CURVE,
}
STATUS = {
    "ok": BOOL, "preview": BOOL, "version": STR, "dry_run": BOOL, "budget_w": NUM,
    "budget_used_w": NUM, "power_settable": BOOL, "curve_temp_min_c": NUM, "curve_max_points": NUM,
    "curve_presets": ("map", {"label": STR, "note": STR, "curve": CURVE}, 8),
    "warnings": ("list", STR, 10), "gpus": ("list", STATUS_GPU, MAX_GPUS),
}
ERROR_REPLY = {"ok": BOOL, "error": STR, "over_budget": BOOL, "total_w": NUM, "budget_w": NUM,
               "others_w": NUM}
HELLO = {
    "proto": INT, "version": STR, "forced_cmd": BOOL, "via_ssh": BOOL,
    "host": {"hostname": STR, "arch": STR, "driver": STR, "pynvml": STR},
    "static": ("list", STATIC_GPU, MAX_GPUS),
}


def _has_uuid(g):
    return isinstance(g, dict) and isinstance(g.get("uuid"), str) and 0 < len(g["uuid"]) <= MAX_UUID


def sanitize_hello(msg):
    out = clean(msg, HELLO)
    if out["proto"] is None:
        raise ProtoError("hello has no protocol version")
    out["static"] = [g for g in (out["static"] or []) if _has_uuid(g)]
    out["host"] = out["host"] or {}
    return out


def sanitize_sample(msg):
    return {"seq": clean(msg.get("seq"), INT), "t": clean(msg.get("t"), NUM),
            "gpus": clean(msg.get("gpus"), ("map", SAMPLE_GPU, MAX_GPUS)) or {}}


def sanitize_status(status):
    """A daemon status (or a node's monitor-only preview). None stays None."""
    out = clean(status, STATUS)
    if out is None:
        return None
    out["gpus"] = [g for g in (out["gpus"] or []) if _has_uuid(g)]
    out["warnings"] = [w for w in (out["warnings"] or []) if w]
    out["curve_presets"] = out["curve_presets"] or {}
    return out


def sanitize_daemon(msg):
    return {"status": sanitize_status(msg.get("status")), "error": clean(msg.get("error"), STR)}


def sanitize_reply(msg):
    """{"id", "resp"} where resp is either a full status (ok) or an error reply; or {"id","error"}."""
    out = {"id": clean(msg.get("id"), INT)}
    resp = msg.get("resp")
    if isinstance(resp, dict):
        out["resp"] = (sanitize_status(resp) if resp.get("ok") is True
                       else {**clean(resp, ERROR_REPLY), "ok": False})
        if out["resp"].get("ok") is False and not out["resp"].get("error"):
            out["resp"]["error"] = "the daemon refused without saying why"
    else:
        out["error"] = clean(msg.get("error"), STR) or "malformed reply"
    return out
