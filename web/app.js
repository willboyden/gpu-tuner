'use strict';
/* gpu-tuner page. No framework, no CDN, no inline script or style (the server's CSP forbids
   both). Every limit shown here is a COPY for display: each machine's own gpu-tunerd re-validates
   every request, so nothing in this file can widen what any card will accept.
   Dynamic text is only ever inserted as text nodes, never as HTML — which matters more now that
   some of it (GPU names, process names, errors) comes from other machines. */

const NS = 'http://www.w3.org/2000/svg';
const $ = (s, r = document) => r.querySelector(s);

function mk(create, tag, props, kids) {
  const n = create(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (k.startsWith('on')) n.addEventListener(k.slice(2), v);
    else if (v !== false && v != null) n.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat(Infinity)) if (kid != null && kid !== false) n.append(kid);
  return n;
}
const el = (tag, props, ...kids) => mk(t => document.createElement(t), tag, props, kids);
const sv = (tag, props, ...kids) => mk(t => document.createElementNS(NS, t), tag, props, kids);

const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
const n0 = v => (v == null ? '–' : Math.round(v).toLocaleString('en-US'));
const n1 = v => (v == null ? '–' : Number(v).toLocaleString('en-US', { maximumFractionDigits: 1 }));
const gib = mib => (mib == null ? '–' : (mib / 1024).toLocaleString('en-US', { maximumFractionDigits: 1 }));
const hhmm = (t, secs) => new Date(t * 1000).toLocaleTimeString([], {
  hour: '2-digit', minute: '2-digit', second: secs ? '2-digit' : undefined, hour12: false });
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const shortName = name => (name || '').replace(/^NVIDIA\s+/, '');
// The first temperature the card itself acts on. T.Limit on discrete cards (92 °C, slowdown 95),
// but a GB10 reports T.Limit 99 °C with slowdown at 86 and shutdown at 90, so take whichever of the
// reported thresholds is lowest, and name it. Ties keep T.Limit.
const THRESHOLD_NAMES = [['t_limit', 'T.Limit'], ['slowdown', 'slowdown'], ['shutdown', 'shutdown']];
function thermalRef(th) {
  let best = null;
  for (const [k, name] of THRESHOLD_NAMES) {
    const v = (th || {})[k];
    if (v && (!best || v < best.v)) best = { v, name };
  }
  return best;
}

const S = { st: null, sel: null, views: new Map(), hist: null, win: 300, hoverT: null, charts: [],
  tableOpen: false, signedOut: false };

// ── theme ────────────────────────────────────────────────────────────────────────────────────
(function theme() {
  let saved = null;
  try { saved = localStorage.getItem('gpu-tuner-theme'); } catch (e) { /* private mode */ }
  if (saved) document.documentElement.dataset.theme = saved;
  $('#theme').addEventListener('click', () => {
    const dark = document.documentElement.dataset.theme
      ? document.documentElement.dataset.theme === 'dark'
      : matchMedia('(prefers-color-scheme: dark)').matches;
    const next = dark ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem('gpu-tuner-theme', next); } catch (e) { /* ignore */ }
  });
})();

// ── api ──────────────────────────────────────────────────────────────────────────────────────
async function getJSON(url) {
  const r = await fetch(url, { credentials: 'same-origin' });
  if (r.status === 401) { S.signedOut = true; throw new Error('signed out'); }
  if (!r.ok) throw new Error('HTTP ' + r.status);
  S.signedOut = false;
  return r.json();
}
// Every change names its machine: the server routes by host, never by GPU UUID alone.
async function apply(host, body) {
  try {
    const r = await fetch('/api/apply', { method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(Object.assign({ host }, body)) });
    return await r.json();
  } catch (e) {
    return { ok: false, error: 'could not reach the gpu-tuner server: ' + e.message };
  }
}
const hostOf = id => (S.st ? S.st.hosts.find(h => h.id === id) : null);
const multi = () => !!S.st && S.st.hosts.length > 1;
const selView = () => S.views.get(S.sel);
const selCards = () => (selView() ? selView().cards : []);
const wallEstimate = (wall, caps) => (wall ? Math.round((wall.non_gpu_dc_w + caps) / wall.psu_efficiency) : null);
const powerSettable = st => (st.power_settable != null ? st.power_settable : st.gpus.some(g => g.power_range));

// ── fan curve editor ─────────────────────────────────────────────────────────────────────────
const interp = (curve, t) => {
  if (t <= curve[0][0]) return curve[0][1];
  if (t >= curve[curve.length - 1][0]) return curve[curve.length - 1][1];
  for (let i = 0; i < curve.length - 1; i++) {
    const [t0, f0] = curve[i], [t1, f1] = curve[i + 1];
    if (t >= t0 && t <= t1) return f0 + (f1 - f0) * (t - t0) / (t1 - t0);
  }
  return 100;
};

