#!/usr/bin/env bash
# install.sh — install gpu-tuner (root daemon + your user's web UI), taking over from
# gpu-fan-curve.service and gpu-power-limit.service. Rolls back to those two if anything fails.
#
#   ops/gpu-tuner/install.sh              install / upgrade (asks for sudo)
#   ops/gpu-tuner/install.sh --dry-run    print every privileged command instead of running it
#   ops/gpu-tuner/install.sh --uninstall  remove gpu-tuner and hand control back to the old units
#
# Run as YOUR user, not with sudo: the UI unit and the launcher are installed for you, and the
# daemon's config records your uid as the one allowed to talk to it.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
LIB=/usr/local/lib/gpu-tuner
ETC=/etc/gpu-tuner
STATE=/var/lib/gpu-tuner/state.json
SOCK=/run/gpu-tuner/control.sock
UNIT=/etc/systemd/system/gpu-tunerd.service
# Fixed paths, not $XDG_*: a snap-launched terminal (VS Code here) points those at ~/snap/…
USER_UNIT_DIR="$HOME/.config/systemd/user"
APPS_DIR="$HOME/.local/share/applications"
OLD_UNITS=(gpu-fan-curve.service gpu-power-limit.service)
MODE=install
DRY=0

for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY=1 ;;
        --uninstall) MODE=uninstall ;;
        -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
        *) echo "install.sh: unknown option $arg" >&2; exit 2 ;;
    esac
done

if [[ $EUID -eq 0 ]]; then
    echo "install.sh: run this as your own user (it uses sudo for the root parts)" >&2
    exit 2
fi

log()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
step() { printf '  %s\n' "$*"; }
die()  { printf 'install.sh: %s\n' "$*" >&2; exit 1; }

# Every privileged command goes through here so --dry-run can show the whole plan.
root() {
    if [[ $DRY -eq 1 ]]; then
        printf '  [sudo]'; printf ' %q' "$@"; printf '\n'
    else
        sudo "$@"
    fi
}
# `systemctl --user` is ours, no sudo, but the dry run should not touch it either.
me() {
    if [[ $DRY -eq 1 ]]; then
        printf '  [user]'; printf ' %q' "$@"; printf '\n'
    else
        "$@"
    fi
}

unit_state() {   # "enabled active" style summary for an old unit, "absent" if not installed
    local u="$1" en ac
    en=$(systemctl is-enabled "$u" 2>/dev/null || true)
    ac=$(systemctl is-active "$u" 2>/dev/null || true)
    if [[ -z $en || $en == not-found ]]; then echo absent; else echo "$en $ac"; fi
}

# ── preflight (no root) ─────────────────────────────────────────────────────────────────────
log "Preflight"
command -v nvidia-smi >/dev/null || die "nvidia-smi not found"
[[ -x /usr/bin/python3 ]] || die "/usr/bin/python3 missing (the daemon runs on the system python)"
/usr/bin/python3 -I -c 'import pynvml' 2>/dev/null \
    || die "the system python cannot import pynvml (apt install python3-pynvml); the daemon runs as root with -I and sees no user site-packages"
for f in gpu-tunerd gpu-tuner gpu_tuner/safety.py gpu_tuner/nvml.py gpu_tuner/daemon.py gpu_tuner/server.py \
         gpu-tunerd.service gpu-tuner-ui.service gpu-tuner.desktop icon.svg \
         web/index.html web/app.js web/style.css; do
    [[ -f "$HERE/$f" ]] || die "missing $HERE/$f"
