"""gpu-tuner node — one machine's GPUs, spoken over stdin/stdout to a gpu-tuner page (the hub).

Runs as the logged-in user, never root, on the machine whose GPUs it reports:
  * locally, the page starts it as a plain subprocess;
  * on another machine, the page starts it over ssh, ideally through a key that authorized_keys
    restricts to exactly this command (see README, "Multiple machines").

It reads NVML itself (reads need no privilege), and forwards only whitelisted change requests to
THIS machine's own gpu-tunerd over its Unix socket. That daemon re-validates every request against
its own safety envelope, exactly as for its local page, so nothing a hub sends can widen a limit
here. With no daemon installed, this machine is monitor-only.

The node exits when the hub goes away: stdin EOF, 30 s with no line from the hub (the hub pings
every 10 s), or a write to stdout blocked for 10 s. That keeps a dropped ssh session from leaving
an orphan polling NVML forever.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import queue
import socket
import sys
import threading
import time

from . import __version__, proto, safety
from .daemon import DEFAULT_SOCKET
from .nvml import Nvml

SAMPLE_S = 1.0
DAEMON_POLL_S = 2.0
DAEMON_RESEND_S = 10.0
HUB_SILENCE_S = 30.0
WRITE_STALL_S = 10.0
DAEMON_TIMEOUT_S = 6.0


def daemon_call(sock_path, req, timeout=4.0):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(sock_path)
        s.sendall(json.dumps(req).encode() + b"\n")
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    return json.loads(buf)


def _comm(pid):
    # comm only, never cmdline: command lines on a GPU box often carry API keys.
    try:
        with open(f"/proc/{pid}/comm") as f:
            return f.read().strip()
    except OSError:
        return "?"


def _pynvml_version():
    try:
        from importlib import metadata
    except ImportError:
        return None
    for dist in ("nvidia-ml-py", "pynvml"):
        try:
            return metadata.version(dist)
        except metadata.PackageNotFoundError:
            continue
    return None


class Collector:
    """Everything this machine knows about its own GPUs, as protocol-shaped dicts."""

    def __init__(self, nv: Nvml, sock_path: str):
        self.nv, self.sock_path = nv, sock_path

    def hello(self):
        env = os.environ
        return {"type": "hello", "proto": proto.PROTO, "version": __version__,
                "host": {"hostname": socket.gethostname(), "arch": platform.machine(),
                         "driver": self.nv.driver, "pynvml": _pynvml_version()},
                # Set by sshd only when authorized_keys forced a command on this login: the hub
                # warns about a remote reached with an unrestricted key.
                "forced_cmd": "SSH_ORIGINAL_COMMAND" in env, "via_ssh": "SSH_CONNECTION" in env,
                "static": [g.static() for g in self.nv.gpus]}

    def sample(self):
        gpus = {}
        for g in self.nv.gpus:
            s = self.nv.sample(g)
            s["procs"] = sorted(({"name": _comm(p["pid"]), **p} for p in self.nv.processes(g)),
                                key=lambda p: -(p["vram_mib"] or 0))[:6]
            gpus[g.uuid] = s
        return gpus

    def preview_status(self):
        """Status-shaped stand-in when this machine has no daemon: each card's envelope (ranges,
        floor, presets) with the controls locked. Nothing here is applied."""
        gpus, used, settable = [], 0, False
        for g in self.nv.gpus:
            prof = safety.profile_for(g.name)
            rng = safety.power_range(prof, g.power_min_w, g.power_max_w, g.power_default_w)
            settable = settable or rng is not None
            live = self.nv.power_limit_w(g) if rng is not None else None
            used += live or 0
            floor, full_by = safety.safety_floor_for(g.thresholds.get("slowdown"))
            gpus.append({
                "uuid": g.uuid, "name": g.name, "fan_min": g.fan_min,
                "profile": None if prof is None else {"label": prof["label"], "note": prof["note"],
                                                      "power_presets": list(prof["power_presets"])},
                "baseline_power_w": None if prof is None or rng is None else prof["baseline_power_w"],
                "power_range": None if rng is None else list(rng),
                "clock_range": [safety.CLOCK_CAP_MIN_MHZ, max(g.clocks)] if g.clocks else None,
                "settings": {"power_w": live, "clock_cap_mhz": None,
                             "fan": {"mode": "unmanaged", "manual_pct": 60,
                                     "curve": [list(p) for p in safety.default_curve(full_by)]}},
                "fan": {"fan_pct": None, "floor_active": False, "fault": None, "temp": None},
                "full_by_c": full_by, "floor": [list(p) for p in floor]})
        return {"ok": True, "preview": True, "version": __version__, "dry_run": False,
                "budget_w": None, "budget_used_w": used, "power_settable": settable,
                "curve_temp_min_c": safety.CURVE_TEMP_MIN_C,
                "curve_max_points": safety.CURVE_MAX_POINTS,
                "curve_presets": {k: {"label": v["label"], "note": v["note"],
                                      "curve": [list(p) for p in v["curve"]]}
                                  for k, v in safety.CURVE_PRESETS.items()},
                "warnings": [], "gpus": gpus}

    def daemon_status(self):
        """(status, error): the daemon's own status, or the preview plus why there is no daemon."""
        try:
            status = daemon_call(self.sock_path, {"op": "status"}, timeout=2.0)
            if status.get("ok"):
                return status, None
            return self.preview_status(), str(status.get("error", "daemon error"))
        except FileNotFoundError:
            return self.preview_status(), "not installed"
        except (OSError, ValueError) as e:
            return self.preview_status(), f"unreachable: {e}"

    def apply(self, req):
        """Forward one whitelisted change request to this machine's daemon; its reply verbatim."""
        if not isinstance(req, dict) or req.get("op") not in proto.APPLY_OPS:
            return {"ok": False, "error": "unknown op"}
        fwd = {k: req[k] for k in proto.APPLY_KEYS if k in req}
        try:
            return daemon_call(self.sock_path, fwd, timeout=DAEMON_TIMEOUT_S)
        except FileNotFoundError:
            return {"ok": False, "error": "the control daemon is not installed on this machine"}
        except (OSError, ValueError) as e:
            return {"ok": False, "error": f"control daemon unreachable: {e}"}