class CurveEditor {
  constructor(card, onChange) {
    this.card = card; this.onChange = onChange;
    this.curve = [[30, 30], [80, 100]]; this.applied = null; this.live = null;
    this.disabled = true; this.drag = null; this.focusIdx = null;
    this.X = [20, 95]; this.H = 230; this.M = { l: 34, r: 10, t: 10, b: 24 };
    this.svg = sv('svg', { class: 'curve', role: 'group', 'aria-label': 'Fan curve editor' });
    this.svg.addEventListener('pointerdown', e => this.down(e));
    this.svg.addEventListener('pointermove', e => this.move(e));
    this.svg.addEventListener('pointerup', () => this.up());
    this.svg.addEventListener('pointercancel', () => this.up());
    this.svg.addEventListener('dblclick', e => this.dbl(e));
    this.svg.addEventListener('keydown', e => this.key(e));
    new ResizeObserver(() => this.render()).observe(this.svg);
  }
  // floor/fullBy are THIS card's own (a mixed-card box can have different slowdown thresholds
  // per card); tMin/maxPts are per-machine constants from that machine's daemon.
  env() { const d = this.card.d(), st = this.card.hv.st(); return { floor: d.floor, fullBy: d.full_by_c, tMin: st.curve_temp_min_c, maxPts: st.curve_max_points }; }
  px(t) { return this.M.l + (t - this.X[0]) / (this.X[1] - this.X[0]) * (this.W - this.M.l - this.M.r); }
  py(f) { return this.M.t + (1 - f / 100) * (this.H - this.M.t - this.M.b); }
  at(e) {
    const r = this.svg.getBoundingClientRect(), x = e.clientX - r.left, y = e.clientY - r.top;
    return { x, y,
      t: this.X[0] + (x - this.M.l) / (this.W - this.M.l - this.M.r) * (this.X[1] - this.X[0]),
      f: (1 - (y - this.M.t) / (this.H - this.M.t - this.M.b)) * 100 };
  }
  nearest(p, within) {
    let best = -1, bd = within;
    this.curve.forEach(([t, f], i) => {
      const d = Math.hypot(this.px(t) - p.x, this.py(f) - p.y);
      if (d < bd) { bd = d; best = i; }
    });
    return best;
  }
  place(i, t, f) {
    const c = this.curve, last = c.length - 1, e = this.env();
    const tLo = i === 0 ? e.tMin : c[i - 1][0] + 1, tHi = i === last ? e.fullBy : c[i + 1][0] - 1;
    const fLo = i === 0 ? this.fanMin : c[i - 1][1], fHi = i === last ? 100 : c[i + 1][1];
    // The last point is pinned at 100%: every curve must reach full speed by fullBy.
    c[i] = [clamp(Math.round(t), tLo, tHi), i === last ? 100 : clamp(Math.round(f), fLo, fHi)];
    this.onChange(c.map(p => p.slice()));
  }
  down(e) {
    if (this.disabled) return;
    const i = this.nearest(this.at(e), 18);
    if (i < 0) return;
    this.drag = i; this.focusIdx = i;
    this.svg.setPointerCapture(e.pointerId);
    this.render();
  }
  move(e) { if (this.drag != null) { const p = this.at(e); this.place(this.drag, p.t, p.f); } }
  up() { if (this.drag != null) { this.drag = null; this.render(); } }
  dbl(e) {
    if (this.disabled) return;
    const p = this.at(e), c = this.curve, hit = this.nearest(p, 14);
    if (hit >= 0) {
      if (c.length > 2 && hit !== c.length - 1) { c.splice(hit, 1); this.focusIdx = null; this.onChange(c.map(q => q.slice())); }
      return;
    }
    const t = Math.round(p.t);
    if (c.length >= this.env().maxPts || t >= c[c.length - 1][0] || t < this.env().tMin) return;
    let k = c.findIndex(q => q[0] >= t);
    if (c[k][0] === t || (k > 0 && t - c[k - 1][0] < 1)) return;
    c.splice(k, 0, [t, 0]);
    this.focusIdx = k;
    this.place(k, t, p.f);
  }
  key(e) {
    const g = e.target.closest && e.target.closest('g.point');
    if (!g || this.disabled) return;
    const i = Number(g.dataset.i), [t, f] = this.curve[i];
    const step = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, 1], ArrowDown: [0, -1] }[e.key];
    if (step) { e.preventDefault(); this.focusIdx = i; this.place(i, t + step[0], f + step[1]); }
    else if ((e.key === 'Delete' || e.key === 'Backspace') && this.curve.length > 2 && i !== this.curve.length - 1) {
      e.preventDefault(); this.curve.splice(i, 1); this.focusIdx = null; this.onChange(this.curve.map(q => q.slice()));
    }
  }
  set(curve, applied, fanMin, tLimit, disabled) {
    this.curve = curve.map(p => p.slice()); this.applied = applied; this.fanMin = fanMin;
    this.tLimit = tLimit; this.disabled = disabled;
    this.render();
  }
  setLive(temp, fan) { this.live = temp == null ? null : { temp, fan }; if (this.drag == null) this.render(); }
  render() {
    this.W = this.svg.clientWidth;
    if (!this.W || !S.st || !this.card.d()) return;
    const { floor, fullBy } = this.env(), g = [], x0 = this.X[0], x1 = this.X[1];
    this.svg.setAttribute('viewBox', `0 0 ${this.W} ${this.H}`);
    this.svg.setAttribute('height', this.H);
    this.svg.classList.toggle('disabled', this.disabled);
    for (const f of [0, 25, 50, 75, 100]) {
      g.push(sv('line', { class: f ? 'gridline' : 'axisline', x1: this.px(x0), x2: this.px(x1), y1: this.py(f), y2: this.py(f) }));
      g.push(sv('text', { x: this.M.l - 6, y: this.py(f) + 4, 'text-anchor': 'end' }, f + '%'));
    }
    for (let t = x0; t <= x1; t += 10) g.push(sv('text', { x: this.px(t), y: this.H - 6, 'text-anchor': 'middle' }, t + '°'));
    // safety floor: the region no curve or fixed speed can enter
    const fl = [[x0, floor[0][1]], ...floor, [x1, 100]];
    const under = fl.map(([t, f]) => `${this.px(t)},${this.py(Math.max(f, this.fanMin))}`).join(' ');
    g.push(sv('polygon', { class: 'floor', points: `${this.px(x0)},${this.py(0)} ${under} ${this.px(x1)},${this.py(0)}` }));
    g.push(sv('polyline', { class: 'floor-edge', points: under }));
    g.push(sv('text', { class: 'lbl', x: this.px(x0) + 8, y: this.py(9) }, 'safety floor: fans never run below this'));
    if (this.tLimit) {
      const lim = this.tLimit.v;
      g.push(sv('line', { class: 'limit', x1: this.px(lim), x2: this.px(lim), y1: this.py(100), y2: this.py(0) }));
      g.push(sv('text', { class: 'lbl', x: this.px(lim) - 4, y: this.py(0) - 5, 'text-anchor': 'end' }, `${this.tLimit.name} ${lim}°`));
    }
    const path = c => [[x0, c[0][1]], ...c, [x1, 100]].map(([t, f]) => `${this.px(t)},${this.py(f)}`).join(' ');
    if (this.applied && !same(this.applied, this.curve)) g.push(sv('polyline', { class: 'applied', points: path(this.applied) }));
    g.push(sv('polyline', { class: 'line', points: path(this.curve) }));
    if (this.live) {
      const lx = this.px(clamp(this.live.temp, x0, x1));
      g.push(sv('line', { class: 'now', x1: lx, x2: lx, y1: this.py(100), y2: this.py(0) }));
      if (this.live.fan != null) g.push(sv('circle', { class: 'nowdot', cx: lx, cy: this.py(this.live.fan), r: 4 }));
      g.push(sv('text', { class: 'lbl', x: lx + 5, y: this.py(100) + 12 }, `now ${this.live.temp}°`));
    }
    const last = this.curve.length - 1;
    this.curve.forEach(([t, f], i) => {
      g.push(sv('g', { class: 'point' + (this.drag === i ? ' drag' : ''), 'data-i': i, tabindex: this.disabled ? null : 0,
        role: 'slider', 'aria-label': `Curve point ${i + 1}`, 'aria-valuetext': `${t} degrees, ${f} percent` },
        sv('circle', { class: 'pt', cx: this.px(t), cy: this.py(f), r: 5.5 }),
        sv('circle', { class: 'hit' + (i === last ? ' locked' : ''), cx: this.px(t), cy: this.py(f), r: 14 })));
    });
    // Re-rendering replaces the point nodes, so put focus back where it was (or where an
    // insert/drag just moved it) or a keyboard user loses their place once a second.
    const active = document.activeElement;
    const had = active && this.svg.contains(active) && active.dataset.i != null ? Number(active.dataset.i) : null;
    this.svg.replaceChildren(...g);
    const want = this.focusIdx != null ? this.focusIdx : had;
    this.focusIdx = null;
    if (want != null && (had != null || this.drag != null)) {
      const n = this.svg.querySelector(`g.point[data-i="${want}"]`);
      if (n) n.focus({ preventScroll: true });
    }
  }
}

// ── one card ─────────────────────────────────────────────────────────────────────────────────
class GpuCard {
  constructor(hv, stat, slot) {
    this.hv = hv; this.uuid = stat.uuid; this.stat = stat; this.slot = slot;
    this.pending = { power: null, fan: null, clock: null };
    this.t = {};
    const tile = (key, label) => {
      const value = el('div', { class: 'value' }), sub = el('div', { class: 'sub' }), extra = el('div');
      const name = el('div', { class: 'label' }, label);
      this.t[key] = { value, sub, extra, name };
      return el('div', { class: 'tile' }, name, value, extra, sub);
    };
    this.title = el('h2');
    this.subtitle = el('span', { class: 'sub' });
    this.chips = el('span', { class: 'row' });
    this.procs = el('div', { class: 'procs' });
    this.note = el('div', { class: 'sub' });
    this.unsupported = el('div', { class: 'sub unsupported' });
    this.root = el('article', { class: `gpu s${slot}` },
      el('header', {}, el('span', { class: 'key' }), this.title, this.chips, this.subtitle),
      el('div', { class: 'tiles' }, tile('temp', 'Temperature'), tile('fan', 'Fan'), tile('power', 'Power draw'),
        tile('util', 'GPU utilization'), tile('vram', 'VRAM'), tile('clock', 'Core clock'),
        tile('limit', 'Clock limiter'), tile('energy', 'Energy used')),
      this.procs, this.note, this.buildPower(), this.buildFan(), this.buildClock(), this.unsupported, this.buildBaseline());
  }
  d() { const st = this.hv.st(); return st ? st.gpus.find(g => g.uuid === this.uuid) : undefined; }
  lv() { const h = this.hv.h(); return (h && h.live.gpus[this.uuid]) || {}; }
  say(node, kind, text) { node.className = 'msg' + (kind ? ' ' + kind : ''); node.textContent = text || ''; }

