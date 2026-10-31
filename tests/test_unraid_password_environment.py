"""Verify the initial password is removed before child processes launch."""

import os
import unittest
from unittest.mock import patch


class PasswordEnvironmentTests(unittest.TestCase):

    def test_password_removed_after_initialization(self):
        from pathlib import Path

        source = Path("container/entrypoint.py").read_text()

        # Check the actual startup sequence without executing the supervisor.
        start = source.index("def main():")
        end = source.index("    validate_existing_state()", start)
        startup = source[start:end]

        self.assertIn("initialize_password()", startup)
        self.assertIn(
            'os.environ.pop("GPU_TUNER_INITIAL_PASSWORD", None)',
            startup,
        )

        namespace = {
            "os": os,
            "initialize_data": lambda: None,
            "initialize_password": lambda: None,
        }

        with patch.dict(
            os.environ,
            {"GPU_TUNER_INITIAL_PASSWORD": "Example-Strong-Password-123"},
        ):
            exec(startup + "\n", namespace)
            namespace["main"]()

            self.assertNotIn(
                "GPU_TUNER_INITIAL_PASSWORD",
                os.environ,
            )

    def test_password_removed_even_if_initialization_fails(self):
        from pathlib import Path

        source = Path("container/entrypoint.py").read_text()
        start = source.index("def main():")
        end = source.index("    validate_existing_state()", start)
        startup = source[start:end]

        def fail():
            raise RuntimeError("Initialization failed")

        namespace = {
            "os": os,
            "initialize_data": lambda: None,
            "initialize_password": fail,
        }

        with patch.dict(
            os.environ,
            {"GPU_TUNER_INITIAL_PASSWORD": "Example-Strong-Password-123"},
        ):
            exec(startup + "\n", namespace)

            with self.assertRaises(RuntimeError):
                namespace["main"]()

            self.assertNotIn(
                "GPU_TUNER_INITIAL_PASSWORD",
                os.environ,
            )


if __name__ == "__main__":
    unittest.main()
