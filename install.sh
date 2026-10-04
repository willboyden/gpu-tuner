#!/usr/bin/env bash
# install.sh — install gpu-tuner (root daemon + your user's web UI), taking over from
# gpu-fan-curve.service and gpu-power-limit.service if they exist. Rolls back to them if anything fails.
#
#   ./install.sh              install / upgrade: daemon + web UI (asks for sudo)
#   ./install.sh --no-ui      daemon only — a headless machine another machine's page will manage
#   ./install.sh --node       code only, no services: a machine that is only monitored remotely
#                             (or whose GPUs have nothing NVML lets you set, e.g. GB10)
#   ./install.sh --dry-run    print every privileged command instead of running it (combines)
#   ./install.sh --uninstall  remove gpu-tuner and hand control back to the old units
#
# Every mode installs the code to /usr/local/lib/gpu-tuner, so a remote page can always reach this
# machine at the same fixed, root-owned path: /usr/local/lib/gpu-tuner/gpu-tuner node --stdio.
#
# Run as YOUR user, not with sudo: the UI unit and the launcher are installed for you, and the
# daemon's config records your uid as the one allowed to talk to it (on a remote machine, that
# must be the user the managing machine logs in as over ssh).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# The checkout path is written into the user unit and launcher (sed s|__HERE__|...|): refuse a path
# that would break that substitution or the unit's ExecStart quoting.
[[ $HERE =~ ^[A-Za-z0-9._/+-]+$ ]] || { echo "install.sh: move the checkout to a path without spaces or special characters (now: $HERE)" >&2; exit 2; }
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
UI=1

for arg in "$@"; do
    # (modes are exclusive; --no-ui and --dry-run combine with install, --dry-run with any)
    case "$arg" in
        --dry-run) DRY=1 ;;
        --uninstall) [[ $MODE == install ]] || { echo "install.sh: --uninstall and --node are separate runs" >&2; exit 2; }; MODE=uninstall ;;
        --node) [[ $MODE == install ]] || { echo "install.sh: --uninstall and --node are separate runs" >&2; exit 2; }; MODE=node ;;
        --no-ui) UI=0 ;;
        -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
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
         gpu_tuner/node.py gpu_tuner/proto.py gpu_tuner/hosts.py gpu_tuner/hub.py gpu_tuner/cli.py \
         gpu_tuner/seed.py gpu_tuner/probe.py \
         gpu-tunerd.service gpu-tuner-ui.service gpu-tuner.desktop icon.svg \
         web/index.html web/app.js web/style.css; do
    [[ -f "$HERE/$f" ]] || die "missing $HERE/$f"
