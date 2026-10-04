"""The page's side of every machine: one NodeLink per host (a `gpu-tuner node --stdio` subprocess,
local or over ssh), plus the history the charts draw from.

Nothing here validates a change request. Each machine's own gpu-tunerd does that, behind its
node; the hub only routes a request to the host the page named, and never by GPU UUID alone.
Nothing here trusts a node either: every message goes through proto.sanitize_* first.

History is timestamped with the HUB's clock — each 1 s tick takes every link's latest sample if
it is fresh, else a gap — so a remote machine's clock skew never bends the charts.
"""
from __future__ import annotations

import collections
import subprocess
import sys
import threading
import time

from . import proto

HISTORY_S = 6 * 3600
MAX_POINTS = 900               # a 15 min window is served raw; longer ones are bucket-averaged
WINDOWS = (300, 900, 3600, HISTORY_S)
SERIES = ("temp", "fan", "power", "cap", "clock", "util")
FRESH_S = 2.5                  # a sample older than this is a gap in the charts
STALE_S = 3.0                  # no sample for this long: the host shows as stale
HELLO_TIMEOUT_S = 25.0         # ssh connect + NVML init on the far side
PING_S = 10.0
SILENT_S = 30.0                # nothing at all from the node for this long: drop and reconnect
CALL_TIMEOUT_S = 8.0
BACKOFF_MAX_S = 60.0
PING_ID_BASE = 1_000_000_000   # change requests use 1..1,000,000
BACKOFF_FIX_S = 300.0          # auth / host key / not installed / version: needs a human anyway
PRE_HELLO_JUNK = 50            # lines of shell-startup noise tolerated before the hello
FLOOD_BYTES = 20 * 1024 * 1024  # more than this from one node in FLOOD_WINDOW_S: drop the link
FLOOD_WINDOW_S = 10.0          # (a healthy node sends a few KB a second)
STDERR_KEEP = 2048


class LinkDown(Exception):
    pass


class LinkBusy(Exception):
    pass


class LinkTimeout(Exception):
    pass


def log(msg):
    print(msg, file=sys.stderr, flush=True)


