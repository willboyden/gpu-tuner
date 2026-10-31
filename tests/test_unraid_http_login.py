"""HTTP password-login tests without a GPU daemon."""

import http.client
import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from gpu_tuner.password_auth import hash_password
from gpu_tuner.server import make_handler


HOST = "192.168.1.10:18765"
ORIGIN = f"http://{HOST}"
PASSWORD = "Example-Strong-Password-123"


class HTTPLoginTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

        config = Path(self.temp.name)
        (config / "password.hash").write_text(
            hash_password(PASSWORD) + "\n",
            encoding="ascii",
        )

        self.config_patch = patch(
            "gpu_tuner.server.config_dir",
            return_value=str(config),
        )
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)

        self.env_patch = patch.dict(
            os.environ,
            {
                "GPU_TUNER_PUBLIC_URL": f"http://{HOST}",
                "GPU_TUNER_LAN_IP": "",
            },
        )
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            make_handler(
                h=None,
                token="test-session-token",
                port=8765,
            ),
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            daemon=True,
        )
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(self, path="/api/login", password=PASSWORD,
                origin=ORIGIN, host=HOST, body=None, cookie=None):
        if body is None:
            body = json.dumps({"password": password}).encode()

        headers = {
            "Host": host,
            "Content-Type": "application/json",
        }
        if origin is not None:
            headers["Origin"] = origin
        if cookie is not None:
            headers["Cookie"] = cookie

        conn = http.client.HTTPConnection(
            "127.0.0.1",
            self.server.server_port,
            timeout=5,
        )
        try:
            conn.request("POST", path, body=body, headers=headers)
            response = conn.getresponse()
            data = response.read()
            return response.status, dict(response.getheaders()), data
        finally:
            conn.close()

    def test_correct_password_sets_session_cookie(self):
        status, headers, _ = self.request()
        self.assertEqual(status, 200)
        self.assertIn("gpu_tuner=", headers["Set-Cookie"])
        self.assertNotIn("gpu_tuner=test-session-token", headers["Set-Cookie"])
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])

    def test_separate_logins_get_independent_valid_sessions(self):
        first_status, first_headers, _ = self.request()
        second_status, second_headers, _ = self.request()

        self.assertEqual(first_status, 200)
        self.assertEqual(second_status, 200)

        first_cookie = first_headers["Set-Cookie"].split(";", 1)[0]
        second_cookie = second_headers["Set-Cookie"].split(";", 1)[0]

        self.assertNotEqual(first_cookie, second_cookie)
        self.assertNotIn("test-session-token", first_cookie)
        self.assertNotIn("test-session-token", second_cookie)

        for session_cookie in (first_cookie, second_cookie):
            with self.subTest(cookie=session_cookie):
                conn = http.client.HTTPConnection(
                    "127.0.0.1",
                    self.server.server_port,
                    timeout=5,
                )
                try:
                    conn.request(
                        "GET",
                        "/",
                        headers={"Host": HOST, "Cookie": session_cookie},
                    )
                    response = conn.getresponse()
                    response.read()
                    self.assertEqual(response.status, 200)
                finally:
                    conn.close()

    def test_nul_rejected_but_literal_escape_can_be_a_password(self):
        status, _, _ = self.request(password="Example-Password-\x00")
        self.assertEqual(status, 400)
        literal = r"Example-Password-\x00-123"
        Path(self.temp.name, "password.hash").write_text(hash_password(literal))
        self.assertEqual(self.request(password=literal)[0], 200)

    def test_incorrect_password_rejected(self):
        status, headers, _ = self.request(
            password="Incorrect-Password-123",
        )
        self.assertEqual(status, 401)
        self.assertNotIn("Set-Cookie", headers)

    def test_missing_origin_rejected(self):
        status, _, _ = self.request(origin=None)
        self.assertEqual(status, 403)

    def test_cross_origin_rejected(self):
        status, _, _ = self.request(
            origin="http://attacker.example",
        )
        self.assertEqual(status, 403)

    def test_unexpected_host_rejected(self):
        status, _, _ = self.request(host="attacker.example:18765")
        self.assertEqual(status, 421)

    def test_rate_limit(self):
        for _ in range(5):
            status, _, _ = self.request(
                password="Incorrect-Password-123",
            )
            self.assertEqual(status, 401)

        status, _, _ = self.request()
        self.assertEqual(status, 429)

    def test_oversized_request_rejected(self):
        status, _, _ = self.request(body=b"x" * 4097)
        self.assertEqual(status, 400)

    def test_gpu_apply_requires_authentication(self):
        status, _, _ = self.request(
            path="/api/apply",
            body=b"{}",
        )
        self.assertEqual(status, 401)


