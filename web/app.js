'use strict';
/* gpu-tuner page. No framework, no CDN, no inline script or style (the server's CSP forbids
   both). Every limit shown here is a COPY for display: gpu-tunerd re-validates each request,
   so nothing in this file can widen what the cards will accept.
   Dynamic text is only ever inserted as text nodes, never as HTML. */

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

const S = { st: null, hist: null, win: 300, hoverT: null, cards: [], charts: [], tableOpen: false, signedOut: false };

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
async function apply(body) {
  try {
    const r = await fetch('/api/apply', { method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    return await r.json();
  } catch (e) {
    return { ok: false, error: 'could not reach the gpu-tuner server: ' + e.message };
  }
}
const wallEstimate = caps => {
  const m = S.st.daemon.wall_model;
  return Math.round((m.non_gpu_dc_w + caps) / m.psu_efficiency);
};
const canEdit = () => !!S.st && !S.st.daemon.preview;

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
  // per card); tMin/maxPts are machine-wide constants.
  env() { const d = this.card.d(), st = S.st.daemon; return { floor: d.floor, fullBy: d.full_by_c, tMin: st.curve_temp_min_c, maxPts: st.curve_max_points }; }
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
    if (!this.W || !S.st) return;
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
      g.push(sv('line', { class: 'limit', x1: this.px(this.tLimit), x2: this.px(this.tLimit), y1: this.py(100), y2: this.py(0) }));
      g.push(sv('text', { class: 'lbl', x: this.px(this.tLimit) - 4, y: this.py(0) - 5, 'text-anchor': 'end' }, `T.Limit ${this.tLimit}°`));
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
  constructor(stat, slot) {
    this.uuid = stat.uuid; this.stat = stat; this.slot = slot;
    this.pending = { power: null, fan: null, clock: null };
    this.t = {};
    const tile = (key, label) => {
      const value = el('div', { class: 'value' }), sub = el('div', { class: 'sub' }), extra = el('div');
      this.t[key] = { value, sub, extra };
      return el('div', { class: 'tile' }, el('div', { class: 'label' }, label), value, extra, sub);
    };
    this.title = el('h2');
    this.subtitle = el('span', { class: 'sub' });
    this.chips = el('span', { class: 'row' });
    this.procs = el('div', { class: 'procs' });
    this.note = el('div', { class: 'sub' });
    this.root = el('article', { class: `gpu s${slot}` },
      el('header', {}, el('span', { class: 'key' }), this.title, this.chips, this.subtitle),
      el('div', { class: 'tiles' }, tile('temp', 'Temperature'), tile('fan', 'Fan'), tile('power', 'Power draw'),
        tile('util', 'GPU utilization'), tile('vram', 'VRAM'), tile('clock', 'Core clock'),
        tile('limit', 'Clock limiter'), tile('energy', 'Energy used')),
      this.procs, this.note, this.buildPower(), this.buildFan(), this.buildClock(), this.buildBaseline());
  }
  d() { return S.st.daemon.gpus.find(g => g.uuid === this.uuid); }
  lv() { return (S.st.live.gpus || {})[this.uuid] || {}; }
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
    const w = clamp(Math.round(v), lo, hi);
    this.pending.power = w === d.settings.power_w ? null : w;
    this.pOverride.hidden = true;   // a changed value needs its own fresh confirmation
    this.syncPower(typing && w !== v);
  }
  syncPower(keepTyped) {
    const d = this.d(), st = S.st.daemon;
    if (!d.power_range) { this.pField.disabled = true; this.say(this.pMsg, '', 'This card reports no settable power range.'); return; }
    const [lo, hi] = d.power_range, applied = d.settings.power_w, val = this.pending.power ?? applied;
    this.pSlider.min = this.pNumber.min = lo; this.pSlider.max = this.pNumber.max = hi;
    this.pSlider.value = val;
    if (!keepTyped && document.activeElement !== this.pNumber) this.pNumber.value = val;
    this.pEnds.replaceChildren(el('span', {}, `${lo} W min`), el('span', {}, `factory default ${this.stat.power_default_w} W`), el('span', {}, `${hi} W max`));
    const otherCards = st.gpus.filter(g => g.uuid !== this.uuid);
    const others = otherCards.reduce((a, g) => a + (g.settings.power_w || 0), 0);
    const total = others + val, over = total > st.budget_w && val > applied;
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
    // pKeep guards ALL three branches, not just the idle one: a rejected apply (unlike the old
    // hard-block) leaves pending.power staged, so without this an immediate re-render would
    // clobber the daemon's real error with this recomputed prediction text.
    if (!this.pKeep) {
      if (over) this.say(this.pMsg, 'warn', `${othersLead} ${total} W: ${total - st.budget_w} W over the ${st.budget_w} W budget. You can still apply — it will ask you to confirm.`);
      else if (staged) this.say(this.pMsg, '', `${applied} W → ${val} W. Combined caps ${total} of ${st.budget_w} W; worst case at the wall about ${n0(wallEstimate(total))} W.${note ? ' ' + note.note : ''}`);
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
    this.fAutoBox = el('div', { class: 'sub' }, 'Hands the fans back to the driver’s own curve. It is safe but lazy: measured here at 78–89 °C under load, against 71–78 °C on the lab curve, which cost 4–8% of clock.');
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
    if (!canEdit()) return;
    const next = Object.assign(structuredClone(this.fanValue()), patch);
    this.pending.fan = same(next, this.d().settings.fan) ? null : next;
    this.syncFan();
  }
  syncFan() {
    const d = this.d(), st = S.st.daemon, fan = this.fanValue(), applied = d.settings.fan, staged = !!this.pending.fan;
    const shown = fan.mode === 'unmanaged' ? 'curve' : fan.mode;
    for (const b of this.fModes.children) b.setAttribute('aria-pressed', String(b.dataset.mode === fan.mode));
    this.fCurveBox.hidden = shown !== 'curve'; this.fManualBox.hidden = shown !== 'manual'; this.fAutoBox.hidden = shown !== 'auto';
    this.editor.set(fan.curve, applied.mode === 'curve' ? applied.curve : null, d.fan_min, (this.stat.thresholds || {}).t_limit, !canEdit());
    this.fPoints.textContent = fan.curve.map(([t, f]) => `${t}° → ${f}%`).join('   ·   ');
    const fKey = JSON.stringify(fan.curve);
    if (fKey !== this.fKey) {
      this.fKey = fKey;
      this.fPresets.replaceChildren(el('span', { class: 'sub' }, 'Presets'), ...Object.values(st.curve_presets).map(p =>
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
    const d = this.d(), next = Object.assign({}, this.clockValue(), patch), cap = d.settings.clock_cap_mhz;
    const asApplied = next.on ? next.mhz === cap : cap == null;
    this.pending.clock = asApplied ? null : next;
    this.syncClock();
  }
  syncClock() {
    const d = this.d();
    if (!d.clock_range) { this.cField.disabled = true; return; }
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
    return el('div', { class: 'actions' }, this.bBtn, this.bMsg);
  }

  async send(body, msgNode, group, okText) {
    this.busy = true; this.sync();
    this.say(msgNode, '', 'Applying…');
    const resp = await apply(body);
    this.busy = false;
    this.lastResp = resp;
    const keep = { power: 'pKeep', fan: 'fKeep', clock: 'cKeep' }[group];
    if (resp.ok) {
      S.st.daemon = resp;
      if (group) this.pending[group] = null;
      this.say(msgNode, 'ok', okText(resp.gpus.find(g => g.uuid === this.uuid)));
    } else {
      this.say(msgNode, 'err', resp.error || 'Request failed.');
    }
    if (keep) { this[keep] = true; setTimeout(() => { this[keep] = false; }, 8000); }
    render();
    return !!resp.ok;
  }

  sync() {
    const edit = canEdit();
    for (const f of [this.pField, this.fField, this.cField]) f.disabled = !edit;
    this.bBtn.disabled = !edit || this.busy;
    this.syncPower(); this.syncFan(); this.syncClock();
  }

  update() {
    const d = this.d(), lv = this.lv(), th = this.stat.thresholds || {}, t = this.t;
    this.title.textContent = d.profile ? d.profile.label : `GPU ${this.stat.index}`;
    this.subtitle.textContent = `${this.stat.name} · PCI ${this.stat.pci.replace(/^0+:/, '')} · VBIOS ${this.stat.vbios}` +
      (lv.pcie_gen ? ` · PCIe Gen ${lv.pcie_gen_max} ×${lv.pcie_width_max ?? lv.pcie_width}` +
        (lv.pcie_width != null && lv.pcie_width_max != null && lv.pcie_width < lv.pcie_width_max
          ? ` (running at ×${lv.pcie_width}: check the slot/riser)` : ` (Gen ${lv.pcie_gen} right now; the link idles down)`) : '') +
      (lv.persistence != null ? ` · persistence ${lv.persistence ? 'on' : 'off'}` : '');
    this.note.textContent = d.profile ? d.profile.note : 'No tuned profile for this card: its power limit can be lowered but not raised above the factory default.';
    this.bBtn.textContent = `Reset to lab baseline (${d.baseline_power_w != null ? d.baseline_power_w + ' W · ' : ''}lab fan curve · no clock cap)`;

    t.temp.value.replaceChildren(n0(lv.temp), el('small', {}, ' °C'));
    t.temp.sub.textContent = th.t_limit && lv.temp != null ? `${th.t_limit - lv.temp}° under T.Limit (${th.t_limit} °C)` : '';
    const fans = (lv.fans || []).filter(v => v != null);
    t.fan.value.replaceChildren(fans.length ? n0(Math.max(...fans)) : '–', el('small', {}, ' %'));
    const mode = { curve: 'curve', manual: 'fixed', auto: 'driver default', unmanaged: 'not managed here' }[d.settings.fan.mode];
    t.fan.sub.textContent = (fans.length > 1 ? fans.map((v, i) => `fan ${i + 1} ${v}%`).join(' · ') + ' · ' : '') + mode;
    const cap = lv.power_limit_w;
    t.power.value.replaceChildren(n0(lv.power_w), el('small', {}, ' W'));
    this.meter(t.power.extra, lv.power_w, cap);
    t.power.sub.textContent = cap != null ? `${lv.power_w != null ? Math.round(lv.power_w / cap * 100) : '–'}% of the ${cap} W cap` : '';
    t.util.value.replaceChildren(n0(lv.util), el('small', {}, ' %'));
    t.util.sub.textContent = `memory bus ${n0(lv.mem_util)}%` +
      (lv.encoder_util || lv.decoder_util ? ` · encode ${n0(lv.encoder_util)}% · decode ${n0(lv.decoder_util)}%` : '');
    t.vram.value.replaceChildren(gib(lv.vram_used_mib), el('small', {}, ` / ${gib(lv.vram_total_mib)} GiB`));
    this.meter(t.vram.extra, lv.vram_used_mib, lv.vram_total_mib);
    t.vram.sub.textContent = '';
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
    t.energy.sub.textContent = 'since the driver loaded';

    const chips = [];
    if (lv.temp != null && th.t_limit && lv.temp >= th.t_limit - 2) chips.push(el('span', { class: 'chip critical' }, 'At the thermal limit'));
    else if (lv.temp != null && lv.temp >= d.full_by_c) chips.push(el('span', { class: 'chip serious' }, `Above ${d.full_by_c} °C: fans forced to 100%`));
    if (lv.ecc_enabled && lv.ecc_uncorrected_total) chips.push(el('span', { class: 'chip critical' }, `${n0(lv.ecc_uncorrected_total)} uncorrected ECC error(s)`));
    else if (lv.ecc_enabled && lv.ecc_corrected_total) chips.push(el('span', { class: 'chip warning' }, `${n0(lv.ecc_corrected_total)} corrected ECC error(s)`));
    this.chips.replaceChildren(...chips);
    const procs = lv.procs || [];
    this.procs.textContent = procs.length ? 'On this card: ' + procs.map(p => `${p.name}${p.vram_mib ? ' ' + gib(p.vram_mib) + ' GiB' : ''}`).join(' · ') : 'Nothing resident on this card.';

    const fs = [];
    if (d.fan.fault) fs.push(el('span', { class: 'chip critical wrap' }, 'Fan fault: ' + d.fan.fault));
    if (d.fan.floor_active) fs.push(el('span', { class: 'chip warning' }, 'The safety floor is setting the speed right now'));
    if (d.settings.fan.mode === 'unmanaged') fs.push(el('span', { class: 'chip' }, 'Fans are run by gpu-fan-curve.service, not by this page'));
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

// ── budget panel ─────────────────────────────────────────────────────────────────────────────
let budgetWired = false;
function wireBudgetEditor() {
  if (budgetWired) return;
  budgetWired = true;
  const input = $('#b-input'), msg = $('#b-msg');
  $('#b-apply').addEventListener('click', async () => {
    const watts = Math.round(Number(input.value));
    if (!Number.isFinite(watts) || watts <= 0) {
      msg.className = 'msg err'; msg.textContent = 'Enter a positive number of watts.';
      return;
    }
    msg.className = 'msg'; msg.textContent = 'Applying…';
    const resp = await apply({ op: 'set_budget', watts });
    if (resp.ok) {
      S.st.daemon = resp;
      msg.className = 'msg ok'; msg.textContent = `Budget set to ${n0(watts)} W.`;
      render();
    } else {
      msg.className = 'msg err'; msg.textContent = resp.error || 'Request failed.';
    }
  });
}
function renderBudget() {
  wireBudgetEditor();
  const st = S.st.daemon, used = st.budget_used_w, left = st.budget_w - used;
  $('#b-used').textContent = n0(used);
  $('#b-sub').textContent = `of a ${n0(st.budget_w)} W combined GPU budget` + (left > 0 ? ` · ${n0(left)} W unallocated` : left === 0 ? ' · fully allocated' : ` · ${n0(-left)} W over`);
  const input = $('#b-input');
  if (document.activeElement !== input) input.value = st.budget_w;
  input.disabled = $('#b-apply').disabled = !canEdit();
  const scale = Math.max(st.budget_w, used);
  $('#b-bar').replaceChildren(...S.cards.map(c => {
    const seg = el('span', { class: 's' + c.slot });
    seg.style.width = ((c.d().settings.power_w || 0) / scale * 100) + '%';
    return seg;
  }));
  $('#b-legend').replaceChildren(...S.cards.map(c => el('span', {}, el('span', { class: `key s${c.slot}` }),
    `${c.d().profile ? c.d().profile.label : 'GPU ' + c.stat.index} ${n0(c.d().settings.power_w)} W`)));
  const draw = S.cards.reduce((a, c) => a + (c.lv().power_w || 0), 0);
  $('#b-draw').replaceChildren(`${n0(draw)} W`, el('span', { class: 'sub' }, 'both cards, measured'));
  $('#b-wall').replaceChildren(`about ${n0(st.wall_estimate_w)} W`, el('span', { class: 'sub' }, 'estimate, everything flat out at once'));
  const c = st.circuits, w = st.wall_estimate_w, names = Object.keys(c);
  const fits = names.find(k => w <= c[k]);
  $('#b-circuit').replaceChildren(fits ? `Within a ${fits} circuit’s continuous rating` : 'Over both common circuit ratings',
    el('span', { class: 'sub' }, names.map(k => `${k}: ${n0(c[k])} W continuous`).join(' · ')));
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
    const h = S.hist; this.W = this.svg.clientWidth;
    if (!h || !this.W) return;
    const def = this.def, k = def.key, g = [];
    this.t1 = h.t.length ? h.t[h.t.length - 1] : Date.now() / 1000; this.t0 = this.t1 - h.window;
    const all = S.cards.flatMap(c => [...h.gpus[c.uuid][k], ...(def.cap ? h.gpus[c.uuid].cap : [])]).filter(v => v != null);
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
      const lim = Math.min(...S.cards.map(c => (c.stat.thresholds || {}).t_limit || 999));
      if (lim < 999) {
        g.push(sv('line', { class: 'ref', x1: this.M.l, x2: this.W - this.M.r, y1: this.py(lim), y2: this.py(lim) }));
        g.push(sv('text', { x: this.W - this.M.r, y: this.py(lim) - 4, 'text-anchor': 'end' }, `T.Limit ${lim} °C`));
      }
    }
    for (const c of S.cards) {
      const s = h.gpus[c.uuid];
      if (def.cap) g.push(sv('path', { class: `capline s${c.slot}`, d: this.path(h.t, s.cap) }));
      g.push(sv('path', { class: `line s${c.slot}`, d: this.path(h.t, s[k]) }));
      let j = s[k].length - 1; while (j >= 0 && s[k][j] == null) j--;
      if (j >= 0) g.push(sv('circle', { class: `dot s${c.slot}`, cx: this.px(h.t[j]), cy: this.py(s[k][j]), r: 4 }));
    }
    this.hover = sv('g');
    g.push(this.hover);
    this.svg.replaceChildren(...g);
    this.root.classList.remove('loading');
    this.drawHover();
  }
  drawHover() {
    const h = S.hist; if (!h || !this.hover) return;
    const k = this.def.key, n = h.t.length;
    const i = S.hoverT != null && n ? nearestIdx(h.t, S.hoverT) : n - 1;
    const marks = [];
    if (S.hoverT != null && n) {
      const x = this.px(h.t[i]);
      marks.push(sv('line', { class: 'cross', x1: x, x2: x, y1: this.M.t, y2: this.H - this.M.b }));
      for (const c of S.cards) { const v = h.gpus[c.uuid][k][i]; if (v != null) marks.push(sv('circle', { class: `dot s${c.slot}`, cx: x, cy: this.py(v), r: 4 })); }
    }
    this.hover.replaceChildren(...marks);
    const name = c => (c.d().profile ? c.d().profile.label : 'GPU ' + c.stat.index);
    // the legend doubles as the readout: latest values, or the values under the crosshair
    const keys = S.cards.map(c => el('span', {}, el('span', { class: `linekey s${c.slot}` }), name(c) + ' ',
      el('b', {}, n ? n1(h.gpus[c.uuid][k][i]) : '–')));
    if (this.def.cap) keys.push(el('span', {}, el('span', { class: 'linekey cap s1' }), 'dashed: power cap'));
    this.legend.replaceChildren(...keys);
    const show = this.own && S.hoverT != null && n;
    this.tip.hidden = !show;
    if (show) {
      this.tip.replaceChildren(el('div', { class: 'when' }, hhmm(h.t[i], true)), ...S.cards.map(c => el('div', { class: 'r' },
        el('span', { class: `linekey s${c.slot}` }), el('b', {}, `${n1(h.gpus[c.uuid][k][i])} ${this.def.unit}`), el('span', { class: 'n' }, name(c)),
        this.def.cap ? el('span', { class: 'n' }, ` · cap ${n0(h.gpus[c.uuid].cap[i])} W`) : null)));
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
  const box = $('#table'), h = S.hist;
  if (!S.tableOpen || !h) return;
  const name = c => (c.d().profile ? c.d().profile.label : 'GPU ' + c.stat.index);
  const cols = CHARTS.map(c => `${c.title} (${c.unit})`);
  const head = el('thead', {},
    el('tr', {}, el('th', {}, ''), ...S.cards.map(c => el('th', { colspan: cols.length }, el('span', { class: `key s${c.slot}` }), name(c)))),
    el('tr', {}, el('th', {}, 'Time'), ...S.cards.flatMap(() => cols.map(t => el('th', {}, t)))));
  const rows = [];
  for (let i = h.t.length - 1; i >= Math.max(0, h.t.length - 60); i--) {
    rows.push(el('tr', {}, el('td', {}, hhmm(h.t[i], true)), ...S.cards.flatMap(c => CHARTS.map(ch => el('td', {}, n1(h.gpus[c.uuid][ch.key][i]))))));
  }
  box.replaceChildren(rows.length ? el('table', {}, head, el('tbody', {}, ...rows)) : el('div', { class: 'empty' }, 'No samples yet.'));
}

// ── page ─────────────────────────────────────────────────────────────────────────────────────
function build() {
  S.cards = S.st.static.map((g, i) => new GpuCard(g, (i % 4) + 1));   // cycle: only 4 colors exist
  $('#gpus').replaceChildren(...S.cards.map(c => c.root));
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
}

function renderBanner() {
  const b = $('#banner'), st = S.st, parts = [];
  let critical = false;
  if (st.daemon_error === 'not installed') {
    parts.push(el('div', {}, el('strong', {}, 'Monitor-only: the control daemon is not installed.'),
      'Every reading and chart below is live, and each card’s safe ranges are shown, but the controls are locked. To unlock them, run this in a terminal (it asks for sudo, takes over from gpu-fan-curve and gpu-power-limit, and rolls back if anything fails):',
      el('br'), el('code', {}, 'ops/gpu-tuner/install.sh')));
  } else if (st.daemon_error) {
    critical = true;
    parts.push(el('div', {}, el('strong', {}, 'The control daemon is not answering.'), `${st.daemon_error}. If it stopped, the fans are back on the driver’s own curve and the power caps are still in place. Check: `, el('code', {}, 'systemctl status gpu-tunerd')));
  }
  if (st.daemon.dry_run) parts.push(el('div', {}, el('strong', {}, 'Dry-run daemon.'), 'Changes are validated and logged exactly as they would be, but nothing is written to the cards.'));
  for (const w of st.daemon.warnings || []) parts.push(el('div', {}, el('strong', {}, 'Daemon warning'), w));
  b.hidden = !parts.length; b.className = 'banner' + (critical ? ' critical' : '');
  b.replaceChildren(...parts);
}

function render() {
  if (!S.st) return;
  if (!S.cards.length) build();
  $('#meta').textContent = `driver ${S.st.driver} · gpu-tuner ${S.st.version}`;
  renderBanner();
  for (const c of S.cards) { c.update(); c.sync(); }
  renderBudget();
}

function setConn(kind, text) { const c = $('#conn'); c.className = 'chip ' + kind; c.textContent = text; }

async function pollState() {
  try {
    S.st = await getJSON('/api/state');
    render();
    setConn(S.st.daemon.preview ? 'warning' : 'good', S.st.daemon.preview ? 'Live · monitor-only' : S.st.daemon.dry_run ? 'Live · dry-run' : 'Live · controls on');
  } catch (e) {
    setConn('critical', S.signedOut ? 'Signed out: run “gpu-tuner open”' : 'Server not responding');
  }
  setTimeout(pollState, 1000);
}
let histTimer = null;
async function pollHistory(now) {
  clearTimeout(histTimer);
  try {
    const h = await getJSON('/api/history?window=' + S.win);
    if (h.window === S.win && S.cards.length) {
      S.hist = h;
      $('#bucket').textContent = h.bucket_s > 1 ? `each point is a ${h.bucket_s} s average` : 'one point per second';
      for (const c of S.charts) c.draw();
      renderTable();
    }
  } catch (e) { /* the connection chip already says so */ }
  histTimer = setTimeout(pollHistory, !S.hist ? 500 : S.win <= 900 ? 2000 : 10000);
}
pollState();
pollHistory();