class Node:
    def __init__(self, collector: Collector, inp, out, clock=time.monotonic):
        self.c, self.inp, self.out, self.clock = collector, inp, out, clock
        self.wlock = threading.Lock()
        self.writing_since = None
        self.last_rx = clock()
        self.writes = queue.Queue(maxsize=4)
        self.kick = threading.Event()
        self.done = threading.Event()

    def send(self, msg):
        data = proto.encode(msg)
        with self.wlock:
            self.writing_since = self.clock()
            try:
                self.out.write(data)
                self.out.flush()
            except (BrokenPipeError, ValueError, OSError):
                self.done.set()
            finally:
                self.writing_since = None

    # ── threads ──────────────────────────────────────────────────────────────────────────
    def sampler(self):
        seq, nxt = 0, self.clock()
        while not self.done.is_set():
            try:
                self.send({"type": "sample", "seq": seq, "t": round(time.time(), 3), "gpus": self.c.sample()})
            except Exception as e:      # noqa: BLE001 — one bad sample must not end the stream
                print(f"gpu-tuner node: sampler: {e}", file=sys.stderr, flush=True)
            seq += 1
            nxt += SAMPLE_S
            self.done.wait(max(0.0, nxt - self.clock()))

    def daemon_watch(self):
        last, last_sent = None, -1e9
        while not self.done.is_set():
            status, err = self.c.daemon_status()
            msg = {"type": "daemon", "status": status, "error": err}
            key = json.dumps(msg, sort_keys=True)
            if key != last or self.clock() - last_sent >= DAEMON_RESEND_S:
                self.send(msg)
                last, last_sent = key, self.clock()
            self.kick.wait(DAEMON_POLL_S)
            self.kick.clear()

    def writer(self):
        while not self.done.is_set():
            try:
                rid, req = self.writes.get(timeout=0.5)
            except queue.Empty:
                continue
            self.send({"type": "reply", "id": rid, "resp": self.c.apply(req)})
            self.kick.set()         # push the new status right away

    def watchdog(self):
        while not self.done.wait(1.0):
            now, since = self.clock(), self.writing_since
            if since is not None and now - since > WRITE_STALL_S:
                os._exit(3)         # the hub stopped reading; nothing else can unblock this
            if now - self.last_rx > HUB_SILENCE_S:
                self.done.set()     # no ping for 30 s: the hub (or the link) is gone

    def handle(self, msg):
        rid = msg.get("id")
        if isinstance(rid, bool) or not isinstance(rid, int) or not 0 <= rid < proto.MAX_ID:
            return
        op = msg.get("op")
        if op == "ping":
            self.send({"type": "pong", "id": rid})
        elif op == "apply":
            try:
                self.writes.put_nowait((rid, msg.get("req")))
            except queue.Full:
                self.send({"type": "reply", "id": rid, "error": "too many changes in flight"})
        else:
            self.send({"type": "reply", "id": rid, "error": "unknown op"})

    def run(self):
        self.send(self.c.hello())
        for fn in (self.sampler, self.daemon_watch, self.writer, self.watchdog):
            threading.Thread(target=fn, daemon=True).start()
        reader = threading.Thread(target=self._read, daemon=True)
        reader.start()
        self.done.wait()
        return 0

    def _read(self):
        try:
            while not self.done.is_set():
                line = self.inp.readline(proto.MAX_LINE + 1)
                if not line:
                    break
                self.last_rx = self.clock()
                try:
                    msg = proto.decode_line(line)
                except proto.ProtoError:
                    continue        # the hub is ours; a bad line is dropped, not fatal
                self.handle(msg)
        except (OSError, ValueError):
            pass
        self.done.set()


