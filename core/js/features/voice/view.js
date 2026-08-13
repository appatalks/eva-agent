//  Voice View — ambient, always-listening mode (sci-fi HUD)
// ═══════════════════════════════════════════════════════════════

var _vv = {
  open: false,
  compactActive: false,
  animFrame: null,
  waveFrame: null,
  audioCtx: null,
  analyser: null,
  micStream: null,
  dataArray: null,
  phase: 'idle', // idle | listening | awake | thinking | speaking | error
  recognition: null,
  whisperProvider: '',
  listenGeneration: 0,
  awakeTimer: null,
  convoMode: true,        // stay in an active conversation after the wake word
  convoTimeoutMs: 30000,  // quiet period before dropping back to standby
  endpointDelayMs: 2200,
  liveTranslation: false,
  liveTranslationRun: 0,
  liveTranslationAbort: null,
  endpoint: null,
  lastTranscript: '',
  lastEvaReply: '',
  speakObserver: null,
  ttsSource: null,
  ttsAnalyser: null,
  ttsDataArray: null,
  ttsDelay: null,
  particles: [],
  hudInterval: null,
  memoryGraph: null,
  memoryGraphFrame: null,
  memoryGraphTimer: null,
  memoryGraphFetchInFlight: false,
  memoryGraphWidth: 0,
  memoryGraphHeight: 0,
  cmdStart: 0
};

var VOICE_MEMORY_GRAPH_ENABLED = false;

function _vvIsActive() {
  return _vv.open || _vv.compactActive;
}

function _vvLoadPreferences() {
  try {
    _vv.convoMode = localStorage.getItem('vvConvoMode') !== '0';
    var savedTimeout = parseInt(localStorage.getItem('vvConvoTimeoutMs'), 10);
    if (Number.isInteger(savedTimeout) && savedTimeout >= 5000 && savedTimeout <= 300000) {
      _vv.convoTimeoutMs = savedTimeout;
    }
    var savedEndpointDelay = parseInt(localStorage.getItem('voice_endpoint_delay_ms'), 10);
    if (Number.isInteger(savedEndpointDelay) && savedEndpointDelay >= 1000 && savedEndpointDelay <= 5000) {
      _vv.endpointDelayMs = savedEndpointDelay;
    }
  } catch (e) {}
}

function toggleCompactVoiceController() {
  if (_vv.compactActive) {
    _vv.compactActive = false;
    _vvSetLiveTranslation(false, true);
    _vvStopListening();
    _vvSetStatus('idle');
    return;
  }
  if (typeof stopVoiceListener === 'function') stopVoiceListener();
  _vv.compactActive = true;
  _vvLoadPreferences();
  _vvSyncConvoControls();
  _vvSyncLiveTranslationControls();
  _vvPrepareAcknowledgements();
  _vvSetStatus('idle');
  _vvStartListening();
}

function toggleVoiceView() {
  if (_vv.open) {
    closeVoiceView();
  } else {
    openVoiceView();
  }
}

function openVoiceView() {
  var el = document.getElementById('voiceView');
  if (!el) return;
  if (typeof closeAgentOperationsForNavigation === 'function') closeAgentOperationsForNavigation();
  if (window.EvaMemoryInspector && typeof window.EvaMemoryInspector.close === 'function') window.EvaMemoryInspector.close();
  _vv.open = true;
  el.classList.add('open');
  el.setAttribute('aria-hidden', 'false');
  // Conversation mode: after the wake word, keep listening between turns until a
  // quiet period elapses. Persisted so the user's choice survives restarts.
  _vvLoadPreferences();
  _vvSyncConvoControls();
  _vvSyncLiveTranslationControls();
  _vvSetStatus('idle');
  _vvPrepareAcknowledgements();

  var closeBtn = document.getElementById('voiceViewClose');
  if (closeBtn) closeBtn.onclick = closeVoiceView;

  var assetsClose = document.getElementById('vvAssetsClose');
  if (assetsClose) assetsClose.onclick = _vvHideAssets;

  var canvas = document.getElementById('voiceViewCanvas');
  if (canvas) canvas.onclick = _vvToggleListening;

  _vv._onEscape = function(e) { if (e.key === 'Escape') closeVoiceView(); };
  document.addEventListener('keydown', _vv._onEscape);

  _vvInitParticles();
  _vvStartCanvas();
  _vvStartWaveBar();
  _vvStartHUD();
  _vvStartLogStream();
  if (VOICE_MEMORY_GRAPH_ENABLED) _vvStartMemoryGraph();
}

function closeVoiceView() {
  var keepCompactController = _vv.compactActive;
  if (!keepCompactController) _vvSetLiveTranslation(false, true);
  _vv.open = false;
  var el = document.getElementById('voiceView');
  if (el) {
    el.classList.remove('open');
    el.setAttribute('aria-hidden', 'true');
    el.removeAttribute('data-phase');
  }
  if (!keepCompactController) {
    if (_vv.speakObserver) { _vv.speakObserver.disconnect(); _vv.speakObserver = null; }
    _vvDetachSpeakStartListeners();
    if (_vv._watchTimer) { clearTimeout(_vv._watchTimer); _vv._watchTimer = null; }
    if (_vv._postTextTimer) { clearTimeout(_vv._postTextTimer); _vv._postTextTimer = null; }
    _vvStopBargeMonitor();
  }
  _vvStopLogStream();
  _vvStopMemoryGraph();
  // Clear the embedded vision panel so a stale frame does not linger on reopen.
  var vvVision = document.getElementById('vvVision');
  if (vvVision) {
    vvVision.classList.remove('open', 'looking');
    vvVision.setAttribute('aria-hidden', 'true');
    var vvShot = document.getElementById('vvVisionShot');
    if (vvShot) vvShot.removeAttribute('src');
    var vvText = document.getElementById('vvVisionText');
    if (vvText) vvText.textContent = '';
  }
  if (!keepCompactController && _vv._wasAutoSpeak !== undefined) {
    var autoSpeak = document.getElementById('autoSpeak');
    if (autoSpeak) autoSpeak.checked = _vv._wasAutoSpeak;
    delete _vv._wasAutoSpeak;
  }
  if (_vv._onEscape) {
    document.removeEventListener('keydown', _vv._onEscape);
    delete _vv._onEscape;
  }
  if (!keepCompactController) _vvStopListening();
  _vvStopCanvas();
  _vvStopWaveBar();
  _vvStopHUD();
  _vvHideAssets();
}

function _vvSetStatus(phase) {
  _vv.phase = phase;
  var el = document.getElementById('voiceView');
  if (el) el.setAttribute('data-phase', phase);
  // Update HUD phase indicator
  var ph = document.getElementById('vvHudPhase');
  if (ph) {
    var labels = { idle: 'IDLE', listening: 'LISTENING', awake: 'AWAKE', thinking: 'PROCESSING', speaking: 'SPEAKING', error: 'ERROR' };
    ph.textContent = labels[phase] || phase.toUpperCase();
  }
  var compact = document.querySelector('.eva-sidebar-voice');
  var compactStatus = document.getElementById('evaSidebarVoiceStatus');
  var compactButton = document.getElementById('evaSidebarMicButton');
  var compactLabels = { idle: 'STANDBY', listening: 'LISTENING', awake: 'AWAKE', thinking: 'WORKING', speaking: 'SPEAKING', error: 'ERROR' };
  if (compact) compact.dataset.state = phase;
  if (compactStatus) compactStatus.textContent = compactLabels[phase] || phase.toUpperCase();
  if (compactButton) {
    compactButton.title = phase === 'idle' ? 'Start Eva voice control' : 'Stop Eva voice control';
    compactButton.setAttribute('aria-label', compactButton.title);
  }
}

// --- Particle system ---

function _vvInitParticles() {
  _vv.particles = [];
  for (var i = 0; i < 60; i++) {
    _vv.particles.push({
      angle: Math.random() * Math.PI * 2,
      dist: 0.5 + Math.random() * 0.6,
      speed: 0.1 + Math.random() * 0.3,
      size: 0.5 + Math.random() * 1.5,
      alpha: 0.1 + Math.random() * 0.3,
      drift: (Math.random() - 0.5) * 0.02
    });
  }
  // Electrical impulse pools: radial discharges shoot outward from the orb edge
  // like neural firings; orbit pulses race along the outer rings leaving a
  // glowing trail. Both are spawned dynamically in the draw loop.
  _vv.impulses = [];
  _vv.orbits = [];
  _vv._lastImpulseT = 0;
}

// --- HUD data feeds ---

function _vvStartHUD() {
  _vvUpdateHUD();
  _vv.hudInterval = setInterval(_vvUpdateHUD, 1000);
}

function _vvStopHUD() {
  if (_vv.hudInterval) { clearInterval(_vv.hudInterval); _vv.hudInterval = null; }
}

// --- Background log feed (faint scrolling bridge stdout) ---

function _vvStartLogStream() {
  var el = document.getElementById('vvLogStream');
  if (!el) return;
  el.innerHTML = '';
  _vv._logSince = 0;
  _vv._logPolling = false;
  _vvPollLogStream();
  _vv.logInterval = setInterval(_vvPollLogStream, 15000);
}

function _vvStopLogStream() {
  if (_vv.logInterval) { clearInterval(_vv.logInterval); _vv.logInterval = null; }
  var el = document.getElementById('vvLogStream');
  if (el) el.innerHTML = '';
}

