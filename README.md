# gpu-tuner — power caps, fan curves and clock caps for every NVIDIA card on the box, with a GUI

A local web page (`http://127.0.0.1:8765/`) that shows every detected card live — temperature,
fan, power, clocks, utilization, VRAM, encode/decode, ECC, PCIe link width, persistence mode,
what is limiting the clock, what is resident — with six hours of charts, and lets you change the
three things that are safe to change on any card, whatever model or how many there are:

| Knob | Range |
|---|---|
| **Power limit** | that card's own NVML min–max, narrowed by a known profile if one exists |
| **Fan** | curve / fixed / driver default, over a safety floor anchored to THAT card's own slowdown threshold |
| **Core clock cap** | 1,000 MHz to that card's top supported clock, or off |

Plus the one thing no single card's own range tells you: a **combined GPU power budget**,
editable from the page itself. A fresh install seeds it to whatever the caps already in force add
up to; raising it later is instant. Exceeding it is a soft limit, not a wall — the page warns you
with the numbers and asks you to confirm, then applies it (still bounded by each card's own
hardware range, which is never overridable).

This lab's own two RTX PRO 6000 cards (`GPU-ce03a2bc…` Max-Q, `GPU-62d53056…` Workstation) get
hand-measured profiles with informed presets (see `PROFILES` in `gpu_tuner/safety.py`); any other
card is discovered generically from live NVML and can be lowered, but not raised above its
factory default, until someone adds a profile for it.

It replaces `gpu-fan-curve.service` and `gpu-power-limit.service` (both are disabled by the
installer, kept on disk, and restored by `--uninstall`).

## Install

```bash
ops/gpu-tuner/install.sh --dry-run   # prints every privileged command, changes nothing
ops/gpu-tuner/install.sh             # asks for sudo; rolls back to the old units on any failure
ops/gpu-tuner/gpu-tuner open         # signs your browser in (also "GPU Tuner" in the app launcher)
```

Also an **upgrade** path: run it again after pulling changes. It's idempotent (existing
`config.json`/`state.json` are kept, not reseeded) and always `restart`s both units so an updated
daemon or UI actually takes effect, not just gets installed alongside a still-running old one.

The takeover keeps the caps currently in force (the installer seeds the daemon's state from
`nvidia-smi` first, and seeds `gpu_budget_w` to their sum) and puts the fans on the lab curve —
the same curve the old daemon ran. One caveat, stated so nobody is surprised: stopping
`gpu-power-limit` runs its `ExecStop`, which restores factory caps for the second or two before
`gpu-tunerd` starts and re-applies the seeded ones.

Without the install the page still runs in **monitor-only** mode (`gpu-tuner serve`): every
reading and chart is live, each card's envelope is shown, the controls are locked.

## What is safe, and why these limits

Every write is validated by the root daemon against `gpu_tuner/safety.py`, never by the page.
The file lives in a root-owned copy under `/usr/local/lib/gpu-tuner`, so nothing running as
the desktop user can widen a limit.

- **Power.** The card's own NVML range, narrowed by its profile if one exists, then the
  combined budget. Lowering is always allowed (an over-budget state must be walkable back
  down); raising past the budget needs an explicit confirm click, which the page's `set_power`
  request carries as `confirm_override`. This lab's two cards' presets carry the measured
  numbers from `notes/findings/gpu-fan-curve-and-power-caps.md` so a choice is informed, not a
  guess. A card with no profile can be lowered but never raised above its factory default.
  The combined budget itself is a `set_budget` request, persisted to `config.json` by the
  daemon — nothing enforces it beyond that one comparison, so raising it is instant and never
  needs a restart.