  // power ────────────────────────────────────────────────
  buildPower() {
    this.pSlider = el('input', { type: 'range', step: 5, 'aria-label': 'Power limit in watts', oninput: e => this.stagePower(+e.target.value) });
    this.pNumber = el('input', { type: 'number', step: 1, 'aria-label': 'Power limit in watts',
      oninput: e => { if (e.target.value !== '') this.stagePower(+e.target.value, true); },
      onchange: e => { e.target.value = this.pending.power ?? this.d().settings.power_w; } });
    this.pEnds = el('div', { class: 'range-ends' });
    this.pPresets = el('div', { class: 'row' });
    this.pMsg = el('div', { class: 'msg' });
    this.pApply = el('button', { class: 'primary', type: 'button', onclick: () => this.applyPower() }, 'Apply power limit');
    this.pOverride = el('button', { class: 'ghost small', type: 'button', hidden: true,
      onclick: () => this.applyPower(true) }, 'Apply anyway (exceeds budget)');
    this.pRevert = el('button', { class: 'ghost small', type: 'button', onclick: () => { this.pending.power = null; this.syncPower(); } }, 'Revert');
    this.pField = el('fieldset', { class: 'ctl' }, el('legend', {}, 'Power limit'),
      el('div', { class: 'row' }, this.pSlider, this.pNumber, el('span', {}, 'W')), this.pEnds, this.pPresets,
      el('div', { class: 'actions' }, this.pApply, this.pOverride, this.pRevert, this.pMsg));
    return this.pField;
  }
  stagePower(v, typing) {
    const d = this.d(), [lo, hi] = d.power_range;
    if (!Number.isFinite(v)) return;
    this.pKeep = false;             // a new value from you always gets a fresh preview
    const w = clamp(Math.round(v), lo, hi);
    this.pending.power = w === d.settings.power_w ? null : w;
    this.pOverride.hidden = true;   // a changed value needs its own fresh confirmation
    this.syncPower(typing && w !== v);
  }
  syncPower(keepTyped) {
    const d = this.d(), st = this.hv.st(), wall = this.hv.h().wall;
    this.pField.hidden = !d.power_range;
    if (!d.power_range) return;
    const [lo, hi] = d.power_range, applied = d.settings.power_w, val = this.pending.power ?? applied;
    this.pSlider.min = this.pNumber.min = lo; this.pSlider.max = this.pNumber.max = hi;
    this.pSlider.value = val;
    if (!keepTyped && document.activeElement !== this.pNumber) this.pNumber.value = val;
    this.pEnds.replaceChildren(el('span', {}, `${lo} W min`),
      el('span', {}, this.stat.power_default_w ? `factory default ${this.stat.power_default_w} W` : ''), el('span', {}, `${hi} W max`));
    const otherCards = st.gpus.filter(g => g.uuid !== this.uuid);
    const others = otherCards.reduce((a, g) => a + (g.settings.power_w || 0), 0);
    const total = others + val, budget = st.budget_w, over = budget != null && total > budget && val > applied;
    const presets = (d.profile ? d.profile.power_presets : []).filter(p => p.w >= lo && p.w <= hi);
    const pKey = JSON.stringify([presets, val]);
    if (pKey !== this.pKey) {
      this.pKey = pKey;
      this.pPresets.replaceChildren(...presets.map(p => el('button', { class: 'small', type: 'button',
        'aria-pressed': String(p.w === val), title: p.note, onclick: () => this.stagePower(p.w) }, p.label)));
    }
    const staged = this.pending.power != null;
    // Over budget no longer blocks Apply outright — the daemon is the authority and will refuse
    // with over_budget:true, at which point applyPower() reveals the explicit override button.
    this.pApply.disabled = !staged || this.busy;
    this.pRevert.hidden = !staged;
    const note = presets.find(p => p.w === val);
    const othersLead = otherCards.length === 0 ? `${val} W is`
      : otherCards.length === 1 ? `${val} W here plus ${others} W on the other card is`
      : `${val} W here plus ${others} W across the other ${otherCards.length} cards is`;
    const wallText = wall ? `; worst case at the wall about ${n0(wallEstimate(wall, total))} W` : '';
    const budgetText = budget != null ? `Combined caps ${total} of ${budget} W${wallText}.` : `Combined caps on this machine ${total} W${wallText}.`;
    // pKeep guards ALL three branches, not just the idle one: a rejected apply (unlike the old
    // hard-block) leaves pending.power staged, so without this an immediate re-render would
    // clobber the daemon's real error with this recomputed prediction text.
    if (!this.pKeep) {
      if (over) this.say(this.pMsg, 'warn', otherCards.length === 0
        ? `${val} W is ${total - budget} W over this machine’s ${budget} W budget. You can still apply — it will ask you to confirm.`
        : `${othersLead} ${total} W: ${total - budget} W over the ${budget} W budget. You can still apply — it will ask you to confirm.`);
      else if (staged) this.say(this.pMsg, '', `${applied} W → ${val} W. ${budgetText}${note ? ' ' + note.note : ''}`);
      else this.say(this.pMsg, '', note ? note.note : '');
    }
  }
  async applyPower(confirmOverride) {
    const watts = this.pending.power;
    const ok = await this.send({ op: 'set_power', uuid: this.uuid, watts, confirm_override: !!confirmOverride }, this.pMsg, 'power',
      g => `Applied${confirmOverride ? ', over budget as confirmed' : ''}. The driver reports ${g.settings.power_w} W in force.`);
    this.pOverride.hidden = !(!ok && this.lastResp && this.lastResp.over_budget);
  }

  // fans ─────────────────────────────────────────────────
  buildFan() {
    this.fModes = el('div', { class: 'seg', role: 'group', 'aria-label': 'Fan mode' },
      ...[['curve', 'Curve'], ['manual', 'Fixed speed'], ['auto', 'Driver default']].map(([m, label]) =>
        el('button', { type: 'button', 'data-mode': m, onclick: () => this.stageFan({ mode: m }) }, label)));
    this.editor = new CurveEditor(this, curve => this.stageFan({ curve }));
    this.fPresets = el('div', { class: 'row' });
    this.fPoints = el('div', { class: 'points' });
    this.fCurveBox = el('div', {}, this.editor.svg, this.fPoints, this.fPresets,
      el('div', { class: 'sub' }, 'Drag a point, or focus it and use the arrow keys. Double-click the plot to add a point, a point to remove it. The last point stays at 100%.'));
    this.fSlider = el('input', { type: 'range', step: 1, max: 100, 'aria-label': 'Fixed fan speed in percent', oninput: e => this.stageFan({ manual_pct: +e.target.value }) });
    this.fPct = el('b');
    this.fManualBox = el('div', {}, el('div', { class: 'row' }, this.fSlider, this.fPct),
      el('div', { class: 'sub' }, 'A fixed speed still rises with temperature once the safety floor passes it, so it cannot cook the card.'));
    this.fAutoBox = el('div', { class: 'sub' }, 'Hands the fans back to the driver’s own curve. It is safe but usually lazy: on the cards this was built on it ran 7–11 °C hotter under load than a cooling-first curve, which cost 4–8% of clock.');
    this.fState = el('div', { class: 'row' });
    this.fMsg = el('div', { class: 'msg' });
    this.fApply = el('button', { class: 'primary', type: 'button', onclick: () => this.applyFan() }, 'Apply fan settings');
    this.fRevert = el('button', { class: 'ghost small', type: 'button', onclick: () => { this.pending.fan = null; this.syncFan(); } }, 'Revert');
    this.fField = el('fieldset', { class: 'ctl' }, el('legend', {}, 'Fans'), this.fModes, this.fState,
      this.fCurveBox, this.fManualBox, this.fAutoBox, el('div', { class: 'actions' }, this.fApply, this.fRevert, this.fMsg));
    return this.fField;
  }
  fanValue() { return this.pending.fan || this.d().settings.fan; }
  stageFan(patch) {
    if (!this.hv.canEdit()) return;
    this.fKeep = false;
    const next = Object.assign(structuredClone(this.fanValue()), patch);
    this.pending.fan = same(next, this.d().settings.fan) ? null : next;
    this.syncFan();
  }
  syncFan() {
    const d = this.d(), st = this.hv.st();
    this.fField.hidden = !this.stat.nfans;      // a fanless card (passive, or SoC-cooled) has nothing to drive
    if (!this.stat.nfans) return;
    const fan = this.fanValue(), applied = d.settings.fan, staged = !!this.pending.fan;
    const shown = fan.mode === 'unmanaged' ? 'curve' : fan.mode;
    for (const b of this.fModes.children) b.setAttribute('aria-pressed', String(b.dataset.mode === fan.mode));
    this.fCurveBox.hidden = shown !== 'curve'; this.fManualBox.hidden = shown !== 'manual'; this.fAutoBox.hidden = shown !== 'auto';
    this.editor.set(fan.curve, applied.mode === 'curve' ? applied.curve : null, d.fan_min, thermalRef(this.stat.thresholds), !this.hv.canEdit());
    this.fPoints.textContent = fan.curve.map(([t, f]) => `${t}° → ${f}%`).join('   ·   ');
    const fKey = JSON.stringify(fan.curve);
    if (fKey !== this.fKey) {
      this.fKey = fKey;
      // only presets this card's own rule accepts (each must reach 100% by its own full_by_c)
      const fits = Object.values(st.curve_presets).filter(p => p.curve[p.curve.length - 1][0] <= d.full_by_c);
      this.fPresets.replaceChildren(el('span', { class: 'sub' }, 'Presets'), ...fits.map(p =>
        el('button', { class: 'small', type: 'button', title: p.note, 'aria-pressed': String(same(p.curve, fan.curve)),
          onclick: () => this.stageFan({ mode: 'curve', curve: p.curve }) }, p.label)));
    }
    this.fSlider.min = d.fan_min; this.fSlider.value = fan.manual_pct; this.fPct.textContent = fan.manual_pct + ' %';
    this.fApply.disabled = !staged || this.busy; this.fRevert.hidden = !staged;
    // fKeep guards both branches: a failed apply leaves pending.fan staged, so without this an
    // immediate re-render would clobber the daemon's error with "Not applied yet." (same class
    // of bug fixed in syncPower — see its comment).
    if (!this.fKeep) {
      if (staged) this.say(this.fMsg, '', 'Not applied yet.' + (applied.mode === 'curve' && fan.mode === 'curve' ? ' The thin grey line is the curve in force.' : ''));
      else this.say(this.fMsg, '', '');
    }
  }
  async applyFan() {
    const f = this.pending.fan;
    await this.send({ op: 'set_fan', uuid: this.uuid, mode: f.mode, curve: f.curve, manual_pct: f.manual_pct }, this.fMsg, 'fan',
      g => `Applied: ${{ curve: 'curve', manual: 'fixed speed', auto: 'driver default' }[g.settings.fan.mode]}.`);
  }

