"""The page's config builder, server half: draft, validate, export. It never writes anything.

The page drafts hosts.json and /etc/gpu-tuner/config.json in the browser; this module checks a
draft with the REAL loaders' rules (hosts.parse_hosts_text, daemon.parse_config) and says what is
wrong. Checking a draft reads no file contents and writes none, and uses no socket or subprocess (it
only asks whether the page's ssh key file exists, for a hint). Installing the result stays with the
human: the page only shows the commands.

What it reads, and why that is safe to show the signed-in page:
  * the hosts the page is RUNNING with — already parsed and validated at startup, re-serialized
    from those objects, so only the schema's fields can appear (id, label, ssh, port, wall: names,
    addresses and watts, no secret field exists). The file is not re-read per request.
  * examples/hosts.example.json from this checkout.
  * a config.json TEMPLATE built from the daemon's own defaults. Its gpu_budget_w is the daemon's
    fallback, a placeholder: the page drafts this machine's file from the budget its daemon reports
    instead. The real /etc/gpu-tuner/config.json is never read here: it belongs to each machine's
    root daemon.
Demo mode reads nothing real at all: its hosts are the made-up machines, its uid a placeholder.
"""
from __future__ import annotations

import json
import os

from . import daemon, hosts, safety

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLE = os.path.join(ROOT, "examples", "hosts.example.json")
KINDS = ("hosts", "config")
MAX_TEXT = hosts.MAX_FILE          # the hosts loader's own cap. gpu-tunerd's load_config has none: for
                                   # config.json this is only this page's limit for a draft
MAX_MSG = 300
DEMO_UID = 1000                    # demo mode never reports the real uid


def _short(s, n=MAX_MSG):
    s = str(s)
    return s if len(s) <= n else s[:n - 1] + "…"


def _dupe_keys(text):
    """Keys that appear twice in one object. json keeps the last silently, which hides a typo."""
    found = []

    def hook(pairs):
        seen = set()
        for k, _v in pairs:
            if k in seen and k not in found:
                found.append(k)
            seen.add(k)
        return dict(pairs)
    try:
        json.loads(text, object_pairs_hook=hook)
    except (ValueError, RecursionError):
        pass
    return [_short(k, 40) for k in found[:5]]


def hosts_doc(machines):
    """A hosts.json document for machines already parsed by hosts.parse_hosts (or the demo's)."""
    out = []
    for m in machines:
        d = {"id": m["id"], "label": m["label"]}
        if m.get("ssh"):
            d["ssh"] = m["ssh"]
        if m.get("port"):
            d["port"] = m["port"]
        w = m.get("wall")
        if w:
            d["wall"] = {"non_gpu_dc_w": w["non_gpu_dc_w"], "psu_efficiency": w["psu_efficiency"]}
            if w.get("circuits"):
                d["wall"]["circuits"] = dict(w["circuits"])
        out.append(d)
    return {"hosts": out}


def example_doc(path=EXAMPLE):
    """The shipped example, only if it passes the loader's rules (else None)."""
    try:
        with open(path, "rb") as f:
            data = f.read(MAX_TEXT + 1)
        hosts.parse_hosts_text(data)
        return json.loads(data)
    except (OSError, ValueError):
        return None


def config_template(uid):
    """The daemon's defaults with this page's uid. NOT what install.sh writes: it seeds the budget
    with the sum of the caps in force, while gpu_budget_w here is the daemon's fallback (750 W), a
    placeholder. A budget below a machine's saved limits makes its daemon lower them on start."""
    return {"allowed_uid": uid, "gpu_budget_w": safety.DEFAULT_GPU_BUDGET_W,
            "interval_s": int(daemon.DEFAULT_INTERVAL_S)}


def _result(errors=(), warnings=(), **extra):
    return {"ok": not errors, "errors": [_short(e) for e in errors],
            "warnings": [_short(w) for w in warnings], **extra}


def budget_effect(budget_w):
    """What a budget does when gpu-tunerd starts (Daemon.load_state), said every time: a budget
    under the saved limits silently lowers them, which no loader treats as an error."""
    return (f"On start gpu-tunerd lowers saved power limits to each card's baseline (or its minimum) if "
            f"they total more than {budget_w} W; compare with that machine's Power budget panel before "
            f"installing")


