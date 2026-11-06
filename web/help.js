'use strict';
/* Help panel: Getting started | FAQ (searchable). The text comes from /help.en.json and is shown
   with text nodes only (`x` is code, a blank line a new paragraph); nothing is parsed as HTML.
   Uses el(), richText() and loadJSON() from app.js, which loads first. */

// A command line with a Copy button. The clipboard needs a secure context, which 127.0.0.1 is.
function cmdBlock(text) {
  const code = el('code', {}, text);
  const msg = el('span', { class: 'sub', role: 'status' });
  const copy = el(
    'button',
    {
      type: 'button',
      class: 'ghost small',
      onclick: async () => {
        try {
          await navigator.clipboard.writeText(text);
          msg.textContent = 'Copied.';
        } catch (e) {
          msg.textContent = 'Copy failed: select the text and copy it by hand.';
        }
        setTimeout(() => {
          msg.textContent = '';
        }, 3000);
      },
    },
    'Copy'
  );
  return el('div', { class: 'cmd' }, el('pre', {}, code), el('div', { class: 'row' }, copy, msg));
}

// FAQ items whose question, answer or command contains every word typed (case-insensitive).
function filterFaq(items, query) {
  const words = String(query || '')
    .toLowerCase()
    .split(/\s+/)
    .filter(Boolean);
  return items.filter(it => {
    const hay = `${it.q} ${it.a} ${it.cmd || ''}`.toLowerCase();
    return words.every(w => hay.includes(w));
  });
}

(function help() {
  const tabs = [$('#help-tab-start'), $('#help-tab-faq')];
  const panels = [$('#help-start'), $('#help-faq')];
  function selectTab(i, focus) {
    tabs.forEach((t, j) => {
      t.setAttribute('aria-selected', String(i === j));
      t.tabIndex = i === j ? 0 : -1;
      panels[j].hidden = i !== j;
    });
    if (focus) tabs[i].focus();
  }
  tabs.forEach((t, i) => {
    t.addEventListener('click', () => selectTab(i, false));
    t.addEventListener('keydown', e => {
      const j = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: tabs.length - 1 }[e.key];
      if (j == null) return;
      e.preventDefault();
      selectTab((j + tabs.length) % tabs.length, true);
    });
  });
  selectTab(0, false);
  $('#help-open').addEventListener('click', () => {
    $('#help').scrollIntoView({ block: 'start' });
    const t = tabs.find(x => x.getAttribute('aria-selected') === 'true');
    if (t) t.focus({ preventScroll: true });
  });

  function renderStart(doc) {
    const gs = doc.getting_started;
    $('#help-start').replaceChildren(
      el('div', { class: 'lead' }, ...richText(gs.lead)),
      el(
        'ol',
        { class: 'steps' },
        ...gs.steps.map(s => el('li', {}, el('h3', {}, s.title), ...richText(s.body), s.cmd ? cmdBlock(s.cmd) : null))
      )
    );
  }
  function renderFaq(doc) {
    const q = $('#faq-q'),
      list = $('#faq-list'),
      count = $('#faq-count');
    const items = doc.faq.map(it => ({
      it,
      node: el(
        'details',
        { class: 'faq-item' },
        el('summary', {}, it.q),
        el('div', { class: 'faq-a' }, ...richText(it.a), it.cmd ? cmdBlock(it.cmd) : null)
      ),
    }));
    const empty = el('p', { class: 'sub', hidden: true }, 'No question matches. Try fewer or other words.');
    list.replaceChildren(...items.map(x => x.node), empty);
    const apply = () => {
      const keep = new Set(filterFaq(doc.faq, q.value));
      let n = 0;
      for (const x of items) {
        x.node.hidden = !keep.has(x.it);
        if (!x.node.hidden) n++;
      }
      empty.hidden = n > 0;
      count.textContent = q.value.trim() ? `${n} of ${items.length} questions` : `${items.length} questions`;
    };
    q.addEventListener('input', apply);
    apply();
  }
  loadJSON('/help.en.json')
    .then(doc => {
      renderStart(doc);
      renderFaq(doc);
    })
    .catch(() => {
      for (const p of [$('#help-start'), $('#faq-list')]) {
        p.replaceChildren(
          el('p', { class: 'sub' }, 'The help text could not be loaded. Reload the page to try again.')
        );
      }
    });
})();
