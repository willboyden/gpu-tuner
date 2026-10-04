#!/usr/bin/env python3
"""Offline tests for gpu-tuner across machines: the wire protocol, hosts.json, the node, the hub's
links, and the page API's routing.

No GPU, no ssh, no root: "remote machines" are tests/fake_node.py subprocesses running the real
node code over fake NVML (and, where a test needs one, a real daemon on a scratch socket). Every
test asserts a specific accept/reject or value, so each one can fail.
"""
import http.client
import json
import os
import socket
import stat
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import fakes  # noqa: E402
from fakes import RTX5090  # noqa: E402
from gpu_tuner import hosts, hub, node, proto, server  # noqa: E402
from gpu_tuner.nvml import Nvml  # noqa: E402

hub.log = lambda msg: None
FAKE = os.path.join(HERE, "fake_node.py")


def fake_argv(*extra):
    return [sys.executable, FAKE, *extra]


def wait_for(fn, timeout=10.0, what="condition"):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def local(hid="x", label=None):
    return {"id": hid, "label": label or hid.upper(), "ssh": None, "port": None, "wall": None}


def started(link):
    link.start()
    return link


# ── protocol ─────────────────────────────────────────────────────────────────────────────────
class Proto(unittest.TestCase):
    def test_non_finite_numbers_rejected(self):
        for bad in (b'{"a": NaN}', b'{"a": Infinity}', b'{"a": [-Infinity]}', b'{"a": 1e999}', b'{"a": -1e400}'):
            with self.assertRaises(proto.ProtoError):
                proto.decode_line(bad)
        with self.assertRaises(ValueError):
            proto.encode({"a": float("nan")})

    def test_size_shape_nesting_and_huge_ints_rejected(self):
        for bad in (b'{"a":"' + b"x" * proto.MAX_LINE + b'"}', b"[1, 2]", b'"x"', b"not json",
                    b"[" * 100000 + b"]" * 100000, b'{"a": ' + b"9" * 100000 + b"}"):
            with self.assertRaises(proto.ProtoError):
                proto.decode_line(bad)
        self.assertEqual(proto.decode_line(b'{"type": "pong", "id": 3}\n'), {"type": "pong", "id": 3})

    def test_clean_whitelists_and_caps(self):
        spec = {"n": proto.NUM, "i": proto.INT, "s": proto.STR, "b": proto.BOOL, "l": ("list", proto.NUM, 3)}
        out = proto.clean({"n": True, "i": 2 ** 60, "s": "x" * 1000, "b": 1, "l": [1, "2", 3e300, 4], "evil": "<b>"}, spec)
        self.assertEqual(out, {"n": None, "i": None, "s": "x" * proto.MAX_STR, "b": None, "l": [1, None, None]})
        self.assertEqual(proto.clean(1e15, proto.NUM), 1e15)
        self.assertIsNone(proto.clean("not a dict", spec))

    def test_hello_needs_a_version_and_drops_gpus_without_uuid(self):
        with self.assertRaises(proto.ProtoError):
            proto.sanitize_hello({"type": "hello"})
        h = proto.sanitize_hello({"proto": 1, "static": [{"uuid": "GPU-a", "name": "A"}, {"name": "no uuid"},
                                                         {"uuid": ""}, {"uuid": "G" * 97}]
                                  + [{"uuid": f"GPU-{i}"} for i in range(40)]})
        self.assertNotIn("G" * 97, [g["uuid"] for g in h["static"]])
        self.assertEqual(h["static"][0]["uuid"], "GPU-a")
        self.assertLessEqual(len(h["static"]), proto.MAX_GPUS)
        self.assertNotIn("no uuid", [g.get("name") for g in h["static"]])

    def test_sample_caps(self):
        s = proto.sanitize_sample({"seq": 1, "t": 5.0, "gpus": {f"GPU-{i}": {"temp": "hot", "fans": [40, "x"],
                                   "procs": [{"pid": 1, "name": "p", "cmdline": "secret"}] * 20} for i in range(40)}})
        self.assertEqual(len(s["gpus"]), proto.MAX_GPUS)
        g = s["gpus"]["GPU-0"]
        self.assertEqual((g["temp"], g["fans"], len(g["procs"])), (None, [40, None], 6))
        self.assertNotIn("cmdline", g["procs"][0])

    def test_replies(self):
        ok = proto.sanitize_reply({"id": 4, "resp": {"ok": True, "gpus": [{"uuid": "GPU-a", "evil": 1}], "evil": 2}})
        self.assertTrue(ok["resp"]["ok"])
        self.assertNotIn("evil", ok["resp"])
        self.assertNotIn("evil", ok["resp"]["gpus"][0])
        bad = proto.sanitize_reply({"id": 5, "resp": {"ok": False, "error": "nope", "over_budget": True, "total_w": 900}})
        self.assertEqual((bad["resp"]["ok"], bad["resp"]["error"], bad["resp"]["total_w"]), (False, "nope", 900))
        self.assertEqual(proto.sanitize_reply({"id": 6, "resp": {"ok": False}})["resp"]["error"],
                         "the daemon refused without saying why")
        self.assertEqual(proto.sanitize_reply({"id": 7})["error"], "malformed reply")