- **Fans.** Any curve or fixed speed is evaluated as `max(requested, floor(temp))`. The floor's
  shape was measured on this lab's own cards (61 °C @ 43%, 78 °C @ 46%, 80 °C @ 59% — never
  lazier than their stock driver curve), but its "100% by" temperature is rescaled per card from
  THAT card's own NVML slowdown threshold (`safety.safety_floor_for`), not a fixed number — on
  this lab's cards that lands at **85 °C**, 10 °C under their 95 °C slowdown (92/93 °C T.Limit,
  98 °C shutdown), which is where the constant came from originally. A curve must rise
  monotonically and end at 100% at or below that card's own floor ceiling. Hysteresis: up at
  once, down only after 2 °C of cooling.
- **Clock cap.** `SetGpuLockedClocks(min, cap)` — it can only slow the card. The value snaps
  down to a supported clock step. 1,000 MHz floor is a fat-finger guard, not a safety limit.
- **Failure policy: always toward the driver's own fan control.** SIGTERM → fans handed back
  in `finally`. Crash or watchdog kill → the unit's `ExecStopPost` hands them back. A refused
  fan write or an unreadable temperature → that card goes to driver control and is retried in
  30 s. Fan speeds are re-asserted every 30 s (suspend/resume and GPU resets silently hand the
  fans back). Power caps are re-checked every 10 s and re-applied if anything else changed
  them. Power caps are **left in place** on exit, on purpose: stopping a service must not
  uncap the Workstation card to 600 W.

### Left out on purpose (also listed on the page)

Core/memory **clock offsets** (no range is known-stable across arbitrary hardware, and an
unstable offset corrupts results before it crashes), thermal-limit / target-temperature changes
(unsupported on most cards; NVML reports the acoustic thresholds unsupported), *toggling* ECC,
MIG or compute mode (ECC's mode and error counts ARE shown, read-only, when a card supports
them), turning persistence mode off (its actual on/off state is shown, read-only — the daemon
holds it on), chassis fans (BMC-owned on this lab's board; the PWM registers do nothing here,
though that varies by board elsewhere).

## How it is built

```
gpu-tunerd  (root, systemd)      gpu-tuner serve  (you, systemd --user)      browser
 owns every NVML write   <--unix socket, SO_PEERCRED-->   NVML reads, history,   <--127.0.0.1:8765-->
 fan loop, validation                                     static page, /api/*
```

| File | Role |
|---|---|
| `gpu_tuner/safety.py` | The envelope: profiles, budget (soft, overridable), floor (per-card), curve rules. Pure functions, unit-tested. |
| `gpu_tuner/nvml.py` | Thin NVML wrapper, generic over however many GPUs are present; `dry_run=True` records writes instead of making them. |
| `gpu_tuner/daemon.py` | Root daemon: socket server, fan loop, state + config persistence, startup/exit policy. |
| `gpu_tuner/server.py` | UI server: sampler (1 Hz, 6 h ring), `/api/state`, `/api/history`, `/api/apply`. |
| `web/` | The page. No framework, no CDN, no inline script/style (CSP), no `innerHTML`. |
| `gpu-tunerd.service` | Root unit: `Type=notify`, watchdog, hardened, `ExecStopPost --restore-fans`. |
| `gpu-tuner-ui.service` | User unit for the page. `gpu-tuner.desktop` (with `icon.svg`) is the launcher. |
| `install.sh` | Install / upgrade / `--dry-run` / `--uninstall`, with rollback. |

**Not containerized, on purpose — checked, not assumed.** This app's writes need root
(`NO_PERMISSION` from NVML otherwise, verified 2026-09-07), and this lab's Docker is
**rootless**: a container's uid 0 maps to the host's own unprivileged user, not real root.
Verified directly: `docker run --gpus all --privileged --cap-add SYS_ADMIN ...
nvidia-smi -pl <value>` still gets `Insufficient Permissions`, and `/proc/self/uid_map` inside
that container shows `0 <host-uid> 1` — the container's "root" *is* the invoking user, as far as
anything outside the container's own namespace is concerned. There is no containerization scheme
that changes this on this security model, so both halves stay host-installed processes.

**Why two processes.** Fan and power writes are root-only on these cards (non-root returns
`NO_PERMISSION`, verified 2026-09-07). The page should not be root. Reads need no privilege,
so the UI server samples NVML itself and only forwards *change requests* to the daemon.

