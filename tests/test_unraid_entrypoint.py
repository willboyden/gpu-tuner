"""Tests for Unraid container initialization and state protection."""

import importlib.util
import json
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = ROOT / "container" / "entrypoint.py"


def load_entrypoint():
    loader = SourceFileLoader("unraid_entrypoint", str(ENTRYPOINT))
    spec = importlib.util.spec_from_loader("unraid_entrypoint", loader)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class StateValidationTests(unittest.TestCase):
    def setUp(self):
        self.module = load_entrypoint()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.json"

        self.valid = {
            "version": 1,
            "gpus": {
                "GPU-TEST-ONLY": {
                    "power_w": 130,
                    "clock_cap_mhz": None,
                    "fan": {
                        "mode": "auto",
                        "curve": [[40, 40], [80, 100]],
                        "manual_pct": 60,
                    },
                }
            },
        }

    def validate(self, data):
        self.path.write_text(json.dumps(data))
        with patch("builtins.open", wraps=open):
            # The entrypoint currently uses a fixed /data path.
            # Replace only the state path in the function's code for testing.
            source = (
                ENTRYPOINT.read_text()
                .replace(
                    'path = "/data/state/state.json"',
                    f'path = {str(self.path)!r}',
                    1,
                )
            )
            namespace = {"__name__": "unraid_test"}
            exec(compile(source, str(ENTRYPOINT), "exec"), namespace)
            namespace["validate_existing_state"]()

    def test_valid_state(self):
        self.validate(self.valid)

    def test_invalid_top_level_structure(self):
        with self.assertRaises(RuntimeError):
            self.validate({"version": 1, "gpus": []})

    def test_invalid_gpu_power(self):
        self.valid["gpus"]["GPU-TEST-ONLY"]["power_w"] = -10
        with self.assertRaises(RuntimeError):
            self.validate(self.valid)

    def test_invalid_fan_mode(self):
        self.valid["gpus"]["GPU-TEST-ONLY"]["fan"]["mode"] = "unsafe"
        with self.assertRaises(RuntimeError):
            self.validate(self.valid)

    def test_invalid_curve_point(self):
        self.valid["gpus"]["GPU-TEST-ONLY"]["fan"]["curve"] = [[40, "fast"]]
        with self.assertRaises(RuntimeError):
            self.validate(self.valid)


if __name__ == "__main__":
    unittest.main()