# ── hosts.json ───────────────────────────────────────────────────────────────────────────────
EXAMPLE = {"hosts": [
    {"id": "local", "label": "Workstation",
     "wall": {"non_gpu_dc_w": 630, "psu_efficiency": 0.9, "circuits": {"15 A": 1440, "20 A": 1920}}},
    {"id": "gpu-box-2", "label": "GPU box 2", "ssh": "gpu-box-2"},
    {"id": "edge-1", "ssh": "me@10.0.0.21", "port": 2222},
]}


class Hosts(unittest.TestCase):
    def test_example_parses(self):
        hs = hosts.parse_hosts(EXAMPLE)
        self.assertEqual([h["id"] for h in hs], ["local", "gpu-box-2", "edge-1"])
        self.assertIsNone(hs[0]["ssh"])
        self.assertEqual((hs[2]["ssh"], hs[2]["port"], hs[2]["label"]), ("me@10.0.0.21", 2222, "edge-1"))
        self.assertEqual(hs[0]["wall"]["circuits"]["15 A"], 1440)

    def test_ssh_destinations_that_could_inject_are_refused(self):
        for dest in ("-oProxyCommand=touch /tmp/x", "-p", "a b", "x;rm -rf ~", "a\nb", "host%h", "spärk",
                     "a@b@c", "", "@host", "user@", "`id`", "$(id)", "h'x", 'h"x', "a/b"):
            with self.assertRaises(hosts.HostsError, msg=repr(dest)):
                hosts.parse_hosts({"hosts": [{"id": "r", "ssh": dest}]})

    def test_structure_rules(self):
        bad = [
            {"hosts": []}, {"hosts": [{"id": "a", "ssh": "a"}], "extra": 1}, [],
            {"hosts": [{"id": "A", "ssh": "a"}]}, {"hosts": [{"id": "-a", "ssh": "a"}]},
            {"hosts": [{"id": "a", "ssh": "a"}, {"id": "a", "ssh": "b"}]},
            {"hosts": [{"id": "local", "ssh": "a"}]}, {"hosts": [{"id": "r"}]},
            {"hosts": [{"id": "r", "ssh": "a", "cmd": "x"}]},
            {"hosts": [{"id": "r", "ssh": "a", "port": True}]}, {"hosts": [{"id": "r", "ssh": "a", "port": 70000}]},
            {"hosts": [{"id": "r", "ssh": "a", "label": "x" * 65}]}, {"hosts": [{"id": "r", "ssh": "a", "label": "a\x1bb"}]},
            {"hosts": [{"id": "local", "wall": {"non_gpu_dc_w": 600, "psu_efficiency": 0.3}}]},
            {"hosts": [{"id": "local", "wall": {"non_gpu_dc_w": 600}}]},
            {"hosts": [{"id": "local", "wall": {"non_gpu_dc_w": 600, "psu_efficiency": 0.9, "x": 1}}]},
            {"hosts": [{"id": "local", "wall": {"non_gpu_dc_w": 600, "psu_efficiency": 0.9,
                                                "circuits": {str(i): 1000 for i in range(5)}}}]},
            {"hosts": [{"id": f"h{i}", "ssh": "a"} for i in range(33)]},
        ]
        for doc in bad:
            with self.assertRaises(hosts.HostsError, msg=json.dumps(doc)[:80]):
                hosts.parse_hosts(doc)

    def test_load_file_rules(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "hosts.json")
        self.assertEqual([h["id"] for h in hosts.load_hosts(path)], ["local"])      # no file: just this machine
        with open(path, "w") as f:
            json.dump(EXAMPLE, f)
        os.chmod(path, 0o600)
        self.assertEqual(len(hosts.load_hosts(path)), 3)
        os.chmod(path, 0o666)
        with self.assertRaises(hosts.HostsError):
            hosts.load_hosts(path)
        os.chmod(path, 0o600)
        link = os.path.join(tmp, "link.json")
        os.symlink(path, link)
        with self.assertRaises(hosts.HostsError):
            hosts.load_hosts(link)
        with open(path, "w") as f:
            f.write('{"hosts": [{"id": "local", "wall": {"non_gpu_dc_w": NaN, "psu_efficiency": 0.9}}]}')
        with self.assertRaises(hosts.HostsError):
            hosts.load_hosts(path)

    def test_ssh_argv_is_exact(self):
        h = {"id": "r", "label": "R", "ssh": "me@box", "port": None, "wall": None}
        with self.assertRaises(hosts.NoHubKey):        # never a fallback to your personal keys or agent
            hosts.ssh_argv(h, key_file=os.path.join(tempfile.mkdtemp(), "absent"))
        key = os.path.join(tempfile.mkdtemp(), "k")
        open(key, "w").close()
        argv = hosts.ssh_argv(h, key_file=key)
        self.assertEqual(argv, [
            "ssh", "-T", "-e", "none", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
            "-o", "UpdateHostKeys=no", "-o", "VerifyHostKeyDNS=no", "-o", "IdentityAgent=none",
            "-o", "PKCS11Provider=none", "-o", "PasswordAuthentication=no", "-o", "KbdInteractiveAuthentication=no",
            "-o", "ControlMaster=no", "-o", "ControlPath=none",
            "-o", "ForwardAgent=no", "-o", "ForwardX11=no", "-o", "ClearAllForwardings=yes",
            "-o", "PermitLocalCommand=no", "-o", "ConnectTimeout=5", "-o", "ServerAliveInterval=5",
            "-o", "ServerAliveCountMax=3", "-i", key, "-o", "IdentitiesOnly=yes",
            "--", "me@box", "/usr/local/lib/gpu-tuner/gpu-tuner node --stdio"])
        argv = hosts.ssh_argv(dict(h, port=2222), key_file=key)
        self.assertEqual(argv[-9:], ["-i", key, "-o", "IdentitiesOnly=yes", "-p", "2222", "--", "me@box", hosts.REMOTE_CMD])
        old = hosts.KEY_FILE
        hosts.KEY_FILE = os.path.join(tempfile.mkdtemp(), "absent")
        try:                                            # no key: the machine's tab says why, no ssh is run
            lk = server.make_link(h, None)
        finally:
            hosts.KEY_FILE = old
        self.assertNotEqual(lk.argv[0], "ssh")
        lk.start()
        self.addCleanup(lk.stop)
        wait_for(lambda: lk.snapshot(time.monotonic())["error"], what="the no-key error")
        s = lk.snapshot(time.monotonic())
        self.assertIn("--init-key", s["error"])
        wait_for(lambda: lk.snapshot(time.monotonic())["retry_in_s"] is not None, what="retry scheduled")
        self.assertGreater(lk.snapshot(time.monotonic())["retry_in_s"], 200)
        self.assertEqual(argv[argv.index("-i") + 1], key)
        self.assertEqual(argv[argv.index("-p") + 1], "2222")
        self.assertEqual(argv[-3:], ["--", "me@box", hosts.REMOTE_CMD])
        self.assertLess(argv.index("-p"), argv.index("--"))

    def test_ssh_env_is_scrubbed(self):
        old = os.environ.get("SSH_AUTH_SOCK")
        os.environ["SSH_AUTH_SOCK"] = "/tmp/agent.sock"
        try:
            env = hosts.ssh_env()
        finally:
            if old is None:
                del os.environ["SSH_AUTH_SOCK"]
            else:
                os.environ["SSH_AUTH_SOCK"] = old
        self.assertNotIn("SSH_AUTH_SOCK", env)
        self.assertLessEqual(set(env), {"HOME", "PATH", "LANG", "USER"})

    def test_restricted_key_line(self):
        pub = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleExampleExampleExampleExample0001 me@laptop\n"
        line = hosts.authorized_keys_line(pub, "10.0.0.5")
        self.assertEqual(line, 'restrict,from="10.0.0.5",command="/usr/local/lib/gpu-tuner/gpu-tuner node --stdio" '
                               "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleExampleExampleExampleExample0001 gpu-tuner-hub")
        self.assertTrue(hosts.authorized_keys_line(pub).startswith('restrict,command="'))
        for bad_from in ('10.0.0.5" ,command="x', "a b"):
            with self.assertRaises(hosts.HostsError):
                hosts.authorized_keys_line(pub, bad_from)
        for bad_pub in ("not a key", 'ssh-ed25519 AAAA"x', "ssh-rsa", 'ssh-ed25519"x AAAA', "ssh- AAAA"):
            with self.assertRaises(hosts.HostsError):
                hosts.authorized_keys_line(bad_pub)

    def test_wall_estimate(self):
        wall = {"non_gpu_dc_w": 630, "psu_efficiency": 0.9}
        self.assertEqual(hosts.wall_estimate_w(925, wall), 1728)     # the original workstation's README arithmetic
        self.assertEqual(hosts.wall_estimate_w(750, wall), 1533)
        self.assertIsNone(hosts.wall_estimate_w(925, None))


