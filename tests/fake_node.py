#!/usr/bin/env python3
"""A stand-in `gpu-tuner node --stdio` for tests: the REAL node code over a fake NVML, optionally
with a REAL daemon (in-process, on a scratch socket, over the same fake cards so writes read
back), or one of a few scripted misbehaviours. Never touches a GPU, never needs root.

  fake_node.py --kind 5090 --daemon-sock /tmp/x.sock --state /tmp/x.json   a controllable 5090
  fake_node.py --kind gb10                                                  a monitor-only GB10
  fake_node.py --script proto99 | fatal | garbage | junk | no-reply | exit-on-apply | stop-sampling
                        | bad-hello | extra-uuid
"""
import argparse
import json
import os
import socket
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import fakes  # noqa: E402
from gpu_tuner import daemon as dm  # noqa: E402
from gpu_tuner import node, proto  # noqa: E402
from gpu_tuner.nvml import Nvml  # noqa: E402

dm.log = lambda msg: print(msg, file=sys.stderr, flush=True)   # stdout is the protocol channel


def run_daemon(lib, sock, state, budget):
    d = dm.Daemon(Nvml(lib=lib), {"gpu_budget_w": budget, "allowed_uid": os.getuid(), "interval_s": 2.0},
                  state, os.path.join(os.path.dirname(state), "config.json"))
    d.load_state()
    d.apply_startup()
    d.save_state()
    srv, _ino = dm.open_socket(sock, os.getuid())

    def loop():
        nxt = 0.0
        while True:
            now = time.monotonic()
            if now >= nxt:
                d.tick(now)
                nxt = now + 2.0
            srv.settimeout(0.5)
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            with conn:
                try:
                    dm.serve_one(conn, d, os.getuid())
                except OSError:
                    pass
    threading.Thread(target=loop, daemon=True).start()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", choices=sorted(fakes.KINDS), default="5090")
    ap.add_argument("--uuid", help="override the first card's UUID")
    ap.add_argument("--daemon-sock")
    ap.add_argument("--state")
    ap.add_argument("--budget", default="null")
    ap.add_argument("--script", default="real")
    ap.add_argument("--record", help="append every forwarded change request here, one JSON per line")
    args = ap.parse_args()
    out = sys.stdout.buffer

    if args.script == "proto99":
        out.write(proto.encode({"type": "hello", "proto": 99, "version": "9.9.9", "static": []}))
        out.flush()
        sys.stdin.buffer.read()
        return 0
    if args.script == "bad-hello":
        out.write(proto.encode({"type": "hello"}))           # no protocol version at all
        out.flush()
        sys.stdin.buffer.read()
        return 0
    if args.script == "fatal":
        out.write(proto.encode({"type": "fatal", "error": "NVML unavailable on this machine: test"}))
        out.flush()
        return 2
    if args.script == "junk":
        out.write(b"Welcome to a chatty login shell!\nlast login: yesterday\n")
        out.flush()

    lib = fakes.KINDS[args.kind]()
    if args.uuid:
        lib.cards[0]["uuid"] = args.uuid
    sock = args.daemon_sock or os.path.join(tempfile.mkdtemp(), "absent.sock")
    if args.daemon_sock:
        run_daemon(lib, args.daemon_sock, args.state, None if args.budget == "null" else int(args.budget))
    nv = Nvml(lib=lib, meminfo=fakes.meminfo_file(tempfile.mkdtemp()))
    c = node.Collector(nv, sock)
    real_apply, real_sample, started = c.apply, c.sample, time.monotonic()

    def apply(req):
        if args.record:
            with open(args.record, "a") as f:
                f.write(json.dumps(req) + "\n")
        if args.script == "no-reply":
            time.sleep(3600)
        if args.script == "exit-on-apply":
            os._exit(1)
        return real_apply(req)

    def sample():
        if args.script == "stop-sampling" and time.monotonic() - started > 2:
            raise RuntimeError("sampling stopped (test)")
        gpus = real_sample()
        if args.script == "extra-uuid":                      # a GPU the hello never announced
            gpus[f"GPU-unannounced-{time.monotonic_ns()}"] = dict(next(iter(gpus.values())))
        return gpus
    c.apply, c.sample = apply, sample

    if args.script == "garbage":
        n = node.Node(c, sys.stdin.buffer, out)
        n.send(c.hello())
        time.sleep(0.5)
        out.write(b"this is not json\n")
        out.flush()
        sys.stdin.buffer.read()
        return 0
    return node.Node(c, sys.stdin.buffer, out).run()


if __name__ == "__main__":
    sys.exit(main())
