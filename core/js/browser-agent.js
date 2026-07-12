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
    poll: null,
    status: null,
    shotTick: 0,
    base: '',                  // immutable bridge base for the owned run
    endpoint: '/v1/browser',   // bridge path prefix for the active run type
    title: 'Browser Agent',
    onComplete: null,          // fired once when a run reaches a terminal state
    completed: false,
    onConfirm: null,           // fired when the agent parks for confirmation/input
    confirmKey: null,          // de-dupes the confirm callback per park
    onProgress: null,          // fired when the agent's plan/subgoal changes
    lastProgress: null,        // last subgoal narrated, to avoid repeats
    generation: 0,
    launching: false
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
    cancelActiveRun();
  }

  // --- Rendering ------------------------------------------------------------

  var BADGE_LABELS = {
    starting: 'starting',
    running: 'working',
    awaiting_confirmation: 'needs approval',
    awaiting_input: 'needs input',
    done: 'done',
    cancelled: 'stopped',
    error: 'error'
  };

  function render(status) {
    if (!status) return;
    _state.status = status;

    // Always evaluate the confirmation gate so a parked run prompts Eva to ask
    // naturally, regardless of which surface (popup or embedded panel) is shown.
    maybeFireConfirm(status);
    // Narrate progress when the plan/subgoal meaningfully changes.
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
    if (subEl) subEl.textContent = status.subgoal ? ('Plan: ' + status.subgoal) : '';

    var urlEl = document.getElementById('ebpUrl');
    if (urlEl) urlEl.textContent = status.title || status.url || status.active_app || status.screen || '';

    var badge = document.getElementById('ebpBadge');
    if (badge) {
      var outcomeState = (typeof EvaActionOutcomes !== 'undefined')
        ? EvaActionOutcomes.displayState(status)
        : (status.outcome && status.outcome.state);
      var label = outcomeState === 'succeeded' ? 'verified'
        : outcomeState === 'indeterminate' ? 'unverified'
        : outcomeState === 'aborted' ? 'stopped'
        : outcomeState === 'failed' ? 'failed'
        : (BADGE_LABELS[status.status] || status.status);
      badge.textContent = label;
      badge.setAttribute('data-state', outcomeState || status.status);
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
    var terminal = (status.status === 'done' || status.status === 'cancelled' || status.status === 'error');
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
        var url = (_state.base || bridgeBase()) + _state.endpoint + '/screenshot?run_id=' +
                  encodeURIComponent(status.id) + '&t=' + (_state.shotTick++);
        _loadShotInto(img, url, stepKey);
      }
    }
    var txt = document.getElementById('vvVisionText');
    if (txt) {
      var line = '[' + (_state.title || 'Agent') + ']';
      if (status.subgoal) line += '\n' + status.subgoal;
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
    var request = status.approval_request || {};
    if (request.kind === 'input') {
      return request.description || 'I need a bit more information to continue. What should I do?';
    }
    var description = request.description || 'Exact effect details are unavailable.';
    return description + '\nApprove only this exact effect? Say yes to continue or no to stop.';
  }

  // Fire the confirmation callback once per park so Eva asks in chat/voice
  // instead of relying on a popup button. The caller wires onConfirm to render
  // and speak the question and to arm interception of the next user reply.
  function maybeFireConfirm(status) {
    var parked = (status.status === 'awaiting_confirmation' || status.status === 'awaiting_input');
    if (!parked) { _state.confirmKey = null; return; }
    var gate = status.approval_request || {};
    var key = gate.gate_id || (status.status + ':' + (status.step != null ? status.step : '') + ':' + (status.id || ''));
    if (_state.confirmKey === key) return;   // already asked for this park
    _state.confirmKey = key;
    var question = _buildConfirmQuestion(status);
    var needsText = (gate.kind === 'input');
    if (typeof _state.onConfirm === 'function') {
      try { _state.onConfirm(question, needsText, status); } catch (e) {}
    }
  }

  // Narrate the agent's plan when it changes, so the user hears progress and
  // knows Eva is working rather than stuck. Throttled to meaningful changes
  // (the director sets a new subgoal every few steps).
  function maybeFireProgress(status) {
    if (typeof _state.onProgress !== 'function') return;
    var sub = (status && status.subgoal) ? String(status.subgoal).trim() : '';
    if (!sub) return;
    // Skip while parked (the confirm/ask already speaks) or terminal.
    if (status.status === 'awaiting_confirmation' || status.status === 'awaiting_input') return;
    if (status.status === 'done' || status.status === 'cancelled' || status.status === 'error') return;
    if (sub === _state.lastProgress) return;
    _state.lastProgress = sub;
    try { _state.onProgress(sub, status); } catch (e) {}
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
    var url = (_state.base || bridgeBase()) + _state.endpoint + '/screenshot?run_id=' +
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
    var shotGeneration = _state.generation;
    var shotRunId = _state.runId;
    var shotKey = retryKey || _state._popupShotKey;
    function shotIsCurrent() {
      if (shotGeneration !== _state.generation || shotRunId !== _state.runId) return false;
      return retryKey
        ? _state._embedShotKey === shotKey
        : _state._popupShotKey === shotKey;
    }
    fetch(url, { cache: 'no-store' }).then(function (r) {
      if (!shotIsCurrent()) throw new Error('stale shot');
      if (!r.ok) throw new Error('shot ' + r.status);
      return r.blob();
    }).then(function (blob) {
      if (!shotIsCurrent()) throw new Error('stale shot');
      return new Promise(function (resolve, reject) {
        var fr = new FileReader();
        fr.onload = function () {
          if (!shotIsCurrent()) { reject(new Error('stale shot')); return; }
          resolve(fr.result);
        };
        fr.onerror = function () { reject(new Error('read failed')); };
        fr.readAsDataURL(blob);
      });
    }).then(function (dataUrl) {
      if (!shotIsCurrent()) return;
      // Set src directly and force visibility (no reliance on onload), matching
      // the camera path so the element can never get stuck hidden.
      img.style.visibility = 'visible';
      img.src = dataUrl;
    }).catch(function () {
      if (!shotIsCurrent()) return;
      if (retryKey && _state._embedShotKey === shotKey) _state._embedShotKey = null;
      if (!retryKey && _state._popupShotKey === shotKey) _state._popupShotKey = null;
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
    var terminal = (status.status === 'done' || status.status === 'cancelled' || status.status === 'error');
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

  async function launch(goal, opts) {
    if (_state.launching) {
      setChatStatus('error', 'Another agent launch is already awaiting authorization.');
      return false;
    }
    _state.launching = true;
    try {
      return await _launchReserved(goal, opts);
    } finally {
      _state.launching = false;
    }
  }

  async function _launchReserved(goal, opts) {
    opts = opts || {};
    goal = (goal || '').trim();
    if (_state.runId && !(await _stopAndWaitActiveRun())) {
      setChatStatus('error', 'The prior agent run is still finishing an action. Try again after it stops.');
      return;
    }
    await cancelActiveRun();
    var launchGeneration = _state.generation;
    var launchBase = bridgeBase();
    var launchEndpoint = opts.endpoint || '/v1/browser';
    _state.base = launchBase;
    _state.endpoint = launchEndpoint;
    _state.title = opts.title || 'Browser Agent';
    _state.onComplete = (typeof opts.onComplete === 'function') ? opts.onComplete : null;
    _state.onConfirm = (typeof opts.onConfirm === 'function') ? opts.onConfirm : null;
    _state.onProgress = (typeof opts.onProgress === 'function') ? opts.onProgress : null;
    _state.lastProgress = null;
    _state.confirmKey = null;
    _state.completed = false;
    if (!goal) {
      setChatStatus('error', _state.title + ': no goal provided.');
      return;
    }
    var key = openaiKey();
    if (!key) {
      setChatStatus('error', _state.title + ' needs an OpenAI key (Settings > Auth).');
      return;
    }

    ensurePopup();
    _applyTitle();
    render({ id: '', goal: goal, status: 'starting', step: 0 });

    var specification = {
      goal: goal,
      autonomy: opts.autonomy || 'pause',
      use_director: opts.use_director !== false
    };
    if (opts.start_url) specification.start_url = opts.start_url;
    if (opts.vision_model) specification.vision_model = opts.vision_model;
    if (opts.max_steps !== undefined) specification.max_steps = opts.max_steps;
    if (opts.postcondition) specification.postcondition = opts.postcondition;
    if (opts.headless !== undefined) specification.headless = opts.headless === true;

    var standalone = global.evaStandalone;
    if (!standalone || typeof standalone.authorizeAgentLaunch !== 'function') {
      setChatStatus('error', _state.title + ' requires trusted standalone launch authorization.');
      render({ id: '', goal: goal, status: 'error', error: 'Trusted launch authorization unavailable.' });
      return;
    }
    var authorization;
    try {
      authorization = await standalone.authorizeAgentLaunch(
        launchEndpoint === '/v1/desktop' ? 'desktop' : 'browser', specification
      );
    } catch (_) {
      authorization = null;
    }
    if (
      launchGeneration !== _state.generation
      || !authorization || authorization.authorized !== true
      || typeof authorization.capability !== 'string'
      || !authorization.specification
      || typeof authorization.specification !== 'object'
    ) {
      setChatStatus('info', _state.title + ' launch was not authorized.');
      render({
        id: '', goal: goal, status: 'cancelled',
        outcome: { state: 'aborted', reason: 'launch_not_authorized' },
        result: 'The user did not authorize this agent run.'
      });
      return;
    }
    var body = Object.assign({}, authorization.specification, {
      openai_api_key: key,
      launch_capability: authorization.capability
    });

    try {
      var resp = await fetch(launchBase + launchEndpoint + '/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      });
      var data = await resp.json();
      if (launchGeneration !== _state.generation) {
        if (resp.ok && data && data.id) {
          _cancelRemoteRun(launchBase, launchEndpoint, data.id);
        }
        return;
      }
      if (!resp.ok) {
        var msg = (data && data.error && data.error.message) || ('HTTP ' + resp.status);
        setChatStatus('error', _state.title + ': ' + msg);
        render({ id: '', goal: goal, status: 'error', error: msg });
        return;
      }
      _state.runId = data.id;
      render(data);
      startPolling();
      setChatStatus('info', _state.title + ' started.');
    } catch (e) {
      if (launchGeneration !== _state.generation) return;
      setChatStatus('error', _state.title + ' could not reach the bridge.');
      render({ id: '', goal: goal, status: 'error', error: String(e) });
    }
  }

  function _applyTitle() {
    var t = document.querySelector('#evaBrowserPopup .ebp-title');
    if (t) t.innerHTML = 'Eva &middot; ' + _state.title;
  }

  function startPolling() {
    stopPolling();
    _state.poll = setInterval(pollOnce, POLL_MS);
    pollOnce();
  }

  function stopPolling() {
    if (_state.poll) {
      clearInterval(_state.poll);
      _state.poll = null;
    }
  }

  async function pollOnce() {
    if (!_state.runId) return;
    var pollGeneration = _state.generation;
    var pollRunId = _state.runId;
    var pollBase = _state.base;
    var pollEndpoint = _state.endpoint;
    try {
      var resp = await fetch(pollBase + pollEndpoint + '/status?run_id=' +
        encodeURIComponent(pollRunId), { signal: AbortSignal.timeout(8000) });
      if (pollGeneration !== _state.generation || pollRunId !== _state.runId) return;
      if (!resp.ok) {
        // Track consecutive failures. After 3 404s the run_id is stale
        // (bridge restarted or run expired). Stop spamming the console.
        _state._pollFails = (_state._pollFails || 0) + 1;
        if (resp.status === 404 && _state._pollFails >= 3) {
          console.warn('[Agent] Run ' + _state.runId + ' no longer exists, stopping poll');
          stopPolling();
          _state.runId = null;
        }
        return;
      }
      _state._pollFails = 0;
      var status = await resp.json();
      if (pollGeneration !== _state.generation || pollRunId !== _state.runId) return;
      render(status);
      if (status.status === 'done' || status.status === 'cancelled' || status.status === 'error') {
        stopPolling();
        // Fire the completion hook exactly once so the caller (Eva) can become
        // aware of the actual outcome and acknowledge it.
        if (!_state.completed) {
          _state.completed = true;
          if (typeof _state.onComplete === 'function') {
            try { _state.onComplete(status, _state.endpoint, _state.title); } catch (e) {}
          }
        }
      }
    } catch (e) {
      // transient; keep polling
    }
  }

  async function confirmRun(approve, text, expectedGateId) {
    if (!_state.runId) return;
    if (typeof approve !== 'boolean') {
      setChatStatus('error', _state.title + ': invalid approval decision; no action was approved.');
      return;
    }
    var confirmGeneration = _state.generation;
    var confirmRunId = _state.runId;
    var confirmBase = _state.base;
    var confirmEndpoint = _state.endpoint;
    var gate = (_state.status && _state.status.approval_request) || null;
    if (!gate || !gate.gate_id || !gate.kind) {
      setChatStatus('error', _state.title + ': approval gate is stale.');
      return;
    }
    if (expectedGateId && gate.gate_id !== expectedGateId) {
      setChatStatus('error', _state.title + ': approval gate changed; no action was approved.');
      return;
    }
    var body = gate.kind === 'input'
      ? (approve === false
          ? { run_id: confirmRunId, gate_id: gate.gate_id, kind: 'input', decision: 'cancel' }
          : { run_id: confirmRunId, gate_id: gate.gate_id, kind: 'input', text: text || '' })
      : {
          run_id: confirmRunId, gate_id: gate.gate_id, kind: 'approval',
          decision: approve === true ? 'approve' : 'deny'
        };
    try {
      var resp = await fetch(confirmBase + confirmEndpoint + '/confirm', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      });
      if (!resp.ok) throw new Error('approval ' + resp.status);
      if (confirmGeneration !== _state.generation || confirmRunId !== _state.runId) return;
      // optimistic: hide the prompt until next poll
      var wrap = document.getElementById('ebpPrompt');
      if (wrap) wrap.hidden = true;
      pollOnce();
    } catch (e) {
      if (confirmGeneration !== _state.generation || confirmRunId !== _state.runId) return;
      setChatStatus('error', 'Browser agent: could not send confirmation.');
    }
  }

  async function stopRun() {
    if (!_state.runId) { closePopup(); return; }
    var stopGeneration = _state.generation;
    var stopRunId = _state.runId;
    var stopBase = _state.base;
    var stopEndpoint = _state.endpoint;
    try {
      await fetch(stopBase + stopEndpoint + '/cancel', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ run_id: stopRunId })
      });
      if (stopGeneration !== _state.generation || stopRunId !== _state.runId) return;
      pollOnce();
    } catch (e) {
      if (stopGeneration !== _state.generation || stopRunId !== _state.runId) return;
      closePopup();
    }
  }

  async function _stopAndWaitActiveRun() {
    if (!_state.runId) return true;
    var runId = _state.runId;
    var base = _state.base || bridgeBase();
    var endpoint = _state.endpoint;
    stopPolling();
    try {
      await fetch(base + endpoint + '/cancel', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ run_id: runId })
      });
      for (var attempt = 0; attempt < 80; attempt += 1) {
        var response = await fetch(base + endpoint + '/status?run_id=' +
          encodeURIComponent(runId), { cache: 'no-store' });
        if (response.ok) {
          var status = await response.json();
          if (_state.runId === runId) render(status);
          if (status.status === 'done' || status.status === 'cancelled' || status.status === 'error') {
            return true;
          }
        }
        await new Promise(function(resolve) { setTimeout(resolve, 250); });
      }
    } catch (_) {}
    if (_state.runId === runId) startPolling();
    return false;
  }

  function _cancelRemoteRun(base, endpoint, runId) {
    if (!runId) return Promise.resolve();
    return fetch(base + endpoint + '/cancel', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ run_id: runId })
    }).catch(function() {});
  }

  function cancelActiveRun() {
    var runId = _state.runId;
    var base = _state.base || bridgeBase();
    var endpoint = _state.endpoint;
    stopPolling();
    _state.generation += 1;
    if (_state._fadeTimer) {
      clearTimeout(_state._fadeTimer);
      _state._fadeTimer = null;
    }
    _state.runId = null;
    _state.base = '';
    _state.status = null;
    _state.onComplete = null;
    _state.onConfirm = null;
    _state.onProgress = null;
    _state.confirmKey = null;
    _state.lastProgress = null;
    _state.completed = true;
    _state._embedShotKey = null;
    _state._popupShotKey = null;
    _state._embedRunId = null;
    var popup = document.getElementById('evaBrowserPopup');
    if (popup) popup.remove();
    var embedded = document.getElementById('vvVision');
    if (embedded) {
      embedded.classList.remove('open', 'looking');
      embedded.setAttribute('aria-hidden', 'true');
    }
    var embeddedShot = document.getElementById('vvVisionShot');
    if (embeddedShot) {
      try { embeddedShot.removeAttribute('src'); } catch (_) {}
    }
    return _cancelRemoteRun(base, endpoint, runId);
  }

  function isActive() {
    return !!_state.runId;
  }

  // True when the active run is parked waiting for a confirmation or input that
  // Eva asked about in chat/voice. The next user reply should answer it.
  function isAwaitingConfirm() {
    return !!(_state.runId && _state.status &&
      (_state.status.status === 'awaiting_confirmation' || _state.status.status === 'awaiting_input'));
  }

  // Answer a parked confirmation. approve=true continues (placing the order /
  // submitting input); approve=false stops. text carries free-form input when
  // the park was an input request.
  function answerConfirm(approve, text, gateId) {
    if (!_state.runId) return;
    if (typeof approve !== 'boolean') {
      setChatStatus('error', _state.title + ': invalid approval decision; no action was approved.');
      return;
    }
    confirmRun(approve, text || '', gateId || '');
  }

  global.EvaBrowser = {
    launch: launch,
    isActive: isActive,
    isAwaitingConfirm: isAwaitingConfirm,
    answerConfirm: answerConfirm,
    cancel: cancelActiveRun,
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
    cancel: cancelActiveRun,
    close: closePopup
  };

})(typeof window !== 'undefined' ? window : this);
