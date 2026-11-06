#!/usr/bin/env python3
"""Offline tests for `gpu-tuner serve --demo`: simulated machines, never real hardware.

What must hold: the demo never loads NVML, never reaches this machine's daemon socket, ssh or the
network; nothing from the machine running it (hostname, process names) reaches the page; the
simulation is deterministic and plausible, and its seeded history joins the live readings; the
page keeps the real one's sign-in and request checks but with its own cookie; and the real page
is unchanged. No GPU, no root, ~15 s.
"""
import hashlib
import http.client
import importlib.util
import json
import math
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from gpu_tuner import demo, hosts, hub, node, nvml, server  # noqa: E402
from gpu_tuner.daemon import DEFAULT_SOCKET  # noqa: E402

hub.log = lambda msg: None
ENTRY = os.path.join(ROOT, "gpu-tuner")
WS = demo.CARDS["ws"]["uuid"]
GB10 = demo.CARDS["gb10"]["uuid"]
SYNTHETIC_PROCS = {c["proc"] for c in demo.CARDS.values()}


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Simulation(unittest.TestCase):
    def test_cards_load_through_the_real_nvml_wrapper(self):
        nv = demo._demo_nvml(demo.DemoLib(demo.plan_for("local")))
        self.assertEqual([g.name for g in nv.gpus], [demo.CARDS["maxq"]["name"], demo.CARDS["ws"]["name"]])
        maxq, ws = nv.gpus
        self.assertEqual((maxq.power_min_w, maxq.power_max_w, maxq.nfans), (250, 325, 1))
        self.assertEqual((ws.power_min_w, ws.power_max_w, ws.nfans, ws.mem_kind), (150, 600, 2, "dedicated"))
        self.assertEqual(ws.thresholds["slowdown"], 95)
        gb = demo._demo_nvml(demo.DemoLib(demo.plan_for("edge-gb10"))).gpus[0]
        self.assertEqual((gb.mem_kind, gb.power_max_w, gb.nfans, gb.clocks), ("unified", None, 0, []))

    def test_same_second_same_reading_in_two_independent_simulations(self):
        t = 1_800_000_000
        a, b = demo.Sim(demo.CARDS["ws"], t), demo.Sim(demo.CARDS["ws"], t)
        self.assertEqual(a.r, b.r)
        a.advance(t + 120)
        b.advance(t + 120)
        self.assertEqual((a.r, a.energy_mj), (b.r, b.energy_mj))

    def test_six_hours_of_history_are_plausible_and_match_the_live_layout(self):
        now = int(time.time())
        rows = demo.history_rows("local", now)
        self.assertEqual(len(rows), demo.HISTORY_S - 1)
        self.assertEqual(rows[-1][0], float(now - 1))
        self.assertEqual(rows[0][1], (demo.CARDS["maxq"]["uuid"], WS))
        n = len(hub.SERIES)
        for _t, _layout, vals in rows:
            self.assertEqual(len(vals), 2 * n)
            for i in range(0, len(vals), n):
                temp, fan, power, cap, clock, util, soc = vals[i:i + n]
                self.assertIsNone(soc)                                    # discrete cards: no SoC
                self.assertTrue(25 <= temp < 95, temp)                    # never at slowdown
                self.assertTrue(30 <= fan <= 100, fan)
                self.assertTrue(0 < power <= cap * 1.02, (power, cap))
                self.assertTrue(180 <= clock <= 3090 and 0 <= util <= 100)
                self.assertTrue(all(math.isfinite(v) for v in vals if v is not None))
        utils = [r[2][5] for r in rows]
        self.assertGreater(max(utils), 80)              # the charts show work...
        self.assertLess(min(utils), 10)                 # ...and idle gaps

    def test_a_gb10_has_a_soc_temperature_above_its_gpu_die(self):
        rows = demo.history_rows("edge-gb10", int(time.time()), seconds=600)
        self.assertEqual([hub.SERIES[i] for i in (0, 6)], ["temp", "soc"])
        for _t, _layout, vals in rows:
            self.assertTrue(vals[0] + 2 < vals[6] < 99, vals)              # a few degrees over the die

    def test_seeded_history_joins_the_live_reading(self):
        now = int(time.time())
        last = demo.history_rows("render-box", now)[-1][2]
        live = demo.DemoLib(demo.plan_for("render-box"), clock=lambda: now - 1).sims[0].r
        self.assertLessEqual(abs(live["temp"] - last[0]), 2)
        self.assertEqual(live["clock"], last[4])

    def test_writes_land_on_the_simulated_card(self):
        lib = demo.DemoLib(demo.plan_for("local"))
        lib.nvmlDeviceSetPowerManagementLimit(1, 400_000)
        self.assertEqual(lib.nvmlDeviceGetPowerManagementLimit(1), 400_000)
        lib.nvmlDeviceSetFanSpeed_v2(1, 0, 77)
        self.assertEqual(lib.nvmlDeviceGetFanSpeed_v2(1, 0), 77)
        lib.nvmlDeviceSetGpuLockedClocks(1, 180, 1500)
        self.assertLessEqual(lib.nvmlDeviceGetClockInfo(1, lib.NVML_CLOCK_GRAPHICS), 1500)
        with self.assertRaises(lib.NVMLError):
            demo.DemoLib(demo.plan_for("edge-gb10")).nvmlDeviceSetPowerManagementLimit(0, 50_000)


