#!/usr/bin/env python3
"""Offline tests for the page's help, (i) pop-ups and config builder.

What must hold: the help and info JSON files are well-formed, every (i) button the page creates has
an entry and every entry is used, and the limits they quote match safety.py / hosts.py / daemon.py;
the config checker applies exactly the loaders' rules; and the two new routes keep the page's
sign-in, Host, Origin, content-type and size checks, read nothing they should not, and never
write. No GPU, no root, no network beyond 127.0.0.1, ~2 s.
"""
import contextlib
import http.client
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WEB = os.path.join(ROOT, "web")
sys.path.insert(0, ROOT)

from gpu_tuner import configcheck, daemon, hosts, safety, server  # noqa: E402

daemon.log = lambda msg: None
EXAMPLE = os.path.join(ROOT, "examples", "hosts.example.json")
ENTRY_KEYS = ("title", "what", "change", "limits", "effect")
# Public repo: only loopback and the documentation ranges may appear in help text, and no path
# into anyone's home directory.
PRIVATE = re.compile(r"\b(10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+|172\.(1[6-9]|2\d|3[01])\.\d+\.\d+)\b"
                     r"|(^|[\s`'\"(=])/(home|root|Users)/")
EXTERNAL_URL = re.compile(r"https?://(?!(127\.0\.0\.1|localhost)[:/])")


def load(name):
    with open(os.path.join(WEB, name), encoding="utf-8") as f:
        return json.load(f)


def strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from strings(v)


def js_sources():
    out = ""
    for name in sorted(os.listdir(WEB)):
        if name.endswith(".js"):
            with open(os.path.join(WEB, name), encoding="utf-8") as f:
                out += f.read()
    return out


class HelpFile(unittest.TestCase):
    def setUp(self):
        self.doc = load("help.en.json")

    def test_shape(self):
        self.assertEqual(set(self.doc) - {"_about"}, {"getting_started", "faq"})
        gs = self.doc["getting_started"]
        self.assertTrue(gs["lead"].strip())
        self.assertGreaterEqual(len(gs["steps"]), 3)
        for s in gs["steps"]:
            self.assertLessEqual(set(s), {"title", "body", "cmd"}, s)
            self.assertTrue(s["title"].strip() and s["body"].strip(), s)
        self.assertGreaterEqual(len(self.doc["faq"]), 4)
        for q in self.doc["faq"]:
            self.assertLessEqual(set(q), {"q", "a", "cmd"}, q)
            self.assertTrue(q["q"].strip() and q["a"].strip(), q)
        qs = [q["q"] for q in self.doc["faq"]]
        self.assertEqual(len(qs), len(set(qs)), "duplicate question")

    def test_plain_text_only(self):
        for s in strings(self.doc):
            self.assertEqual(s.count("`") % 2, 0, f"unbalanced backtick: {s[:60]}")
            self.assertIsNone(EXTERNAL_URL.search(s), s[:80])
            self.assertIsNone(PRIVATE.search(s), s[:80])
            self.assertNotRegex(s, r"<[a-zA-Z/]", "looks like markup; the page shows it as text")


