#!/usr/local/bin/python3
import json
import os
import pwd
import signal
import subprocess
import sys
import time

ROOT = "/opt/gpu-tuner"
sys.path.insert(0, ROOT)
DAEMON = [
    sys.executable, f"{ROOT}/gpu-tunerd",
    "--config", "/data/config/config.json",
    "--state", "/data/state/state.json",
    "--socket", "/run/gpu-tuner/control.sock",
]
UI = [
    sys.executable, f"{ROOT}/gpu-tuner",
    "serve",
    "--socket", "/run/gpu-tuner/control.sock",
]

children = []
stopping = False


def stop(_sig=None, _frame=None):
    global stopping
    stopping = True
    for child in children:
        if child.poll() is None:
            child.terminate()


def as_ui_user():
    user = pwd.getpwnam("gputuner")
    os.setgroups([])
    os.setgid(user.pw_gid)
    os.setuid(user.pw_uid)


def restore_fans():
    print("Supervisor: restoring NVIDIA automatic fan control", flush=True)
    try:
        subprocess.run(
            DAEMON + ["--restore-fans"],
            timeout=15,
            check=True,
        )
    except Exception as exc:
        print(f"Supervisor: fan restoration failed: {exc}", flush=True)

def configure_webui():
    import socket
    import ipaddress

    addresses = {
        ipaddress.IPv4Address(item[4][0])
        for item in socket.getaddrinfo(
            socket.gethostname(), None, socket.AF_INET
        )
        if not ipaddress.IPv4Address(item[4][0]).is_loopback
    }
    if len(addresses) != 1:
        raise RuntimeError(
            f"Expected exactly one container IPv4 address, got: {addresses}"
        )
    ip = str(addresses.pop())
    os.environ["GPU_TUNER_LAN_IP"] = ip
    os.environ["GPU_TUNER_BIND"] = "0.0.0.0"
    print(f"Supervisor: WebUI address http://{ip}:8765", flush=True)


