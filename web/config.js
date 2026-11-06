'use strict';
/* Config files: draft, check, export. It never writes a file and never changes a setting.

   Load:   what the page runs with (GET /api/config: the hosts it parsed at startup, the shipped
           example, a config.json template), a local file (read in the browser, never uploaded
           except to the check below), or text pasted into the Text view.
   Edit:   a Form view (each field with an (i) pop-up) and a Text view sharing one draft.
   Check:  POST /api/validate-config runs the same rules gpu-tuner serve and gpu-tunerd apply when
           they load the file, on the text only.
   Export: the changes against what was loaded, Copy, Download, and the commands to install it,
           which you run yourself.
   Uses el(), $(), fieldInfo(), richText() from app.js and cmdBlock() from help.js. */

const CFG = {
  meta: null, file: 'hosts', uid: 0,
  files: {
    hosts: { name: 'hosts.json', loaded: null, from: '', draft: '', view: 'form', check: null, checked: null, busy: false, sha: null, shaFor: null },
    config: { name: 'config.json', loaded: null, from: '', draft: '', view: 'form', check: null, checked: null, busy: false, sha: null, shaFor: null },
  },
};
const CFG_MAX_BYTES = 65536;          // the hosts loader's own cap, and this page's limit for any draft
const CFG_MAX_DEPTH = 64;             // real files nest 4 deep; far deeper would overflow the formatter
const HOSTS_DEFAULT = '~/.config/gpu-tuner/hosts.json';