class NothingReal(unittest.TestCase):
    def test_soc_temperature_is_simulated_never_read_from_sys(self):
        real = nvml.Nvml.soc_temp_c
        nvml.Nvml.soc_temp_c = lambda _self: self.fail("the demo read /sys/class/thermal")
        try:
            p = demo.plan_for("edge-gb10")
            lib = demo.DemoLib(p)
            g = demo._demo_collector(demo._demo_nvml(lib), "/nonexistent.sock", p).sample()[GB10]
            self.assertEqual(g["soc_temp"], lib.sims[0].r["soc"])
            self.assertGreater(g["soc_temp"], g["temp"])
        finally:
            nvml.Nvml.soc_temp_c = real

    def test_node_reports_made_up_hostname_and_process_names_without_reading_proc(self):
        real_comm = node._comm
        node._comm = lambda pid: self.fail("the demo read /proc for a process name")
        try:
            for p in demo.PLAN:
                c = demo._demo_collector(demo._demo_nvml(demo.DemoLib(p)), "/nonexistent.sock", p)
                hello = c.hello()
                self.assertEqual(hello["host"]["hostname"], p["id"])
                self.assertNotEqual(hello["host"]["hostname"], socket.gethostname())
                for g in c.sample().values():
                    self.assertTrue({x["name"] for x in g["procs"]} <= SYNTHETIC_PROCS)
        finally:
            node._comm = real_comm

    def test_labels_are_generic(self):
        names = {p["label"] for p in demo.PLAN} | {p["id"] for p in demo.PLAN}
        self.assertNotIn(socket.gethostname(), names)

    def test_demo_nodes_never_get_ssh_or_the_real_daemon_socket(self):
        for p in demo.PLAN:
            argv = demo.node_argv(p["id"], "/tmp/x")
            self.assertEqual(argv[1:3], [hosts.ENTRY, "demo-node"])
            self.assertNotIn("ssh", argv)
            self.assertNotIn(DEFAULT_SOCKET, argv)

    def test_a_demo_process_cannot_load_nvml(self):
        code = ("import sys; sys.path.insert(0, sys.argv[1]); from gpu_tuner import demo; demo._block_nvml()\n"
                "lib = demo.DemoLib(demo.plan_for('local')); nv = demo._demo_nvml(lib); nv.sample(nv.gpus[0])\n"
                "try:\n    import pynvml\nexcept ImportError:\n    print('blocked')\n")
        out = subprocess.run([sys.executable, "-I", "-c", code, ROOT], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.stdout.strip(), "blocked", out.stderr)


