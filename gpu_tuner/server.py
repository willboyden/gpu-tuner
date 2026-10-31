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
from .browser_sessions import BrowserSessions
from .login_limit import LoginLimiter
from .password_auth import verify_password, valid_password_hash

DEFAULT_PORT = 8765
COOKIE = "gpu_tuner"
REMEMBER_SECONDS = 400 * 24 * 60 * 60  # Renewed during authenticated use.
NONCE_TTL_S = 60
NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{32,64}$")
WEB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
STATIC = {"/": ("index.html", "text/html; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/style.css": ("style.css", "text/css; charset=utf-8"),
          "/theme.js": ("theme.js", "text/javascript; charset=utf-8"),
          "/login": ("login.html", "text/html; charset=utf-8"),
          "/login.js": ("login.js", "text/javascript; charset=utf-8")}


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


def state_json(h: hub.Hub, demo=False):
    out = {"version": __version__, "reasons": {k: label for _bit, k, label in REASONS},
           "windows": list(hub.WINDOWS), **h.state_json()}
    if demo:
        out["demo"] = True
    return out



def public_url_host(value):
    """Return the exact HTTP Host for a configured LAN URL."""
    from urllib.parse import urlsplit
    import ipaddress

    if not value:
        return None

    url = urlsplit(value)

    if (
        url.scheme != "http"
        or not url.netloc
        or url.username is not None
        or url.password is not None
        or url.path not in ("", "/")
        or url.query
        or url.fragment
    ):
        raise ValueError(
            "GPU_TUNER_PUBLIC_URL must be an HTTP URL without credentials, "
            "a path, query or fragment"
        )

    try:
        hostname = url.hostname
        port = url.port
        if hostname is None or port is None:
            raise ValueError("explicit port required")

        # Require a literal IPv4 address or localhost. Do not allow
        # arbitrary DNS names in the Host allowlist.
        if hostname != "localhost":
            hostname = str(ipaddress.IPv4Address(hostname))

        expected = f"{hostname}:{port}"
        if url.netloc != expected:
            raise ValueError("noncanonical host or port")

        if not 1 <= port <= 65535:
            raise ValueError("invalid port")

    except ValueError as exc:
        raise ValueError(
            f"Invalid GPU_TUNER_PUBLIC_URL {value!r}: {exc}"
        ) from exc

    return expected

def make_handler(h: hub.Hub, token: str, port: int, cookie: str = COOKIE, demo: bool = False):
    """The page's request handler. `cookie` names the session cookie: demo mode uses its own, so
    signing in to a demo never replaces the real page's cookie (cookies ignore the port)."""
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    lan_ip = os.environ.get("GPU_TUNER_LAN_IP", "").strip()
    if lan_ip:
        import ipaddress
        lan_ip = str(ipaddress.IPv4Address(lan_ip))
        allowed_hosts.add(f"{lan_ip}:{port}")
    public_host = public_url_host(
        os.environ.get("GPU_TUNER_PUBLIC_URL", "").strip()
    )
    if public_host:
        allowed_hosts.add(public_host)

    origins = {f"http://{x}" for x in allowed_hosts}
    login_limiter = LoginLimiter()
    browser_sessions = BrowserSessions(
        persistence_path=os.path.join(config_dir(), "browser-sessions.json"),
    )

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
            refresh = getattr(self, "_remembered_token", None)
            if refresh and not any(k.lower() == "set-cookie" for k, _ in extra):
                self.send_header("Set-Cookie", self._browser_cookie(refresh, True))
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
            got = jar.get(cookie)
            if got is None:
                return False
            if hmac.compare_digest(got.value, token):
                return True
            if not browser_sessions.valid(got.value, self._password_credential()):
                return False
            if browser_sessions.remembered(got.value):
                self._remembered_token = got.value
            return True

        def _browser_cookie(self, browser_token, remember):
            persistence = f"; Max-Age={REMEMBER_SECONDS}" if remember else ""
            return f"{cookie}={browser_token}; HttpOnly; SameSite=Strict; Path=/{persistence}"

        def _password_credential(self):
            """Read the current credential without performing password hashing."""
            try:
                with open(os.path.join(config_dir(), "password.hash"), encoding="ascii") as f:
                    stored = f.read(256).strip()
                if valid_password_hash(stored):
                    return stored
            except (OSError, ValueError, UnicodeError):
                pass
            return None

        def _gate(self):
            if self.headers.get("Host") not in allowed_hosts:
                self._send(421, {"error": "bad host"})
                return False
            return True

        def _static_body(self, name):
            with open(os.path.join(WEB, name), "rb") as f:
                body = f.read()
            if name == "login.html":
                container = os.environ.get("HOST_CONTAINERNAME", "gpu-tuner")
                if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", container):
                    container = "gpu-tuner"
                body = body.replace(b"docker exec -it gpu-tuner ",
                                    f"docker exec -it {container} ".encode("ascii"))
            return body

        def do_GET(self):
            if not self._gate():
                return
            url = urlsplit(self.path)
            qs = parse_qs(url.query)
            if url.path == "/" and "nonce" in qs:
                if redeem_nonce(qs["nonce"][0]):
                    self._send(303, b"", "text/plain", extra=(
                        ("Location", "/"),
                        ("Set-Cookie", f"{cookie}={token}; HttpOnly; SameSite=Strict; Path=/")))
                else:
                    self._send(403, b"That link has expired or was already used. Run: gpu-tuner open\n",
                               "text/plain; charset=utf-8")
                return
            password_enabled = os.path.isfile(
                os.path.join(config_dir(), "password.hash"))

            if password_enabled and url.path in (
                "/login", "/login.js", "/theme.js", "/style.css"
            ):
                name, ctype = STATIC[url.path]
                self._send(200, self._static_body(name), ctype)
                return

            if not self._authed():
                if password_enabled and url.path == "/":
                    self._send(303, b"", "text/plain", extra=(("Location", "/login"),))
                else:
                    self._send(401, b"Not signed in. Open gpu-tuner from the app launcher, or run:\n\n"
                                    b"    gpu-tuner open\n", "text/plain; charset=utf-8")
                return
            if url.path in STATIC:
                name, ctype = STATIC[url.path]
                self._send(200, self._static_body(name), ctype)
            elif url.path == "/api/state":
                self._send(200, state_json(h, demo))
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

        def _password_login(self):
            if self.headers.get("Origin") not in origins:
                self._send(403, {"error": "cross-origin request refused"})
                return

            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                self._send(415, {"error": "application/json only"})
                return

            try:
                length = int(self.headers.get("Content-Length", ""))
                if not 0 < length <= 4096:
                    raise ValueError("invalid length")
            except ValueError:
                self._send(400, {"error": "invalid request"})
                return

            if not login_limiter.allow():
                self._send(429, {"error": "too many login attempts"})
                return

            try:
                body = json.loads(self.rfile.read(length))
                if (not isinstance(body, dict) or "password" not in body
                        or set(body) - {"password", "remember"}):
                    raise ValueError("invalid body")
                remember = body.get("remember", False)
                if not isinstance(remember, bool):
                    raise ValueError("invalid remember choice")
                password = body["password"]
                if (
                    not isinstance(password, str)
                    or not 12 <= len(password) <= 1024
                    or "\x00" in password
                ):
                    raise ValueError("invalid password")
            except (ValueError, UnicodeError, TypeError):
                self._send(400, {"error": "invalid request"})
                return

            stored = self._password_credential()
            if stored is None:
                self._send(503, {"error": "password authentication unavailable"})
                return

            if not verify_password(password, stored):
                self._send(401, {"error": "invalid credentials"})
                return

            browser_token = browser_sessions.create(
                stored, remember=remember,
            )
            self._send(
                200,
                {"ok": True},
                extra=((
                    "Set-Cookie",
                    self._browser_cookie(browser_token, remember),
                ),),
            )

        def do_POST(self):
            if not self._gate():
                return
            if urlsplit(self.path).path == "/api/login":
                self._password_login()
                return
            if not self._authed():
                self._send(401, {"error": "not signed in"})
                return
            if self.headers.get("Origin") not in origins:
                self._send(403, {"error": "cross-origin request refused"})
                return
            if urlsplit(self.path).path == "/api/logout":
                jar = cookies.SimpleCookie()
                try:
                    jar.load(self.headers.get("Cookie", ""))
                except cookies.CookieError:
                    self._send(400, {"error": "invalid cookie"})
                    return
                current = jar.get(cookie)
                if current is not None and not hmac.compare_digest(current.value, token):
                    browser_sessions.revoke(current.value)
                self._send(
                    200,
                    {"ok": True},
                    extra=((
                        "Set-Cookie",
                        f"{cookie}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0",
                    ),),
                )
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
    if args.demo:
        from . import demo
        return demo.serve(args.port or demo.DEMO_PORT)
    args.port = args.port or DEFAULT_PORT
    token = session_token()
    try:
        machines = hosts.load_hosts(args.hosts)
    except hosts.HostsError as e:
        sys.exit(f"gpu-tuner: {args.hosts}: {e}")
    h = hub.Hub(machines, lambda m: make_link(m, args.socket))
    h.start()
    try:
        httpd = ThreadingHTTPServer((os.environ.get("GPU_TUNER_BIND", "127.0.0.1"), args.port), make_handler(h, token, args.port))
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
    if not args.port:
        if args.demo:
            from .demo import DEMO_PORT
            args.port = DEMO_PORT
        else:
            args.port = DEFAULT_PORT
    try:
        socket.create_connection(("127.0.0.1", args.port), timeout=1).close()
    except OSError:
        if args.demo:
            sys.exit(f"gpu-tuner: nothing is listening on 127.0.0.1:{args.port}. Start the demo with\n"
                     f"    gpu-tuner serve --demo")
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
        p.add_argument("--port", type=int, default=None,
                       help=f"default {DEFAULT_PORT}, or 8766 with --demo")
        p.add_argument("--demo", action="store_true",
                       help="three simulated machines, nothing real is read or changed (serve), "
                            "or sign in to that demo page (open)")
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
