"""Security regression tests for the Unraid public URL setting."""

import unittest

from gpu_tuner.server import public_url_host


class PublicURLTests(unittest.TestCase):

    def test_valid_lan_urls(self):
        for url, expected in (
            ("http://192.168.1.10:8765", "192.168.1.10:8765"),
            ("http://192.168.1.10:18765", "192.168.1.10:18765"),
            ("http://localhost:8765", "localhost:8765"),
        ):
            with self.subTest(url=url):
                self.assertEqual(public_url_host(url), expected)

    def test_invalid_urls(self):
        invalid = (
            "https://192.168.1.10:8765",
            "http://example.com:8765",
            "http://192.168.1.10",
            "http://192.168.1.10:8765/admin",
            "http://user:pass@192.168.1.10:8765",
            "http://192.168.1.10:99999",
            "http://192.168.1.10:8765/?token=secret",
        )
        for url in invalid:
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    public_url_host(url)

    def test_empty_configuration(self):
        self.assertIsNone(public_url_host(""))


if __name__ == "__main__":
    unittest.main()
