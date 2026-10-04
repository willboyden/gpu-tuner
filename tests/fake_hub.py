#!/usr/bin/env python3
"""Test-only: the REAL page server (server.make_handler + hub.Hub) over four fake machines, for the
live UI test. Production has no flag for this: here the machines' transports are built in code.

  ws      a 2x RTX PRO 6000 box (Max-Q + Workstation) with a real daemon over fake NVML, a wall model
  gb10    a DGX Spark-like GB10, monitor-only (no daemon)
  rtx5090 a GeForce RTX 5090 with a real daemon; every change it receives is recorded
  gone    a machine whose node exits at once: always unreachable

  fake_hub.py --port 8767 --tmp DIR
"""
import argparse
import os
import sys
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from gpu_tuner import hub, server  # noqa: E402

FAKE = os.path.join(HERE, "fake_node.py")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--tmp", required=True)
    args = ap.parse_args()
    t = args.tmp

    def with_daemon(hid, kind, budget):
        return [sys.executable, FAKE, "--kind", kind, "--daemon-sock", os.path.join(t, f"{hid}.sock"),
                "--state", os.path.join(t, f"{hid}.json"), "--budget", budget,
                "--record", os.path.join(t, f"rec-{hid}.jsonl")]
    argv = {"ws": with_daemon("ws", "pair", "750"),
            "gb10": [sys.executable, FAKE, "--kind", "gb10"],
            "rtx5090": with_daemon("rtx5090", "5090", "575"),
            "gone": [sys.executable, "-c", "import sys; sys.stderr.write('ssh: connect to host gone port 22: No route to host\\n'); sys.exit(255)"]}
    machines = [
        {"id": "ws", "label": "Workstation box", "ssh": None, "port": None,
         "wall": {"non_gpu_dc_w": 630, "psu_efficiency": 0.9, "circuits": {"15 A": 1440, "20 A": 1920}}},
        {"id": "gb10", "label": "Spark 1", "ssh": "gb10-box", "port": None, "wall": None},
        {"id": "rtx5090", "label": "RTX box", "ssh": "rtx-box", "port": None, "wall": None},
        {"id": "gone", "label": "Gone box", "ssh": "gone", "port": None, "wall": None},
    ]
    h = hub.Hub(machines, lambda m: hub.NodeLink(m, argv[m["id"]]))
    h.start()
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), server.make_handler(h, server.session_token(), args.port))
    print(f"fake hub on http://127.0.0.1:{args.port}/", flush=True)
    try:
        httpd.serve_forever()
    finally:
        h.stop()


if __name__ == "__main__":
    sys.exit(main())
