#!/usr/bin/env bash
# tests/gpu-tuner-test.sh — offline tests for gpu-tuner (fake NVML, no GPU, no root).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
TUNER="$(cd "$HERE/.." && pwd)"

status=0
python3 "$HERE/gpu-tuner-test.py" || status=$?
# Across machines: protocol, hosts.json, node, links (fake node subprocesses), routing. ~20 s.
python3 "$HERE/gpu-tuner-fleet-test.py" || status=$?
# Demo mode: simulated machines through the real page, hub, node and daemon code. ~15 s.
python3 "$HERE/gpu-tuner-demo-test.py" || status=$?
# Help, (i) pop-ups and the config builder: the JSON files, the checker against the real loaders,
# and the two read-only routes. ~5 s.
python3 "$HERE/gpu-tuner-help-test.py" || status=$?

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
    # An upgrade keeps /etc/gpu-tuner/config.json: one the NEW daemon would refuse must stop the
    # install before any sudo (otherwise the restart fails and the rollback removes gpu-tunerd).
    # Run on a copy of the checkout whose ETC points at a temp dir, never at the real one.
    pre=$(mktemp -d)
    mkdir -p "$pre/c" "$pre/etc"
    cp -r "$TUNER"/{install.sh,gpu-tunerd,gpu-tuner,gpu_tuner,web,examples,icon.svg,gpu-tuner.desktop} "$pre/c/"
    cp "$TUNER"/gpu-tunerd.service "$TUNER"/gpu-tuner-ui.service "$pre/c/"
    sed -i "s|^ETC=/etc/gpu-tuner\$|ETC=$pre/etc|" "$pre/c/install.sh"
    if ! grep -qxF "ETC=$pre/etc" "$pre/c/install.sh"; then
        echo "  FAIL: could not point a copy of install.sh at a temp ETC (did the ETC= line change?)"
        status=1
    else
        printf '{"allowed_uid": 1000, "gpu_budget_w": 200000, "interval_s": 2}\n' > "$pre/etc/config.json"
        rc=0; out=$("$pre/c/install.sh" --dry-run 2>&1) || rc=$?
        if [[ $rc -ne 0 ]] && grep -q 'would refuse the existing' <<<"$out" && grep -q 'gpu_budget_w' <<<"$out" \
                && ! grep -qF '[sudo]' <<<"$out" && ! grep -q '^Before' <<<"$(sed 's/\x1b\[[0-9;]*m//g' <<<"$out")"; then
            echo "  PASS: install.sh stops before any sudo on a config.json the new daemon refuses"
        else
            echo "  FAIL: install.sh --dry-run with a refused config.json (exit $rc): $(tail -n 3 <<<"$out")"
            status=1
        fi
        printf '{"allowed_uid": 1000, "gpu_budget_w": 900, "interval_s": 2}\n' > "$pre/etc/config.json"
        rc=0; out=$("$pre/c/install.sh" --dry-run 2>&1) || rc=$?
        if [[ $rc -eq 0 ]] && grep -q 'the new gpu-tunerd accepts it' <<<"$out"; then
            echo "  PASS: install.sh --dry-run accepts a config.json the new daemon loads"
        else
            echo "  FAIL: install.sh --dry-run with a valid config.json (exit $rc): $(tail -n 3 <<<"$out")"
            status=1
        fi
    fi
    rm -rf "$pre"
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
# `style:` (bare or quoted key) catches el('div', { style: … }), which el() would turn into a
# style attribute.
SINKS='innerHTML|outerHTML|insertAdjacentHTML|document\.write|setAttribute\(.style|\bstyle["'"'"']?\s*:|\beval\(|new Function'
if grep -nE "$SINKS" "$TUNER"/web/*.js; then
    echo "  FAIL: web/*.js inserts HTML, sets a style attribute or evaluates strings (see the lines above)"
    status=1
else
    echo "  PASS: no page script uses innerHTML, HTML insertion, style attributes or eval"
fi
for f in "$TUNER"/web/*.json; do
    if ! python3 -c 'import json, sys; json.load(open(sys.argv[1]))' "$f" 2>/dev/null; then
        echo "  FAIL: $(basename "$f") is not valid JSON"
        status=1
    fi
done

# Formatting (development only: the page and the installer need no Node). Prettier checks the code
# and JSON files (.prettierignore lists what it leaves alone). Skip (not fail) until `npm ci` has
# installed the pinned copy into node_modules/.
if [[ -x "$TUNER/node_modules/.bin/prettier" ]]; then
    if fmt_out=$(cd "$TUNER" && node_modules/.bin/prettier --list-different . 2>&1); then
        echo "  PASS: prettier --check (code and JSON are formatted)"
    else
        echo "  FAIL: not formatted (run \`npm run fmt\`): $(tr '\n' ' ' <<<"$fmt_out")"
        status=1
    fi
else
    echo "  SKIP: prettier --check (run \`npm ci\` once to install the pinned Prettier)"
fi

exit $status
