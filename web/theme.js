'use strict';

// ── theme ────────────────────────────────────────────────────────────────────────────────────
(function theme() {
  const $ = selector => document.querySelector(selector);
  let saved = null;
  try { saved = localStorage.getItem('gpu-tuner-theme'); } catch (e) { /* private mode */ }
  if (saved === 'dark' || saved === 'light') document.documentElement.dataset.theme = saved;
  const button = $('#theme');
  const systemTheme = matchMedia('(prefers-color-scheme: dark)');
  const isDark = () => document.documentElement.dataset.theme
    ? document.documentElement.dataset.theme === 'dark'
    : systemTheme.matches;
  function updateLabel() {
    const label = isDark() ? 'Light mode' : 'Dark mode';
    button.textContent = label;
    button.setAttribute('aria-label', `Switch to ${label.toLowerCase()}`);
    button.title = `Switch to ${label.toLowerCase()}`;
  }
  updateLabel();
  systemTheme.addEventListener('change', updateLabel);
  button.addEventListener('click', () => {
    const next = isDark() ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem('gpu-tuner-theme', next); } catch (e) { /* ignore */ }
    updateLabel();
  });
})();
