"""gpu-tuner UI server (the hub) — runs as the desktop user, never as root.

Serves the page and its API. Every machine it shows — this one included — is a `gpu-tuner node`
process (hub.py): a plain subprocess here, an ssh session elsewhere (hosts.py). Change requests go
to the machine the page names, whose own gpu-tunerd is the authority. This process validates
nothing itself, so a bug here cannot widen a limit anywhere. With no daemon on a machine, that
machine is monitor-only.

Why a localhost page needs auth at all: a GPU box often runs untrusted agents and containers, and
any local process can reach 127.0.0.1. So: loopback bind only, a session cookie whose secret never
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
import time
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from . import __version__, hosts, hub, proto
from .daemon import DEFAULT_SOCKET
from .nvml import REASONS

DEFAULT_PORT = 8765
NONCE_TTL_S = 60
NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{32,64}$")
WEB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
STATIC = {"/": ("index.html", "text/html; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/style.css": ("style.css", "text/css; charset=utf-8")}


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
            os.chmod(path, 0o600)
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


def state_json(h: hub.Hub):
    return {"version": __version__, "reasons": {k: label for _bit, k, label in REASONS},
            "windows": list(hub.WINDOWS), **h.state_json()}


def make_handler(h: hub.Hub, token: str, port: int):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    origins = {f"http://{x}" for x in allowed_hosts}

    class Handler(BaseHTTPRequestHandler):
        server_version = "gpu-tuner"
        sys_version = ""
        timeout = 10                            # a local process can't park a request thread forever

        def log_message(self, fmt, *args):      # request lines carry the nonce: never log them
            pass

        def _send(self, code, body, ctype="application/json", extra=()):
            # allow_nan=False: one NaN would make the browser's JSON.parse throw and blank the page
            data = body if isinstance(body, bytes) else json.dumps(body, allow_nan=False).encode()
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
            if self.headers.get("Host") not in allowed_hosts:
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
                                b"    gpu-tuner open\n", "text/plain; charset=utf-8")
                return
            if url.path in STATIC:
                name, ctype = STATIC[url.path]
                with open(os.path.join(WEB, name), "rb") as f:
                    self._send(200, f.read(), ctype)
            elif url.path == "/api/state":
                self._send(200, state_json(h))
            elif url.path == "/api/history":
                try:
                    window = int(qs.get("window", ["300"])[0])
                except ValueError:
                    window = 300
                hid = qs.get("host", [h.hosts[0]["id"]])[0]
                if hid not in h.links:
                    self._send(404, {"error": "no such machine"})
                    return
                self._send(200, h.history_json(hid, window if window in hub.WINDOWS else 300))
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
                req = proto.decode_line(self.rfile.read(length))
                if req.get("op") not in proto.APPLY_OPS:
                    raise ValueError("unknown op")
                # Which machine: required once there is more than one; never inferred from a GPU UUID.
                hid = req.get("host")
                if hid is None and len(h.hosts) == 1:
                    hid = h.hosts[0]["id"]
                if not isinstance(hid, str) or hid not in h.links:
                    raise ValueError("no such machine")
            except ValueError as e:
                self._send(400, {"ok": False, "error": f"bad request: {e}"})
                return
            code, resp = h.apply(hid, {k: req[k] for k in proto.APPLY_KEYS if k in req})
            self._send(code, resp)

    return Handler


def make_link(host, socket_path):
    if host.get("ssh"):
        try:
            argv = hosts.ssh_argv(host)
        except hosts.NoHubKey as e:
            # Shown on that machine's tab; never a fallback to your personal keys.
            msg = str(e)
            argv = [sys.executable, "-c", "import sys; sys.stderr.write(sys.argv[1] + '\\n'); sys.exit(78)", msg]
        return hub.NodeLink(host, argv, hosts.ssh_env())
    return hub.NodeLink(host, hosts.local_argv(socket_path))


def cmd_serve(args):
    token = session_token()
    try:
        machines = hosts.load_hosts(args.hosts)
    except hosts.HostsError as e:
        sys.exit(f"gpu-tuner: {args.hosts}: {e}")
    h = hub.Hub(machines, lambda m: make_link(m, args.socket))
    h.start()
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(h, token, args.port))
    except OSError as e:
        h.stop()
        sys.exit(f"gpu-tuner: cannot listen on 127.0.0.1:{args.port}: {e}")
    httpd.daemon_threads = True
    names = ", ".join(m["id"] + (f" (ssh {m['ssh']})" if m["ssh"] else "") for m in machines)
    print(f"gpu-tuner {__version__} on http://127.0.0.1:{args.port}/  managing {names}\n"
          f"sign in with: gpu-tuner open", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        h.stop()
    return 0


def cmd_open(args):
    try:
        socket.create_connection(("127.0.0.1", args.port), timeout=1).close()
    except OSError:
        sys.exit(f"gpu-tuner: nothing is listening on 127.0.0.1:{args.port}. Start it with\n"
                 f"    systemctl --user start gpu-tuner-ui      (after install.sh), or\n"
                 f"    gpu-tuner serve")
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
    ap = argparse.ArgumentParser(prog="gpu-tuner", description="GPU power, fan and clock tuning GUI",
                                 epilog="also: gpu-tuner node --stdio|--check · gpu-tuner probe · "
                                        "gpu-tuner hosts --check|--init-key")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("serve", cmd_serve), ("open", cmd_open)):
        p = sub.add_parser(name)
        p.add_argument("--port", type=int, default=DEFAULT_PORT)
        p.set_defaults(fn=fn)
    sub.choices["serve"].add_argument("--socket", default=DEFAULT_SOCKET,
                                      help="this machine's gpu-tunerd control socket")
    sub.choices["serve"].add_argument("--hosts", default=hosts.HOSTS_FILE,
                                      help="machines to manage (default: just this one if the file is absent)")
    sub.choices["open"].add_argument("--print-url", action="store_true",
                                     help="print a single-use sign-in URL instead of opening a browser")
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