class NodeLink:
    def __init__(self, host, argv, env=None, *, clock=time.monotonic, wallclock=time.time):
        self.host, self.argv, self.env = host, argv, env
        self.clock, self.wallclock = clock, wallclock
        self.lock = threading.Lock()
        self.stop_ev = threading.Event()
        self.write_lock = threading.Lock()      # one change in flight per host
        self.conn, self.error, self.retry_at, self.notes = "connecting", None, None, []
        self.hello = None
        self.static = []
        self.sample, self.sample_at, self.last_seen = None, None, None
        self.daemon, self.daemon_error = None, None
        self.skew_s = self.rtt_ms = None
        self.proc = None
        self.pending = {}
        self.next_id = 1
        self.backoff = 1.0
        self.in_lock = threading.Lock()

    # ── lifecycle ────────────────────────────────────────────────────────────────────────
    def start(self):
        threading.Thread(target=self._run, daemon=True, name=f"link-{self.host['id']}").start()

    def stop(self):
        self.stop_ev.set()
        self._kill()

    def _kill(self):
        p = self.proc
        if p is not None and p.poll() is None:
            try:
                p.kill()
            except OSError:
                pass

    def _run(self):
        while not self.stop_ev.is_set():
            began = self.clock()
            try:
                err, wait = self._session()
            except Exception as e:      # noqa: BLE001 — nothing may end this loop but stop()
                self._kill()
                err, wait = f"internal error in the link: {type(e).__name__}: {e}", None
            if self.stop_ev.is_set():
                return
            if wait is None:                         # ordinary drop: exponential backoff
                if self.clock() - began > 60:
                    self.backoff = 1.0
                wait = self.backoff
                self.backoff = min(BACKOFF_MAX_S, self.backoff * 2)
            with self.lock:
                if self.conn != "incompatible":
                    self.conn = "down"
                self.error = err
                self.retry_at = self.clock() + wait
            self._fail_pending(err or "disconnected")
            self.stop_ev.wait(wait)
            with self.lock:
                if self.conn != "incompatible":
                    self.conn = "connecting"
                self.retry_at = None

    def _session(self):
        """One connection, start to finish. Returns (error text, forced retry delay or None)."""
        try:
            proc = subprocess.Popen(self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, env=self.env, start_new_session=True)
        except OSError as e:
            return f"could not start {self.argv[0]}: {e.strerror}", None
        self.proc = proc
        if self.stop_ev.is_set():        # stop() ran while we were spawning: it couldn't see this child
            self._kill()
        tail = bytearray()

        def drain():
            for chunk in iter(lambda: proc.stderr.read1(4096), b""):
                tail.extend(chunk)
                del tail[:-STDERR_KEEP]
        drainer = threading.Thread(target=drain, daemon=True)
        drainer.start()
        timed_out = threading.Event()

        def hello_timeout():
            timed_out.set()
            self._kill()
        timer = threading.Timer(HELLO_TIMEOUT_S, hello_timeout)
        timer.daemon = True
        timer.start()
        err, forced_wait = None, None
        try:
            hello, junk = None, 0
            while hello is None:
                line = proc.stdout.readline(proto.MAX_LINE + 1)
                if not line:
                    break
                try:
                    msg = proto.decode_line(line)
                except proto.ProtoError:
                    junk += 1
                    if junk > PRE_HELLO_JUNK:
                        err = "the far side printed something other than gpu-tuner's protocol"
                        break
                    continue
                if msg.get("type") == "fatal":
                    err = proto.clean(msg.get("error"), proto.STR) or "the node gave up"
                    break
                if msg.get("type") == "hello":
                    try:
                        hello = proto.sanitize_hello(msg)
                    except proto.ProtoError as e:
                        err = f"the node sent a malformed hello: {e}"
                        break
            timer.cancel()
            if hello is None:
                if timed_out.is_set() and err is None:
                    err = f"no answer from the node within {HELLO_TIMEOUT_S:.0f} s"
            elif hello["proto"] != proto.PROTO:
                err = (f"{self.host['label']} runs gpu-tuner {hello.get('version') or '?'} (protocol "
                       f"{hello['proto']}); this page speaks protocol {proto.PROTO}. Update gpu-tuner "
                       f"on whichever is older.")
                forced_wait = BACKOFF_FIX_S
                with self.lock:          # state and its reason change together: never one without the other
                    self.conn, self.error = "incompatible", err
            else:
                notes = []
                if junk:
                    notes.append(f"ignored {junk} line(s) of non-protocol output before the hello "
                                 f"(a shell startup file printing to stdout?)")
                if self.host.get("ssh") and not hello.get("forced_cmd"):
                    notes.append("reached with an UNRESTRICTED ssh key: restrict it to the node command "
                                 "in authorized_keys (gpu-tuner hosts --init-key prints the line)")
                with self.lock:
                    self.hello, self.static, self.notes = hello, hello["static"], notes
                    self.conn, self.error, self.retry_at = "up", None, None
                log(f"link {self.host['id']}: up · {len(hello['static'])} GPU(s) · gpu-tuner {hello.get('version')}")
                err = self._stream(proc)
        finally:
            timer.cancel()
            # Give a process that closed its stdout a moment to exit by itself, so its real exit
            # status (ssh's 255, a shell's 127) and its last stderr lines can explain the drop.
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._kill()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
            drainer.join(timeout=2)
            for f in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    f.close()
                except (OSError, ValueError):
                    pass
        if forced_wait is not None:
            return err, forced_wait
        return self._classify(proc, bytes(tail), err)

    def _stream(self, proc):
        last_rx = [self.clock()]
        sent = {}
        with self.lock:
            announced = {g["uuid"] for g in self.static}
        window_start, window_bytes = self.clock(), 0

        def pinger():
            n = 0
            while proc.poll() is None and not self.stop_ev.wait(PING_S):
                if self.clock() - last_rx[0] > SILENT_S:
                    log(f"link {self.host['id']}: silent for {SILENT_S:.0f} s, reconnecting")
                    self._kill()
                    return
                n += 1
                pid = PING_ID_BASE + n % 1000000         # pings never share an id with a change request
                if len(sent) > 16:
                    sent.clear()                          # a node that never pongs can't grow this
                sent[pid] = self.clock()
                try:
                    self._send({"id": pid, "op": "ping"})
                except LinkDown:
                    return
        threading.Thread(target=pinger, daemon=True).start()
        while True:
            line = proc.stdout.readline(proto.MAX_LINE + 1)
            if not line:
                return None
            last_rx[0] = self.clock()
            window_bytes += len(line)
            if last_rx[0] - window_start > FLOOD_WINDOW_S:
                window_start, window_bytes = last_rx[0], len(line)
            elif window_bytes > FLOOD_BYTES:
                return f"the node sent over {FLOOD_BYTES >> 20} MB in {FLOOD_WINDOW_S:.0f} s; dropped it"
            try:
                msg = proto.decode_line(line)
            except proto.ProtoError as e:
                return f"protocol error from the node: {e}"
            kind = msg.get("type")
            if kind == "sample":
                s = proto.sanitize_sample(msg)
                # only the GPUs its hello announced: a node can't grow the page's state with new UUIDs
                s["gpus"] = {u: g for u, g in s["gpus"].items() if u in announced}
                with self.lock:
                    self.sample, self.sample_at, self.last_seen = s, self.clock(), self.wallclock()
                    if s["t"] is not None:
                        self.skew_s = round(s["t"] - self.wallclock(), 1)
            elif kind == "daemon":
                d = proto.sanitize_daemon(msg)
                with self.lock:
                    self.daemon, self.daemon_error = d["status"], d["error"]
            elif kind == "reply":
                r = proto.sanitize_reply(msg)
                slot = self.pending.get(r["id"])
                if slot is not None:
                    slot[1] = r
                    slot[0].set()
            elif kind == "pong":
                at = sent.pop(proto.clean(msg.get("id"), proto.INT), None)
                if at is not None:
                    with self.lock:
                        self.rtt_ms = round((self.clock() - at) * 1000)
            elif kind == "fatal":
                return proto.clean(msg.get("error"), proto.STR) or "the node gave up"

    def _classify(self, proc, tail, err):
        """Turn an ended session into (message, forced retry delay or None)."""
        rc = proc.poll()
        text = tail.decode("utf-8", "replace").strip()
        last = text.splitlines()[-1][:300] if text else ""
        dest = self.host.get("ssh")
        if err:
            return err, None
        if rc == 78:                    # server.make_link's stand-in when the page has no ssh key yet
            return last, BACKOFF_FIX_S
        if dest and rc == 255:
            if "Permission denied" in text:
                return (f"ssh refused the login to {dest}. Add this page's key to authorized_keys there "
                        f"(gpu-tuner hosts --init-key prints the line)"), BACKOFF_FIX_S
            if "Host key verification failed" in text or "IDENTIFICATION HAS CHANGED" in text:
                return (f"{dest}'s host key is unknown or has changed. Check it and pin it by connecting "
                        f"once by hand: ssh {dest} true"), BACKOFF_FIX_S
            return f"ssh to {dest} failed: {last or 'exit 255'}", None
        if rc == 127 or (dest and "No such file or directory" in text):
            return (f"gpu-tuner isn't installed on {self.host['label']} (expected "
                    f"/usr/local/lib/gpu-tuner): run its install.sh there (--no-ui, or --node)"), BACKOFF_FIX_S
        if rc not in (0, None, -9):
            return f"the node exited ({rc}){': ' + last if last else ''}", None
        return last or "the connection closed", None

    # ── talking to the node ──────────────────────────────────────────────────────────────
    def _send(self, obj):
        p = self.proc
        if p is None or p.poll() is not None:
            raise LinkDown("not connected")
        data = proto.encode(obj)
        with self.in_lock:
            try:
                p.stdin.write(data)
                p.stdin.flush()
            except (OSError, ValueError):
                self._kill()
                raise LinkDown("the connection dropped") from None

    def _fail_pending(self, why):
        for slot in list(self.pending.values()):
            slot[1] = {"id": None, "error": why, "down": True}
            slot[0].set()

    def call(self, req, timeout=CALL_TIMEOUT_S):
        """Send one change request; the node's reply. Raises LinkDown / LinkBusy / LinkTimeout."""
        with self.lock:
            if self.conn != "up":
                raise LinkDown(self.error or self.conn)
        if not self.write_lock.acquire(blocking=False):
            raise LinkBusy()
        try:
            with self.lock:
                rid = self.next_id
                self.next_id = self.next_id % 1000000 + 1
            slot = [threading.Event(), None]
            self.pending[rid] = slot
            try:
                self._send({"id": rid, "op": "apply", "req": req})
                if not slot[0].wait(timeout):
                    raise LinkTimeout()
            finally:
                self.pending.pop(rid, None)
            r = slot[1]
            if r.get("down"):
                raise LinkDown(r["error"])
            if "resp" in r and r["resp"].get("ok"):
                with self.lock:
                    self.daemon = r["resp"]            # the page shows the new state at once
            return r
        finally:
            self.write_lock.release()

    # ── what the page sees ───────────────────────────────────────────────────────────────
    def fresh_sample(self, now):
        with self.lock:
            if self.sample is None or self.sample_at is None or now - self.sample_at > FRESH_S:
                return None
            return self.sample

    def snapshot(self, now):
        with self.lock:
            conn = self.conn
            if conn == "up" and (self.sample_at is None or now - self.sample_at > STALE_S):
                conn = "stale"
            h = self.hello or {}
            return {
                "conn": conn, "error": self.error,
                "retry_in_s": None if self.retry_at is None else max(0, round(self.retry_at - now)),
                "last_seen": self.last_seen,
                "notes": list(self.notes),
                "node": None if not h else {**(h.get("host") or {}), "version": h.get("version"),
                                            "forced_cmd": h.get("forced_cmd"), "skew_s": self.skew_s,
                                            "rtt_ms": self.rtt_ms},
                "static": self.static,
                "live": {"gpus": (self.sample or {}).get("gpus") or {},
                         "age_s": None if self.sample_at is None else round(now - self.sample_at, 1)},
                "daemon": self.daemon, "daemon_error": self.daemon_error}