**Why the page has a login at all.** This box runs untrusted agents and containers, and any
local process can reach `127.0.0.1`. So: loopback bind only; an `HttpOnly; SameSite=Strict`
session cookie whose secret never appears in a URL or a log (`gpu-tuner open` mints a
single-use 60 s nonce and the server trades it for the cookie); a `Host` check against DNS
rebinding; an `Origin` check on every write. The daemon socket is `0600`, owned by the
configured uid, and re-checks the peer uid on every connection. Process names shown on the
page come from `/proc/<pid>/comm`, never `cmdline` (command lines here carry API keys).

**Why a daemon and not a one-shot.** NVML has no "install a curve" call: `SetFanSpeed` pins
a fixed speed until the next write, so a curve has to be re-evaluated as temperature moves.

## Security review

Reviewed before the first public push (2026-09-23). No way was found for an unauthenticated
actor, a CSRF'd browser tab, or the non-root UI process to force a write the root daemon
shouldn't allow, or to bypass a card's own hardware range. Fixed as a result:

- `save_state`/`save_config` now `chmod` the file explicitly (`0600`/`0644`) instead of trusting
  the ambient umask — the directory's own mode already prevented this from being exploitable
  today, but a future change to that mode shouldn't silently make `state.json` world-readable.
- `validate_clock_cap`'s 1,000 MHz "fat-finger" floor used to make the clock-cap feature
  unusable on any card whose *entire* range sits under 1,000 MHz (an inverted "1000-800 MHz"
  range rejected every value) — now the floor only applies when the card's own top clock clears
  it; below that, the card's own minimum takes over.
- `serve_one()`'s request read loop had no overall time limit, only a per-`recv()` one — a
  process sharing the daemon's configured uid (the socket's only real gate) could trickle bytes
  just under that timeout indefinitely and stall the fan-safety tick loop, which runs on the same
  thread. It now has a hard 2 s wall-clock deadline regardless of how the bytes arrive.
- `confirm_override` is now checked with `is True`, not `bool(...)` — the loose form would let a
  client send `"confirm_override": "false"` (a non-empty string) and have it override anyway.
- `set_budget` now rejects a value above the sum of every detected card's own hardware maximum —
  above that, the budget could never actually bind on anything.
- `gpu-tuner-ui.service` had no sandboxing at all, unlike its root sibling, despite being the
  process that parses attacker-reachable HTTP input. It now has a comparable baseline
  (`NoNewPrivileges`, `ProtectSystem=strict`, a scoped `ReadWritePaths`, `RestrictAddressFamilies`,
  `MemoryDenyWriteExecute`, etc.).

**What running this means, stated plainly.** `gpu-tunerd` runs as root with
`CapabilityBoundingSet=CAP_CHOWN CAP_DAC_OVERRIDE CAP_SYS_ADMIN`. `CAP_SYS_ADMIN` is required —
verified directly (see "Not containerized, on purpose" above) — and a process that holds it is
not meaningfully contained against its own compromise, whatever else the unit's hardening does.
Read the daemon's source before you `sudo` this install; the sandboxing narrows the blast radius
of a parsing bug, it doesn't remove the trust you're extending.

