"""gpu-tuner UI server — runs as the desktop user, never as root.

Reads NVML directly (reads need no privilege), keeps the chart history, serves the page, and
forwards change requests to gpu-tunerd's socket. It validates nothing itself: the daemon is the
authority, and a bug here cannot widen a limit. With no daemon it runs monitor-only.

Why a localhost page needs auth at all: this box runs untrusted agents and containers, and any
local process can reach 127.0.0.1. So: loopback bind only, a session cookie whose secret never
appears in a URL or a log (`gpu-tuner open` trades a single-use 60 s nonce for it), a Host check
(DNS rebinding) and an Origin check on writes (CSRF from any page open in the browser).
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from . import __version__, safety
from .daemon import DEFAULT_SOCKET
from .nvml import REASONS, Nvml

DEFAULT_PORT = 8765
HISTORY_S = 6 * 3600
MAX_POINTS = 900               # a 15 min window is served raw; longer ones are bucket-averaged
WINDOWS = (300, 900, 3600, HISTORY_S)
NONCE_TTL_S = 60
NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{32,64}$")
SERIES = ("temp", "fan", "power", "cap", "clock", "util")
WEB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
STATIC = {"/": ("index.html", "text/html; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/style.css": ("style.css", "text/css; charset=utf-8")}
APPLY_OPS = ("set_power", "set_fan", "set_clock_cap", "baseline", "set_budget")


def config_dir():
    # ~/.config on purpose, not $XDG_CONFIG_HOME: a snap-launched terminal points that at
    # ~/snap/<app>/…, and `gpu-tuner open` from the launcher must find the same token dir.
    path = os.path.join(os.path.expanduser("~"), ".config", "gpu-tuner")
    os.makedirs(os.path.join(path, "nonces"), mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def session_token():
    path = os.path.join(config_dir(), "token")
    try:
        with open(path) as f:
            tok = f.read().strip()
        if len(tok) >= 32:
            return tok
    except FileNotFoundError:
        pass
    tok = secrets.token_urlsafe(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(tok)
    return tok


def mint_nonce():
    nonce = secrets.token_urlsafe(32)
    fd = os.open(os.path.join(config_dir(), "nonces", nonce), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    return nonce


def redeem_nonce(nonce):
    """Single use: the file is removed whether or not it was still fresh."""
    ndir = os.path.join(config_dir(), "nonces")
    now = time.time()
    for name in os.listdir(ndir):                     # sweep anything stale while we are here
        p = os.path.join(ndir, name)
        try:
            if now - os.stat(p).st_mtime > NONCE_TTL_S and name != nonce:
                os.unlink(p)
        except OSError:
            pass
    if not nonce or not NONCE_RE.match(nonce):
        return False
    path = os.path.join(ndir, nonce)
    try:
        fresh = now - os.stat(path).st_mtime <= NONCE_TTL_S
        os.unlink(path)
        return fresh
    except OSError:
        return False


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
    # comm only, never cmdline: command lines on this box carry API keys.
    try:
        with open(f"/proc/{pid}/comm") as f:
            return f.read().strip()
    except OSError:
        return "?"


class Monitor:
    def __init__(self, nv: Nvml, sock_path: str):
        self.nv, self.sock_path = nv, sock_path
        self.lock = threading.Lock()
        self.history = deque(maxlen=HISTORY_S)
        self.latest = {}
        self._daemon_cache = (0.0, None, None)

    def sample_once(self):
        now = time.time()
        live, row = {}, {}
        for g in self.nv.gpus:
            s = self.nv.sample(g)
            s["procs"] = sorted(({"name": _comm(p["pid"]), **p} for p in self.nv.processes(g)),
                                key=lambda p: -(p["vram_mib"] or 0))[:6]
            fans = [f for f in s["fans"] if f is not None]
            row[g.uuid] = (s["temp"], max(fans) if fans else None, s["power_w"],
                           s["power_limit_w"], s["clock_mhz"], s["util"])
            live[g.uuid] = s
        with self.lock:
            self.latest = {"t": now, "gpus": live}
            self.history.append((now, row))

    def run(self):
        nxt = time.monotonic()
        while True:
            try:
                self.sample_once()
            except Exception as e:      # noqa: BLE001 — one bad sample must not end the charts
                print(f"sampler: {e}", flush=True)
            nxt += 1.0
            time.sleep(max(0.0, nxt - time.monotonic()))

    def history_json(self, window):
        with self.lock:
            rows = list(self.history)
        cutoff = time.time() - window
        rows = [r for r in rows if r[0] >= cutoff]
        step = max(1, -(-len(rows) // MAX_POINTS))
        out = {"window": window, "bucket_s": step, "t": [],
               "gpus": {g.uuid: {k: [] for k in SERIES} for g in self.nv.gpus}}
        for i in range(0, len(rows), step):
            chunk = rows[i:i + step]
            out["t"].append(round(chunk[-1][0], 1))
            for g in self.nv.gpus:
                for k, key in enumerate(SERIES):
                    vals = [r[1][g.uuid][k] for r in chunk if g.uuid in r[1] and r[1][g.uuid][k] is not None]
                    out["gpus"][g.uuid][key].append(round(sum(vals) / len(vals), 1) if vals else None)
        return out

    def preview_status(self):
        """Status-shaped stand-in when the daemon is absent, so the page can show each card's
        envelope (ranges, floor, presets) with the controls disabled. Nothing here is applied."""
        gpus, used = [], 0
        for g in self.nv.gpus:
            prof = safety.profile_for(g.name)
            rng = safety.power_range(prof, g.power_min_w, g.power_max_w, g.power_default_w)
            live = self.nv.power_limit_w(g)
            used += live or 0
            floor, full_by = safety.safety_floor_for(g.thresholds.get("slowdown"))
            gpus.append({
                "uuid": g.uuid, "name": g.name, "fan_min": g.fan_min,
                "profile": None if prof is None else {"label": prof["label"], "note": prof["note"],
                                                      "power_presets": list(prof["power_presets"])},
                "baseline_power_w": None if prof is None else prof["baseline_power_w"],
                "power_range": None if rng is None else list(rng),
                "clock_range": [safety.CLOCK_CAP_MIN_MHZ, max(g.clocks)] if g.clocks else None,
                "settings": {"power_w": live, "clock_cap_mhz": None,
                             "fan": {"mode": "unmanaged", "manual_pct": 60,
                                     "curve": [list(p) for p in safety.LAB_CURVE]}},
                "fan": {"fan_pct": None, "floor_active": False, "fault": None, "temp": None},
                "full_by_c": full_by, "floor": [list(p) for p in floor]})
        return {"ok": True, "preview": True, "version": __version__, "dry_run": False,
                "budget_w": safety.DEFAULT_GPU_BUDGET_W, "budget_used_w": used,
                "wall_estimate_w": safety.wall_estimate_w(used),
                "circuits": safety.CIRCUIT_CONTINUOUS_W,
                "wall_model": {"non_gpu_dc_w": safety.NON_GPU_DC_W,
                               "psu_efficiency": safety.PSU_EFFICIENCY},
                "curve_temp_min_c": safety.CURVE_TEMP_MIN_C,
                "curve_max_points": safety.CURVE_MAX_POINTS,
                "curve_presets": {k: {"label": v["label"], "note": v["note"],
                                      "curve": [list(p) for p in v["curve"]]}
                                  for k, v in safety.CURVE_PRESETS.items()},
                "warnings": [], "gpus": gpus}

    def daemon_status(self):
        at, status, err = self._daemon_cache
        if time.monotonic() - at < 0.5:
            return status, err
        try:
            status, err = daemon_call(self.sock_path, {"op": "status"}, timeout=2.0), None
            if not status.get("ok"):
                status, err = None, status.get("error", "daemon error")
        except FileNotFoundError:
            status, err = None, "not installed"
        except (OSError, ValueError) as e:
            status, err = None, f"unreachable: {e}"
        self._daemon_cache = (time.monotonic(), status, err)
        return status, err

    def state_json(self):
        status, err = self.daemon_status()
        with self.lock:
            latest = self.latest
        return {"version": __version__, "driver": self.nv.driver,
                "static": [g.static() for g in self.nv.gpus], "live": latest,
                "reasons": {k: label for _bit, k, label in REASONS},
                "daemon": status if status is not None else self.preview_status(),
                "daemon_error": err, "windows": list(WINDOWS)}


def make_handler(mon: Monitor, token: str, port: int):
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    origins = {f"http://{h}" for h in hosts}

    class Handler(BaseHTTPRequestHandler):
        server_version = "gpu-tuner"
        sys_version = ""

        def log_message(self, fmt, *args):      # request lines carry the nonce: never log them
            pass

        def _send(self, code, body, ctype="application/json", extra=()):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; script-src 'self'; style-src 'self'; "
                             "connect-src 'self'; img-src 'self' data:; base-uri 'none'; "
                             "form-action 'none'; frame-ancestors 'none'")
            for k, v in extra:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _authed(self):
            jar = cookies.SimpleCookie()
            try:
                jar.load(self.headers.get("Cookie", ""))
            except cookies.CookieError:
                return False
            got = jar.get("gpu_tuner")
            return got is not None and hmac.compare_digest(got.value, token)

        def _gate(self):
            if self.headers.get("Host") not in hosts:
                self._send(421, {"error": "bad host"})
                return False
            return True

        def do_GET(self):
            if not self._gate():
                return
            url = urlsplit(self.path)
            qs = parse_qs(url.query)
            if url.path == "/" and "nonce" in qs:
                if redeem_nonce(qs["nonce"][0]):
                    self._send(303, b"", "text/plain", extra=(
                        ("Location", "/"),
                        ("Set-Cookie", f"gpu_tuner={token}; HttpOnly; SameSite=Strict; Path=/")))
                else:
                    self._send(403, b"That link has expired or was already used. Run: gpu-tuner open\n",
                               "text/plain; charset=utf-8")
                return
            if not self._authed():
                self._send(401, b"Not signed in. Open gpu-tuner from the app launcher, or run:\n\n"
                                b"    ops/gpu-tuner/gpu-tuner open\n", "text/plain; charset=utf-8")
                return
            if url.path in STATIC:
                name, ctype = STATIC[url.path]
                with open(os.path.join(WEB, name), "rb") as f:
                    self._send(200, f.read(), ctype)
            elif url.path == "/api/state":
                self._send(200, mon.state_json())
            elif url.path == "/api/history":
                try:
                    window = int(qs.get("window", ["300"])[0])
                except ValueError:
                    window = 300
                self._send(200, mon.history_json(window if window in WINDOWS else 300))
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._gate():
                return
            if not self._authed():
                self._send(401, {"error": "not signed in"})
                return
            if self.headers.get("Origin") not in origins:
                self._send(403, {"error": "cross-origin request refused"})
                return
            if urlsplit(self.path).path != "/api/apply":
                self._send(404, {"error": "not found"})
                return
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                self._send(415, {"error": "application/json only"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 16384:
                    raise ValueError("bad length")
                req = json.loads(self.rfile.read(length))
                if not isinstance(req, dict) or req.get("op") not in APPLY_OPS:
                    raise ValueError("unknown op")
            except ValueError as e:
                self._send(400, {"ok": False, "error": f"bad request: {e}"})
                return
            try:
                resp = daemon_call(mon.sock_path, req)
            except (OSError, ValueError) as e:
                self._send(503, {"ok": False, "error": f"control daemon unreachable: {e}"})
                return
            mon._daemon_cache = (0.0, None, None)
            self._send(200 if resp.get("ok") else 400, resp)

    return Handler


def cmd_serve(args):
    token = session_token()
    nv = Nvml()
    mon = Monitor(nv, args.socket)
    mon.sample_once()
    threading.Thread(target=mon.run, daemon=True).start()
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(mon, token, args.port))
    except OSError as e:
        sys.exit(f"gpu-tuner: cannot listen on 127.0.0.1:{args.port}: {e}")
    httpd.daemon_threads = True
    status, err = mon.daemon_status()
    print(f"gpu-tuner {__version__} on http://127.0.0.1:{args.port}/  "
          f"control daemon: {'connected' if status else 'MONITOR-ONLY (' + str(err) + ')'}\n"
          f"sign in with: gpu-tuner open", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def cmd_open(args):
    try:
        socket.create_connection(("127.0.0.1", args.port), timeout=1).close()
    except OSError:
        sys.exit(f"gpu-tuner: nothing is listening on 127.0.0.1:{args.port}. Start it with\n"
                 f"    systemctl --user start gpu-tuner-ui      (after install.sh), or\n"
                 f"    ops/gpu-tuner/gpu-tuner serve")
    url = f"http://127.0.0.1:{args.port}/?nonce={mint_nonce()}"
    if args.print_url:
        # The nonce is single-use and expires in 60 s; the cookie value itself is never printed.
        print(url)
        return 0
    try:
        subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        sys.exit("gpu-tuner: xdg-open not found; use `gpu-tuner open --print-url`")
    print("opened gpu-tuner in your browser")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="gpu-tuner", description="GPU power, fan and clock tuning GUI")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("serve", cmd_serve), ("open", cmd_open)):
        p = sub.add_parser(name)
        p.add_argument("--port", type=int, default=DEFAULT_PORT)
        p.set_defaults(fn=fn)
    sub.choices["serve"].add_argument("--socket", default=DEFAULT_SOCKET,
                                      help="gpu-tunerd control socket")
    sub.choices["open"].add_argument("--print-url", action="store_true",
                                     help="print a single-use sign-in URL instead of opening a browser")
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