  // clock cap ────────────────────────────────────────────
  buildClock() {
    this.cOn = el('input', { type: 'checkbox', onchange: e => this.stageClock({ on: e.target.checked }) });
    this.cSlider = el('input', { type: 'range', step: 15, 'aria-label': 'Maximum core clock in MHz', oninput: e => this.stageClock({ mhz: +e.target.value }) });
    this.cVal = el('b');
    this.cMsg = el('div', { class: 'msg' });
    this.cApply = el('button', { class: 'primary', type: 'button', onclick: () => this.applyClock() }, 'Apply clock cap');
    this.cRevert = el('button', { class: 'ghost small', type: 'button', onclick: () => { this.pending.clock = null; this.syncClock(); } }, 'Revert');
    this.cField = el('fieldset', { class: 'ctl' }, el('legend', {}, 'Core clock cap'),
      el('label', { class: 'check' }, this.cOn, 'Cap the maximum core clock'),
      el('div', { class: 'row' }, this.cSlider, this.cVal),
      el('div', { class: 'sub' }, 'A cap can only slow the card, never push it. It is mostly useful for memory-bound decode, where the core clocks high for little gain; for everything else the power limit is the better lever. Off by default.'),
      el('div', { class: 'actions' }, this.cApply, this.cRevert, this.cMsg));
    return this.cField;
  }
  clockValue() {
    const d = this.d(), cap = d.settings.clock_cap_mhz;
    return this.pending.clock || { on: cap != null, mhz: cap != null ? cap : (d.clock_range ? d.clock_range[1] : 0) };
  }
  stageClock(patch) {
    this.cKeep = false;
    const d = this.d(), next = Object.assign({}, this.clockValue(), patch), cap = d.settings.clock_cap_mhz;
    const asApplied = next.on ? next.mhz === cap : cap == null;
    this.pending.clock = asApplied ? null : next;
    this.syncClock();
  }
  syncClock() {
    const d = this.d();
    this.cField.hidden = !d.clock_range;
    if (!d.clock_range) return;
    const c = this.clockValue(), staged = !!this.pending.clock;
    this.cSlider.min = d.clock_range[0]; this.cSlider.max = d.clock_range[1];
    this.cOn.checked = c.on; this.cSlider.value = c.mhz; this.cSlider.disabled = !c.on;
    this.cVal.textContent = c.on ? `${n0(c.mhz)} MHz` : `uncapped (${n0(d.clock_range[1])} MHz)`;
    this.cApply.disabled = !staged || this.busy; this.cRevert.hidden = !staged;
    if (!this.cKeep) {
      if (staged) this.say(this.cMsg, '', 'Not applied yet. The daemon snaps the value down to a clock step the card supports.');
      else this.say(this.cMsg, '', '');
    }
  }
  async applyClock() {
    const c = this.pending.clock;
    await this.send({ op: 'set_clock_cap', uuid: this.uuid, mhz: c.on ? c.mhz : null }, this.cMsg, 'clock',
      g => (g.settings.clock_cap_mhz == null ? 'Applied: cap removed.' : `Applied: capped at ${n0(g.settings.clock_cap_mhz)} MHz.`));
  }

  // baseline ─────────────────────────────────────────────
  buildBaseline() {
    this.bMsg = el('div', { class: 'msg' });
    this.bBtn = el('button', { type: 'button', onclick: async () => {
      if (await this.send({ op: 'baseline', uuid: this.uuid }, this.bMsg, null, () => 'Reset to the lab baseline.')) {
        this.pending = { power: null, fan: null, clock: null };
        this.sync();
      }
    } });
    this.bRow = el('div', { class: 'actions' }, this.bBtn, this.bMsg);
    return this.bRow;
  }

  async send(body, msgNode, group, okText) {
    this.busy = true; this.sync();
    this.say(msgNode, '', 'Applying…');
    const resp = await apply(this.hv.id, body);
    this.busy = false;
    this.lastResp = resp;
    const keep = { power: 'pKeep', fan: 'fKeep', clock: 'cKeep' }[group];
    const h = this.hv.h();
    if (resp.ok) {
      if (h) h.daemon = resp;
      if (group) this.pending[group] = null;
      const g = resp.gpus.find(x => x.uuid === this.uuid);
      this.say(msgNode, 'ok', g ? okText(g) : 'Applied.');
    } else {
      this.say(msgNode, 'err', resp.error || 'Request failed.');
    }
    if (keep) { this[keep] = true; setTimeout(() => { this[keep] = false; }, 8000); }
    render();
    return !!resp.ok;
  }

  sync() {
    if (!this.d()) return;
    const edit = this.hv.canEdit();
    for (const f of [this.pField, this.fField, this.cField]) f.disabled = !edit;
    this.bBtn.disabled = !edit || this.busy;
    this.syncPower(); this.syncFan(); this.syncClock();
    const d = this.d(), missing = [];
    if (!d.power_range) missing.push('power limit');
    if (!this.stat.nfans) missing.push('fan control');
    if (!d.clock_range) missing.push('clock cap');
    this.unsupported.hidden = !missing.length;
    this.unsupported.textContent = missing.length
      ? `Not offered for this GPU: ${missing.join(', ')} — NVML reports ${missing.length > 1 ? 'them' : 'it'} as unavailable` +
        (this.stat.mem_kind === 'unified' ? ' (on a system-on-chip GPU, firmware usually owns power and cooling).' : '.')
      : '';
    this.bRow.hidden = missing.length === 3;
  }

