"""Seed a FRESH install's state.json and power budget from the caps in force right now.

install.sh runs this (as you, not root) on `nvidia-smi --query-gpu=uuid,power.limit
--format=csv,noheader,nounits` output, then installs the result with sudo. Only used when no
state.json exists yet: an upgrade keeps the saved settings.

  * A card whose power limit nvidia-smi can't report ("[N/A]", "[Not Supported]" — GB10 and other
    parts whose power is firmware-managed) gets no power_w at all: the daemon then adopts whatever
    NVML reports live, or leaves power alone if NVML reports nothing.
  * The budget is the sum of the reported caps, or None (JSON null) if no card reported one.
  * Fans start on the driver's own curve ("auto") unless this machine already ran a
    gpu-fan-curve service, so a new machine's fans don't change behaviour until you choose a curve.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys


def parse_limit(text):
    """nvidia-smi's power.limit cell -> whole watts, or None if it isn't a positive number."""
    try:
        v = float(str(text).strip())
    except ValueError:
        return None
    return int(round(v)) if math.isfinite(v) and v > 0 else None


def build(rows, fan_mode="auto"):
    """rows: [uuid, power.limit] lists. Returns (state_doc, budget_w_or_None)."""
    if fan_mode not in ("auto", "curve"):
        raise ValueError("fan_mode must be auto or curve")
    gpus = {}
    for row in rows:
        if len(row) < 2 or not row[0].strip():
            continue
        # No curve: the daemon gives each card its own default, fitted under that card's
        # thresholds (nvidia-smi here can't tell us those).
        entry = {"clock_cap_mhz": None, "fan": {"mode": fan_mode, "manual_pct": 60}}
        watts = parse_limit(row[1])
        if watts is not None:
            entry["power_w"] = watts
        gpus[row[0].strip()] = entry
    total = sum(g.get("power_w", 0) for g in gpus.values())
    return {"version": 1, "gpus": gpus}, (total or None)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="seed.py", description=__doc__.split("\n")[0])
    ap.add_argument("csv_in")
    ap.add_argument("state_out")
    ap.add_argument("budget_out")
    ap.add_argument("--fan-mode", choices=("auto", "curve"), default="auto")
    args = ap.parse_args(argv)
    with open(args.csv_in, newline="") as f:
        doc, budget = build(csv.reader(f), args.fan_mode)
    if not doc["gpus"]:
        sys.exit("seed.py: no GPUs in the nvidia-smi output")
    with open(args.state_out, "w") as f:
        json.dump(doc, f, indent=1)
    with open(args.budget_out, "w") as f:
        f.write("null" if budget is None else str(budget))
    caps = ", ".join(f"{u[:12]}… {g['power_w']} W" if "power_w" in g else f"{u[:12]}… no settable cap"
                     for u, g in doc["gpus"].items())
    print(f"  seed: {caps} ({'no budget' if budget is None else str(budget) + ' W total'}); "
          f"fans start on {'the driver curve' if args.fan_mode == 'auto' else 'the lab curve'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
