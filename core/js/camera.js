// core/js/camera.js
// Eva's "eyes": a thin frontend controller over the bridge camera sensor.
//
//   POST /v1/camera/start   -> start the local presence worker
//   POST /v1/camera/stop    -> stop it (releases the camera)
//   GET  /v1/camera/status  -> presence/looking/faces/motion + arrival_seq
//   GET  /v1/camera/frame   -> latest JPEG frame (for the cloud "look")
//
// Two behaviors:
//   1. Presence auto-wake ("Jarvis comes alive"): while presence mode is on,
//      poll the sensor; when a face newly appears and Eva is idle, open the
//      voice view and enter the awake state (with a short greeting), debounced
//      by a cooldown so passing by does not spam wakeups.
//   2. Eva's eyes (look-on-demand): grab a frame and send it to the vision
//      model so Eva can describe what she sees. If presence mode is off, this
//      momentarily starts the camera, grabs one frame, and stops it again.
//
// Privacy: the camera only runs while presence mode is on or during a one-shot
// look. Frames never leave the machine except the single image sent to the
// vision model when a look is explicitly requested.

(function (global) {
  'use strict';

  var POLL_MS = 5000;
  var _state = {
    enabled: false,            // presence mode on (worker running + polling)
    poll: null,
    lastArrival: 0,            // last arrival_seq we reacted to
    lastWakeAt: 0,
    wakeCooldownMs: 30000,
    present: false,
    looking: false,
    available: null,           // OpenCV present on the bridge?
    device: 0,
    busyLook: false
  };

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

  // --- Presence mode --------------------------------------------------------

  async function enable(device) {
    var dev = (device == null) ? _state.device : device;
    try {
      var resp = await fetch(bridgeBase() + '/v1/camera/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ purpose: 'presence', device: dev })
      });
      var data = await resp.json();
      if (!resp.ok) {
        var msg = _canonicalSecondaryText(
          (data && data.error && data.error.message) ||
          ('Camera start failed (HTTP ' + resp.status + ').')
        ) || 'Camera start failed.';
        setChatStatus('error', 'Camera: ' + msg);
        _state.available = false;
        return false;
      }
      _state.enabled = true;
      _state.device = dev;
      _state.available = true;
      _state.lastArrival = (data && data.arrival_seq) || 0;
      try { localStorage.setItem('cameraPresence', '1'); } catch (e) {}
      startPolling();
      setChatStatus('info', 'Camera presence on.');
      return true;
    } catch (e) {
      setChatStatus('error', 'Camera: could not reach the bridge.');
      return false;
    }
  }

  async function disable() {
    stopPolling();
    _state.enabled = false;
    _state.present = false;
    _state.looking = false;
    try { localStorage.setItem('cameraPresence', '0'); } catch (e) {}
    try {
      await fetch(bridgeBase() + '/v1/camera/stop', { method: 'POST' });
    } catch (e) { /* ignore */ }
  }

  function isEnabled() { return _state.enabled; }

  function startPolling() {
    stopPolling();
    _state.poll = setInterval(pollOnce, POLL_MS);
    pollOnce();
  }

  function stopPolling() {
    if (_state.poll) { clearInterval(_state.poll); _state.poll = null; }
  }

  async function pollOnce() {
    try {
      var resp = await fetch(bridgeBase() + '/v1/camera/status', {
        signal: (typeof AbortSignal !== 'undefined' && AbortSignal.timeout) ? AbortSignal.timeout(4000) : undefined
      });
      if (!resp.ok) return;
      var s = await resp.json();
      _state.present = !!s.present;
      _state.looking = !!s.looking;
      if (typeof s.available === 'boolean') _state.available = s.available;
      // Worker died unexpectedly: reflect that and stop polling.
      if (_state.enabled && s.enabled === false) {
        _state.enabled = false;
        stopPolling();
        return;
      }
      // Presence arrival edge -> maybe wake Eva.
      if (s.present && typeof s.arrival_seq === 'number' && s.arrival_seq > _state.lastArrival) {
        _state.lastArrival = s.arrival_seq;
        maybeWake();
      }
    } catch (e) { /* transient; keep polling */ }
  }

  function _vvBusy() {
    // True when Eva is mid-turn and should not be interrupted by a wakeup.
    if (typeof _vv === 'undefined' || !_vv) return false;
    return _vv.phase === 'thinking' || _vv.phase === 'speaking' || _vv.phase === 'awake';
  }

  function maybeWake() {
    var now = Date.now();
    if (now - _state.lastWakeAt < _state.wakeCooldownMs) return;
    if (_vvBusy()) return;
    _state.lastWakeAt = now;
    triggerWake();
  }

  // Jarvis "comes alive": surface the orb and listen for a command.
  function triggerWake() {
    try {
      var vvOpen = (typeof _vv !== 'undefined' && _vv && _vv.open);
      if (!vvOpen && typeof openVoiceView === 'function') {
        openVoiceView();
      }
      if (typeof _vvEnterAwake === 'function') {
        _vvEnterAwake((typeof _vv !== 'undefined' && _vv && _vv.convoTimeoutMs) || 12000);
      }
      // Soft greeting so the wake is audible even before a command.
      var autoSpeakEl = document.getElementById('autoSpeak');
      var greet = _pickGreeting();
      if ((vvOpen || (autoSpeakEl && autoSpeakEl.checked) || (typeof _vv !== 'undefined' && _vv && _vv.open)) &&
          typeof speakText === 'function') {
        speakText(greet);
      }
    } catch (e) { /* never let a wake attempt throw */ }
  }

  var _GREETINGS = ['Hey. I see you.', 'I\'m here.', 'Yes? I\'m listening.', 'Welcome back.', 'Hi. What do you need?'];
  function _pickGreeting() {
    return _GREETINGS[Math.floor(Math.random() * _GREETINGS.length)];
  }

  // --- Eva's eyes (look on demand) -----------------------------------------

  async function _getFrameDataUrl(captureReceipt) {
    if (!captureReceipt || captureReceipt.contract !== 'eva.camera-capture/1' ||
        !/^[0-9a-f]{32}$/.test(String(captureReceipt.capture_id || '')) ||
        !/^[0-9a-f]{64}$/.test(String(captureReceipt.question_hash || ''))) {
      throw new Error('Camera capture receipt is invalid.');
    }
    var resp = await fetch(
      bridgeBase() + '/v1/camera/frame?capture_id=' +
        encodeURIComponent(captureReceipt.capture_id),
      { cache: 'no-store' }
    );
    if (!resp.ok) return null;
    var contract = resp.headers.get('X-Eva-Camera-Contract');
    var captureId = resp.headers.get('X-Eva-Camera-Capture-Id');
    var frameSeq = resp.headers.get('X-Eva-Camera-Frame-Seq');
    var questionHash = resp.headers.get('X-Eva-Camera-Question-Hash');
    if (contract !== 'eva.camera-capture/1' ||
        captureId !== captureReceipt.capture_id ||
        questionHash !== captureReceipt.question_hash ||
        !/^[1-9][0-9]{0,15}$/.test(String(frameSeq || '')) ||
        Number(frameSeq) <= captureReceipt.baseline_frame_seq) {
      throw new Error('Camera fresh-frame attestation failed.');
    }
    var blob = await resp.blob();
    var dataUrl = await new Promise(function (resolve) {
      var fr = new FileReader();
      fr.onload = function () { resolve(fr.result); };
      fr.onerror = function () { resolve(null); };
      fr.readAsDataURL(blob);
    });
    return {
      dataUrl: dataUrl,
      receipt: {
        contract: contract, capture_id: captureId, state: 'succeeded',
        question_hash: questionHash, frame_seq: String(frameSeq)
      }
    };
  }

  // Current published frame sequence (increments once per captured frame).
  async function _frameSeq() {
    try {
      var resp = await fetch(bridgeBase() + '/v1/camera/status', { cache: 'no-store' });
      if (!resp.ok) return -1;
      var s = await resp.json();
      return (typeof s.frame_seq === 'number') ? s.frame_seq : -1;
    } catch (e) { return -1; }
  }

  // Wait until the worker has published `advance` new frames since baseSeq, so
  // the next fetched frame is guaranteed live (not a stale/cached image).
  async function _waitForNewFrame(baseSeq, advance, timeoutMs) {
    if (baseSeq < 0) return false; // worker has no seq yet; caller falls back
    var deadline = Date.now() + (timeoutMs || 6000);
    while (Date.now() < deadline) {
      await _sleep(200);
      var seq = await _frameSeq();
      if (seq >= 0 && seq >= baseSeq + advance) return true;
    }
    return false;
  }

  function _sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

  // --- Look popup (visual confirmation of what Eva sees) --------------------
  // A small floating window, styled like the browser/desktop agent popup, that
  // shows the exact frame sent to the vision model plus the description. It lets
  // the user confirm the camera is pointed correctly and see which model
  // answered. "Look again" re-runs the look with the same question.
  var _popup = { lastQuestion: '' };

  function _ensurePopup() {
    var el = document.getElementById('evaCameraPopup');
    if (el) return el;
    el = document.createElement('div');
    el.id = 'evaCameraPopup';
    el.className = 'eva-browser-popup eva-camera-popup';
    el.setAttribute('role', 'dialog');
    el.setAttribute('aria-label', 'Eva camera view');
    el.innerHTML = [
      '<div class="ebp-titlebar" id="ecpTitlebar">',
      '  <span class="ebp-dot"></span>',
      '  <span class="ebp-title">Eva &middot; Looking</span>',
      '  <button class="ebp-close" id="ecpClose" type="button" aria-label="Close">&times;</button>',
      '</div>',
      '<div class="ebp-stage">',
      '  <img class="ebp-shot" id="ecpShot" alt="Camera view" />',
      '  <div class="ebp-shot-empty" id="ecpShotEmpty">Capturing&hellip;</div>',
      '</div>',
      '<div class="ebp-subgoal" id="ecpResult"></div>',
      '<div class="ebp-statusrow">',
      '  <span class="ebp-badge" id="ecpBadge">looking</span>',
      '  <span class="ebp-url" id="ecpModel"></span>',
      '</div>',
      '<div class="ebp-footer">',
      '  <button class="ebp-btn ebp-stop" id="ecpAgain" type="button">Look again</button>',
      '</div>'
    ].join('');
    document.body.appendChild(el);
    document.getElementById('ecpClose').addEventListener('click', _closePopup);
    document.getElementById('ecpAgain').addEventListener('click', function () {
      _lookAgainAuthorized();
    });
    var shot = document.getElementById('ecpShot');
    shot.addEventListener('load', function () {
      this.style.visibility = 'visible';
      var e = document.getElementById('ecpShotEmpty'); if (e) e.hidden = true;
    });
    shot.addEventListener('error', function () {
      this.style.visibility = 'hidden';
      var e = document.getElementById('ecpShotEmpty'); if (e) e.hidden = false;
    });
    return el;
  }

  function _closePopup() {
    var el = document.getElementById('evaCameraPopup');
    if (el) el.remove();
  }

  async function _lookAgainAuthorized() {
    try {
      if (!global.evaStandalone ||
          typeof global.evaStandalone.authorizeCameraLook !== 'function') {
        throw new Error('Native camera authorization is unavailable.');
      }
      var authorization = await global.evaStandalone.authorizeCameraLook(
        _popup.lastQuestion, _state.device
      );
      if (!authorization || authorization.authorized !== true) return;
      await look(_popup.lastQuestion, {
        silent: true, authorization: authorization
      });
    } catch (error) {
      _popupBadge('error');
      _popupResult(_canonicalSecondaryText(
        (error && error.message) || 'Camera authorization failed.'
      ));
    }
  }

  // --- Embedded vision panel (voice view) ----------------------------------
  // When the fullscreen voice view is open, mirror the look into the faint panel
  // on Eva's right, so it reads as "seeing her thoughts" rather than a separate
  // window. Returns true when the voice view is open (so the popup can be
  // suppressed in that mode).
  function _vvOpen() {
    return (typeof _vv !== 'undefined' && _vv && _vv.open);
  }
  function _visionPanel() { return document.getElementById('vvVision'); }
  function _visionShow(looking) {
    var p = _visionPanel();
    if (!p) return;
    p.classList.add('open');
    p.setAttribute('aria-hidden', 'false');
    if (looking) p.classList.add('looking'); else p.classList.remove('looking');
  }
  function _visionFrame(dataUrl) {
    var img = document.getElementById('vvVisionShot');
    if (img && dataUrl) img.src = dataUrl;
  }
  function _visionText(text) {
    var t = document.getElementById('vvVisionText');
    if (t) t.textContent = text || '';
  }
  function _visionDone() {
    var p = _visionPanel();
    if (p) p.classList.remove('looking');
    // Leave the frame + text up briefly, then fade out.
    if (_popup._fadeTimer) clearTimeout(_popup._fadeTimer);
    _popup._fadeTimer = setTimeout(function () {
      var pp = _visionPanel();
      if (pp) { pp.classList.remove('open'); pp.setAttribute('aria-hidden', 'true'); }
    }, 9000);
  }

  function _popupShow() { _ensurePopup(); }
  function _popupFrame(dataUrl) {
    _ensurePopup();
    var img = document.getElementById('ecpShot');
    if (img && dataUrl) img.src = dataUrl;
  }
  function _popupBadge(text) {
    var b = document.getElementById('ecpBadge');
    if (b) {
      b.textContent = text;
      // Reuse the agent popup badge colour states (done=green, error=red).
      var st = (text === 'done' || text === 'error') ? text : '';
      if (st) b.setAttribute('data-state', st); else b.removeAttribute('data-state');
    }
  }
  function _popupResult(text) {
    var r = document.getElementById('ecpResult');
    if (r) r.textContent = text || '';
  }
  function _popupModel(text) {
    var m = document.getElementById('ecpModel');
    if (m) m.textContent = text || '';
  }

  function _canonicalSecondaryText(value) {
    if (typeof canonicalizeEvaResponse === 'function') {
      return canonicalizeEvaResponse(value, {
        allowCamera: false, allowAgentControls: false
      }).text;
    }
    var source = String(value || '');
    var first = source.search(/\[\[\/?EVA_/);
    return (first < 0 ? source : source.slice(0, first)).trim();
  }

  // Grab the current camera frame and ask the vision model about it. Returns a
  // plain-text description, or throws with a user-facing message. Also drives
  // the confirmation popup so the user can see the exact frame that was sent.
  async function look(question, opts) {
    opts = opts || {};
    var authorization = opts.authorization;
    if (!authorization || authorization.authorized !== true ||
        typeof authorization.capability !== 'string' ||
        !authorization.specification ||
        authorization.specification.question !== question ||
        authorization.specification.device !== _state.device) {
      throw new Error('One-shot camera authorization is required.');
    }
    if (_state.busyLook) return 'I\'m already looking.';
    _state.busyLook = true;
    _popup.lastQuestion = question || '';
    var startedHere = false;
    // In the fullscreen voice view, render into the embedded panel on Eva's
    // right ("her thoughts"); otherwise use the floating confirmation popup.
    var embedded = _vvOpen();
    if (embedded) {
      _visionShow(true);
      _visionText('');
    } else {
      _popupShow();
      _popupBadge('capturing');
      _popupResult('');
      _popupModel('');
    }
    try {
      var key = openaiKey();

      // Always capture a GENUINELY FRESH frame. The worker publishes a
      // frame_seq that increments per captured frame; wait until it advances
      // past the value seen when the look began so we never describe a stale or
      // cached image (the repeated "old image" problem).
      var dataUrl = null;
      startedHere = !_state.enabled;
      var captureReceipt = await enableOneShot(authorization, question);
      if (!captureReceipt) throw new Error('I could not authorize a fresh camera frame.');
      var baseSeq = captureReceipt.baseline_frame_seq;
      // Wait for at least 2 new frames so the published image is current.
      var fresh = await _waitForNewFrame(baseSeq, 2, 6000);
      var frameResult = null;
      if (fresh) frameResult = await _getFrameDataUrl(captureReceipt);
      // Fallbacks if the seq never advanced (older worker / odd driver).
      if (!frameResult) {
        for (var i = 0; i < 8 && !frameResult; i++) {
          await _sleep(250);
          frameResult = await _getFrameDataUrl(captureReceipt);
        }
      }
      dataUrl = frameResult && frameResult.dataUrl;
      if (!dataUrl) throw new Error('I could not get a picture from the camera.');

      if (embedded) { _visionFrame(dataUrl); }
      else { _popupFrame(dataUrl); _popupBadge('looking'); }

      var res = await _describe(dataUrl, question, key);
      res.text = _canonicalSecondaryText(res && res.text);
      if (!res.text) throw new Error('The vision response contained no safe description.');
      if (embedded) {
        // Prefix the backend so the model is visible even in the ambient panel.
        var tag = res.model ? ('[' + res.model + ']\n') : '';
        _visionText(tag + (res.text || ''));
        _visionDone();
      } else {
        _popupBadge('done');
        _popupResult(res.text || '');
        _popupModel(res.model || '');
      }
      return { text: res.text, capture_receipt: frameResult.receipt };
    } catch (e) {
      var msg = _canonicalSecondaryText(
        (e && e.message) ? e.message : String(e)
      ) || 'Camera operation failed.';
      if (embedded) { _visionText(msg); _visionDone(); }
      else { _popupBadge('error'); _popupResult(msg); }
      throw e;
    } finally {
      if (startedHere) {
        try { await fetch(bridgeBase() + '/v1/camera/stop', { method: 'POST' }); } catch (e) {}
        _state.enabled = false;
        stopPolling();
      }
      _state.busyLook = false;
    }
  }

  // Start the worker without flipping persistent presence mode on.
  async function enableOneShot(authorization, question) {
    try {
      var resp = await fetch(bridgeBase() + '/v1/camera/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          purpose: 'one_shot', device: _state.device, question: question,
          launch_capability: authorization.capability
        })
      });
      if (!resp.ok) return null;
      var data = await resp.json();
      var receipt = data && data.capture_receipt;
      if (!receipt || receipt.contract !== 'eva.camera-capture/1' ||
          receipt.state !== 'authorized' ||
          !/^[0-9a-f]{32}$/.test(String(receipt.capture_id || '')) ||
          !/^[0-9a-f]{64}$/.test(String(receipt.question_hash || '')) ||
          typeof receipt.baseline_frame_seq !== 'number') return null;
      _state.enabled = true;
      return receipt;
    } catch (e) { return null; }
  }

  // Describe the frame. Returns { text, model }. Routing by preference order:
  //   localStorage 'cameraVisionProvider' = 'github' | 'openai' | 'copilot' | 'auto'
  //   Default (auto/empty): GitHub-hosted model first (needs a GitHub PAT with
  //   the Models permission), then OpenAI direct (needs an OpenAI key), then the
  //   Copilot/Claude bridge. Each backend is a fallback for the previous one, so
  //   a look still answers if the preferred backend is missing or fails.
  async function _describe(dataUrl, question, key) {
    var prompt = (question && String(question).trim()) ||
      'Describe what you see in this webcam image in one or two natural sentences.';
    var provider = '';
    try { provider = (localStorage.getItem('cameraVisionProvider') || '').toLowerCase(); } catch (e) {}
    var pat = _githubPat();

    // Build the ordered backend list based on the preference.
    var order;
    if (provider === 'openai') order = ['openai', 'github', 'copilot'];
    else if (provider === 'copilot' || provider === 'claude') order = ['copilot', 'github', 'openai'];
    else order = ['github', 'openai', 'copilot']; // auto / default

    var lastErr = null;
    for (var i = 0; i < order.length; i++) {
      try {
        if (order[i] === 'github' && pat) {
          var g = await _describeViaGitHubModels(dataUrl, prompt, pat);
          if (g && g.text) return g;
        } else if (order[i] === 'openai' && key) {
          var r = await _describeViaOpenAI(dataUrl, prompt, key);
          if (r) return { text: r, model: 'OpenAI gpt-4o (direct)' };
        } else if (order[i] === 'copilot') {
          var b = await _describeViaBridge(dataUrl, prompt);
          if (b && b.text) return b;
        }
      } catch (e) { lastErr = e; /* try the next backend */ }
    }
    if (lastErr) throw lastErr;
    throw new Error('No vision backend available (set a GitHub PAT with Models permission or an OpenAI key in Settings > Auth).');
  }

  function _githubPat() {
    if (typeof getAuthKey === 'function') return getAuthKey('GITHUB_PAT') || '';
    return (global.GITHUB_PAT || '');
  }

  function _visionSystemPrompt() {
    return 'You are Eva looking through the user\'s webcam. Answer naturally and briefly, ' +
      'in the first person, as if you are seeing it now. Do not mention pixels, images, or that ' +
      'you were given a photo.';
  }

  // GitHub-hosted model via the GitHub Models API (OpenAI-compatible, PAT auth).
  // Returns { text, model }. The catalog uses 'publisher/model' ids; default to
  // openai/gpt-4o, overridable via localStorage 'cameraVisionGithubModel'.
  async function _describeViaGitHubModels(dataUrl, prompt, pat) {
    var model = '';
    try { model = localStorage.getItem('cameraVisionGithubModel') || ''; } catch (e) {}
    if (!model) model = 'openai/gpt-4o';
    var resp = await fetch('https://models.github.ai/inference/chat/completions', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer ' + pat, 'Content-Type': 'application/json' },
      body: JSON.stringify({
        model: model,
        max_tokens: 220,
        temperature: 0.4,
        messages: [
          { role: 'system', content: _visionSystemPrompt() },
          { role: 'user', content: [
            { type: 'text', text: prompt },
            { type: 'image_url', image_url: { url: dataUrl } }
          ] }
        ]
      }),
      signal: (typeof AbortSignal !== 'undefined' && AbortSignal.timeout) ? AbortSignal.timeout(60000) : undefined
    });
    if (!resp.ok) {
      throw new Error('GitHub Models vision request failed (HTTP ' + resp.status + ').');
    }
    var data = await resp.json();
    var text = (data.choices && data.choices[0] && data.choices[0].message &&
                data.choices[0].message.content || '').trim();
    if (!text) return null;
    return { text: text, model: 'GitHub ' + model };
  }

  async function _describeViaOpenAI(dataUrl, prompt, key) {
    var resp = await fetch('https://api.openai.com/v1/chat/completions', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json' },
      body: JSON.stringify({
        model: 'gpt-4o',
        max_tokens: 220,
        temperature: 0.4,
        messages: [
          { role: 'system', content: _visionSystemPrompt() },
          { role: 'user', content: [
            { type: 'text', text: prompt },
            { type: 'image_url', image_url: { url: dataUrl } }
          ] }
        ]
      })
    });
    if (!resp.ok) {
      throw new Error('OpenAI vision request failed (HTTP ' + resp.status + ').');
    }
    var data = await resp.json();
    return (data.choices && data.choices[0] && data.choices[0].message &&
            data.choices[0].message.content || '').trim();
  }

  // Ask the bridge to describe the frame with a Copilot/Claude vision model.
  // Returns { text, model }, or null if the bridge route is unavailable.
  async function _describeViaBridge(dataUrl, prompt) {
    try {
      var comma = dataUrl.indexOf(',');
      var b64 = comma >= 0 ? dataUrl.slice(comma + 1) : dataUrl;
      var sys = 'You are Eva looking through the user\'s webcam. Answer naturally and briefly, ' +
        'in the first person, as if you are seeing it now. Do not mention pixels, images, or that ' +
        'you were given a photo. ';
      var model = '';
      try { model = localStorage.getItem('cameraVisionModel') || ''; } catch (e) {}
      var resp = await fetch(bridgeBase() + '/v1/vision/look', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          image_b64: b64,
          mime: 'image/jpeg',
          question: sys + prompt,
          model: model || undefined
        }),
        signal: (typeof AbortSignal !== 'undefined' && AbortSignal.timeout) ? AbortSignal.timeout(95000) : undefined
      });
      if (!resp.ok) return null;
      var data = await resp.json();
      var text = (data && data.text) ? String(data.text).trim() : '';
      if (!text) return null;
      return { text: text, model: (data && data.model) ? ('Copilot ' + data.model) : 'Copilot' };
    } catch (e) {
      return null;
    }
  }

  function status() { return Object.assign({}, _state); }
  function device() { return _state.device; }

  global.EvaCamera = {
    enable: enable,
    disable: disable,
    isEnabled: isEnabled,
    look: look,
    device: device,
    status: status
  };

})(typeof window !== 'undefined' ? window : this);