class InfoCatalog(unittest.TestCase):
    def setUp(self):
        self.doc = load("info.en.json")
        self.js = js_sources()

    def test_every_entry_is_complete_and_plain(self):
        entries = list(self.doc["controls"].items())
        for file, cat in self.doc["fields"].items():
            entries += [(f"{file}:{k}", v) for k, v in cat.items()]
        self.assertGreater(len(entries), 20)
        for key, e in entries:
            self.assertEqual(set(e), set(ENTRY_KEYS), key)
            for k in ENTRY_KEYS:
                self.assertTrue(isinstance(e[k], str) and e[k].strip(), f"{key}.{k}")
        for s in strings(self.doc):
            self.assertEqual(s.count("`") % 2, 0, f"unbalanced backtick: {s[:60]}")
            self.assertIsNone(EXTERNAL_URL.search(s), s[:80])
            self.assertIsNone(PRIVATE.search(s), s[:80])

    def test_every_info_button_has_an_entry_and_every_entry_a_button(self):
        # \s*: a formatter may put the arguments of a long call on lines of their own
        used = set(re.findall(r"\binfo\(\s*'([^']+)'", self.js))
        self.assertEqual(used, set(self.doc["controls"]))
        # fieldInfo(file, key, ...) directly, or through config.js's field(file, key, ...) helper
        used_fields = set(re.findall(r"\b(?:fieldInfo|field)\(\s*'([^']+)',\s*'([^']+)'", self.js))
        have = {(f, k) for f, cat in self.doc["fields"].items() for k in cat}
        self.assertEqual(used_fields, have)

    def test_the_page_controls_named_in_the_brief_all_have_one(self):
        for k in ("machines", "machine-tabs", "budget", "power-limit", "power-presets", "fan-mode", "fan-curve",
                  "fan-presets", "fan-manual", "fan-auto", "clock-cap", "baseline", "history"):
            self.assertIn(k, self.doc["controls"])

    def test_every_key_either_loader_reads_has_a_field_entry(self):
        hosts_keys = {"hosts", *(f"hosts.#.{k}" for k in hosts.HOST_KEYS),
                      *(f"hosts.#.wall.{k}" for k in hosts.WALL_KEYS)}
        self.assertEqual(set(self.doc["fields"]["hosts"]), hosts_keys)
        self.assertEqual(set(self.doc["fields"]["config"]), set(daemon.CONFIG_KEYS))

    def test_quoted_limits_match_the_code(self):
        c, f = self.doc["controls"], self.doc["fields"]
        curve = c["fan-curve"]["limits"]
        self.assertIn(f"{safety.CURVE_MIN_POINTS} to {safety.CURVE_MAX_POINTS} points", curve)
        self.assertIn(f"from {safety.CURVE_TEMP_MIN_C} °C", curve)
        self.assertIn(f"cooled {safety.HYSTERESIS_C} °C", curve)
        self.assertIn(f"{safety.FAN_FLOOR_MARGIN_C} °C under", c["fan-mode"]["limits"])
        self.assertIn(f"({safety.FAN_FULL_BY_C} °C when", c["fan-mode"]["limits"])
        self.assertIn(f"again in {daemon.FAULT_RETRY_S} s", c["fan-mode"]["limits"])
        self.assertIn(f"every {daemon.FAN_REASSERT_S} s", c["fan-mode"]["change"])
        self.assertIn(f"every {daemon.POWER_CHECK_S} s", c["power-limit"]["effect"])
        self.assertIn(f"From {safety.CLOCK_CAP_MIN_MHZ:,} MHz", c["clock-cap"]["limits"])
        for p in safety.PROFILES:
            self.assertIn(f"{p['match']}: {p['power_min_w']} to {p['power_max_w']} W", c["power-limit"]["limits"])
            self.assertIn(f"{p['baseline_power_w']} W", c["baseline"]["change"])
        for key, preset in safety.CURVE_PRESETS.items():
            self.assertIn(f"{preset['curve'][-1][0]} °C", c["fan-presets"]["what"], key)
        self.assertIn(f"{safety.DEFAULT_GPU_BUDGET_W} W", f["config"]["gpu_budget_w"]["limits"])
        self.assertIn(f"Left out: {int(daemon.DEFAULT_INTERVAL_S)}", f["config"]["interval_s"]["limits"])
        self.assertIn(f"1 to {hosts.MAX_HOSTS} machines", f["hosts"]["hosts"]["limits"])
        self.assertIn(f"{hosts.MAX_FILE:,} bytes", f["hosts"]["hosts"]["limits"])
        self.assertIn(f"1 to {hosts.LABEL_MAX} printable", f["hosts"]["hosts.#.label"]["limits"])
        self.assertIn(f"{hosts.MAX_HOSTS} machines", c["machines"]["limits"])
        # interval_s: "1 to 10 seconds" is the parse_config rule
        self.assertIn("1 to 10 seconds", f["config"]["interval_s"]["limits"])
        for ok in (1, 10):
            daemon.parse_config({"interval_s": ok})
        for bad in (0.99, 10.01):
            with self.assertRaises(daemon.ConfigError):
                daemon.parse_config({"interval_s": bad})