def initialize_data():
    """Create persistent directories without replacing user configuration."""
    # The unprivileged UI must traverse the bind-mount root, even if the
    # host created it with mode 0700. Child directories retain private modes.
    os.chmod("/data", 0o711)
    for name in ("config", "state", "ui"):
        path = f"/data/{name}"
        os.makedirs(path, exist_ok=True)
        os.chmod(path, 0o700)

    config_path = "/data/config/config.json"
    if not os.path.exists(config_path):
        config = {
            "allowed_uid": 10001,
            "gpu_budget_w": None,
            "interval_s": 2.0,
        }
        fd = os.open(
            config_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(fd, "w") as f:
            json.dump(config, f, indent=2)
            f.write("\n")

    os.chown("/data/ui", 10001, 10001)
    os.chmod("/data/ui", 0o700)



def initialize_password():
    """Create the initial WebUI password hash without replacing it on restart."""
    from gpu_tuner.password_auth import hash_password, valid_password_hash

    parent = "/data/ui/.config"
    os.makedirs(parent, mode=0o700, exist_ok=True)
    os.chown(parent, 10001, 10001)
    os.chmod(parent, 0o700)
    directory = os.path.join(parent, "gpu-tuner")
    os.makedirs(directory, mode=0o700, exist_ok=True)
    os.chown(directory, 10001, 10001)
    os.chmod(directory, 0o700)

    path = os.path.join(directory, "password.hash")

    if os.path.lexists(path):
        if not os.path.isfile(path) or os.path.islink(path):
            raise RuntimeError("WebUI password file is not a regular file")
        with open(path, encoding="ascii") as f:
            stored = f.read().strip()
        if not valid_password_hash(stored):
            raise RuntimeError("WebUI password hash is invalid")
        os.chown(path, 10001, 10001)
        os.chmod(path, 0o600)
        return

    password = os.environ.get("GPU_TUNER_INITIAL_PASSWORD")
    if not password:
        raise RuntimeError(
            "Set GPU_TUNER_INITIAL_PASSWORD for first-run WebUI setup"
        )

    stored = hash_password(password)

    fd = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    with os.fdopen(fd, "w", encoding="ascii") as f:
        f.write(stored + "\n")

    os.chown(path, 10001, 10001)
    os.chmod(path, 0o600)

def reset_password():
    """Reset WebUI credentials from an administrator's interactive console."""
    import getpass
    import tempfile
    from gpu_tuner.password_auth import hash_password

    if os.geteuid() != 0:
        raise RuntimeError("Run password reset as root inside the container")
    if not sys.stdin.isatty():
        raise RuntimeError("Password reset requires an interactive terminal (docker exec -it)")
    password = getpass.getpass("New WebUI password: ")
    stored = hash_password(password)
    if password != getpass.getpass("Confirm new password: "):
        raise ValueError("Passwords do not match; password unchanged")

    directory = "/data/ui/.config/gpu-tuner"
    if not os.path.isdir(directory) or os.path.islink(directory):
        raise RuntimeError("WebUI credential directory is unavailable")
    fd, temporary = tempfile.mkstemp(prefix=".password-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as f:
            os.fchmod(f.fileno(), 0o600)
            os.fchown(f.fileno(), 10001, 10001)
            f.write(stored + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, os.path.join(directory, "password.hash"))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    print("WebUI password reset. Sign in with the new password; no restart needed.", flush=True)


def validate_existing_state():
    """Refuse to start if persistent GPU settings are unreadable."""
    path = "/data/state/state.json"

    if not os.path.exists(path):
        return

    with open(path, encoding="utf-8") as f:
        doc = json.load(f)

    if (
        not isinstance(doc, dict)
        or doc.get("version") != 1
        or not isinstance(doc.get("gpus"), dict)
    ):
        raise RuntimeError(
            f"Invalid state file {path}: expected version 1 with a GPUs object"
        )

    for uuid, settings in doc["gpus"].items():
        if not isinstance(uuid, str) or not uuid:
            raise RuntimeError(f"Invalid GPU identifier in {path}")

        if not isinstance(settings, dict):
            raise RuntimeError(f"Invalid settings for {uuid}: expected an object")

        if "power_w" in settings:
            watts = settings["power_w"]
            if watts is not None and (
                isinstance(watts, bool)
                or not isinstance(watts, int)
                or watts <= 0
            ):
                raise RuntimeError(f"Invalid power_w for {uuid}")

        if "clock_cap_mhz" in settings:
            clock = settings["clock_cap_mhz"]
            if clock is not None and (
                isinstance(clock, bool)
                or not isinstance(clock, int)
                or clock <= 0
            ):
                raise RuntimeError(f"Invalid clock_cap_mhz for {uuid}")

        if "fan" in settings:
            fan = settings["fan"]
            if not isinstance(fan, dict):
                raise RuntimeError(f"Invalid fan settings for {uuid}")

            if "mode" in fan and fan["mode"] not in (
                "auto", "curve", "manual"
            ):
                raise RuntimeError(f"Invalid fan mode for {uuid}")

            if "curve" in fan:
                curve = fan["curve"]
                if not isinstance(curve, list) or not curve:
                    raise RuntimeError(f"Invalid fan curve for {uuid}")

                for point in curve:
                    if (
                        not isinstance(point, (list, tuple))
                        or len(point) != 2
                        or any(
                            isinstance(value, bool)
                            or not isinstance(value, int)
                            for value in point
                        )
                    ):
                        raise RuntimeError(
                            f"Invalid fan curve point for {uuid}"
                        )

            if "manual_pct" in fan:
                pct = fan["manual_pct"]
                if (
                    isinstance(pct, bool)
                    or not isinstance(pct, int)
                    or not 0 <= pct <= 100
                ):
                    raise RuntimeError(f"Invalid manual_pct for {uuid}")

def main():
    initialize_data()
    try:
        initialize_password()
    finally:
        # Child processes must never inherit the initial password.
        # Docker's saved container configuration may still contain it.
        os.environ.pop("GPU_TUNER_INITIAL_PASSWORD", None)
    validate_existing_state()
    configure_webui()
    os.makedirs("/run/gpu-tuner", mode=0o711, exist_ok=True)
    os.chmod("/run/gpu-tuner", 0o711)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    try:
        daemon = subprocess.Popen(DAEMON)
        children.append(daemon)

        socket_path = "/run/gpu-tuner/control.sock"
        for _ in range(100):
            if stopping or daemon.poll() is not None:
                raise RuntimeError("Daemon stopped before socket became ready")
            if os.path.exists(socket_path):
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("Daemon socket did not appear")

        if stopping:
            return

        ui = subprocess.Popen(
            UI,
            preexec_fn=as_ui_user,
            env={**os.environ, "HOME": "/data/ui"},
        )
        children.append(ui)

        print("Supervisor: daemon and UI started", flush=True)

        while not stopping:
            for child in children:
                if child.poll() is not None:
                    raise RuntimeError(
                        f"Child PID {child.pid} exited with {child.returncode}"
                    )
            time.sleep(0.5)

    finally:
        stop()
        for child in children:
            if child.poll() is None:
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()

        restore_fans()


if __name__ == "__main__":
    try:
        if sys.argv[1:] == ["--reset-password"]:
            reset_password()
        elif sys.argv[1:]:
            raise ValueError("Usage: gpu-tuner-entrypoint [--reset-password]")
        else:
            main()
    except Exception as exc:
        print(f"Supervisor: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)