function _vvGraphHash(value) {
  var text = String(value || '');
  var hash = 2166136261;
  for (var index = 0; index < text.length; index++) {
    hash ^= text.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return (hash >>> 0) / 4294967295;
}

function _vvMemoryGraphSnapshot(graph) {
  var rawNodes = (graph && graph.nodes || []).slice(0, 48);
  var allowed = {};
  rawNodes.forEach(function(node) { allowed[node.id] = true; });
  var edges = (graph && graph.edges || []).filter(function(edge) {
    return allowed[edge.source] && allowed[edge.target];
  }).slice(0, 72);
  var nodes = {};
  rawNodes.forEach(function(node) { nodes[node.id] = Object.assign({}, node); });

  var entityNodes = rawNodes.filter(function(node) { return node.type === 'entity'; });
  var agentNodes = rawNodes.filter(function(node) { return node.type === 'agent'; });
  var childEdges = {};
  edges.forEach(function(edge) {
    if (edge.type !== 'memory') return;
    childEdges[edge.source] = childEdges[edge.source] || [];
    childEdges[edge.source].push(edge.target);
  });
  var positions = { 'eva-root': { x: 0.5, y: 0.5 } };
  entityNodes.forEach(function(node, index) {
    var angle = -Math.PI / 2 + (Math.PI * 2 * index / Math.max(entityNodes.length, 1));
    var radius = 0.23 + (_vvGraphHash(node.id) * 0.08);
    positions[node.id] = { x: 0.5 + Math.cos(angle) * radius, y: 0.5 + Math.sin(angle) * radius };
  });
  agentNodes.forEach(function(node, index) {
    var angle = -Math.PI / 2 + (Math.PI * 2 * index / Math.max(agentNodes.length, 1));
    positions[node.id] = { x: 0.5 + Math.cos(angle) * 0.39, y: 0.5 + Math.sin(angle) * 0.32 };
  });
  Object.keys(childEdges).forEach(function(parentId) {
    var parent = positions[parentId] || positions['eva-root'];
    childEdges[parentId].forEach(function(childId, index) {
      var angle = (_vvGraphHash(childId) * Math.PI * 2) + index * 0.42;
      var radius = 0.07 + (index % 3) * 0.028;
      positions[childId] = {
        x: Math.max(0.06, Math.min(0.94, parent.x + Math.cos(angle) * radius)),
        y: Math.max(0.08, Math.min(0.92, parent.y + Math.sin(angle) * radius))
      };
    });
  });
  rawNodes.forEach(function(node, index) {
    if (positions[node.id]) return;
    var theta = _vvGraphHash(node.id) * Math.PI * 2;
    positions[node.id] = { x: 0.5 + Math.cos(theta) * 0.18, y: 0.5 + Math.sin(theta) * 0.18 };
  });
  return { nodes: nodes, edges: edges, positions: positions };
}

function _vvStartMemoryGraph() {
  _vv.memoryGraph = null;
  _vv.memoryGraphFetchInFlight = false;
  _vvRefreshMemoryGraph();
  _vv.memoryGraphTimer = setInterval(_vvRefreshMemoryGraph, 12000);
  _vvDrawMemoryGraph();
}

function _vvStopMemoryGraph() {
  if (_vv.memoryGraphTimer) { clearInterval(_vv.memoryGraphTimer); _vv.memoryGraphTimer = null; }
  if (_vv.memoryGraphFrame) { cancelAnimationFrame(_vv.memoryGraphFrame); _vv.memoryGraphFrame = null; }
  _vv.memoryGraph = null;
  _vv.memoryGraphFetchInFlight = false;
  var canvas = document.getElementById('vvMemoryGraph');
  if (canvas) {
    var context = canvas.getContext('2d');
    context.clearRect(0, 0, canvas.width, canvas.height);
  }
}

async function _vvRefreshMemoryGraph() {
  if (!_vv.open || _vv.memoryGraphFetchInFlight) return;
  var base = (typeof getSafeBridgeBaseUrl === 'function') ? getSafeBridgeBaseUrl() : '';
  if (!base) return;
  _vv.memoryGraphFetchInFlight = true;
  try {
    var options = { method: 'GET' };
    if (typeof AbortSignal !== 'undefined' && AbortSignal.timeout) options.signal = AbortSignal.timeout(3000);
    var response = await fetch(base.replace(/\/+$/, '') + '/v1/agents/overview?include_graph=1', options);
    if (!response.ok) return;
    var payload = await response.json();
    if (_vv.open && payload && payload.graph) _vv.memoryGraph = _vvMemoryGraphSnapshot(payload.graph);
  } catch (_) {
    // The topology is ambient; a transient bridge failure should stay invisible.
  } finally {
    _vv.memoryGraphFetchInFlight = false;
  }
}

function _vvDrawMemoryGraph() {
  var canvas = document.getElementById('vvMemoryGraph');
  if (!canvas) return;
  var context = canvas.getContext('2d');
  function draw(now) {
    if (!_vv.open) return;
    var bounds = canvas.getBoundingClientRect();
    var ratio = Math.min(window.devicePixelRatio || 1, 2);
    if (bounds.width !== _vv.memoryGraphWidth || bounds.height !== _vv.memoryGraphHeight) {
      _vv.memoryGraphWidth = bounds.width;
      _vv.memoryGraphHeight = bounds.height;
      canvas.width = Math.max(1, Math.round(bounds.width * ratio));
      canvas.height = Math.max(1, Math.round(bounds.height * ratio));
      context.setTransform(ratio, 0, 0, ratio, 0, 0);
    }
    var width = Math.max(1, bounds.width);
    var height = Math.max(1, bounds.height);
    context.clearRect(0, 0, width, height);
    var graph = _vv.memoryGraph;
    if (graph && graph.edges.length) {
      var time = now / 1000;
      var phaseEnergy = _vv.phase === 'speaking' ? 1 : _vv.phase === 'thinking' ? 0.75 : _vv.phase === 'awake' ? 0.55 : 0.3;
      graph.edges.forEach(function(edge, index) {
        var source = graph.positions[edge.source];
        var target = graph.positions[edge.target];
        if (!source || !target) return;
        var sx = source.x * width;
        var sy = source.y * height;
        var tx = target.x * width;
        var ty = target.y * height;
        var agentLink = edge.type === 'orchestration' || edge.type === 'dependency';
        context.beginPath();
        context.moveTo(sx, sy);
        context.lineTo(tx, ty);
        context.strokeStyle = agentLink ? 'rgba(125,172,255,0.42)' : 'rgba(95,240,207,' + (0.16 + Number(edge.confidence || 0) * 0.24) + ')';
        context.lineWidth = agentLink ? 1.1 : 0.7;
        context.stroke();
        if (phaseEnergy > 0.45 && (agentLink || Number(edge.confidence || 0) >= 0.8)) {
          var progress = (time * (agentLink ? 0.22 : 0.14) + index * 0.131) % 1;
          context.beginPath();
          context.arc(sx + (tx - sx) * progress, sy + (ty - sy) * progress, agentLink ? 2 : 1.35, 0, Math.PI * 2);
          context.fillStyle = agentLink ? 'rgba(191,218,255,0.8)' : 'rgba(177,255,233,0.7)';
          context.fill();
        }
      });
      Object.keys(graph.nodes).forEach(function(nodeId) {
        var node = graph.nodes[nodeId];
        var position = graph.positions[nodeId];
        if (!position) return;
        var x = position.x * width;
        var y = position.y * height;
        var core = node.type === 'core';
        var entity = node.type === 'entity';
        var agent = node.type === 'agent';
        var radius = core ? 7 : entity ? 4.5 : agent ? 4 : 2.2;
        var color = core ? 'rgba(255,211,116,0.9)' : entity ? 'rgba(213,134,255,0.8)' : agent ? 'rgba(137,184,255,0.86)' : 'rgba(104,241,204,0.72)';
        var pulse = 1 + Math.sin(now * 0.002 + _vvGraphHash(nodeId) * 12) * 0.18;
        context.beginPath();
        context.arc(x, y, radius * pulse * 2.2, 0, Math.PI * 2);
        context.fillStyle = color.replace(/,[^,]+\)$/, ',0.12)');
        context.fill();
        context.beginPath();
        if (agent) context.rect(x - radius, y - radius, radius * 2, radius * 2);
        else context.arc(x, y, radius, 0, Math.PI * 2);
        context.fillStyle = color;
        context.fill();
      });
    }
    _vv.memoryGraphFrame = requestAnimationFrame(draw);
  }
  _vv.memoryGraphFrame = requestAnimationFrame(draw);
}

async function _vvPollLogStream() {
  if (!_vv.open || _vv._logPolling) return;
  _vv._logPolling = true;
  try {
    var base = (typeof getSafeBridgeBaseUrl === 'function') ? getSafeBridgeBaseUrl() : '';
    if (!base) return;
    var opts = { method: 'GET' };
    if (typeof AbortSignal !== 'undefined' && AbortSignal.timeout) opts.signal = AbortSignal.timeout(2500);
    var resp = await fetch(base.replace(/\/+$/, '') + '/v1/logs?since=' + (_vv._logSince || 0) + '&limit=40', opts);
    if (!resp.ok) return;
    var data = await resp.json();
    var lines = (data && Array.isArray(data.lines)) ? data.lines : [];
    if (typeof data.last === 'number') _vv._logSince = data.last;
    if (!lines.length) return;
    var el = document.getElementById('vvLogStream');
    if (!el) return;
    lines.forEach(function (ln) {
      var div = document.createElement('div');
      div.className = 'vv-log-line';
      div.textContent = String(ln.text || '');
      el.appendChild(div);
    });
    // Cap the rendered backlog so the small corner box stays light.
    while (el.childNodes.length > 24) el.removeChild(el.firstChild);
    el.scrollTop = el.scrollHeight;
  } catch (_) {
    // Bridge unreachable or logs unavailable; stay quiet.
  } finally {
    _vv._logPolling = false;
  }
}

function _vvUpdateHUD() {
  // Model
  var modelEl = document.getElementById('vvHudModel');
  if (modelEl) {
    var sel = document.getElementById('selModel');
    var modelName = sel ? (sel.selectedOptions && sel.selectedOptions[0] ? sel.selectedOptions[0].text : sel.value) : '--';
    if (modelName.length > 16) modelName = modelName.substring(0, 14) + '..';
    modelEl.textContent = modelName;
  }
  // Signal level from mic
  var sigEl = document.getElementById('vvHudSignal');
  if (sigEl) {
    if (_vv.analyser && _vv.dataArray && (_vv.phase === 'listening' || _vv.phase === 'awake')) {
      _vv.analyser.getByteFrequencyData(_vv.dataArray);
      var sum = 0;
      for (var i = 0; i < _vv.dataArray.length; i++) sum += _vv.dataArray[i];
      var avg = sum / _vv.dataArray.length;
      var db = Math.round(20 * Math.log10(Math.max(avg, 1) / 255));
      sigEl.textContent = db + ' dB';
    } else {
      sigEl.textContent = '--';
    }
  }
  // Latency
  var latEl = document.getElementById('vvHudLatency');
  if (latEl) {
    if (_vv.phase === 'thinking' && _vv.cmdStart) {
      latEl.textContent = Math.round(performance.now() - _vv.cmdStart) + ' ms';
    } else if (typeof _netStats !== 'undefined' && _netStats.lastLatency) {
      latEl.textContent = _netStats.lastLatency + ' ms';
    } else {
      latEl.textContent = '-- ms';
    }
  }
  // Live token telemetry replaces the lower-screen hint tidbit
  var telEl = document.getElementById('vvHudTelemetry');
  if (telEl) {
    var ctxTokens = 0, msgCount = 0;
    try { ctxTokens = computeMessagesTokens() || 0; } catch (e) { ctxTokens = 0; }
    try { msgCount = _countAllMessages() || 0; } catch (e) { msgCount = 0; }
    if (ctxTokens > 0 || msgCount > 0) {
      var ctxStr = ctxTokens >= 1000 ? (ctxTokens / 1000).toFixed(1) + 'k' : String(ctxTokens);
      var parts = ['CTX ' + ctxStr + ' tok', msgCount + ' msg'];
      if (typeof _netStats !== 'undefined') {
        parts.push('REQ ' + (_netStats.requests || 0));
        if (_netStats.errors) parts.push('ERR ' + _netStats.errors);
        if (_netStats.lastProvider) parts.push(String(_netStats.lastProvider).toUpperCase());
      }
      telEl.innerHTML = parts.join(' &middot; ');
    } else {
      telEl.innerHTML = 'tap orb to listen &middot; say <em>Eva</em> to wake';
    }
  }
}

// --- Main orb canvas ---

