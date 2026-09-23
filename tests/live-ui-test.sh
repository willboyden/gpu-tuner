#!/usr/bin/env bash
# tests/live-ui-test.sh — opt-in live UI test (headless Firefox + dry-run daemon; no root).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
status=0
python3 "$HERE/live-ui-test.py" || status=$?
exit $status
