var EvaSignalSettings = (function() {
  'use strict';
  var state = { id: '', ownedId: '', status: 'idle', timer: null, visible: false, busy: false, sequence: 0 };

  function show(value) {
    state.status = value.status || 'failed';
    if (value.id) state.id = value.id;
    var running = ['starting', 'pending', 'cancelling'].indexOf(state.status) >= 0;
    var start = document.getElementById('signalLinkBtn');
    var cancel = document.getElementById('signalLinkCancelBtn');
    var image = document.getElementById('signalLinkQr');
    var status = document.getElementById('signalLinkStatus');
    if (start) start.disabled = state.busy || running;
    if (cancel) { cancel.hidden = !running; cancel.disabled = state.busy || state.status === 'cancelling'; }
    if (status) status.textContent = value.message || 'Link Signal to send notifications from Eva.';
    if (image) {
      if (state.visible && state.status === 'pending' && /^data:image\/png;base64,[A-Za-z0-9+/=]+$/.test(value.qr || '')) {
        image.src = value.qr;
        image.hidden = false;
      } else {
        image.hidden = true;
        image.removeAttribute('src');
      }
    }
    if (state.status === 'linked') {
      [['authSignalSender', 'signal_sender', value.sender], ['authSignalRecipient', 'signal_recipient', value.recipient]].forEach(function(item) {
        if (!item[2]) return;
        var input = document.getElementById(item[0]);
        if (input && (item[0] === 'authSignalSender' || !input.value.trim())) input.value = item[2];
        localStorage.setItem(item[1], item[2]);
      });
    }
  }

  async function request(path, data) {
    var base = typeof getSafeBridgeBaseUrl === 'function' ? getSafeBridgeBaseUrl() : getACPBridgeUrl().replace(/\/+$/, '');
    var options = { headers: getBridgeCapabilityHeaders(), cache: 'no-store', signal: AbortSignal.timeout(10000) };
    if (data !== undefined) {
      options.method = 'POST';
      options.body = JSON.stringify(data);
    }
    var response = await fetch(base + path, options);
    var value = await response.json();
    if (!response.ok) throw new Error(value.error && value.error.message || 'Signal linking is unavailable.');
    return value;
  }

  function poll() {
    clearTimeout(state.timer);
    if (state.visible && ['starting', 'pending', 'cancelling'].indexOf(state.status) >= 0) {
      state.timer = setTimeout(refresh, 1000);
    }
  }

  async function refresh() {
    state.visible = true;
    var sequence = ++state.sequence;
    try {
      var value = await request('/v1/signal/link');
      if (sequence !== state.sequence || !state.visible) return;
      show(value);
    } catch (error) {
      if (sequence !== state.sequence) return;
      show({ status: 'failed', message: error.message + ' Reopen Accounts to check the current attempt.' });
    }
    poll();
  }

  async function start() {
    if (state.busy) return;
    state.busy = true;
    state.visible = true;
    state.id = '';
    state.ownedId = '';
    ++state.sequence;
    show({ status: 'starting', message: 'Preparing a fresh linking code...' });
    try {
      var value = await request('/v1/signal/link', {});
      state.ownedId = value.id || '';
      if (!state.visible) {
        if (value.id) await request('/v1/signal/link/cancel', { id: value.id });
        return;
      }
      show(value);
    } catch (error) {
      show({ status: 'failed', message: error.message });
    } finally {
      state.busy = false;
      var startButton = document.getElementById('signalLinkBtn');
      if (startButton) startButton.disabled = ['starting', 'pending', 'cancelling'].indexOf(state.status) >= 0;
      var cancelButton = document.getElementById('signalLinkCancelBtn');
      if (cancelButton) cancelButton.disabled = state.status === 'cancelling';
      poll();
    }
  }

  async function cancel() {
    if (!state.id) return;
    var sequence = ++state.sequence;
    clearTimeout(state.timer);
    show({ status: 'cancelling', message: 'Cancelling linking...' });
    try {
      var value = await request('/v1/signal/link/cancel', { id: state.id });
      if (sequence === state.sequence) show(value);
    } catch (error) {
      if (sequence === state.sequence) show({ status: 'failed', message: error.message });
    }
    poll();
  }

  function close() {
    state.visible = false;
    ++state.sequence;
    clearTimeout(state.timer);
    if (state.id && state.ownedId === state.id && ['starting', 'pending', 'cancelling'].indexOf(state.status) >= 0) cancel();
    var image = document.getElementById('signalLinkQr');
    if (image) { image.hidden = true; image.removeAttribute('src'); }
  }

  document.addEventListener('DOMContentLoaded', function() {
    var startButton = document.getElementById('signalLinkBtn');
    var cancelButton = document.getElementById('signalLinkCancelBtn');
    if (startButton) startButton.addEventListener('click', start);
    if (cancelButton) cancelButton.addEventListener('click', cancel);
  });
  return { refresh: refresh, close: close };
})();