function _vvStartCanvas() {
  var canvas = document.getElementById('voiceViewCanvas');
  if (!canvas) return;
  var ctx = canvas.getContext('2d');
  var w = canvas.width, h = canvas.height;
  var cx = w / 2, cy = h / 2, baseR = w * 0.28;

  function draw() {
    if (!_vv.open) return;
    ctx.clearRect(0, 0, w, h);

    if (!_vv.impulses) _vv.impulses = [];
    if (!_vv.orbits) _vv.orbits = [];

    var t = performance.now() / 1000;
    var phase = _vv.phase;

    // Audio data
    var freqData = null;
    if (_vv.analyser && _vv.dataArray) {
      _vv.analyser.getByteFrequencyData(_vv.dataArray);
      freqData = _vv.dataArray;
    }
    var ttsData = null;
    if (_vv.ttsAnalyser && _vv.ttsDataArray && phase === 'speaking') {
      _vv.ttsAnalyser.getByteFrequencyData(_vv.ttsDataArray);
      ttsData = _vv.ttsDataArray;
    }
    var activeData = ttsData || freqData;

    // Band energies drive organic motion and impulse spawning. Voice lives in
    // the low/mid bins, so we weight those for the overall level.
    var bass = 0, mid = 0, treble = 0, level = 0;
    if (activeData && activeData.length) {
      var an = activeData.length;
      var bEnd = Math.max(1, Math.floor(an * 0.12));
      var mEnd = Math.max(bEnd + 1, Math.floor(an * 0.45));
      var sb = 0; for (var bb = 0; bb < bEnd; bb++) sb += activeData[bb];
      var sm = 0; for (var bm = bEnd; bm < mEnd; bm++) sm += activeData[bm];
      var st = 0; for (var bt = mEnd; bt < an; bt++) st += activeData[bt];
      bass = sb / bEnd / 255;
      mid = sm / (mEnd - bEnd) / 255;
      treble = st / (an - mEnd) / 255;
      level = bass * 0.6 + mid * 0.32 + treble * 0.08;
    }

    // Phase colors
    var hue, sat, light, glowAlpha, ringHue;
    if (phase === 'awake')      { hue = 270; sat = 75; light = 65; glowAlpha = 0.6; ringHue = 265; }
    else if (phase === 'thinking') { hue = 210; sat = 80; light = 60; glowAlpha = 0.5; ringHue = 200; }
    else if (phase === 'speaking') { hue = 155; sat = 65; light = 55; glowAlpha = 0.55; ringHue = 145; }
    else if (phase === 'listening'){ hue = 250; sat = 55; light = 50; glowAlpha = 0.35; ringHue = 240; }
    else if (phase === 'error')    { hue = 0; sat = 60; light = 50; glowAlpha = 0.35; ringHue = 350; }
    else                           { hue = 220; sat = 25; light = 35; glowAlpha = 0.15; ringHue = 215; }

    var col = function(h, s, l, a) { return 'hsla(' + h + ',' + s + '%,' + l + '%,' + a + ')'; };

    // === Background radial glow ===
    var bgGrad = ctx.createRadialGradient(cx, cy, baseR * 0.3, cx, cy, baseR * 2.5);
    bgGrad.addColorStop(0, col(hue, sat, light, glowAlpha * 0.15));
    bgGrad.addColorStop(0.5, col(hue, sat, light * 0.5, glowAlpha * 0.05));
    bgGrad.addColorStop(1, 'transparent');
    ctx.fillStyle = bgGrad;
    ctx.fillRect(0, 0, w, h);

    // === Outer ring 3 (thin, far, slow rotate) ===
    ctx.save();
    ctx.translate(cx, cy);
    ctx.rotate(t * 0.08);
    ctx.beginPath();
    var r3 = baseR * 1.7;
    for (var i = 0; i < 72; i++) {
      var a = (i / 72) * Math.PI * 2;
      var gap = (i % 6 === 0) ? 0.3 : 1;
      if (gap < 1) continue;
      var x1 = Math.cos(a) * (r3 - 1), y1 = Math.sin(a) * (r3 - 1);
      var x2 = Math.cos(a) * (r3 + 1), y2 = Math.sin(a) * (r3 + 1);
      ctx.moveTo(x1, y1);
      ctx.lineTo(x2, y2);
    }
    ctx.strokeStyle = col(ringHue, 35, 32, 0.04);
    ctx.lineWidth = 0.5;
    ctx.stroke();
    ctx.restore();

    // === Outer ring 2 (dashed, counter-rotate) ===
    ctx.save();
    ctx.translate(cx, cy);
    ctx.rotate(-t * 0.15);
    ctx.beginPath();
    ctx.setLineDash([8, 16]);
    ctx.arc(0, 0, baseR * 1.45, 0, Math.PI * 2);
    ctx.strokeStyle = col(ringHue, 35, 32, 0.05);
    ctx.lineWidth = 0.8;
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.restore();

    // === Outer ring 1 (solid, subtle) ===
    ctx.save();
    ctx.translate(cx, cy);
    ctx.rotate(t * 0.25);
    ctx.beginPath();
    ctx.arc(0, 0, baseR * 1.2, 0, Math.PI * 2);
    ctx.strokeStyle = col(ringHue, sat, light * 0.55, 0.06);
    ctx.lineWidth = 1;
    ctx.stroke();
    // Tick marks every 30 deg
    for (var d = 0; d < 12; d++) {
      var ta = (d / 12) * Math.PI * 2;
      ctx.beginPath();
      ctx.moveTo(Math.cos(ta) * baseR * 1.18, Math.sin(ta) * baseR * 1.18);
      ctx.lineTo(Math.cos(ta) * baseR * 1.24, Math.sin(ta) * baseR * 1.24);
      ctx.strokeStyle = col(ringHue, sat, light * 0.55, 0.1);
      ctx.lineWidth = d % 3 === 0 ? 1.5 : 0.7;
      ctx.stroke();
    }
    ctx.restore();

    // === Scanning beam (thinking/awake only) ===
    if (phase === 'thinking' || phase === 'awake') {
      ctx.save();
      ctx.translate(cx, cy);
      var sweepAngle = (t * 1.5) % (Math.PI * 2);
      var sweepGrad = ctx.createConicGradient(sweepAngle, 0, 0);
      sweepGrad.addColorStop(0, col(hue, sat, light, 0.25));
      sweepGrad.addColorStop(0.15, 'transparent');
      sweepGrad.addColorStop(1, 'transparent');
      ctx.beginPath();
      ctx.arc(0, 0, baseR * 1.15, 0, Math.PI * 2);
      ctx.fillStyle = sweepGrad;
      ctx.fill();
      ctx.restore();
    }

    // === Particles ===
    for (var pi = 0; pi < _vv.particles.length; pi++) {
      var p = _vv.particles[pi];
      p.angle += p.speed * 0.008;
      p.dist += p.drift * 0.005;
      if (p.dist < 0.35 || p.dist > 1.3) p.drift = -p.drift;
      var pr = baseR * p.dist * 1.6;
      var px = cx + Math.cos(p.angle + t * 0.1) * pr;
      var py = cy + Math.sin(p.angle + t * 0.1) * pr;
      var pa = p.alpha * (0.5 + 0.5 * Math.sin(t * 2 + pi));
      ctx.beginPath();
      ctx.arc(px, py, p.size, 0, Math.PI * 2);
      ctx.fillStyle = col(hue, sat - 10, light + 20, pa);
      ctx.fill();
    }

    // === Main waveform orb ===
    // The perimeter is deformed by three overlapping influences so the WHOLE
    // ring stays alive (no flat arc): (1) traveling harmonic ripples that orbit
    // the circle continuously, (2) audio energy that is itself rotated around
    // the ring over time so loud bins sweep around instead of pinning to a
    // fixed angle, and (3) a gentle breath. This reads as an organic, living
    // membrane rather than a static spectrum readout.
    var segments = 180;
    var dataLen = (activeData && activeData.length) ? activeData.length : 0;
    var usableBins = dataLen ? Math.max(1, Math.floor(dataLen * 0.6)) : 0;
    var swirl = t * 0.18; // audio sweep rate around the ring
    ctx.beginPath();
    for (var si = 0; si <= segments; si++) {
      var angle = (si / segments) * Math.PI * 2 - Math.PI / 2;
      var pos = si / segments;

      // Audio energy, rotated around the ring and reflected so there is no seam.
      var amp = 0;
      if (usableBins > 0) {
        var swept = pos + swirl;
        var frac = swept - Math.floor(swept);          // 0..1 wrapped
        var refl = frac <= 0.5 ? frac * 2 : (1 - frac) * 2; // 0..1..0, seamless
        var fi = Math.min(usableBins - 1, Math.floor(refl * usableBins));
        amp = (activeData[fi] / 255) * (0.22 + level * 0.5);
      }

      // Traveling harmonic ripples. Different speeds/directions keep every part
      // of the ring in motion; amplitude swells with audio level but never goes
      // fully flat, so the orb always breathes.
      var organic =
        Math.sin(angle * 3 + t * 1.6) * 0.55 +
        Math.sin(angle * 5 - t * 1.1) * 0.30 +
        Math.sin(angle * 8 + t * 2.4) * 0.18 +
        Math.sin(angle * 13 - t * 3.1) * 0.10;
      organic *= (0.022 + level * 0.085 + bass * 0.04);

      var breathe = Math.sin(t * 1.2) * 0.012;
      var pulse = (phase === 'awake' || phase === 'speaking') ? Math.sin(t * 4) * 0.018 : 0;
      var think = (phase === 'thinking') ? Math.sin(t * 5 + angle * 9) * 0.03 : 0;

      var r = baseR * (1 + amp * 0.4 + organic + breathe + pulse + think);
      var x = cx + Math.cos(angle) * r;
      var y = cy + Math.sin(angle) * r;
      if (si === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    }
    ctx.closePath();

    // Orb fill
    var fillGrad = ctx.createRadialGradient(cx, cy - baseR * 0.2, 0, cx, cy, baseR * 1.3);
    fillGrad.addColorStop(0, col(hue, sat, light + 20, 0.1));
    fillGrad.addColorStop(0.5, col(hue, sat, light, 0.04));
    fillGrad.addColorStop(1, 'transparent');
    ctx.fillStyle = fillGrad;
    ctx.fill();

    // Orb stroke (double glow)
    ctx.strokeStyle = col(hue, sat, light, 0.5 + glowAlpha * 0.4);
    ctx.lineWidth = 1.5;
    ctx.shadowColor = col(hue, sat, light, glowAlpha);
    ctx.shadowBlur = 24;
    ctx.stroke();
    ctx.shadowColor = col(hue, sat, light + 10, glowAlpha * 0.5);
    ctx.shadowBlur = 60;
    ctx.stroke();
    ctx.shadowBlur = 0;

    // === Electrical impulses ===
    // Two effects layered for a "the future is now" feel:
    //   1. Radial discharges: jagged lightning that fires outward from the orb
    //      surface, like synapses or energy arcing into the field.
    //   2. Orbit pulses: bright sparks that race along the outer rings leaving a
    //      fading comet trail, so energy is always traveling through the window.
    // Spawn rates scale with the phase and live audio level.
    var dt = Math.min(0.05, Math.max(0.001, t - (_vv._lastT || t)));
    _vv._lastT = t;
    var activity = phase === 'speaking' ? (0.2 + level * 1.1)
                 : phase === 'thinking' ? 0.32
                 : phase === 'awake' ? 0.16
                 : phase === 'listening' ? (0.07 + level * 0.5)
                 : phase === 'error' ? 0.12
                 : 0.03;

    // Spawn radial discharges.
    if (_vv.impulses.length < 16 && Math.random() < activity * 0.32) {
      var ia = Math.random() * Math.PI * 2;
      var nodes = 5 + Math.floor(Math.random() * 4);
      var offs = [];
      for (var ni = 0; ni < nodes; ni++) offs.push((Math.random() - 0.5));
      _vv.impulses.push({
        angle: ia,
        reach: 0.45 + Math.random() * 0.7,   // how far past the orb it travels
        prog: 0,
        speed: 1.6 + Math.random() * 1.8,
        offs: offs,
        width: 0.8 + Math.random() * 1.2
      });
    }
    // Spawn orbit pulses.
    if (_vv.orbits.length < 8 && Math.random() < activity * 0.2) {
      var ringR = (Math.random() < 0.5 ? 1.2 : 1.45) + (Math.random() - 0.5) * 0.1;
      _vv.orbits.push({
        angle: Math.random() * Math.PI * 2,
        radius: ringR,
        dir: Math.random() < 0.5 ? 1 : -1,
        speed: 1.4 + Math.random() * 1.8,
        prog: 0,
        life: 0.7 + Math.random() * 0.6
      });
    }

    // Draw + update radial discharges.
    for (var di = _vv.impulses.length - 1; di >= 0; di--) {
      var im = _vv.impulses[di];
      im.prog += dt * im.speed;
      if (im.prog >= 1) { _vv.impulses.splice(di, 1); continue; }
      var headLen = baseR * (0.04 + im.reach * im.prog);
      var startR = baseR * (1.0 + 0.02 * Math.sin(t * 4 + im.angle));
      var ca = Math.cos(im.angle), sa = Math.sin(im.angle);
      var px0 = cx + ca * startR, py0 = cy + sa * startR;
      var perpX = -sa, perpY = ca;
      var seg = im.offs.length;
      var fade = Math.sin(Math.PI * im.prog); // ramp in then out
      ctx.beginPath();
      ctx.moveTo(px0, py0);
      for (var ii = 0; ii < seg; ii++) {
        var frac2 = (ii + 1) / seg;
        var rr = startR + headLen * frac2;
        var jitter = im.offs[ii] * 10 * (1 - frac2) * (0.6 + level);
        var jx = cx + ca * rr + perpX * jitter;
        var jy = cy + sa * rr + perpY * jitter;
        ctx.lineTo(jx, jy);
      }
      ctx.strokeStyle = col(hue, sat - 5, light + 25, 0.5 * fade);
      ctx.lineWidth = im.width;
      ctx.shadowColor = col(hue, sat, light + 15, 0.7 * fade);
      ctx.shadowBlur = 12;
      ctx.stroke();
      // Bright head spark.
      var hx = cx + ca * (startR + headLen) + perpX * im.offs[seg - 1] * 4;
      var hy = cy + sa * (startR + headLen) + perpY * im.offs[seg - 1] * 4;
      ctx.beginPath();
      ctx.arc(hx, hy, im.width * 1.3, 0, Math.PI * 2);
      ctx.fillStyle = col(hue, sat - 15, 90, 0.8 * fade);
      ctx.fill();
      ctx.shadowBlur = 0;
    }

    // Draw + update orbit pulses (comet trails along the outer rings).
    for (var oi = _vv.orbits.length - 1; oi >= 0; oi--) {
      var ob = _vv.orbits[oi];
      ob.prog += dt * (ob.speed / 6);
      if (ob.prog >= ob.life) { _vv.orbits.splice(oi, 1); continue; }
      var oFade = Math.sin(Math.PI * (ob.prog / ob.life));
      var orbR = baseR * ob.radius;
      var headA = ob.angle + ob.dir * ob.prog * 4.2;
      var trail = 14;
      for (var ti = 0; ti < trail; ti++) {
        var ta2 = headA - ob.dir * ti * 0.045;
        var tAlpha = oFade * (1 - ti / trail) * 0.6;
        if (tAlpha <= 0.01) continue;
        var tx = cx + Math.cos(ta2) * orbR;
        var ty = cy + Math.sin(ta2) * orbR;
        ctx.beginPath();
        ctx.arc(tx, ty, (1 - ti / trail) * 1.8 + 0.3, 0, Math.PI * 2);
        ctx.fillStyle = col(ringHue, sat, light + 25, tAlpha);
        ctx.fill();
      }
      // Bright head with glow.
      var ohx = cx + Math.cos(headA) * orbR;
      var ohy = cy + Math.sin(headA) * orbR;
      ctx.beginPath();
      ctx.arc(ohx, ohy, 2.2, 0, Math.PI * 2);
      ctx.fillStyle = col(ringHue, sat - 10, 92, 0.85 * oFade);
      ctx.shadowColor = col(ringHue, sat, light + 20, 0.8 * oFade);
      ctx.shadowBlur = 14;
      ctx.fill();
      ctx.shadowBlur = 0;
    }

    // === Inner ring (heartbeat) ===
    var innerPulse = 0.55 + Math.sin(t * 2) * 0.02;
    ctx.beginPath();
    ctx.arc(cx, cy, baseR * innerPulse, 0, Math.PI * 2);
    ctx.strokeStyle = col(hue, sat, light, 0.07);
    ctx.lineWidth = 0.5;
    ctx.stroke();

    // === Center dot ===
    var dotR = 3 + Math.sin(t * 3) * 1;
    ctx.beginPath();
    ctx.arc(cx, cy, dotR, 0, Math.PI * 2);
    ctx.fillStyle = col(hue, sat, light + 20, 0.4 + glowAlpha * 0.3);
    ctx.shadowColor = col(hue, sat, light, 0.6);
    ctx.shadowBlur = 15;
    ctx.fill();
    ctx.shadowBlur = 0;

    // === Phase label under orb ===
    var phaseLabel = { idle: '', listening: 'LISTENING', awake: 'AWAKE', thinking: 'PROCESSING', speaking: 'SPEAKING', error: 'MIC ERROR' }[phase] || '';
    if (phaseLabel) {
      ctx.font = '600 10px "SF Mono", "Fira Code", monospace';
      ctx.textAlign = 'center';
      ctx.fillStyle = col(hue, sat, light, 0.4);
      ctx.letterSpacing = '3px';
      ctx.fillText(phaseLabel, cx, cy + baseR * 1.35);
    }

    _vv.animFrame = requestAnimationFrame(draw);
  }

  _vv.animFrame = requestAnimationFrame(draw);
}

function _vvStopCanvas() {
  if (_vv.animFrame) {
    cancelAnimationFrame(_vv.animFrame);
    _vv.animFrame = null;
  }
}

// --- Linear waveform bar (bottom HUD) ---

function _vvStartWaveBar() {
  var canvas = document.getElementById('vvWaveBar');
  if (!canvas) return;
  var ctx = canvas.getContext('2d');
  var w = canvas.width, h = canvas.height;

  function draw() {
    if (!_vv.open) return;
    ctx.clearRect(0, 0, w, h);

    var activeData = null;
    if (_vv.ttsAnalyser && _vv.ttsDataArray && _vv.phase === 'speaking') {
      _vv.ttsAnalyser.getByteFrequencyData(_vv.ttsDataArray);
      activeData = _vv.ttsDataArray;
    } else if (_vv.analyser && _vv.dataArray) {
      _vv.analyser.getByteFrequencyData(_vv.dataArray);
      activeData = _vv.dataArray;
    }

    var bars = 80;
    var barW = w / bars;
    var phase = _vv.phase;
    var hue = phase === 'speaking' ? 155 : phase === 'awake' ? 270 : phase === 'thinking' ? 210 : 220;
    var t = performance.now() / 1000;

    // Center baseline
    var mid = h / 2;

    for (var i = 0; i < bars; i++) {
      var val = 0;
      if (activeData && activeData.length > 0) {
        // Compress sampling toward the low/mid bins (where voice energy lives)
        // so the bars spread the motion across the whole bar instead of leaving
        // the high-frequency right side dead.
        var norm = i / bars;
        var curved = Math.pow(norm, 1.8);
        var fi = Math.floor(curved * activeData.length * 0.6);
        val = activeData[Math.min(activeData.length - 1, fi)] / 255;
      }
      // Always-alive traveling shimmer so the bar feels organic even in silence.
      var shimmer = (Math.sin(t * 1.6 - i * 0.22) * 0.5 + 0.5) * 0.04
                  + Math.abs(Math.sin(t * 0.8 + i * 0.15)) * 0.02;
      val = Math.max(val, shimmer);

      var barH = val * mid * 0.85;
      var x = i * barW + 1;
      var alpha = 0.15 + val * 0.6;

      ctx.fillStyle = 'hsla(' + hue + ',60%,55%,' + alpha + ')';
      ctx.fillRect(x, mid - barH, barW - 2, barH); // top half
      ctx.fillStyle = 'hsla(' + hue + ',60%,55%,' + alpha * 0.5 + ')';
      ctx.fillRect(x, mid, barW - 2, barH * 0.5); // mirror (dimmer)
    }

    // Center line
    ctx.beginPath();
    ctx.moveTo(0, mid);
    ctx.lineTo(w, mid);
    ctx.strokeStyle = 'hsla(' + hue + ',50%,50%,0.08)';
    ctx.lineWidth = 0.5;
    ctx.stroke();

    _vv.waveFrame = requestAnimationFrame(draw);
  }

  _vv.waveFrame = requestAnimationFrame(draw);
}

function _vvStopWaveBar() {
  if (_vv.waveFrame) {
    cancelAnimationFrame(_vv.waveFrame);
    _vv.waveFrame = null;
  }
}

// --- Mic analyser ---

function _vvStartMicAnalyser() {
  if (_vv.analyser) return Promise.resolve();
  var AudioCtx = window.AudioContext || window.webkitAudioContext;
  if (!AudioCtx) return Promise.resolve();

  // Echo cancellation is what makes barge-in possible: it removes Eva's own TTS
  // output (played through the speakers) from the mic signal, so the energy the
  // barge monitor sees while she speaks is mostly the user, not Eva.
  var preferredInputId = getPreferredAudioInputDeviceId();
  return navigator.mediaDevices.getUserMedia(getPreferredMicrophoneConstraints()).catch(function(error) {
    if (!preferredInputId || !/NotFoundError|OverconstrainedError/.test(error && error.name)) throw error;
    localStorage.removeItem('audio_input_device_id');
    _audioDeviceStatus('Selected microphone is unavailable; using the system default.');
    refreshAudioDevicePreferences(false);
    return navigator.mediaDevices.getUserMedia(getPreferredMicrophoneConstraints());
  }).then(function(stream) {
    _vv.micStream = stream;
    if (!_vv.audioCtx) _vv.audioCtx = new AudioCtx();
    if (_vv.audioCtx.state === 'suspended') _vv.audioCtx.resume();
    applyPreferredAudioOutputDevice();
    var source = _vv.audioCtx.createMediaStreamSource(stream);
    _vv.analyser = _vv.audioCtx.createAnalyser();
    _vv.analyser.fftSize = 256;
    _vv.dataArray = new Uint8Array(_vv.analyser.frequencyBinCount);
    source.connect(_vv.analyser);
  }).catch(function(err) {
    console.warn('[VoiceView] Mic access denied:', err.message);
  });
}

function _vvStopMicAnalyser() {
  if (_vv.micStream) {
    _vv.micStream.getTracks().forEach(function(t) { t.stop(); });
    _vv.micStream = null;
  }
  if (_vv.audioCtx && _vv.audioCtx.state === 'running') {
    try { _vv.audioCtx.suspend(); } catch(e) {}
  }
  _vv.analyser = null;
  _vv.dataArray = null;
}

// --- TTS audio analyser ---

function _vvConnectTTSAnalyser() {
  _vvDisconnectTTSAnalyser();
  var audio = document.getElementById('audioPlayback');
  if (!audio) return;

  var AudioCtx = window.AudioContext || window.webkitAudioContext;
  if (!AudioCtx) return;

  if (!_vv.audioCtx) _vv.audioCtx = new AudioCtx();
  if (_vv.audioCtx.state === 'suspended') _vv.audioCtx.resume();
  applyPreferredAudioOutputDevice();
  var ctx = _vv.audioCtx;

  try {
    // createMediaElementSource permanently reroutes the audio element into the
    // Web Audio graph. Connect it to the speakers FIRST so Eva is always
    // audible, even if the analyser wiring below fails. Without this ordering a
    // failure after the source is created leaves the element hijacked but with
    // no path to the speakers, which silences all TTS until a reload.
    if (!audio._vvSource) {
      audio._vvSource = ctx.createMediaElementSource(audio);
    }
    _vv.ttsSource = audio._vvSource;
    try { _vv.ttsSource.connect(ctx.destination); } catch (e) {}

    _vv.ttsAnalyser = ctx.createAnalyser();
    _vv.ttsAnalyser.fftSize = 256;
    // Lower smoothing than the 0.8 default so the bars track speech onsets and
    // pauses crisply instead of lagging behind and mushing together.
    _vv.ttsAnalyser.smoothingTimeConstant = 0.6;
    _vv.ttsDataArray = new Uint8Array(_vv.ttsAnalyser.frequencyBinCount);

    // The analyser taps the signal at the graph node, which is BEFORE the
    // device output latency (often 80-200ms on Linux/PulseAudio). Reading it
    // directly makes the waveform run AHEAD of the audio you hear. Route the
    // analyser branch (only) through a DelayNode set to the output latency so
    // the visualization lines up with the heard voice. The speaker path above
    // stays undelayed.
    var lat = ctx.outputLatency || ctx.baseLatency || 0;
    lat = Math.min(0.3, Math.max(0, lat));
    _vv.ttsDelay = ctx.createDelay(0.5);
    _vv.ttsDelay.delayTime.value = lat;
    _vv.ttsSource.connect(_vv.ttsDelay);
    _vv.ttsDelay.connect(_vv.ttsAnalyser);

    // outputLatency is often 0 until playback actually starts. Refine the
    // compensation once the device reports a real value.
    audio.addEventListener('playing', function _vvSyncDelay() {
      audio.removeEventListener('playing', _vvSyncDelay);
      if (!_vv.ttsDelay || !_vv.audioCtx) return;
      var rl = _vv.audioCtx.outputLatency || _vv.audioCtx.baseLatency || 0;
      rl = Math.min(0.3, Math.max(0, rl));
      try { _vv.ttsDelay.delayTime.value = rl; } catch (e) {}
    });
  } catch(e) {
    _vv.ttsAnalyser = null;
    _vv.ttsDataArray = null;
    _vv.ttsDelay = null;
    // Recovery: guarantee the element can still reach the speakers.
    try {
      if (audio._vvSource && _vv.audioCtx) audio._vvSource.connect(_vv.audioCtx.destination);
    } catch (e2) {}
  }
}

function _vvDisconnectTTSAnalyser() {
  // Tear down the analyser branch (source -> delay -> analyser). The speaker
  // path (source -> destination) is left intact so audio keeps playing.
  if (_vv.ttsSource && _vv.ttsDelay) {
    try { _vv.ttsSource.disconnect(_vv.ttsDelay); } catch(e) {}
  }
  if (_vv.ttsDelay && _vv.ttsAnalyser) {
    try { _vv.ttsDelay.disconnect(_vv.ttsAnalyser); } catch(e) {}
  }
  if (_vv.ttsSource && _vv.ttsAnalyser) {
    try { _vv.ttsSource.disconnect(_vv.ttsAnalyser); } catch(e) {}
  }
  _vv.ttsAnalyser = null;
  _vv.ttsDataArray = null;
  _vv.ttsDelay = null;
}

// --- Voice-mode asset surface ---

// Show images (or other media) in the voice view's asset window so the user
// can see what Eva surfaced without leaving the orb overlay.
function _vvSurfaceAssets(assets) {
  if (!assets || !assets.length) return;
  var panel = document.getElementById('vvAssets');
  var body = document.getElementById('vvAssetsBody');
  if (!panel || !body) return;

  body.innerHTML = '';
  assets.forEach(function(a) {
    if (!a || !a.url) return;
    var img = document.createElement('img');
    img.className = 'eva-inline-img';
    img.src = a.url;
    img.alt = a.caption || 'Image';
    body.appendChild(img);
    if (a.caption) {
      var cap = document.createElement('div');
      cap.className = 'vv-assets-caption';
      cap.textContent = a.generated ? a.caption + ' (AI generated)' : a.caption;
      body.appendChild(cap);
    }
  });

  panel.classList.add('open');
  panel.setAttribute('aria-hidden', 'false');
}

function _vvHideAssets() {
  var panel = document.getElementById('vvAssets');
  var body = document.getElementById('vvAssetsBody');
  if (panel) {
    panel.classList.remove('open');
    panel.setAttribute('aria-hidden', 'true');
  }
  if (body) body.innerHTML = '';
}


// --- Voice recognition ---

function _vvGetEndpoint() {
  if (_vv.endpoint || typeof VoiceEndpoint === 'undefined') return _vv.endpoint;
  _vv.endpoint = new VoiceEndpoint({
    delayMs: _vv.endpointDelayMs,
    onCommit: function(transcript) { _vvHandleTranscript(transcript); },
    onEvent: function(event) {
      if (event.type === 'merged' || event.type === 'duplicate') {
        try {
          var ring = JSON.parse(localStorage.getItem('voice_endpoint_events') || '[]');
          if (!Array.isArray(ring)) ring = [];
          ring.push({ ts: Date.now(), type: event.type, provider: event.provider || '', fragments: event.fragments || 0 });
          localStorage.setItem('voice_endpoint_events', JSON.stringify(ring.slice(-100)));
        } catch (_) {}
      }
    }
  });
  return _vv.endpoint;
}

function _vvQueueTranscript(transcript, provider) {
  var endpoint = _vvGetEndpoint();
  if (!endpoint) {
    _vvHandleTranscript(transcript);
    return;
  }
  endpoint.setDelay(_vv.endpointDelayMs);
  endpoint.accept(transcript, {
    provider: provider || '',
    delayMs: provider === 'local' ? Math.min(800, _vv.endpointDelayMs) : _vv.endpointDelayMs
  });
}

function _vvToggleListening() {
  // Note: the desktop agent guard was removed because it also blocked the real
  // user from clicking the orb after an agent task completed. The agent's
  // pyautogui clicks are distinguished by EvaDesktop._isAgentClick() instead.
  if (_vv.recognition || _vv.whisperMode) {
    _vvStopListening();
  } else {
    _vvStartListening();
  }
}

function _vvStartListening() {
  if (window.evaStandalone && window.evaStandalone.isStandalone) {
    _vvStartWhisperListening('local');
    return;
  }

  // Browser SpeechRecognition always follows the operating system default
  // microphone. A selected device therefore needs the MediaRecorder path,
  // which carries getUserMedia's deviceId constraint through to Whisper.
  if (getPreferredAudioInputDeviceId()) {
    var whisperKey = typeof getAuthKey === 'function' ? getAuthKey('OPENAI_API_KEY') : '';
    if (whisperKey) {
      _vvStartWhisperListening('openai');
      return;
    }
    _audioDeviceStatus('Selected microphone needs an OpenAI key outside Standalone; using the system-default microphone.');
  }

  var SpeechRec = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRec) {
    _vvStartWhisperListening();
    return;
  }

  if (typeof stopVoiceListener === 'function') stopVoiceListener();

  _vvStartMicAnalyser();

  _vv.recognition = new SpeechRec();
  _vv.recognition.lang = 'en-US';
  _vv.recognition.continuous = true;
  _vv.recognition.interimResults = false;

  _vv.recognition.onstart = function() {
    _vvSetStatus('listening');
  };

  _vv.recognition.onresult = function(event) {
    _vv._lastResultTime = Date.now();
    for (var i = event.resultIndex; i < event.results.length; i++) {
      if (!event.results[i].isFinal) continue;
      _vvQueueTranscript(event.results[i][0].transcript.trim(), 'browser');
    }
  };

  _vv.recognition.onerror = function(event) {
    if (event.error === 'no-speech' || event.error === 'aborted') return;
    if (event.error === 'not-allowed') {
      _vvSetStatus('error');
      _vv.recognition = null;
      return;
    }
    if (event.error === 'network' || event.error === 'service-not-allowed') {
      console.warn('[VoiceView] Web Speech API unavailable (' + event.error + '), falling back to Whisper');
      _vv.recognition = null;
      _vvStartWhisperListening();
      return;
    }
  };

  _vv.recognition.onend = function() {
    if (_vv.recognition && _vvIsActive()) {
      // Browser stopped recognition (normal after silence/timeout). Restart.
      _vv._restartAttempts = (_vv._restartAttempts || 0) + 1;
      var delay = Math.min(300 * _vv._restartAttempts, 2000);
      setTimeout(function() {
        if (!_vv.recognition || !_vvIsActive()) return;
        try {
          _vv.recognition.start();
          _vv._restartAttempts = 0; // reset on success
        } catch(e) {
          console.warn('[VoiceView] Recognition restart failed (attempt ' + _vv._restartAttempts + '):', e);
          if (_vv._restartAttempts >= 5) {
            // Too many failures, fall back to Whisper if available
            console.warn('[VoiceView] Falling back to Whisper after repeated restart failures');
            _vv.recognition = null;
            _vv._restartAttempts = 0;
            if (typeof _vvStartWhisperListening === 'function') {
              _vvStartWhisperListening();
            } else {
              _vvSetStatus('error');
            }
          }
        }
      }, delay);
    }
  };

  _vv.recognition.start();
  _vv._lastResultTime = Date.now();
  _vv._restartAttempts = 0;

  // Watchdog: if no recognition results for 60s while the view is open and
  // not in speaking/thinking phase, force-restart recognition. The browser
  // sometimes silently stops delivering results without firing onend.
  if (_vv._watchdog) clearInterval(_vv._watchdog);
  _vv._watchdog = setInterval(function() {
    if (!_vvIsActive() || !_vv.recognition) {
      clearInterval(_vv._watchdog);
      _vv._watchdog = null;
      return;
    }
    if (_vv.phase === 'speaking' || _vv.phase === 'thinking') return;
    var elapsed = Date.now() - (_vv._lastResultTime || Date.now());
    if (elapsed > 60000) {
      console.warn('[VoiceView] Watchdog: no results for 60s, restarting recognition');
      try { _vv.recognition.stop(); } catch(_) {}
      setTimeout(function() {
        if (_vv.recognition && _vvIsActive()) {
          try { _vv.recognition.start(); _vv._lastResultTime = Date.now(); } catch(_) {}
        }
      }, 500);
    }
  }, 15000);
}