def check(sock_path):
    """`gpu-tuner node --check`: what this machine would report, for a human (install.sh uses it)."""
    nv = Nvml()
    c = Collector(nv, sock_path)
    print(f"driver {nv.driver} · {len(nv.gpus)} GPU(s) · pynvml {_pynvml_version() or 'unknown'}")
    for g in nv.gpus:
        power = "no settable power limit" if g.power_max_w is None else f"power {g.power_min_w}-{g.power_max_w} W"
        clocks = f"clock cap up to {max(g.clocks)} MHz" if g.clocks else "no clock cap"
        print(f"  gpu{g.index} {g.name} · {power} · fans {g.nfans} · {clocks} · memory {g.mem_kind}")
    _status, err = c.daemon_status()
    print(f"control daemon: {'running' if err is None else err}")
    nv.close()
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="gpu-tuner node", description=__doc__.split("\n")[0])
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--stdio", action="store_true", help="speak the hub protocol on stdin/stdout")
    mode.add_argument("--check", action="store_true", help="print what this machine reports, then exit")
    ap.add_argument("--socket", default=DEFAULT_SOCKET, help="this machine's gpu-tunerd socket")
    args = ap.parse_args(argv)
    if args.check:
        return check(args.socket)
    out = sys.stdout.buffer
    try:
        nv = Nvml()
    except Exception as e:      # noqa: BLE001 — tell the hub why, in-protocol, then go
        out.write(proto.encode({"type": "fatal", "error": f"NVML unavailable on this machine: {e}"[:200]}))
        out.flush()
        return 2
    # No nvmlShutdown on the way out: the sampler may be mid-call, and process exit releases NVML.
    return Node(Collector(nv, args.socket), sys.stdin.buffer, out).run()