  update() {
    const d = this.d(), lv = this.lv(), th = this.stat.thresholds || {}, t = this.t;
    if (!d) return;
    this.title.textContent = d.profile ? d.profile.label : shortName(this.stat.name) || `GPU ${this.stat.index}`;
    const pci = this.stat.pci ? ` · PCI ${this.stat.pci.replace(/^0+:/, '')}` : '';
    const vbios = this.stat.vbios ? ` · VBIOS ${this.stat.vbios}` : '';
    this.subtitle.textContent = `${this.stat.name}${pci}${vbios}` +
      // An integrated GPU (unified memory, e.g. GB10) has no slot: NVML still reports a "Gen 1 ×1 of
      // ×16" link for it, which would read as a riser fault. Only discrete cards get the PCIe line.
      (lv.pcie_gen && this.stat.mem_kind !== 'unified' ? ` · PCIe Gen ${lv.pcie_gen_max} ×${lv.pcie_width_max ?? lv.pcie_width}` +
        (lv.pcie_width != null && lv.pcie_width_max != null && lv.pcie_width < lv.pcie_width_max
          ? ` (running at ×${lv.pcie_width}: check the slot/riser)` : ` (Gen ${lv.pcie_gen} right now; the link idles down)`) : '') +
      (lv.persistence != null ? ` · persistence ${lv.persistence ? 'on' : 'off'}` : '');
    this.note.textContent = d.profile ? d.profile.note
      : d.power_range ? 'No tuned profile for this card: its power limit can be lowered but not raised above the factory default.' : '';
    this.bBtn.textContent = `Reset to lab baseline (${d.baseline_power_w != null ? d.baseline_power_w + ' W · ' : ''}${this.stat.nfans ? 'lab fan curve · ' : ''}no clock cap)`;

    t.temp.value.replaceChildren(n0(lv.temp), el('small', {}, ' °C'));
    const ref = thermalRef(th);
    t.temp.sub.textContent = ref && lv.temp != null ? `${ref.v - lv.temp}° under ${ref.name} (${ref.v} °C)` : '';
    const fans = (lv.fans || []).filter(v => v != null);
    t.fan.value.replaceChildren(fans.length ? n0(Math.max(...fans)) : '–', el('small', {}, ' %'));
    const mode = { curve: 'curve', manual: 'fixed', auto: 'driver default', unmanaged: 'not managed here' }[d.settings.fan.mode];
    t.fan.sub.textContent = !this.stat.nfans ? 'no controllable fans reported'
      : (fans.length > 1 ? fans.map((v, i) => `fan ${i + 1} ${v}%`).join(' · ') + ' · ' : '') + mode;
    const cap = lv.power_limit_w;
    t.power.value.replaceChildren(n0(lv.power_w), el('small', {}, ' W'));
    this.meter(t.power.extra, lv.power_w, cap);
    t.power.sub.textContent = cap != null ? `${lv.power_w != null ? Math.round(lv.power_w / cap * 100) : '–'}% of the ${cap} W cap` : (lv.power_w != null ? 'no software cap' : '');
    t.util.value.replaceChildren(n0(lv.util), el('small', {}, ' %'));
    t.util.sub.textContent = `memory bus ${n0(lv.mem_util)}%` +
      (lv.encoder_util || lv.decoder_util ? ` · encode ${n0(lv.encoder_util)}% · decode ${n0(lv.decoder_util)}%` : '');
    const unified = (lv.mem_kind || this.stat.mem_kind) === 'unified';
    t.vram.name.textContent = unified ? 'System memory (shared)' : 'VRAM';
    t.vram.value.replaceChildren(gib(lv.vram_used_mib), el('small', {}, ` / ${gib(lv.vram_total_mib)} GiB`));
    this.meter(t.vram.extra, lv.vram_used_mib, lv.vram_total_mib);
    t.vram.sub.textContent = unified ? 'the GPU shares RAM with the CPU; this is the whole machine' : '';
    t.clock.value.replaceChildren(n0(lv.clock_mhz), el('small', {}, ' MHz'));
    t.clock.sub.textContent = `memory ${n0(lv.mem_clock_mhz)} MHz · P${lv.pstate ?? '–'}` + (d.settings.clock_cap_mhz ? ` · capped ${n0(d.settings.clock_cap_mhz)}` : '');
    const r = lv.reasons || [], names = S.st.reasons;
    const bad = r.find(k => ['hw_thermal', 'hw_slowdown', 'hw_power_brake'].includes(k));
    let kind = 'good', text = 'None active', sub = 'The card is running as fast as the load asks.';
    if (bad) { kind = 'critical'; text = names[bad]; sub = 'The hardware is protecting itself. Check cooling and power delivery.'; }
    else if (r.includes('sw_thermal')) { kind = 'serious'; text = names.sw_thermal; sub = 'Clocks are being cut for temperature.'; }
    else if (r.includes('sw_power_cap')) { kind = ''; text = names.sw_power_cap; sub = 'Normal under sustained load: the cap sets the clock.'; }
    else if (r.includes('idle')) { kind = 'good'; text = 'Idle'; sub = 'Nothing is asking for clocks.'; }
    t.limit.value.replaceChildren(el('span', { class: 'chip ' + kind }, text));
    t.limit.sub.textContent = sub;
    t.energy.value.replaceChildren(lv.energy_j != null ? (lv.energy_j / 3.6e6).toLocaleString('en-US', { maximumFractionDigits: 2 }) : '–', el('small', {}, ' kWh'));
    t.energy.sub.textContent = lv.energy_j != null ? 'since the driver loaded' : 'not reported';

    const chips = [];
    if (lv.temp != null && ref && lv.temp >= ref.v - 2) chips.push(el('span', { class: 'chip critical' }, `At the thermal limit (${ref.name} ${ref.v} °C)`));
    else if (this.stat.nfans && lv.temp != null && lv.temp >= d.full_by_c) chips.push(el('span', { class: 'chip serious' }, `Above ${d.full_by_c} °C: fans forced to 100%`));
    if (lv.ecc_enabled && lv.ecc_uncorrected_total) chips.push(el('span', { class: 'chip critical' }, `${n0(lv.ecc_uncorrected_total)} uncorrected ECC error(s)`));
    else if (lv.ecc_enabled && lv.ecc_corrected_total) chips.push(el('span', { class: 'chip warning' }, `${n0(lv.ecc_corrected_total)} corrected ECC error(s)`));
    this.chips.replaceChildren(...chips);
    const procs = lv.procs || [];
    this.procs.textContent = procs.length ? 'On this card: ' + procs.map(p => `${p.name}${p.vram_mib ? ' ' + gib(p.vram_mib) + ' GiB' : ''}`).join(' · ') : 'Nothing resident on this card.';

    const fs = [];
    if (d.fan.fault) fs.push(el('span', { class: 'chip critical wrap' }, 'Fan fault: ' + d.fan.fault));
    if (d.fan.floor_active) fs.push(el('span', { class: 'chip warning' }, 'The safety floor is setting the speed right now'));
    if (d.settings.fan.mode === 'unmanaged') fs.push(el('span', { class: 'chip' }, 'Monitor-only: this page is not driving these fans'));
    else if (d.fan.fan_pct != null) fs.push(el('span', { class: 'chip' }, `Daemon target ${d.fan.fan_pct}% at ${d.fan.temp} °C`));
    this.fState.replaceChildren(...fs);
    this.editor.setLive(lv.temp, fans.length ? Math.max(...fans) : null);
  }
  meter(node, v, max) {
    const bar = el('span');
    bar.style.width = (v != null && max ? clamp(v / max * 100, 0, 100) : 0) + '%';
    node.className = 'meter';
    node.replaceChildren(bar);
  }
}

// ── one machine's budget panel ───────────────────────────────────────────────────────────────
class BudgetPanel {
  constructor(hv) {
    this.hv = hv;
    const q = c => el('span', { class: c });
    this.used = q('b-used'); this.subEl = el('div', { class: 'sub b-sub' });
    this.bar = el('div', { class: 'stack b-bar', role: 'img', 'aria-label': 'Combined power caps against the budget' });
    this.legend = el('div', { class: 'legend b-legend' });
    this.input = el('input', { class: 'b-input', type: 'number', step: 1, min: 1, 'aria-label': 'Combined GPU power budget in watts' });
    this.applyBtn = el('button', { class: 'ghost small b-apply', type: 'button', onclick: () => this.setBudget() }, 'Set budget');
    this.msg = el('span', { class: 'msg b-msg' });
    this.draw = el('dd', { class: 'b-draw' }); this.wall = el('dd', { class: 'b-wall' }); this.circuit = el('dd', { class: 'b-circuit' });
    this.wallRows = [el('div', {}, el('dt', {}, 'Worst case at the wall, at these caps'), this.wall),
      el('div', {}, el('dt', {}, 'Against a wall circuit'), this.circuit)];
    this.note = el('p', { class: 'note' });
    this.root = el('section', { class: 'panel budget' },
      el('h2', {}, 'Power budget'),
      el('div', { class: 'budget-row' },
        el('div', { class: 'budget-main' },
          el('div', { class: 'hero' }, this.used, el('span', { class: 'hero-unit' }, ' W capped')),
          this.subEl, this.bar, this.legend,
          el('div', { class: 'row' }, this.input, el('span', {}, 'W'), this.applyBtn, this.msg)),
        el('dl', { class: 'facts' }, el('div', {}, el('dt', {}, 'Drawing now'), this.draw), ...this.wallRows)),
      this.note);
  }
  async setBudget() {
    const watts = Math.round(Number(this.input.value));
    if (!Number.isFinite(watts) || watts <= 0) {
      this.msg.className = 'msg b-msg err'; this.msg.textContent = 'Enter a positive number of watts.';
      return;
    }
    this.msg.className = 'msg b-msg'; this.msg.textContent = 'Applying…';
    const resp = await apply(this.hv.id, { op: 'set_budget', watts });
    if (resp.ok) {
      const h = this.hv.h();
      if (h) h.daemon = resp;
      this.msg.className = 'msg b-msg ok'; this.msg.textContent = `Budget set to ${n0(watts)} W.`;
      render();
    } else {
      this.msg.className = 'msg b-msg err'; this.msg.textContent = resp.error || 'Request failed.';
    }
  }
  render() {
    const h = this.hv.h(), st = this.hv.st();
    this.root.hidden = !st || !powerSettable(st);
    if (this.root.hidden) return;
    const used = st.budget_used_w, budget = st.budget_w, cards = this.hv.cards;
    this.used.textContent = n0(used);
    const left = budget == null ? null : budget - used;
    this.subEl.textContent = st.preview ? 'monitor-only: the control daemon keeps this machine’s budget once installed'
      : budget == null ? 'no combined budget is set on this machine'
      : `of a ${n0(budget)} W combined GPU budget` + (left > 0 ? ` · ${n0(left)} W unallocated` : left === 0 ? ' · fully allocated' : ` · ${n0(-left)} W over`);
    if (document.activeElement !== this.input) this.input.value = budget ?? '';
    this.input.disabled = this.applyBtn.disabled = !this.hv.canEdit();
    const scale = Math.max(budget || 0, used) || 1;
    this.bar.replaceChildren(...cards.map(c => {
      const seg = el('span', { class: 's' + c.slot });
      seg.style.width = (((c.d() && c.d().settings.power_w) || 0) / scale * 100) + '%';
      return seg;
    }));
    this.legend.replaceChildren(...cards.filter(c => c.d()).map(c => el('span', {}, el('span', { class: `key s${c.slot}` }),
      `${c.title.textContent || shortName(c.stat.name)} ${n0(c.d().settings.power_w)} W`)));
    const draw = cards.reduce((a, c) => a + (c.lv().power_w || 0), 0);
    this.draw.replaceChildren(`${n0(draw)} W`, el('span', { class: 'sub' }, cards.length === 1 ? 'measured' : `all ${cards.length} cards, measured`));
    const wall = h.wall, w = wallEstimate(wall, used);
    for (const r of this.wallRows) r.hidden = !wall;
    if (wall) {
      this.wall.replaceChildren(`about ${n0(w)} W`, el('span', { class: 'sub' }, 'estimate, everything flat out at once'));
      const c = wall.circuits || {}, names = Object.keys(c), fits = names.find(k => w <= c[k]);
      this.circuit.replaceChildren(names.length ? (fits ? `Within a ${fits} circuit’s continuous rating` : 'Over every circuit rating listed') : 'no circuits listed',
        el('span', { class: 'sub' }, names.map(k => `${k}: ${n0(c[k])} W continuous`).join(' · ')));
    }
    this.note.textContent = wall
      ? `The wall figure is an estimate from this machine’s “wall” setting in hosts.json: ${n0(wall.non_gpu_dc_w)} W for everything that isn’t a GPU, on top of the GPU caps, at ${Math.round(wall.psu_efficiency * 100)}% supply efficiency. Each card’s own maximum is not this machine’s safe maximum, which is why its cards share one budget.`
      : 'Each card’s own maximum is not this machine’s safe maximum, which is why its cards share one budget. Add a “wall” setting for this machine in hosts.json to see a wall-power estimate here.';
  }
}