done
/usr/bin/python3 -m py_compile "$HERE"/gpu_tuner/*.py || die "gpu_tuner does not compile"
# An older distro pynvml can lack a setter; the daemon then reports that control as refused by the
# driver rather than crashing, but say so up front instead of leaving it to be discovered.
MISSING=$(/usr/bin/python3 -I -c '
import pynvml
need = ("nvmlDeviceSetPowerManagementLimit", "nvmlDeviceSetFanSpeed_v2", "nvmlDeviceSetDefaultFanSpeed_v2",
        "nvmlDeviceSetGpuLockedClocks", "nvmlDeviceResetGpuLockedClocks", "nvmlDeviceSetPersistenceMode")
print(" ".join(n for n in need if not hasattr(pynvml, n)))') || die "pynvml symbol check failed"
if [[ -n $MISSING ]]; then
    step "WARNING: this pynvml has no $MISSING — those controls will be refused on this machine (a newer nvidia-ml-py fixes it)"
fi
step "python + pynvml ok, all files present"

# The code, root-owned, at the fixed path every mode shares (the daemon and remote pages run it).
install_code() {
    root install -d -m 755 "$LIB" "$LIB/gpu_tuner"
    root install -m 755 "$HERE/gpu-tunerd" "$LIB/gpu-tunerd"
    root install -m 755 "$HERE/gpu-tuner" "$LIB/gpu-tuner"
    for f in "$HERE"/gpu_tuner/*.py; do root install -m 644 "$f" "$LIB/gpu_tuner/$(basename "$f")"; done
}

if [[ $MODE == node ]]; then
    log "Node only (no services)"
    if [[ -f $UNIT ]]; then
        die "gpu-tunerd is installed here; upgrade it (and the code) with ./install.sh or ./install.sh --no-ui instead"
    fi
    install_code
    if [[ $DRY -eq 1 ]]; then log "Dry run: nothing was changed"; exit 0; fi
    "$LIB/gpu-tuner" node --check || die "the installed node could not read the GPUs"
    log "Installed. A managing machine can now reach this one with:"
    step "ssh <this-host> $LIB/gpu-tuner node --stdio     (restrict its key to that command: see README)"
    exit 0
fi

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
# gpu-power-limit (if this machine has it) runs its ExecStop, which restores factory defaults for
# the second or two until gpu-tunerd starts and applies this file. The budget defaults to the sum
# of those caps — whatever THIS machine already runs at, not a number from anyone else's circuit —
# or null if no card reports a settable cap (gpu_tuner/seed.py).
SEED=$(mktemp)
trap 'rm -f "$SEED" "$SEED".*' EXIT
nvidia-smi --query-gpu=uuid,power.limit --format=csv,noheader,nounits > "$SEED.csv" \
    || die "nvidia-smi query failed"
# Fans keep the driver's curve on a new machine; only a machine that already ran a
# gpu-fan-curve service starts on the lab curve, so taking over never changes its fan behaviour.
FAN_MODE=auto
[[ ${WAS[gpu-fan-curve.service]} == absent ]] || FAN_MODE=curve
/usr/bin/python3 "$HERE/gpu_tuner/seed.py" --fan-mode "$FAN_MODE" "$SEED.csv" "$SEED" "$SEED.budget" \
    || die "could not build the seed state"
rm -f "$SEED.csv"
BUDGET_W=$(cat "$SEED.budget")
[[ $BUDGET_W == null || $BUDGET_W =~ ^[0-9]+$ ]] || die "seed produced an unexpected budget: $BUDGET_W"
# /var/lib/gpu-tuner is root-only (0700), so only root can tell whether a saved state.json exists:
# testing it as yourself always says "missing", and an upgrade would then reseed it — losing your
# saved fan curves and clock caps. A dry run checks only if sudo needs no password right now.
HAVE_STATE=unknown
if [[ $DRY -eq 0 ]] || sudo -n true 2>/dev/null; then
    if sudo test -e "$STATE"; then HAVE_STATE=yes; else HAVE_STATE=no; fi
fi
case $HAVE_STATE in
    yes) step "$STATE exists: keeping your saved settings, seed unused" ;;
    no)  step "will seed $STATE with the caps above" ;;
    *)   step "dry run without cached sudo: can't see into $(dirname "$STATE"); a real run keeps $STATE if it exists" ;;
esac

CONFIG_JSON=$(printf '{\n  "allowed_uid": %d,\n  "gpu_budget_w": %s,\n  "interval_s": 2\n}\n' "$(id -u)" "$BUDGET_W")

log "Installing (sudo)"
INSTALLED=0
rollback() {   # called from the EXIT trap with the script's exit status as $1
    local rc=$1
    if [[ $rc -ne 0 && $INSTALLED -eq 1 && $DRY -eq 0 ]]; then
        log "FAILED (exit $rc) — rolling back to the previous units"
        sudo systemctl disable --now gpu-tunerd.service 2>/dev/null || true
        sudo rm -f "$UNIT"; sudo rm -rf "$LIB"; sudo systemctl daemon-reload
        if [[ $UI -eq 1 ]]; then         # a --no-ui run never touched the UI unit: leave any it found
            systemctl --user disable --now gpu-tuner-ui.service 2>/dev/null || true
            rm -f "$USER_UNIT_DIR/gpu-tuner-ui.service" "$APPS_DIR/gpu-tuner.desktop"
            systemctl --user daemon-reload 2>/dev/null || true
        fi
        for u in "${OLD_UNITS[@]}"; do
            [[ ${WAS[$u]} == enabled* ]] && sudo systemctl enable --now "$u" || true
        done
        echo "  restored: $(for u in "${OLD_UNITS[@]}"; do printf '%s=%s ' "$u" "$(unit_state "$u")"; done)"
    fi
    rm -f "$SEED" "$SEED.budget" "$SEED.unit" "$SEED.desktop" "$SEED.root-unit"
}
trap 'rollback $?' EXIT

install_code
root install -d -m 755 "$ETC"
if [[ ! -f $ETC/config.json ]]; then
    if [[ $BUDGET_W == null ]]; then
        step "writing $ETC/config.json (allowed_uid=$(id -u), no power budget: no card here reports a settable cap)"
    else
        step "writing $ETC/config.json (allowed_uid=$(id -u), gpu_budget_w=$BUDGET_W — the sum of the caps above; raise it any time from the page)"
    fi
    if [[ $DRY -eq 1 ]]; then printf '%s\n' "$CONFIG_JSON" | sed 's/^/      /'; else printf '%s\n' "$CONFIG_JSON" | sudo tee "$ETC/config.json" >/dev/null; fi
    root chmod 644 "$ETC/config.json"
else
    step "$ETC/config.json exists: keeping it (including its gpu_budget_w)"
fi
root install -d -m 700 "$(dirname "$STATE")"
if [[ $HAVE_STATE != yes ]]; then
    [[ $HAVE_STATE == no ]] || step "(only if $STATE does not exist yet:)"
    root install -m 600 "$SEED" "$STATE"
fi
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

if [[ $UI -eq 1 ]]; then
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
else
    step "--no-ui: skipping the web UI unit and launcher (manage this machine from another machine's page)"
fi

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
budget = "no budget" if d["budget_w"] is None else "budget %s/%s W" % (d["budget_used_w"], d["budget_w"])
print("  daemon ok · %s · %s" % (budget,
      " · ".join("%s %s fan=%s" % (g["profile"]["label"] if g["profile"] else g["name"],
                                   "-" if g["settings"]["power_w"] is None else "%s W" % g["settings"]["power_w"],
                                   "-" if g["fan"]["fan_pct"] is None else "%s%%" % g["fan"]["fan_pct"]) for g in d["gpus"])))
for w in d["warnings"]: print("  WARNING " + w)
PY
) || die "the daemon did not answer on its socket"
echo "$STATUS"
step "limits in force: $(nvidia-smi --query-gpu=name,power.limit --format=csv,noheader | tr '\n' ';' || echo unavailable)"
if [[ $UI -eq 0 ]]; then
    log "Installed (daemon only). A managing machine reaches this one with:"
    step "ssh <this-host> $LIB/gpu-tuner node --stdio     (restrict its key to that command: see README)"
    exit 0
fi
systemctl --user is-active --quiet gpu-tuner-ui.service || { systemctl --user status gpu-tuner-ui --no-pager; die "the UI service is not running"; }
# "active" isn't enough: a unit sandboxing property once made the page unable to reach the daemon
# socket, and the page came up silently monitor-only. Ask the page itself what it sees. The session
# token is read from your own config dir and never printed.
/usr/bin/python3 - "$HOME/.config/gpu-tuner/token" <<'CHECK' || die "the page is running but does not reach the control daemon (journalctl --user -u gpu-tuner-ui)"
import http.client, json, os, sys, time
deadline = time.time() + 25
while True:
    try:
        with open(sys.argv[1]) as f:
            tok = f.read().strip()
        c = http.client.HTTPConnection("127.0.0.1", 8765, timeout=3)
        c.request("GET", "/api/state", headers={"Host": "127.0.0.1:8765", "Cookie": "gpu_tuner=" + tok})
        st = json.loads(c.getresponse().read())
        local = [h for h in st["hosts"] if h["id"] == "local"]
        if not local:
            print("  page check skipped: hosts.json lists no local machine")
            sys.exit(0)
        h = local[0]
        if h["conn"] == "up" and h["daemon"] is not None:
            if h["daemon_error"]:
                sys.exit("  the page says this machine's daemon is " + h["daemon_error"])
            print("  the page reaches the daemon: controls on")
            sys.exit(0)
    except (OSError, ValueError, KeyError):
        pass
    if time.time() > deadline:
        sys.exit("  the page did not report this machine within 25 s")
    time.sleep(1)
CHECK
step "UI on http://127.0.0.1:8765/"

log "Installed. Open it with:  $HERE/gpu-tuner open   (or 'GPU Tuner' in the app launcher)"
step "logs: journalctl -u gpu-tunerd -f        settings: $STATE        budget: $ETC/config.json"
step "old units are disabled but still installed; '$0 --uninstall' brings them back"