done
/usr/bin/python3 -m py_compile "$HERE"/gpu_tuner/*.py || die "gpu_tuner does not compile"
step "python + pynvml ok, all files present"

if [[ $MODE == uninstall ]]; then
    log "Uninstall"
    step "stopping gpu-tunerd (its ExecStopPost hands the fans back to the driver; power caps stay as they are)"
    me systemctl --user disable --now gpu-tuner-ui.service 2>/dev/null || true
    root systemctl disable --now gpu-tunerd.service || true
    root rm -f "$UNIT"
    root rm -rf "$LIB"
    root systemctl daemon-reload
    me rm -f "$USER_UNIT_DIR/gpu-tuner-ui.service" "$APPS_DIR/gpu-tuner.desktop"
    me systemctl --user daemon-reload
    step "config ($ETC) and saved settings ($STATE) are kept; remove them by hand if you want a clean slate"
    for u in "${OLD_UNITS[@]}"; do
        if [[ -f /etc/systemd/system/$u ]]; then
            step "re-enabling $u"
            root systemctl enable --now "$u"
        fi
    done
    log "Done. Verify:"
    step "systemctl status gpu-fan-curve gpu-power-limit --no-pager"
    step "nvidia-smi --query-gpu=name,power.limit,fan.speed --format=csv"
    exit 0
fi

# ── install ─────────────────────────────────────────────────────────────────────────────────
log "Before"
declare -A WAS
for u in "${OLD_UNITS[@]}"; do WAS[$u]=$(unit_state "$u"); step "$u: ${WAS[$u]}"; done
step "gpu-tunerd.service: $(unit_state gpu-tunerd.service)"

# Seed the daemon's state with the caps in force RIGHT NOW so the takeover keeps them. Stopping
# gpu-power-limit runs its ExecStop, which restores factory defaults (Workstation card -> 600 W)
# for the second or two until gpu-tunerd starts and applies this file.
SEED=$(mktemp)
trap 'rm -f "$SEED"' EXIT
nvidia-smi --query-gpu=uuid,power.limit --format=csv,noheader,nounits > "$SEED.csv" \
    || die "nvidia-smi query failed"
/usr/bin/python3 - "$SEED.csv" "$SEED" "$SEED.budget" <<'PY' || die "could not build the seed state"
import csv, json, sys
gpus = {}
with open(sys.argv[1]) as f:
    for uuid, limit in csv.reader(f):
        gpus[uuid.strip()] = {"power_w": int(round(float(limit))), "clock_cap_mhz": None,
                              "fan": {"mode": "curve", "curve": [[30, 30], [45, 45], [55, 60], [65, 75], [72, 88], [80, 100]],
                                      "manual_pct": 60}}
if not gpus:
    sys.exit("no GPUs in nvidia-smi output")
json.dump({"version": 1, "gpus": gpus}, open(sys.argv[2], "w"), indent=1)
# Portable default: whatever the combined caps already are RIGHT NOW, on THIS machine's cards —
# not a number this lab derived from its own circuit. safety.DEFAULT_GPU_BUDGET_W (750) is only
# the last-resort fallback if config.json is ever hand-deleted with no live GPUs to seed from.
total = sum(g["power_w"] for g in gpus.values())
open(sys.argv[3], "w").write(str(total))
print("  seed: " + ", ".join(f"{u[:12]}… {g['power_w']} W" for u, g in gpus.items()) + f" ({total} W total)")
PY
rm -f "$SEED.csv"
BUDGET_W=$(cat "$SEED.budget")
if [[ ! -f "$STATE" ]]; then step "will seed $STATE with the caps above and the lab fan curve"; else step "$STATE exists: keeping your saved settings, seed unused"; fi

CONFIG_JSON=$(printf '{\n  "allowed_uid": %d,\n  "gpu_budget_w": %d,\n  "interval_s": 2\n}\n' "$(id -u)" "$BUDGET_W")

log "Installing (sudo)"
INSTALLED=0
rollback() {   # called from the EXIT trap with the script's exit status as $1
    local rc=$1
    if [[ $rc -ne 0 && $INSTALLED -eq 1 && $DRY -eq 0 ]]; then
        log "FAILED (exit $rc) — rolling back to the previous units"
        sudo systemctl disable --now gpu-tunerd.service 2>/dev/null || true
        sudo rm -f "$UNIT"; sudo rm -rf "$LIB"; sudo systemctl daemon-reload
        systemctl --user disable --now gpu-tuner-ui.service 2>/dev/null || true
        rm -f "$USER_UNIT_DIR/gpu-tuner-ui.service" "$APPS_DIR/gpu-tuner.desktop"
        systemctl --user daemon-reload 2>/dev/null || true
        for u in "${OLD_UNITS[@]}"; do
            [[ ${WAS[$u]} == enabled* ]] && sudo systemctl enable --now "$u" || true
        done
        echo "  restored: $(for u in "${OLD_UNITS[@]}"; do printf '%s=%s ' "$u" "$(unit_state "$u")"; done)"
    fi
    rm -f "$SEED" "$SEED.budget" "$SEED.unit" "$SEED.desktop" "$SEED.root-unit"
}
trap 'rollback $?' EXIT

root install -d -m 755 "$LIB" "$LIB/gpu_tuner"
root install -m 755 "$HERE/gpu-tunerd" "$LIB/gpu-tunerd"
for f in "$HERE"/gpu_tuner/*.py; do root install -m 644 "$f" "$LIB/gpu_tuner/$(basename "$f")"; done
root install -d -m 755 "$ETC"
if [[ ! -f $ETC/config.json ]]; then
    step "writing $ETC/config.json (allowed_uid=$(id -u), gpu_budget_w=$BUDGET_W — the sum of the caps above; raise it any time from the page)"
    if [[ $DRY -eq 1 ]]; then printf '%s\n' "$CONFIG_JSON" | sed 's/^/      /'; else printf '%s\n' "$CONFIG_JSON" | sudo tee "$ETC/config.json" >/dev/null; fi
    root chmod 644 "$ETC/config.json"
else
    step "$ETC/config.json exists: keeping it (including its gpu_budget_w)"
fi
root install -d -m 700 "$(dirname "$STATE")"
if [[ ! -f "$STATE" ]]; then root install -m 600 "$SEED" "$STATE"; fi
sed "s|__HERE__|$HERE|g" "$HERE/gpu-tunerd.service" > "$SEED.root-unit"
root install -m 644 "$SEED.root-unit" "$UNIT"
root systemctl daemon-reload
INSTALLED=1

step "stopping the old units (gpu-fan-curve hands the fans back; gpu-power-limit's ExecStop restores factory caps briefly)"
for u in "${OLD_UNITS[@]}"; do
    [[ ${WAS[$u]} == absent ]] || root systemctl disable --now "$u"
done
step "starting gpu-tunerd"
# enable --now no-ops on an already-active unit: an upgrade that changed the unit file (or the
# code it runs) would otherwise keep running the OLD process. restart always picks up the new one.
root systemctl enable gpu-tunerd.service
root systemctl restart gpu-tunerd.service

log "Installing the UI for $(id -un)"
# Both templates carry __HERE__ so the UI runs from wherever this checkout lives.
sed "s|__HERE__|$HERE|g" "$HERE/gpu-tuner-ui.service" > "$SEED.unit"
sed "s|__HERE__|$HERE|g" "$HERE/gpu-tuner.desktop" > "$SEED.desktop"
me install -d -m 700 "$USER_UNIT_DIR"
me install -m 644 "$SEED.unit" "$USER_UNIT_DIR/gpu-tuner-ui.service"
me systemctl --user daemon-reload
# Same reasoning as gpu-tunerd above: on an upgrade, enable --now would leave the OLD gpu-tuner
# code running on :8765 rather than picking up whatever changed in this checkout.
me systemctl --user enable gpu-tuner-ui.service
me systemctl --user restart gpu-tuner-ui.service
me install -d "$APPS_DIR"
me install -m 644 "$SEED.desktop" "$APPS_DIR/gpu-tuner.desktop"

if [[ $DRY -eq 1 ]]; then
    log "Dry run: nothing was changed"
    exit 0
fi

log "Verifying"
sleep 3
systemctl is-active --quiet gpu-tunerd.service || { journalctl -u gpu-tunerd -n 20 --no-pager; die "gpu-tunerd is not running"; }
[[ -S $SOCK ]] || die "daemon socket $SOCK missing"
[[ -r $SOCK && -w $SOCK ]] || die "daemon socket is not accessible to $(id -un) (check allowed_uid in $ETC/config.json)"
STATUS=$(/usr/bin/python3 - "$SOCK" <<'PY'
import json, socket, sys
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); s.settimeout(5); s.connect(sys.argv[1])
s.sendall(b'{"op":"status"}\n'); buf = b""
while not buf.endswith(b"\n"):
    c = s.recv(65536)
    if not c: break
    buf += c
d = json.loads(buf)
if not d.get("ok"): sys.exit("daemon status: " + str(d.get("error")))
print("  daemon ok · budget %d/%d W · %s" % (d["budget_used_w"], d["budget_w"],
      " · ".join("%s %s W fan=%s%%" % (g["profile"]["label"] if g["profile"] else g["name"], g["settings"]["power_w"], g["fan"]["fan_pct"]) for g in d["gpus"])))
for w in d["warnings"]: print("  WARNING " + w)
PY
) || die "the daemon did not answer on its socket"
echo "$STATUS"
step "limits in force: $(nvidia-smi --query-gpu=name,power.limit --format=csv,noheader | tr '\n' ';' || echo unavailable)"
systemctl --user is-active --quiet gpu-tuner-ui.service || { systemctl --user status gpu-tuner-ui --no-pager; die "the UI service is not running"; }
step "UI on http://127.0.0.1:8765/"

log "Installed. Open it with:  $HERE/gpu-tuner open   (or 'GPU Tuner' in the app launcher)"
step "logs: journalctl -u gpu-tunerd -f        settings: $STATE        budget: $ETC/config.json"
step "old units are disabled but still installed; '$0 --uninstall' brings them back"
