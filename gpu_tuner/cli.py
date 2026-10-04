"""`gpu-tuner <command>`: one entry point, each command importing only what it needs.

  serve | open            the page (server.py)
  node --stdio | --check  this machine's GPUs, for a page here or elsewhere (node.py)
  probe                   read-only NVML capability report, as JSON (probe.py)
  hosts --check           connect to every machine in hosts.json once and say what's wrong
  hosts --init-key        create the page's own ssh key and print the restricted line to authorize it
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time


def cmd_hosts(argv):
    from . import hosts
    from .daemon import DEFAULT_SOCKET

    ap = argparse.ArgumentParser(prog="gpu-tuner hosts", description="check or set up the machines a page manages")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="connect to each machine once and report")
    mode.add_argument("--init-key", action="store_true",
                      help="create this page's ssh key (if missing) and print the line each machine needs")
    ap.add_argument("--hosts", default=hosts.HOSTS_FILE)
    ap.add_argument("--socket", default=DEFAULT_SOCKET, help="this machine's gpu-tunerd socket")
    ap.add_argument("--from", dest="from_", metavar="ADDR",
                    help="with --init-key: only accept the key from this address (the managing machine's IP "
                         "as the others see it), e.g. 10.0.0.5")
    ap.add_argument("--timeout", type=float, default=30.0, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if args.init_key:
        return init_key(args.from_)
    return check_hosts(args.hosts, args.socket, args.timeout)


def init_key(from_):
    from . import hosts
    key = hosts.KEY_FILE
    if os.path.exists(key):
        print(f"using the existing key {key}")
    else:
        for d in (hosts.CONFIG_DIR, os.path.dirname(key)):     # makedirs' mode only reaches the leaf
            os.makedirs(d, mode=0o700, exist_ok=True)
            os.chmod(d, 0o700)
        old = os.umask(0o077)
        try:
            # No passphrase: the page's service has to use it unattended. What limits it is the
            # authorized_keys line below (this command only, no shell, no forwarding).
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "gpu-tuner-hub", "-f", key],
                           check=True)
        except (OSError, subprocess.CalledProcessError) as e:
            sys.exit(f"gpu-tuner: ssh-keygen failed: {e}")
        finally:
            os.umask(old)
        print(f"created {key} (private half 0600, never printed)")
    try:
        with open(key + ".pub") as f:
            pub = f.read()
    except FileNotFoundError:               # the public half can always be derived again
        try:
            pub = subprocess.run(["ssh-keygen", "-y", "-f", key], check=True, capture_output=True,
                                 text=True).stdout
        except (OSError, subprocess.CalledProcessError) as e:
            sys.exit(f"gpu-tuner: {key}.pub is missing and could not be rebuilt: {e}")
    try:
        line = hosts.authorized_keys_line(pub, from_)
    except hosts.HostsError as e:
        sys.exit(f"gpu-tuner: {e}")
    print("\nAdd this ONE line to ~/.ssh/authorized_keys on every machine this page should manage")
    print("(as the user that machine's install.sh was run as):\n")
    print(line)
    if not from_:
        print("\nWARNING: without --from, a copy of this key works from anywhere. Re-run with\n"
              "  --from <this machine's IP as the others see it>   (comma-separate several)\n"
              "so each machine only accepts it from here.")
    print("\nThen list the machines in ~/.config/gpu-tuner/hosts.json and run: gpu-tuner hosts --check")
    return 0


def check_hosts(path, socket_path, timeout):
    from . import hosts
    from .server import make_link
    try:
        machines = hosts.load_hosts(path)
    except hosts.HostsError as e:
        print(f"{path}: {e}")
        return 1
    links = {m["id"]: make_link(m, socket_path) for m in machines}
    for link in links.values():
        link.start()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snaps = {hid: link.snapshot(time.monotonic()) for hid, link in links.items()}
        # settled: every machine either failed, or is up and has reported its daemon state
        settled = all(s["conn"] in ("down", "incompatible") or (s["conn"] == "up" and s["daemon"] is not None)
                      for s in snaps.values())
        if settled:
            break
        time.sleep(0.5)
    bad = 0
    for m in machines:
        s = links[m["id"]].snapshot(time.monotonic())
        how = f"ssh {m['ssh']}" if m["ssh"] else "this machine"
        if s["conn"] in ("up", "stale"):
            node = s["node"] or {}
            daemon = "control daemon running" if not s["daemon_error"] else f"monitor-only ({s['daemon_error']})"
            print(f"OK    {m['id']:<12} {how:<28} {len(s['static'])} GPU(s) · gpu-tuner {node.get('version')} "
                  f"· driver {node.get('driver')} · {daemon}")
            for g in s["static"]:
                print(f"        gpu{g['index']} {g['name']}")
        else:
            bad += 1
            print(f"FAIL  {m['id']:<12} {how:<28} {s['conn']}: {s['error'] or 'no answer yet'}")
        for note in s["notes"]:
            print(f"        note: {note}")
    for link in links.values():
        link.stop()
    return 1 if bad else 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else ""
    if cmd == "node":
        from .node import main as node_main
        return node_main(argv[1:])
    if cmd == "probe":
        from .probe import main as probe_main
        return probe_main()
    if cmd == "hosts":
        return cmd_hosts(argv[1:])
    from .server import main as server_main
    return server_main(argv)