class Hub:
    def __init__(self, hosts, link_factory, clock=time.monotonic, wallclock=time.time):
        self.hosts = hosts
        self.clock, self.wallclock = clock, wallclock
        self.links = {h["id"]: link_factory(h) for h in hosts}
        self.hlock = threading.Lock()
        self.history = {h["id"]: collections.deque(maxlen=HISTORY_S) for h in hosts}
        self._pos = {}

    def start(self):
        for link in self.links.values():
            link.start()
        threading.Thread(target=self._ticker, daemon=True, name="hub-tick").start()

    def stop(self):
        for link in self.links.values():
            link.stop()

    def _ticker(self):
        nxt = self.clock()
        while True:
            try:
                self.tick()
            except Exception as e:      # noqa: BLE001 — one bad tick must not end the charts
                log(f"hub tick: {e}")
            nxt += 1.0
            time.sleep(max(0.0, nxt - self.clock()))

    def tick(self):
        """One history row per host: (hub time, uuid layout, flat values) — the layout tuple is
        shared between rows, so six hours of a 4-GPU host stays a few MB."""
        now, t = self.clock(), self.wallclock()
        with self.hlock:
            for hid, link in self.links.items():
                s = link.fresh_sample(now)
                if s is None:
                    self.history[hid].append((t, (), ()))
                    continue
                layout = tuple(s["gpus"])
                if len(self._pos) > 64:             # a node inventing new UUIDs every second can't grow this
                    self._pos.clear()
                layout = self._pos.setdefault(layout, layout)
                vals = []
                for u in layout:
                    g = s["gpus"][u] or {}
                    fans = [f for f in (g.get("fans") or []) if f is not None]
                    vals += [g.get("temp"), max(fans) if fans else None, g.get("power_w"),
                             g.get("power_limit_w"), g.get("clock_mhz"), g.get("util")]
                self.history[hid].append((t, layout, tuple(vals)))

    def history_json(self, hid, window):
        link = self.links[hid]
        with link.lock:
            uuids = [g["uuid"] for g in link.static]
        with self.hlock:
            rows = list(self.history[hid])
        cutoff = self.wallclock() - window
        rows = [r for r in rows if r[0] >= cutoff]
        step = max(1, -(-len(rows) // MAX_POINTS))
        out = {"host": hid, "window": window, "bucket_s": step, "t": [],
               "gpus": {u: {k: [] for k in SERIES} for u in uuids}}
        positions = {}                      # layout tuple -> {uuid: index}, for this request only

        def pos_of(layout, u):
            if layout not in positions:
                positions[layout] = {x: i for i, x in enumerate(layout)}
            return positions[layout].get(u)
        for i in range(0, len(rows), step):
            chunk = rows[i:i + step]
            out["t"].append(round(chunk[-1][0], 1))
            for u in uuids:
                for k, key in enumerate(SERIES):
                    vals = []
                    for _t, layout, flat in chunk:
                        pos = pos_of(layout, u) if layout else None
                        if pos is not None and flat[pos * 6 + k] is not None:
                            vals.append(flat[pos * 6 + k])
                    out["gpus"][u][key].append(round(sum(vals) / len(vals), 1) if vals else None)
        return out

    def state_json(self):
        now = self.clock()
        hosts, owner, warnings = [], {}, []
        for h in self.hosts:
            snap = self.links[h["id"]].snapshot(now)
            for g in snap["static"]:
                if g["uuid"] in owner:
                    warnings.append(f"GPU {g['uuid'][:16]}… is reported by both {owner[g['uuid']]} and "
                                    f"{h['label']}: is one machine listed twice in hosts.json?")
                owner.setdefault(g["uuid"], h["label"])
            hosts.append({"id": h["id"], "label": h["label"], "remote": bool(h.get("ssh")),
                          "wall": h.get("wall"), **snap})
        return {"hosts": hosts, "warnings": warnings}

    def apply(self, hid, req):
        """(http status, body) for one change request to one named host."""
        link = self.links.get(hid)
        if link is None:
            return 400, {"ok": False, "error": "no such machine"}
        label = link.host["label"]
        try:
            proto.encode(req)                   # a value JSON can't carry (inf) is the request's fault
        except ValueError as e:
            return 400, {"ok": False, "error": f"bad request: {e}"}
        try:
            r = link.call(req)
        except LinkDown as e:
            code, body = 503, {"ok": False, "error": f"{label} is not connected ({e})"}
        except LinkBusy:
            code, body = 409, {"ok": False, "error": f"another change to {label} is still being "
                                                     f"applied; try again in a moment"}
        except LinkTimeout:
            code, body = 504, {"ok": False, "error": f"no answer from {label} within {CALL_TIMEOUT_S:.0f} s: "
                                                     f"the change may or may not have applied. The page "
                                                     f"will show what is in force once {label} answers."}
        else:
            if "resp" in r:
                body = r["resp"]
                code = 200 if body.get("ok") else 400
            else:
                code, body = 502, {"ok": False, "error": r.get("error") or "no reply"}
        detail = {k: req.get(k) for k in ("op", "uuid", "watts", "mode", "mhz") if k in req}
        log(f"apply host={hid} {detail} -> {'ok' if body.get('ok') else 'REJECTED: ' + str(body.get('error'))}")
        return code, body
