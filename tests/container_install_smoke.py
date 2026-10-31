"""Exercise installed image and first-run persistence without GPU access.

Run only in a disposable container with an empty /data mount. No daemon is started.
"""
import http.client
import importlib.util
import json
import os
from pathlib import Path
import secrets
import threading
from http.server import ThreadingHTTPServer

import sys
sys.path.insert(0, "/opt/gpu-tuner")
from gpu_tuner.password_auth import verify_password
from gpu_tuner.server import make_handler

# The installed entrypoint has no .py extension.
from importlib.machinery import SourceFileLoader
spec = importlib.util.spec_from_loader("entrypoint", SourceFileLoader("entrypoint", "/usr/local/bin/gpu-tuner-entrypoint"))
entrypoint = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entrypoint)

assert not any(Path("/data").iterdir()), "Requires empty disposable /data"
os.chmod("/data", 0o700)
entrypoint.initialize_data()
assert Path("/data").stat().st_mode & 0o777 == 0o711
try:
    entrypoint.initialize_password()
except RuntimeError:
    pass
else:
    raise AssertionError("Missing first-run password must fail")
# Simulate a migrated appdata parent created privately by root.
os.chown("/data/ui/.config", 0, 0)
os.chmod("/data/ui/.config", 0o700)
password = secrets.token_urlsafe(24)
os.environ["GPU_TUNER_INITIAL_PASSWORD"] = password
entrypoint.initialize_password()
credential = Path("/data/ui/.config/gpu-tuner/password.hash")
original = credential.read_bytes()
assert original.endswith(b"\n")
assert verify_password(password, original.decode().strip())
assert credential.stat().st_mode & 0o777 == 0o600
assert credential.stat().st_uid == 10001
config = Path("/data/config/config.json")
settings = json.loads(config.read_text())
settings["gpu_budget_w"] = 125
config.write_text(json.dumps(settings))
state = Path("/data/state/state.json")
state.write_text(json.dumps({"version": 1, "gpus": {}}))
# Re-run initialization as on container recreation; existing data must survive.
os.environ["GPU_TUNER_INITIAL_PASSWORD"] = secrets.token_urlsafe(24)
entrypoint.initialize_data()
entrypoint.initialize_password()
entrypoint.validate_existing_state()
assert credential.read_bytes() == original
assert json.loads(config.read_text())["gpu_budget_w"] == 125
assert json.loads(state.read_text()) == {"version": 1, "gpus": {}}
# Exercise the documented administrator recovery path on disposable data.
old_password = password
saved_config = config.read_bytes()
saved_state = state.read_bytes()
credential.unlink()
os.environ.pop("GPU_TUNER_INITIAL_PASSWORD")
try:
    entrypoint.initialize_password()
except RuntimeError:
    pass
else:
    raise AssertionError("Recovery without a new password must fail")
assert not credential.exists()
os.environ["GPU_TUNER_INITIAL_PASSWORD"] = "short"
try:
    entrypoint.initialize_password()
except ValueError:
    pass
else:
    raise AssertionError("Recovery with a weak password must fail")
assert not credential.exists()
password = secrets.token_urlsafe(24)
os.environ["GPU_TUNER_INITIAL_PASSWORD"] = password
entrypoint.initialize_password()
replacement = credential.read_bytes()
assert replacement != original
assert verify_password(password, replacement.decode().strip())
assert not verify_password(old_password, replacement.decode().strip())
assert credential.stat().st_mode & 0o777 == 0o600
assert credential.stat().st_uid == 10001
assert config.read_bytes() == saved_config
assert state.read_bytes() == saved_state
os.environ.pop("GPU_TUNER_INITIAL_PASSWORD")
# Interactive administrator reset: failures preserve credentials and GPU data.
from unittest.mock import patch
before_reset = credential.read_bytes()
for answers in (("short",), (secrets.token_urlsafe(24), "different")):
    with patch("sys.stdin.isatty", return_value=True), patch("getpass.getpass", side_effect=answers):
        try:
            entrypoint.reset_password()
        except ValueError:
            pass
        else:
            raise AssertionError("Invalid interactive reset must fail")
    assert credential.read_bytes() == before_reset
with patch("sys.stdin.isatty", return_value=False):
    try:
        entrypoint.reset_password()
    except RuntimeError:
        pass
    else:
        raise AssertionError("Noninteractive reset must fail")
old_password = password
password = secrets.token_urlsafe(24)
with patch("sys.stdin.isatty", return_value=True), patch("getpass.getpass", return_value=password), patch.object(entrypoint.os, "replace", side_effect=OSError("simulated write failure")):
    try:
        entrypoint.reset_password()
    except OSError:
        pass
    else:
        raise AssertionError("Failed replacement must fail")
assert credential.read_bytes() == before_reset
assert not list(credential.parent.glob(".password-*"))
with patch("sys.stdin.isatty", return_value=True), patch("getpass.getpass", return_value=password):
    entrypoint.reset_password()
replacement = credential.read_bytes()
assert verify_password(password, replacement.decode().strip())
assert not verify_password(old_password, replacement.decode().strip())
assert credential.stat().st_mode & 0o777 == 0o600
assert credential.stat().st_uid == 10001
assert config.read_bytes() == saved_config
assert state.read_bytes() == saved_state
# A subsequent restart must work without the initialization secret.
entrypoint.initialize_password()
assert credential.read_bytes() == replacement
os.environ["GPU_TUNER_PUBLIC_URL"] = "http://192.0.2.10:18765"
os.environ["GPU_TUNER_LAN_IP"] = ""
entrypoint.as_ui_user()
assert os.getuid() == 10001
server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(None, secrets.token_urlsafe(32), 8765))
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()

def request(method, path, body=None, cookie=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    headers = {"Host": "192.0.2.10:18765", "Origin": "http://192.0.2.10:18765", "Content-Type": "application/json"}
    if cookie:
        headers["Cookie"] = cookie
    try:
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        return result
    finally:
        conn.close()

try:
    assert request("GET", "/")[0] == 303
    assert request("GET", "/login")[0] == 200
    assert request("POST", "/api/login", json.dumps({"password": old_password}))[0] == 401
    status, headers, _ = request("POST", "/api/login", json.dumps({"password": password}))
    assert status == 200, status
    cookie = headers["Set-Cookie"].split(";", 1)[0]
    assert request("GET", "/", cookie=cookie)[0] == 200
    assert request("GET", "/app.js", cookie=cookie)[0] == 200
    assert request("POST", "/api/logout", cookie=cookie)[0] == 200
    assert request("GET", "/app.js", cookie=cookie)[0] == 401
finally:
    server.shutdown()
    server.server_close()
    thread.join()
print("PASS: installed assets, first-run login, UID, persistence, password recovery and logout (no GPU)")
