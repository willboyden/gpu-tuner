#!/usr/bin/env bash
# tests/gpu-tuner-test.sh — offline tests for gpu-tuner (fake NVML, no GPU, no root).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
TUNER="$(cd "$HERE/.." && pwd)"

status=0
python3 "$HERE/gpu-tuner-test.py" || status=$?
# Across machines: protocol, hosts.json, node, links (fake node subprocesses), routing. ~20 s.
python3 "$HERE/gpu-tuner-fleet-test.py" || status=$?

# The install script's dry run must produce a plan without touching anything, in every mode. It
# needs nvidia-smi; skip (not fail) on a box without one.
if command -v nvidia-smi >/dev/null 2>&1; then
    for mode in "" "--no-ui" "--node"; do
        # --node refuses on a machine that already has the daemon installed (upgrade it instead)
        if [[ $mode == --node && -f /etc/systemd/system/gpu-tunerd.service ]]; then
            if "$TUNER/install.sh" --dry-run --node >/dev/null 2>&1; then
                echo "  FAIL: install.sh --dry-run --node should refuse where gpu-tunerd is installed"
                status=1
            else
                echo "  PASS: install.sh --dry-run --node refuses where gpu-tunerd is installed"
            fi
            continue
        fi
        if ! "$TUNER/install.sh" --dry-run $mode >/dev/null 2>&1; then
            echo "  FAIL: install.sh --dry-run $mode exited non-zero"
            status=1
        else
            echo "  PASS: install.sh --dry-run $mode"
        fi
    done
    out=$("$TUNER/install.sh" --dry-run --no-ui 2>&1) || true
    if grep -q 'gpu-tuner-ui.service' <<<"$out"; then
        echo "  FAIL: install.sh --dry-run --no-ui still installs the UI unit"
        status=1
    elif ! grep -q 'gpu-tunerd.service' <<<"$out"; then
        echo "  FAIL: install.sh --dry-run --no-ui does not install the daemon unit either"
        status=1
    else
        echo "  PASS: --no-ui installs the daemon unit and no UI unit"
    fi
    if "$TUNER/install.sh" --dry-run --node --uninstall >/dev/null 2>&1 \
            || "$TUNER/install.sh" --dry-run --uninstall --node >/dev/null 2>&1; then
        echo "  FAIL: --node with --uninstall should be refused in either order"
        status=1
    else
        echo "  PASS: --node and --uninstall refuse to combine, in either order"
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
