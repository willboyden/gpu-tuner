"""Which machines a gpu-tuner page manages, and exactly how it reaches each one.

~/.config/gpu-tuner/hosts.json (optional; without it the page manages just this machine):

    {"hosts": [
      {"id": "local", "label": "Workstation",
       "wall": {"non_gpu_dc_w": 630, "psu_efficiency": 0.9, "circuits": {"15 A": 1440, "20 A": 1920}}},
      {"id": "gpu-box-2", "label": "GPU box 2", "ssh": "gpu-box-2"},
      {"id": "edge-1", "ssh": "me@10.0.0.21", "port": 2222}
    ]}

  id     lowercase name used in the page's URL. "local" (and only "local") is this machine.
  ssh    what you'd type after `ssh` — a ~/.ssh/config alias or user@host. Never an option.
  wall   optional, display-only: the page estimates worst-case wall power as
         (non_gpu_dc_w + the GPU caps) / psu_efficiency and compares it to each circuit.

The file is read only at startup, only from disk, and must be writable by you alone: it decides
what this page runs ssh against. Nothing a browser sends can change it or reach ssh's arguments.
Every remote runs one fixed command (REMOTE_CMD) — and should be made to, by a key restricted to
it in authorized_keys (`gpu-tuner hosts --init-key` prints the line).
"""
from __future__ import annotations

import json
import math
import os
import re
import socket
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENTRY = os.path.join(ROOT, "gpu-tuner")
CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".config", "gpu-tuner")   # not $XDG_*: see server.config_dir
HOSTS_FILE = os.path.join(CONFIG_DIR, "hosts.json")
KEY_FILE = os.path.join(CONFIG_DIR, "ssh", "id_ed25519")
REMOTE_NODE = "/usr/local/lib/gpu-tuner/gpu-tuner"     # install.sh puts it here in every mode
REMOTE_CMD = f"{REMOTE_NODE} node --stdio"

MAX_HOSTS = 32
MAX_FILE = 64 * 1024
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
# [user@]host: no leading '-' (it would be read as an ssh option, e.g. -oProxyCommand=...), no '%'
# (ssh token expansion), no whitespace or shell metacharacters.
SSH_RE = re.compile(r"^(?:[A-Za-z0-9._-]{1,32}@)?[A-Za-z0-9][A-Za-z0-9._-]{0,252}$")
FROM_RE = re.compile(r"^[0-9A-Za-z.:/*?!,-]{1,256}$")
LABEL_MAX = 64
HOST_KEYS = {"id", "label", "ssh", "port", "wall"}
WALL_KEYS = {"non_gpu_dc_w", "psu_efficiency", "circuits"}


class HostsError(ValueError):
    pass


class NoHubKey(HostsError):
    pass


def default_hosts():
    return [{"id": "local", "label": socket.gethostname() or "This machine", "ssh": None,
             "port": None, "wall": None}]


def _num(v, lo, hi, what):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not lo <= v <= hi:
        raise HostsError(f"{what} must be a number from {lo} to {hi}")
    return v


def _wall(w, where):
    if w is None:
        return None
    if not isinstance(w, dict) or set(w) - WALL_KEYS or not {"non_gpu_dc_w", "psu_efficiency"} <= set(w):
        raise HostsError(f"{where}: wall needs non_gpu_dc_w and psu_efficiency (and optionally circuits), nothing else")
    circuits = w.get("circuits") or {}
    if not isinstance(circuits, dict) or len(circuits) > 4:
        raise HostsError(f"{where}: wall.circuits must be an object of up to 4 \"name\": watts entries")
    out_c = {}
    for name, watts in circuits.items():
        if not isinstance(name, str) or not 0 < len(name) <= 16 or not name.isprintable():
            raise HostsError(f"{where}: wall.circuits names must be 1-16 printable characters")
        out_c[name] = _num(watts, 1, 100000, f"{where}: wall.circuits[{name!r}]")
    return {"non_gpu_dc_w": _num(w["non_gpu_dc_w"], 0, 100000, f"{where}: wall.non_gpu_dc_w"),
            "psu_efficiency": _num(w["psu_efficiency"], 0.5, 1.0, f"{where}: wall.psu_efficiency"),
            "circuits": out_c}


def parse_hosts(doc):
    if not isinstance(doc, dict) or set(doc) != {"hosts"}:
        raise HostsError('the file must be {"hosts": [ ... ]} and nothing else')
    raw = doc["hosts"]
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_HOSTS:
        raise HostsError(f"hosts must be a list of 1-{MAX_HOSTS} machines")
    out, seen = [], set()
    for i, h in enumerate(raw):
        where = f"hosts[{i}]"
        if not isinstance(h, dict):
            raise HostsError(f"{where} must be an object")
        extra = set(h) - HOST_KEYS
        if extra:
            raise HostsError(f"{where}: unknown key(s) {', '.join(sorted(map(str, extra)))}")
        hid = h.get("id")
        if not isinstance(hid, str) or not ID_RE.match(hid):
            raise HostsError(f"{where}: id must be lowercase letters, digits and '-', 1-32 long, not starting with '-'")
        if hid in seen:
            raise HostsError(f"{where}: id {hid!r} is used twice")
        seen.add(hid)
        where = f"host {hid!r}"
        label = h.get("label", hid)
        if not isinstance(label, str) or not 0 < len(label) <= LABEL_MAX or not label.isprintable():
            raise HostsError(f"{where}: label must be 1-{LABEL_MAX} printable characters")
        dest, port = h.get("ssh"), h.get("port")
        if hid == "local":
            if dest is not None or port is not None:
                raise HostsError(f'{where}: "local" is this machine; it takes no ssh or port')
        else:
            if not isinstance(dest, str) or not SSH_RE.match(dest):
                raise HostsError(f"{where}: ssh must be a host alias or user@host (letters, digits, '.', '_', '-'; "
                                 f"not starting with '-')")
            if port is not None and (isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535):
                raise HostsError(f"{where}: port must be 1-65535")
        out.append({"id": hid, "label": label, "ssh": dest, "port": port, "wall": _wall(h.get("wall"), where)})
    return out