class Checker(unittest.TestCase):
    """configcheck must say yes exactly when the real loader would."""

    def test_hosts_agrees_with_load_hosts(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "hosts.json")
        with open(EXAMPLE) as f:
            example = f.read()
        docs = [example, '{"hosts": [{"id": "local"}]}', '{"hosts": []}', '{"hosts": [{"id": "A"}]}',
                '{"hosts": [{"id": "r", "ssh": "-oProxyCommand=x"}]}', "not json",
                '{"hosts": [{"id": "local", "wall": {"non_gpu_dc_w": NaN, "psu_efficiency": 0.9}}]}',
                '{"hosts": [{"id": "r", "ssh": "a", "port": 70000}]}', "[" * 20000 + "]" * 20000,
                '{"hosts": [{"id": "x", "ssh": "a"}], "hosts": [{"id": "local"}]}']
        for text in docs:
            with open(path, "w") as f:
                f.write(text)
            os.chmod(path, 0o600)
            try:
                hosts.load_hosts(path)
                loads = True
            except hosts.HostsError:
                loads = False
            r = configcheck.check("hosts", text, key_file=os.path.join(tmp, "no-key"))
            self.assertEqual(r["ok"], loads, text[:60])
            self.assertEqual(bool(r["errors"]), not loads)

    def test_hosts_over_the_size_cap(self):
        text = '{"hosts": [{"id": "local", "label": "' + "x" * hosts.MAX_FILE + '"}]}'
        r = configcheck.check("hosts", text)
        self.assertFalse(r["ok"])
        self.assertIn(f"over {hosts.MAX_FILE} bytes", r["errors"][0])

    def test_config_over_the_size_cap_is_this_pages_limit(self):
        r = configcheck.check("config", '{"x": "' + "x" * configcheck.MAX_TEXT + '"}')
        self.assertFalse(r["ok"])
        self.assertIn("this page's limit for a draft", r["errors"][0])
        self.assertIn("gpu-tunerd itself sets no size limit", r["errors"][0])

    def test_unpaired_surrogates_are_a_verdict_not_an_exception(self):
        """A lone surrogate has no UTF-8 form: str.encode() raised UnicodeEncodeError (a 500)."""
        lone = chr(0xD800)
        for kind, text in (("hosts", '{"hosts": [{"id": "local", "label": "%s"}]}' % lone),
                           ("hosts", lone), ("config", '{"x": "%s"}' % lone), ("config", lone + "{}")):
            r = configcheck.check(kind, text, page_uid=1000)
            self.assertFalse(r["ok"], (kind, text))
            self.assertIn("U+D800, an unpaired surrogate", r["errors"][0])
        self.assertFalse(configcheck.check_hosts(lone)["ok"])
        self.assertFalse(configcheck.check_config(lone)["ok"])
        # a paired surrogate (an emoji) is ordinary text
        self.assertTrue(configcheck.check("hosts", '{"hosts": [{"id": "local", "label": "\U0001F600"}]}')["ok"])

    def test_hosts_warnings(self):
        tmp = tempfile.mkdtemp()
        r = configcheck.check("hosts", '{"hosts": [{"id": "r", "ssh": "box", "ssh": "box2"}]}',
                              key_file=os.path.join(tmp, "missing"))
        self.assertTrue(r["ok"])
        w = " ".join(r["warnings"])
        self.assertIn("'ssh' appears twice", w)
        self.assertIn('no "local" entry', w)
        self.assertIn("--init-key", w)
        self.assertEqual(r["machines"], [{"id": "r", "label": "r", "remote": True}])
        # demo mode never looks at the real key file
        r = configcheck.check("hosts", '{"hosts": [{"id": "r", "ssh": "box"}]}', demo=True)
        self.assertNotIn("--init-key", " ".join(r["warnings"]))

    def test_config_agrees_with_load_config(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "config.json")
        docs = ['{"allowed_uid": 1000, "gpu_budget_w": 800, "interval_s": 3}', '{"gpu_budget_w": null}', "{}",
                '{"gpu_budget_w": -1}', '{"gpu_budget_w": true}', '{"allowed_uid": "me"}', '{"interval_s": 0.1}',
                "[]", '{"interval_s": NaN}', '{"allowed_uid": 1.5}', '{"other": 1}']
        for text in docs:
            with open(path, "w") as f:
                f.write(text)
            try:
                want = daemon.load_config(path)
            except SystemExit:
                want = None
            r = configcheck.check("config", text, page_uid=1000)
            self.assertEqual(r["ok"], want is not None, text)
            if want is not None:
                self.assertEqual(r["effective"], want, text)

    def test_config_warnings_and_template(self):
        t = configcheck.config_template(1000)
        r = configcheck.check("config", json.dumps(t), page_uid=1000)
        # the template's budget is the daemon's fallback: every check says what a budget does on start
        self.assertEqual((r["ok"], r["warnings"]), (True, [configcheck.budget_effect(safety.DEFAULT_GPU_BUDGET_W)]))
        self.assertEqual(r["effective"]["gpu_budget_w"], safety.DEFAULT_GPU_BUDGET_W)
        self.assertIn(f"lowers saved power limits to each card's baseline (or its minimum) if they total more "
                      f"than {safety.DEFAULT_GPU_BUDGET_W} W", r["warnings"][0])
        self.assertIn("Power budget panel before installing", r["warnings"][0])
        w = configcheck.check("config", '{"allowed_uid": 1000, "gpu_budget_w": 1200}', page_uid=1000)["warnings"]
        self.assertEqual(w, [configcheck.budget_effect(1200)])
        self.assertIn("more than 1200 W", w[0])
        # no budget (null): nothing to lower, so no such warning
        r = configcheck.check("config", '{"allowed_uid": 1000, "gpu_budget_w": null}', page_uid=1000)
        self.assertEqual((r["ok"], r["warnings"]), (True, []))
        # left out: both the fallback and its effect are named
        w = " ".join(configcheck.check("config", '{"allowed_uid": 1000}', page_uid=1000)["warnings"])
        self.assertIn(f"falls back to {safety.DEFAULT_GPU_BUDGET_W} W", w)
        self.assertIn(f"more than {safety.DEFAULT_GPU_BUDGET_W} W", w)
        w = " ".join(configcheck.check("config", '{"foo": 1}', page_uid=1000)["warnings"])
        self.assertIn("ignored by gpu-tunerd: foo", w)
        self.assertIn("root-only", w)
        self.assertIn(f"falls back to {safety.DEFAULT_GPU_BUDGET_W} W", w)
        w = " ".join(configcheck.check("config", '{"allowed_uid": 1001}', page_uid=1000)["warnings"])
        self.assertIn("runs as uid 1000", w)

    def test_huge_numbers_are_refused_not_raised(self):
        """A 400-digit number in any numeric field is the loaders' normal error, never an
        OverflowError (math.isfinite on a huge int raised one, which dropped the connection and
        crashed `gpu-tuner serve` at startup)."""
        big = "9" * 400
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "hosts.json")
        wall = '"non_gpu_dc_w": {dc}, "psu_efficiency": {eff}, "circuits": {{"15 A": {c}}}'
        hosts_docs = {
            "port": '{"hosts": [{"id": "r", "ssh": "a", "port": %s}]}' % big,
            "non_gpu_dc_w": '{"hosts": [{"id": "local", "wall": {%s}}]}' % wall.format(dc=big, eff=0.9, c=1440),
            "psu_efficiency": '{"hosts": [{"id": "local", "wall": {%s}}]}' % wall.format(dc=630, eff=big, c=1440),
            "circuits": '{"hosts": [{"id": "local", "wall": {%s}}]}' % wall.format(dc=630, eff=0.9, c=big),
        }
        for field, text in hosts_docs.items():
            for sign in ("", "-"):
                t = text.replace(big, sign + big)
                r = configcheck.check("hosts", t, key_file=os.path.join(tmp, "no-key"))
                self.assertFalse(r["ok"], field)
                self.assertIn(field if field != "circuits" else "wall.circuits", r["errors"][0])
                with open(path, "w") as f:
                    f.write(t)
                os.chmod(path, 0o600)
                with self.assertRaises(hosts.HostsError, msg=field):
                    hosts.load_hosts(path)
        for field in daemon.CONFIG_KEYS:
            for sign in ("", "-"):
                r = configcheck.check("config", '{"%s": %s%s}' % (field, sign, big), page_uid=1000)
                self.assertFalse(r["ok"], field)
                self.assertIn(field, r["errors"][0])
                with self.assertRaises(daemon.ConfigError, msg=field):
                    daemon.parse_config({field: int(sign + big)})

    def test_config_upper_bounds(self):
        # uid_t is 32 bits and 4294967295 means "unchanged" to chown(2): the daemon would crash on it
        self.assertEqual(daemon.parse_config({"allowed_uid": daemon.MAX_UID})["allowed_uid"], 4294967294)
        for bad in (4294967295, 2**32, 2**63):
            with self.assertRaises(daemon.ConfigError):
                daemon.parse_config({"allowed_uid": bad})
        top = safety.MAX_GPU_BUDGET_W
        self.assertEqual(daemon.parse_config({"gpu_budget_w": top})["gpu_budget_w"], top)
        with self.assertRaises(daemon.ConfigError):
            daemon.parse_config({"gpu_budget_w": top + 1})
        # anything install.sh or Set budget could have written still loads: both stay under the sum
        # of the cards' hardware maximums, a few kW on any real machine
        for real in (1, 325, 925, 1200, 4800, 16 * 1500):
            self.assertEqual(daemon.parse_config({"gpu_budget_w": real})["gpu_budget_w"], real)
        f = load("info.en.json")["fields"]["config"]
        self.assertIn(f"from 1 to {top}", f["gpu_budget_w"]["limits"])
        self.assertIn(f"from 0 to {daemon.MAX_UID}", f["allowed_uid"]["limits"])

    def test_bad_requests_raise(self):
        for kind, text in (("state", "{}"), ("hosts", 5), ("config", None)):
            with self.assertRaises(ValueError):
                configcheck.check(kind, text)

    def test_running_hosts_round_trip_through_the_loader(self):
        with open(EXAMPLE) as f:
            parsed = hosts.parse_hosts(json.load(f))
        doc = configcheck.hosts_doc(parsed)
        self.assertEqual(hosts.parse_hosts(doc), parsed)
        self.assertEqual(set(doc), {"hosts"})
        for h in doc["hosts"]:
            self.assertLessEqual(set(h), hosts.HOST_KEYS)

    def test_example_is_served_only_if_valid(self):
        self.assertIsNotNone(configcheck.example_doc())
        tmp = tempfile.mkdtemp()
        bad = os.path.join(tmp, "bad.json")
        with open(bad, "w") as f:
            f.write('{"hosts": [{"id": "r", "ssh": "-x"}]}')
        self.assertIsNone(configcheck.example_doc(bad))
        self.assertIsNone(configcheck.example_doc(os.path.join(tmp, "missing.json")))