// ── one machine ──────────────────────────────────────────────────────────────────────────────
class HostView {
  constructor(id) {
    this.id = id; this.cards = []; this.cardKey = null;
    this.banner = el('div', { class: 'banner', hidden: true });
    this.budget = new BudgetPanel(this);
    this.grid = el('section', { class: 'gpu-grid', 'aria-label': 'Cards' });
    this.empty = el('div', { class: 'panel empty' });
    this.root = el('div', { class: 'hostview', id: `view-${id}`, role: 'tabpanel', 'data-host': id },
      this.banner, this.budget.root, this.empty, this.grid);
  }
  h() { return hostOf(this.id); }
  st() { const h = this.h(); return h ? h.daemon : null; }
  canEdit() { const h = this.h(); return !!h && h.conn === 'up' && !!h.daemon && !h.daemon.preview; }
  render() {
    const h = this.h();
    if (!h) return;
    const key = JSON.stringify(h.static.map(g => g.uuid));
    if (h.daemon && key !== this.cardKey) {
      // Built once per set of GPUs and then only updated, so staged edits survive re-renders
      // and host switches; rebuilt only if that machine's GPUs actually change.
      this.cards = h.static.map((g, i) => new GpuCard(this, g, (i % 4) + 1));   // cycle: only 4 colors exist
      this.grid.replaceChildren(...this.cards.map(c => c.root));
      this.cardKey = key;
    }
    for (const c of this.cards) c.stat = h.static.find(g => g.uuid === c.uuid) || c.stat;
    this.root.classList.toggle('offline', h.conn !== 'up');
    this.empty.hidden = this.cards.length > 0;
    this.empty.textContent = h.conn === 'up' || h.conn === 'connecting'
      ? (h.static.length || !h.daemon ? `Waiting for ${h.label}…` : `${h.label} reports no NVIDIA GPUs.`)
      : `Nothing to show for ${h.label} yet.`;
    this.renderBanner(h);
    for (const c of this.cards) { c.update(); c.sync(); }
    this.budget.render();
  }
  renderBanner(h) {
    const parts = [], st = h.daemon;
    let critical = false;
    const install = h.remote ? `./install.sh --no-ui     (in a gpu-tuner checkout on ${h.label}; it asks for sudo there)`
      : './install.sh     (in the gpu-tuner checkout on this machine)';
    if (h.conn === 'down' || h.conn === 'incompatible') {
      critical = true;
      const seen = h.last_seen ? ` Last reading ${hhmm(h.last_seen, true)}.` : '';
      const retry = h.retry_in_s != null ? ` Retrying in ${h.retry_in_s} s.` : '';
      parts.push(el('div', {}, el('strong', {}, h.conn === 'incompatible' ? `${h.label} runs an incompatible gpu-tuner` : `${h.label} is unreachable`),
        `${h.error || 'The connection dropped.'}${seen}${retry} Its controls are locked until it answers again; whatever was last applied there stays in force.`));
    } else if (h.conn === 'stale') {
      parts.push(el('div', {}, el('strong', {}, `No reading from ${h.label} for ${n0(h.live.age_s)} s`), 'The link is up but quiet; values below may be out of date.'));
    }
    // Nothing NVML would let a daemon set on any of this machine's GPUs (a GB10, say): no daemon needed.
    const nothingSettable = st && st.gpus.length && !powerSettable(st) && !st.gpus.some(g => g.clock_range)
      && !h.static.some(g => g.nfans);
    if (st && h.daemon_error === 'not installed' && nothingSettable) {
      parts.push(el('div', {}, el('strong', {}, `Monitor-only, and that is all ${h.label} needs.`),
        'NVML offers no power limit, fan control or clock cap on its GPUs, so there is nothing for the control daemon to do there.'));
    } else if (st && h.daemon_error === 'not installed') {
      parts.push(el('div', {}, el('strong', {}, `Monitor-only: the control daemon is not installed on ${h.label}.`),
        'Every reading and chart is live and each card’s safe ranges are shown, but the controls are locked. To unlock them, run:',
        el('br'), el('code', {}, install)));
    } else if (st && h.daemon_error) {
      critical = true;
      parts.push(el('div', {}, el('strong', {}, `The control daemon on ${h.label} is not answering.`),
        `${h.daemon_error}. If it stopped, its fans are back on the driver’s own curve and its power caps are still in place. Check there: `,
        el('code', {}, 'systemctl status gpu-tunerd')));
    }
    if (st && st.dry_run) parts.push(el('div', {}, el('strong', {}, 'Dry-run daemon.'), 'Changes are validated and logged exactly as they would be, but nothing is written to the cards.'));
    for (const w of (st && st.warnings) || []) parts.push(el('div', {}, el('strong', {}, 'Daemon warning'), w));
    for (const n of h.notes || []) parts.push(el('div', {}, el('strong', {}, 'Connection note'), n));
    if (h.node && h.node.skew_s != null && Math.abs(h.node.skew_s) > 2)
      parts.push(el('div', {}, el('strong', {}, `${h.label}’s clock is ${n1(Math.abs(h.node.skew_s))} s ${h.node.skew_s > 0 ? 'ahead' : 'behind'}`),
        'Charts use this machine’s clock, so they are unaffected; check NTP there if it matters elsewhere.'));
    this.banner.hidden = !parts.length; this.banner.className = 'banner' + (critical ? ' critical' : '');
    this.banner.replaceChildren(...parts);
  }
}

// ── fleet strip + host tabs ──────────────────────────────────────────────────────────────────
function linkChip(h) {
  const st = h.daemon;
  if (h.conn === 'up') {
    if (!st) return ['', 'Connecting…'];
    if (st.preview) return ['warning', 'Live · monitor-only'];
    return st.dry_run ? ['good', 'Live · dry-run'] : ['good', 'Live · controls on'];
  }
  if (h.conn === 'stale') return ['warning', `Stale · ${n0(h.live.age_s)} s`];
  if (h.conn === 'connecting') return ['', 'Connecting…'];
  if (h.conn === 'incompatible') return ['critical', 'Version mismatch'];
  return ['critical', h.retry_in_s != null ? `Unreachable · retry ${h.retry_in_s} s` : 'Unreachable'];
}