function _vvStopListening() {
  _vv.listenGeneration += 1;
  if (_vv.awakeTimer) { clearTimeout(_vv.awakeTimer); _vv.awakeTimer = null; }
  if (_vv.silenceTimer) { clearTimeout(_vv.silenceTimer); _vv.silenceTimer = null; }
  if (_vv.recordingCap) { clearTimeout(_vv.recordingCap); _vv.recordingCap = null; }
  if (_vv._watchdog) { clearInterval(_vv._watchdog); _vv._watchdog = null; }
  if (_vv._whisperWatchdog) { clearInterval(_vv._whisperWatchdog); _vv._whisperWatchdog = null; }
  if (_vv._energyMonitor) { clearInterval(_vv._energyMonitor); _vv._energyMonitor = null; }
  if (_vv.endpoint) _vv.endpoint.reset();
  if (_vv.recognition) {
    var rec = _vv.recognition;
    _vv.recognition = null;
    try { rec.stop(); } catch(e) {}
  }
  if (_vv.mediaRecorder) {
    try { _vv.mediaRecorder.stop(); } catch(e) {}
  }
  if (_vv._capture) {
    if (_vv._capture.silenceTimer) clearTimeout(_vv._capture.silenceTimer);
    if (_vv._capture.recordingCap) clearTimeout(_vv._capture.recordingCap);
  }
  _vv.whisperMode = false;
  _vv.whisperProvider = '';
  _vv._whisperInflight = false;
  _vv.audioChunks = [];
  _vv.speechDetected = false;
  _vvStopAck();
  _vvStopMicAnalyser();
  _vvDisconnectTTSAnalyser();
  if (_vvIsActive()) _vvSetStatus('idle');
}

