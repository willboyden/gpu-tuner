#!/usr/bin/env python3
"""Live, opt-in test for the gpu-tuner UI: `bash tests/run.sh gpu-tuner-live`.

Needs a GPU (real NVML reads) and Firefox; needs NO root — the daemon runs with --dry-run, so
every request is validated and logged exactly as it would be and nothing is written to the
cards. This is the checked-in form of the manual pass behind the README's "Verified" line.

What it proves:
  auth    401 without the cookie, 421 on a bad Host, 403 on a forged / replayed nonce,
          403 on a cross-origin POST, 200 after a real sign-in
  page    both cards and all five charts render, no JS errors
  apply   a power change over the budget warns and needs a confirm click, which then applies it;
          an in-budget one applies straight away and reads back; a fan preset and a clock cap
          apply; baseline resets everything
  budget  the budget editor persists a new value and the page reflects it live
  keys    a curve point moved with the arrow keys keeps focus across a poll tick
  layout  no horizontal overflow at phone width with the table open
  fleet   (tests/fake_hub.py: the real page server over four fake machines) the fleet strip and
          tabs list every machine; a GB10 shows monitor-only with unified memory and no power or
          fan controls; a change on the 5090's tab reaches ONLY that machine's daemon; an
          unreachable machine says so; still no overflow at phone width
Uses Firefox's Marionette protocol directly (no geckodriver, no selenium).
"""
import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

TUNER = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8766                       # not the real UI's 8765, so this can run beside it
FLEET_PORT = 8767
FAILS = []


def check(cond, what):
    print(f"  {'PASS' if cond else 'FAIL'}: {what}", flush=True)
    if not cond:
        FAILS.append(what)


def wait_for(fn, seconds, what):
    t0 = time.monotonic()
    while time.monotonic() - t0 < seconds:
        if fn():
            return True
        time.sleep(0.25)
    sys.exit(f"gave up waiting for {what}")


class Marionette:
    def __init__(self, profile, width, height, port=2830):
        with open(os.path.join(profile, "user.js"), "w") as f:
            f.write(f'user_pref("marionette.port", {port});\nuser_pref("ui.prefersReducedMotion", 1);\n')
        self.proc = subprocess.Popen(["firefox", "--headless", "--no-remote", "--marionette", "--profile",
                                      profile, f"--window-size={width},{height}"],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.s, self.n = None, 0

        def up():
            try:
                self.s = socket.create_connection(("127.0.0.1", port), timeout=30)
                return True
            except OSError:
                return False
        wait_for(up, 40, "firefox marionette")
        self._recv()
        self.cmd("WebDriver:NewSession", {})

    def _recv(self):
        head = b""
        while not head.endswith(b":"):
            head += self.s.recv(1)
        need, buf = int(head[:-1]), b""
        while len(buf) < need:
            buf += self.s.recv(need - len(buf))
        return json.loads(buf)

    def cmd(self, name, params):
        self.n += 1
        msg = json.dumps([0, self.n, name, params])
        self.s.sendall(f"{len(msg)}:{msg}".encode())
        _t, _i, err, res = self._recv()
        if err:
            raise RuntimeError(f"{name}: {err.get('message')}")
        return res.get("value") if isinstance(res, dict) and "value" in res else res

    def go(self, url):
        self.cmd("WebDriver:Navigate", {"url": url})

    def js(self, script):
        return self.cmd("WebDriver:ExecuteScript", {"script": script, "args": []})

    def resize(self, w, h):
        self.cmd("WebDriver:SetWindowRect", {"width": w, "height": h})

    def quit(self):
        try:
            self.cmd("Marionette:Quit", {"flags": ["eForceQuit"]})
        except Exception:      # noqa: BLE001 — already gone is fine
            pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):     # surface the 303 itself (it carries the cookie)
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def http(url, method="GET", headers=None, data=None):
    req = urllib.request.Request(url, method=method, data=data, headers=headers or {})
    try:
        with _OPENER.open(req, timeout=5) as r:
            return r.status, r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.headers
    except urllib.error.URLError:
        return None, {}                  # not listening (yet)