function renderFleet() {
  const box = $('#fleet'), hosts = S.st.hosts;
  box.hidden = !multi();
  if (box.hidden) return;
  // Link sits next to the machine so a phone shows who is up without scrolling the table.
  const head = el('thead', {}, el('tr', {}, ...['Machine', 'Link', 'GPU', 'Temp', 'Power', 'Util', 'Clock', 'Fan']
    .map(t => el('th', { scope: 'col' }, t))));
  const rows = [];
  for (const h of hosts) {
    const [kind, text] = linkChip(h), off = h.conn !== 'up';
    const pick = el('button', { class: 'linkish', type: 'button', 'aria-current': String(h.id === S.sel),
      onclick: () => select(h.id, true) }, h.label);
    const chip = el('span', { class: 'chip ' + kind }, text);
    if (!h.static.length) {
      rows.push(el('tr', { class: off ? 'off' : '' }, el('td', {}, pick), el('td', {}, chip),
        el('td', { colspan: 6, class: 'sub' }, h.error || (h.conn === 'up' ? 'no NVIDIA GPUs reported' : 'no data yet'))));
      continue;
    }
    h.static.forEach((g, i) => {
      const lv = h.live.gpus[g.uuid] || {}, fans = (lv.fans || []).filter(v => v != null);
      const prof = h.daemon && h.daemon.gpus.find(x => x.uuid === g.uuid);
      const name = (prof && prof.profile && prof.profile.label) || shortName(g.name);
      rows.push(el('tr', { class: (off ? 'off ' : '') + (i ? 'cont' : '') },
        el('td', {}, i ? el('span', { class: 'sr' }, h.label) : pick),
        el('td', {}, i ? '' : chip),
        el('td', {}, name),
        el('td', {}, lv.temp != null ? `${n0(lv.temp)} °C` : '–'),
        el('td', {}, lv.power_limit_w != null ? `${n0(lv.power_w)} / ${n0(lv.power_limit_w)} W` : `${n0(lv.power_w)} W`),
        el('td', {}, lv.util != null ? `${n0(lv.util)} %` : '–'),
        el('td', {}, lv.clock_mhz != null ? `${n0(lv.clock_mhz)} MHz` : '–'),
        el('td', {}, fans.length ? `${n0(Math.max(...fans))} %` : '–')));
    });
  }
  $('#fleet-table').replaceChildren(head, el('tbody', {}, ...rows));
}

function renderTabs() {
  const nav = $('#tabs');
  nav.hidden = !multi();
  if (nav.hidden) return;
  const key = JSON.stringify([S.sel, S.st.hosts.map(h => [h.id, h.label, linkChip(h)[0]])]);
  if (key === S.tabKey) return;
  S.tabKey = key;
  nav.replaceChildren(...S.st.hosts.map(h => el('button', { type: 'button', role: 'tab', class: 'tab',
    id: `tab-${h.id}`, 'aria-controls': `view-${h.id}`, 'aria-selected': String(h.id === S.sel),
    tabindex: h.id === S.sel ? 0 : -1, onclick: () => select(h.id, true),
    onkeydown: e => {
      const ids = S.st.hosts.map(x => x.id), i = ids.indexOf(h.id);
      const j = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: ids.length - 1 }[e.key];
      if (j == null) return;
      e.preventDefault();
      select(ids[(j + ids.length) % ids.length], true);
      const b = document.getElementById(`tab-${S.sel}`); if (b) b.focus();
    } }, el('span', { class: 'dot ' + linkChip(h)[0], title: linkChip(h)[1], 'aria-label': linkChip(h)[1] }), h.label)));
}

function select(id, user) {
  if (!hostOf(id) || id === S.sel) return;
  S.sel = id;
  if (user) {
    try { history.replaceState(null, '', '#host=' + encodeURIComponent(id)); } catch (e) { /* ignore */ }
  }
  S.hist = null; S.hoverT = null;
  for (const c of S.charts) c.root.classList.add('loading');
  render();
  pollHistory(true);
}