# ── the node, in-process ─────────────────────────────────────────────────────────────────────
class FakeDaemon:
    """A daemon socket that records what reaches it and answers ok."""

    def __init__(self):
        self.path = os.path.join(tempfile.mkdtemp(), "d.sock")
        self.got = []
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(self.path)
        self.srv.listen(4)
        threading.Thread(target=self.loop, daemon=True).start()

    def loop(self):
        while True:
            conn, _ = self.srv.accept()
            with conn:
                req = json.loads(conn.makefile("rb").readline())
                self.got.append(req)
                resp = {"ok": True, "gpus": [], "budget_w": 1, "budget_used_w": 0} if req["op"] != "status" \
                    else {"ok": False, "error": "status not wanted in this test"}
                conn.sendall(json.dumps(resp).encode() + b"\n")


class NodeUnit(unittest.TestCase):
    def run_node(self, sock=None, out=None):
        r_in, w_in = os.pipe()
        r_out, w_out = os.pipe()
        inp, writer = os.fdopen(r_in, "rb"), os.fdopen(w_in, "wb")
        sink = out or os.fdopen(w_out, "wb")
        reader = os.fdopen(r_out, "rb")
        nv = Nvml(lib=fakes.FakeLib())
        n = node.Node(node.Collector(nv, sock or os.path.join(tempfile.mkdtemp(), "absent")), inp, sink)
        result = {}
        t = threading.Thread(target=lambda: result.setdefault("rc", n.run()), daemon=True)
        t.start()

        def cleanup():
            n.done.set()
            t.join(3)
            for f in (writer, reader, inp, sink):
                try:
                    f.close()
                except (OSError, ValueError, AttributeError):
                    pass
        self.addCleanup(cleanup)
        return n, writer, reader, t, result

    def read_until(self, reader, kind, limit=50):
        for _ in range(limit):
            m = proto.decode_line(reader.readline())
            if m["type"] == kind:
                return m
        raise AssertionError(f"no {kind} message")

    def test_hello_first_then_samples_and_a_preview(self):
        _n, _w, r, _t, _res = self.run_node()
        first = proto.decode_line(r.readline())
        self.assertEqual((first["type"], first["proto"], len(first["static"])), ("hello", 1, 2))
        self.assertIn("forced_cmd", first)
        d = self.read_until(r, "daemon")
        self.assertEqual(d["error"], "not installed")
        self.assertTrue(d["status"]["preview"])
        self.assertIsNone(d["status"]["budget_w"])
        s = self.read_until(r, "sample")
        p = s["gpus"][fakes.WS]["procs"][0]
        self.assertEqual((p["pid"], p["vram_mib"], sorted(p)), (1, 8 << 10, ["name", "pid", "vram_mib"]))

    def test_ping_unknown_ops_and_bad_ids(self):
        _n, w, r, _t, _res = self.run_node()
        for msg in ({"id": "1", "op": "ping"}, {"id": True, "op": "ping"}, {"id": 2, "op": "shell", "cmd": "id"},
                    {"id": 3, "op": "ping"}):
            w.write(proto.encode(msg))
        w.flush()
        reply = self.read_until(r, "reply")
        self.assertEqual((reply["id"], reply["error"]), (2, "unknown op"))
        self.assertEqual(self.read_until(r, "pong")["id"], 3)

    def test_only_whitelisted_ops_and_fields_reach_the_daemon(self):
        fd = FakeDaemon()
        _n, w, r, _t, _res = self.run_node(sock=fd.path)
        w.write(proto.encode({"id": 1, "op": "apply", "req": {"op": "status"}}))
        w.write(proto.encode({"id": 2, "op": "apply", "req": {"op": "set_power", "uuid": "GPU-a", "watts": 300,
                                                               "socket": "/etc/shadow", "evil": 1}}))
        w.flush()
        r1, r2 = self.read_until(r, "reply"), self.read_until(r, "reply")
        self.assertEqual(r1["resp"], {"ok": False, "error": "unknown op"})
        self.assertTrue(r2["resp"]["ok"])
        sent = [g for g in fd.got if g["op"] != "status"]
        self.assertEqual(sent, [{"op": "set_power", "uuid": "GPU-a", "watts": 300}])

    def test_exits_on_eof(self):
        _n, w, _r, t, res = self.run_node()
        w.close()
        t.join(3)
        self.assertEqual(res.get("rc"), 0)

    def test_exits_when_the_hub_goes_silent(self):
        old = node.HUB_SILENCE_S
        node.HUB_SILENCE_S = 0.5
        try:
            _n, _w, _r, t, res = self.run_node()
            t.join(4)
            self.assertEqual(res.get("rc"), 0)
        finally:
            node.HUB_SILENCE_S = old

    def test_exits_on_a_broken_pipe(self):
        class Broken:
            def write(self, _):
                raise BrokenPipeError()

            def flush(self):
                pass
        _n, _w, _r, t, res = self.run_node(out=Broken())
        t.join(3)
        self.assertEqual(res.get("rc"), 0)