// --- Whisper fallback ---

function _vvStartWhisperListening(provider) {
  if (typeof stopVoiceListener === 'function') stopVoiceListener();

  _vv.whisperProvider = provider || 'openai';
  _vv.listenGeneration += 1;
  var generation = _vv.listenGeneration;
  _vv.whisperMode = true;
  _vv._whisperInflight = false;
  _vvSetStatus('listening');

  function beginRecording() {
    if (generation !== _vv.listenGeneration || !_vvIsActive() || !_vv.whisperMode) return;
    _vvStartMicAnalyser().then(function() {
      if (generation !== _vv.listenGeneration || !_vvIsActive() || !_vv.whisperMode) {
        _vvStopMicAnalyser();
        return;
      }
      _vvWhisperRecord();
    });
  }

  if (_vv.whisperProvider === 'local') {
    if (!window.evaStandalone || typeof window.evaStandalone.localVoicesStart !== 'function') {
      _vvSetStatus('error');
      return;
    }
    window.evaStandalone.localVoicesStart('', '').then(function() {
      _vvWarmAcknowledgements();
      beginRecording();
    }).catch(function(error) {
      if (generation !== _vv.listenGeneration || !_vvIsActive() || !_vv.whisperMode) return;
      console.warn('[VoiceView] Local transcription unavailable:', error && error.message ? error.message : error);
      _vvSetStatus('error');
    });
  } else {
    beginRecording();
  }

  // Whisper recording loop watchdog: if the loop has stalled (no active
  // recorder and no API call in flight) while we should be listening, restart.
  if (_vv._whisperWatchdog) clearInterval(_vv._whisperWatchdog);
  _vv._whisperWatchdog = setInterval(function() {
    if (!_vvIsActive() || !_vv.whisperMode) {
      clearInterval(_vv._whisperWatchdog); _vv._whisperWatchdog = null;
      return;
    }
    if (_vv.phase === 'thinking') return;
    var recActive = _vv.mediaRecorder && _vv.mediaRecorder.state === 'recording';
    if (!recActive && !_vv._whisperInflight) {
      console.warn('[VoiceView] transcription recording loop stalled, restarting');
      _vvWhisperRecord();
    }
  }, 10000);
}

function _vvWhisperRecord() {
  if (!_vvIsActive() || !_vv.whisperMode || !_vv.micStream) return;
  if (_vv.phase === 'thinking') return;
  // A recorder remains owned until its onstop callback has drained its final
  // chunks. MediaRecorder changes to inactive before that callback, so checking
  // state alone would allow a second capture to overwrite the first one.
  if (_vv._capture) return;

  var mimeType = 'audio/webm';
  if (typeof MediaRecorder !== 'undefined' && MediaRecorder.isTypeSupported) {
    if (MediaRecorder.isTypeSupported('audio/webm;codecs=opus')) mimeType = 'audio/webm;codecs=opus';
  }

  var capture = {
    generation: _vv.listenGeneration,
    chunks: [],
    speechDetected: false,
    silenceTimer: null,
    recordingCap: null,
    recorder: null
  };

  try {
    capture.recorder = new MediaRecorder(_vv.micStream, { mimeType: mimeType });
  } catch(e) {
    _vvSetStatus('error');
    return;
  }

  _vv._capture = capture;
  _vv.mediaRecorder = capture.recorder;
  _vv.audioChunks = capture.chunks;
  _vv.speechDetected = false;

  capture.recorder.ondataavailable = function(e) {
    if (e.data && e.data.size > 0) capture.chunks.push(e.data);
  };

  capture.recorder.onstop = function() {
    if (capture.recordingCap) clearTimeout(capture.recordingCap);
    if (capture.silenceTimer) clearTimeout(capture.silenceTimer);
    var ownsCapture = _vv._capture === capture;
    if (ownsCapture) {
      _vv._capture = null;
      _vv.mediaRecorder = null;
      _vv.recordingCap = null;
      _vv.silenceTimer = null;
    }
    if (capture.generation !== _vv.listenGeneration || !_vvIsActive() || !_vv.whisperMode) {
      // A fast stop/start leaves the old recorder pending onstop. Release it
      // first, then immediately arm the newer listening generation.
      if (ownsCapture && _vvIsActive() && _vv.whisperMode && _vv.phase !== 'thinking') {
        setTimeout(function() { _vvWhisperRecord(); }, 0);
      }
      return;
    }
    if (!capture.speechDetected || !capture.chunks.length) {
      if (_vvIsActive() && _vv.whisperMode) setTimeout(function() { _vvWhisperRecord(); }, 200);
      return;
    }
    var blob = new Blob(capture.chunks, { type: mimeType });
    capture.chunks = [];
    _vvWhisperTranscribe(blob);
  };

  capture.recorder.start(250);

  capture.recordingCap = setTimeout(function() {
    capture.recordingCap = null;
    if (capture.recorder.state === 'recording') {
      try { capture.recorder.stop(); } catch(e) {}
    }
  }, 30000);
  _vv.recordingCap = capture.recordingCap;

  _vvWhisperMonitor(capture);
}

function _vvWhisperMonitor(capture) {
  if (!_vvIsActive() || !_vv.whisperMode || !_vv.analyser || !_vv.dataArray) return;

  // A fixed high threshold can animate the waveform without ever qualifying
  // ordinary near-field speech for transcription, especially on laptop mics.
  var threshold = 12;
  var silenceDelay = _vv.liveTranslation ? 650 : _vv.endpointDelayMs;

  // Use setInterval instead of requestAnimationFrame so that Electron does not
  // throttle the energy monitor to 0 fps when the window is unfocused.
  if (_vv._energyMonitor) clearInterval(_vv._energyMonitor);
  _vv._energyMonitor = setInterval(function() {
    if (!_vvIsActive() || !_vv.whisperMode || _vv._capture !== capture || !_vv.analyser || !_vv.dataArray) {
      clearInterval(_vv._energyMonitor); _vv._energyMonitor = null; return;
    }
    if (_vv.phase === 'thinking') return;
    if (capture.recorder.state !== 'recording') return;

    _vv.analyser.getByteFrequencyData(_vv.dataArray);
    var sum = 0;
    for (var i = 0; i < _vv.dataArray.length; i++) sum += _vv.dataArray[i];
    var avg = sum / _vv.dataArray.length;

    if (avg > threshold) {
      capture.speechDetected = true;
      _vv.speechDetected = true;
      if (capture.silenceTimer) { clearTimeout(capture.silenceTimer); capture.silenceTimer = null; }
    } else if (capture.speechDetected && !capture.silenceTimer) {
      capture.silenceTimer = setTimeout(function() {
        capture.silenceTimer = null;
        if (capture.recorder.state === 'recording') {
          try { capture.recorder.stop(); } catch(e) {}
        }
      }, silenceDelay);
      _vv.silenceTimer = capture.silenceTimer;
    }
  }, 100);
}