def _not_utf8(text):
    """An error result if text has no UTF-8 form, else None. A lone surrogate (a JSON "\\ud800"
    escape decoded into the request's string) cannot be in any file, and str.encode() raises on
    it: answer that like any other bad draft, never with an exception (a 500)."""
    try:
        text.encode()
    except UnicodeEncodeError as e:
        return _result([f"the text holds a character that cannot be saved as UTF-8 "
                        f"(U+{ord(e.object[e.start]):04X}, an unpaired surrogate): no file can hold it"])
    return None


def check_hosts(text, *, demo=False, key_file=None):
    bad = _not_utf8(text)
    if bad:
        return bad
    try:
        machines = hosts.parse_hosts_text(text)
    except hosts.HostsError as e:
        return _result([f"gpu-tuner serve would refuse this file: {e}"])
    warnings = [f"key {k!r} appears twice in one object; the loader keeps the last one" for k in _dupe_keys(text)]
    if not any(m["id"] == "local" for m in machines):
        warnings.append('no "local" entry: the page will not show the machine it runs on (fine on a machine '
                        'without GPUs)')
    if any(m["ssh"] for m in machines) and not demo and not os.path.exists(key_file or hosts.KEY_FILE):
        warnings.append("this page has no ssh key yet, so it will not try ssh: run `gpu-tuner hosts --init-key` "
                        "and add the line it prints to authorized_keys on each machine")
    summary = [{"id": m["id"], "label": m["label"], "remote": bool(m["ssh"])} for m in machines]
    return _result(warnings=warnings, machines=summary)


def check_config(text, *, page_uid=None):
    bad = _not_utf8(text)
    if bad:
        return bad
    if len(text.encode()) > MAX_TEXT:
        return _result([f"it is over {MAX_TEXT} bytes, this page's limit for a draft (gpu-tunerd itself "
                        f"sets no size limit)"])
    try:
        raw = json.loads(text)
    except (ValueError, RecursionError) as e:
        return _result([f"gpu-tunerd would not start: not valid JSON: {e}"])
    try:
        cfg = daemon.parse_config(raw, "config.json")
    except daemon.ConfigError as e:
        return _result([f"gpu-tunerd would not start: {e}"])
    warnings = [f"key {k!r} appears twice in one object; the loader keeps the last one" for k in _dupe_keys(text)]
    extra = sorted(str(k) for k in raw if k not in daemon.CONFIG_KEYS)
    if extra:
        names = ", ".join(_short(k, 40) for k in extra[:10])
        warnings.append(f"ignored by gpu-tunerd: {names}. Setting the budget from the page rewrites this file "
                        f"with its three keys only, so these would then be dropped")
    if cfg["allowed_uid"] is None:
        warnings.append("no allowed_uid: the daemon's socket stays root-only, so no page (this one or a remote "
                        "one) can reach it and that machine's controls stay locked")
    elif page_uid is not None and cfg["allowed_uid"] != page_uid:
        warnings.append(f"this page runs as uid {page_uid}: if this file is for THIS machine, its daemon will "
                        f"refuse the page. For another machine, use the uid the page logs in as there")
    if "gpu_budget_w" not in raw:
        warnings.append(f"no gpu_budget_w: the daemon falls back to {safety.DEFAULT_GPU_BUDGET_W} W")
    if cfg["gpu_budget_w"] is not None:
        warnings.append(budget_effect(cfg["gpu_budget_w"]))
    return _result(warnings=warnings, effective=cfg)


def check(kind, text, *, demo=False, page_uid=None, key_file=None):
    """Diagnostics for one draft. Raises ValueError for a malformed request (not a bad draft)."""
    if kind not in KINDS:
        raise ValueError("kind must be hosts or config")
    if not isinstance(text, str):
        raise ValueError("text must be a string")
    if kind == "hosts":
        return check_hosts(text, demo=demo, key_file=key_file)
    return check_config(text, page_uid=page_uid)