# ── links to (fake) nodes ────────────────────────────────────────────────────────────────────
class Links(unittest.TestCase):
    def link(self, *extra, host=None):
        lk = hub.NodeLink(host or local(), fake_argv(*extra))
        self.addCleanup(lk.stop)
        return started(lk)

    def daemon_args(self, kind="5090", uuid=None, budget="575"):
        tmp = tempfile.mkdtemp()
        args = ["--kind", kind, "--daemon-sock", os.path.join(tmp, "d.sock"), "--state", os.path.join(tmp, "s.json"),
                "--budget", budget, "--record", os.path.join(tmp, "record.jsonl")]
        if uuid:
            args += ["--uuid", uuid]
        return args, os.path.join(tmp, "record.jsonl")

    def snap(self, lk):
        return lk.snapshot(time.monotonic())

    def test_monitor_only_node_comes_up(self):
        lk = self.link("--kind", "gb10")
        wait_for(lambda: self.snap(lk)["daemon"] is not None, what="daemon status")
        s = self.snap(lk)
        self.assertEqual((s["conn"], s["daemon_error"], s["static"][0]["name"]), ("up", "not installed", "NVIDIA GB10"))
        self.assertEqual(s["static"][0]["mem_kind"], "unified")
        self.assertFalse(s["daemon"]["power_settable"])
        wait_for(lambda: self.snap(lk)["live"]["gpus"], what="a sample")
        self.assertEqual(self.snap(lk)["live"]["gpus"][fakes.GB10]["mem_kind"], "unified")

    def test_change_is_routed_validated_and_read_back(self):
        args, record = self.daemon_args()
        lk = self.link(*args)
        wait_for(lambda: self.snap(lk)["daemon"] and not self.snap(lk)["daemon"].get("preview"), what="daemon")
        r = lk.call({"op": "set_power", "uuid": RTX5090, "watts": 550})
        self.assertTrue(r["resp"]["ok"], r)
        self.assertEqual(r["resp"]["gpus"][0]["settings"]["power_w"], 550)
        self.assertEqual(self.snap(lk)["daemon"]["gpus"][0]["settings"]["power_w"], 550)   # shown at once
        r = lk.call({"op": "set_power", "uuid": RTX5090, "watts": 590})                    # no profile: lower only
        self.assertFalse(r["resp"]["ok"])
        self.assertIn("400-575 W", r["resp"]["error"])
        with open(record) as f:
            self.assertEqual([json.loads(x)["watts"] for x in f], [550, 590])

    def test_reconnects_after_the_node_dies(self):
        lk = self.link("--kind", "5090")
        wait_for(lambda: self.snap(lk)["conn"] == "up", what="up")
        first = lk.proc
        first.kill()
        wait_for(lambda: self.snap(lk)["conn"] in ("down", "connecting"), what="down")
        wait_for(lambda: self.snap(lk)["conn"] == "up" and lk.proc is not first, timeout=10, what="back up")
        self.assertIsNotNone(self.snap(lk)["static"])

    def test_goes_stale_when_samples_stop(self):
        lk = self.link("--script", "stop-sampling")
        wait_for(lambda: self.snap(lk)["conn"] == "up", what="up")
        wait_for(lambda: self.snap(lk)["conn"] == "stale", timeout=10, what="stale")

    def test_timeout_busy_and_down(self):
        lk = self.link("--script", "no-reply")
        wait_for(lambda: self.snap(lk)["conn"] == "up", what="up")
        req = {"op": "set_power", "uuid": RTX5090, "watts": 500}
        errs = []
        t = threading.Thread(target=lambda: errs.append(self._raises(lk.call, req, 1.5)))
        t.start()
        time.sleep(0.3)
        self.assertIs(self._raises(lk.call, req, 1.0), hub.LinkBusy)        # one change in flight per host
        t.join(5)
        self.assertEqual(errs, [hub.LinkTimeout])
        lk2 = self.link("--script", "exit-on-apply")
        wait_for(lambda: self.snap(lk2)["conn"] == "up", what="up")
        self.assertIs(self._raises(lk2.call, req, 5.0), hub.LinkDown)

    @staticmethod
    def _raises(fn, req, timeout):
        try:
            fn(req, timeout)
        except (hub.LinkBusy, hub.LinkTimeout, hub.LinkDown) as e:
            return type(e)
        return None

    def test_incompatible_node_is_not_retried_in_a_storm(self):
        lk = self.link("--script", "proto99")
        wait_for(lambda: self.snap(lk)["conn"] == "incompatible", what="incompatible")
        self.assertIn("protocol 99", self.snap(lk)["error"])       # never the state without its reason
        wait_for(lambda: self.snap(lk)["retry_in_s"] is not None, what="retry scheduled")
        s = self.snap(lk)
        self.assertIn("protocol 99", s["error"])
        self.assertGreater(s["retry_in_s"], 200)

    def test_malformed_hello_retries_instead_of_killing_the_link(self):
        lk = self.link("--script", "bad-hello")
        wait_for(lambda: "malformed hello" in (self.snap(lk)["error"] or ""), what="the hello error")
        wait_for(lambda: self.snap(lk)["retry_in_s"] is not None, what="a retry scheduled")
        first = lk.proc
        wait_for(lambda: lk.proc is not first, timeout=6, what="a second attempt")   # the link thread lives

    def test_samples_for_unannounced_gpus_are_dropped(self):
        lk = self.link("--kind", "5090", "--script", "extra-uuid")
        wait_for(lambda: self.snap(lk)["live"]["gpus"], what="a sample")
        time.sleep(1.5)
        self.assertEqual(list(self.snap(lk)["live"]["gpus"]), [RTX5090])

    def test_fatal_and_garbage_and_junk(self):
        lk = self.link("--script", "fatal")
        wait_for(lambda: self.snap(lk)["error"], what="an error")
        self.assertIn("NVML unavailable", self.snap(lk)["error"])
        lk = self.link("--script", "garbage")
        wait_for(lambda: (self.snap(lk)["error"] or "").startswith("protocol error"), what="protocol error")
        lk = self.link("--script", "junk")
        wait_for(lambda: self.snap(lk)["conn"] == "up", what="up")
        self.assertTrue(any("non-protocol output" in n for n in self.snap(lk)["notes"]))

    def test_unrestricted_key_is_flagged_for_ssh_hosts_only(self):
        lk = self.link("--kind", "gb10", host={"id": "r", "label": "R", "ssh": "box", "port": None, "wall": None})
        wait_for(lambda: self.snap(lk)["conn"] == "up", what="up")
        self.assertTrue(any("UNRESTRICTED" in n for n in self.snap(lk)["notes"]))
        lk = self.link("--kind", "gb10")
        wait_for(lambda: self.snap(lk)["conn"] == "up", what="up")
        self.assertEqual(self.snap(lk)["notes"], [])

    def test_ssh_failures_are_explained(self):
        class P:
            def __init__(self, rc):
                self.rc = rc

            def poll(self):
                return self.rc
        lk = hub.NodeLink({"id": "r", "label": "R", "ssh": "box", "port": None, "wall": None}, ["true"])
        msg, wait = lk._classify(P(255), b"me@box: Permission denied (publickey).", None)
        self.assertIn("--init-key", msg)
        self.assertEqual(wait, hub.BACKOFF_FIX_S)
        msg, wait = lk._classify(P(255), b"Host key verification failed.", None)
        self.assertIn("ssh box true", msg)
        msg, wait = lk._classify(P(127), b"bash: /usr/local/lib/gpu-tuner/gpu-tuner: No such file or directory", None)
        self.assertIn("install.sh", msg)
        msg, wait = lk._classify(P(255), b"ssh: connect to host box port 22: No route to host", None)
        self.assertEqual((msg, wait), ("ssh to box failed: ssh: connect to host box port 22: No route to host", None))