// ── text: format, parse, diff ───────────────────────────────────────────────────────────────
// Deeper than `max` levels? Iterative, so absurd nesting cannot overflow the stack here.
function nestedOver(v, max) {
  const stack = [[v, 1]];
  while (stack.length) {
    const [x, d] = stack.pop();
    if (!x || typeof x !== 'object') continue;
    if (d > max) return true;
    for (const y of Object.values(x)) stack.push([y, d + 1]);
  }
  return false;
}
function compactJSON(v) {
  if (Array.isArray(v)) return '[' + v.map(compactJSON).join(', ') + ']';
  if (v && typeof v === 'object') return '{' + Object.entries(v).map(([k, x]) => JSON.stringify(k) + ': ' + compactJSON(x)).join(', ') + '}';
  return JSON.stringify(v);
}
// hosts.json one machine per line, like examples/hosts.example.json; config.json as install.sh writes it.
// null if it cannot be formatted (parseDraft refuses such nesting first; this is the backstop).
function formatDoc(file, obj) {
  try { return formatDocUnsafe(file, obj); } catch (e) { return null; }
}
function formatDocUnsafe(file, obj) {
  if (file === 'hosts' && obj && typeof obj === 'object' && !Array.isArray(obj) && Array.isArray(obj.hosts)
      && Object.keys(obj).length === 1 && obj.hosts.length) {
    return '{"hosts": [\n' + obj.hosts.map(h => '  ' + compactJSON(h)).join(',\n') + '\n]}\n';
  }
  return JSON.stringify(obj, null, 2) + '\n';
}
function parseDraft(text) {
  let value;
  try { value = JSON.parse(text); } catch (e) {
    return { ok: false, error: e instanceof RangeError ? 'it is nested too deeply for this browser to read' : e.message };
  }
  if (nestedOver(value, CFG_MAX_DEPTH)) {
    return { ok: false, deep: true, error: `it is nested more than ${CFG_MAX_DEPTH} levels deep, which this page does not show or compare (Check still runs the loaders on it)` };
  }
  return { ok: true, value };
}
// Line diff (longest common subsequence). Files here are small; very large drafts get no diff.
function lineDiff(a, b) {
  const A = a.replace(/\n$/, '').split('\n'), B = b.replace(/\n$/, '').split('\n');
  if (a === '') A.length = 0;
  if (b === '') B.length = 0;
  const n = A.length, m = B.length;
  if ((n + 1) * (m + 1) > 4e6) return null;
  const L = new Uint32Array((n + 1) * (m + 1)), W = m + 1;
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      L[i * W + j] = A[i] === B[j] ? L[(i + 1) * W + j + 1] + 1 : Math.max(L[(i + 1) * W + j], L[i * W + j + 1]);
    }
  }
  const out = [];
  let i = 0, j = 0;
  while (i < n && j < m) {
    if (A[i] === B[j]) { out.push([' ', A[i]]); i++; j++; }
    else if (L[(i + 1) * W + j] >= L[i * W + j + 1]) out.push(['-', A[i++]]);
    else out.push(['+', B[j++]]);
  }
  while (i < n) out.push(['-', A[i++]]);
  while (j < m) out.push(['+', B[j++]]);
  return out;
}
// The draft's sha256 as Download writes it (a Blob of a string is UTF-8, like TextEncoder). Shown
// with the install commands so the file is checked before it is copied anywhere. crypto.subtle
// needs a secure context; 127.0.0.1 and localhost are one.
async function sha256Hex(text) {
  const buf = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
  return Array.from(new Uint8Array(buf), b => b.toString(16).padStart(2, '0')).join('');
}
const shq = s => (/^[A-Za-z0-9_./-]+$/.test(s) ? s : "'" + s.replace(/'/g, "'\\''") + "'");
// A path for the shell: `~` and `~/…` become "$HOME"/… (a quoted ~ would not expand).
const shPath = p => (p === '~' ? '"$HOME"' : p.startsWith('~/') ? (p.length > 2 ? '"$HOME"/' + shq(p.slice(2)) : '"$HOME"') : shq(p));
const parentDir = p => { const i = p.lastIndexOf('/'); return i < 0 ? '.' : i === 0 ? '/' : p.slice(0, i); };

// ── the panel ────────────────────────────────────────────────────────────────────────────────
(function configBuilder() {
  const root = $('#cfg-root');
  let uid = 0;
  const nid = () => `cfg-${++uid}`;
  const cur = () => CFG.files[CFG.file];

  const fileTabs = el('div', { class: 'seg', role: 'tablist', 'aria-label': 'Config file' });
  const pathLine = el('p', { class: 'sub cfg-path' });
  const loadRow = el('div', { class: 'row cfg-load' });
  const loadMsg = el('span', { class: 'msg', role: 'status' });
  const sourceNote = el('p', { class: 'msg' });
  const fileInput = el('input', { type: 'file', accept: '.json,application/json', class: 'sr', tabindex: -1, 'aria-hidden': 'true' });
  const viewSeg = el('div', { class: 'seg', role: 'group', 'aria-label': 'Edit as' });
  const formBox = el('div', { class: 'cfg-form' });
  const textArea = el('textarea', { class: 'cfg-text', rows: 14, maxlength: CFG_MAX_BYTES, spellcheck: 'false', autocomplete: 'off', 'aria-label': 'File text' });
  const textBox = el('div', {}, textArea, el('p', { class: 'sub' }, 'Type or paste the whole file here. The form and the text are the same draft.'));
  const parseMsg = el('div', { class: 'msg' });
  const checkBtn = el('button', { type: 'button', class: 'primary', onclick: () => runCheck() }, 'Check');
  const checkBox = el('div', { class: 'cfg-check', role: 'status' });
  const diffSum = el('p', { class: 'sub' });
  const diffPre = el('pre', { class: 'diff' });
  const exportMsg = el('span', { class: 'sub', role: 'status' });
  const readyNote = el('p', { class: 'msg' });
  const cmds = el('div');
  const panel = el('div', { role: 'tabpanel', id: 'cfg-panel' },
    pathLine, loadRow, sourceNote, fileInput,
    el('div', { class: 'row' }, viewSeg),
    formBox, textBox, parseMsg,
    el('div', { class: 'actions' }, checkBtn, el('span', { class: 'sub' }, 'Runs the real loaders’ rules on this text. Nothing is saved.')),
    checkBox,
    el('h3', {}, 'Export'),
    diffSum, diffPre, readyNote,
    el('div', { class: 'row' },
      el('button', { type: 'button', onclick: () => copyDraft() }, 'Copy'),
      el('button', { type: 'button', onclick: () => downloadDraft() }, 'Download'), exportMsg),
    el('h3', {}, 'Install it yourself'), cmds);
  root.replaceChildren(fileTabs, panel);

  const tabFor = {};
  ['hosts', 'config'].forEach((k, i, all) => {
    const t = el('button', { type: 'button', role: 'tab', id: `cfg-tab-${k}`, 'aria-controls': 'cfg-panel',
      onclick: () => selectFile(k, false),
      onkeydown: e => {
        const j = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: all.length - 1 }[e.key];
        if (j == null) return;
        e.preventDefault();
        selectFile(all[(j + all.length) % all.length], true);
      } }, CFG.files[k].name);
    tabFor[k] = t;
    fileTabs.append(t);
  });
  const views = [['form', 'Form'], ['text', 'Text']].map(([v, label]) =>
    el('button', { type: 'button', 'data-view': v, onclick: () => setView(v) }, label));
  viewSeg.append(...views);

  textArea.addEventListener('input', () => { cur().draft = textArea.value; derived(); });
  fileInput.addEventListener('change', () => {
    const f = fileInput.files && fileInput.files[0];
    fileInput.value = '';
    if (!f) return;
    if (f.size > CFG_MAX_BYTES) { say(loadMsg, 'err', `${f.name} is over ${CFG_MAX_BYTES} bytes, this page’s limit for a draft.`); return; }
    const r = new FileReader();
    r.onload = () => load(String(r.result), `your file ${f.name}`);
    r.onerror = () => say(loadMsg, 'err', `Could not read ${f.name}.`);
    r.readAsText(f);
  });

  function say(node, kind, text) { node.className = 'msg' + (kind ? ' ' + kind : ''); node.textContent = text || ''; }

  function selectFile(k, focus) {
    CFG.file = k;
    for (const [key, t] of Object.entries(tabFor)) {
      t.setAttribute('aria-selected', String(key === k));
      t.tabIndex = key === k ? 0 : -1;
    }
    panel.setAttribute('aria-labelledby', `cfg-tab-${k}`);
    if (focus) tabFor[k].focus();
    say(loadMsg, '', '');
    renderFile();
  }

  function sources() {
    const m = CFG.meta;
    if (!m) return [];
    if (CFG.file === 'hosts') {
      const running = m.demo ? 'the demo’s made-up machines'
        : m.hosts.from_file ? `what this page runs with (${m.hosts.path}, as read at startup)`
          : 'what this page runs with (no hosts.json: just this machine)';
      return [['What the page runs with', m.hosts.running, running], ['Example', m.hosts.example, 'the example shipped with gpu-tuner']]
        .filter(s => s[1]);
    }
    const hasLocal = !!(m.hosts.running && m.hosts.running.hosts.some(h => h.id === 'local'));
    return [hasLocal ? ['This machine', () => localConfig(), null] : null,
      [`Template (budget: the daemon’s ${fallbackW()} W fallback)`, () => templateConfig(), null]].filter(Boolean);
  }
  const fallbackW = () => (CFG.meta ? CFG.meta.config.template.gpu_budget_w : '');
  const templateConfig = () => ({ doc: CFG.meta.config.template, placeholder: true,
    from: `a template from the daemon’s defaults (its ${fallbackW()} W budget is a placeholder)` });

  // This machine's config.json from the live state the page already has: the page's uid and the
  // budget this machine's gpu-tunerd runs with now. Without a daemon: what install.sh would seed,
  // the sum of the limits in force, or null where no card has a settable limit. interval_s is
  // not in the daemon's status, so it is the default. null when the state is not there (yet).
  function localConfig(st) {
    const m = CFG.meta, h = (st || S.st) && (st || S.st).hosts.find(x => x.id === 'local');
    if (!m || !h || h.conn !== 'up' || !h.daemon) return null;
    const d = h.daemon;
    let budget, how;
    if (!d.preview && !h.daemon_error) {
      budget = d.budget_w == null ? null : d.budget_w;
      how = budget == null ? 'no budget, as its gpu-tunerd runs now' : `the ${budget} W budget its gpu-tunerd runs with now`;
    } else if (!d.power_settable) {
      budget = null; how = 'no budget: no card here has a settable limit';
    } else if (d.budget_used_w > 0) {
      budget = Math.round(d.budget_used_w); how = `${budget} W, the sum of the limits in force (what install.sh would seed)`;
    } else return null;
    return { doc: { allowed_uid: CFG.uid, gpu_budget_w: budget, interval_s: m.config.template.interval_s }, placeholder: false,
      from: `this machine (${h.label}): uid ${CFG.uid}, ${how}, interval_s the default` };
  }

  function renderFile() {
    const f = cur(), m = CFG.meta;
    pathLine.replaceChildren();
    if (CFG.file === 'hosts') {
      pathLine.append('Lives at ', el('code', {}, m ? m.hosts.path : HOSTS_DEFAULT),
        ' on the machine with this page, owned by you, mode 600. Read when the page starts. ', fieldInfo('hosts', 'hosts', 'hosts.json'));
    } else {
      pathLine.append('Lives at ', el('code', {}, '/etc/gpu-tuner/config.json'),
        ' on each machine with gpu-tunerd, owned by root. Read when gpu-tunerd starts; this page never reads it. ');
    }
    loadRow.replaceChildren(el('span', { class: 'sub' }, 'Load:'),
      ...sources().map(([label, doc, from]) => el('button', { type: 'button', class: 'small', onclick: () => {
        // 'This machine' is rebuilt from the live state at the click; the others are fixed documents
        const got = typeof doc === 'function' ? doc() : { doc, from };
        if (!got) { say(loadMsg, 'err', 'This machine’s state is not available right now. Try again in a moment.'); return; }
        const text = formatDoc(CFG.file, got.doc);
        if (text == null) { say(loadMsg, 'err', 'That source is not available right now.'); return; }
        load(text, got.from, got.placeholder);
      } }, label)),
      el('button', { type: 'button', class: 'small', onclick: () => fileInput.click() }, 'Open a file…'), loadMsg);
    setView(f.view);
  }

  function load(text, from, placeholder) {
    const f = cur();
    if (f.loaded != null && f.draft !== f.loaded && !confirm(`Replace your unsaved draft with ${from}?`)) return;
    f.loaded = text; f.from = from; f.draft = text; f.check = null; f.checked = null; f.placeholder = !!placeholder;
    say(loadMsg, 'ok', `Loaded ${from}.`);
    setView(f.view);
  }

  // Why the loaded budget is what it is. A budget under a machine's saved limits makes its daemon
  // lower them on start, so a placeholder must never look like a recommendation.
  function renderSourceNote() {
    const f = cur();
    if (CFG.file !== 'config') { say(sourceNote, '', ''); sourceNote.hidden = true; return; }
    sourceNote.hidden = false;
    if (f.placeholder) {
      say(sourceNote, 'warn', `gpu_budget_w ${fallbackW()} W is the daemon’s fallback, a placeholder, not a value for any machine. ` +
        'If the power limits saved on the machine this file is for add up to more, its gpu-tunerd lowers them to each card’s baseline when it starts. ' +
        'Set it from that machine’s Power budget panel (or the sum of its cards’ limits) before installing.');
    } else {
      say(sourceNote, 'sub', 'For another machine, set allowed_uid and gpu_budget_w from that machine (its uid there, its Power budget panel): a budget under its saved limits makes its gpu-tunerd lower them when it starts.');
    }
  }

  function setView(v) {
    const f = cur();
    f.view = v;
    for (const b of views) b.setAttribute('aria-pressed', String(b.dataset.view === v));
    formBox.hidden = v !== 'form'; textBox.hidden = v !== 'text';
    if (v === 'form') renderForm(); else textArea.value = f.draft;
    renderSourceNote();
    derived();
  }

  // The form edits the parsed draft and writes it back as text; structural changes re-render it.
  function commit(obj) {
    const f = cur(), text = formatDoc(CFG.file, obj);
    if (text == null) return;
    f.draft = text;
    textArea.value = f.draft;
    derived();
  }

  function field(file, key, label, input, ...extra) {
    const id = nid();
    input.id = id;
    return el('div', { class: 'cfg-field' }, el('div', { class: 'cfg-lbl' }, el('label', { for: id }, label), fieldInfo(file, key, label)),
      el('div', { class: 'cfg-in' }, input, ...extra));
  }
  const numOrDrop = (obj, k, input, int) => {
    const v = input.value.trim() === '' ? NaN : Number(input.value);
    if (!Number.isFinite(v)) delete obj[k]; else obj[k] = int ? Math.trunc(v) : v;
  };
  const textInput = (value, attrs) => el('input', Object.assign({ type: 'text', value: value == null ? '' : String(value),
    spellcheck: 'false', autocomplete: 'off' }, attrs || {}));
  const numInput = (value, attrs) => el('input', Object.assign({ type: 'number',
    value: typeof value === 'number' ? String(value) : '' }, attrs || {}));

  function renderForm() {
    const f = cur(), p = parseDraft(f.draft);
    if (!p.ok) {
      formBox.replaceChildren(el('p', { class: 'msg warn' }, p.deep ? `The form cannot show this text: ${p.error}. Edit it in the Text view.`
        : 'The text is not valid JSON, so the form cannot show it. Fix it in the Text view, or load a file again.'));
      return;
    }
    if (CFG.file === 'hosts') renderHostsForm(p.value); else renderConfigForm(p.value);
  }

  function renderHostsForm(doc) {
    if (!doc || typeof doc !== 'object' || Array.isArray(doc) || !Array.isArray(doc.hosts)
        || doc.hosts.some(h => !h || typeof h !== 'object' || Array.isArray(h))) {
      formBox.replaceChildren(el('p', { class: 'msg warn' }, 'The form shows a {"hosts": [ … ]} file with one object per machine; this text has another shape. Edit it in the Text view.'));
      return;
    }
    const other = Object.keys(doc).filter(k => k !== 'hosts');
    const cards = doc.hosts.map((h, i) => hostCard(doc, h, i));
    const add = el('button', { type: 'button', class: 'small', disabled: doc.hosts.length >= 32,
      onclick: () => { doc.hosts.push({ id: '', ssh: '' }); commit(doc); renderForm(); focusLast('.cfg-host input'); } }, 'Add a machine');
    formBox.replaceChildren(...[
      other.length ? el('p', { class: 'msg warn' }, `Other top-level keys (the page refuses them): ${other.join(', ')}`) : null,
      ...cards, el('div', { class: 'row' }, add, el('span', { class: 'sub' }, 'Order here is the order of the tabs.'))].filter(Boolean));
  }
  function focusLast(sel) { const all = formBox.querySelectorAll(sel); if (all.length) all[all.length - 1].focus(); }

  function hostCard(doc, h, i) {
    const n = doc.hosts.length, re = () => { commit(doc); renderForm(); };
    const setStr = (k, keepEmpty) => e => { const v = e.target.value; if (v === '' && !keepEmpty) delete h[k]; else h[k] = v; commit(doc); };
    const port = numInput(h.port, { min: 1, max: 65535, step: 1 });
    port.addEventListener('input', () => { numOrDrop(h, 'port', port, true); commit(doc); });
    const hasWall = !!h.wall && typeof h.wall === 'object' && !Array.isArray(h.wall);
    const wallOn = el('input', { type: 'checkbox', checked: hasWall,
      onchange: e => { if (e.target.checked) h.wall = { non_gpu_dc_w: null, psu_efficiency: null }; else delete h.wall; re(); } });
    const wallBox = el('div', { class: 'cfg-wall' });
    if (hasWall) {
      const w = h.wall;
      const dc = numInput(w.non_gpu_dc_w, { min: 0, max: 100000, step: 1 });
      dc.addEventListener('input', () => { numOrDrop(w, 'non_gpu_dc_w', dc, false); commit(doc); });
      const eff = numInput(w.psu_efficiency, { min: 0.5, max: 1, step: 0.01 });
      eff.addEventListener('input', () => { numOrDrop(w, 'psu_efficiency', eff, false); commit(doc); });
      const circ = w.circuits && typeof w.circuits === 'object' && !Array.isArray(w.circuits) ? w.circuits : {};
      const rows = Object.entries(circ);
      const writeRows = () => { if (rows.length) w.circuits = Object.fromEntries(rows); else delete w.circuits; commit(doc); };
      const rowEls = rows.map((r, j) => {
        const name = textInput(r[0], { 'aria-label': `Circuit ${j + 1} name`, maxlength: 16, class: 'cfg-cname' });
        const watts = numInput(r[1], { 'aria-label': `Circuit ${j + 1} watts`, min: 1, max: 100000, step: 1 });
        name.addEventListener('input', () => { r[0] = name.value; writeRows(); });
        watts.addEventListener('input', () => { const v = Number(watts.value); r[1] = watts.value.trim() !== '' && Number.isFinite(v) ? v : null; writeRows(); });
        return el('div', { class: 'row' }, name, watts, el('span', {}, 'W'),
          el('button', { type: 'button', class: 'ghost small', onclick: () => { rows.splice(j, 1); writeRows(); renderForm(); } }, 'Remove'));
      });
      const otherW = Object.keys(w).filter(k => !['non_gpu_dc_w', 'psu_efficiency', 'circuits'].includes(k));
      wallBox.append(
        field('hosts', 'hosts.#.wall.non_gpu_dc_w', 'non_gpu_dc_w', dc, el('span', {}, 'W')),
        field('hosts', 'hosts.#.wall.psu_efficiency', 'psu_efficiency', eff),
        el('div', { class: 'cfg-field' }, el('div', { class: 'cfg-lbl' }, el('span', {}, 'circuits'), fieldInfo('hosts', 'hosts.#.wall.circuits', 'circuits')),
          el('div', { class: 'cfg-in cfg-rows' }, ...rowEls,
            el('div', { class: 'row' }, el('button', { type: 'button', class: 'small', disabled: rows.length >= 4,
              onclick: () => { rows.push(['', null]); writeRows(); renderForm(); } }, 'Add a circuit')))));
      if (otherW.length) wallBox.append(el('p', { class: 'msg warn' }, `Other keys in wall (the page refuses them): ${otherW.join(', ')}`));
    }
    const other = Object.keys(h).filter(k => !['id', 'label', 'ssh', 'port', 'wall'].includes(k));
    const move = (d) => { const [x] = doc.hosts.splice(i, 1); doc.hosts.splice(i + d, 0, x); re(); };
    const title = typeof h.id === 'string' && h.id ? h.id : `machine ${i + 1}`;
    return el('fieldset', { class: 'cfg-host' }, el('legend', {}, `Machine ${i + 1}`),
      field('hosts', 'hosts.#.id', 'id', textInput(h.id, { oninput: setStr('id', true), maxlength: 32 })),
      field('hosts', 'hosts.#.label', 'label', textInput(h.label, { oninput: setStr('label'), maxlength: 64 })),
      field('hosts', 'hosts.#.ssh', 'ssh', textInput(h.ssh, { oninput: setStr('ssh'), placeholder: 'alias or user@host' })),
      field('hosts', 'hosts.#.port', 'port', port),
      el('div', { class: 'cfg-field' }, el('div', { class: 'cfg-lbl' }, el('label', { class: 'check' }, wallOn, 'wall'), fieldInfo('hosts', 'hosts.#.wall', 'wall')),
        el('div', { class: 'cfg-in' }, el('span', { class: 'sub' }, 'optional wall-power estimate, display only'))),
      wallBox,
      other.length ? el('p', { class: 'msg warn' }, `Other keys (the page refuses them): ${other.join(', ')}`) : null,
      el('div', { class: 'row' },
        el('button', { type: 'button', class: 'ghost small', disabled: i === 0, onclick: () => move(-1) }, 'Move up'),
        el('button', { type: 'button', class: 'ghost small', disabled: i === n - 1, onclick: () => move(1) }, 'Move down'),
        el('button', { type: 'button', class: 'ghost small', 'aria-label': `Remove ${title}`,
          onclick: () => { doc.hosts.splice(i, 1); re(); } }, 'Remove')));
  }

  function renderConfigForm(doc) {
    if (!doc || typeof doc !== 'object' || Array.isArray(doc)) {
      formBox.replaceChildren(el('p', { class: 'msg warn' }, 'config.json must be a JSON object; this text is not. Edit it in the Text view.'));
      return;
    }
    const uidIn = numInput(doc.allowed_uid, { min: 0, max: 4294967294, step: 1 });
    uidIn.addEventListener('input', () => { numOrDrop(doc, 'allowed_uid', uidIn, false); commit(doc); });
    const isNull = 'gpu_budget_w' in doc && doc.gpu_budget_w === null;
    const budget = numInput(doc.gpu_budget_w, { min: 1, max: 100000, step: 1, disabled: isNull });
    budget.addEventListener('input', () => { numOrDrop(doc, 'gpu_budget_w', budget, false); commit(doc); });
    const noBudget = el('input', { type: 'checkbox', checked: isNull, onchange: e => {
      if (e.target.checked) doc.gpu_budget_w = null; else delete doc.gpu_budget_w;
      commit(doc); renderForm();
    } });
    const interval = numInput(doc.interval_s, { min: 1, max: 10, step: 0.5 });
    interval.addEventListener('input', () => { numOrDrop(doc, 'interval_s', interval, false); commit(doc); });
    const other = Object.keys(doc).filter(k => !['allowed_uid', 'gpu_budget_w', 'interval_s'].includes(k));
    formBox.replaceChildren(el('fieldset', { class: 'cfg-host' }, el('legend', {}, 'gpu-tunerd settings'),
      field('config', 'allowed_uid', 'allowed_uid', uidIn, el('span', { class: 'sub' }, CFG.meta && CFG.meta.demo ? 'demo: a placeholder uid' : `this page runs as uid ${CFG.uid}`)),
      field('config', 'gpu_budget_w', 'gpu_budget_w', budget, el('span', {}, 'W'),
        el('label', { class: 'check' }, noBudget, 'null: no card here has a settable limit')),
      field('config', 'interval_s', 'interval_s', interval, el('span', {}, 's')),
      other.length ? el('p', { class: 'msg warn' }, `Other keys (ignored by gpu-tunerd): ${other.join(', ')}`) : null,
      el('p', { class: 'sub' }, 'A field left empty is left out of the file, and the daemon uses its default.')));
  }

  // ── derived views: parse state, check result, diff, commands ────────────────────────────────
  function derived() {
    const f = cur(), p = parseDraft(f.draft);
    if (f.view === 'text') say(parseMsg, p.ok ? '' : 'warn', p.ok ? '' : p.deep ? `Valid JSON, but ${p.error}.` : `Not valid JSON yet: ${p.error}`);
    else say(parseMsg, '', '');
    renderCheck();
    renderDiff();
    renderCommands();
  }

  async function runCheck() {
    const f = cur(), kind = CFG.file, text = f.draft;
    f.busy = true; checkBtn.disabled = true;
    checkBox.replaceChildren(el('p', { class: 'msg' }, 'Checking…'));
    let resp;
    try {
      const r = await fetch('/api/validate-config', { method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ kind, text }) });
      resp = r.status === 401 ? { ok: false, error: 'signed out: run “gpu-tuner open”' } : await r.json();
      if (!r.ok && !resp.error) resp.error = 'HTTP ' + r.status;
    } catch (e) {
      resp = { ok: false, error: 'could not reach the gpu-tuner server: ' + e.message };
    }
    f.busy = false; checkBtn.disabled = false;
    f.check = resp; f.checked = text;
    if (CFG.file === kind) derived();
  }

  function renderCheck() {
    const f = cur(), c = f.check;
    if (!c) { checkBox.replaceChildren(el('p', { class: 'sub' }, 'Not checked yet.')); return; }
    const nodes = [];
    if (f.checked !== f.draft) nodes.push(el('p', { class: 'msg warn' }, 'These results are for an earlier draft: check again.'));
    if (c.error) nodes.push(el('p', { class: 'msg err' }, `The check could not run: ${c.error}`));
    else if (c.ok) {
      let summary = '';
      if (c.machines) summary = ` ${c.machines.length} machine${c.machines.length === 1 ? '' : 's'}: ` +
        c.machines.map(m => `${m.label} (${m.id === 'local' ? 'this machine' : 'over ssh'})`).join(', ') + '.';
      if (c.effective) {
        const e = c.effective;
        summary = ` gpu-tunerd would run with allowed_uid ${e.allowed_uid == null ? 'none (root only)' : e.allowed_uid}, ` +
          `budget ${e.gpu_budget_w == null ? 'none' : e.gpu_budget_w + ' W'}, interval ${e.interval_s} s.`;
      }
      nodes.push(el('p', { class: 'msg ok' }, 'Passes.' + summary));
    }
    for (const e of c.errors || []) nodes.push(el('p', { class: 'msg err' }, e));
    for (const w of c.warnings || []) nodes.push(el('p', { class: 'msg warn' }, w));
    checkBox.replaceChildren(...nodes);
  }

  function renderDiff() {
    const f = cur(), a = parseDraft(f.loaded || ''), b = parseDraft(f.draft);
    const fa = f.loaded != null && a.ok && b.ok ? formatDoc(CFG.file, a.value) : null;
    const fb = fa != null ? formatDoc(CFG.file, b.value) : null;
    const canon = fa != null && fb != null;
    const base = f.loaded == null ? '' : canon ? fa : f.loaded;
    const now = canon ? fb : f.draft;
    const d = lineDiff(base, now);
    if (!d) { diffSum.textContent = 'Too large to compare here.'; diffPre.replaceChildren(); return; }
    const add = d.filter(x => x[0] === '+').length, del = d.filter(x => x[0] === '-').length;
    const against = f.loaded == null ? 'nothing loaded yet' : f.from;
    diffSum.textContent = !add && !del ? `No changes from ${against}.`
      : `Compared with ${against}${canon ? ' (formatting ignored)' : ''}: ${add} line${add === 1 ? '' : 's'} added, ${del} removed.`;
    diffPre.replaceChildren(...d.map(([t, s]) => el('span', { class: t === '+' ? 'add' : t === '-' ? 'del' : 'same' }, `${t} ${s}\n`)));
    diffPre.hidden = !add && !del;
  }

  function readiness() {
    const f = cur(), c = f.check;
    if (!c || f.checked !== f.draft) return ['warn', 'Not checked since the last change. Check it before you install it.'];
    if (c.error) return ['warn', 'The last check could not run. Check it again before you install it.'];
    if (!c.ok) return ['err', CFG.file === 'hosts' ? 'The last check failed: gpu-tuner serve would refuse this file and not start.'
      : 'The last check failed: gpu-tunerd would refuse this file and not start.'];
    return ['ok', 'Checked: this exact text passes.'];
  }

  // The draft's hash, or null while it is computed (renderCommands runs again when it is ready),
  // or '' where this browser cannot compute one.
  function draftSha() {
    const f = cur(), text = f.draft;
    if (f.sha && f.sha.text === text) return f.sha.hex;
    if (!(window.crypto && crypto.subtle)) return '';
    if (f.shaFor !== text) {
      f.shaFor = text;
      sha256Hex(text).then(hex => { f.sha = { text, hex }; }, () => { f.sha = { text, hex: '' }; })
        .then(() => { if (cur() === f && f.draft === text) renderCommands(); });
    }
    return null;
  }

  // One block, run as a whole: `( set -e …)` stops at the first command that fails (each command
  // is on its own line, where set -e applies), so a failed sha256 check installs nothing.
  // hex: the draft's sha256, '' where the browser cannot compute one. null while it is computed.
  function block(lines) { return ['( set -e', ...lines.map(l => '  ' + l), ')'].join('\n'); }
  const shaCheck = (hex, file) => `echo "${hex}  ${file}" | sha256sum -c -`;

  function renderCommands() {
    const [kind, text] = readiness();
    say(readyNote, kind, text);
    const m = CFG.meta, hex = draftSha(), name = cur().name, dl = `"$HOME"/Downloads/${name}`;
    const shaLine = el('p', { class: 'sub cfg-sha' }, hex == null ? 'sha256 of the download: working it out…'
      : hex ? `sha256 of the download: ${hex}` : 'sha256 of the download: not available in this browser.');
    if (hex == null) { cmds.replaceChildren(shaLine); return; }
    const noSha = '# this browser cannot compute a sha256: compare the file with the draft yourself first';
    if (CFG.file === 'hosts') {
      const path = m ? m.hosts.path : HOSTS_DEFAULT, p = shPath(path);
      const mkdir = path === HOSTS_DEFAULT ? ['mkdir -p ~/.config/gpu-tuner', 'chmod 700 ~/.config/gpu-tuner']
        : [`mkdir -p ${shPath(parentDir(path))}`];
      const check = path === HOSTS_DEFAULT ? './gpu-tuner hosts --check' : `./gpu-tuner hosts --check --hosts ${p}`;
      cmds.replaceChildren(el('p', { class: 'sub' }, 'On the machine with this page, as yourself (not root), as one block: it stops at the first step that fails. The loader wants a regular file, owned by you, not writable by group or others. Adjust the Downloads path to where your browser saved the download. If the download is not byte-for-byte this draft, the sha256 check fails and nothing after it runs.'),
        shaLine,
        cmdBlock(block([
          ...mkdir,
          `[ ! -f ${p} ] || cp -p ${p} ${p}.bak      # keep the current one`,
          hex ? shaCheck(hex, `$HOME/Downloads/${name}`) : noSha,
          `install -m 600 ${dl} ${p}`,
          'systemctl --user restart gpu-tuner-ui      # the page reads hosts.json only at start',
          `${check}      # from the gpu-tuner checkout: reaches each machine once`,
        ])));
    } else {
      const etc = '/etc/gpu-tuner/config.json';
      cmds.replaceChildren(el('p', { class: 'sub' }, 'On the machine this file is for (each machine has its own; copy the file there first, for example with scp), as one block: it stops at the first step that fails. The file is copied next to the real one as root, and the sha256 check runs on that root-owned copy, so config.json is replaced only by exactly this draft. Restarting gpu-tunerd hands the fans to the driver for a moment and leaves the power limits in place; it then re-applies the saved settings, lowered to each card’s baseline if they total more than gpu_budget_w.'),
        shaLine,
        cmdBlock(block([
          'sudo install -d -m 755 /etc/gpu-tuner',
          `sudo install -o root -g root -m 644 ${dl} ${etc}.new`,
          hex ? shaCheck(hex, `${etc}.new`) : noSha,
          `[ ! -f ${etc} ] || sudo cp -p ${etc} ${etc}.bak      # keep the current one`,
          `sudo mv ${etc}.new ${etc}`,
          'sudo systemctl restart gpu-tunerd',
          'journalctl -u gpu-tunerd -n 5      # its start line shows the budget and interval it read',
        ])));
    }
  }

  async function copyDraft() {
    try { await navigator.clipboard.writeText(cur().draft); exportMsg.textContent = `Copied ${cur().name}.`; }
    catch (e) { exportMsg.textContent = 'Copy failed: use the Text view and copy it by hand.'; }
  }
  function downloadDraft() {
    const f = cur(), url = URL.createObjectURL(new Blob([f.draft], { type: 'application/json' }));
    const a = el('a', { href: url, download: f.name, class: 'sr' });
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
    exportMsg.textContent = `Downloaded ${f.name}.`;
  }

  // Start hosts.json from what the page runs with, and config.json from this machine's live state
  // (its budget, not the daemon's fallback); the template only where that state is not there.
  Promise.all([loadJSON('/api/config'), S.st ? Promise.resolve(S.st) : getJSON('/api/state').catch(() => null)]).then(([m, st]) => {
    CFG.meta = m;
    CFG.uid = m.config.template.allowed_uid;
    const cfg = localConfig(st) || templateConfig();
    const first = { hosts: { doc: m.hosts.running, from: m.demo ? 'the demo’s made-up machines' : 'what this page runs with' },
      config: cfg };
    for (const k of ['hosts', 'config']) {
      const f = CFG.files[k], text = formatDoc(k, first[k].doc);
      f.loaded = text; f.from = first[k].from; f.draft = text; f.placeholder = !!first[k].placeholder;
    }
    selectFile('hosts', false);
  }).catch(() => {
    root.replaceChildren(el('p', { class: 'msg err' }, 'Could not load the config builder. Reload the page to try again.'));
  });
})();
