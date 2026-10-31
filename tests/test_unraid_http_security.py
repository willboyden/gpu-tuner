"""HTTP Host and Origin security tests, without a GPU daemon."""

import http.client
import os
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from gpu_tuner.server import make_handler


class HTTPSecurityTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(
            os.environ,
            {
                "GPU_TUNER_PUBLIC_URL": "http://192.168.1.10:18765",
                "GPU_TUNER_LAN_IP": "",
            },
        )
        self.env.start()

        # The handler needs no Hub methods for rejected requests.
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

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.env.stop()

    def request(self, method, host, origin=None, cookie=None):
        headers = {"Host": host}
        if origin is not None:
            headers["Origin"] = origin
        if cookie is not None:
            headers["Cookie"] = cookie

        if method == "POST":
            headers["Content-Type"] = "application/json"

        conn = http.client.HTTPConnection(
            "127.0.0.1",
            self.server.server_port,
            timeout=5,
        )
        try:
            conn.request(method, "/api/apply", body=b"{}", headers=headers)
            response = conn.getresponse()
            response.read()
            return response.status
        finally:
            conn.close()

    def test_public_host_passes_host_gate(self):
        # No cookie: accepted Host reaches authentication and returns 401.
        self.assertEqual(
            self.request("GET", "192.168.1.10:18765"),
            401,
        )

    def test_unexpected_host_rejected(self):
        self.assertEqual(
            self.request("GET", "attacker.example:18765"),
            421,
        )

    def test_cross_origin_post_rejected(self):
        self.assertEqual(
            self.request(
                "POST",
                "192.168.1.10:18765",
                origin="http://attacker.example",
                cookie="gpu_tuner=test-session-token",
            ),
            403,
        )

    def test_valid_origin_reaches_request_validation(self):
        # Valid Host, cookie, and Origin should pass security gates.
        # /api/apply with an empty body is rejected at request validation.
        status = self.request(
            "POST",
            "192.168.1.10:18765",
            origin="http://192.168.1.10:18765",
            cookie="gpu_tuner=test-session-token",
        )
        self.assertNotIn(status, (401, 403, 421))


if __name__ == "__main__":
    unittest.main()
