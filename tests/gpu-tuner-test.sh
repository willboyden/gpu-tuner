#!/usr/bin/env bash
# tests/gpu-tuner-test.sh — offline tests for gpu-tuner (fake NVML, no GPU, no root).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
TUNER="$(cd "$HERE/.." && pwd)"

status=0
python3 "$HERE/gpu-tuner-test.py" || status=$?

# The install script's dry run must produce a plan without touching anything. It needs
# nvidia-smi; skip (not fail) on a box without one.
if command -v nvidia-smi >/dev/null 2>&1; then
    if ! "$TUNER/install.sh" --dry-run >/dev/null 2>&1; then
        echo "  FAIL: install.sh --dry-run exited non-zero"
        status=1
    else
        echo "  PASS: install.sh --dry-run"
    fi
else
    echo "  SKIP: install.sh --dry-run (no nvidia-smi here)"
fi

# The page must not need inline script/style: the server's CSP forbids both.
if grep -qE '<script>|<style>| on[a-z]+=|style="' "$TUNER/web/index.html"; then
    echo "  FAIL: web/index.html has inline script/style/handlers (blocked by the CSP)"
    status=1
else
    echo "  PASS: index.html has no inline script or style"
fi
if grep -q 'innerHTML' "$TUNER/web/app.js"; then
    echo "  FAIL: web/app.js uses innerHTML"
    status=1
else
    echo "  PASS: app.js never uses innerHTML"
fi

exit $status