def main():
    for tool in ("firefox", "nvidia-smi"):
        if not shutil.which(tool):
            print(f"  SKIP: {tool} not found")
            return 0
    if os.environ.get("XDG_RUNTIME_DIR") is None:
        print("  SKIP: no XDG_RUNTIME_DIR for a short socket path")
        return 0
    tmp = tempfile.mkdtemp(prefix="gpu-tuner-live-")
    sock = os.path.join(os.environ["XDG_RUNTIME_DIR"], "gpu-tuner-test.sock")
    # firefox is a snap here: its profile must live under ~/snap/firefox/common to be readable by it
    prof_base = os.path.expanduser("~/snap/firefox/common/gpu-tuner-test")
    profile = os.path.join(prof_base, "profile")
    shutil.rmtree(prof_base, ignore_errors=True)
    os.makedirs(profile)
    cfg = os.path.join(tmp, "config.json")
    with open(cfg, "w") as f:
        json.dump({"allowed_uid": os.getuid(), "gpu_budget_w": 750, "interval_s": 2}, f)
    logs = (open(os.path.join(tmp, "daemon.log"), "w"), open(os.path.join(tmp, "ui.log"), "w"),
            open(os.path.join(tmp, "fleet.log"), "w"))
    procs, browser = [], None
    try:
        procs.append(subprocess.Popen([os.path.join(TUNER, "gpu-tunerd"), "--dry-run", "--config", cfg,
                                       "--state", os.path.join(tmp, "state.json"), "--socket", sock],
                                      stdout=logs[0], stderr=subprocess.STDOUT))
        wait_for(lambda: os.path.exists(sock), 15, "daemon socket")
        procs.append(subprocess.Popen([os.path.join(TUNER, "gpu-tuner"), "serve", "--port", str(PORT), "--socket", sock,
                                       "--hosts", os.path.join(tmp, "no-hosts.json")],
                                      stdout=logs[1], stderr=subprocess.STDOUT))
        base = f"http://127.0.0.1:{PORT}"
        wait_for(lambda: http(base + "/")[0] == 401, 15, "UI server")

        print("auth gates")
        check(http(base + "/api/state")[0] == 401, "no cookie -> 401")
        check(http(base + "/", headers={"Host": "evil.example:1"})[0] == 421, "bad Host -> 421")
        check(http(base + "/?nonce=" + "A" * 43)[0] == 403, "forged nonce -> 403")
        url = subprocess.check_output([os.path.join(TUNER, "gpu-tuner"), "open", "--port", str(PORT), "--print-url"], text=True).strip()
        st, hdr = http(url)
        cookie = (hdr.get("Set-Cookie") or "").split(";")[0]
        check(st == 303 and cookie.startswith("gpu_tuner="), "real nonce -> 303 + cookie")
        check(http(url)[0] == 403, "replayed nonce -> 403")
        check(http(base + "/api/state", headers={"Cookie": cookie})[0] == 200, "cookie -> 200")
        body = json.dumps({"op": "set_power", "uuid": "x", "watts": 1}).encode()
        check(http(base + "/api/apply", "POST", {"Cookie": cookie, "Content-Type": "application/json",
                                                  "Origin": "http://evil.example"}, body)[0] == 403, "cross-origin POST -> 403")
        check(http(base + "/api/apply", "POST", {"Content-Type": "application/json", "Origin": base}, body)[0] == 401,
              "POST without cookie -> 401")

        print("page")
        browser = Marionette(profile, 1400, 1000)
        browser.go(subprocess.check_output([os.path.join(TUNER, "gpu-tuner"), "open", "--port", str(PORT), "--print-url"], text=True).strip())
        time.sleep(3)
        browser.js("window.__errs=[]; addEventListener('error', e => __errs.push(e.message));"
                   "addEventListener('unhandledrejection', e => __errs.push(String(e.reason)))")
        check(browser.js("return location.pathname") == "/", "signed in and redirected to /")
        check(browser.js("return document.getElementById('conn').textContent") == "Live · dry-run", "connection chip says dry-run")
        check(browser.js("return document.querySelectorAll('.gpu').length") == 2, "two cards rendered")
        check(browser.js("return document.querySelectorAll('.chart').length") == 5, "five charts rendered")
        gpus = browser.js("return [...document.querySelectorAll('.gpu h2')].map(h => h.textContent)")
        check(gpus == ["Max-Q", "Workstation"], f"cards are Max-Q + Workstation ({gpus})")

        print("apply (dry-run daemon)")
        r = browser.js("""
          const card = document.querySelectorAll('.gpu')[1];
          const num = card.querySelector('input[type=number]'); num.value = 600; num.dispatchEvent(new Event('input', {bubbles:true}));
          const btn = [...card.querySelectorAll('button')].find(x => x.textContent === 'Apply power limit');
          return [btn.disabled, card.querySelector('.msg').textContent];""")
        check(r[0] is False and "over the 750 W budget" in r[1] and "still apply" in r[1],
              f"600 W on the Workstation card warns but does not disable Apply ({r})")
        r = browser.js("""
          const card = document.querySelectorAll('.gpu')[1];
          [...card.querySelectorAll('button')].find(x => x.textContent === 'Apply power limit').click();
          return true;""")
        time.sleep(2)
        r = browser.js("""
          const card = document.querySelectorAll('.gpu')[1];
          const over = [...card.querySelectorAll('button')].find(x => x.textContent.startsWith('Apply anyway'));
          return [card.querySelector('.msg').textContent, over && !over.hidden];""")
        check("confirm to exceed it anyway" in r[0] and r[1] is True,
              f"unconfirmed over-budget apply shows the DAEMON's rejection (not a stale client prediction) and reveals the override button ({r})")
        browser.js("""
          const card = document.querySelectorAll('.gpu')[1];
          [...card.querySelectorAll('button')].find(x => x.textContent.startsWith('Apply anyway')).click();""")
        time.sleep(2)
        r = browser.js("const c = document.querySelectorAll('.gpu')[1]; return [c.querySelector('.msg').textContent, document.querySelector('.hostview:not([hidden]) .b-used').textContent]")
        check("confirmed" in r[0] and "600 W in force" in r[0] and r[1] == "900", f"confirmed override applied 600 W; hero shows 900 ({r})")
        browser.js("""
          const card = document.querySelectorAll('.gpu')[1];
          const num = card.querySelector('input[type=number]'); num.value = 425; num.dispatchEvent(new Event('input', {bubbles:true}));
          [...card.querySelectorAll('button')].find(x => x.textContent === 'Apply power limit').click();""")
        time.sleep(2)
        r = browser.js("const c = document.querySelectorAll('.gpu')[1]; return [c.querySelector('.msg').textContent, document.querySelector('.hostview:not([hidden]) .b-used').textContent]")
        check("425 W in force" in r[0] and r[1] == "725", f"lowering back down applies with no confirm needed; hero shows 725 ({r})")

        print("budget editor")
        before = browser.js("return document.querySelector('.hostview:not([hidden]) .b-input').value")
        check(before == "750", f"budget input starts at 750 ({before})")
        browser.js("""
          const input = document.querySelector('.hostview:not([hidden]) .b-input'); input.value = 900;
          document.querySelector('.hostview:not([hidden]) .b-apply').click();""")
        time.sleep(1)
        r = browser.js("return [document.querySelector('.hostview:not([hidden]) .b-msg').textContent, document.querySelector('.hostview:not([hidden]) .b-sub').textContent]")
        check("900" in r[0] and "900 W combined GPU budget" in r[1], f"budget editor persists a new value live ({r})")
        browser.js("""
          const card = document.querySelectorAll('.gpu')[0];
          [...card.querySelectorAll('button')].find(x => x.textContent === 'Quiet').click();
          [...card.querySelectorAll('button')].find(x => x.textContent === 'Apply fan settings').click();""")
        time.sleep(2)
        check("Applied: curve" in browser.js("return [...document.querySelectorAll('.gpu')[0].querySelectorAll('.msg')][1].textContent"),
              "Quiet fan preset applied")
        browser.js("""
          const card = document.querySelectorAll('.gpu')[1];
          const cb = card.querySelector('input[type=checkbox]'); cb.checked = true; cb.dispatchEvent(new Event('change', {bubbles:true}));
          const sl = card.querySelectorAll('input[type=range]')[2]; sl.value = 2001; sl.dispatchEvent(new Event('input', {bubbles:true}));
          [...card.querySelectorAll('button')].find(x => x.textContent === 'Apply clock cap').click();""")
        time.sleep(2)
        msg = browser.js("return [...document.querySelectorAll('.gpu')[1].querySelectorAll('.msg')][2].textContent")
        check("capped at" in msg, f"clock cap applied and snapped ({msg})")

        print("keyboard")
        r = browser.js("""
          const card = document.querySelectorAll('.gpu')[0];
          const pt = card.querySelector('g.point[data-i="1"]'); pt.focus();
          pt.dispatchEvent(new KeyboardEvent('keydown', {key:'ArrowUp', bubbles:true}));
          return [card.querySelector('.points').textContent, document.activeElement && document.activeElement.dataset.i];""")
        check("60° → 46%" in r[0] and r[1] == "1", f"ArrowUp raised point 2 by 1% and kept focus ({r})")
        time.sleep(1.3)
        check(browser.js("return document.activeElement && document.activeElement.dataset.i") == "1", "focus survives a poll re-render")

        print("baseline")
        for i in (0, 1):
            browser.js(f"[...document.querySelectorAll('.gpu')[{i}].querySelectorAll('button')].find(x => x.textContent.startsWith('Reset to lab baseline')).click()")
            time.sleep(1.5)
        r = browser.js("return [...document.querySelectorAll('.gpu')].map(c => c.querySelector('input[type=number]').value + '/' + c.querySelector('fieldset:nth-of-type(3) b').textContent.split(' ')[0])")
        check(r == ["300/uncapped", "450/uncapped"], f"both cards back at baseline ({r})")
        check(browser.js("return __errs") == [], "no JS errors")

        print("layout")
        browser.resize(420, 900)
        browser.js("document.getElementById('table-toggle').click()")
        time.sleep(1.5)
        r = browser.js("return [document.documentElement.scrollWidth, document.documentElement.clientWidth]")
        check(r[0] <= r[1], f"no horizontal overflow at phone width with the table open ({r})")

        print("fleet (fake machines)")
        procs.append(subprocess.Popen([sys.executable, os.path.join(TUNER, "tests", "fake_hub.py"),
                                       "--port", str(FLEET_PORT), "--tmp", tmp],
                                      stdout=logs[2], stderr=subprocess.STDOUT))
        fbase = f"http://127.0.0.1:{FLEET_PORT}"
        wait_for(lambda: http(fbase + "/")[0] == 401, 15, "fake hub")
        browser.resize(1400, 1000)
        browser.go(subprocess.check_output([os.path.join(TUNER, "gpu-tuner"), "open", "--port", str(FLEET_PORT), "--print-url"], text=True).strip())
        time.sleep(5)
        browser.js("window.__errs=[]; addEventListener('error', e => __errs.push(e.message));"
                   "addEventListener('unhandledrejection', e => __errs.push(String(e.reason)))")
        r = browser.js("return [document.querySelectorAll('#fleet-table tbody tr').length, document.querySelectorAll('#tabs [role=tab]').length, document.getElementById('conn').textContent, document.getElementById('fleet').hidden]")
        check(r == [5, 4, "Live · 3 of 4 machines", False], f"fleet strip: 5 GPU rows on 4 machines, 3 live ({r})")
        r = browser.js("return [...document.querySelectorAll('#fleet-table tbody tr')].map(t => t.children[1].textContent)")
        check(r[-1].startswith("Unreachable") and r[2] == "Live · monitor-only", f"link states read in words ({r})")
        browser.js("document.getElementById('tab-gb10').click()")
        time.sleep(2)
        r = browser.js("""
          const v = document.querySelector('.hostview:not([hidden])');
          return [location.hash, v.dataset.host, v.querySelector('.gpu h2').textContent,
                  [...v.querySelectorAll('.tile .label')].map(x => x.textContent)[4],
                  [...v.querySelectorAll('fieldset')].filter(f => !f.hidden).length,
                  v.querySelector('.unsupported').textContent, v.querySelector('.banner').textContent,
                  v.querySelector('.budget').hidden, document.getElementById('hist-h').textContent,
                  document.querySelectorAll('.chart:not([hidden])').length];""")
        check(r[:5] == ["#host=gb10", "gb10", "GB10", "System memory (shared)", 0],
              f"GB10 tab: unified memory, no power/fan/clock controls ({r[:5]})")
        check("power limit, fan control, clock cap" in r[5] and "Monitor-only, and that is all Spark 1 needs" in r[6] and r[7] is True
              and r[8] == "History · Spark 1", f"GB10 says why, is monitor-only, has no budget panel ({r[5:9]})")
        check(r[9] == 4, f"no empty fan chart for a fanless machine ({r[9]} charts shown)")
        r = browser.js("""
          const v = document.querySelector('.hostview:not([hidden])');
          return [v.querySelector('.gpu header .sub').textContent, v.querySelector('.tile .sub').textContent];""")
        check("PCIe" not in r[0] and "under slowdown (86 °C)" in r[1],
              f"GB10: no bogus PCIe riser warning, headroom against its lowest threshold ({r})")
        browser.js("document.getElementById('tab-rtx5090').click()")
        time.sleep(2)
        r = browser.js("""
          const v = document.querySelector('.hostview:not([hidden])'), card = v.querySelector('.gpu');
          const num = card.querySelector('input[type=number]'); num.value = 550; num.dispatchEvent(new Event('input', {bubbles:true}));
          [...card.querySelectorAll('button')].find(x => x.textContent === 'Apply power limit').click();
          return card.querySelector('h2').textContent;""")
        time.sleep(2.5)
        msg = browser.js("return document.querySelector('.hostview:not([hidden]) .gpu .msg').textContent")
        check(r == "GeForce RTX 5090" and "550 W in force" in msg, f"a 5090 change applies on its own machine ({r}, {msg})")
        n = browser.js("return document.querySelectorAll('.chart:not([hidden])').length")
        check(n == 5, f"the fan chart comes back on a machine with fans ({n} charts shown)")
        recs = {}
        for hid in ("ws", "rtx5090"):
            path = os.path.join(tmp, f"rec-{hid}.jsonl")
            recs[hid] = open(path).read().count("set_power") if os.path.exists(path) else 0
        check(recs == {"ws": 0, "rtx5090": 1}, f"...and reached ONLY the 5090's daemon ({recs})")
        r = browser.js("""
          const card = document.querySelector('.hostview:not([hidden]) .gpu');
          const num = card.querySelector('input[type=number]'); num.value = 575; num.dispatchEvent(new Event('input', {bubbles:true}));
          return card.querySelector('.msg').textContent;""")
        check(r.startswith("575 W is 25 W over this machine’s 550 W budget"), f"one-card over-budget warning reads right ({r})")
        browser.js("""
          const card = document.querySelector('.hostview:not([hidden]) .gpu');
          [...card.querySelectorAll('button')].find(x => x.textContent === 'Apply power limit').click();""")
        time.sleep(2.5)
        r = browser.js("return document.querySelector('.hostview:not([hidden]) .gpu .msg').textContent")
        check(r.startswith("575 W is 25 W over this machine's 550 W GPU budget") and "other card" not in r,
              f"...and so does the daemon's refusal, with the browser still up ({r})")
        browser.js("document.getElementById('tab-gone').click()")
        time.sleep(1.5)
        r = browser.js("return document.querySelector('.hostview:not([hidden]) .banner').textContent")
        check("Gone box is unreachable" in r and "No route to host" in r, f"an unreachable machine says why ({r[:120]})")
        browser.resize(420, 900)
        time.sleep(1.5)
        r = browser.js("return [document.documentElement.scrollWidth, document.documentElement.clientWidth]")
        check(r[0] <= r[1], f"no horizontal overflow at phone width with four machines ({r})")
        check(browser.js("return __errs") == [], "no JS errors on the fleet page")

        with open(os.path.join(tmp, "daemon.log")) as f:
            log = f.read()
        check("REJECTED" not in log.split("audit")[0] and log.count("-> ok") >= 5, "daemon audit log records the applied requests")
        limits = subprocess.check_output(["nvidia-smi", "--query-gpu=power.limit", "--format=csv,noheader"], text=True)
        check("DRY-RUN" in log, "daemon ran in dry-run")
        print(f"  (real limits now: {limits.strip().replace(chr(10), ', ')} — unchanged by design: dry-run)")
    finally:
        if browser is not None:
            browser.quit()
        for p in reversed(procs):
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
        for f in logs:
            f.close()
        shutil.rmtree(prof_base, ignore_errors=True)
        if FAILS:
            print(f"  logs kept in {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)
    if FAILS:
        print(f"\n{len(FAILS)} FAILED: " + "; ".join(FAILS))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
