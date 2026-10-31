'use strict';

const form = document.querySelector('#login-form');
const passwordInput = document.querySelector('#password');
const button = document.querySelector('#login-button');
const error = document.querySelector('#login-error');

form.addEventListener('submit', async event => {
  event.preventDefault();

  error.hidden = true;
  error.textContent = '';
  button.disabled = true;
  button.textContent = 'Signing in…';

  let signedIn = false;

  try {
    const response = await fetch('/api/login', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        password: passwordInput.value,
        remember: document.querySelector('#remember').checked,
      }),
      cache: 'no-store',
    });

    if (response.ok) {
      signedIn = true;
      passwordInput.value = '';
      window.location.replace('/');
      return;
    }

    if (response.status === 429) {
      error.textContent = 'Too many sign-in attempts. Please wait a minute and try again.';
    } else if (response.status === 503) {
      error.textContent = 'Password authentication is unavailable. Check the container configuration.';
    } else if (response.status === 401) {
      error.textContent = 'Incorrect password.';
    } else {
      error.textContent = 'Unable to sign in. Please try again.';
    }
  } catch (e) {
    error.textContent = 'Could not connect to GPU Tuner. Check your connection.';
  } finally {
    if (!signedIn) {
      error.hidden = false;
      button.disabled = false;
      button.textContent = 'Sign in';
    }
  }
});

const resetCommand = document.querySelector('#reset-command');
const copyStatus = document.querySelector('#copy-status');
document.querySelector('#copy-reset').addEventListener('click', async () => {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(resetCommand.value);
    } else {
      resetCommand.focus();
      resetCommand.select();
      if (!document.execCommand('copy')) throw new Error('Copy unavailable');
    }
    copyStatus.textContent = 'Command copied. Paste it into the Unraid terminal.';
  } catch (_) {
    resetCommand.focus();
    resetCommand.select();
    copyStatus.textContent = 'Select and copy the command, then paste it into the Unraid terminal.';
  }
});