def _reject_constant(name):
    raise HostsError(f"{name} is not a number")


def load_hosts(path=HOSTS_FILE):
    """The configured machines, or just this one if the file doesn't exist."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return default_hosts()
    except OSError as e:
        raise HostsError(f"cannot open it: {e.strerror}") from None
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        if st.st_uid != os.geteuid():
            raise HostsError("it must be owned by you: it decides what this page runs ssh against")
        if st.st_mode & 0o022:
            raise HostsError("it must not be group- or world-writable (chmod 600)")
        data = f.read(MAX_FILE + 1)
    if len(data) > MAX_FILE:
        raise HostsError(f"it is over {MAX_FILE} bytes")
    try:
        doc = json.loads(data, parse_constant=_reject_constant)
    except (ValueError, RecursionError) as e:
        raise HostsError(f"not valid JSON: {e}") from None
    return parse_hosts(doc)


# ── how each machine is reached ──────────────────────────────────────────────────────────────
def ssh_argv(host, key_file=None):
    """The exact ssh command for one remote. Your ~/.ssh/config still applies (aliases, users,
    jump hosts) and ~/.ssh/known_hosts is only READ: StrictHostKeyChecking=yes never adds or
    updates a key, so an unknown or changed host key is an error, not a silent trust.

    It authenticates with the page's own key only: no agent, no passwords, and no falling back to
    your default ~/.ssh/id_* keys. (An IdentityFile your ~/.ssh/config names for that host is still
    tried — ssh adds those up — and a login through one is unrestricted; the page then says so.)
    Raises NoHubKey if `gpu-tuner hosts --init-key` hasn't been run."""
    key_file = key_file or KEY_FILE         # looked up now, not when this function was defined
    if not os.path.exists(key_file):
        raise NoHubKey("this page has no ssh key yet: run `gpu-tuner hosts --init-key`, then add the "
                       "line it prints to authorized_keys on each machine")
    argv = ["ssh", "-T", "-e", "none",
            "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "UpdateHostKeys=no",
            "-o", "VerifyHostKeyDNS=no", "-o", "IdentityAgent=none", "-o", "PKCS11Provider=none",
            "-o", "PasswordAuthentication=no", "-o", "KbdInteractiveAuthentication=no",
            # a ControlMaster in your config would try to create a socket under ~/.ssh, which the
            # page's service can't write; and nothing here should ride on a shared connection
            "-o", "ControlMaster=no", "-o", "ControlPath=none",
            "-o", "ForwardAgent=no", "-o", "ForwardX11=no", "-o", "ClearAllForwardings=yes",
            "-o", "PermitLocalCommand=no", "-o", "ConnectTimeout=5",
            "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=3",
            "-i", key_file, "-o", "IdentitiesOnly=yes"]
    if host.get("port"):
        argv += ["-p", str(host["port"])]
    return argv + ["--", host["ssh"], REMOTE_CMD]


def ssh_env():
    """ssh gets a scrubbed environment: no SSH_AUTH_SOCK, so a terminal and the systemd unit
    authenticate identically (with the page's own key), and nothing else leaks into the child."""
    env = {"HOME": os.path.expanduser("~"), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "LANG": "C.UTF-8"}
    if os.environ.get("USER"):
        env["USER"] = os.environ["USER"]
    return env


def local_argv(socket_path=None):
    argv = [sys.executable, ENTRY, "node", "--stdio"]
    if socket_path:
        argv += ["--socket", socket_path]
    return argv


def wall_estimate_w(caps_total_w, wall):
    """Worst case at the wall at these caps, or None without a wall model. An ESTIMATE."""
    if not wall:
        return None
    return int(round((wall["non_gpu_dc_w"] + caps_total_w) / wall["psu_efficiency"]))


def authorized_keys_line(pubkey, from_=None):
    """The line to add to ~/.ssh/authorized_keys on each managed machine: this key may run the
    node and nothing else — no shell, no forwarding, no pty (`restrict`) — and, with from_, only
    from the managing machine's address."""
    pubkey = pubkey.strip()
    parts = pubkey.split()
    if len(parts) < 2 or not re.fullmatch(r"(ssh|ecdsa|sk)-[a-z0-9@.-]+", parts[0]) \
            or not re.fullmatch(r"[A-Za-z0-9+/=]+", parts[1]):
        raise HostsError("that doesn't look like an OpenSSH public key")
    opts = ["restrict"]
    if from_:
        if not FROM_RE.match(from_):
            raise HostsError("--from must be addresses or patterns like 10.0.0.5 or 10.0.0.0/24")
        opts.append(f'from="{from_}"')
    opts.append(f'command="{REMOTE_CMD}"')
    return f"{','.join(opts)} {parts[0]} {parts[1]} gpu-tuner-hub"