function _vvWhisperTranscribe(blob) {
  var generation = _vv.listenGeneration;
  var phaseAtStart = _vv.phase;
  _vv._whisperInflight = true;
  var request;
  if (_vv.whisperProvider === 'local') {
    request = blob.arrayBuffer().then(function(audio) {
      return window.evaStandalone.localSpeechTranscribe(audio, blob.type || 'audio/webm;codecs=opus', getLocalVoicesLanguage(), _vv.liveTranslation && _vvUseMultilingualTranslationStt());
    });
  } else {
    var apiKey = typeof getAuthKey === 'function' ? getAuthKey('OPENAI_API_KEY') : null;
    if (!apiKey) {
      _vv._whisperInflight = false;
      _vvSetStatus('error');
      return;
    }
    var formData = new FormData();
    formData.append('file', blob, 'audio.webm');
    formData.append('model', 'whisper-1');
    var whisperLanguage = getLocalVoicesLanguage();
    if (whisperLanguage !== 'auto') formData.append('language', whisperLanguage);
    var controller = new AbortController();
    var fetchTimeout = setTimeout(function() { controller.abort(); }, 20000);
    request = fetch('https://api.openai.com/v1/audio/transcriptions', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer ' + apiKey },
      body: formData,
      signal: controller.signal
    }).then(function(res) {
      if (!res.ok) throw new Error('Whisper API returned ' + res.status);
      return res.json();
    }).finally(function() { clearTimeout(fetchTimeout); });
  }

  request.then(function(data) {
    if (generation !== _vv.listenGeneration || !_vv.whisperMode) return;
    _vv._whisperInflight = false;
    if (data.text && data.text.trim()) {
      // Local STT returns complete utterances after VAD. Buffering those again
      // can lose the wake phrase when voice lifecycle state changes, so retain
      // the established direct dispatch path for standalone transcription.
      if (_vv.whisperProvider === 'local') {
        _vvHandleTranscript(data.text.trim());
      } else {
        _vvQueueTranscript(data.text.trim(), _vv.whisperProvider);
      }
    } else {
      var emptyTranscriptEl = document.getElementById('vvTranscript');
      if (emptyTranscriptEl) emptyTranscriptEl.textContent = 'I did not catch that.';
    }
    if (_vvIsActive() && _vv.whisperMode && _vv.phase !== 'thinking') {
      _vvWhisperRecord();
    }
  }).catch(function(err) {
    if (generation !== _vv.listenGeneration || !_vv.whisperMode) return;
    _vv._whisperInflight = false;
    var message = err && err.message ? err.message : String(err);
    // A rejected or empty recording is normal during interruption and should
    // return Eva to capture immediately instead of stranding the conversation.
    if (/HTTP 400/.test(message)) {
      // Do not steal the state from an active response: a failed background
      // capture while Eva is speaking/thinking must not silence or bypass it.
      if (_vv.phase === 'speaking') {
        if (_vvIsActive() && _vv.whisperMode) setTimeout(function() { _vvWhisperRecord(); }, 200);
        return;
      }
      if (_vv.phase === 'thinking') return;
      if (_vv.phase === 'awake') {
        _vvEnterAwake(_vv.convoMode ? _vv.convoTimeoutMs : 10000);
      } else if (phaseAtStart === 'awake' && _vv.convoMode) {
        _vvEnterAwake(_vv.convoTimeoutMs);
      } else {
        _vvSetStatus('listening');
      }
      if (_vvIsActive() && _vv.whisperMode) setTimeout(function() { _vvWhisperRecord(); }, 200);
      return;
    }
    console.warn('[VoiceView] transcription error:', message);
    if (_vvIsActive() && _vv.whisperMode) setTimeout(function() { _vvWhisperRecord(); }, 1000);
  });
}

// --- Transcript + command handling ---

// Reflect conversation-mode state into the voice-view controls and bind their
// change handlers (idempotent; safe to call on each open).
function _vvSyncConvoControls() {
  var toggle = document.getElementById('vvConvoToggle');
  var timeoutSel = document.getElementById('vvConvoTimeout');
  if (toggle) {
    toggle.checked = !!_vv.convoMode;
    if (!toggle._vvBound) {
      toggle._vvBound = true;
      toggle.addEventListener('change', function() {
        _vv.convoMode = !!toggle.checked;
        try { localStorage.setItem('vvConvoMode', _vv.convoMode ? '1' : '0'); } catch (e) {}
        // Apply immediately if currently idling between turns.
        if (_vvIsActive()) {
          if (_vv.convoMode && _vv.phase === 'listening') {
            _vvEnterAwake(_vv.convoTimeoutMs);
          } else if (!_vv.convoMode && _vv.phase === 'awake') {
            if (_vv.awakeTimer) { clearTimeout(_vv.awakeTimer); _vv.awakeTimer = null; }
            _vvSetStatus('listening');
          }
        }
      });
    }
  }
  if (timeoutSel) {
    timeoutSel.value = String(_vv.convoTimeoutMs);
    if (!timeoutSel._vvBound) {
      timeoutSel._vvBound = true;
      timeoutSel.addEventListener('change', function() {
        var ms = parseInt(timeoutSel.value, 10);
        if (Number.isInteger(ms) && ms >= 5000 && ms <= 300000) {
          _vv.convoTimeoutMs = ms;
          try { localStorage.setItem('vvConvoTimeoutMs', String(ms)); } catch (e) {}
          if (_vvIsActive() && _vv.phase === 'awake') _vvEnterAwake(ms);
        }
      });
    }
  }
}

var _VV_TRANSLATION_TARGETS = {
  en: { label: 'English', locale: 'en-US' },
  ko: { label: 'Korean', locale: 'ko-KR' },
  es: { label: 'Spanish', locale: 'es-ES' },
  uk: { label: 'Ukrainian', locale: 'uk-UA' }
};
var _VV_LIVE_TRANSLATION_TIMEOUT_MS = 12000;

function _vvSyncLiveTranslationControls() {
  var settingsTarget = document.getElementById('liveTranslationTarget');
  if (settingsTarget) settingsTarget.value = getLiveTranslationTarget();
}

function _vvTranslationTarget() {
  return _VV_TRANSLATION_TARGETS[getLiveTranslationTarget()] || _VV_TRANSLATION_TARGETS.en;
}

function _vvUseMultilingualTranslationStt() {
  var configuredLanguage = getLocalVoicesLanguage();
  if (configuredLanguage === 'ko') return true;
  if (configuredLanguage === 'en') return false;
  // In automatic mode, the translated-to language is the best available hint
  // about the source language. English-to-Korean is common and can use the
  // faster English model; other travel translation defaults to multilingual.
  return getLiveTranslationTarget() !== 'ko';
}

function _vvSpeakBrowser(text, locale) {
  if (!text || !window.speechSynthesis || typeof window.SpeechSynthesisUtterance === 'undefined') return;
  try {
    window.speechSynthesis.cancel();
    var utterance = new SpeechSynthesisUtterance(text);
    utterance.lang = locale || 'en-US';
    var voices = window.speechSynthesis.getVoices ? window.speechSynthesis.getVoices() : [];
    var matchingVoice = voices.find(function(voice) { return voice.lang && voice.lang.toLowerCase().indexOf(utterance.lang.slice(0, 2).toLowerCase()) === 0; });
    if (matchingVoice) utterance.voice = matchingVoice;
    utterance.rate = 1.16;
    utterance.pitch = 1;
    window.speechSynthesis.speak(utterance);
  } catch (_) {}
}

function _vvSpeakLiveStatus(text) {
  _vvSpeakBrowser(text, 'en-US');
}

function _vvUpdateLiveTranslationHint() {
  var hint = document.getElementById('vvHudTelemetry');
  if (!hint) return;
  if (_vv.liveTranslation) {
    hint.textContent = 'live translation | listening | output: ' + _vvTranslationTarget().label;
  } else {
    hint.innerHTML = 'tap orb to listen &middot; say <em>Eva</em> to wake &middot; talk over her to redirect';
  }
}

function _vvSetLiveTranslation(enabled, silent) {
  enabled = !!enabled;
  if (_vv.liveTranslation === enabled) {
    _vvSyncLiveTranslationControls();
    return;
  }
  _vv.liveTranslation = enabled;
  _vv.liveTranslationRun += 1;
  if (_vv.liveTranslationAbort) {
    try { _vv.liveTranslationAbort.abort(); } catch (_) {}
    _vv.liveTranslationAbort = null;
  }
  _vvSyncLiveTranslationControls();
  _vvUpdateLiveTranslationHint();
  if (!enabled) {
    if (window.speechSynthesis) { try { window.speechSynthesis.cancel(); } catch (_) {} }
    if (!silent && _vvIsActive()) _vvSpeakLiveStatus('Live translation off. Returning to Eva voice.');
    if (!silent && _vvIsActive() && (_vv.recognition || _vv.whisperMode)) {
      _vvStopListening();
      _vvStartListening();
    }
    return;
  }
  if (!_vvIsActive()) return;
  _vvStopListening();
  _vv.liveTranslation = true;
  _vvSyncLiveTranslationControls();
  if (!silent) _vvSpeakLiveStatus('Live translation on. Speaking ' + _vvTranslationTarget().label + '.');
  if (!silent && getLiveTranslationModel() !== getResolvedLiveTranslationModel()) {
    var transcriptEl = document.getElementById('vvTranscript');
    if (transcriptEl) transcriptEl.textContent = 'OpenAI key unavailable. Using Eva backend.';
  }
  if (window.evaStandalone && typeof window.evaStandalone.localSpeechWarmTranslation === 'function') {
    window.evaStandalone.localSpeechWarmTranslation(_vvUseMultilingualTranslationStt()).catch(function() {});
  }
  _vvStartWhisperListening(window.evaStandalone && window.evaStandalone.isStandalone ? 'local' : 'openai');
}

function _vvTranslationCommand(transcript) {
  var text = String(transcript || '').toLowerCase();
  var stop = /\b(?:stop|end|disable|turn off|exit)\b[^.!?]{0,30}\b(?:live|real[ -]?time)?\s*(?:translation|translate)\b/.test(text);
  var start = /\b(?:live|real[ -]?time)\s+(?:translation|translate)\b|\blisten\s+and\s+translate\b|\b(?:start|enable|turn on|switch to|begin)\b[^.!?]{0,30}\b(?:translation|translate)\b/.test(text);
  if (stop) return false;
  if (start) return true;
  return null;
}

function _vvTranslateLiveTranscript(transcript) {
  var source = String(transcript || '').trim();
  if (!source || !_vv.liveTranslation) return;
  var target = _vvTranslationTarget();
  var runId = _vv.liveTranslationRun;
  if (_vv.liveTranslationAbort) {
    try { _vv.liveTranslationAbort.abort(); } catch (_) {}
  }
  var controller = typeof AbortController === 'undefined' ? null : new AbortController();
  var timeout = controller ? setTimeout(function() { controller.abort(); }, _VV_LIVE_TRANSLATION_TIMEOUT_MS) : null;
  _vv.liveTranslationAbort = controller;
  var bridgeUrl = typeof getACPBridgeUrl === 'function' ? getACPBridgeUrl() : 'http://localhost:8888';
  fetch(bridgeUrl.replace(/\/+$/, '') + '/v1/translate', {
    method: 'POST',
    headers: getBridgeCapabilityHeaders(),
    body: JSON.stringify({
      input: source,
      target_language: target.label,
      model: getResolvedLiveTranslationModel() === 'aig' ? ((document.getElementById('selAIGBackend') || {}).value || 'gpt-5.6-luna') : getResolvedLiveTranslationModel(),
      lmstudio_base_url: typeof getLmStudioBaseUrl === 'function' ? getLmStudioBaseUrl() : '',
      lmstudio_model: typeof getLmStudioModel === 'function' ? getLmStudioModel() : '',
      github_pat: typeof getAuthKey === 'function' ? getAuthKey('GITHUB_PAT') : '',
      openai_api_key: typeof getAuthKey === 'function' ? getAuthKey('OPENAI_API_KEY') : ''
    }),
    signal: controller ? controller.signal : undefined
  }).then(function(response) {
    if (response.ok) return response.json();
    return response.json().catch(function() { return {}; }).then(function(data) {
      var detail = data && data.error && data.error.message ? data.error.message : 'Translation request returned ' + response.status;
      throw new Error(detail);
    });
  }).then(function(data) {
    if (!_vv.liveTranslation || runId !== _vv.liveTranslationRun) return;
    var translated = (((data.choices || [])[0] || {}).message || {}).content || '';
    translated = String(translated).replace(/^\s*["“]|["”]\s*$/g, '').trim();
    if (!translated) return;
    var transcriptEl = document.getElementById('vvTranscript');
    if (transcriptEl) transcriptEl.textContent = translated;
    _vvSpeakBrowser(translated, target.locale);
  }).catch(function(error) {
    if (error && error.name === 'AbortError') return;
    if (_vv.liveTranslation && runId === _vv.liveTranslationRun) {
      var transcriptEl = document.getElementById('vvTranscript');
      if (transcriptEl) transcriptEl.textContent = 'Translation unavailable: ' + String(error && error.message ? error.message : error).slice(0, 140);
    }
  }).finally(function() {
    if (timeout) clearTimeout(timeout);
    if (_vv.liveTranslationAbort === controller) _vv.liveTranslationAbort = null;
  });
}

// Enter the 'awake' conversation window. While awake, the user can speak follow
// ups without repeating the wake word. After timeoutMs of no speech we fall back
// to 'listening' (standby), which requires saying "Eva" again.
function _vvEnterAwake(timeoutMs) {
  _vvSetStatus('awake');
  if (_vv.awakeTimer) { clearTimeout(_vv.awakeTimer); _vv.awakeTimer = null; }
  _vv.awakeTimer = setTimeout(function() {
    _vv.awakeTimer = null;
    if (_vv.phase === 'awake') _vvSetStatus('listening');
  }, timeoutMs || 10000);
}

