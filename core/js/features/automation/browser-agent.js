// browser-agent.js
// Eva's vision browser agent: frontend controller + floating, Eva-themed popup.
//
// Talks to the ACP bridge endpoints:
//   POST /v1/browser/run        -> { id, status, ... }
//   GET  /v1/browser/status     -> run status snapshot
//   GET  /v1/browser/screenshot -> latest PNG for the run
//   POST /v1/browser/confirm    -> approve/answer a parked run
//   POST /v1/browser/cancel     -> stop a run
//
// Public API (window.EvaBrowser):
//   launch(goal, opts)  -> starts a run and opens the popup
//   isActive()          -> true while a run is being tracked
//
// Designed to be extensible: the popup is a generic "Eva activity window" we can
// reuse for future visual tasks. Keep new run types flowing through launch().

(function (global) {
  'use strict';

  var POLL_MS = 1200;
  var _state = {
    runId: null,
    starting: false,
    generation: 0,
    cancelRequested: false,
    cancelPromise: null,
    closeAfterCancel: false,
    poll: null,
    status: null,
    shotTick: 0,
    endpoint: '/v1/browser',   // bridge path prefix for the active run type
    title: 'Browser Agent',
    onComplete: null,          // fired once when a run reaches a terminal state
    completed: false,
    onConfirm: null,           // fired when the agent parks for confirmation/input
    confirmKey: null,          // de-dupes the confirm callback per park
    onProgress: null,          // fired when an executed receipt changes
    lastProgress: null         // last executed receipt, to avoid repeats
  };

  // --- Bridge helpers -------------------------------------------------------

  function bridgeBase() {
    if (typeof getSafeBridgeBaseUrl === 'function') return getSafeBridgeBaseUrl();
    return 'http://localhost:8888';
  }

  function openaiKey() {
    if (typeof getAuthKey === 'function') return getAuthKey('OPENAI_API_KEY') || '';
    return (global.OPENAI_API_KEY || '');
  }

  function setChatStatus(type, text) {
    if (typeof setStatus === 'function') setStatus(type, text);
  }

  function isTerminal(status) {
    return status === 'done' || status === 'blocked' || status === 'cancelled' || status === 'error';
  }

  function isLiveStatus(status) {
    return !!status && !isTerminal(status.status);
  }

  function removePopup() {
    stopPolling();
    var el = document.getElementById('evaBrowserPopup');
    if (el) el.remove();
    _state.runId = null;
    _state.starting = false;
    _state.status = null;
    _state.cancelRequested = false;
    _state.cancelPromise = null;
    _state.closeAfterCancel = false;
    _state.confirmKey = null;
  }

  // --- Popup construction ---------------------------------------------------

  function ensurePopup() {
    var el = document.getElementById('evaBrowserPopup');
    if (el) return el;

    el = document.createElement('div');
    el.id = 'evaBrowserPopup';
    el.className = 'eva-browser-popup';
    el.setAttribute('role', 'dialog');
    el.setAttribute('aria-label', 'Eva browser agent');
    el.innerHTML = [
      '<div class="ebp-titlebar" id="ebpTitlebar">',
      '  <span class="ebp-dot"></span>',
      '  <span class="ebp-title">Eva &middot; Browser Agent</span>',
      '  <span class="ebp-step" id="ebpStep"></span>',
      '  <button class="ebp-close" id="ebpClose" type="button" aria-label="Close">&times;</button>',
      '</div>',
      '<div class="ebp-goal" id="ebpGoal"></div>',
      '<div class="ebp-stage">',
      '  <img class="ebp-shot" id="ebpShot" alt="Browser view" />',
      '  <div class="ebp-shot-empty" id="ebpShotEmpty">Waiting for the first screenshot&hellip;</div>',
      '</div>',
      '<div class="ebp-subgoal" id="ebpSubgoal"></div>',
      '<div class="ebp-statusrow">',
      '  <span class="ebp-badge" id="ebpBadge">starting</span>',
      '  <span class="ebp-executor" id="ebpExecutor"></span>',
      '  <span class="ebp-url" id="ebpUrl"></span>',
      '</div>',
      '<div class="ebp-prompt" id="ebpPrompt" hidden>',
      '  <div class="ebp-prompt-text" id="ebpPromptText"></div>',
      '  <input class="ebp-input" id="ebpInput" type="text" placeholder="Type your answer&hellip;" hidden />',
      '  <div class="ebp-prompt-actions" id="ebpPromptActions"></div>',
      '</div>',
      '<div class="ebp-footer">',
      '  <button class="ebp-btn ebp-stop" id="ebpStop" type="button">Stop</button>',
      '</div>'
    ].join('');

    document.body.appendChild(el);

    document.getElementById('ebpClose').addEventListener('click', closePopup);
    document.getElementById('ebpStop').addEventListener('click', stopRun);
    document.getElementById('ebpShot').addEventListener('error', function () {
      this.style.visibility = 'hidden';
      var empty = document.getElementById('ebpShotEmpty');
      if (empty) empty.hidden = false;
    });
    document.getElementById('ebpShot').addEventListener('load', function () {
      this.style.visibility = 'visible';
      var empty = document.getElementById('ebpShotEmpty');
      if (empty) empty.hidden = true;
    });

    makeDraggable(el, document.getElementById('ebpTitlebar'));
    return el;
  }

  function makeDraggable(panel, handle) {
    var ox = 0, oy = 0, dragging = false;
    handle.addEventListener('mousedown', function (e) {
      if (e.target && e.target.id === 'ebpClose') return;
      dragging = true;
      var rect = panel.getBoundingClientRect();
      ox = e.clientX - rect.left;
      oy = e.clientY - rect.top;
      panel.style.right = 'auto';
      panel.style.bottom = 'auto';
      document.body.style.userSelect = 'none';
    });
    document.addEventListener('mousemove', function (e) {
      if (!dragging) return;
      var x = Math.max(0, Math.min(window.innerWidth - 80, e.clientX - ox));
      var y = Math.max(0, Math.min(window.innerHeight - 40, e.clientY - oy));
      panel.style.left = x + 'px';
      panel.style.top = y + 'px';
    });
    document.addEventListener('mouseup', function () {
      dragging = false;
      document.body.style.userSelect = '';
    });
  }

  function closePopup() {
    if (isActive()) {
      _state.closeAfterCancel = true;
      stopRun();
      return;
    }
    removePopup();
  }

  // --- Rendering ------------------------------------------------------------

  var BADGE_LABELS = {
    starting: 'starting',
    running: 'working',
    awaiting_confirmation: 'needs approval',
    awaiting_input: 'needs input',
    done: 'done',
    blocked: 'blocked',
    cancelled: 'stopped',
    error: 'error'
  };

  function neutralActionLabel(action) {
    var value = String(action || '').toLowerCase();
    if (/\b(click|tap|mouse)\b/.test(value)) return 'Click';
    if (/\b(type|write|input|fill)\b/.test(value)) return 'Type';
    if (/\b(scroll|wheel)\b/.test(value)) return 'Scroll';
    if (/\b(navigate|open|visit|url)\b/.test(value)) return 'Navigate';
    if (/\b(select|choose|pick)\b/.test(value)) return 'Select';
    if (/\b(key|press|hotkey)\b/.test(value)) return 'Press key';
    if (/\b(drag|drop|move)\b/.test(value)) return 'Move';
    if (/\b(wait|sleep)\b/.test(value)) return 'Wait';
    if (/\b(read|inspect|observe|look|screen|screenshot)\b/.test(value)) return 'Read screen';
    return 'Action';
  }

  function executedReceipt(status) {
    var steps = status && Array.isArray(status.steps) ? status.steps : [];
    for (var index = steps.length - 1; index >= 0; index--) {
      var step = steps[index] || {};
      if (!step.action || step.result == null) continue;
      var resultText = typeof step.result === 'string' ? step.result : JSON.stringify(step.result);
      var failed = !!step.error || /\b(error|failed|failure|unable|could not)\b/i.test(resultText || '');
      return {
        key: String(step.id || step.step || index) + ':' + String(step.action) + ':' + resultText,
        action: neutralActionLabel(step.action),
        outcome: failed ? 'error' : 'success'
      };
    }
    return null;
  }

  function executorLabel(status) {
    if (!status) return '';
    var model = String(status.executor_model || '').trim();
    var provider = String(status.executor_provider || '').trim();
    if (!model && !provider) return '';
    if (model && provider) return model + ' / ' + provider;
    return model || provider;
  }

  function render(status) {
    if (!status) return;
    _state.status = status;

    // Always evaluate the confirmation gate so a parked run prompts Eva to ask
    // naturally, regardless of which surface (popup or embedded panel) is shown.
    maybeFireConfirm(status);
    // Narrate progress only when the latest executed receipt changes.
    maybeFireProgress(status);

    // In the fullscreen voice view, render into the faint embedded panel on
    // Eva's right ("her thoughts") instead of the floating popup.
    if (_embeddedOpen()) {
      // Only remove the floating popup ELEMENT; do NOT call closePopup() here,
      // because closePopup() stops polling and nulls runId, which would kill the
      // run's status updates after the first poll (no more screenshots and the
      // completion hook never fires).
      var _pop = document.getElementById('evaBrowserPopup');
      if (_pop) _pop.remove();
      renderEmbedded(status);
      return;
    }

    ensurePopup();

    var goalEl = document.getElementById('ebpGoal');
    if (goalEl) goalEl.textContent = status.goal || '';

    var stepEl = document.getElementById('ebpStep');
    if (stepEl) stepEl.textContent = status.step != null ? ('step ' + status.step) : '';

    var subEl = document.getElementById('ebpSubgoal');
    var popupReceipt = executedReceipt(status);
    if (subEl) subEl.textContent = popupReceipt ? (popupReceipt.action + ': ' + popupReceipt.outcome) : '';

    var executorEl = document.getElementById('ebpExecutor');
    if (executorEl) executorEl.textContent = executorLabel(status);

    var urlEl = document.getElementById('ebpUrl');
    if (urlEl) urlEl.textContent = status.title || status.url || status.active_app || status.screen || '';

    var badge = document.getElementById('ebpBadge');
    if (badge) {
      badge.textContent = BADGE_LABELS[status.status] || status.status;
      badge.setAttribute('data-state', status.status);
    }

    refreshShot(status);
    renderPrompt(status);
    renderFooter(status);
  }

  // True when Eva's fullscreen voice view is open (render into the embedded
  // panel instead of the floating popup).
  function _embeddedOpen() {
    return (typeof _vv !== 'undefined' && _vv && _vv.open);
  }

  // Render the agent's live screenshot + status into the faint right-side panel
  // (#vvVision), the same surface the camera "look" uses, so it reads as Eva's
  // thoughts rather than a separate window.
  function renderEmbedded(status) {
    var panel = document.getElementById('vvVision');
    if (!panel) return;
    var terminal = isTerminal(status.status);
    panel.classList.add('open');
    panel.setAttribute('aria-hidden', 'false');
    if (terminal) panel.classList.remove('looking'); else panel.classList.add('looking');

    var img = document.getElementById('vvVisionShot');
    // The camera "look" shares this image element. On the FIRST embedded render
    // of a run, drop any leftover frame (e.g. the webcam image from a prior
    // look) so the panel never shows a stale picture. Do NOT hide the element
    // via visibility here: the camera path (which works) never toggles
    // visibility, and relying on an onload handler to restore it can leave the
    // panel as an empty outline box if onload does not fire.
    if (img && _state._embedRunId !== status.id) {
      _state._embedRunId = status.id;
      _state._embedShotKey = null;
      try { img.removeAttribute('src'); } catch (e) {}
    }

    var live = (status.status === 'running' || status.status === 'starting' ||
                status.status === 'awaiting_confirmation' || status.status === 'awaiting_input' ||
                status.status === 'done');
    if (img && live && status.id) {
      // Load the screenshot via fetch -> data URL, EXACTLY like the camera frame
      // (a direct http <img src> is blocked from a file:// page under Electron
      // webSecurity; fetch + FileReader sidesteps it).
      var stepKey = String(status.step != null ? status.step : '') + ':' + status.id;
      if (_state._embedShotKey !== stepKey) {
        _state._embedShotKey = stepKey;
        var url = bridgeBase() + _state.endpoint + '/screenshot?run_id=' +
                  encodeURIComponent(status.id) + '&t=' + (_state.shotTick++);
        _loadShotInto(img, url, stepKey);
      }
    }
    var txt = document.getElementById('vvVisionText');
    if (txt) {
      var line = '[' + (_state.title || 'Agent') + ']';
      var embeddedExecutor = executorLabel(status);
      if (embeddedExecutor) line += '\n' + embeddedExecutor;
      var embeddedReceipt = executedReceipt(status);
      if (embeddedReceipt) line += '\n' + embeddedReceipt.action + ': ' + embeddedReceipt.outcome;
      else if (status.url || status.title) line += '\n' + (status.title || status.url);
      if (terminal && status.result) line += '\n' + status.result;
      else if (terminal && status.error) line += '\nError: ' + status.error;
      txt.textContent = line;
    }
    if (terminal) {
      // Fade the panel out shortly after the run ends.
      if (_state._fadeTimer) clearTimeout(_state._fadeTimer);
      _state._fadeTimer = setTimeout(function () {
        var p = document.getElementById('vvVision');
        if (p) { p.classList.remove('open', 'looking'); p.setAttribute('aria-hidden', 'true'); }
      }, 8000);
    }
  }

  // Build a natural, spoken-style question for a parked confirmation/input.
  function _buildConfirmQuestion(status) {
    if (status.status === 'awaiting_input') {
      return status.pending_question || 'I need a bit more information to continue. What should I do?';
    }
    return 'I\'m ready to continue with the requested action. Do you want me to approve it? Say yes to continue or no to stop.';
  }

  // Fire the confirmation callback once per park so Eva asks in chat/voice
  // instead of relying on a popup button. The caller wires onConfirm to render
  // and speak the question and to arm interception of the next user reply.
  function maybeFireConfirm(status) {
    var parked = (status.status === 'awaiting_confirmation' || status.status === 'awaiting_input');
    if (!parked) { _state.confirmKey = null; return; }
    var key = status.status + ':' + (status.step != null ? status.step : '') + ':' + (status.id || '');
    if (_state.confirmKey === key) return;   // already asked for this park
    _state.confirmKey = key;
    var question = _buildConfirmQuestion(status);
    var needsText = (status.status === 'awaiting_input');
    if (typeof _state.onConfirm === 'function') {
      try { _state.onConfirm(question, needsText, status); } catch (e) {}
    }
  }

  // Narrate only executed receipts. Planner text can contain imperatives,
  // reasons, or user-provided text and is not a trusted activity signal.
  function maybeFireProgress(status) {
    if (typeof _state.onProgress !== 'function') return;
    var receipt = executedReceipt(status);
    if (!receipt) return;
    // Skip while parked (the confirm/ask already speaks) or terminal.
    if (status.status === 'awaiting_confirmation' || status.status === 'awaiting_input') return;
    if (isTerminal(status.status)) return;
    if (receipt.key === _state.lastProgress) return;
    _state.lastProgress = receipt.key;
    try { _state.onProgress(receipt, status); } catch (e) {}
  }

  function refreshShot(status) {
    // Refresh the screenshot whenever the step advances or we are mid-run.
    var img = document.getElementById('ebpShot');
    if (!img) return;
    var live = (status.status === 'running' || status.status === 'starting' ||
                status.status === 'awaiting_confirmation' || status.status === 'awaiting_input' ||
                status.status === 'done');
    if (!live) return;
    var pkey = String(status.step != null ? status.step : '') + ':' + status.id;
    if (_state._popupShotKey === pkey) return;
    _state._popupShotKey = pkey;
    var url = bridgeBase() + _state.endpoint + '/screenshot?run_id=' +
              encodeURIComponent(status.id) + '&t=' + (_state.shotTick++);
    _loadShotInto(img, url);
  }

  // Fetch a screenshot and display it as a data URL (exactly how the camera
  // frame displays, which is known to work). A direct http <img src> is blocked
  // from a file:// page under Electron webSecurity; fetch + FileReader sidesteps
  // it. On failure, clear the relevant cache key so the next poll re-fetches
  // instead of leaving a stale or blank image up.
  function _loadShotInto(img, url, retryKey) {
    if (!img) return;
    fetch(url, { cache: 'no-store' }).then(function (r) {
      if (!r.ok) throw new Error('shot ' + r.status);
      return r.blob();
    }).then(function (blob) {
      return new Promise(function (resolve, reject) {
        var fr = new FileReader();
        fr.onload = function () { resolve(fr.result); };
        fr.onerror = function () { reject(new Error('read failed')); };
        fr.readAsDataURL(blob);
      });
    }).then(function (dataUrl) {
      // Set src directly and force visibility (no reliance on onload), matching
      // the camera path so the element can never get stuck hidden.
      img.style.visibility = 'visible';
      img.src = dataUrl;
    }).catch(function () {
      if (retryKey && _state._embedShotKey === retryKey) _state._embedShotKey = null;
      if (!retryKey && _state._popupShotKey) _state._popupShotKey = null;
    });
  }

  function renderPrompt(status) {
    // Confirmations and input requests are now handled by Eva asking naturally
    // in chat/voice (see maybeFireConfirm + the interception in options.js), so
    // the popup no longer shows Approve/Decline/Send buttons. Keep the prompt
    // area hidden; the chat reply drives the decision.
    var wrap = document.getElementById('ebpPrompt');
    var input = document.getElementById('ebpInput');
    var textEl = document.getElementById('ebpPromptText');
    var actions = document.getElementById('ebpPromptActions');
    if (actions) actions.innerHTML = '';
    if (wrap) wrap.hidden = true;
    if (input) input.hidden = true;
    if (textEl) textEl.textContent = '';
  }

  function renderFooter(status) {
    var stop = document.getElementById('ebpStop');
    if (!stop) return;
    var terminal = isTerminal(status.status);
    if (terminal) {
      stop.textContent = 'Close';
      stop.classList.add('ebp-done');
      stop.onclick = closePopup;
      if (status.result) {
        var sub = document.getElementById('ebpSubgoal');
        if (sub) sub.textContent = status.result;
      } else if (status.error) {
        var subE = document.getElementById('ebpSubgoal');
        if (subE) subE.textContent = 'Error: ' + status.error;
      }
    } else {
      stop.textContent = 'Stop';
      stop.classList.remove('ebp-done');
      stop.onclick = stopRun;
    }
  }

  function addBtn(parent, label, cls, fn) {
    var b = document.createElement('button');
    b.type = 'button';
    b.className = 'ebp-btn ' + cls;
    b.textContent = label;
    b.addEventListener('click', fn);
    parent.appendChild(b);
  }

  // --- Network actions ------------------------------------------------------

  function buildRunPayload(goal, opts) {
    opts = opts || {};
    var backend = Object.prototype.hasOwnProperty.call(opts, 'backend')
      ? String(opts.backend || '').trim()
      : String((document.getElementById('selAIGBackend') || {}).value || '').trim();
    var body = {
      goal: String(goal || '').trim(),
      autonomy: opts.autonomy || 'pause',
      use_director: opts.use_director !== false
    };
    if (backend) body.backend = backend;
    var effort = Object.prototype.hasOwnProperty.call(opts, 'reasoning_effort')
      ? opts.reasoning_effort
      : (typeof getReasoningEffort === 'function' ? getReasoningEffort() : '');
    if (effort) body.reasoning_effort = effort;
    if (opts.start_url) body.start_url = opts.start_url;
    if (opts.max_steps) body.max_steps = opts.max_steps;

    var legacyVisionModel = String(opts.vision_model || '').trim();
    var directBackend = backend === 'openai' || backend.indexOf('openai:') === 0;
    if (!backend && legacyVisionModel) body.vision_model = legacyVisionModel;
    if (directBackend || (!backend && legacyVisionModel)) {
      var key = openaiKey();
      if (key) body.openai_api_key = key;
    }
    return body;
  }

  async function launch(goal, opts) {
    opts = opts || {};
    goal = (goal || '').trim();
    if (isActive()) {
      setChatStatus('error', (_state.title || 'Agent') + ' is already active. Stop it before starting another task.');
      return false;
    }
    removePopup();
    var generation = ++_state.generation;
    _state.endpoint = opts.endpoint || '/v1/browser';
    _state.title = opts.title || 'Browser Agent';
    _state.onComplete = (typeof opts.onComplete === 'function') ? opts.onComplete : null;
    _state.onConfirm = (typeof opts.onConfirm === 'function') ? opts.onConfirm : null;
    _state.onProgress = (typeof opts.onProgress === 'function') ? opts.onProgress : null;
    _state.lastProgress = null;
    _state.confirmKey = null;
    _state.completed = false;
    _state.starting = true;
    _state.cancelRequested = false;
    _state.cancelPromise = null;
    _state.closeAfterCancel = false;
    if (!goal) {
      _state.starting = false;
      setChatStatus('error', _state.title + ': no goal provided.');
      return false;
    }

    ensurePopup();
    _applyTitle();
    render({ id: '', goal: goal, status: 'starting', step: 0 });

    var body = buildRunPayload(goal, opts);

    try {
      var resp = await fetch(bridgeBase() + _state.endpoint + '/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      });
      var data = await resp.json();
      if (generation !== _state.generation) return false;
      if (!resp.ok) {
        _state.starting = false;
        var msg = (data && data.error && data.error.message) || ('HTTP ' + resp.status);
        setChatStatus('error', _state.title + ': ' + msg);
        render({ id: '', goal: goal, status: 'error', error: msg });
        return false;
      }
      _state.runId = data.id;
      _state.starting = false;
      render(data);
      startPolling(data.id, generation);
      if (_state.cancelRequested) stopRun();
      setChatStatus('info', _state.title + ' started.');
      return true;
    } catch (e) {
      if (generation !== _state.generation) return false;
      _state.starting = false;
      setChatStatus('error', _state.title + ' could not reach the bridge.');
      render({ id: '', goal: goal, status: 'error', error: String(e) });
      return false;
    }
  }

  function _applyTitle() {
    var t = document.querySelector('#evaBrowserPopup .ebp-title');
    if (t) t.innerHTML = 'Eva &middot; ' + _state.title;
  }

  function startPolling(runId, generation) {
    stopPolling();
    _state.poll = setInterval(function () { pollOnce(runId, generation); }, POLL_MS);
    pollOnce(runId, generation);
  }

  function stopPolling() {
    if (_state.poll) {
      clearInterval(_state.poll);
      _state.poll = null;
    }
  }

  async function pollOnce(runId, generation) {
    runId = runId || _state.runId;
    generation = generation == null ? _state.generation : generation;
    if (!runId || generation !== _state.generation || runId !== _state.runId) return;
    try {
      var resp = await fetch(bridgeBase() + _state.endpoint + '/status?run_id=' +
        encodeURIComponent(runId), { signal: AbortSignal.timeout(8000) });
      if (generation !== _state.generation || runId !== _state.runId) return;
      if (!resp.ok) {
        // Track consecutive failures. After 3 404s the run_id is stale
        // (bridge restarted or run expired). Stop spamming the console.
        _state._pollFails = (_state._pollFails || 0) + 1;
        if (resp.status === 404 && _state._pollFails >= 3) {
          console.warn('[Agent] Run ' + runId + ' no longer exists, stopping poll');
          stopPolling();
          _state.runId = null;
          _state.status = { status: 'error', error: 'The automation run is no longer available.' };
        }
        return;
      }
      _state._pollFails = 0;
      var status = await resp.json();
      if (generation !== _state.generation || runId !== _state.runId) return;
      if (status.id && status.id !== runId) return;
      render(status);
      if (isTerminal(status.status)) {
        stopPolling();
        _state.starting = false;
        _state.confirmKey = null;
        // Fire the completion hook exactly once so the caller (Eva) can become
        // aware of the actual outcome and acknowledge it.
        if (!_state.completed) {
          _state.completed = true;
          if (typeof _state.onComplete === 'function') {
            try { _state.onComplete(status, _state.endpoint, _state.title); } catch (e) {}
          }
        }
        if (_state.closeAfterCancel) {
          setTimeout(removePopup, 0);
        }
      }
    } catch (e) {
      // transient; keep polling
    }
  }

  async function confirmRun(approve, text) {
    if (!_state.runId || _state.cancelRequested || !isLiveStatus(_state.status)) return false;
    var runId = _state.runId;
    var generation = _state.generation;
    try {
      var resp = await fetch(bridgeBase() + _state.endpoint + '/confirm', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ run_id: runId, approve: approve, text: text || '' })
      });
      if (generation !== _state.generation || runId !== _state.runId) return false;
      if (!resp.ok) {
        var data = await resp.json().catch(function () { return {}; });
        var msg = (data && data.error && data.error.message) || ('HTTP ' + resp.status);
        _state.confirmKey = null;
        setChatStatus('error', _state.title + ': confirmation failed: ' + msg);
        pollOnce(runId, generation);
        return false;
      }
      // optimistic: hide the prompt until next poll
      var wrap = document.getElementById('ebpPrompt');
      if (wrap) wrap.hidden = true;
      pollOnce(runId, generation);
      return true;
    } catch (e) {
      if (generation !== _state.generation || runId !== _state.runId) return false;
      _state.confirmKey = null;
      setChatStatus('error', 'Browser agent: could not send confirmation.');
      return false;
    }
  }

  async function stopRun() {
    if (!_state.runId) {
      if (_state.starting) {
        _state.cancelRequested = true;
        setChatStatus('info', _state.title + ': stopping the task.');
        return false;
      }
      removePopup();
      return true;
    }
    if (_state.cancelPromise) return _state.cancelPromise;
    var runId = _state.runId;
    var generation = _state.generation;
    _state.cancelRequested = true;
    _state.cancelPromise = (async function () {
    try {
      var resp = await fetch(bridgeBase() + _state.endpoint + '/cancel', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ run_id: runId })
      });
      if (generation !== _state.generation || runId !== _state.runId) return false;
      if (!resp.ok) {
        var data = await resp.json().catch(function () { return {}; });
        var msg = (data && data.error && data.error.message) || ('HTTP ' + resp.status);
        _state.cancelRequested = false;
        setChatStatus('error', _state.title + ': could not stop the task: ' + msg);
        return false;
      }
      pollOnce(runId, generation);
      return true;
    } catch (e) {
      if (generation !== _state.generation || runId !== _state.runId) return false;
      _state.cancelRequested = false;
      setChatStatus('error', _state.title + ': could not stop the task.');
      return false;
    }
    })();
    try { return await _state.cancelPromise; } finally {
      if (generation === _state.generation && runId === _state.runId) _state.cancelPromise = null;
    }
  }

  function isActive() {
    return !!(_state.starting || (_state.runId && isLiveStatus(_state.status)));
  }

  // True when the active run is parked waiting for a confirmation or input that
  // Eva asked about in chat/voice. The next user reply should answer it.
  function isAwaitingConfirm() {
    return !!(_state.runId && _state.status &&
      (_state.status.status === 'awaiting_confirmation' || _state.status.status === 'awaiting_input'));
  }

  // Answer a parked confirmation. approve=true continues or submits input;
  // approve=false declines. text carries free-form input when requested.
  function answerConfirm(approve, text) {
    return confirmRun(!!approve, text || '');
  }

  global.EvaBrowser = {
    launch: launch,
    buildRunPayload: buildRunPayload,
    isActive: isActive,
    isAwaitingConfirm: isAwaitingConfirm,
    answerConfirm: answerConfirm,
    stop: stopRun,
    close: closePopup
  };

  // Desktop ('computer use') agent reuses the same popup + controller, pointed
  // at the bridge's /v1/desktop endpoints.
  global.EvaDesktop = {
    launch: function (goal, opts) {
      opts = opts || {};
      opts.endpoint = '/v1/desktop';
      opts.title = opts.title || 'Desktop Agent';
      return launch(goal, opts);
    },
    isActive: isActive,
    isAwaitingConfirm: isAwaitingConfirm,
    answerConfirm: answerConfirm,
    stop: stopRun,
    close: closePopup
  };

})(typeof window !== 'undefined' ? window : this);