class Page(unittest.TestCase):
    """The demo page end to end: a real `gpu-tuner serve --demo` under a scratch HOME and TMPDIR."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.env = {**os.environ, "HOME": cls.tmp, "TMPDIR": cls.tmp}
        cls.port = free_port()
        cls.proc = subprocess.Popen([sys.executable, ENTRY, "serve", "--demo", "--port", str(cls.port)],
                                    env=cls.env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                socket.create_connection(("127.0.0.1", cls.port), timeout=0.5).close()
                break
            except OSError:
                time.sleep(0.2)
        url = subprocess.run([sys.executable, ENTRY, "open", "--demo", "--port", str(cls.port), "--print-url"],
                             env=cls.env, capture_output=True, text=True, timeout=20).stdout.strip()
        st, hd, _ = cls.req("GET", url.split(f":{cls.port}", 1)[1], cookie=None)
        cls.signin = (st, hd.get("Set-Cookie", ""))
        cls.cookie = cls.signin[1].split(";")[0]

    @classmethod
    def tearDownClass(cls):
        if cls.proc.poll() is None:
            cls.proc.kill()
        cls.proc.wait()

    @classmethod
    def req(cls, method, path, body=None, cookie="default", headers=None):
        h = {"Host": f"127.0.0.1:{cls.port}"}
        if cookie == "default":
            cookie = cls.cookie
        if cookie:
            h["Cookie"] = cookie
        if body is not None:
            h.update({"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{cls.port}"})
            body = json.dumps(body).encode()
        h.update(headers or {})
        c = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=15)
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, dict(r.getheaders()), data

    def state(self):
        return json.loads(self.req("GET", "/api/state")[2])

    def wait_up(self):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            s = self.state()
            if all(h["conn"] == "up" and h.get("daemon") for h in s["hosts"]):
                return s
            time.sleep(0.5)
        self.fail(f"demo machines never all came up: {[(h['id'], h['conn']) for h in s['hosts']]}")

    def test_sign_in_sets_the_demo_cookie_only(self):
        st, cookie = self.signin
        self.assertEqual(st, 303)
        self.assertTrue(cookie.startswith(f"{demo.COOKIE}="))
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)

    def test_same_request_checks_as_the_real_page(self):
        self.assertEqual(self.req("GET", "/api/state", cookie=None)[0], 401)
        real_name = "gpu_tuner=" + self.cookie.split("=", 1)[1]
        self.assertEqual(self.req("GET", "/api/state", cookie=real_name)[0], 401)
        self.assertEqual(self.req("GET", "/api/state", headers={"Host": "evil.example:80"})[0], 421)
        st, hd, _ = self.req("GET", "/api/state")
        self.assertEqual(st, 200)
        self.assertIn("default-src 'none'", hd["Content-Security-Policy"])
        self.assertEqual(hd["X-Frame-Options"], "DENY")
        body = {"op": "set_power", "host": "local", "uuid": WS, "watts": 400}
        self.assertEqual(self.req("POST", "/api/apply", body, headers={"Origin": "http://evil.example"})[0], 403)

    def test_three_simulated_machines_with_six_hours_of_history(self):
        s = self.wait_up()
        self.assertIs(s["demo"], True)
        self.assertEqual([(h["id"], h["label"], h["remote"]) for h in s["hosts"]],
                         [("local", "Workstation", False), ("render-box", "Render box", True),
                          ("edge-gb10", "Edge GB10", True)])
        hist = json.loads(self.req("GET", "/api/history?host=local&window=21600")[2])
        self.assertGreater(hist["t"][-1] - hist["t"][0], 5.9 * 3600)
        self.assertGreater(sum(v is not None for v in hist["gpus"][WS]["power"]), 800)

    def test_a_change_goes_through_the_daemon_checks_and_reads_back(self):
        self.wait_up()
        st, _, data = self.req("POST", "/api/apply", {"op": "set_power", "host": "local", "uuid": WS, "watts": 420})
        self.assertEqual((st, json.loads(data)["ok"]), (200, True))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            loc = next(h for h in self.state()["hosts"] if h["id"] == "local")
            if ((loc.get("live") or {}).get("gpus") or {}).get(WS, {}).get("power_limit_w") == 420:
                break
            time.sleep(0.5)
        else:
            self.fail("420 W never read back from the simulated card")
        st, _, data = self.req("POST", "/api/apply", {"op": "set_power", "host": "local", "uuid": WS, "watts": 900})
        self.assertEqual((st, json.loads(data)["ok"]), (400, False))           # outside the card's range
        st, _, data = self.req("POST", "/api/apply", {"op": "set_power", "host": "edge-gb10", "uuid": GB10, "watts": 50})
        self.assertEqual(st, 400)
        self.assertIn("not installed", json.loads(data)["error"])

    def test_config_builder_shows_only_made_up_machines_and_checks_drafts(self):
        st, _, data = self.req("GET", "/api/config")
        doc = json.loads(data)
        self.assertEqual((st, doc["demo"], doc["hosts"]["from_file"]), (200, True, False))
        self.assertEqual(doc["hosts"]["running"], {"hosts": [
            {k: v for k, v in h.items() if v} for h in json.loads(json.dumps(demo.hub_hosts()))]})
        self.assertEqual(doc["config"]["template"]["allowed_uid"], 1000)        # a placeholder, not this uid
        self.assertNotIn(socket.gethostname().encode(), data)
        self.assertNotIn(self.tmp.encode(), data)                                # no real path either
        st, _, data = self.req("POST", "/api/validate-config", {"kind": "hosts", "text": '{"hosts": [{"id": "r", "ssh": "x"}]}'})
        r = json.loads(data)
        self.assertEqual((st, r["ok"]), (200, True))
        self.assertNotIn("--init-key", " ".join(r["warnings"]))                  # never looks for a real key
        self.assertEqual(self.req("POST", "/api/validate-config", {"kind": "config", "text": "{}"},
                                  headers={"Origin": "http://evil.example"})[0], 403)

    def test_zz_sigterm_removes_the_temporary_directory(self):
        self.assertTrue(any(n.startswith("gpu-tuner-demo-") for n in os.listdir(self.tmp)))
        self.proc.send_signal(signal.SIGTERM)
        self.proc.wait(timeout=15)
        self.assertFalse(any(n.startswith("gpu-tuner-demo-") for n in os.listdir(self.tmp)))


def _marionette_class():
    """live-ui-test.py's Marionette client (Firefox's own protocol, no geckodriver or selenium)."""
    spec = importlib.util.spec_from_file_location("live_ui_test", os.path.join(HERE, "live-ui-test.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)          # defines things only; its main() runs under __main__
    return mod.Marionette


@unittest.skipUnless(shutil.which("firefox"), "no firefox here: the in-browser checks need it")
class LockedCardInBrowser(unittest.TestCase):
    """In a real (headless) browser: a monitor-only card's controls are all disabled, and calling a
    staging handler anyway changes nothing, while its (i) buttons still open their explanations.
    The demo's Render box loses its daemon (its socket is removed), which is exactly what a machine
    without gpu-tunerd looks like to the page. Also: the config builder shows the draft's sha256
    and checks it with `sha256sum -c` before the install line."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        env = {**os.environ, "HOME": cls.tmp, "TMPDIR": cls.tmp}
        cls.port = free_port()
        cls.proc = subprocess.Popen([sys.executable, ENTRY, "serve", "--demo", "--port", str(cls.port)],
                                    env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.browser, cls.profile_base = None, None
        try:
            deadline = time.monotonic() + 20
            sock = None
            while time.monotonic() < deadline and sock is None:
                dirs = [d for d in os.listdir(cls.tmp) if d.startswith("gpu-tuner-demo-")]
                cand = os.path.join(cls.tmp, dirs[0], "render-box.sock") if dirs else None
                sock = cand if cand and os.path.exists(cand) else None
                time.sleep(0.2)
            if sock is None:
                raise RuntimeError("the demo's Render box daemon never started")
            os.unlink(sock)                   # Render box: no daemon from now on -> monitor-only
            cls.url = subprocess.run([sys.executable, ENTRY, "open", "--demo", "--port", str(cls.port), "--print-url"],
                                     env=env, capture_output=True, text=True, timeout=20).stdout.strip()
            # A snap Firefox reads profiles only under ~/snap/firefox/common (not /tmp, not dot-dirs).
            snap = os.path.expanduser("~/snap/firefox/common")
            cls.profile_base = tempfile.mkdtemp(prefix="gpu-tuner-demo-test-", dir=snap if os.path.isdir(snap) else None)
            profile = os.path.join(cls.profile_base, "profile")
            os.makedirs(profile)
            cls.browser = _marionette_class()(profile, 1400, 1000, port=free_port())
            cls.browser.go(cls.url)           # trades the single-use nonce for the demo cookie
        except Exception:
            cls.tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls):
        if cls.browser:
            cls.browser.quit()
        if cls.proc.poll() is None:
            cls.proc.terminate()
            try:
                cls.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                cls.proc.kill()
                cls.proc.wait()
        if cls.profile_base:
            shutil.rmtree(cls.profile_base, ignore_errors=True)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def until(self, script, seconds=30):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            v = self.browser.js(script)
            if v:
                return v
            time.sleep(0.25)
        self.fail(f"never true in the page: {script[:120]}")

    def test_a_locked_card_disables_its_controls_but_not_its_info_buttons(self):
        b = self.browser
        b.go(f"http://127.0.0.1:{self.port}/#host=render-box")
        visible = ("const fs = () => [...document.querySelectorAll('#hosts fieldset.ctl')]"
                   ".filter(f => !f.hidden && f.getClientRects().length);")
        self.until(visible + "return fs().length === 3 && fs().every(f => f.classList.contains('locked'));")
        r = b.js(visible + """
          const ctl = fs().flatMap(f => [...f.querySelectorAll('input, select, textarea, button:not(.info)')]);
          const infos = fs().flatMap(f => [...f.querySelectorAll('button.info')]);
          return {ctl: ctl.length, enabled: ctl.filter(c => !c.disabled).map(c => c.outerHTML.slice(0, 90)),
                  infos: infos.length, infoDisabled: infos.filter(x => x.disabled).length};""")
        self.assertGreater(r["ctl"], 5)
        self.assertEqual(r["enabled"], [])
        self.assertGreaterEqual(r["infos"], 3)
        self.assertEqual(r["infoDisabled"], 0)

        # Staging handlers return early on a locked card: an input event the browser would not
        # deliver to a disabled control, dispatched anyway, leaves Apply disabled and says nothing.
        r = b.js(visible + """
          const [p, f, c] = fs();
          const slider = p.querySelector('input[type=range]');
          slider.value = String(+slider.min + 10);
          slider.dispatchEvent(new Event('input', {bubbles: true}));
          const box = c.querySelector('input[type=checkbox]');
          box.checked = !box.checked;
          box.dispatchEvent(new Event('change', {bubbles: true}));
          const apply = [...p.querySelectorAll('button.primary'), ...c.querySelectorAll('button.primary')];
          return {applyEnabled: apply.filter(x => !x.disabled).length,
                  msgs: [...p.querySelectorAll('.msg'), ...c.querySelectorAll('.msg')].map(m => m.textContent).join('')};""")
        self.assertEqual(r, {"applyEnabled": 0, "msgs": ""})

        # A real click (WebDriver, not a synthetic one) on the power limit's (i) opens its pop-up.
        el = b.cmd("WebDriver:FindElements", {"using": "css selector",
                                                "value": "#hosts fieldset.ctl.locked legend button.info"})
        shown = [e for e in el if b.cmd("WebDriver:IsElementDisplayed", {"id": list(e.values())[0]})]
        self.assertTrue(shown)
        b.cmd("WebDriver:ElementClick", {"id": list(shown[0].values())[0]})
        r = self.until("""
          const btn = document.activeElement && document.activeElement.id === 'info-pop'
            ? document.querySelector('button.info[aria-expanded=true]') : null;
          const pop = document.getElementById('info-pop');
          return btn && pop && pop.getClientRects().length && /What it is/.test(pop.textContent)
            ? {label: btn.getAttribute('aria-label'), controls: btn.getAttribute('aria-controls')} : null;""")
        self.assertEqual(r["controls"], "info-pop")
        self.assertTrue(r["label"].startswith("About "), r)

    def test_install_commands_check_the_drafts_sha256_first(self):
        b = self.browser
        b.go(f"http://127.0.0.1:{self.port}/")
        self.until("""const t = document.querySelector('#cfg-root button[data-view=text]');
                      if (t && document.querySelector('#cfg-root .cfg-sha')) { t.click(); return true; }""")
        r = self.until("""
          const sha = document.querySelector('#cfg-root .cfg-sha');
          const code = document.querySelector('#cfg-root .cmd code');
          const text = document.querySelector('#cfg-root textarea.cfg-text');
          return sha && /[0-9a-f]{64}/.test(sha.textContent) && code && text && text.value
            ? {sha: sha.textContent, code: code.textContent, draft: text.value} : null;""")
        want = hashlib.sha256(r["draft"].encode()).hexdigest()
        self.assertIn(want, r["sha"])
        lines = r["code"].split("\n")
        # one block that stops at the first failing step: the sha256 check gates everything after it
        self.assertEqual((lines[0], lines[-1]), ("( set -e", ")"), r["code"])
        self.assertTrue(all(line.startswith("  ") for line in lines[1:-1]), r["code"])
        self.assertNotIn("&&", r["code"])           # set -e skips the left side of an && list
        i = next(n for n, line in enumerate(lines) if "sha256sum -c -" in line)
        self.assertEqual(lines[i], f'  echo "{want}  $HOME/Downloads/hosts.json" | sha256sum -c -')
        self.assertEqual(lines[i + 1], '  install -m 600 "$HOME"/Downloads/hosts.json "$HOME"/.config/gpu-tuner/hosts.json')
        # the default directory is the only one made private; a ~ is never quoted
        self.assertEqual(lines[1:3], ["  mkdir -p ~/.config/gpu-tuner", "  chmod 700 ~/.config/gpu-tuner"])
        self.assertNotIn("'~", r["code"])

    def test_config_json_starts_from_this_machine_and_installs_a_checked_root_copy(self):
        b = self.browser
        b.go(f"http://127.0.0.1:{self.port}/")
        self.until("""const t = document.getElementById('cfg-tab-config');
                      if (t && document.querySelector('#cfg-root .cfg-sha')) { t.click(); return true; }""")
        self.until("""const t = document.querySelector('#cfg-root button[data-view=text]');
                      if (t) { t.click(); return true; }""")
        r = self.until("""
          const sha = document.querySelector('#cfg-root .cfg-sha');
          const code = document.querySelector('#cfg-root .cmd code');
          const text = document.querySelector('#cfg-root textarea.cfg-text');
          const loads = [...document.querySelectorAll('#cfg-root .cfg-load button')].map(x => x.textContent);
          const note = [...document.querySelectorAll('#cfg-root p.msg')].find(p => /fallback|another machine/i.test(p.textContent));
          return sha && /[0-9a-f]{64}/.test(sha.textContent) && code && /sha256sum/.test(code.textContent) && text && text.value
            ? {code: code.textContent, draft: text.value, loads, max: text.maxLength,
               note: note ? [note.className, note.textContent] : null,
               diff: document.querySelector('#cfg-root .cfg-panel, #cfg-panel').textContent} : null;""")
        # the demo's local machine runs its daemon with a 750 W budget; the draft says it came from there
        self.assertEqual(json.loads(r["draft"]), {"allowed_uid": 1000, "gpu_budget_w": 750, "interval_s": 2})
        self.assertIn("this machine (Workstation)", r["diff"])
        self.assertIn("This machine", r["loads"])
        self.assertTrue(any("daemon’s 750 W fallback" in x for x in r["loads"]), r["loads"])
        self.assertEqual(r["max"], 65536)
        self.assertNotIn("warn", r["note"][0])        # not the placeholder warning
        want = hashlib.sha256(r["draft"].encode()).hexdigest()
        lines = r["code"].split("\n")
        self.assertEqual((lines[0], lines[-1]), ("( set -e", ")"), r["code"])
        etc = "/etc/gpu-tuner/config.json"
        body = [line.split("      #")[0].strip() for line in lines[1:-1]]
        self.assertEqual(body, [
            "sudo install -d -m 755 /etc/gpu-tuner",
            f'sudo install -o root -g root -m 644 "$HOME"/Downloads/config.json {etc}.new',
            f'echo "{want}  {etc}.new" | sha256sum -c -',
            f"[ ! -f {etc} ] || sudo cp -p {etc} {etc}.bak",
            f"sudo mv {etc}.new {etc}",
            "sudo systemctl restart gpu-tunerd",
            "journalctl -u gpu-tunerd -n 5"])
        # the template is labelled a placeholder, with a warning
        b.js("""[...document.querySelectorAll('#cfg-root .cfg-load button')].find(x => /fallback/.test(x.textContent)).click();""")
        r = self.until("""
          const note = [...document.querySelectorAll('#cfg-root p.msg.warn')].find(p => /placeholder/.test(p.textContent));
          return note ? note.textContent : null;""")
        self.assertIn("750 W is the daemon’s fallback, a placeholder", r)
        self.assertIn("lowers them to each card’s baseline", r)
        # absurd nesting: a clear message, and the page keeps working (no stack overflow)
        r = b.js("""
          const t = document.querySelector('#cfg-root textarea.cfg-text');
          t.value = '{"x": ' + '['.repeat(5000) + ']'.repeat(5000) + '}';
          t.dispatchEvent(new Event('input'));
          return [...document.querySelectorAll('#cfg-root .msg')].map(m => m.textContent).join(' | ');""")
        self.assertIn("nested more than 64 levels deep", r)


class RealPageUnchanged(unittest.TestCase):
    def test_real_state_has_no_demo_flag_and_keeps_its_cookie(self):
        h = hub.Hub([], lambda m: None)
        self.assertNotIn("demo", server.state_json(h))
        self.assertEqual(server.COOKIE, "gpu_tuner")
        self.assertNotEqual(demo.COOKIE, server.COOKIE)
        self.assertNotEqual(demo.DEMO_PORT, server.DEFAULT_PORT)


if __name__ == "__main__":
    unittest.main(verbosity=1)