// Called when a turn completes. In conversation mode Eva stays awake for a
// follow-up; otherwise she returns to standby and waits for the wake word.
function _vvAfterTurn() {
  _vv._thinkingStart = null;
  if (!_vvIsActive()) return;
  if (!(_vv.recognition || _vv.whisperMode)) { _vvSetStatus('idle'); return; }
  if (_vv.convoMode) {
    _vvEnterAwake(_vv.convoTimeoutMs);
  } else {
    _vvSetStatus('listening');
  }
  if (_vv.whisperMode) _vvWhisperRecord();
}

// --- Barge-in (interrupt Eva while she speaks) ---

// Hard-stop any TTS playback. Pausing the audio element silences the network
// voices; cancel() stops the browser SpeechSynthesis engine.
function _vvStopTTS() {
  // Cancel any in-flight chunked playback so queued sentences do not resume.
  if (typeof _ttsChunk !== 'undefined') { _ttsChunk.runId += 1; _ttsChunk.cancelled = true; _ttsChunk.active = false; }
  var audio = document.getElementById('audioPlayback');
  if (audio) {
    try { audio.pause(); } catch (e) {}
    try { audio.currentTime = 0; } catch (e) {}
  }
  if (window.speechSynthesis) { try { window.speechSynthesis.cancel(); } catch (e) {} }
}

// Invoked when the barge monitor detects the user talking over Eva. Routes the
// speaking phase through its finalizer with the barged flag so Eva goes quiet
// and immediately opens a conversation window to catch the redirect.
function _vvBargeIn() {
  if (_vv.phase !== 'speaking') return;
  if (typeof _vv._finishSpeaking === 'function') {
    _vv._finishSpeaking(true);
  } else {
    // Fallback: finishSpeaking not set (stale state). Force recovery.
    _vvStopTTS();
    _vvStopBargeMonitor();
    _vvAfterBarge();
  }
}

// After a barge-in, open the conversation window (no wake word needed) and arm
// capture so the user's redirect is heard right away.
function _vvAfterBarge() {
  if (!_vvIsActive()) return;
  if (!(_vv.recognition || _vv.whisperMode)) { _vvSetStatus('idle'); return; }
  // Re-arm speech recognition in case the browser killed it during TTS
  if (_vv.recognition) {
    try { _vv.recognition.stop(); } catch (_) {}
    setTimeout(function() {
      if (_vvIsActive()) {
        try { _vv.recognition.start(); } catch (_) {}
      }
    }, 200);
  }
  _vvEnterAwake(_vv.convoTimeoutMs);
  if (_vv.whisperMode) _vvWhisperRecord();
}

// Local STT keeps an active recorder while Eva speaks. With echo cancellation
// enabled, sustained microphone energy is a practical low-latency barge signal:
// stop playback now, then let the same recording finish and transcribe the
// user's redirect after silence. Browser recognition retains wake-word-only
// interruption because it does not expose a reliable local VAD result.
function _vvStartBargeMonitor() {
  _vvStopBargeMonitor();
  if (_vv.whisperProvider !== 'local' || !_vv.analyser || !_vv.dataArray) return;
  _vv._bargeEnergyFrames = 0;
  _vv._bargeMonitor = setInterval(function() {
    if (_vv.phase !== 'speaking' || !_vv.analyser || !_vv.dataArray) {
      _vvStopBargeMonitor();
      return;
    }
    _vv.analyser.getByteFrequencyData(_vv.dataArray);
    var total = 0;
    var peak = 0;
    var voicedBins = 0;
    for (var index = 0; index < _vv.dataArray.length; index++) {
      var level = _vv.dataArray[index];
      total += level;
      if (level > peak) peak = level;
      if (level > 45) voicedBins += 1;
    }
    var average = total / _vv.dataArray.length;
    var speechLike = average > 38 && peak > 90 && voicedBins >= Math.max(3, Math.floor(_vv.dataArray.length * 0.08));
    _vv._bargeEnergyFrames = speechLike ? _vv._bargeEnergyFrames + 1 : 0;
    if (_vv._bargeEnergyFrames >= 8) _vvBargeIn();
  }, 100);
}

function _vvStopBargeMonitor() {
  if (_vv.bargeRAF) { cancelAnimationFrame(_vv.bargeRAF); _vv.bargeRAF = null; }
  if (_vv._bargeMonitor) { clearInterval(_vv._bargeMonitor); _vv._bargeMonitor = null; }
  _vv._bargeEnergyFrames = 0;
}

function _vvWakeWordMatch(transcript) {
  // Faster Whisper commonly renders Eva as "Ava". Treat that close phonetic
  // variant as the wake name only inside the voice interaction.
  return String(transcript || '').match(/\b(eva|ava)\b/i);
}

function _vvHandleTranscript(transcript) {
  if (typeof evaTextPromptConsumeVoice === 'function' && evaTextPromptConsumeVoice(transcript)) {
    var promptTranscript = document.getElementById('vvTranscript');
    if (promptTranscript) promptTranscript.textContent = '\u25B8 ' + transcript;
    if (_vvIsActive()) _vvEnterAwake(_vv.convoTimeoutMs);
    return;
  }
  var translationCommand = _vvTranslationCommand(transcript);
  var commandIsAuthorized = _vv.liveTranslation || _vv.phase === 'awake' || !!_vvWakeWordMatch(transcript);
  if (translationCommand === true && commandIsAuthorized) {
    _vvSetLiveTranslation(true, false);
    return;
  }
  if (translationCommand === false && commandIsAuthorized) {
    _vvSetLiveTranslation(false, false);
    return;
  }
  if (_vv.liveTranslation) {
    _vvTranslateLiveTranscript(transcript);
    return;
  }
  // A response is mid-flight: if stuck too long, recover instead of ignoring forever.
  if (_vv.phase === 'thinking') {
    // Allow up to 90s of thinking, then force recovery
    if (!_vv._thinkingStart) _vv._thinkingStart = Date.now();
    if (Date.now() - _vv._thinkingStart > 90000) {
      console.warn('[VoiceView] Stuck in thinking for 90s, forcing recovery');
      _vv._thinkingStart = null;
      _vv.phase = 'listening';
      _vvSetStatus('listening');
      // Fall through to process the transcript instead of returning
    } else {
      return;
    }
  }
  // Speaking only yields to an explicit wake name. This keeps room noise and
  // side conversations from stopping playback while preserving "Eva, ..." as
  // an immediate redirect.
  if (_vv.phase === 'speaking') {
    if (!transcript || transcript.trim().length <= 1) return;
    if (_vv.whisperProvider !== 'local' && !_vvWakeWordMatch(transcript)) return;
    _vvBargeIn();
  }

  // Show transcript in HUD
  var transcriptEl = document.getElementById('vvTranscript');
  if (transcriptEl) transcriptEl.textContent = transcript;

  var lower = transcript.toLowerCase();
  var wakeMatch = _vvWakeWordMatch(lower);

  if (wakeMatch) {
    var command = transcript.substring(wakeMatch.index + wakeMatch[0].length).trim().replace(/^[,.\s]+/, '').trim();
    if (command.length > 1) {
      _vvSendCommand(command, true);
    } else {
      // Wake word only: open the conversation window and wait for the command.
      _vvEnterAwake(_vv.convoMode ? _vv.convoTimeoutMs : 10000);
    }
    return;
  }

  if (_vv.phase === 'awake') {
    if (transcript.length > 1) {
      _vvSendCommand(transcript);
    } else {
      // Too short to act on; stay awake and re-arm the standby timer instead of
      // dropping out of the conversation on a stray noise.
      _vvEnterAwake(_vv.convoMode ? _vv.convoTimeoutMs : 10000);
    }
  }
}

// Short, varied acknowledgements fill the gap before a full answer begins. The
// standalone app renders and caches these clips using the active Local Voices
// profile, avoiding browser SpeechSynthesis's generic computer voice.
var _VV_ACK_PHRASES = {
  en: [
    'On it.', 'One moment.', 'Let me take a look.', 'Working on it.',
    'Sure, give me a second.', 'Okay, looking into that.', 'Got it.', 'Right away.',
    'I will check that now.', 'Let me investigate.', 'I am on it.', 'Give me a moment.',
    'I will find out.', 'Checking now.', 'Let me see what I can do.', 'I will take care of that.',
    'I am looking into it.', 'Let me pull that up.', 'I will get started.', 'I have it.',
    'I will check the details.', 'Let me work through that.', 'I am taking a look.', 'I will handle it.'
  ],
  ko: [
    '알겠습니다.', '잠시만요.', '확인해 볼게요.', '처리하고 있어요.',
    '조금만 기다려 주세요.', '지금 확인하고 있어요.', '네, 알겠습니다.', '바로 할게요.',
    '지금 살펴볼게요.', '조사해 볼게요.', '제가 확인할게요.', '잠깐만요.',
    '알아볼게요.', '확인 중이에요.', '무엇을 할 수 있는지 볼게요.', '제가 처리할게요.',
    '살펴보고 있어요.', '불러와 볼게요.', '시작할게요.', '확인했어요.',
    '세부 사항을 확인할게요.', '차근차근 살펴볼게요.', '지금 보고 있어요.', '맡겨 주세요.'
  ]
};

function _vvAcknowledgementLanguage() {
  var configuredLanguage = getLocalVoicesLanguage();
  if (configuredLanguage === 'ko') return 'ko';
  if (configuredLanguage === 'en') return 'en';
  return getLocalVoicesProfile() === 'bundled:eva-korean' ? 'ko' : 'en';
}

function _vvStopAck() {
  _vv._ackRunId = (_vv._ackRunId || 0) + 1;
  _vv._ackActive = false;
  if (_vv._ackTimer) { clearTimeout(_vv._ackTimer); _vv._ackTimer = null; }
  if (_vv._ackAudio) {
    try { _vv._ackAudio.pause(); } catch (_) {}
    _vv._ackAudio = null;
  }
  if (_vv._ackUrl) {
    try { URL.revokeObjectURL(_vv._ackUrl); } catch (_) {}
    _vv._ackUrl = '';
  }
}

function _vvWarmAcknowledgements() {
  if (!window.evaStandalone || typeof window.evaStandalone.localSpeechWarmAcknowledgements !== 'function') return Promise.resolve();
  var language = _vvAcknowledgementLanguage();
  var profileId = getLocalVoicesProfile();
  var cacheKey = profileId + '|' + language;
  if (_vv._ackWarmCompleteKey === cacheKey) return Promise.resolve();
  if (_vv._ackWarmKey === cacheKey && _vv._ackWarmPromise) return _vv._ackWarmPromise;
  _vv._ackWarmKey = cacheKey;
  _vv._ackWarmPromise = window.evaStandalone.localSpeechWarmAcknowledgements({
    phrases: _VV_ACK_PHRASES[language],
    language: language,
    languageMode: language,
    profileId: profileId
  }).then(function(result) {
    if (_vv._ackWarmKey === cacheKey) _vv._ackWarmCompleteKey = cacheKey;
    return result;
  }).catch(function() {
    if (_vv._ackWarmKey === cacheKey) _vv._ackWarmKey = '';
  }).finally(function() {
    if (_vv._ackWarmKey === cacheKey) _vv._ackWarmPromise = null;
  });
  return _vv._ackWarmPromise;
}