if __name__ == "__main__":
    unittest.main()


class LoginPageRoutingTests(unittest.TestCase):
    setUp = HTTPLoginTests.setUp
    stop_server = HTTPLoginTests.stop_server
    """Public login assets must not expose protected dashboard resources."""

    def get(self, path, cookie=None, host=HOST):
        headers = {"Host": host}
        if cookie is not None:
            headers["Cookie"] = cookie

        conn = http.client.HTTPConnection(
            "127.0.0.1",
            self.server.server_port,
            timeout=5,
        )
        try:
            conn.request("GET", path, headers=headers)
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def test_login_assets_are_public(self):
        for path in ("/login", "/login.js", "/theme.js", "/style.css"):
            with self.subTest(path=path):
                status, _, body = self.get(path)
                self.assertEqual(status, 200)
                self.assertTrue(body)

    def test_unauthenticated_dashboard_redirects_to_login(self):
        status, headers, _ = self.get("/")
        self.assertEqual(status, 303)
        self.assertEqual(headers.get("Location"), "/login")

    def test_dashboard_script_and_api_remain_protected(self):
        for path in ("/app.js", "/api/state"):
            with self.subTest(path=path):
                status, _, _ = self.get(path)
                self.assertEqual(status, 401)

    def test_authenticated_dashboard_is_accessible(self):
        status, _, body = self.get(
            "/",
            cookie="gpu_tuner=test-session-token",
        )
        self.assertEqual(status, 200)
        self.assertIn(b"/app.js", body)

    def test_unexpected_host_cannot_fetch_login_page(self):
        status, _, _ = self.get("/login", host="evil.example")
        self.assertEqual(status, 421)

    def test_desktop_mode_does_not_expose_login_page(self):
        with patch("gpu_tuner.server.os.path.isfile", return_value=False):
            status, _, _ = self.get("/login")
        self.assertEqual(status, 401)

    def test_reset_command_uses_unraid_container_name(self):
        with patch.dict(os.environ, {"HOST_CONTAINERNAME": "gpu-tuner-install-test"}):
            status, _, body = self.get("/login")
        self.assertEqual(status, 200)
        self.assertIn(b"docker exec -it gpu-tuner-install-test python", body)

    def test_reset_command_rejects_unsafe_container_name(self):
        with patch.dict(os.environ, {"HOST_CONTAINERNAME": "bad;command"}):
            _, _, body = self.get("/login")
        self.assertIn(b"docker exec -it gpu-tuner python", body)
        self.assertNotIn(b"bad;command", body)


class LogoutTests(unittest.TestCase):
    setUp = HTTPLoginTests.setUp
    stop_server = HTTPLoginTests.stop_server
    request = HTTPLoginTests.request
    """Logout revokes only the requesting browser session."""

    def post_logout(self, cookie, origin=ORIGIN):
        headers = {
            "Host": HOST,
            "Cookie": cookie,
            "Content-Length": "0",
        }
        if origin is not None:
            headers["Origin"] = origin

        conn = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=5,
        )
        try:
            conn.request("POST", "/api/logout", body=b"", headers=headers)
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def get_dashboard(self, cookie):
        conn = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=5,
        )
        try:
            conn.request("GET", "/", headers={"Host": HOST, "Cookie": cookie})
            response = conn.getresponse()
            response.read()
            return response.status
        finally:
            conn.close()

    def test_logout_revokes_only_current_browser(self):
        _, first_headers, _ = self.request()
        _, second_headers, _ = self.request()

        first = first_headers["Set-Cookie"].split(";", 1)[0]
        second = second_headers["Set-Cookie"].split(";", 1)[0]

        status, headers, _ = self.post_logout(first)

        self.assertEqual(status, 200)
        self.assertIn("Max-Age=0", headers["Set-Cookie"])
        self.assertEqual(self.get_dashboard(first), 303)
        self.assertEqual(self.get_dashboard(second), 200)

    def test_cross_origin_logout_is_rejected(self):
        _, headers, _ = self.request()
        cookie = headers["Set-Cookie"].split(";", 1)[0]

        status, _, _ = self.post_logout(
            cookie, origin="http://evil.example",
        )

        self.assertEqual(status, 403)
        self.assertEqual(self.get_dashboard(cookie), 200)

    def test_unauthenticated_logout_is_rejected(self):
        status, _, _ = self.post_logout("gpu_tuner=invalid")
        self.assertEqual(status, 401)

    def test_desktop_token_survives_logout(self):
        desktop = "gpu_tuner=test-session-token"

        status, _, _ = self.post_logout(desktop)

        self.assertEqual(status, 200)
        self.assertEqual(self.get_dashboard(desktop), 200)