# ── the hub: routing, history, warnings ──────────────────────────────────────────────────────
class Hub(unittest.TestCase):
    def make(self, specs):
        def factory(h):
            return hub.NodeLink(h, fake_argv(*specs[h["id"]]))
        h = hub.Hub([local(i) for i in specs], factory)
        for lk in h.links.values():
            lk.start()
        self.addCleanup(h.stop)
        wait_for(lambda: all(lk.snapshot(time.monotonic())["daemon"] for lk in h.links.values()), what="all up")
        return h

    def test_change_reaches_only_the_named_machine(self):
        a_args, a_rec = Links.daemon_args(self, uuid="GPU-aaaa")
        b_args, b_rec = Links.daemon_args(self, uuid="GPU-bbbb")
        h = self.make({"a": a_args, "b": b_args})
        code, body = h.apply("b", {"op": "set_power", "uuid": "GPU-bbbb", "watts": 500})
        self.assertEqual((code, body["ok"]), (200, True))
        code, body = h.apply("a", {"op": "set_power", "uuid": "GPU-bbbb", "watts": 500})   # right uuid, wrong host
        self.assertEqual((code, body["error"]), (400, "no such GPU"))
        with open(b_rec) as f:
            self.assertEqual(len(f.readlines()), 1)
        with open(a_rec) as f:
            self.assertEqual(len(f.readlines()), 1)                     # the refused one, refused BY a
        self.assertEqual(h.apply("zzz", {"op": "set_power"})[0], 400)
        self.assertEqual(h.apply("a", {"op": "set_power", "uuid": "GPU-aaaa", "watts": float("inf")})[0], 400)

    def test_duplicate_uuid_across_machines_is_flagged(self):
        h = self.make({"a": ["--kind", "gb10"], "b": ["--kind", "gb10"]})
        self.assertTrue(any("reported by both" in w for w in h.state_json()["warnings"]))

    def test_history_is_per_machine_on_the_hubs_clock_with_gaps(self):
        h = self.make({"a": ["--kind", "5090"], "b": ["--kind", "gb10"]})
        wait_for(lambda: all(lk.fresh_sample(time.monotonic()) for lk in h.links.values()), what="samples")
        for _ in range(3):
            h.tick()
        h.links["b"].stop()
        wait_for(lambda: h.links["b"].fresh_sample(time.monotonic()) is None, timeout=6, what="b stale")
        h.tick()
        a, b = h.history_json("a", 300), h.history_json("b", 300)
        self.assertEqual(len(a["t"]), 4)
        self.assertEqual(a["gpus"][RTX5090]["temp"], [48, 48, 48, 48])
        self.assertEqual(b["gpus"][fakes.GB10]["temp"][:3], [52, 52, 52])
        self.assertIsNone(b["gpus"][fakes.GB10]["temp"][3])               # a gap, not a stale value
        self.assertEqual(b["gpus"][fakes.GB10]["cap"][0], None)           # GB10: no software cap
        json.dumps(h.state_json(), allow_nan=False)