function _vvPrepareAcknowledgements() {
  if (!window.evaStandalone || typeof window.evaStandalone.localVoicesStart !== 'function') return;
  window.evaStandalone.localVoicesStart('', '').then(function() {
    _vvWarmAcknowledgements();
  }).catch(function() {});
}
function _vvSpeakAck() {
  if (!window.evaStandalone || typeof window.evaStandalone.localSpeechAcknowledgement !== 'function') return;
  _vvStopAck();
  var runId = _vv._ackRunId;
  var language = _vvAcknowledgementLanguage();
  var profileId = getLocalVoicesProfile();
  var cacheKey = profileId + '|' + language;
  var availablePhrases = _VV_ACK_PHRASES[language] || _VV_ACK_PHRASES.en;
  var phrases = _vv._ackWarmCompleteKey === cacheKey ? availablePhrases : [availablePhrases[0]];
  var phrase = phrases[Math.floor(Math.random() * phrases.length)];
  window.evaStandalone.localSpeechAcknowledgement({
    input: phrase,
    language: language,
    languageMode: language,
    profileId: profileId
  }).then(function(bytes) {
    if (runId !== _vv._ackRunId || !_vvIsActive() || _vv.phase !== 'thinking') return;
    var audio = new Audio();
    var url = URL.createObjectURL(new Blob([bytes], { type: 'audio/wav' }));
    _vv._ackAudio = audio;
    _vv._ackUrl = url;
    _vv._ackActive = true;
    function finish() {
      if (runId !== _vv._ackRunId) return;
      _vv._ackActive = false;
      _vv._ackAudio = null;
      if (_vv._ackUrl === url) {
        try { URL.revokeObjectURL(url); } catch (_) {}
        _vv._ackUrl = '';
      }
    }
    audio.onended = finish;
    audio.onerror = finish;
    audio.src = url;
    var start = function() { audio.play().catch(finish); };
    if (typeof audio.setSinkId === 'function') {
      audio.setSinkId(getPreferredAudioOutputDeviceId()).then(start).catch(start);
    } else {
      start();
    }
    _vv._ackTimer = setTimeout(finish, 12000);
  }).catch(function() {});
}
function _vvSendCommand(command, fromWakeWord) {
  if (typeof _vvStopTTS === 'function') _vvStopTTS();
  // Natural agent confirmation via voice: if an agent is parked on a yes/no,
  // interpret this utterance as the answer instead of a new command.
  // But NOT if the user used the wake word "Eva" — that signals a new intent.
  if (!fromWakeWord && typeof _agentConfirm !== 'undefined' && _agentConfirm.pending) {
    if (_maybeAnswerAgentConfirm(command)) {
      var transcriptElC = document.getElementById('vvTranscript');
      if (transcriptElC) transcriptElC.textContent = '\u25B8 ' + command;
      // Stay in conversation: return to listening/awake after answering.
      if (typeof _vvAfterTurn === 'function') _vvAfterTurn();
      return;
    }
  }
  var compactVoiceTurnId = typeof evaCreateAuditTurnId === 'function' ? evaCreateAuditTurnId() : '';
  if (typeof evaAuditEvent === 'function') evaAuditEvent('voice.command', 'submitted', { correlation_id: compactVoiceTurnId, request_chars: String(command || '').length });
  if (typeof _runVoiceNavigationCommand === 'function' && _runVoiceNavigationCommand(command, compactVoiceTurnId)) {
    _vvAfterTurn();
    return;
  }
  _vv.lastTranscript = command;
  _vv.cmdStart = performance.now();
  _vv._thinkingStart = Date.now();
  if (_vv.awakeTimer) { clearTimeout(_vv.awakeTimer); _vv.awakeTimer = null; }
  _vvSetStatus('thinking');
  _vvHideAssets();
  // Speak an instant local acknowledgment to cover pipeline + TTS latency, so
  // the turn does not feel like dead air before the real reply is synthesized.
  _vvSpeakAck();

  // Show command in transcript area
  var transcriptEl = document.getElementById('vvTranscript');
  if (transcriptEl) transcriptEl.textContent = '\u25B8 ' + command;

  var txtMsg = document.getElementById('txtMsg');
  if (txtMsg) txtMsg.textContent = command;

  var autoSpeak = document.getElementById('autoSpeak');
  _vv._wasAutoSpeak = autoSpeak ? autoSpeak.checked : false;
  if (autoSpeak) autoSpeak.checked = true;

  _vvWatchForResponse();

  if (typeof sendData === 'function') sendData();
}

function _vvWatchForResponse() {
  var txtOutput = document.getElementById('txtOutput');
  if (!txtOutput) return;

  if (_vv.speakObserver) { _vv.speakObserver.disconnect(); _vv.speakObserver = null; }
  _vvDetachSpeakStartListeners();
  if (_vv._watchTimer) { clearTimeout(_vv._watchTimer); _vv._watchTimer = null; }
  if (_vv._postTextTimer) { clearTimeout(_vv._postTextTimer); _vv._postTextTimer = null; }

  var finished = false;   // the whole turn has resolved
  var speaking = false;   // real audio/synth playback has started
  var gotText = false;    // Eva's text response has been observed
  var audio = document.getElementById('audioPlayback');
  var synth = window.speechSynthesis;

  function cleanupTriggers() {
    if (_vv.speakObserver) { _vv.speakObserver.disconnect(); _vv.speakObserver = null; }
    _vvDetachSpeakStartListeners();
    if (_vv._watchTimer) { clearTimeout(_vv._watchTimer); _vv._watchTimer = null; }
    if (_vv._postTextTimer) { clearTimeout(_vv._postTextTimer); _vv._postTextTimer = null; }
  }

  function finishToListening() {
    if (finished || !_vvIsActive()) return;
    finished = true;
    cleanupTriggers();
    _vvRestoreAutoSpeak();
    if (_vvIsActive()) {
      _vvSetStatus('listening');
      if (_vv.whisperMode) _vvWhisperRecord();
    }
  }

  // Enter the speaking phase ONLY when real audio is heard. The earlier design
  // flipped to 'speaking' as soon as Eva's text appeared, but TTS (network
  // voices, or a slow cognition turn that renders text well before audio) can
  // lag the text by seconds. That produced a green 'speaking' orb with no
  // sound, which then timed out back to 'listening' just before the real audio
  // started. Triggering on the actual audio/synth start keeps them in sync.
  function beginSpeaking() {
    if (finished || speaking || !_vvIsActive()) return;
    speaking = true;
    _vvStopAck();
    if (_vv.speakObserver) { _vv.speakObserver.disconnect(); _vv.speakObserver = null; }
    _vvDetachSpeakStartListeners();
    if (_vv._watchTimer) { clearTimeout(_vv._watchTimer); _vv._watchTimer = null; }
    if (_vv._postTextTimer) { clearTimeout(_vv._postTextTimer); _vv._postTextTimer = null; }

    var evaResponse = (typeof lastResponse === 'string') ? lastResponse.trim() : '';
    if (evaResponse) _vv.lastEvaReply = evaResponse;

    _vvSetStatus('speaking');
    _vvConnectTTSAnalyser();
    _vvStartBargeMonitor();
    if (_vv.whisperMode) _vvWhisperRecord();

    // Single finalizer for the speaking phase, reachable from both the natural
    // speech-end and a user barge-in. Idempotent so whichever path wins runs the
    // teardown exactly once.
    var ended = false;
    function finishSpeaking(barged) {
      if (ended) return;
      ended = true;
      _vv._finishSpeaking = null;
      _vvStopBargeMonitor();
      if (barged) _vvStopTTS();
      _vvDisconnectTTSAnalyser();
      finished = true;
      cleanupTriggers();
      _vvRestoreAutoSpeak();
      if (barged) _vvAfterBarge(); else _vvAfterTurn();
    }
    _vv._finishSpeaking = finishSpeaking;

    _vvWaitForSpeechEnd(function() { finishSpeaking(false); });
  }

  // Eva's text response arrived. Stay in 'thinking' and wait for audio to begin
  // (the reliable speaking trigger). If no audio starts within the grace window
  // the response was effectively silent, so return to listening rather than
  // showing a speaking phase that never produces sound.
  function onResponseText() {
    if (finished || speaking || gotText || !_vvIsActive()) return;
    var evaResponse = (typeof lastResponse === 'string') ? lastResponse.trim() : '';
    if (!evaResponse || evaResponse === _vv.lastEvaReply) return;
    gotText = true;
    _vv.lastEvaReply = evaResponse;
    if (_vv._watchTimer) { clearTimeout(_vv._watchTimer); _vv._watchTimer = null; }
    _vv._postTextTimer = setTimeout(function() {
      _vv._postTextTimer = null;
      if (!speaking && !finished) {
        // Text completed but no audio played (silent or disabled TTS). The turn
        // is still done, so continue the conversation rather than abandoning it.
        finished = true;
        cleanupTriggers();
        _vvRestoreAutoSpeak();
        _vvAfterTurn();
      }
    }, 20000);
  }

  _vv.speakObserver = new MutationObserver(onResponseText);
  _vv.speakObserver.observe(txtOutput, { childList: true, subtree: true, characterData: true });

  // Audio playback / synth start are the authoritative speaking triggers.
  _vv._onSpeakStart = function() { beginSpeaking(); };
  if (audio) {
    audio.addEventListener('playing', _vv._onSpeakStart);
    audio.addEventListener('play', _vv._onSpeakStart);
  }
  if (synth) {
    _vv._synthPoll = setInterval(function() {
      if (finished || !_vvIsActive()) { clearInterval(_vv._synthPoll); _vv._synthPoll = null; return; }
      // Ignore the short acknowledgment filler so it does not flip the phase
      // to 'speaking' before the real reply audio actually starts.
      if (synth.speaking && !_vv._ackActive) beginSpeaking();
    }, 200);
  }

  // No-response watchdog. Heavy cognition turns (draft + review on slow models)
  // can legitimately run for minutes, so while Eva is still 'thinking' and no
  // text or audio has arrived we keep waiting and re-arm. Once text arrives the
  // post-text grace above takes over; once audio starts beginSpeaking does. We
  // only force a recovery here if the phase already moved on or a hard ceiling
  // is hit (a stuck turn that never produced text or audio).
  var watchStart = performance.now();
  var WATCH_ABS_MAX = 600000; // 10 min hard ceiling
  function watchdog() {
    _vv._watchTimer = null;
    if (finished || speaking || gotText || !_vvIsActive()) return;
    if (_vv.phase === 'thinking' && (performance.now() - watchStart) < WATCH_ABS_MAX) {
      _vv._watchTimer = setTimeout(watchdog, 10000);
      return;
    }
    finishToListening();
  }
  _vv._watchTimer = setTimeout(watchdog, 60000);
}

function _vvDetachSpeakStartListeners() {
  var audio = document.getElementById('audioPlayback');
  if (audio && _vv._onSpeakStart) {
    try { audio.removeEventListener('playing', _vv._onSpeakStart); } catch(e) {}
    try { audio.removeEventListener('play', _vv._onSpeakStart); } catch(e) {}
  }
  _vv._onSpeakStart = null;
  if (_vv._synthPoll) { clearInterval(_vv._synthPoll); _vv._synthPoll = null; }
}

function _vvRestoreAutoSpeak() {
  if (_vv._wasAutoSpeak === undefined) return;
  var autoSpeak = document.getElementById('autoSpeak');
  if (autoSpeak) autoSpeak.checked = _vv._wasAutoSpeak;
  delete _vv._wasAutoSpeak;
}

function _vvWaitForSpeechEnd(callback) {
  var audio = document.getElementById('audioPlayback');

  // Chunked TTS fires the audio element's 'ended' between sentence chunks, so
  // wait for the whole chunk queue to drain rather than a single 'ended'.
  if (typeof _ttsChunk !== 'undefined' && _ttsChunk.active) {
    var chunkPoll = setInterval(function () {
      if (!_vvIsActive() || !_ttsChunk.active || _ttsChunk.cancelled) {
        clearInterval(chunkPoll); setTimeout(callback, 300);
      }
    }, 300);
    setTimeout(function () { clearInterval(chunkPoll); callback(); }, 600000);
    return;
  }

  var synth = window.speechSynthesis;
  if (synth && synth.speaking) {
    var synthCheck = setInterval(function() {
      if (!synth.speaking) { clearInterval(synthCheck); setTimeout(callback, 500); }
    }, 500);
    setTimeout(function() { clearInterval(synthCheck); callback(); }, 30000);
    return;
  }

  if (!audio) { setTimeout(callback, 2000); return; }

  var checkCount = 0;
  var maxChecks = 30;
  function check() {
    if (!_vvIsActive()) { callback(); return; }
    checkCount++;
    if (checkCount > maxChecks) { callback(); return; }
    if (!audio.paused && !audio.ended) {
      audio.addEventListener('ended', function onEnd() {
        audio.removeEventListener('ended', onEnd);
        setTimeout(callback, 500);
      }, { once: true });
    } else {
      setTimeout(check, 1000);
    }
  }
  setTimeout(check, 1500);
}

function OnLoad() {
  // Initialize the session manager, which opens a fresh chat on launch.
    if (typeof initSessions === 'function') initSessions();

  // Show the welcome message until the session manager completes startup.
    var txtOutput = document.getElementById("txtOutput");
    if (!txtOutput.innerHTML.trim()) {
      showWelcome();
    }
}

// ── Eva Theme helpers ──────────────────────────────────────
// Click a suggestion bubble → populate input and send
function evaSuggestionClick(btn) {
  var prompt = btn.getAttribute('data-prompt');
  var input = document.getElementById('txtMsg');
  if (input && prompt) {
    input.textContent = prompt;
    sendData();
  }
}

// Hide the Eva welcome MOTD when user sends first message
function hideEvaWelcome() {
  var w = document.getElementById('evaWelcome');
  if (w) w.style.display = 'none';
}

// Populate Eva sidebar's recent sessions (Today section)
function populateEvaSidebarSessions() {
  var ul = document.getElementById('evaSidebarSessionList');
  if (!ul) return;
  ul.innerHTML = '';
  if (typeof getAllSessions !== 'function') return;
  getAllSessions().then(function(sessions) {
    var today = new Date().toDateString();
    var recent = (sessions || [])
      .filter(function(s) { return new Date(s.updatedAt || s.createdAt).toDateString() === today; })
      .sort(function(a, b) { return (b.updatedAt || b.createdAt) - (a.updatedAt || a.createdAt); })
      .slice(0, 5);
    if (!recent.length) {
      ul.innerHTML = '<li class="eva-session-empty">No chats yet today</li>';
      return;
    }
    recent.forEach(function(s) {
      var li = document.createElement('li');
      li.className = 'eva-session-item';
      li.textContent = s.title || 'Untitled';
      li.title = s.title || 'Untitled';
      li.onclick = function() { if (typeof loadSession === 'function') loadSession(s.id); };
      ul.appendChild(li);
    });
  }).catch(function() {});
}