function pickInitialHost() {
  const m = /(?:^|[#&])host=([^&]+)/.exec(location.hash);
  const want = m ? decodeURIComponent(m[1]) : null;
  S.sel = hostOf(want) ? want : S.st.hosts[0].id;
}

// ── charts ───────────────────────────────────────────────────────────────────────────────────
const CHARTS = [
  { key: 'temp', title: 'Temperature', unit: '°C', domain: [20, 100], tlimit: true },
  { key: 'fan', title: 'Fan speed', unit: '%', domain: [0, 100], hint: 'fastest fan on each card' },
  { key: 'power', title: 'Power draw', unit: 'W', cap: true },
  { key: 'clock', title: 'Core clock', unit: 'MHz' },
  { key: 'util', title: 'GPU utilization', unit: '%', domain: [0, 100] },
];
const TICK_S = { 300: 60, 900: 180, 3600: 600, 21600: 3600 };
const cardName = c => (c.d() && c.d().profile ? c.d().profile.label : shortName(c.stat.name) || 'GPU ' + c.stat.index);
const series = (c, k) => (S.hist && S.hist.gpus[c.uuid] ? S.hist.gpus[c.uuid][k] : []);

function niceMax(v) {
  if (!(v > 0)) return 1;
  const raw = v / 4, mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => s >= raw);
  return step * Math.ceil(v / step);
}

class Chart {
  constructor(def) {
    this.def = def; this.H = 190; this.M = { l: 44, r: 14, t: 10, b: 22 };
    this.legend = el('span', { class: 'legend' });
    this.svg = sv('svg', { tabindex: 0, role: 'img', 'aria-label': `${def.title} history; use the left and right arrow keys to read values` });
    this.tip = el('div', { class: 'tip', hidden: true });
    this.root = el('figure', { class: 'chart' },
      el('figcaption', {}, el('span', { class: 'title' }, def.title + ' ', el('small', {}, def.unit + (def.hint ? ' · ' + def.hint : ''))), this.legend),
      this.svg, this.tip);
    this.svg.addEventListener('pointermove', e => { this.own = true; setHover(this.tAt(e)); });
    this.svg.addEventListener('pointerleave', () => { this.own = false; setHover(null); });
    this.svg.addEventListener('blur', () => { this.own = false; setHover(null); });
    this.svg.addEventListener('keydown', e => {
      const h = S.hist; if (!h || !h.t.length || !['ArrowLeft', 'ArrowRight'].includes(e.key)) return;
      e.preventDefault(); this.own = true;
      const i = S.hoverT == null ? h.t.length - 1 : clamp(nearestIdx(h.t, S.hoverT) + (e.key === 'ArrowLeft' ? -1 : 1), 0, h.t.length - 1);
      setHover(h.t[i]);
    });
    new ResizeObserver(() => this.draw()).observe(this.root);
  }
  tAt(e) { const r = this.svg.getBoundingClientRect(); return this.t0 + (e.clientX - r.left - this.M.l) / (this.W - this.M.l - this.M.r) * (this.t1 - this.t0); }
  px(t) { return this.M.l + (t - this.t0) / (this.t1 - this.t0) * (this.W - this.M.l - this.M.r); }
  py(v) { return this.M.t + (1 - (v - this.lo) / (this.hi - this.lo)) * (this.H - this.M.t - this.M.b); }
  path(ts, vals) {
    let d = '', pen = false;
    vals.forEach((v, i) => { if (v == null) { pen = false; return; } d += `${pen ? 'L' : 'M'}${this.px(ts[i]).toFixed(1)},${this.py(v).toFixed(1)}`; pen = true; });
    return d;
  }
  draw() {
    // A machine with no controllable fans (a GB10, a passive card) gets no empty fan chart. Decided
    // before the width check: a hidden chart has no width, and must still be able to come back.
    const cards = selCards();
    this.root.hidden = this.def.key === 'fan' && cards.length > 0 && !cards.some(c => c.stat.nfans);
    const h = S.hist; this.W = this.svg.clientWidth;
    if (!h || !this.W) return;
    const def = this.def, k = def.key, g = [];
    this.t1 = h.t.length ? h.t[h.t.length - 1] : Date.now() / 1000; this.t0 = this.t1 - h.window;
    const all = cards.flatMap(c => [...series(c, k), ...(def.cap ? series(c, 'cap') : [])]).filter(v => v != null);
    [this.lo, this.hi] = def.domain || [0, niceMax(Math.max(1, ...all))];
    this.svg.setAttribute('viewBox', `0 0 ${this.W} ${this.H}`); this.svg.setAttribute('height', this.H);
    for (let i = 0; i <= 4; i++) {
      const v = this.lo + (this.hi - this.lo) * i / 4, y = this.py(v);
      g.push(sv('line', { class: i ? 'gridline' : 'axisline', x1: this.M.l, x2: this.W - this.M.r, y1: y, y2: y }));
      g.push(sv('text', { x: this.M.l - 6, y: y + 4, 'text-anchor': 'end' }, n0(v)));
    }
    const step = TICK_S[h.window] || 60;
    for (let t = Math.ceil(this.t0 / step) * step; t <= this.t1; t += step) {
      if (this.px(t) < this.M.l + 16 || this.px(t) > this.W - this.M.r - 16) continue;
      g.push(sv('text', { x: this.px(t), y: this.H - 5, 'text-anchor': 'middle' }, hhmm(t)));
    }
    if (def.tlimit) {
      const refs = cards.map(c => thermalRef(c.stat.thresholds)).filter(Boolean);
      const ref = refs.reduce((a, b) => (!a || b.v < a.v ? b : a), null);
      if (ref) {
        g.push(sv('line', { class: 'ref', x1: this.M.l, x2: this.W - this.M.r, y1: this.py(ref.v), y2: this.py(ref.v) }));
        g.push(sv('text', { x: this.W - this.M.r, y: this.py(ref.v) - 4, 'text-anchor': 'end' }, `${ref.name} ${ref.v} °C`));
      }
    }
    for (const c of cards) {
      const s = series(c, k);
      if (def.cap) g.push(sv('path', { class: `capline s${c.slot}`, d: this.path(h.t, series(c, 'cap')) }));
      g.push(sv('path', { class: `line s${c.slot}`, d: this.path(h.t, s) }));
      let j = s.length - 1; while (j >= 0 && s[j] == null) j--;
      if (j >= 0) g.push(sv('circle', { class: `dot s${c.slot}`, cx: this.px(h.t[j]), cy: this.py(s[j]), r: 4 }));
    }
    this.hover = sv('g');
    g.push(this.hover);
    this.svg.replaceChildren(...g);
    this.root.classList.remove('loading');
    this.drawHover();
  }
  drawHover() {
    const h = S.hist; if (!h || !this.hover) return;
    const k = this.def.key, n = h.t.length, cards = selCards();
    const i = S.hoverT != null && n ? nearestIdx(h.t, S.hoverT) : n - 1;
    const marks = [];
    if (S.hoverT != null && n) {
      const x = this.px(h.t[i]);
      marks.push(sv('line', { class: 'cross', x1: x, x2: x, y1: this.M.t, y2: this.H - this.M.b }));
      for (const c of cards) { const v = series(c, k)[i]; if (v != null) marks.push(sv('circle', { class: `dot s${c.slot}`, cx: x, cy: this.py(v), r: 4 })); }
    }
    this.hover.replaceChildren(...marks);
    // the legend doubles as the readout: latest values, or the values under the crosshair
    const keys = cards.map(c => el('span', {}, el('span', { class: `linekey s${c.slot}` }), cardName(c) + ' ',
      el('b', {}, n ? n1(series(c, k)[i]) : '–')));
    if (this.def.cap) keys.push(el('span', {}, el('span', { class: 'linekey cap s1' }), 'dashed: power cap'));
    this.legend.replaceChildren(...keys);
    const show = this.own && S.hoverT != null && n;
    this.tip.hidden = !show;
    if (show) {
      this.tip.replaceChildren(el('div', { class: 'when' }, hhmm(h.t[i], true)), ...cards.map(c => el('div', { class: 'r' },
        el('span', { class: `linekey s${c.slot}` }), el('b', {}, `${n1(series(c, k)[i])} ${this.def.unit}`), el('span', { class: 'n' }, cardName(c)),
        this.def.cap ? el('span', { class: 'n' }, ` · cap ${n0(series(c, 'cap')[i])} W`) : null)));
      const x = this.px(h.t[i]), flip = x > this.W * 0.6;
      this.tip.style.left = flip ? '' : (x + 24) + 'px';
      this.tip.style.right = flip ? (this.W - x + 24) + 'px' : '';
    }
  }
}
function nearestIdx(ts, t) {
  let lo = 0, hi = ts.length - 1;
  while (hi - lo > 1) { const mid = (lo + hi) >> 1; if (ts[mid] < t) lo = mid; else hi = mid; }
  return Math.abs(ts[lo] - t) <= Math.abs(ts[hi] - t) ? lo : hi;
}
function setHover(t) { S.hoverT = t; for (const c of S.charts) c.drawHover(); }

function renderTable() {
  const box = $('#table'), h = S.hist, cards = selCards();
  if (!S.tableOpen || !h) return;
  const cols = CHARTS.map(c => `${c.title} (${c.unit})`);
  const head = el('thead', {},
    el('tr', {}, el('th', {}, ''), ...cards.map(c => el('th', { colspan: cols.length }, el('span', { class: `key s${c.slot}` }), cardName(c)))),
    el('tr', {}, el('th', {}, 'Time'), ...cards.flatMap(() => cols.map(t => el('th', {}, t)))));
  const rows = [];
  for (let i = h.t.length - 1; i >= Math.max(0, h.t.length - 60); i--) {
    rows.push(el('tr', {}, el('td', {}, hhmm(h.t[i], true)), ...cards.flatMap(c => CHARTS.map(ch => el('td', {}, n1(series(c, ch.key)[i]))))));
  }
  box.replaceChildren(rows.length ? el('table', {}, head, el('tbody', {}, ...rows)) : el('div', { class: 'empty' }, 'No samples yet.'));
}

// ── page ─────────────────────────────────────────────────────────────────────────────────────
function buildOnce() {
  S.charts = CHARTS.map(d => new Chart(d));
  $('#charts').replaceChildren(...S.charts.map(c => c.root));
  const label = s => (s < 3600 ? `${s / 60} min` : `${s / 3600} h`);
  $('#windows').replaceChildren(...S.st.windows.map(w => el('button', { type: 'button', 'data-w': w, 'aria-pressed': String(w === S.win),
    onclick: () => { S.win = w; for (const b of $('#windows').children) b.setAttribute('aria-pressed', String(+b.dataset.w === w));
      for (const c of S.charts) c.root.classList.add('loading'); pollHistory(true); } }, label(w))));
  $('#table-toggle').addEventListener('click', e => {
    S.tableOpen = !S.tableOpen; $('#table').hidden = !S.tableOpen;
    e.target.textContent = S.tableOpen ? 'Hide table' : 'Show as table'; e.target.setAttribute('aria-expanded', String(S.tableOpen));
    renderTable();
  });
  addEventListener('hashchange', () => {
    const m = /(?:^|[#&])host=([^&]+)/.exec(location.hash);
    if (m) select(decodeURIComponent(m[1]), false);
  });
}

function render() {
  if (!S.st) return;
  if (!S.charts.length) { pickInitialHost(); buildOnce(); }
  if (!hostOf(S.sel)) S.sel = S.st.hosts[0].id;
  const box = $('#hosts');
  for (const h of S.st.hosts) {
    if (!S.views.has(h.id)) { const v = new HostView(h.id); S.views.set(h.id, v); box.append(v.root); }
  }
  for (const [id, v] of S.views) v.root.hidden = id !== S.sel;
  const h = hostOf(S.sel), node = h.node || {};
  $('#meta').textContent = (multi() ? `${h.label} · ` : '') + `driver ${node.driver || '–'} · gpu-tuner ${S.st.version}`;
  $('#hist-h').textContent = multi() ? `History · ${h.label}` : 'History';
  const g = $('#banner'), warns = S.st.warnings || [];
  g.hidden = !warns.length;
  g.replaceChildren(...warns.map(w => el('div', {}, el('strong', {}, 'Configuration'), w)));
  renderFleet();
  renderTabs();
  selView().render();
}

function setConn() {
  const c = $('#conn');
  let kind, text;
  if (!S.st) { kind = 'critical'; text = S.signedOut ? 'Signed out: run “gpu-tuner open”' : 'Server not responding'; }
  else if (!multi()) [kind, text] = linkChip(S.st.hosts[0]);
  else {
    const up = S.st.hosts.filter(h => h.conn === 'up').length, n = S.st.hosts.length;
    kind = up === n ? 'good' : up ? 'warning' : 'critical';
    text = `Live · ${up} of ${n} machines`;
  }
  c.className = 'chip ' + kind; c.textContent = text;
}

async function pollState() {
  try {
    S.st = await getJSON('/api/state');
    render();
    setConn();
  } catch (e) {
    const c = $('#conn');
    c.className = 'chip critical';
    c.textContent = S.signedOut ? 'Signed out: run “gpu-tuner open”' : 'Server not responding';
  }
  setTimeout(pollState, 1000);
}
let histTimer = null;
async function pollHistory() {
  clearTimeout(histTimer);
  const host = S.sel, win = S.win;
  if (host != null) {
    try {
      const h = await getJSON(`/api/history?host=${encodeURIComponent(host)}&window=${win}`);
      if (h.window === S.win && h.host === S.sel && S.charts.length) {
        S.hist = h;
        $('#bucket').textContent = h.bucket_s > 1 ? `each point is a ${h.bucket_s} s average` : 'one point per second';
        for (const c of S.charts) c.draw();
        renderTable();
      }
    } catch (e) { /* the connection chip already says so */ }
  }
  histTimer = setTimeout(pollHistory, !S.hist ? 500 : S.win <= 900 ? 2000 : 10000);
}
pollState();
pollHistory();