@unittest.skipUnless(shutil.which("node"), "no node here: the page's pure helpers are checked with it")
class PageHelpers(unittest.TestCase):
    """config.js's pure helpers, run in node: the shell paths the install commands are built from,
    and the nesting guard (an absurdly nested draft must be a message, never a stack overflow)."""

    def run_js(self, expr):
        with open(os.path.join(WEB, "config.js"), encoding="utf-8") as f:
            src = f.read()
        # only the top-level helpers, up to the panel's IIFE (which needs a DOM)
        src = src[:src.index("(function configBuilder()")]
        prog = src + "\nprocess.stdout.write(JSON.stringify(" + expr + "));\n"
        out = subprocess.run(["node", "-e", prog], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr[-500:])
        return json.loads(out.stdout)

    def test_shell_paths(self):
        got = self.run_js("""[shPath('~'), shPath('~/'), shPath('~/.config/gpu-tuner/hosts.json'),
            shPath('~/my dir/h.json'), shPath("/srv/it's.json"), shPath('/plain/path.json'),
            parentDir('~/h.json'), parentDir('/h.json'), parentDir('h.json'), parentDir('/a/b/h.json')]""")
        self.assertEqual(got, ['"$HOME"', '"$HOME"', '"$HOME"/.config/gpu-tuner/hosts.json',
                               '"$HOME"/\'my dir/h.json\'', "'/srv/it'\\''s.json'", "/plain/path.json",
                               "~", "/", ".", "/a/b"])
        # "$HOME"/x really is the home path in a shell, and a quoted ~ would not have been
        sh = subprocess.run(["bash", "-c", "printf %s " + got[3]], capture_output=True, text=True,
                            env={"HOME": "/h", "PATH": os.environ.get("PATH", "")})
        self.assertEqual(sh.stdout, "/h/my dir/h.json")

    def test_absurd_nesting_is_a_message(self):
        got = self.run_js("""[parseDraft('{"x": ' + '['.repeat(200000) + ']'.repeat(200000) + '}'),
            parseDraft('{"x": ' + '['.repeat(63) + ']'.repeat(63) + '}').ok,
            parseDraft('{"x": ' + '['.repeat(64) + ']'.repeat(64) + '}').ok,
            formatDoc('hosts', {hosts: [{id: 'local'}]}),
            parseDraft('{"a": 1').ok,
            formatDoc('config', (() => { let o = 1; for (let i = 0; i < 200000; i++) o = [o]; return o; })()),
            formatDoc('hosts', {hosts: [(() => { let o = 1; for (let i = 0; i < 200000; i++) o = {o}; return o; })()]})]""")
        bad = got[0]
        self.assertFalse(bad["ok"])
        self.assertTrue("nested more than 64 levels" in bad["error"] or "nested too deeply" in bad["error"], bad)
        self.assertEqual(got[1:], [True, False, '{"hosts": [\n  {"id": "local"}\n]}\n', False, None, None])