class PasswordChangeTests(unittest.TestCase):
    setUp = HTTPLoginTests.setUp
    stop_server = HTTPLoginTests.stop_server
    request = HTTPLoginTests.request
    get = LoginPageRoutingTests.get

    def test_password_change_invalidates_browser_sessions(self):
        _, headers, _ = self.request()
        old_cookie = headers["Set-Cookie"].split(";", 1)[0]
        self.assertEqual(self.get("/app.js", old_cookie)[0], 200)

        new_password = "Replacement-Strong-Password-456"
        Path(self.temp.name, "password.hash").write_text(
            hash_password(new_password), encoding="ascii",
        )
        self.assertEqual(self.get("/app.js", old_cookie)[0], 401)
        self.assertEqual(self.request()[0], 401)
        status, headers, _ = self.request(password=new_password)
        self.assertEqual(status, 200)
        new_cookie = headers["Set-Cookie"].split(";", 1)[0]
        self.assertEqual(self.get("/app.js", new_cookie)[0], 200)
        self.assertEqual(self.get("/app.js", "gpu_tuner=test-session-token")[0], 200)

    @patch("gpu_tuner.server.LoginLimiter.allow", return_value=True)
    def test_unavailable_credentials_fail_closed(self, _allow):
        path = Path(self.temp.name, "password.hash")
        original = path.read_bytes()
        for damaged in (None, b"invalid", b"\xff"):
            with self.subTest(damaged=damaged):
                path.write_bytes(original)
                _, headers, _ = self.request()
                cookie = headers["Set-Cookie"].split(";", 1)[0]
                if damaged is None:
                    path.unlink()
                else:
                    path.write_bytes(damaged)
                self.assertEqual(self.get("/app.js", cookie)[0], 401)
                self.assertEqual(self.request()[0], 503)
                self.assertEqual(self.get("/app.js", "gpu_tuner=test-session-token")[0], 200)
                path.write_bytes(original)
                self.assertEqual(self.get("/app.js", cookie)[0], 401)


class RememberLoginTests(unittest.TestCase):
    setUp = HTTPLoginTests.setUp
    stop_server = HTTPLoginTests.stop_server
    request = HTTPLoginTests.request
    get = LoginPageRoutingTests.get
    post_logout = LogoutTests.post_logout

    def test_regular_login_has_session_cookie(self):
        for body in ({"password": PASSWORD}, {"password": PASSWORD, "remember": False}):
            status, headers, _ = self.request(body=json.dumps(body).encode())
            self.assertEqual(status, 200)
            self.assertNotIn("Max-Age", headers["Set-Cookie"])

    def test_remembered_cookie_logout_and_password_reset(self):
        body = json.dumps({"password": PASSWORD, "remember": True}).encode()
        status, headers, _ = self.request(body=body)
        self.assertEqual(status, 200)
        self.assertIn("Max-Age=34560000", headers["Set-Cookie"])
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        self.assertEqual(self.get("/app.js", cookie)[0], 200)
        self.assertEqual(self.post_logout(cookie)[0], 200)
        self.assertEqual(self.get("/app.js", cookie)[0], 401)
        _, headers, _ = self.request(body=body)
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        Path(self.temp.name, "password.hash").write_text(hash_password("Replacement-Password-456"))
        self.assertEqual(self.get("/app.js", cookie)[0], 401)
        self.assertEqual(self.get("/app.js", "gpu_tuner=test-session-token")[0], 200)

    def test_remember_choice_must_be_boolean(self):
        for value in ("true", 1, None):
            with self.subTest(value=value):
                status, headers, _ = self.request(body=json.dumps({"password": PASSWORD, "remember": value}).encode())
                self.assertEqual(status, 400)
                self.assertNotIn("Set-Cookie", headers)


    def test_remembered_session_survives_handler_restart_and_renews_cookie(self):
        _, headers, _ = self.request(body=json.dumps({"password": PASSWORD, "remember": True}).encode())
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        # A new handler factory reloads sessions just as a new server process does.
        self.server.RequestHandlerClass = make_handler(None, "test-session-token", 8765)
        status, headers, _ = self.get("/app.js", cookie)
        self.assertEqual(status, 200)
        self.assertIn("Max-Age=34560000", headers["Set-Cookie"])
        status, headers, _ = self.post_logout(cookie)
        self.assertEqual(status, 200)
        self.assertIn("Max-Age=0", headers["Set-Cookie"])
        self.server.RequestHandlerClass = make_handler(None, "test-session-token", 8765)
        self.assertEqual(self.get("/app.js", cookie)[0], 401)