**Recommended, not yet done — needs testing against real hardware before changing:**
`CAP_DAC_OVERRIDE` in that same bounding set isn't obviously justified (state/config files are
already root-owned with owner-rw bits, so root can open them without a DAC override; it's most
likely there for `/dev/nvidia*` access if those nodes are group-restricted). Removing it, or
replacing it with `SupplementaryGroups=video` (or your distro's GPU group), would tighten this
further — untested here because the daemon in question is live, root, and controlling real
hardware on the machine this was built on, and a wrong guess breaks GPU control until someone's
at the console to fix it. Same caution applies to further `gpu-tunerd.service` hardening
(`SystemCallFilter=@system-service`, `DeviceAllow=char-nvidia rw` with `DevicePolicy=closed`,
`RestrictSUIDSGID`, `ProtectClock`, `ProtectHostname`, `ProtectKernelLogs`, `ProcSubset=pid`,
`RemoveIPC`) — all plausible defense-in-depth, none verified compatible with NVML's ioctls yet.

## Files on disk after install

| Path | What |
|---|---|
| `/usr/local/lib/gpu-tuner/` | root-owned copy of the daemon code |
| `/etc/gpu-tuner/config.json` | `allowed_uid`, `gpu_budget_w` (editable from the page — this is just where the daemon persists it), `interval_s` |
| `/var/lib/gpu-tuner/state.json` | the applied settings; re-validated on every start |
| `/run/gpu-tuner/control.sock` | daemon socket |
| `~/.config/gpu-tuner/` | session token and sign-in nonces (0600) |
| `~/.config/systemd/user/gpu-tuner-ui.service`, `~/.local/share/applications/gpu-tuner.desktop`, `icon.svg` | UI unit and launcher |

## Operate

```bash
journalctl -u gpu-tunerd -f                       # every fan decision, every request (audit lines), every fault
systemctl status gpu-tunerd; systemctl --user status gpu-tuner-ui
nvidia-smi --query-gpu=name,power.limit,fan.speed,temperature.gpu --format=csv
ops/gpu-tuner/install.sh --uninstall              # back to gpu-fan-curve + gpu-power-limit
```

To try the daemon without root: `ops/gpu-tuner/gpu-tunerd --dry-run --socket /run/user/$UID/gt.sock
--state /tmp/gpu-tuner-tryout/state.json` (the `--state` override matters: it defaults to the
root-owned `/var/lib/gpu-tuner/state.json`, which a non-root run cannot create) then
`ops/gpu-tuner/gpu-tuner serve --socket /run/user/$UID/gt.sock`. Every request is validated
and logged exactly as it would be; nothing is written to the cards.

## Tests

`bash tests/gpu-tuner-test.sh` (from this directory; `bash tests/run.sh gpu-tuner` if you're
inside local-ai-lab, which keeps this exact repo nested at `ops/gpu-tuner/`) — 52 offline tests
against a fake NVML: budget (including the soft-override and its `BudgetExceeded` distinguishing
from a hard `SafetyError`), ranges, per-card floor derivation, curve rules, hysteresis, fault
handling, state and config persistence, drift re-apply, the restart-race socket handoff, the
clock-cap floor on a low-clock card, the budget's hardware-maximum ceiling; plus the installer's
dry run and the page's CSP invariants.

`bash tests/live-ui-test.sh` (opt-in, ~40 s, needs a GPU and Firefox, **no root**; `bash
tests/run.sh gpu-tuner-live` from local-ai-lab) — `tests/live-ui-test.py` starts a dry-run
daemon and the UI on a scratch socket and port, then checks the auth gates (401/421/403, nonce
replay, cross-origin POST) and drives the
real page in headless Firefox: the over-budget warn-then-confirm flow end to end (including that
the shown error is the daemon's own, not a stale client prediction — a real bug this suite
caught), the budget editor persisting live, power/fan/clock apply and read-back, keyboard curve
editing across a re-render, baseline reset, no JS errors, no overflow at phone width. Nothing is
written to the cards.

Both suites pass as of 2026-09-23 on driver 595.91 (the live one: 28/28). Two bugs found and
fixed while building this pass, both live only on this exact machine: `nvml.py`'s ECC counter
constant is `NVML_VOLATILE_ECC`, not `…_ECC_ERRORS`, on the installed nvidia-ml-py; and
`syncPower()`/`syncFan()`/`syncClock()`'s message recompute wasn't guarded by `pKeep`/`fKeep`/
`cKeep` on their staged/warn branches, so a *rejected* apply (newly reachable once over-budget
stopped hard-disabling the button) had its real error immediately overwritten by the client's own
prediction text on the next render.