class _Hub:
    """Just what the two new routes use: the machines the page runs with."""
    def __init__(self, machines):
        self.hosts, self.links = machines, {m["id"]: None for m in machines}


class Routes(unittest.TestCase):
    TOKEN = "t" * 43

    def serve(self, **kw):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        with open(EXAMPLE) as f:
            h = _Hub(hosts.parse_hosts(json.load(f)))
        httpd = ThreadingHTTPServer(("127.0.0.1", port), server.make_handler(h, self.TOKEN, port, **kw))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return port

    def req(self, port, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
        h = {"Host": f"127.0.0.1:{port}", "Cookie": f"gpu_tuner={self.TOKEN}"}
        if body is not None:
            h.update({"Origin": f"http://127.0.0.1:{port}", "Content-Type": "application/json"})
            body = body if isinstance(body, bytes) else json.dumps(body).encode()
        h.update(headers or {})
        h = {k: v for k, v in h.items() if v is not None}
        c.request(method, path, body, h)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, dict(r.getheaders()), data

    def test_static_files_are_served_with_the_csp(self):
        port = self.serve()
        for path in ("/help.js", "/config.js", "/help.en.json", "/info.en.json"):
            st, hd, data = self.req(port, "GET", path)
            self.assertEqual(st, 200, path)
            self.assertIn("default-src 'none'", hd["Content-Security-Policy"])
            self.assertEqual(hd["X-Content-Type-Options"], "nosniff")
            self.assertEqual(hd["Cross-Origin-Resource-Policy"], "same-origin")
            self.assertEqual(hd["Cross-Origin-Opener-Policy"], "same-origin")
            if path.endswith(".json"):
                json.loads(data)
        self.assertEqual(self.req(port, "GET", "/help.en.json", headers={"Cookie": None})[0], 401)
        self.assertEqual(self.req(port, "GET", "/../examples/hosts.example.json")[0], 404)

    def test_config_route_needs_sign_in_and_host(self):
        port = self.serve()
        self.assertEqual(self.req(port, "GET", "/api/config", headers={"Cookie": None})[0], 401)
        self.assertEqual(self.req(port, "GET", "/api/config", headers={"Host": "evil.example:80"})[0], 421)
        self.assertEqual(self.req(port, "POST", "/api/config", {"kind": "hosts", "text": "{}"})[0], 404)

    def test_a_non_ascii_cookie_is_a_401_not_a_dropped_connection(self):
        """hmac.compare_digest raised TypeError on a non-ASCII str (a quoted cookie decodes to one),
        which dropped the connection. Now: 401 like any wrong cookie, and the server keeps serving."""
        port = self.serve()
        for bad in ('gpu_tuner="\xe9"', 'gpu_tuner="t\xff\xfe"', 'gpu_tuner="' + "\xe9" * 43 + '"'):
            for method, path, body in (("GET", "/api/config", None),
                                       ("POST", "/api/validate-config", {"kind": "config", "text": "{}"})):
                st, hd, _ = self.req(port, method, path, body, headers={"Cookie": bad})
                self.assertEqual(st, 401, (bad, path))
                self.assertEqual(hd["Cross-Origin-Resource-Policy"], "same-origin")
        self.assertEqual(self.req(port, "GET", "/api/config")[0], 200)

    def test_config_route_contents(self):
        home = os.path.expanduser("~")
        port = self.serve(hosts_from_file=True, hosts_path=os.path.join(home, ".config", "gpu-tuner", "hosts.json"))
        st, _, data = self.req(port, "GET", "/api/config")
        doc = json.loads(data)
        self.assertEqual(st, 200)
        self.assertEqual(doc["hosts"]["path"], "~/.config/gpu-tuner/hosts.json")
        self.assertTrue(doc["hosts"]["from_file"])
        self.assertEqual([h["id"] for h in doc["hosts"]["running"]["hosts"]], ["local", "gpu-box-2", "edge-1"])
        self.assertEqual(doc["config"]["template"]["allowed_uid"], os.getuid())
        self.assertIs(doc["demo"], False)
        self.assertEqual(configcheck.check("config", json.dumps(doc["config"]["template"]))["ok"], True)

    def test_config_route_in_demo_reports_nothing_real(self):
        port = self.serve(demo=True, hosts_from_file=True, hosts_path="/somewhere/else.json")
        doc = json.loads(self.req(port, "GET", "/api/config")[2])
        self.assertEqual((doc["demo"], doc["hosts"]["from_file"]), (True, False))
        self.assertEqual(doc["hosts"]["path"], "~/.config/gpu-tuner/hosts.json")
        self.assertEqual(doc["config"]["template"]["allowed_uid"], configcheck.DEMO_UID)

    def test_validate_route_checks(self):
        port = self.serve()
        ok = {"kind": "hosts", "text": '{"hosts": [{"id": "local"}]}'}
        st, _, data = self.req(port, "POST", "/api/validate-config", ok)
        self.assertEqual((st, json.loads(data)["ok"]), (200, True))
        self.assertEqual(self.req(port, "POST", "/api/validate-config", ok, headers={"Cookie": None})[0], 401)
        self.assertEqual(self.req(port, "POST", "/api/validate-config", ok, headers={"Host": "evil.example:80"})[0], 421)
        self.assertEqual(self.req(port, "POST", "/api/validate-config", ok, headers={"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.req(port, "POST", "/api/validate-config", ok, headers={"Origin": None})[0], 403)
        self.assertEqual(self.req(port, "POST", "/api/validate-config", ok, headers={"Content-Type": "text/plain"})[0], 415)
        for bad in ({"kind": "state", "text": "{}"}, {"kind": "hosts"}, {"kind": "hosts", "text": 1},
                    {"kind": "hosts", "text": "{}", "write": True}, b'{"kind": "hosts", "text": NaN}', b"[]"):
            self.assertEqual(self.req(port, "POST", "/api/validate-config", bad)[0], 400, bad)
        big = {"kind": "hosts", "text": '"' * (server.VALIDATE_MAX_BODY // 2 + 10)}
        self.assertEqual(self.req(port, "POST", "/api/validate-config", big)[0], 400)
        self.assertEqual(self.req(port, "GET", "/api/validate-config")[0], 404)

    def test_validate_route_answers_a_bad_draft_without_echoing_it(self):
        port = self.serve()
        secretish = "sk-THISMUSTNOTCOMEBACK"
        for kind, text in (("hosts", '{"hosts": [{"id": "r", "ssh": "-o' + secretish + '"}]}'),
                           ("hosts", '{"hosts": [{"id": "r", "label": "' + secretish + '\\u0007"}]}'),
                           ("config", '{"allowed_uid": "' + secretish + '"}'),
                           ("config", '{"x": "' + secretish)):
            st, _, data = self.req(port, "POST", "/api/validate-config", {"kind": kind, "text": text})
            self.assertEqual(st, 200)
            self.assertFalse(json.loads(data)["ok"])
            self.assertNotIn(secretish.encode(), data)

    def test_validate_route_answers_500_on_a_checker_bug(self):
        port = self.serve()
        real = configcheck.check

        def boom(*_a, **_k):
            raise OverflowError("secret-ish detail sk-NOTBACK")
        configcheck.check = boom
        log = io.StringIO()
        try:
            with contextlib.redirect_stderr(log):
                st, _, data = self.req(port, "POST", "/api/validate-config", {"kind": "config", "text": "{}"})
        finally:
            configcheck.check = real
        self.assertEqual(log.getvalue(), "gpu-tuner: validate-config failed: OverflowError\n")   # type only
        self.assertEqual(st, 500)
        self.assertEqual(json.loads(data), {"ok": False, "error": "internal error"})
        self.assertNotIn(b"Traceback", data)
        self.assertNotIn(b"sk-NOTBACK", data)
        # the slot was released: the next check runs
        self.assertEqual(self.req(port, "POST", "/api/validate-config", {"kind": "config", "text": "{}"})[0], 200)

    def test_validate_route_answers_a_lone_surrogate_normally(self):
        port = self.serve()
        for kind, text in (("hosts", '{"hosts": [{"id": "local", "label": "\ud800"}]}'), ("config", '{"x": "\udfff"}')):
            # json.dumps sends it as a \\ud800 escape; the server's json.loads makes it a lone surrogate
            body = json.dumps({"kind": kind, "text": text}).encode()
            self.assertIn(b"\\ud", body)
            st, _, data = self.req(port, "POST", "/api/validate-config", body)
            self.assertEqual(st, 200, kind)
            r = json.loads(data)
            self.assertFalse(r["ok"], kind)
            self.assertIn("unpaired surrogate", r["errors"][0])

    def test_validate_route_huge_numbers_answer_normally(self):
        port = self.serve()
        big = "9" * 400
        for kind, text in (("hosts", '{"hosts": [{"id": "local", "wall": {"non_gpu_dc_w": %s, "psu_efficiency": 0.9}}]}' % big),
                           ("hosts", '{"hosts": [{"id": "r", "ssh": "a", "port": %s}]}' % big),
                           ("config", '{"allowed_uid": %s}' % big), ("config", '{"gpu_budget_w": %s}' % big)):
            st, _, data = self.req(port, "POST", "/api/validate-config", {"kind": kind, "text": text})
            self.assertEqual(st, 200, text[:60])
            self.assertFalse(json.loads(data)["ok"], text[:60])

    def test_a_slow_body_never_holds_a_validate_slot(self):
        real = (server.VALIDATE_SLOTS, server.BODY_DEADLINE_S)
        server.VALIDATE_SLOTS, server.BODY_DEADLINE_S = 1, 1.5
        try:
            port = self.serve()
            slow = socket.create_connection(("127.0.0.1", port), timeout=10)
            self.addCleanup(slow.close)
            slow.sendall((f"POST /api/validate-config HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                          f"Cookie: gpu_tuner={self.TOKEN}\r\nOrigin: http://127.0.0.1:{port}\r\n"
                          "Content-Type: application/json\r\nContent-Length: 100\r\n\r\n{\"kind\"").encode())
            time.sleep(0.3)                    # the slow request is now reading its body
            ok = {"kind": "config", "text": "{}"}
            self.assertEqual(self.req(port, "POST", "/api/validate-config", ok)[0], 200)
            t0 = time.monotonic()
            for _ in range(3):                 # a byte now and then must not extend the deadline
                time.sleep(0.4)
                try:
                    slow.sendall(b" ")
                except OSError:
                    break
            answer = b""
            while True:
                chunk = slow.recv(4096)
                if not chunk:
                    break
                answer += chunk
            self.assertTrue(answer.startswith(b"HTTP/1.0 408") or answer.startswith(b"HTTP/1.1 408"), answer[:40])
            self.assertLess(time.monotonic() - t0, 5)
            self.assertEqual(self.req(port, "POST", "/api/validate-config", ok)[0], 200)
        finally:
            server.VALIDATE_SLOTS, server.BODY_DEADLINE_S = real

    def test_a_missing_page_file_is_a_clean_404(self):
        tmp = tempfile.mkdtemp()
        real = server.WEB
        server.WEB = tmp                       # an incomplete install: no web/ files at all
        try:
            port = self.serve()
            for path in ("/", "/help.js", "/info.en.json"):
                st, _, data = self.req(port, "GET", path)
                self.assertEqual(st, 404, path)
                self.assertEqual(json.loads(data), {"error": "not found"})
        finally:
            server.WEB = real

    def test_validate_route_has_a_concurrency_limit(self):
        real = server.VALIDATE_SLOTS
        server.VALIDATE_SLOTS = 0          # every slot taken
        try:
            port = self.serve()
        finally:
            server.VALIDATE_SLOTS = real
        st, _, _ = self.req(port, "POST", "/api/validate-config", {"kind": "config", "text": "{}"})
        self.assertEqual(st, 429)

    def test_nothing_is_written(self):
        tmp = tempfile.mkdtemp()
        hp = os.path.join(tmp, "hosts.json")
        with open(hp, "w") as f:
            f.write('{"hosts": [{"id": "local"}]}')
        before = {n: os.stat(os.path.join(tmp, n)).st_mtime_ns for n in os.listdir(tmp)}
        cwd = os.getcwd()
        os.chdir(tmp)
        try:
            port = self.serve(hosts_from_file=True, hosts_path=hp)
            self.req(port, "GET", "/api/config")
            for kind in ("hosts", "config"):
                self.req(port, "POST", "/api/validate-config", {"kind": kind, "text": '{"hosts": [{"id": "x", "ssh": "y"}]}'})
        finally:
            os.chdir(cwd)
        self.assertEqual({n: os.stat(os.path.join(tmp, n)).st_mtime_ns for n in os.listdir(tmp)}, before)


if __name__ == "__main__":
    unittest.main(verbosity=1)