# ── the page API ─────────────────────────────────────────────────────────────────────────────
class Http(unittest.TestCase):
    TOKEN = "t" * 43

    def serve(self, h):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        httpd = ThreadingHTTPServer(("127.0.0.1", port), server.make_handler(h, self.TOKEN, port))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)                 # cleanups run last-in first-out: stop, then close
        return port

    def req(self, port, method, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
        headers = {"Host": f"127.0.0.1:{port}", "Cookie": f"gpu_tuner={self.TOKEN}"}
        if body is not None:
            headers.update({"Origin": f"http://127.0.0.1:{port}", "Content-Type": "application/json"})
            body = body if isinstance(body, bytes) else json.dumps(body).encode()
        c.request(method, path, body, headers)
        r = c.getresponse()
        data = r.read()
        return r.status, json.loads(data, parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))

    def test_routing_and_validation(self):
        a_args, _ = Links.daemon_args(self, uuid="GPU-aaaa")
        h = Hub.make(self, {"a": a_args, "b": ["--kind", "gb10"]})
        port = self.serve(h)
        st, state = self.req(port, "GET", "/api/state")
        self.assertEqual((st, [x["id"] for x in state["hosts"]]), (200, ["a", "b"]))
        self.assertIn("reasons", state)
        ok = {"op": "set_power", "uuid": "GPU-aaaa", "watts": 450}
        self.assertEqual(self.req(port, "POST", "/api/apply", ok)[0], 400)                      # which machine?
        self.assertEqual(self.req(port, "POST", "/api/apply", dict(ok, host="zz"))[0], 400)
        self.assertEqual(self.req(port, "POST", "/api/apply", dict(ok, host=["a"]))[0], 400)
        self.assertEqual(self.req(port, "POST", "/api/apply", {"op": "status", "host": "a"})[0], 400)
        self.assertEqual(self.req(port, "POST", "/api/apply", b'{"op": "set_power", "host": "a", "watts": NaN}')[0], 400)
        self.assertEqual(self.req(port, "POST", "/api/apply", b'{"op": "set_power", "host": "a", "watts": 1e999}')[0], 400)
        st, body = self.req(port, "POST", "/api/apply", dict(ok, host="a"))
        self.assertEqual((st, body["gpus"][0]["settings"]["power_w"]), (200, 450))
        st, body = self.req(port, "POST", "/api/apply", dict(ok, host="b", uuid=fakes.GB10))
        self.assertEqual(st, 400)                                                           # monitor-only machine
        self.assertIn("not installed", body["error"])
        self.assertEqual(self.req(port, "GET", "/api/history?host=zz")[0], 404)
        st, hist = self.req(port, "GET", "/api/history?host=b&window=300")
        self.assertEqual((st, hist["host"], list(hist["gpus"])), (200, "b", [fakes.GB10]))

    def test_single_machine_needs_no_host_field(self):
        a_args, _ = Links.daemon_args(self, uuid="GPU-aaaa")
        h = Hub.make(self, {"local": a_args})
        port = self.serve(h)
        st, body = self.req(port, "POST", "/api/apply", {"op": "set_power", "uuid": "GPU-aaaa", "watts": 450})
        self.assertEqual((st, body["ok"]), (200, True))


if __name__ == "__main__":
    unittest.main(verbosity=1)
