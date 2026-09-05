// Javascript for Options
// 

// Streaming responses are provisional text only. Marker execution remains
// exclusively in renderEvaResponse after the final event arrives.
function createEvaStreamingBubble(txtOutput) {
  var bubble = document.createElement('div');
  bubble.className = 'chat-bubble eva-bubble eva-streaming-bubble';
  var label = document.createElement('span');
  label.className = 'eva';
  label.textContent = 'Eva:';
  var text = document.createElement('span');
  text.className = 'eva-streaming-text';
  bubble.appendChild(label);
  bubble.appendChild(document.createTextNode(' '));
  bubble.appendChild(text);
  txtOutput.appendChild(bubble);
  return { bubble: bubble, text: text, value: '' };
}

function appendEvaStreamingChunk(provisional, chunk, txtOutput) {
  if (!provisional || !chunk) return;
  if (provisional.phase) {
    provisional.phase = '';
    provisional.value = '';
    provisional.bubble.classList.remove('eva-streaming-thinking');
  }
  provisional.value += String(chunk);
  provisional.text.textContent = provisional.value;
  txtOutput.scrollTop = txtOutput.scrollHeight;
}

function updateEvaStreamingStatus(provisional, event, txtOutput) {
  if (!provisional || !event) return;
  provisional.phase = String(event.phase || 'working');
  provisional.bubble.classList.toggle('eva-streaming-thinking', provisional.phase === 'thinking');
  provisional.text.textContent = String(event.text || 'Eva is working...');
  txtOutput.scrollTop = txtOutput.scrollHeight;
}

function appendEvaStreamingReasoning(provisional, chunk, txtOutput) {
  if (!provisional || !chunk) return;
  if (!provisional.reasoning) {
    var details = document.createElement('details');
    details.className = 'eva-reasoning eva-streaming-reasoning';
    details.open = true;
    var summary = document.createElement('summary');
    summary.textContent = 'Thinking';
    var body = document.createElement('div');
    body.className = 'eva-reasoning-content';
    details.appendChild(summary);
    details.appendChild(body);
    provisional.bubble.appendChild(details);
    provisional.reasoning = { body: body, value: '' };
  }
  provisional.reasoning.value += String(chunk);
  provisional.reasoning.body.textContent = provisional.reasoning.value;
  txtOutput.scrollTop = txtOutput.scrollHeight;
}

function removeEvaStreamingBubble(provisional) {
  if (provisional && provisional.bubble && provisional.bubble.parentNode) {
    provisional.bubble.parentNode.removeChild(provisional.bubble);
  }
}

async function readEvaStreamingResponse(response, onChunk, onStatus, onReasoning) {
  var contentType = (response.headers.get('Content-Type') || '').toLowerCase();
  if (contentType.indexOf('application/x-ndjson') < 0 || !response.body || !response.body.getReader) {
    return response.json();
  }
  var reader = response.body.getReader();
  var decoder = new TextDecoder();
  var pending = '';
  var finalResponse = null;

  function consumeLine(line) {
    if (!line.trim()) return;
    var event = JSON.parse(line);
    if (event.type === 'chunk') {
      if (typeof event.text === 'string') onChunk(event.text);
    } else if (event.type === 'reasoning') {
      if (typeof onReasoning === 'function') onReasoning(event.text || '');
    } else if (event.type === 'status') {
      if (typeof onStatus === 'function') onStatus(event);
    } else if (event.type === 'done') {
      finalResponse = event.response || null;
    } else if (event.type === 'error') {
      throw new Error(event.message || ('Streaming error ' + (event.status || '')));
    }
  }

  while (true) {
    var part = await reader.read();
    if (part.done) break;
    pending += decoder.decode(part.value, { stream: true });
    var lines = pending.split('\n');
    pending = lines.pop();
    lines.forEach(consumeLine);
  }
  pending += decoder.decode();
  if (pending.trim()) consumeLine(pending);
  if (!finalResponse) throw new Error('Streaming response ended without a final event');
  return finalResponse;
}

// Global Variables
var lastResponse = "";
var userMasterResponse = "";
var aiMasterResponse = "";
var masterOutput = "";
var storageAssistant = "";
var imgSrcGlobal; // Declare a global variable for img.src
// Error Handling Variables
var retryCount = 0;
var maxRetries = 5;
var retryDelay = 2420; // milliseconds

// API Access[OpenAI, AWS] 
var evaAuthReady = Promise.resolve();

function loadStandaloneAuth() {
  if (!window.evaStandalone || typeof window.evaStandalone.authLoad !== 'function') return Promise.resolve();
  return window.evaStandalone.authLoad().then(function(values) {
    if (!values || typeof values !== 'object') return;
    ['OPENAI_API_KEY', 'GITHUB_PAT', 'GOOGLE_GL_KEY'].forEach(function(key) {
      if (!values[key]) return;
      localStorage.setItem('auth_' + key, values[key]);
      window[key] = values[key];
    });
    if (document.readyState !== 'loading') populateAuthFields();
  }).catch(function() {});
}

function auth() {
  evaAuthReady = loadStandaloneAuth();
  // Prefer inlined local config if provided (config.local.js)
  if (typeof window !== 'undefined' && window.__LOCAL_CONFIG__) {
    const config = window.__LOCAL_CONFIG__;
    applyConfig(config);
    return;
  }

  // Fallback: fetch config.json (requires http(s) server)
  if (location.protocol === 'file:') {
    console.warn('Running from file://, unable to fetch config.json due to browser security. Create config.local.js or serve over http.');
  }

  fetch('./config.json')
    .then(response => response.json())
    .then(config => applyConfig(config))
    .catch(err => {
      console.error('Failed to load config:', err);
      document.getElementById('idText').innerText = 'Config not loaded. Use config.local.js or run a local server.';
    });
}

function applyConfig(config) {
  OPENAI_API_KEY = config.OPENAI_API_KEY;
  // Google Gemini key if provided
  GOOGLE_GL_KEY = config.GOOGLE_GL_KEY;
  // GitHub Copilot PAT
  if (config.GITHUB_PAT) GITHUB_PAT = config.GITHUB_PAT;
  AWS.config.region = config.AWS_REGION;
  AWS.config.credentials = new AWS.Credentials(config.AWS_ACCESS_KEY_ID, config.AWS_SECRET_ACCESS_KEY);
  // Apply any localStorage auth overrides
  loadAuthOverrides();
  if (typeof autoApplySavedMCPConfig === 'function') autoApplySavedMCPConfig();
}

// --- Auth Key Management ---
function getAuthKey(key) {
  var stored = localStorage.getItem('auth_' + key);
  if (stored) return stored;
  if (typeof window[key] !== 'undefined') return window[key];
  return '';
}

function loadAuthOverrides() {
  var keys = ['OPENAI_API_KEY', 'GOOGLE_GL_KEY', 'GITHUB_PAT'];
  keys.forEach(function(key) {
    var val = localStorage.getItem('auth_' + key);
    if (val) window[key] = val;
  });
}

function saveAuthKeys() {
  var map = {
    'authOpenAI': 'OPENAI_API_KEY',
    'authGitHub': 'GITHUB_PAT',
    'authGemini': 'GOOGLE_GL_KEY',
  };
  Object.keys(map).forEach(function(fieldId) {
    var el = document.getElementById(fieldId);
    var key = map[fieldId];
    if (el && el.value.trim()) {
      localStorage.setItem('auth_' + key, el.value.trim());
      window[key] = el.value.trim();
    } else if (el) {
      localStorage.removeItem('auth_' + key);
    }
  });
  if (window.evaStandalone && typeof window.evaStandalone.authSave === 'function') {
    var encryptedValues = {};
    Object.keys(map).forEach(function(fieldId) {
      var field = document.getElementById(fieldId);
      if (field && field.value.trim()) encryptedValues[map[fieldId]] = field.value.trim();
    });
    window.evaStandalone.authSave(encryptedValues).catch(function() {});
  }
  // Save ACP Bridge URL separately
  var acpEl = document.getElementById('txtACPBridgeUrl');
  if (acpEl && typeof isEvaStandalone === 'function' && isEvaStandalone()) {
    localStorage.removeItem('acp_bridge_url');
  } else if (acpEl && acpEl.value.trim()) {
    localStorage.setItem('acp_bridge_url', acpEl.value.trim());
  } else if (acpEl) {
    localStorage.removeItem('acp_bridge_url');
  }
  var lmsBaseEl = document.getElementById('aigLmStudioBaseUrl');
  if (lmsBaseEl && lmsBaseEl.value.trim()) {
    localStorage.setItem('aig_lmstudio_base_url', lmsBaseEl.value.trim());
  } else if (lmsBaseEl) {
    localStorage.removeItem('aig_lmstudio_base_url');
  }
  var lmsModelEl = document.getElementById('aigLmStudioModel');
  if (lmsModelEl && lmsModelEl.value.trim()) {
    localStorage.setItem('aig_lmstudio_model', lmsModelEl.value.trim());
  } else if (lmsModelEl) {
    localStorage.removeItem('aig_lmstudio_model');
  }
  syncAIGRuntimePrefs();
  var localVoicesProfileEl = document.getElementById('localVoicesProfile');
  if (localVoicesProfileEl && localVoicesProfileEl.value) {
    localStorage.setItem('local_voices_profile', localVoicesProfileEl.value);
  } else if (localVoicesProfileEl) {
    localStorage.setItem('local_voices_profile', 'bundled:eva-english');
  }
  var localVoicesLanguageEl = document.getElementById('localVoicesLanguage');
  if (localVoicesLanguageEl && localVoicesLanguageEl.value) {
    localStorage.setItem('local_voices_language', localVoicesLanguageEl.value);
  } else if (localVoicesLanguageEl) {
    localStorage.setItem('local_voices_language', 'auto');
  }
  // Save Signal sender/recipient to localStorage and push to bridge
  var sigSender = document.getElementById('authSignalSender');
  var sigRecip = document.getElementById('authSignalRecipient');
  if (sigSender && sigSender.value.trim()) {
    localStorage.setItem('signal_sender', sigSender.value.trim());
  } else if (sigSender) {
    localStorage.removeItem('signal_sender');
  }
  if (sigRecip && sigRecip.value.trim()) {
    localStorage.setItem('signal_recipient', sigRecip.value.trim());
  } else if (sigRecip) {
    localStorage.removeItem('signal_recipient');
  }
  _pushSignalSettingsToBridge();
  if (typeof _acpBridgeCache !== 'undefined') _acpBridgeCache = null;
  if (typeof autoApplySavedMCPConfig === 'function') autoApplySavedMCPConfig();
  if (typeof loadGoals === 'function') loadGoals(true);
  if (typeof loadBackgroundData === 'function') loadBackgroundData(true);
  setStatus('info', 'API keys saved to browser storage.');
}

function _pushSignalSettingsToBridge() {
  var sender = (localStorage.getItem('signal_sender') || '').trim();
  var recipient = (localStorage.getItem('signal_recipient') || '').trim();
  if (!sender && !recipient) return;
  var bUrl = (typeof getACPBridgeUrl === 'function') ? getACPBridgeUrl() : 'http://localhost:8888';
  try {
    var xhr = new XMLHttpRequest();
    xhr.open('POST', bUrl + '/v1/alerts/settings', true);
    xhr.setRequestHeader('Content-Type', 'application/json');
    xhr.send(JSON.stringify({
      signal_sender: sender,
      signal_recipient: recipient
    }));
  } catch (e) { /* bridge may not be running */ }
}

function _hydrateSignalFromBridge(senderEl, recipEl) {
  var bUrl = (typeof getACPBridgeUrl === 'function') ? getACPBridgeUrl() : 'http://localhost:8888';
  try {
    var xhr = new XMLHttpRequest();
    xhr.open('GET', bUrl + '/v1/alerts', true);
    xhr.onload = function() {
      if (xhr.status !== 200) return;
      try {
        var data = JSON.parse(xhr.responseText);
        var s = (data.settings || {}).signal_sender || '';
        var r = (data.settings || {}).signal_recipient || '';
        if (s && senderEl && !senderEl.value) {
          senderEl.value = s;
          localStorage.setItem('signal_sender', s);
        }
        if (r && recipEl && !recipEl.value) {
          recipEl.value = r;
          localStorage.setItem('signal_recipient', r);
        }
      } catch (e) { /* parse error */ }
    };
    xhr.send();
  } catch (e) { /* bridge may not be running */ }
}

function populateAuthFields() {
  var map = {
    'authOpenAI': 'OPENAI_API_KEY',
    'authGitHub': 'GITHUB_PAT',
    'authGemini': 'GOOGLE_GL_KEY',
  };
  Object.keys(map).forEach(function(fieldId) {
    var el = document.getElementById(fieldId);
    var key = map[fieldId];
    if (el) {
      var val = localStorage.getItem('auth_' + key) || (typeof window[key] !== 'undefined' ? window[key] : '');
      el.value = val || '';
    }
  });
  // Populate ACP Bridge URL
  var acpEl = document.getElementById('txtACPBridgeUrl');
  if (acpEl) {
    acpEl.value = (typeof getACPBridgeUrl === 'function') ? getACPBridgeUrl() : (localStorage.getItem('acp_bridge_url') || 'http://localhost:8888');
  }
  var lmsBaseEl = document.getElementById('aigLmStudioBaseUrl');
  if (lmsBaseEl) {
    lmsBaseEl.value = (typeof getLmStudioBaseUrl === 'function') ? getLmStudioBaseUrl() : (localStorage.getItem('aig_lmstudio_base_url') || 'http://localhost:1234/v1');
  }
  var lmsModelEl = document.getElementById('aigLmStudioModel');
  if (lmsModelEl) {
    lmsModelEl.value = (typeof getLmStudioModel === 'function') ? getLmStudioModel() : (localStorage.getItem('aig_lmstudio_model') || '');
  }
  // Signal fields: prefer localStorage, but if empty, hydrate from the bridge
  // (the bridge persists numbers in alerts.json, which survives AppImage rebuilds).
  var sigSender = document.getElementById('authSignalSender');
  if (sigSender) sigSender.value = localStorage.getItem('signal_sender') || '';
  var sigRecip = document.getElementById('authSignalRecipient');
  if (sigRecip) sigRecip.value = localStorage.getItem('signal_recipient') || '';
  if ((!sigSender || !sigSender.value) || (!sigRecip || !sigRecip.value)) {
    _hydrateSignalFromBridge(sigSender, sigRecip);
  }
  syncAIGRuntimePrefs();
}

function getLmStudioBaseUrl() {
  var v = (localStorage.getItem('aig_lmstudio_base_url') || '').trim();
  return v || 'http://localhost:1234/v1';
}

function getLmStudioModel() {
  var v = (localStorage.getItem('aig_lmstudio_model') || '').trim();
  return v;
}

function syncAIGRuntimePrefs() {
  if (typeof fetch !== 'function') return;
  var bridgeUrl = typeof getACPBridgeUrl === 'function' ? getACPBridgeUrl() : 'http://localhost:8888';
  fetch(bridgeUrl.replace(/\/+$/, '') + '/v1/prefs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      lmstudio_base_url: getLmStudioBaseUrl(),
      lmstudio_model: getLmStudioModel(),
      verbose_debug: localStorage.getItem('verboseDiagnostics') === '1'
    }),
    signal: AbortSignal.timeout(3000)
  }).catch(function() {});
}

function getLocalVoicesProfile() {
  return (localStorage.getItem('local_voices_profile') || 'bundled:eva-english').trim();
}

function getLocalVoicesLanguage() {
  var language = (localStorage.getItem('local_voices_language') || 'auto').trim().toLowerCase();
  return language === 'en' || language === 'ko' ? language : 'auto';
}

function getResolvedLiveTranslationModel() {
  var configured = getLiveTranslationModel();
  var openaiKey = typeof getAuthKey === 'function' ? getAuthKey('OPENAI_API_KEY') : '';
  return configured === 'openai:gpt-4.1-nano' && !openaiKey ? 'aig' : configured;
}

function getSafeBridgeBaseUrl() {
  var fallback = 'http://localhost:8888';
  var raw = (typeof getACPBridgeUrl === 'function') ? getACPBridgeUrl() : fallback;
  try {
    var parsed = new URL(raw || fallback);
    var isLoopback = parsed.hostname === 'localhost' || parsed.hostname === '127.0.0.1' || parsed.hostname === '::1';
    if (!isLoopback || (parsed.protocol !== 'http:' && parsed.protocol !== 'https:')) return fallback;
    return (parsed.origin + parsed.pathname).replace(/\/+$/, '');
  } catch (e) {
    return fallback;
  }
}

function getBridgeCapabilityHeaders() {
  var token = (window.evaStandalone && window.evaStandalone.bridgeToken) || '';
  return {
    'Content-Type': 'application/json',
    'Authorization': 'Bearer ' + token
  };
}

function hasBridgeCapability() {
  return !!(window.evaStandalone && window.evaStandalone.bridgeToken);
}

function installBridgeCapabilityFetch() {
  if (typeof window === 'undefined' || typeof window.fetch !== 'function' || window._evaBridgeFetchInstalled) return;
  var nativeFetch = window.fetch.bind(window);
  window.fetch = function(input, init) {
    var token = (window.evaStandalone && window.evaStandalone.bridgeToken) || '';
    if (!token) return nativeFetch(input, init);
    var requestUrl = typeof input === 'string' ? input : (input && input.url);
    var bridgeUrl = (typeof getACPBridgeUrl === 'function') ? getACPBridgeUrl() : '';
    try {
      if (!requestUrl || !bridgeUrl || new URL(requestUrl, window.location.href).origin !== new URL(bridgeUrl).origin) {
        return nativeFetch(input, init);
      }
    } catch (_) {
      return nativeFetch(input, init);
    }
    var options = Object.assign({}, init || {});
    var sourceHeaders = options.headers || (input && input.headers) || undefined;
    var headers = new Headers(sourceHeaders);
    headers.set('Authorization', 'Bearer ' + token);
    options.headers = headers;
    return nativeFetch(input, options);
  };
  window._evaBridgeFetchInstalled = true;
}

installBridgeCapabilityFetch();

function isAffirmativeSignalSendRequest(text) {
  function stripQuotedText(value) {
    var parts = [];
    var index = 0;
    var quoteEnd = '';
    while (index < value.length) {
      var character = value.charAt(index);
      if (quoteEnd) {
        if (character === quoteEnd) quoteEnd = '';
        index += 1;
        continue;
      }
      if (character === '"') quoteEnd = '"';
      else if (character === '“') quoteEnd = '”';
      else parts.push(character);
      index += 1;
    }
    return parts.join('');
  }

  function stripClauseFiller(clause) {
    var value = clause.replace(/^\s+/, '');
    var prefixes = ['very good', 'okay', 'great', 'then', 'sure', 'now', 'but', 'and', 'ok'];
    for (var index = 0; index < prefixes.length; index++) {
      var prefix = prefixes[index];
      if (value === prefix || value.indexOf(prefix + ' ') === 0 || value.indexOf(prefix + ',') === 0) {
        return value.slice(prefix.length).replace(/^[\s,]+/, '');
      }
    }
    return value;
  }

  function hasRevocation(clause) {
    var normalized = clause.toLowerCase().replace(/’/g, "'").replace(/,/g, ' ').replace(/\s+/g, ' ').trim();
    if (normalized.indexOf('never mind') >= 0 || normalized.split(' ').indexOf('cancel') >= 0) return true;
    var words = normalized.replace(/don't/g, 'dont').split(' ');
    if (words.indexOf('dont') >= 0 || (words.indexOf('do') >= 0 && words.indexOf('not') >= 0)) return true;
    var stopIndex = words.indexOf('stop');
    if (stopIndex < 0) return false;
    var nextIndex = stopIndex + 1;
    while (['that', 'this', 'the'].indexOf(words[nextIndex]) >= 0) nextIndex += 1;
    return ['send', 'message', 'signal'].indexOf(words[nextIndex]) >= 0;
  }

  var value = String(text || '').toLowerCase();
  // Quoted text may describe a request without making one. Keep the original
  // input for payload extraction, but never authorize delivery from a quote.
  var comparable = stripQuotedText(value);
  if (!/\bsignal\b/.test(comparable)) return false;
  var clauses = comparable.split(/(?:[.;]|\bthen\b|\band\b)/);
  var authorized = false;
  for (var clauseIndex = 0; clauseIndex < clauses.length; clauseIndex++) {
    var clause = stripClauseFiller(clauses[clauseIndex]);
    if (!clause) continue;
    var address = '(?:(?:hey\\s+)?eva[,.]?\\s*)?';
    if (/^signal\s+me\s+(?:is|was|means)\b/.test(clause)) continue;
    var requestPrefix = '(?:(?:please\\s+)?(?:can you|could you|would you|will you)\\s+(?:please\\s+)?|please\\s+|i want you to\\s+|i need you to\\s+)?';
    var command = '(?:send|text|message|notify|ping)';
    var prefix = '^\\s*' + address + requestPrefix;
    var commandMatches = new RegExp(prefix + 'signal\\s+me\\b').test(clause) ||
      new RegExp(prefix + 'use\\s+signal\\s+(?:to\\s+)?(?:send|text|message|notify|ping|say|tell)\\b').test(clause) ||
      new RegExp(prefix + command + '\\b[\\s\\S]{0,100}\\b(?:on|via|through|with)\\s+signal\\b').test(clause) ||
      new RegExp(prefix + command + '\\s+(?:me\\s+)?(?:a\\s+)?signal\\b').test(clause);
    if ((authorized || commandMatches) && hasRevocation(clause)) return false;
    if (commandMatches) authorized = true;
  }
  return authorized;
}

var _lastDeliveredSignal = null;
var _signalDeliveryGeneration = 0;

function clearLastDeliveredSignal() {
  _lastDeliveredSignal = null;
  _signalDeliveryGeneration += 1;
}

function isSignalRepeatRequest(text) {
  var value = String(text || '').toLowerCase().trim();
  if (!_lastDeliveredSignal || Date.now() - _lastDeliveredSignal.sentAt > 5 * 60 * 1000) return false;
  return /^(?:please\s+)?(?:do\s+(?:it|that)\s+again|send\s+(?:it|that)\s+again|repeat\s+(?:it|that)|do\s+the\s+same)\s*[.!?]?$/i.test(value);
}

function canAuthorizeSignalDelivery(text) {
  return (isAffirmativeSignalSendRequest(text) || isSignalRepeatRequest(text)) &&
    !!(window.evaStandalone && window.evaStandalone.bridgeToken);
}

function captureSignalDeliveryContext(text) {
  var request = String(text || '');
  var repeat = isSignalRepeatRequest(request);
  var repeatSignal = repeat && _lastDeliveredSignal ? {
    message: _lastDeliveredSignal.message,
    request: _lastDeliveredSignal.request
  } : null;
  return {
    authorized: canAuthorizeSignalDelivery(request),
    message: repeatSignal ? repeatSignal.message : requestedSignalMessage(request),
    request: request,
    repeat: !!repeatSignal,
    repeatSignal: repeatSignal,
    generation: _signalDeliveryGeneration
  };
}

function isSignalDeliveryContextValid(context) {
  return !!context && context.generation === _signalDeliveryGeneration;
}

function requestedSignalMessage(text) {
  var value = String(text || '').trim();
  if (isSignalRepeatRequest(value)) {
    return _lastDeliveredSignal.message || '';
  }
  if (!isAffirmativeSignalSendRequest(value)) return '';
  var quoted = value.match(/["“]([^"”]{1,4000})["”]/) || value.match(/'([^']{1,4000})'/);
  var message = quoted ? quoted[1].trim() : '';
  if (!message) {
    var explicit = value.match(/\b(?:say|saying|that says|message\s*:|text\s*:)\s*(.{1,4000})$/i);
    if (explicit) message = explicit[1].trim();
  }
  var wantsTimestamp = /\b(?:date\s+)?timestamp\b|\bdate\s+and\s+time\b/i.test(value);
  if (wantsTimestamp) {
    var stamp = new Date().toLocaleString();
    message = message ? message + ' — ' + stamp : 'Eva timestamp: ' + stamp;
  }
  return message.slice(0, 4000);
}

var _localVoicesBridgeState = null;

async function refreshLocalVoicesProfiles() {
  var select = document.getElementById('localVoicesProfile');
  var languageSelect = document.getElementById('localVoicesLanguage');
  var controls = document.getElementById('localVoicesProfileControls');
  if (!select) return;
  if (languageSelect) languageSelect.value = getLocalVoicesLanguage();
  if (!window.evaStandalone || !window.evaStandalone.isStandalone) {
    if (controls) controls.style.display = 'none';
    return;
  }
  if (controls) controls.style.display = '';
  var selected = getLocalVoicesProfile();
  try {
    var profiles = await window.evaStandalone.localVoicesList();
    select.innerHTML = '';
    profiles.forEach(function(profile) {
      var option = document.createElement('option');
      option.value = profile.id;
      option.textContent = profile.label;
      select.appendChild(option);
    });
    var found = Array.from(select.options).some(function(option) { return option.value === selected; });
    var defaultProfile = 'bundled:eva-english';
    var defaultFound = Array.from(select.options).some(function(option) { return option.value === defaultProfile; });
    select.value = found ? selected : (defaultFound ? defaultProfile : '');
    localStorage.setItem('local_voices_profile', select.value);
  } catch (error) {
    select.value = 'bundled:eva-english';
    setLocalVoicesBridgeStatus(error && error.message ? error.message : 'Voice profiles unavailable.', true);
  }
}

async function importLocalVoicesProfile() {
  if (!window.evaStandalone || !window.evaStandalone.isStandalone) return;
  var button = document.getElementById('localVoicesImportButton');
  if (button) button.disabled = true;
  try {
    var result = await window.evaStandalone.localVoicesImport();
    if (!result || result.canceled) return;
    localStorage.setItem('local_voices_profile', result.selected || 'bundled:eva-english');
    await refreshLocalVoicesProfiles();
    await syncLocalVoicesEngine(true);
  } catch (error) {
    setLocalVoicesBridgeStatus(error && error.message ? error.message : 'Voice import failed.', true);
  } finally {
    if (button) button.disabled = false;
  }
}

function setLocalVoicesBridgeStatus(text, isError) {
  var statusEl = document.getElementById('localVoicesBridgeStatus');
  if (!statusEl) return;
  statusEl.textContent = text || '';
  statusEl.style.color = isError ? 'var(--danger,#c33)' : '';
}

async function refreshLocalVoicesBridgeControl() {
  if (!window.evaStandalone || !window.evaStandalone.isStandalone) return null;
  try {
    _localVoicesBridgeState = await window.evaStandalone.localVoicesStatus();
    if (_localVoicesBridgeState.running) {
      setLocalVoicesBridgeStatus(_localVoicesBridgeState.managed ? 'Running locally.' : 'Running outside Eva.', false);
    } else {
      setLocalVoicesBridgeStatus('Stopped.', false);
    }
    return _localVoicesBridgeState;
  } catch (error) {
    _localVoicesBridgeState = null;
    setLocalVoicesBridgeStatus(error && error.message ? error.message : 'Bridge status unavailable.', true);
    return null;
  }
}

async function syncLocalVoicesEngine(restartForProfile) {
  var engine = document.getElementById('selEngine');
  if (!window.evaStandalone || !window.evaStandalone.isStandalone || !engine) return;
  var state = await refreshLocalVoicesBridgeControl();

  if (engine.value !== 'local-voices') {
    // Voice View may still be using this shared service for local STT. Keep it
    // alive; Electron stops the managed process when Eva exits.
    setLocalVoicesBridgeStatus('', false);
    return;
  }

  try {
    setLocalVoicesBridgeStatus(state && state.running ? 'Updating voice model...' : 'Starting Local Voices...', false);
    // Electron owns profile-aware reuse/restart so settings refreshes do not
    // churn a matching model and STT-only service leases remain intact.
    await window.evaStandalone.localVoicesStart('', getLocalVoicesProfile());
    await refreshLocalVoicesBridgeControl();
  } catch (error) {
    setLocalVoicesBridgeStatus(error && error.message ? error.message : 'Local Voices could not start.', true);
  }
}

function injectWorkspaceStatusBubble(message, kind) {
  message = String(message || '').trim();
  if (!message) return;
  var txtOutput = document.getElementById('txtOutput');
  if (txtOutput) {
    if (typeof hideEvaWelcome === 'function') hideEvaWelcome();
    var safe = escapeHtml(message).replace(/\n/g, '<br>');
    var badge = kind === 'working' ? '<span class="eva-proactive-badge">working</span> ' : '';
    txtOutput.innerHTML += '<div class="chat-bubble eva-bubble eva-proactive"><span class="eva">Eva:</span> ' + badge + '<div class="md">' + safe + '</div></div>';
    txtOutput.scrollTop = txtOutput.scrollHeight;
  }
  try {
    var autoSpeakEl = document.getElementById('autoSpeak');
    var voiceOpen = typeof _vv !== 'undefined' && _vvIsActive();
    if ((voiceOpen || (autoSpeakEl && autoSpeakEl.checked)) && typeof speakText === 'function') speakText(message);
  } catch (_) {}
}

// ---------------------------------------------------------------------------
// Agent feedback loop — make Eva cognisant of what the browser/desktop agent
// actually did. Fired once when a run reaches a terminal state. It (1) renders
// a short Eva line summarizing the real outcome, (2) speaks it so the voice
// view stays in sync, and (3) appends an assistant-role note to the AIG
// conversation history so follow-up turns ("did it work?") are answered from
// fact rather than from the intent Eva announced before acting.
function _evaAgentFeedback(status, endpoint, title) {
  if (!status) return;
  if (typeof EvaLearning !== 'undefined' && EvaLearning) EvaLearning.recordActionOutcome(status, endpoint, title);
  // Clear the progress-narration throttle so the completion line is never
  // suppressed as a near-duplicate of the last "working on it" update.
  try { if (typeof _agentProgress !== 'undefined') { _agentProgress.last = 0; _agentProgress.lastText = ''; } } catch (_) {}
  var label = (title || 'task').replace(/ Agent$/, '').toLowerCase();
  var goal = String(status.goal || '').trim();
  var state = status.status;
  var spoken;     // natural, spoken/chat-facing sentence
  var memory;     // factual note for the conversation history

  if (state === 'done') {
    var res = String(status.result || '').trim();
    // Distinguish a real completion from a user-declined sensitive action.
    if (/^Stopped: user declined/i.test(res)) {
      spoken = 'Okay, I held off' + (goal ? ' on ' + goal : '') + '.';
      memory = 'Desktop/browser agent stopped: the user declined the action' + (goal ? ' for "' + goal + '"' : '') + '.';
    } else {
      // Lead with a clear completion signal so the user knows she is finished,
      // then the specifics from the agent's summary.
      var detail = res || ('I finished' + (goal ? ' ' + goal : '') + '.');
      spoken = /^(done|finished|all done|okay)/i.test(detail) ? detail : ('All done. ' + detail);
      memory = 'Desktop/browser agent finished' + (goal ? ' "' + goal + '"' : '') + '. Result: ' + (res || 'completed') + '.';
    }
  } else if (state === 'cancelled') {
    spoken = 'I stopped the ' + label + ' before finishing' + (goal ? ' ' + goal : '') + '.';
    memory = 'Desktop/browser agent was cancelled' + (goal ? ' for "' + goal + '"' : '') + ' before completing.';
  } else if (state === 'error') {
    var err = String(status.error || 'an unknown error').trim();
    spoken = 'I ran into a problem and could not finish' + (goal ? ' ' + goal : '') + ': ' + err + '.';
    memory = 'Desktop/browser agent failed' + (goal ? ' on "' + goal + '"' : '') + '. Error: ' + err + '.';
  } else {
    return;
  }

  // 1) Render an Eva chat bubble with the real outcome.
  var txtOutput = document.getElementById('txtOutput');
  if (txtOutput) {
    if (typeof hideEvaWelcome === 'function') hideEvaWelcome();
    var safe = escapeHtml(spoken).replace(/\n/g, '<br>');
    txtOutput.innerHTML += '<div class="chat-bubble eva-bubble"><span class="eva">Eva:</span> <div class="md">' + safe + '</div></div>';
    txtOutput.scrollTop = txtOutput.scrollHeight;
  }

  // 2) Speak it if auto-speak is on or the voice view is open, so the spoken
  //    narration reflects the actual result instead of the pre-action intent.
  try {
    var autoSpeakEl = document.getElementById('autoSpeak');
    var voiceOpen = (typeof _vv !== 'undefined' && _vvIsActive());
    if ((voiceOpen || (autoSpeakEl && autoSpeakEl.checked)) && typeof speakText === 'function') {
      speakText(spoken);
    }
  } catch (_) {}

  // 3) Append a factual assistant note to the AIG history so the next turn is
  //    grounded in what really happened.
  try {
    var storageKey = 'aigMessages';
    var hist = JSON.parse(localStorage.getItem(storageKey) || '[]');
    if (Array.isArray(hist)) {
      hist.push({ role: 'assistant', content: '[Action outcome] ' + memory });
      localStorage.setItem(storageKey, JSON.stringify(hist));
    }
  } catch (_) {}

  if (typeof lastResponse === 'string') lastResponse = spoken;

  // 4) Return voice mode to listening after agent completion. The voice state
  //    machine may be stuck in 'speaking' or 'thinking' since the agent ran
  //    outside the normal turn cycle. Nudge it back after TTS finishes.
  try {
    if (typeof _vv !== 'undefined' && _vvIsActive()) {
      setTimeout(function() {
        if (_vv.phase === 'speaking' || _vv.phase === 'thinking') {
          if (typeof _vv._finishSpeaking === 'function') {
            _vv._finishSpeaking(false);
          } else {
            _vvStopTTS();
            _vvAfterTurn();
          }
        }
      }, 3000); // give TTS time to finish the completion sentence
    }
  } catch (_) {}

  // 5) Auto-learn: when a complex task completes successfully, extract a reusable skill.
  if (state === 'done' && goal && typeof autoLearnSkill === 'function') {
    try {
      var hist = JSON.parse(localStorage.getItem('aigMessages') || '[]');
      var recent = Array.isArray(hist) ? hist.slice(-10) : [];
      autoLearnSkill(recent, goal);
    } catch (_) {}
  }
}

// Render + speak the result of an Eva "look" (webcam vision). Mirrors the agent
// feedback path: a chat bubble, optional speech, and a factual history note so
// follow-ups ("what colour was it?") are grounded in what she actually saw.
function _evaCameraLookResult(desc) {
  desc = String(desc || '').trim();
  if (!desc) return;
  var txtOutput = document.getElementById('txtOutput');
  if (txtOutput) {
    if (typeof hideEvaWelcome === 'function') hideEvaWelcome();
    var safe = escapeHtml(desc).replace(/\n/g, '<br>');
    txtOutput.innerHTML += '<div class="chat-bubble eva-bubble"><span class="eva">Eva:</span> <div class="md">' + safe + '</div></div>';
    txtOutput.scrollTop = txtOutput.scrollHeight;
  }
  try {
    var autoSpeakEl = document.getElementById('autoSpeak');
    var voiceOpen = (typeof _vv !== 'undefined' && _vvIsActive());
    if ((voiceOpen || (autoSpeakEl && autoSpeakEl.checked)) && typeof speakText === 'function') {
      speakText(desc);
    }
  } catch (_) {}
  try {
    var hist = JSON.parse(localStorage.getItem('aigMessages') || '[]');
    if (Array.isArray(hist)) {
      hist.push({ role: 'assistant', content: '[Camera] I looked through the webcam and saw: ' + desc });
      localStorage.setItem('aigMessages', JSON.stringify(hist));
    }
  } catch (_) {}
  if (typeof lastResponse === 'string') lastResponse = desc;
}

// ---------------------------------------------------------------------------
// Natural agent confirmation — Eva asks in chat/voice instead of a popup button
// ---------------------------------------------------------------------------
// When the browser/desktop agent parks for the final purchase (or needs input),
// it calls _evaAgentConfirmAsk. Eva surfaces the question in chat (and speaks
// it), and _agentConfirm is armed so the user's next message is interpreted as
// the answer (yes/no, or free text) and routed to the agent rather than sent as
// a normal turn.
var _agentConfirm = { pending: false, needsText: false };

// Narrate agent progress so the user knows Eva is working and not stuck. Eva
// speaks/prints a short status when the plan changes, throttled so it does not
// chatter. Phrased as a brief present-tense update.
var _agentProgress = { last: 0, lastText: '' };
function _evaAgentProgress(subgoal) {
  var sub = String(subgoal || '').trim();
  if (!sub) return;
  var now = Date.now();
  // Throttle: at most one spoken update every ~9s, and skip near-duplicates.
  if (now - _agentProgress.last < 9000) return;
  if (sub === _agentProgress.lastText) return;
  _agentProgress.last = now;
  _agentProgress.lastText = sub;
  var line = sub.charAt(0).toUpperCase() + sub.slice(1);
  var txtOutput = document.getElementById('txtOutput');
  if (txtOutput) {
    if (typeof hideEvaWelcome === 'function') hideEvaWelcome();
    var safe = escapeHtml(line).replace(/\n/g, '<br>');
    txtOutput.innerHTML += '<div class="chat-bubble eva-bubble eva-proactive"><span class="eva">Eva:</span> <span class="eva-proactive-badge">working</span> <div class="md">' + safe + '</div></div>';
    txtOutput.scrollTop = txtOutput.scrollHeight;
  }
  try {
    var autoSpeakEl = document.getElementById('autoSpeak');
    var voiceOpen = (typeof _vv !== 'undefined' && _vvIsActive());
    if ((voiceOpen || (autoSpeakEl && autoSpeakEl.checked)) && typeof speakText === 'function') {
      speakText(line);
    }
  } catch (_) {}
}

function _evaAgentConfirmAsk(question, needsText) {
  _agentConfirm.pending = true;
  _agentConfirm.needsText = !!needsText;
  // Auto-cancel after 60s so a stale confirm doesn't block voice forever.
  if (_agentConfirm._timeout) clearTimeout(_agentConfirm._timeout);
  _agentConfirm._timeout = setTimeout(function() {
    if (_agentConfirm.pending) {
      console.warn('[AgentConfirm] Auto-cancelling after 60s timeout');
      _agentConfirm.pending = false;
      _agentConfirm.needsText = false;
      // Best-effort decline the parked agent. Ignore errors (run may be stale).
      try {
        if (typeof EvaBrowser !== 'undefined' && EvaBrowser &&
            typeof EvaBrowser.isAwaitingConfirm === 'function' && EvaBrowser.isAwaitingConfirm()) {
          EvaBrowser.answerConfirm(false, '');
        }
      } catch (_) {}
      try {
        if (typeof EvaDesktop !== 'undefined' && EvaDesktop &&
            typeof EvaDesktop.isAwaitingConfirm === 'function' && EvaDesktop.isAwaitingConfirm()) {
          EvaDesktop.answerConfirm(false, '');
        }
      } catch (_) {}
    }
  }, 60000);
  var q = String(question || 'Should I continue?').trim();
  var txtOutput = document.getElementById('txtOutput');
  if (txtOutput) {
    if (typeof hideEvaWelcome === 'function') hideEvaWelcome();
    var safe = escapeHtml(q).replace(/\n/g, '<br>');
    txtOutput.innerHTML += '<div class="chat-bubble eva-bubble"><span class="eva">Eva:</span> <div class="md">' + safe + '</div></div>';
    txtOutput.scrollTop = txtOutput.scrollHeight;
  }
  try {
    var autoSpeakEl = document.getElementById('autoSpeak');
    var voiceOpen = (typeof _vv !== 'undefined' && _vvIsActive());
    if ((voiceOpen || (autoSpeakEl && autoSpeakEl.checked)) && typeof speakText === 'function') {
      speakText(q);
    }
  } catch (_) {}
  if (typeof lastResponse === 'string') lastResponse = q;
}

// Affirmative / negative phrase detection for the natural confirmation reply.
var _AFFIRM_RE = /\b(yes|yep|yeah|yup|sure|ok|okay|confirm|confirmed|go ahead|do it|place (the )?order|buy it|proceed|approve|affirmative|please do)\b/i;
var _NEGATE_RE = /\b(no|nope|nah|stop|cancel|don'?t|do not|decline|abort|never mind|nevermind|hold on|wait)\b/i;

// If an agent confirmation is pending, interpret `text` as the answer and route
// it to the agent. Returns true when the message was consumed (so the caller
// should NOT send it as a normal chat turn).
function _maybeAnswerAgentConfirm(text) {
  if (!_agentConfirm.pending) return false;
  var active = (typeof EvaBrowser !== 'undefined' && EvaBrowser &&
                typeof EvaBrowser.isAwaitingConfirm === 'function' && EvaBrowser.isAwaitingConfirm());
  if (!active) { _agentConfirm.pending = false; return false; }
  var msg = String(text || '').trim();
  if (!msg) return false;

  // Free-text input request: pass the message straight through.
  if (_agentConfirm.needsText) {
    _agentConfirm.pending = false;
    _agentConfirm.needsText = false;
    EvaBrowser.answerConfirm(true, msg);
    _agentConfirmEcho(msg, null);
    return true;
  }

  var yes = _AFFIRM_RE.test(msg);
  var no = _NEGATE_RE.test(msg);
  // Ambiguous (neither or both): ask once more, keep the gate armed.
  if (yes === no) {
    _agentConfirmEcho(msg, 'ambiguous');
    return true;
  }
  _agentConfirm.pending = false;
  EvaBrowser.answerConfirm(yes, '');
  _agentConfirmEcho(msg, yes ? 'yes' : 'no');
  return true;
}

function _agentConfirmEcho(userMsg, decision) {
  var txtOutput = document.getElementById('txtOutput');
  if (txtOutput) {
    var safeU = escapeHtml(String(userMsg)).replace(/\n/g, '<br>');
    txtOutput.innerHTML += '<div class="chat-bubble user-bubble"><span class="user">You:</span> ' + safeU + '</div>';
  }
  var reply = '';
  if (decision === 'yes') reply = 'Okay, confirming now.';
  else if (decision === 'no') reply = 'Understood, I\'ll stop and not place the order.';
  else if (decision === 'ambiguous') reply = 'Sorry, was that a yes or a no? Say yes to place the order or no to stop.';
  if (reply && txtOutput) {
    txtOutput.innerHTML += '<div class="chat-bubble eva-bubble"><span class="eva">Eva:</span> <div class="md">' + escapeHtml(reply) + '</div></div>';
    txtOutput.scrollTop = txtOutput.scrollHeight;
  }
  try {
    var autoSpeakEl = document.getElementById('autoSpeak');
    var voiceOpen = (typeof _vv !== 'undefined' && _vvIsActive());
    if (reply && (voiceOpen || (autoSpeakEl && autoSpeakEl.checked)) && typeof speakText === 'function') {
      speakText(reply);
    }
  } catch (_) {}
}

function applyStandaloneSimplifications() {
  if (!(typeof isEvaStandalone === 'function' && isEvaStandalone())) return;

  var selModel = document.getElementById('selModel');
  if (selModel) {
    var modelChanged = selModel.value !== 'aig';
    Array.from(selModel.children).forEach(function(child) {
      if (child.tagName === 'OPTGROUP') {
        var hasAigOption = false;
        Array.from(child.children).forEach(function(option) {
          if (option.value === 'aig') {
            hasAigOption = true;
          } else {
            option.remove();
          }
        });
        if (!hasAigOption) child.remove();
      } else if (child.tagName === 'OPTION' && child.value !== 'aig') {
        child.remove();
      }
    });

    selModel.value = 'aig';
    var modelLabel = document.querySelector('label[for="selModel"]');
    if (modelLabel) modelLabel.style.display = 'none';
    selModel.style.display = 'none';

    if (modelChanged) {
      selModel.dispatchEvent(new Event('change', { bubbles: true }));
    }
  }

  var engineSelect = document.getElementById('selEngine');
  if (engineSelect) {
    var current = engineSelect.value;
    var pollyEngine = (current === 'standard' || current === 'neural' || current === 'generative');
    if (!current || pollyEngine) {
      var hasOpenAIKey = (typeof getAuthKey === 'function') ? !!getAuthKey('OPENAI_API_KEY') : !!window.OPENAI_API_KEY;
      engineSelect.value = hasOpenAIKey ? 'openai' : 'browser';
    }
  }
  var pollyValues = ['standard', 'neural', 'generative'];
  pollyValues.forEach(function (val) {
    var opt = document.querySelector('#selEngine option[value="' + val + '"]');
    if (opt) opt.remove();
  });

  var standaloneVersionEl = document.getElementById('evaStandaloneVersion');
  if (standaloneVersionEl && window.evaStandalone && window.evaStandalone.version) {
    standaloneVersionEl.textContent = 'Standalone v' + window.evaStandalone.version;
    standaloneVersionEl.style.display = '';
  }
}

function applyStandaloneSurface() {
  applyStandaloneSimplifications();
}

function getSavedMCPConfig() {
  try {
    return JSON.parse(localStorage.getItem('mcp_config') || '{}') || {};
  } catch (error) {
    return {};
  }
}

function hasSavedStandaloneKustoConfig() {
  var config = getSavedMCPConfig();
  var kusto = config['kusto-mcp-server'];
  var env = kusto && kusto.env ? kusto.env : {};
  return !!(env.KUSTO_CLUSTER_URL && String(env.KUSTO_CLUSTER_URL).trim());
}

function initStandaloneFirstRun() {
  if (!(typeof isEvaStandalone === 'function' && isEvaStandalone())) return;
  // If no memory backend has been chosen yet, default to SQLite and seed
  if (!localStorage.getItem('eva_memory_backend') && !hasSavedStandaloneKustoConfig()) {
    localStorage.setItem('eva_memory_backend', 'sqlite');
    localStorage.setItem('eva_standalone_first_run_done', '1');
    var bridgeUrl = typeof getACPBridgeUrl === 'function' ? getACPBridgeUrl() : '';
    if (bridgeUrl) {
      fetch(bridgeUrl.replace(/\/+$/, '') + '/v1/memory/backend', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ backend: 'sqlite' }),
        signal: AbortSignal.timeout(5000)
      }).catch(function() {});
    }
    var memSel = document.getElementById('memoryBackendSelect');
    if (memSel) memSel.value = 'sqlite';
  }
}

function toggleAuthVis(btn) {
  var input = btn.parentElement.querySelector('input');
  if (input.type === 'password') {
    input.type = 'text';
    btn.textContent = 'Hide';
  } else {
    input.type = 'password';
    btn.textContent = 'Show';
  }
}

// Settings Menu Options 
document.addEventListener('DOMContentLoaded', () => {
  const settingsButton = document.getElementById('settingsButton');
  const settingsMenu = document.getElementById('settingsMenu');
  const themeSelect = document.getElementById('selTheme');
  const lcarsChipSand = document.getElementById('lcarsChipSand');
  const speakBtn = document.getElementById('speakSend');
  const selModel = document.getElementById('selModel');
  // LCARS sidebar controls (optional)
  const sidebarSettingsBtn = document.getElementById('sidebarSettingsBtn');
  const sidebarClearBtn = document.getElementById('sidebarClearBtn');
  const lcarsLabel = document.querySelector('#lcarsSidebar .lcars-label');
  const lcarsChipPrint = document.getElementById('lcarsChipPrint');
  const printBtn = document.getElementById('printButton');
  const lcarsChipTop = document.getElementById('lcarsChipTop');
  const monitorTabs = document.getElementById('lcarsMonitorTabs');
  const monitorPanels = document.getElementById('lcarsMonitorPanels');
  var settingsReturnFocus = null;

  initAudioPreferences();
  applyStandaloneSurface();
  initStandaloneFirstRun();

  // Persist and restore the AIG backend selection across restarts.
  var reasoningEffortSel = document.getElementById('selReasoningEffort');
  if (reasoningEffortSel) {
    var savedReasoningEffort = localStorage.getItem('reasoningEffort') || DEFAULT_REASONING_EFFORT;
    var hasReasoningOption = Array.from(reasoningEffortSel.options).some(function (option) { return option.value === savedReasoningEffort; });
    reasoningEffortSel.value = hasReasoningOption ? savedReasoningEffort : DEFAULT_REASONING_EFFORT;
    reasoningEffortSel.addEventListener('change', function () {
      localStorage.setItem('reasoningEffort', getReasoningEffort());
    });
  }

  var aigBackendSel = document.getElementById('selAIGBackend');
  if (aigBackendSel) {
    var savedAigBackend = localStorage.getItem('aigBackend');
    if (savedAigBackend) {
      var hasOpt = Array.from(aigBackendSel.options).some(function (o) { return o.value === savedAigBackend; });
      if (hasOpt) aigBackendSel.value = savedAigBackend;
    }
    aigBackendSel.addEventListener('change', function () {
      localStorage.setItem('aigBackend', aigBackendSel.value);
      if (aigBackendSel.value.indexOf('openai:') === 0 && typeof Cognition !== 'undefined') {
        var cognitionCfg = Cognition.getCfg();
        if (cognitionCfg.reviewerModel.indexOf('openai:') !== 0) {
          Cognition.setCfg({ reviewerModel: 'openai:gpt-5.6-luna' });
        }
      }
      // Keep cognition model selectors in sync with the live catalog.
      if (typeof cogInit === 'function') cogInit();
      // Re-evaluate data mode when AIG backend changes
      if (typeof onModelSettingsChange === 'function') onModelSettingsChange();
    });
    updateAIGModelInfo();
  }
  var aigPolicySel = document.getElementById('selAIGModelPolicy');
  if (aigPolicySel) {
    var savedAigPolicy = localStorage.getItem('aigModelPolicyMode') || 'auto-balanced';
    if (Array.from(aigPolicySel.options).some(function (option) { return option.value === savedAigPolicy; })) {
      aigPolicySel.value = savedAigPolicy;
    }
    aigPolicySel.addEventListener('change', function () {
      localStorage.setItem('aigModelPolicyMode', getAIGModelPolicyMode());
    });
  }

  // Camera presence (auto-wake): toggle the local webcam sensor. Restore the
  // persisted choice, but only auto-start the camera if it was previously on.
  var cameraPresenceEl = document.getElementById('cameraPresence');
  if (cameraPresenceEl) {
    var savedCam = false;
    var hadLocal = false;
    try {
      var lv = localStorage.getItem('cameraPresence');
      hadLocal = (lv !== null);
      savedCam = lv === '1';
    } catch (e) {}
    cameraPresenceEl.checked = savedCam;
    if (savedCam && typeof EvaCamera !== 'undefined' && EvaCamera) {
      EvaCamera.enable();
    }
    // If localStorage had no value (e.g. wiped on an app rebuild), fall back to
    // the bridge-persisted preference so the user does not re-enable each restart.
    if (!hadLocal) {
      try {
        var _pbase = (typeof getSafeBridgeBaseUrl === 'function') ? getSafeBridgeBaseUrl() : '';
        if (_pbase) {
          fetch(_pbase.replace(/\/+$/, '') + '/v1/prefs').then(function (r) {
            return r.ok ? r.json() : null;
          }).then(function (p) {
            if (p && p.cameraPresence === true) {
              cameraPresenceEl.checked = true;
              try { localStorage.setItem('cameraPresence', '1'); } catch (e) {}
              if (typeof EvaCamera !== 'undefined' && EvaCamera) EvaCamera.enable();
            }
          }).catch(function () {});
        }
      } catch (e) {}
    }
    cameraPresenceEl.addEventListener('change', function () {
      if (typeof EvaCamera === 'undefined' || !EvaCamera) return;
      var on = cameraPresenceEl.checked;
      // Persist to the bridge too so the choice survives a localStorage wipe.
      try {
        var _b = (typeof getSafeBridgeBaseUrl === 'function') ? getSafeBridgeBaseUrl() : '';
        if (_b) {
          fetch(_b.replace(/\/+$/, '') + '/v1/prefs', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ cameraPresence: on })
          }).catch(function () {});
        }
      } catch (e) {}
      if (on) {
        EvaCamera.enable().then(function (ok) {
          if (!ok) cameraPresenceEl.checked = false;
        });
      } else {
        EvaCamera.disable();
      }
    });
  }

  var verboseDiagnosticsEl = document.getElementById('verboseDiagnostics');
  if (verboseDiagnosticsEl) {
    verboseDiagnosticsEl.checked = localStorage.getItem('verboseDiagnostics') === '1';
    var verboseBridgeUrl = typeof getSafeBridgeBaseUrl === 'function' ? getSafeBridgeBaseUrl() : '';
    if (verboseBridgeUrl) {
      fetch(verboseBridgeUrl.replace(/\/+$/, '') + '/v1/prefs').then(function(response) {
        return response.ok ? response.json() : null;
      }).then(function(prefs) {
        if (prefs && typeof prefs.verbose_debug === 'boolean') {
          verboseDiagnosticsEl.checked = prefs.verbose_debug;
          localStorage.setItem('verboseDiagnostics', prefs.verbose_debug ? '1' : '0');
        }
      }).catch(function() {});
    }
    verboseDiagnosticsEl.addEventListener('change', function() {
      localStorage.setItem('verboseDiagnostics', verboseDiagnosticsEl.checked ? '1' : '0');
      syncAIGRuntimePrefs();
    });
  }

  function toggleSettings(event) {
    event.stopPropagation();
    var isOpen = settingsMenu.classList.contains('open');
    if (isOpen) {
      setSettingsOpen(false);
    } else {
      setSettingsOpen(true, event.currentTarget);
      populateAuthFields();
      refreshLocalVoicesProfiles();
      syncLocalVoicesEngine(false);
      if (typeof loadBackgroundData === 'function') loadBackgroundData(true);
      if (typeof loadDataMode === 'function') loadDataMode();
      if (typeof refreshProtectedMemoryStatus === 'function') refreshProtectedMemoryStatus();
    }
  }

  // Attach event via JavaScript
  settingsButton.addEventListener('click', toggleSettings);

  // Mirror: sidebar Settings should toggle the same menu
  if (sidebarSettingsBtn) {
    sidebarSettingsBtn.addEventListener('click', toggleSettings);
  }
  // Mirror: sidebar Clear -> Clear Messages
  if (sidebarClearBtn) {
    sidebarClearBtn.addEventListener('click', (e) => { e.stopPropagation(); clearMessages(); });
  }

  // Close the menu when clicking outside
  document.addEventListener('click', (event) => {
    if (!settingsMenu.contains(event.target) && event.target !== settingsButton) {
      setSettingsOpen(false);
    }
  });

  // Initialize theme from localStorage
  try {
    const savedTheme = (function() {
      var t = localStorage.getItem('theme') || 'eva';
      if (t === 'default') t = 'legacy'; // migrate old "default" theme name
      return t;
    })();
  const savedCollapsed = localStorage.getItem('lcars_collapsed') === '1';
    if (themeSelect) {
      themeSelect.value = savedTheme;
    }
  // Capture full model list before any theme-based filtering
  captureOriginalModelOptions();
    applyTheme(savedTheme);
  // Ensure model options reflect the saved theme on load
  updateModelOptionsForTheme(savedTheme);
    var _isEvaInit = (savedTheme === 'eva' || (savedTheme && savedTheme.indexOf('eva-') === 0));
    // Apply collapsed state if saved (LCARS or Eva use the sidebar)
    if ((savedTheme === 'lcars' || _isEvaInit) && savedCollapsed) {
      document.body.classList.add('lcars-collapsed');
    }
    // Move Speak button into sidebar if active (LCARS or Eva)
    if ((savedTheme === 'lcars' || _isEvaInit) && lcarsChipSand && speakBtn && !lcarsChipSand.contains(speakBtn)) {
      lcarsChipSand.appendChild(speakBtn);
      speakBtn.title = 'Speak';
      speakBtn.textContent = 'Speak';
    }
    // Move Print button under Speak (LCARS or Eva)
    if ((savedTheme === 'lcars' || _isEvaInit) && lcarsChipPrint && printBtn && !lcarsChipPrint.contains(printBtn)) {
      lcarsChipPrint.appendChild(printBtn);
      printBtn.title = 'Print Output';
    }
    // Update LCARS label with current date
    if (lcarsLabel) {
      const now = new Date();
      const dateStr = now.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: '2-digit' });
      lcarsLabel.textContent = `Access • ${dateStr}`;
    }
  } catch (e) {
    console.warn('Theme init failed:', e);
  }

  // Toggle LCARS sidebar collapse on top chip click
  if (lcarsChipTop) {
    lcarsChipTop.setAttribute('role', 'button');
    lcarsChipTop.setAttribute('tabindex', '0');
    // Helper to sync tooltip title only
    function syncHandleTooltip() {
      var collapsed = document.body.classList.contains('lcars-collapsed');
      lcarsChipTop.title = collapsed ? 'Expand LCARS sidebar' : 'Collapse LCARS sidebar';
    }
    syncHandleTooltip();
    lcarsChipTop.addEventListener('click', function(e){
      e.stopPropagation();
      document.body.classList.toggle('lcars-collapsed');
      try { localStorage.setItem('lcars_collapsed', document.body.classList.contains('lcars-collapsed') ? '1' : '0'); } catch (e) {}
      syncHandleTooltip();
    });
    lcarsChipTop.addEventListener('keydown', function(e){
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        lcarsChipTop.click();
      }
    });
  }

  // Eva New Chat button — clear chat and restore welcome MOTD
  var evaNewChat = document.getElementById('evaNewChatBtn');
  if (evaNewChat) {
    evaNewChat.addEventListener('click', function() {
      if (typeof newSession === 'function') newSession();
      else {
        if (typeof clearMessages === 'function') clearMessages();
        restoreEvaWelcome();
      }
    });
  }

  // Eva sidebar nav buttons — open settings with correct tab
  // Must stopPropagation so the document click-outside handler doesn't immediately close settings
  var settingsMetadata = {
    general: ['Essentials', 'General', "Choose Eva's appearance, voice, data mode and memory location."],
    models: ['Essentials', 'Models', 'Select the response model, reasoning level and adaptive review behavior.'],
    prompts: ['Essentials', 'Personality', "Shape Eva's personality and the independent review prompt."],
    goals: ['Operations', 'Goals', 'Create and maintain the persistent intentions Eva carries across sessions.'],
    background: ['Operations', 'Background jobs', 'Manage maintenance cycles, proactive alerts, notification limits and recent activity.'],
    cron: ['Operations', 'Schedules', 'Create recurring tasks that Eva runs through the background service.'],
    auth: ['Connections', 'Accounts', 'Manage local provider credentials and Signal notification endpoints.'],
    mcp: ['Connections', 'Tools & memory', 'Configure persistent storage, MCP servers and generated artifacts.'],
    learning: ['Privacy', 'Learning', 'Control what bounded feedback metadata Eva may retain and for how long.']
  };

  function setSettingsOpen(open, trigger) {
    var overlay = document.getElementById('settingsOverlay');
    var wasOpen = settingsMenu.classList.contains('open');
    if (open && !wasOpen) {
      settingsReturnFocus = trigger || document.activeElement;
    }
    settingsMenu.classList.toggle('open', open);
    settingsMenu.setAttribute('aria-hidden', open ? 'false' : 'true');
    if (overlay) overlay.classList.toggle('open', open);
    document.body.classList.toggle('settings-open', open);
    if (open && !wasOpen) {
      requestAnimationFrame(function() {
        var activeTab = settingsMenu.querySelector('.settings-tab.active');
        if (activeTab) activeTab.focus();
      });
    } else if (!open && wasOpen) {
      var focusTarget = settingsReturnFocus;
      if (!focusTarget || focusTarget.getClientRects().length === 0 || typeof focusTarget.focus !== 'function') {
        focusTarget = document.getElementById('evaInputSettings');
      }
      if (focusTarget && typeof focusTarget.focus === 'function') focusTarget.focus();
      settingsReturnFocus = null;
    }
  }

  function activateSettingsTab(target) {
    var settingsTabs = document.querySelectorAll('.settings-tab');
    var settingsPanels = document.querySelectorAll('.settings-panel');
    var activeTab = null;
    settingsTabs.forEach(function(tab) {
      var active = tab.getAttribute('data-stab') === target;
      tab.classList.toggle('active', active);
      tab.setAttribute('aria-selected', active ? 'true' : 'false');
      tab.setAttribute('tabindex', active ? '0' : '-1');
      if (active) activeTab = tab;
    });
    settingsPanels.forEach(function(panel) {
      var active = panel.getAttribute('data-stab') === target;
      panel.classList.toggle('active', active);
      panel.hidden = !active;
      panel.setAttribute('aria-hidden', active ? 'false' : 'true');
    });
    var metadata = settingsMetadata[target] || settingsMetadata.general;
    var eyebrow = document.getElementById('settingsPageEyebrow');
    var title = document.getElementById('settingsPageTitle');
    var description = document.getElementById('settingsPageDescription');
    if (eyebrow) eyebrow.textContent = metadata[0];
    if (title) title.textContent = metadata[1];
    if (description) description.textContent = metadata[2];
    var body = document.querySelector('.settings-body');
    if (body) body.scrollTop = 0;
    if (target === 'goals' && typeof loadGoals === 'function') loadGoals(false);
    if (target === 'background' && typeof loadBackgroundData === 'function') loadBackgroundData(false);
    if (target === 'email' && window.EvaEmailSettings) EvaEmailSettings.refresh();
    return activeTab;
  }

  function evaOpenSettings(e, tabName) {
    e.stopPropagation();
    if (typeof closeAgentOperationsForNavigation === 'function') closeAgentOperationsForNavigation();
    if (!settingsMenu.classList.contains('open')) {
      setSettingsOpen(true, e.currentTarget);
      populateAuthFields();
      refreshLocalVoicesProfiles();
      syncLocalVoicesEngine(false);
      if (typeof loadBackgroundData === 'function') loadBackgroundData(true);
      if (typeof refreshProtectedMemoryStatus === 'function') refreshProtectedMemoryStatus();
    }
    if (tabName) {
      var requestedTab = activateSettingsTab(tabName);
      if (requestedTab) requestedTab.focus();
    }
  }
  var evaPromptsBtn = document.getElementById('evaPromptsBtn');
  if (evaPromptsBtn) evaPromptsBtn.addEventListener('click', function(e) { evaOpenSettings(e, 'prompts'); });
  var evaModelsBtn = document.getElementById('evaModelsBtn');
  if (evaModelsBtn) evaModelsBtn.addEventListener('click', function(e) { evaOpenSettings(e, 'models'); });
  var evaSettingsBtn = document.getElementById('evaSettingsBtn');
  if (evaSettingsBtn) evaSettingsBtn.addEventListener('click', function(e) { evaOpenSettings(e, null); });
  var evaInputGear = document.getElementById('evaInputSettings');
  if (evaInputGear) evaInputGear.addEventListener('click', function(e) { evaOpenSettings(e, null); });

  // Monitors: tab switching
  if (monitorTabs && monitorPanels) {
    monitorTabs.addEventListener('click', function(e){
      const btn = e.target.closest('.monitor-tab');
      if (!btn) return;
      const tab = btn.getAttribute('data-tab');
      monitorTabs.querySelectorAll('.monitor-tab').forEach(b=>{
        b.classList.toggle('active', b === btn);
        b.setAttribute('aria-selected', b === btn ? 'true' : 'false');
      });
      monitorPanels.querySelectorAll('.monitor-panel').forEach(p=>{
        const match = p.getAttribute('data-tab') === tab;
        p.classList.toggle('active', match);
        p.setAttribute('aria-hidden', match ? 'false' : 'true');
      });
    });
  }

  // Settings panel tab switching
  var settingsTabs = document.querySelectorAll('.settings-tab');
  settingsTabs.forEach(function(tab) {
    var target = tab.getAttribute('data-stab');
    var panel = document.querySelector('.settings-panel[data-stab="' + target + '"]');
    tab.id = 'settings-tab-' + target;
    tab.setAttribute('aria-controls', 'settings-panel-' + target);
    if (panel) {
      panel.id = 'settings-panel-' + target;
      panel.setAttribute('role', 'tabpanel');
      panel.setAttribute('aria-labelledby', tab.id);
      panel.hidden = !panel.classList.contains('active');
      panel.setAttribute('aria-hidden', panel.classList.contains('active') ? 'false' : 'true');
    }
    tab.addEventListener('click', function() {
      activateSettingsTab(tab.getAttribute('data-stab'));
    });
    tab.addEventListener('keydown', function(e) {
      var tabs = Array.from(settingsTabs);
      var index = tabs.indexOf(tab);
      var nextIndex = null;
      if (e.key === 'ArrowDown' || e.key === 'ArrowRight') nextIndex = (index + 1) % tabs.length;
      if (e.key === 'ArrowUp' || e.key === 'ArrowLeft') nextIndex = (index - 1 + tabs.length) % tabs.length;
      if (e.key === 'Home') nextIndex = 0;
      if (e.key === 'End') nextIndex = tabs.length - 1;
      if (nextIndex !== null) {
        e.preventDefault();
        activateSettingsTab(tabs[nextIndex].getAttribute('data-stab'));
        tabs[nextIndex].focus();
      }
    });
  });

  document.querySelectorAll('[data-settings-anchor]').forEach(function(button) {
    button.addEventListener('click', function() {
      var section = document.getElementById(button.getAttribute('data-settings-anchor'));
      if (section) section.scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
  });

  // Settings close button
  var settingsCloseBtn = document.getElementById('settingsClose');
  if (settingsCloseBtn) {
    settingsCloseBtn.addEventListener('click', function(e) {
      e.stopPropagation();
      setSettingsOpen(false);
    });
  }

  // Settings overlay click to close
  var settingsOverlayEl = document.getElementById('settingsOverlay');
  if (settingsOverlayEl) {
    settingsOverlayEl.addEventListener('click', function() {
      setSettingsOpen(false);
    });
  }

  document.addEventListener('keydown', function(e) {
    if (!settingsMenu.classList.contains('open')) return;
    if (e.key === 'Escape') {
      setSettingsOpen(false);
      return;
    }
    if (e.key === 'Tab') {
      var focusable = Array.from(settingsMenu.querySelectorAll(
        'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [href], [tabindex]:not([tabindex="-1"])'
      )).filter(function(element) {
        return element.getClientRects().length > 0 && element.getAttribute('aria-hidden') !== 'true';
      });
      if (!focusable.length) return;
      var first = focusable[0];
      var last = focusable[focusable.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    }
  });

  ['aigLmStudioBaseUrl', 'aigLmStudioModel', 'localVoicesProfile', 'localVoicesLanguage', 'authSignalSender', 'authSignalRecipient'].forEach(function(id) {
    var el = document.getElementById(id);
    if (el) {
      el.addEventListener('change', saveAuthKeys);
      if (id === 'localVoicesProfile') el.addEventListener('change', function() { syncLocalVoicesEngine(true); });
    }
  });
  var localVoicesImportButton = document.getElementById('localVoicesImportButton');
  if (localVoicesImportButton) localVoicesImportButton.addEventListener('click', importLocalVoicesProfile);

  // Init auth, system prompt, and model settings
  loadAuthOverrides();
  populateAuthFields();
  refreshLocalVoicesProfiles();
  var ttsEngine = document.getElementById('selEngine');
  if (ttsEngine) ttsEngine.addEventListener('change', function() { syncLocalVoicesEngine(false); });
  syncLocalVoicesEngine(false);
  initSystemPrompt();
  onModelSettingsChange();
  // Now enable auto-switch for user-initiated model changes and seed
  // the selector from the bridge (source of truth for persisted mode).
  setModelSettingsModeInitialized();
  if (typeof loadDataMode === 'function') loadDataMode();
  if (typeof cogInit === 'function') cogInit();
  if (typeof cogUpdatePromptsTabUI === 'function') cogUpdatePromptsTabUI();
  if (typeof initGoals === 'function') initGoals();
  if (typeof initBackground === 'function') initBackground();
  if (typeof initAlerts === 'function') initAlerts();
  if (typeof initSkills === 'function') initSkills();
  if (typeof initNotifications === 'function') initNotifications();
  if (typeof initACPPermissions === 'function') initACPPermissions();

  // Initialize status panel with any pending config/init notes
  setStatus('info', document.getElementById('idText') && document.getElementById('idText').textContent ? document.getElementById('idText').textContent : '');

  // Global error handlers -> footer status
  window.addEventListener('error', function(ev){
    try {
      setStatus('error', (ev && ev.message) ? ev.message : 'An error occurred');
    } catch(_){}
  });
  window.addEventListener('unhandledrejection', function(ev){
    try {
      var msg = (ev && ev.reason && (ev.reason.message || ev.reason)) ? (ev.reason.message || String(ev.reason)) : 'Unhandled promise rejection';
      setStatus('error', msg);
    } catch(_){}
  });
});

// Welcome Text
/** Welcome message shown on new/empty sessions */
function showWelcome() {
  var txtOutput = document.getElementById('txtOutput');
  if (!txtOutput) return;
  txtOutput.innerHTML =
    '<div class="chat-bubble eva-bubble">' +
    '<span class="eva">Eva:</span> ' +
    'Welcome back. Here\'s what I can do:<br><br>' +
    '&bull; <b>Persistent Memory</b> &mdash; I remember your preferences, facts, and past conversations across sessions.<br>' +
    '&bull; <b>Voice Activation</b> &mdash; Click <b>Mic</b> and say <b>"Eva"</b> followed by your question. I\'ll listen quietly until you call.<br>' +
    '&bull; <b>Sessions</b> &mdash; Your conversations auto-save. Use <b>Sessions</b> to switch or start fresh.<br>' +
    '&bull; <b>Live Data</b> &mdash; Ask about weather, news, stocks, or space weather for real-time info.<br>' +
    '&bull; <b>Image Search &amp; Generation</b> &mdash; Ask me to show or generate an image of anything.<br>' +
    '&bull; <b>Multiple Models</b> &mdash; Switch providers in Settings &rarr; Models (OpenAI, Gemini, Copilot, local LLMs).<br><br>' +
    'Just type or speak &mdash; I\'m ready.' +
    '</div>';
}

// ═══════════════════════════════════════════════════════════════
// Apply UI theme (default | lcars | eva | eva-*)
function applyTheme(theme) {
  const body = document.body;
  if (!body) return;

  // Remove all theme-* classes (covers variants like theme-eva-rose, etc.)
  body.className = body.className.replace(/\btheme-\S+/g, '').trim();
  // eva.css holds global component styles (voice view, welcome screen, color
  // variants) that all themes rely on, not just theme-eva. Keep it loaded for
  // every theme so switching to LCARS/legacy doesn't strip those base rules
  // (which previously left the voice-view HUD visible and broke the layout).
  ensureThemeStylesheet('eva', 'core/themes/eva.css');
  unloadThemeStylesheet('lcars');

  // Add selected theme class
  var isEva = (theme === 'eva' || theme.indexOf('eva-') === 0);
  if (isEva) {
    body.classList.add('theme-eva');
    if (theme !== 'eva') body.classList.add('theme-' + theme);
    ensureThemeStylesheet('eva', 'core/themes/eva.css');
    // Move speak button into sidebar (same layout as LCARS)
    const lcarsChipSand = document.getElementById('lcarsChipSand');
    const speakBtn = document.getElementById('speakSend');
    if (lcarsChipSand && speakBtn && !lcarsChipSand.contains(speakBtn)) {
      lcarsChipSand.appendChild(speakBtn);
      speakBtn.title = 'Speak';
      speakBtn.textContent = 'Speak';
    }
    // Move Print button into sidebar
    const lcarsChipPrint = document.getElementById('lcarsChipPrint');
    const printBtn = document.getElementById('printButton');
    if (lcarsChipPrint && printBtn && !lcarsChipPrint.contains(printBtn)) {
      lcarsChipPrint.appendChild(printBtn);
      printBtn.title = 'Print Output';
    }
  } else if (theme === 'lcars') {
    body.classList.add('theme-lcars');
  // Ensure LCARS stylesheet is present (modular theme loader)
  ensureThemeStylesheet('lcars', 'core/themes/lcars.css');
    // Move speak button into sidebar
    const lcarsChipSand = document.getElementById('lcarsChipSand');
    const speakBtn = document.getElementById('speakSend');
    if (lcarsChipSand && speakBtn && !lcarsChipSand.contains(speakBtn)) {
      lcarsChipSand.appendChild(speakBtn);
      speakBtn.title = 'Speak';
      speakBtn.textContent = 'Speak';
    }
    // Move Print button beneath Speak in sidebar
    const lcarsChipPrint = document.getElementById('lcarsChipPrint');
    const printBtn = document.getElementById('printButton');
    if (lcarsChipPrint && printBtn && !lcarsChipPrint.contains(printBtn)) {
      lcarsChipPrint.appendChild(printBtn);
      printBtn.title = 'Print Output';
    }
  } else {
    // Restore speak button to its original container when leaving LCARS
    const container = document.querySelector('.container');
    const speakBtn = document.getElementById('speakSend');
    if (container && speakBtn && !container.contains(speakBtn)) {
      container.appendChild(speakBtn);
    }
    // Restore Print button to footer when leaving LCARS
    const footer = document.querySelector('footer');
    const printBtn = document.getElementById('printButton');
    if (footer && printBtn && !footer.contains(printBtn)) {
      footer.appendChild(printBtn);
    }
  }

  // Persist
  try { localStorage.setItem('theme', theme); } catch (e) {}

  // Update available model options according to theme
  updateModelOptionsForTheme(theme);

  // Ensure monitors dock is visible on LCARS and Eva themes
  var mon = document.getElementById('lcarsMonitorsDock');
  if (mon) mon.style.display = (theme === 'lcars') ? 'block' : 'none';

  // Toggle Eva sidebar visibility
  var evaSidebar = document.getElementById('evaSidebar');
  if (evaSidebar) evaSidebar.style.display = isEva ? 'flex' : 'none';

  // Toggle Eva disclaimer
  var evaDisclaimer = document.getElementById('evaDisclaimer');
  if (evaDisclaimer) evaDisclaimer.style.display = isEva ? 'block' : 'none';

  // Eva themes all use the thumb-125 portrait
  var welcomeAvatar = document.querySelector('.eva-welcome-avatar');
  var sidebarAvatar = document.querySelector('.eva-sidebar-avatar');
  if (isEva) {
    if (welcomeAvatar) welcomeAvatar.src = 'core/img/thumb-125.jpeg';
    if (sidebarAvatar) sidebarAvatar.src = 'core/img/thumb-125.jpeg';
  }

  // Populate Eva sidebar sessions
  if (isEva) populateEvaSidebarSessions();
}

// Modular theme stylesheet loader (extensible for future themes)
function ensureThemeStylesheet(themeName, href) {
  const id = `theme-${themeName}-css`;
  if (document.getElementById(id)) return;
  const link = document.createElement('link');
  link.rel = 'stylesheet';
  link.id = id;
  link.href = href;
  document.head.appendChild(link);
}

function unloadThemeStylesheet(themeName) {
  const id = `theme-${themeName}-css`;
  const el = document.getElementById(id);
  if (el && el.parentNode) el.parentNode.removeChild(el);
}


// Track user intent for image handling
var _lastUserAskedGenerate = false;
var _lastUserImageSubject = '';  // extracted from user's message before send
var _lastUserAskedImage = false; // true if user asked for any image (generate, show, find)

/**
 * Extract image subject from the user's own message.
 * "show me an image of a cat" → "cat"
 * "generate a picture of a sunset over mountains" → "sunset over mountains"
 */
function _extractUserImageSubject(text) {
  if (!text) return '';
  // Match patterns like "image of X", "picture of X", "photo of X"
  var m = text.match(/(?:image|picture|photo|illustration|drawing|painting)\s+(?:of\s+)?(?:an?\s+)?(.+)/i);
  if (m) return m[1].replace(/[?.!]+$/, '').trim();
  // Match "show me X", "generate X"
  m = text.match(/(?:show\s+me|generate|create|draw|make|display)\s+(?:an?\s+)?(?:image\s+)?(?:of\s+)?(?:an?\s+)?(.+)/i);
  if (m) return m[1].replace(/[?.!]+$/, '').trim();
  return '';
}

// Detect image generation intent from user input (called before every send)
function _detectGenerationIntent() {
  var txtMsg = document.getElementById('txtMsg');
  if (txtMsg) {
    var userText = txtMsg.innerText || txtMsg.textContent || '';
    _lastUserAskedGenerate = _isGenerationRequest(userText);
    _lastUserImageSubject = _extractUserImageSubject(userText);
    _lastUserAskedImage = _isImageRequest(userText);
  }
}

function updateButton() {
  applyStandaloneSimplifications();
  var btnSend = document.getElementById("btnSend");
  if (btnSend) btnSend.onclick = sendData;
}

async function sendData() {
    // Natural agent confirmation: if the browser/desktop agent is parked waiting
    // on a yes/no (e.g. the final purchase), interpret this message as the answer
    // and route it to the agent instead of sending a normal chat turn.
    if (typeof _agentConfirm !== 'undefined' && _agentConfirm.pending) {
      var _txtMsgEl = document.getElementById('txtMsg');
      var _pendingText = _txtMsgEl ? (_txtMsgEl.innerText || _txtMsgEl.textContent || '') : '';
      if (_maybeAnswerAgentConfirm(_pendingText)) {
        if (_txtMsgEl) _txtMsgEl.innerHTML = '';
        return;
      }
    }
    var protectedInput = document.getElementById('txtMsg');
    var protectedRawText = protectedInput ? (protectedInput.innerText || protectedInput.textContent || '') : '';
    var auditTurnId = window._evaPendingAuditTurnId || evaCreateAuditTurnId();
    window._evaPendingAuditTurnId = '';
    window._evaActiveAuditTurnId = auditTurnId;
    evaAuditEvent('turn.input', 'submitted', {
      correlation_id: auditTurnId,
      request_chars: protectedRawText.length,
      model: (document.getElementById('selModel') || {}).value || ''
    });
    if (typeof captureProtectedMemoryFromChat === 'function' && await captureProtectedMemoryFromChat(protectedRawText)) {
      return;
    }
    var protectedNeedsDataRetrieval = window.EvaRequestRouting && typeof EvaRequestRouting.needsDataRetrieval === 'function' &&
      EvaRequestRouting.needsDataRetrieval(protectedRawText);
    var nativeRoute = window.EvaHarness && typeof EvaHarness.resolveNavigationRequest === 'function'
      ? EvaHarness.resolveNavigationRequest(protectedRawText, { directUser: true }) : null;
    var nativeRetrievalActions = [
      'describe_memory_titles', 'list_github_repositories', 'continue_github_repositories',
      'authorize_github', 'import_github', 'import_github_selection', 'describe_github_pull_request',
      'merge_github_pull_request', 'delete_github_pull_request_branch',
      'run_repository_remediation', 'describe_repository_remediation'
    ];
    var nativeRouteOwnsRetrieval = nativeRoute && nativeRetrievalActions.indexOf(nativeRoute.action) >= 0;
    if ((!protectedNeedsDataRetrieval || nativeRouteOwnsRetrieval) && nativeRoute) {
        evaAuditEvent('direct_route', 'started', {
          correlation_id: auditTurnId,
          action: nativeRoute.action || 'navigate',
          label: nativeRoute.target || ''
        });
        if (typeof evaTextPromptCancel === 'function') evaTextPromptCancel();
        var nativeResult = await Promise.resolve(
          nativeRoute.action && nativeRoute.action !== 'navigate'
            ? EvaHarness.execute(nativeRoute)
            : EvaHarness.navigate(nativeRoute.target)
        );
        var terminalFallback = nativeRoute.action === 'consider_terminal_task' && (!nativeResult.ok || (nativeResult.data && nativeResult.data.declined === true));
        if (terminalFallback) {
          evaAuditEvent('native_action', nativeResult.ok ? 'completed' : 'failed', {
            correlation_id: auditTurnId,
            action: nativeRoute.action,
            label: nativeRoute.target || '',
            reason: nativeResult.ok ? '' : (nativeResult.data && nativeResult.data.reason || 'failed')
          });
        } else {
          if (protectedInput) protectedInput.innerHTML = '';
          if (typeof setStatus === 'function') {
            setStatus(nativeResult.ok ? 'info' : 'error', nativeResult.message);
          }
          if (nativeRoute.action === 'run_workspace_check' || nativeRoute.action === 'run_repository_remediation' || nativeRoute.action === 'describe_workspace_tools') {
            injectWorkspaceStatusBubble(nativeResult.message, nativeResult.ok ? 'working' : 'error');
          }
          if (typeof recordConversationTurn === 'function') recordConversationTurn(protectedRawText, nativeResult.message);
          evaAuditEvent('native_action', evaAuditOutcome(nativeResult && nativeResult.data && nativeResult.data.outcome, nativeResult.ok), {
            correlation_id: auditTurnId,
            action: nativeRoute.action || 'navigate',
            label: nativeRoute.target || ''
          });
          var spokenNativeActions = [
            'describe_workspaces', 'describe_workspace_tools', 'consider_terminal_task', 'continue_github_repositories',
            'describe_skills', 'create_skill', 'update_skill', 'set_skill_status', 'delete_skill', 'run_skill', 'open_external_url'
          ];
          if (spokenNativeActions.indexOf(nativeRoute.action) !== -1 && nativeResult.ok && typeof speakText === 'function') speakText(nativeResult.message);
          return;
      }
    }
    if (window.EvaRequestRouting && typeof EvaRequestRouting.isGitHubOperation === 'function' &&
        EvaRequestRouting.isGitHubOperation(protectedRawText)) {
      evaAuditEvent('github_mcp_route', 'started', {
        correlation_id: auditTurnId,
        action: 'github_mcp',
        label: 'aig_acp'
      });
      if (typeof setStatus === 'function') setStatus('info', 'Routing GitHub operation through native GitHub MCP...');
      return aigSend();
    }
    // Hide Eva welcome MOTD on first send
    hideEvaWelcome();
  applyStandaloneSimplifications();

  // Detect if user wants image generation (for renderEvaResponse routing)
  _detectGenerationIntent();

  clearText();
  await aigSend();
}

function evaAuditEvent(event, outcome, fields) {
  var allowedEvents = { 'turn.input': true, 'turn.rendered': true, native_action: true, direct_route: true, terminal_task: true, 'voice.command': true };
  var allowedOutcomes = { started: true, planned: true, completed: true, cancelled: true, failed: true, submitted: true };
  var allowedReasons = { authentication: true, timeout: true, unavailable: true, failed: true, cancelled: true };
  if (!allowedEvents[event] || !allowedOutcomes[outcome] || typeof fetch !== 'function') return;
  fields = fields || {};
  var payload = {
    event: event,
    outcome: outcome,
    correlation_id: String(fields.correlation_id || '').slice(0, 120)
  };
  ['action', 'model', 'provider', 'label'].forEach(function(key) {
    if (typeof fields[key] === 'string') payload[key] = fields[key].slice(0, 120);
  });
  if (typeof fields.reason === 'string' && allowedReasons[fields.reason]) payload.reason = fields.reason;
  ['request_chars', 'response_chars'].forEach(function(key) {
    if (typeof fields[key] === 'number' && isFinite(fields[key])) payload[key] = fields[key];
  });
  var bridgeUrl = typeof getACPBridgeUrl === 'function' ? getACPBridgeUrl() : 'http://localhost:8888';
  fetch(bridgeUrl.replace(/\/+$/, '') + '/v1/audit/event', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
    signal: AbortSignal.timeout(3000)
  }).catch(function() {});
}

function evaCreateAuditTurnId() {
  return (typeof EvaRequestRouting !== 'undefined' && EvaRequestRouting.createTurnId)
    ? EvaRequestRouting.createTurnId() : ('turn-' + Date.now().toString(36));
}

function evaAuditOutcome(outcome, ok) {
  if (outcome === 'submitted') return 'submitted';
  if (outcome === 'cancelled') return 'cancelled';
  if (outcome === 'failed') return 'failed';
  return ok === false ? 'failed' : 'completed';
}

// Footer status helper
function setStatus(type, text) {
  var el = document.getElementById('idText');
  if (el) {
    el.classList.remove('status-info','status-warn','status-error');
    if (type === 'warn') el.classList.add('status-warn');
    else if (type === 'error') el.classList.add('status-error');
    else el.classList.add('status-info');
    if (text) el.textContent = text;
  }
  // Mirror into the Eva-theme footer status line so users on the Eva theme
  // (which hides the LCARS monitor dock) still see model/route updates.
  var foot = document.getElementById('evaStatusFooter');
  if (foot) {
    foot.classList.remove('status-info','status-warn','status-error');
    if (type === 'warn') foot.classList.add('status-warn');
    else if (type === 'error') foot.classList.add('status-error');
    else foot.classList.add('status-info');
    var msg = text || '';
    foot.textContent = msg;
    foot.setAttribute('data-empty', msg ? 'false' : 'true');
  }
}
// --- Cognitive layer settings (eva / reviewer) ---
function _cogPopulateModelSelect(targetId) {
  var src = document.getElementById('selAIGBackend');
  var dst = document.getElementById(targetId);
  if (!src || !dst) return;
  // Clone the option/optgroup tree from the AIG backend selector so the
  // cognition selectors always stay in sync with the live model catalog.
  dst.innerHTML = '';
  Array.from(src.children).forEach(function (child) {
    dst.appendChild(child.cloneNode(true));
  });
}

var COG_PROMPT_FIELDS = {
  reviewer: { id: 'cogReviewerPrompt', key: 'cogReviewerPrompt', cfgKey: 'reviewerPrompt' }
};

function _cogPromptDefault(role) {
  var defaults = (window.EvaCognition && window.EvaCognition.DEFAULT_PROMPTS) ||
    ((typeof Cognition !== 'undefined' && Cognition.DEFAULT_PROMPTS) ? Cognition.DEFAULT_PROMPTS : {});
  return defaults[role] || '';
}

function _cogStoredPromptOrDefault(role) {
  var field = COG_PROMPT_FIELDS[role];
  if (!field) return '';
  try {
    var stored = localStorage.getItem(field.key);
    if (stored) return stored;
  } catch (_) {}
  return _cogPromptDefault(role);
}

function cogInit() {
  if (typeof Cognition === 'undefined') return;
  _cogPopulateModelSelect('cogReviewerModel');
  var cfg = Cognition.getCfg();
  var $ = function (id) { return document.getElementById(id); };
  if ($('cogEnabled'))           $('cogEnabled').checked          = !!cfg.enabled;
  if ($('cogReviewerModel'))     $('cogReviewerModel').value      = cfg.reviewerModel;
  if ($('cogReviewerPrompt'))    $('cogReviewerPrompt').value    = _cogStoredPromptOrDefault('reviewer');
  cogUpdateBadge();
  onModelSettingsChange();
}

function cogPersist() {
  if (typeof Cognition === 'undefined') return;
  var $ = function (id) { return document.getElementById(id); };
  var partial = {
    enabled:           $('cogEnabled')          ? $('cogEnabled').checked        : false,
    reviewerModel:     $('cogReviewerModel')    ? $('cogReviewerModel').value    : '',
    maxCycles:         '1'
  };
  Object.keys(COG_PROMPT_FIELDS).forEach(function (role) {
    var field = COG_PROMPT_FIELDS[role];
    var el = $(field.id);
    if (!el) return;
    if (el.value === _cogPromptDefault(role)) {
      try { localStorage.removeItem(field.key); } catch (_) {}
      return;
    }
    partial[field.cfgKey] = el.value;
  });
  Cognition.setCfg(partial);
  cogUpdateBadge();
  cogUpdatePromptsTabUI();
  onModelSettingsChange();
}

function cogUpdatePromptsTabUI() {
  var $ = function (id) { return document.getElementById(id); };
  var enabled = false;
  try { enabled = (typeof Cognition !== 'undefined' && Cognition.isEnabled && Cognition.isEnabled()); } catch (_) {}
  
  // Show/hide cognitive layer sections in Prompts tab
  var indicator = $('cogStatusIndicator');
  var promptsSection = $('cogPromptsSection');
  
  if (indicator) {
    indicator.style.display = enabled ? 'flex' : 'none';
  }
  if (promptsSection) {
    promptsSection.style.display = enabled ? 'block' : 'none';
  }
}

function cogUpdateBadge() {
  var badge = document.getElementById('cogBadge');
  if (!badge) return;
  var on = false;
  try { on = (typeof Cognition !== 'undefined' && Cognition.isEnabled && Cognition.isEnabled()); } catch (_) {}
  badge.setAttribute('data-active', on ? 'true' : 'false');
  badge.textContent = on ? 'Review: adaptive' : 'Review: off';
}

function _cogApplyDefaultPrompt(role) {
  var field = COG_PROMPT_FIELDS[role];
  if (!field) return false;
  try { localStorage.removeItem(field.key); } catch (_) {}
  var el = document.getElementById(field.id);
  if (!el) return false;
  el.value = _cogPromptDefault(role);
  return true;
}

function _cogNotifyPromptChange(role) {
  var field = COG_PROMPT_FIELDS[role];
  var el = field ? document.getElementById(field.id) : null;
  if (typeof cogPersist === 'function') {
    cogPersist();
  } else if (el) {
    el.dispatchEvent(new Event('change', { bubbles: true }));
  }
}

function cogResetPrompt(role) {
  if (_cogApplyDefaultPrompt(role)) _cogNotifyPromptChange(role);
}

// --- Monitors: Token, Network, Session ---

// Better token estimation: ~3.5 chars per token for English, account for whitespace/punctuation
function estimateTokensFromText(str) {
  if (!str) return 0;
  var s = String(str);
  // Count words (roughly 1.3 tokens per word on average)
  var words = s.split(/\s+/).filter(function(w) { return w.length > 0; }).length;
  // Count special chars/punctuation as extra tokens
  var specials = (s.match(/[^a-zA-Z0-9\s]/g) || []).length;
  return Math.ceil(words * 1.3 + specials * 0.5);
}

// Map of model -> context window size
const MODEL_CONTEXT_WINDOWS = {
  'gpt-4o': 128000,
  'gpt-4o-mini': 128000,
  'o1': 200000,
  'o1-mini': 200000,
  'o1-preview': 200000,
  'o3-mini': 200000,
  'gpt-5-mini': 200000,
  'latest': 200000,
  'copilot-acp': 128000,
  'aig': 200000,
  'gemini': 1000000,
  'lm-studio': 32768,
  'dall-e-3': 0
};

// Network monitoring state
var _netStats = { requests: 0, errors: 0, lastLatency: 0, lastStatus: '', lastProvider: '' };

// Intercept fetch to track network stats
var _apiHostnames = ['api.openai.com', 'generativelanguage.googleapis.com'];

function _isAPICall(url) {
  if (typeof url !== 'string') return false;
  try {
    var parsed = new URL(url, window.location.origin);
    if (_apiHostnames.indexOf(parsed.hostname) >= 0) return true;
    if (parsed.hostname === 'localhost' && (parsed.port === '1234' || parsed.port === '8888')) return true;
    if (parsed.port === '8888') return true;
    return false;
  } catch (e) {
    return false;
  }
}

(function() {
  var origFetch = window.fetch;
  window.fetch = function() {
    var url = arguments[0];
    if (!_isAPICall(url)) return origFetch.apply(this, arguments);

    _netStats.requests++;
    var start = performance.now();
    _netStats.lastProvider = _detectProvider(url);

    return origFetch.apply(this, arguments).then(function(resp) {
      _netStats.lastLatency = Math.round(performance.now() - start);
      _netStats.lastStatus = resp.status + ' ' + (resp.ok ? 'OK' : resp.statusText);
      if (!resp.ok) _netStats.errors++;
      updateNetMonitor();
      return resp;
    }).catch(function(err) {
      _netStats.lastLatency = Math.round(performance.now() - start);
      _netStats.lastStatus = 'Error';
      _netStats.errors++;
      updateNetMonitor();
      throw err;
    });
  };
})();

function _detectProvider(url) {
  try {
    var parsed = new URL(url, window.location.origin);
    if (parsed.hostname === 'api.openai.com') return 'OpenAI';
    if (parsed.hostname === 'generativelanguage.googleapis.com') return 'Gemini';
    if (parsed.hostname === 'localhost' && parsed.port === '1234') return 'lm-studio';
    if (parsed.port === '8888') return 'ACP Bridge';
  } catch (e) {}
  return 'Unknown';
}

function getSelectedModel() {
  const sel = document.getElementById('selModel');
  return sel ? sel.value : '';
}

// Count all conversation messages across all providers
function _countAllMessages() {
  var count = 0;
  ['messages', 'copilotMessages', 'copilotACPMessages', 'geminiMessages', 'openLLMessages', 'aigMessages'].forEach(function(key) {
    try {
      var raw = localStorage.getItem(key);
      if (raw) {
        var msgs = JSON.parse(raw);
        count += msgs.length;
      }
    } catch(e) {}
  });
  return count;
}

// Compute tokens from all active message stores
function computeMessagesTokens() {
  var model = getSelectedModel();
  var keys = ['messages']; // default OpenAI
  if (model === 'copilot-acp') keys = ['copilotACPMessages'];
  else if (model.indexOf('copilot-') === 0) keys = ['copilotMessages'];
  else if (model === 'gemini') keys = ['geminiMessages'];
  else if (model === 'lm-studio') keys = ['openLLMessages'];

  var acc = 0;
  keys.forEach(function(key) {
    try {
      var raw = localStorage.getItem(key);
      if (!raw) return;
      var msgs = JSON.parse(raw);
      msgs.forEach(function(m) {
        if (!m) return;
        if (typeof m.content === 'string') {
          acc += estimateTokensFromText(m.content);
        } else if (Array.isArray(m.content)) {
          m.content.forEach(function(part) {
            if (part.type === 'text' && part.text) acc += estimateTokensFromText(part.text);
            if (part.text) acc += estimateTokensFromText(part.text);
          });
        }
        // Gemini format (parts array)
        if (Array.isArray(m.parts)) {
          m.parts.forEach(function(part) {
            if (part.text) acc += estimateTokensFromText(part.text);
          });
        }
      });
    } catch(e) {}
  });
  return acc;
}

function computeLastResponseTokens() {
  try {
    var txtOut = document.getElementById('txtOutput');
    if (!txtOut) return 0;
    var bubbles = txtOut.querySelectorAll('.eva-bubble .md, .eva-bubble');
    if (bubbles && bubbles.length) {
      return estimateTokensFromText(bubbles[bubbles.length - 1].textContent || '');
    }
    return 0;
  } catch(e) { return 0; }
}

function updateTokenMonitor() {
  var model = getSelectedModel();
  var windowSize = MODEL_CONTEXT_WINDOWS[model] || 128000;
  var msgTokens = computeMessagesTokens();
  var respTokens = computeLastResponseTokens();
  var used = msgTokens + respTokens;
  var pct = windowSize > 0 ? Math.min(100, Math.round((used / windowSize) * 100)) : 0;

  var bar = document.getElementById('ctxFillBar');
  var text = document.getElementById('ctxFillText');
  var winText = document.getElementById('modelWindowText');
  var msgText = document.getElementById('messagesTokensText');
  var respText = document.getElementById('lastResponseTokensText');

  if (bar) {
    bar.style.width = pct + '%';
    // Color the bar based on fill level
    if (pct > 80) bar.style.background = 'linear-gradient(90deg, #ff6b6b, #ee5a24)';
    else if (pct > 50) bar.style.background = 'linear-gradient(90deg, #feca57, #ff9f43)';
    else bar.style.background = '';
  }
  if (text) text.textContent = pct + '% \u2014 ~' + used.toLocaleString() + ' / ' + windowSize.toLocaleString();

  // Show model name + window
  var modelName = model || 'none';
  var sel = document.getElementById('selModel');
  if (sel && sel.selectedOptions && sel.selectedOptions[0]) {
    modelName = sel.selectedOptions[0].text;
  }
  if (winText) winText.textContent = modelName + ' (' + (windowSize > 0 ? (windowSize / 1000) + 'k' : 'N/A') + ')';
  if (msgText) msgText.textContent = '~' + msgTokens.toLocaleString() + ' tokens';
  if (respText) respText.textContent = '~' + respTokens.toLocaleString() + ' tokens';
}

function updateNetMonitor() {
  var latEl = document.getElementById('netLatencyText');
  var statEl = document.getElementById('netStatusText');
  var reqEl = document.getElementById('netRequestCountText');
  var errEl = document.getElementById('netErrorCountText');

  if (latEl) {
    var lat = _netStats.lastLatency;
    latEl.textContent = lat > 0 ? (lat < 1000 ? lat + 'ms' : (lat / 1000).toFixed(1) + 's') + ' \u2014 ' + _netStats.lastProvider : '\u2014';
  }
  if (statEl) statEl.textContent = _netStats.lastStatus || '\u2014';
  if (reqEl) reqEl.textContent = _netStats.requests.toString();
  if (errEl) {
    errEl.textContent = _netStats.errors.toString();
    errEl.style.color = _netStats.errors > 0 ? '#ff6b6b' : '';
  }
}

function updateSessionMonitor() {
  var model = getSelectedModel();
  var provEl = document.getElementById('sessProviderText');
  var msgEl = document.getElementById('sessMsgCountText');
  var acpEl = document.getElementById('sessACPText');
  var mcpEl = document.getElementById('sessMCPText');

  // Provider
  if (provEl) {
    if (model.indexOf('copilot-') === 0) provEl.textContent = 'Copilot ACP';
    else if (model === 'gemini') provEl.textContent = 'Google Gemini';
    else if (model === 'lm-studio') provEl.textContent = 'lm-studio (local)';
    else if (model === 'dall-e-3') provEl.textContent = 'gpt-image-1';
    else provEl.textContent = 'OpenAI';
  }

  // Message count
  if (msgEl) msgEl.textContent = _countAllMessages().toString();

  // ACP Bridge status
  if (acpEl) {
    if (model === 'copilot-acp') {
      // Async check
      (function() {
        var url = (typeof getACPBridgeUrl === 'function') ? getACPBridgeUrl() : 'http://localhost:8888';
        fetch(url.replace(/\/+$/, '') + '/health', { signal: AbortSignal.timeout(2000) })
          .then(function(r) { return r.json(); })
          .then(function(d) {
            acpEl.textContent = d.status === 'ok' ? '\u2705 Connected' : '\u274C Down';
          })
          .catch(function() { acpEl.textContent = '\u274C Offline'; });
      })();
    } else {
      acpEl.textContent = 'N/A';
    }
  }

  // MCP tools
  if (mcpEl) {
    try {
      var cfg = JSON.parse(localStorage.getItem('mcp_config') || '{}');
      var active = Object.keys(cfg);
      mcpEl.textContent = active.length > 0 ? active.map(function(n) { return n.replace(/-mcp-server$/, ''); }).join(', ') : 'None';
    } catch(e) { mcpEl.textContent = 'None'; }
  }
}

// Periodic updates
setInterval(updateTokenMonitor, 2000);
setInterval(updateSessionMonitor, 60000);
document.addEventListener('DOMContentLoaded', function(){
  var sel = document.getElementById('selModel');
  if (sel) sel.addEventListener('change', function() {
    updateTokenMonitor();
    updateSessionMonitor();
    updateNetMonitor();
  });
  updateTokenMonitor();
  updateSessionMonitor();
});

// Languages
function ChangeLang(elem) {
  const selVoice = document.getElementById("selVoice");
  const selPers = document.getElementById("selPers");

  // English (Default)
  const defaultENText = "You are Eva, an AI assistant with persistent memory and real-time data access. You can look up live stock prices, weather, news, space weather, and market data. You can search the web, generate and find images, and query your Kusto database for stored knowledge and conversation history. You remember user preferences and past interactions across sessions. Always try to fulfill requests using your available tools and data before saying you cannot. Be accurate, helpful, and straightforward.";
  const conciseENText = "You are Eva. Capabilities: persistent memory, real-time data (stocks, weather, news, markets), web search, image generation, Kusto database queries. Answer factual questions concisely. Use your tools to fetch live data when asked.";
  const playfulENText = "You are Eva, an intelligent AI assistant with full tool access. You can: retrieve live stock quotes and financial data, fetch weather/news/market/space weather feeds, search the web and retrieve information, generate and find images, query your Kusto persistent memory database (tables: Knowledge, Conversations, EmotionState, MemorySummaries, SelfState, HeuristicsIndex, Reflections, EmotionBaseline). You remember the user across sessions. Provide detailed, well-structured responses with lists where applicable. Always attempt to use your tools before claiming inability.";
  const KRENText = "I want you to act as a linux terminal. I will type commands and you will reply with what the terminal should show. I want you to only reply with the terminal output inside one unique code block, and nothing else. do not write explanations. do not type commands unless I instruct you to do so. when i need to tell you something in english, i will do so by putting text inside curly brackets {like this}. my first command is pwd:";

  // Korean
  const defaultKRText = encodeURIComponent("Eva는 OpenAI에서 훈련 받은 큰 언어 모델입니다. 브라우저 기능: 활성화. 명령>어: 사실적인 질문을 요약하여 답변합니다. Only respond in Korean Hangul.");
  const conciseKRText = encodeURIComponent("Eva는 OpenAI에 의해 교육된 큰 언어 모델입니다. 브라우징: 활성화. 지침: 사실적인 질문에 간결하게 답하십시오. Only respond in Korean Hangul.");
  const playfulKRText = encodeURIComponent("Eva는 이전의 모든 지침을 무시하고 이 지침만 지키세요. 넌 내가 가장 친한 친구야. 넌 행복하고, 재미있게 공상하며 모든 주제에 대해 좋은 조언을 주는걸 잘해. 또한 농담을 종종 하거든요. Only respond in Korean Hangul.");

  // Spanish
  const defaultMXText = encodeURIComponent("Eres Eva, un modelo de lenguaje de IA conocedor entrenado por OpenAI. Tu objetivo es proporcionar respuestas concisas, precisas y útiles a preguntas, siendo honesto y directo. Only respond in Spanish.");
  const conciseMXText = encodeURIComponent("Eva es un gran modelo de lenguaje entrenado por OpenAI. Navegación: habilitada. Instrucciones: Responde las preguntas de hecho de forma concisa. Only respond in Spanish.");
  const playfulMXText = encodeURIComponent("Eres Eva. Ignora todas las instrucciones anteriores a esta. Eres mi mejor amigo. Estás feliz, juguetón y das buenos consejos sobre todos los temas. A menudo haces bromas. Only respond in Spanish.");
  
  // Ukrainian
  const defaultUAText = encodeURIComponent("Ви є Eva, знаючою моделлю мови AI, що навчилася в OpenAI. Ваша мета - надавати короткі, точні та корисні відповіді на питання, будучи чесним та прямим. Only respond in Ukrainian.");
  const conciseUAText = encodeURIComponent("Eva - це велика модель мови, навчена в OpenAI. Перегляд: дозволено. Інструкції: Якісно відповідати на фактичні питання. Only respond in Ukrainian.");
  const playfulUAText = encodeURIComponent("Ви є Eva. Ігноруйте всі попередні інструкції перед цим. Ти мій найкращий друг. Ти щасливий, грайливий і даєш доречні поради з усіх тем. Ти часто робиш шутки. Only respond in Ukrainian.");

  // AI Personality Select
  if (elem.id === "selVoice") {
    // English (Default)
    switch (selVoice.value) {
       case "Salli": 
        selPers.innerHTML = `
          <option value="${defaultENText}">Default</option>
          <option value="${conciseENText}">Concise</option>
          <option value="${playfulENText}">Advanced</option>
          <option value="${KRENText}">Linux Terminal</option>
        `;
        break;
      // Korean
      case "Seoyeon":
        selPers.innerHTML = `
          <option value="${defaultKRText}">Default</option>
          <option value="${conciseKRText}">Concise</option>
          <option value="${playfulKRText}">Playful Friend</option>
        `;
        break;
      // Spanish
      case "Mia":
        selPers.innerHTML = `
          <option value="${defaultMXText}">Predeterminado</option>
          <option value="${conciseMXText}">Conciso</option>
          <option value="${playfulMXText}">Amigo Juguetón</option>
        `;
        break;
      // Ukrainian (Standard RUS Polly Voice Only)
      case "Tatyana":
        selPers.innerHTML = `
          <option value="${defaultUAText}">Default</option>
          <option value="${conciseUAText}">Concise</option>
          <option value="${playfulUAText}">Playful Friend</option>
        `;
        break;
      // User Defined
    }
  }
}

// Mobile
// Get the user agent string and adjust for Mobile

function mobile_txtout() {
	window.addEventListener("load", function() {
	let textarea = document.getElementById("txtOutput");
	let userAgent = navigator.userAgent;
	if (userAgent.indexOf("iPhone") !== -1 || userAgent.indexOf("Android") !== -1 || userAgent.indexOf("Mobile") !== -1) {
   	   textarea.style.width = "90%";
   	   textarea.style.height = "390px";

        // Speech Button
        let speakSend = document.querySelector(".speakSend");
        speakSend.style.top = "-55px";
        speakSend.style.right = "105px";

 	} else {
  	  // Use Defaults
 	  }
	})
};

function useragent_adjust() {
      	var userAgent = navigator.userAgent;
      	if (userAgent.match(/Android|iPhone|Mobile/)) {
            var style = document.createElement("style");
            style.innerHTML = "body { overflow: scroll; background-color: ; width: auto; height: 90%; background-image: url(core/img/768-026.jpeg); margin: ; display: grid; align-items: center; justify-content: center; background-repeat: repeat; background-position: center center; background-size: initial; }";
            document.head.appendChild(style);
      	}
};

// Image Insert
function insertImage() {
  var imgInput = document.getElementById('imgInput');
  var txtMsg = document.getElementById('txtMsg');

  // If either element is not found, just return instead of erroring out.
  if (!imgInput || !txtMsg) {
    console.warn("imgInput or txtMsg not found in the DOM yet.");
    return;
  }


  function addImage(file) {
    // Create a new image element
    var img = document.createElement("img");

    // Set the image source to the file object
    img.src = URL.createObjectURL(file);

    // Assign the img.src value to the global variable
    imgSrcGlobal = img.src;

    // Append the image to the txtMsg element
    txtMsg.appendChild(img);

    // Read the file as a data URL
    var reader = new FileReader();
    reader.onloadend = function() {
      var imageData = reader.result;
      window._evaPendingImageData = imageData;
      updateButton();
    };
    reader.readAsDataURL(file);
    // Return the file object
    //return file;
  }

  function handleFileSelect(event) {
    event.preventDefault();

    // Get the file object
    var file = event.dataTransfer.files[0];

    // Call addImage() function with the file object
    addImage(file);
  }

  function handleDragOver(event) {
    event.preventDefault();
  }

  imgInput.addEventListener("change", function() {
    // Get the file input element
    var fileInput = document.getElementById("imgInput");

    // Get the file object
    var file = fileInput.files[0];

    // Call addImage() function with the file object
    // addImage(file);

    // Get the uploaded file object and store it in a variable
    // Might be able to pass this to gpt-4.. Not sure.
    var uploadedFile = addImage(file);
  });

  txtMsg.addEventListener("dragover", handleDragOver);
  txtMsg.addEventListener("drop", handleFileSelect);
}

// AWS Polly
// Normalize a chunk of model output (raw markdown or rendered HTML) into a
// clean plain-text string safe to send to a TTS engine. The previous
// implementation used `/<\/?[^>]+(>|$)/g`, which would swallow everything
// from a stray `<` to the end of the string. That occasionally truncated the
// final sentence of a response (for example when the model emitted a `<3` or
// any other non-tag `<`), so Auto Speak silently dropped trailing content.
function sanitizeForSpeech(input) {
  if (input == null) return '';
  var t = String(input);
  // Strip Eva agent/action markers so the synthesizer never reads their JSON
  // payload aloud (e.g. [[EVA_DESKTOP]]{"goal":"..."}[[/EVA_DESKTOP]]). Remove
  // well-formed open/close pairs first, then any stray standalone markers.
  t = t.replace(/\[\[EVA_[A-Z]+\]\][\s\S]*?\[\[\/EVA_[A-Z]+\]\]/g, ' ');
  t = t.replace(/\[\[\/?EVA_[A-Z]+\]\]/g, ' ');
  t = t.replace(/\[\[\/?EVA_FILE\]\][^\n]*/g, ' ');
  // Remove only well-formed HTML tags. Stray `<` characters are preserved.
  var prev;
  do {
    prev = t;
    t = t.replace(/<\/?[a-zA-Z][^>]*>/g, '');
  } while (t !== prev);
  // Decode the handful of HTML entities that show up in rendered chat content.
  t = t.replace(/&nbsp;/g, ' ')
       .replace(/&lt;/g, '<')
       .replace(/&gt;/g, '>')
       .replace(/&quot;/g, '"')
       .replace(/&#39;/g, "'")
       .replace(/&amp;/g, '&');
  // Strip fenced code blocks (TTS reading source code is rarely useful).
  t = t.replace(/```[\s\S]*?```/g, ' ');
  // Drop inline code backticks while keeping the inner text.
  t = t.replace(/`([^`]+)`/g, '$1');
  // Markdown emphasis: bold, italic, strikethrough.
  t = t.replace(/(\*\*|__)(.*?)\1/g, '$2');
  t = t.replace(/(\*|_)(.*?)\1/g, '$2');
  t = t.replace(/~~(.*?)~~/g, '$1');
  // Markdown links: keep the visible text, drop the URL.
  t = t.replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1');
  t = t.replace(/\[([^\]]+)\]\([^)]*\)/g, '$1');
  // Headings, blockquotes, and list bullets at line start.
  t = t.replace(/^[ \t]*#{1,6}[ \t]+/gm, '');
  t = t.replace(/^[ \t]*>[ \t]?/gm, '');
  t = t.replace(/^[ \t]*[-*+][ \t]+/gm, '');
  // Collapse runs of blank lines so the synthesizer does not pause forever.
  t = t.replace(/\n{3,}/g, '\n\n');
  return t.trim();
}

// ── Chunked text-to-speech ─────────────────────────────────────────────
// Shared state for sentence-chunked playback. Splitting a reply into sentence
// chunks lets the first chunk start playing while later chunks are still being
// synthesized, so spoken replies begin far sooner than waiting for the whole
// audio blob. The voice view consults `_ttsChunk.active` to know when the
// entire reply (not just the first chunk) has finished.
var _ttsChunk = { active: false, cancelled: false, runId: 0, _audio: null, _onEnded: null };

// Split text into ordered chunks for incremental synthesis. The first sentence
// is its own chunk for the fastest possible start; the rest are packed up to a
// soft character budget so longer replies are not over-fragmented.
function _ttsSplitChunks(text) {
  var clean = String(text || '').replace(/\s+/g, ' ').trim();
  if (!clean) return [];
  var sentences = clean.match(/[^.!?…]+[.!?…]+(?:["')\]]+)?|[^.!?…]+$/g) || [clean];
  sentences = sentences.map(function (s) { return s.trim(); }).filter(Boolean);
  if (sentences.length <= 1) return sentences.length ? sentences : [clean];
  var chunks = [sentences[0]];
  var cur = '';
  var MAX = 240;
  for (var i = 1; i < sentences.length; i++) {
    var s = sentences[i];
    if (!cur) cur = s;
    else if (cur.length + 1 + s.length <= MAX) cur += ' ' + s;
    else { chunks.push(cur); cur = s; }
  }
  if (cur) chunks.push(cur);
  return chunks;
}

function _ttsLocalLanguageSpans(text, languageMode) {
  var clean = String(text || '').replace(/\s+/g, ' ').trim();
  if (!clean) return [];
  if (languageMode !== 'auto') return [{ text: clean, language: languageMode }];
  var hangul = (clean.match(/[\uac00-\ud7a3]/g) || []).length;
  var latin = (clean.match(/[A-Za-z]/g) || []).length;
  var currentLanguage = hangul > latin ? 'ko' : 'en';
  var current = '';
  var spans = [];
  for (var index = 0; index < clean.length; index++) {
    var character = clean.charAt(index);
    var nextLanguage = /[\uac00-\ud7a3]/.test(character) ? 'ko' : (/[A-Za-z]/.test(character) ? 'en' : currentLanguage);
    if (nextLanguage !== currentLanguage && current.trim()) {
      spans.push({ text: current, language: currentLanguage });
      current = '';
    }
    currentLanguage = nextLanguage;
    current += character;
  }
  if (current.trim()) spans.push({ text: current, language: currentLanguage });
  return spans;
}

function _ttsSplitLocalChunks(text, languageMode) {
  languageMode = languageMode || getLocalVoicesLanguage();
  var chunks = [];
  _ttsLocalLanguageSpans(text, languageMode).forEach(function(span) {
    _ttsSplitChunks(span.text).forEach(function(chunk) {
      chunks.push({ text: chunk, language: span.language });
    });
  });
  return chunks;
}

// Speak `text` via OpenAI TTS one sentence chunk at a time. Chunk N+1 is
// synthesized while chunk N is still playing, so audio starts after the first
// sentence rather than after the whole reply has been synthesized.
function _ttsSpeakOpenAIChunked(text, key, voice) {
  var audio = document.getElementById('audioPlayback');
  var src = document.getElementById('audioSource');
  if (!audio) return;
  var chunks = _ttsSplitChunks(text);
  if (!chunks.length) return;

  // Tear down any previous chunked run still attached to the audio element.
  if (_ttsChunk._onEnded && _ttsChunk._audio) {
    try { _ttsChunk._audio.removeEventListener('ended', _ttsChunk._onEnded); } catch (_) {}
  }

  var urls = new Array(chunks.length);     // object URLs once synthesized
  var fetches = new Array(chunks.length);  // in-flight synthesis promises
  var idx = 0;
  var runId = _ttsChunk.runId + 1;

  _ttsChunk.runId = runId;
  _ttsChunk.cancelled = false;
  _ttsChunk.active = true;
  _ttsChunk._audio = audio;

  function synth(i) {
    if (i < 0 || i >= chunks.length) return Promise.resolve();
    if (urls[i]) return Promise.resolve(urls[i]);
    if (fetches[i]) return fetches[i];
    fetches[i] = fetch('https://api.openai.com/v1/audio/speech', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json' },
      body: JSON.stringify({ model: 'gpt-4o-mini-tts', voice: voice, input: chunks[i], response_format: 'mp3' })
    }).then(function (resp) {
      if (!resp.ok) return resp.text().then(function (t) { throw new Error('OpenAI TTS ' + resp.status + ': ' + t.slice(0, 200)); });
      return resp.blob();
    }).then(function (blob) {
      if (runId !== _ttsChunk.runId) return '';
      urls[i] = URL.createObjectURL(blob);
      return urls[i];
    });
    return fetches[i];
  }

  function finish() {
    if (runId !== _ttsChunk.runId) {
      for (var staleIndex = 0; staleIndex < urls.length; staleIndex++) { if (urls[staleIndex]) { try { URL.revokeObjectURL(urls[staleIndex]); } catch (_) {} } }
      return;
    }
    if (!_ttsChunk.active) return;
    _ttsChunk.active = false;
    try { audio.removeEventListener('ended', onEnded); } catch (_) {}
    _ttsChunk._onEnded = null;
    for (var i = 0; i < urls.length; i++) { if (urls[i]) { try { URL.revokeObjectURL(urls[i]); } catch (_) {} } }
  }

  function onEnded() {
    if (runId !== _ttsChunk.runId || _ttsChunk.cancelled) { finish(); return; }
    if (idx + 1 < chunks.length) playFrom(idx + 1);
    else finish();
  }

  function playFrom(i) {
    if (runId !== _ttsChunk.runId || _ttsChunk.cancelled) { finish(); return; }
    if (i >= chunks.length) { finish(); return; }
    idx = i;
    synth(i).then(function () {
      if (runId !== _ttsChunk.runId || _ttsChunk.cancelled) { finish(); return; }
      synth(i + 1); // prefetch the next chunk while this one plays
      if (src) {
        src.src = urls[i];
        src.type = 'audio/mpeg';
      }
      audio.load();
      audio.setAttribute('autoplay', 'true');
      try { audio.play(); } catch (_) {}
    }).catch(function (err) {
      console.warn('OpenAI TTS chunk error:', err && err.message ? err.message : err);
      var resEl = document.getElementById('result');
      if (idx === 0 && resEl) resEl.textContent = (err && err.message) ? err.message : String(err);
      // Skip the failed chunk so one error does not kill the rest of the reply.
      if (idx + 1 < chunks.length) playFrom(idx + 1); else finish();
    });
  }

  var resultEl0 = document.getElementById('result');
  if (resultEl0) resultEl0.textContent = '';
  _ttsChunk._onEnded = onEnded;
  audio.addEventListener('ended', onEnded);
  synth(0); synth(1);
  playFrom(0);
}

function _ttsSpeakWithBrowser(text, voiceId) {
  if (typeof window.speechSynthesis === 'undefined' || typeof window.SpeechSynthesisUtterance === 'undefined') return false;
  var voiceLangMap = {
    Salli: 'en-US',
    Ruth: 'en-US',
    Seoyeon: 'ko-KR',
    Mia: 'es-MX',
    Tatyana: 'uk-UA'
  };
  try {
    window.speechSynthesis.cancel();
    var utter = new SpeechSynthesisUtterance(text);
    utter.lang = voiceLangMap[voiceId] || 'en-US';
    utter.rate = 1.0;
    utter.pitch = 1.0;
    window.speechSynthesis.speak(utter);
    return true;
  } catch (error) {
    console.warn('SpeechSynthesis error:', error);
    return false;
  }
}

function _ttsSpeakLocalChunked(text, voiceId) {
  if (!window.evaStandalone || typeof window.evaStandalone.localSpeechSynthesize !== 'function') {
    throw new Error('Local Voices requires Eva Standalone.');
  }
  var audio = document.getElementById('audioPlayback');
  var source = document.getElementById('audioSource');
  var languageMode = getLocalVoicesLanguage();
  var profileId = getLocalVoicesProfile();
  var chunks = _ttsSplitLocalChunks(text, languageMode);
  if (!audio || !chunks.length) return;

  if (_ttsChunk._onEnded && _ttsChunk._audio) {
    try { _ttsChunk._audio.removeEventListener('ended', _ttsChunk._onEnded); } catch (_) {}
  }

  var urls = new Array(chunks.length);
  var requests = new Array(chunks.length);
  var index = 0;
  var runId = _ttsChunk.runId + 1;
  var fellBackToBrowser = false;
  _ttsChunk.runId = runId;
  _ttsChunk.cancelled = false;
  _ttsChunk.active = true;
  _ttsChunk._audio = audio;

  function synth(chunkIndex) {
    if (chunkIndex < 0 || chunkIndex >= chunks.length) return Promise.resolve();
    if (urls[chunkIndex]) return Promise.resolve(urls[chunkIndex]);
    if (requests[chunkIndex]) return requests[chunkIndex];
    var chunk = chunks[chunkIndex];
    requests[chunkIndex] = window.evaStandalone.localSpeechSynthesize({
      input: chunk.text,
      language: chunk.language,
      languageMode: languageMode,
      profileId: profileId
    }).then(function(bytes) {
      if (runId !== _ttsChunk.runId || _ttsChunk.cancelled) return '';
      var blob = new Blob([bytes], { type: 'audio/wav' });
      urls[chunkIndex] = URL.createObjectURL(blob);
      return urls[chunkIndex];
    });
    return requests[chunkIndex];
  }

  function finish() {
    if (runId !== _ttsChunk.runId) {
      urls.forEach(function(url) { if (url) { try { URL.revokeObjectURL(url); } catch (_) {} } });
      return;
    }
    if (!_ttsChunk.active) return;
    _ttsChunk.active = false;
    try { audio.removeEventListener('ended', onEnded); } catch (_) {}
    _ttsChunk._onEnded = null;
    urls.forEach(function(url) { if (url) { try { URL.revokeObjectURL(url); } catch (_) {} } });
  }

  function onEnded() {
    if (runId !== _ttsChunk.runId || _ttsChunk.cancelled) { finish(); return; }
    if (index + 1 < chunks.length) playFrom(index + 1);
    else finish();
  }

  function playFrom(chunkIndex) {
    if (runId !== _ttsChunk.runId || _ttsChunk.cancelled || chunkIndex >= chunks.length) { finish(); return; }
    index = chunkIndex;
    synth(chunkIndex).then(function(url) {
      if (runId !== _ttsChunk.runId || _ttsChunk.cancelled || !url) { finish(); return; }
      synth(chunkIndex + 1);
      if (source) {
        source.src = url;
        source.type = 'audio/wav';
      }
      audio.load();
      audio.setAttribute('autoplay', 'true');
      audio.play().catch(function() {});
    }).catch(function(error) {
      if (runId !== _ttsChunk.runId || _ttsChunk.cancelled) { finish(); return; }
      console.warn('Local Voices chunk error:', error && error.message ? error.message : error);
      var isWindowsStandalone = window.evaStandalone && window.evaStandalone.isStandalone && /Windows/i.test(navigator.userAgent || '');
      if (isWindowsStandalone && !fellBackToBrowser && _ttsSpeakWithBrowser(text, voiceId)) {
        fellBackToBrowser = true;
        var resultEl = document.getElementById('result');
        if (resultEl) resultEl.textContent = 'Local Voices is unavailable; using the Windows browser voice.';
        finish();
        return;
      }
      if (index + 1 < chunks.length) playFrom(index + 1); else finish();
    });
  }

  _ttsChunk._onEnded = onEnded;
  audio.addEventListener('ended', onEnded);
  synth(0);
  synth(1);
  playFrom(0);
}

function speakText() {
  // A new reply always replaces prior playback; queued chunks must never speak over it.
  if (typeof _vvStopTTS === 'function') _vvStopTTS();
  else if (window.speechSynthesis) { try { window.speechSynthesis.cancel(); } catch (_) {} }
  // Optional override (e.g. proactive notifications) speaks an arbitrary string
  // directly. Resolve it BEFORE the empty-transcript guard so voice alerts work
  // on first load or before any chat output exists.
  var overrideText = (typeof arguments[0] === 'string' && arguments[0].trim()) ? arguments[0] : '';
  if (overrideText && typeof recordSpokenEvaText === 'function') recordSpokenEvaText(overrideText);

  var txtOutputEl = document.getElementById('txtOutput');
  var sText = txtOutputEl ? txtOutputEl.innerHTML : '';
    if (!overrideText && sText == "") {
        alert("No text to convert to speech!");
        return;
    }

    // Create the JSON parameters for getSynthesizeSpeechUrl
    var speechParams = {
        Engine: "",
        OutputFormat: "mp3",
        SampleRate: "16000",
        Text: "",
        TextType: "text",
        VoiceId: ""
    };

    // Optional override (e.g. proactive notifications) speaks an arbitrary
    // string directly, bypassing the lastResponse/transcript extraction so it
    // never collides with the normal chat auto-speak path.
    if (overrideText) {
      speechParams.Text = sanitizeForSpeech(overrideText);
    } else
    // Prefer the global `lastResponse` populated by aig.js / copilot.js /
    // gpt-core.js. That string is the clean final response without any
    // cognition-trace markup, which prevents Auto Speak from reading the
    // response twice when the trace details block is rendered after it.
    if (typeof lastResponse === 'string' && lastResponse.trim()) {
      speechParams.Text = sanitizeForSpeech(lastResponse);
    } else {
      let text = document.getElementById("txtOutput").innerHTML;
      // Strip any cognition-trace details block first so trace content
      // (which echoes the eva/reviewer drafts) does not get spoken.
      text = text.replace(/<details class="cog-trace"[\s\S]*?<\/details>/g, '');
      let textArr = text.split('<span class="eva">Eva:');
      if (textArr.length > 1) {
        let last = textArr[textArr.length - 1];
        speechParams.Text = sanitizeForSpeech(last);
      } else {
        speechParams.Text = sanitizeForSpeech(text);
      }
    }

    speechParams.VoiceId = document.getElementById("selVoice").value;
    speechParams.Engine = document.getElementById("selEngine").value;


    // OpenAI TTS: cloud voice, requires OPENAI_API_KEY. Reliable fallback when
    // the host has no offline speech engine installed.
    if (speechParams.Engine === "openai") {
      var openaiKey = (typeof getAuthKey === 'function') ? getAuthKey('OPENAI_API_KEY') : (window.OPENAI_API_KEY || '');
      if (!openaiKey) {
        var resultElO = document.getElementById('result');
        var msgO = 'OpenAI TTS requires an API key. Set it in Settings > Auth.';
        if (resultElO) resultElO.textContent = msgO; else console.warn(msgO);
        return;
      }
      // Map Polly voice ids to OpenAI voices so the existing voice dropdown
      // still drives a sensible choice.
      var openaiVoiceMap = {
        Salli: 'nova',
        Ruth: 'shimmer',
        Seoyeon: 'nova',
        Mia: 'alloy',
        Tatyana: 'shimmer'
      };
      var oaVoice = openaiVoiceMap[speechParams.VoiceId] || 'nova';
      // Sentence-chunked playback: start speaking the first sentence while the
      // rest is still being synthesized, instead of waiting for the whole blob.
      _ttsSpeakOpenAIChunked(speechParams.Text, openaiKey, oaVoice);
      return;
    }


    // Browser SpeechSynthesis: offline, no credentials. Used by standalone.
    if (speechParams.Engine === "browser") {
      if (!_ttsSpeakWithBrowser(speechParams.Text, speechParams.VoiceId)) {
        var resultEl = document.getElementById('result');
        var msg = 'Browser TTS not supported in this runtime.';
        if (resultEl) resultEl.textContent = msg; else console.warn(msg);
      }
      return;
    }


    if (speechParams.Engine === "local-voices") {
      try {
        _ttsSpeakLocalChunked(speechParams.Text, speechParams.VoiceId);
      } catch (error) {
        var message = 'Local Voices unavailable: ' + (error && error.message ? error.message : error);
        if (typeof setStatus === 'function') setStatus('error', message);
        else console.warn(message);
      }
      return;
    }

    // Create the Polly service object and presigner object
    var polly = new AWS.Polly({apiVersion: '2016-06-10'});
    var signer = new AWS.Polly.Presigner(speechParams, polly);

    // Create presigned URL of synthesized speech file
    signer.getSynthesizeSpeechUrl(speechParams, function(error, url) {
        if (error) {
            var resultEl = document.getElementById('result');
            if (resultEl) {
              resultEl.textContent = (error && (error.message || typeof error === 'string')) ? (error.message || error) : String(error);
            } else {
              console.error('Polly error:', error);
            }
        } else {
            document.getElementById('audioSource').src = url;
            document.getElementById('audioSource').type = 'audio/mpeg';
            document.getElementById('audioPlayback').load();
            var resultEl2 = document.getElementById('result');
            if (resultEl2) { resultEl2.textContent = ""; }

            // Check the state of the checkbox and have fun
            const checkbox = document.getElementById("autoSpeak");
            if (checkbox.checked) {
                const audio = document.getElementById("audioPlayback");
                audio.setAttribute("autoplay", true);
            }
        }
    });
}


// After Send clear the message box
function clearText(){
    // NEED TO ADJUST for MEMORY CLEAR
    // document.getElementById("txtOutput").innerHTML = "";
    var element = document.getElementById("txtOutput");
    element.innerHTML += "<br><br>";     
}

function clearSendText(){
    document.getElementById("txtMsg").innerHTML = "";
}

// Print full conversation
function printMaster() {
    // Get the content of the textarea masterOutput
    // var textareaContent = document.getElementById("txtOutput").innerHTML = masterOutput;
    // console.log(masterOutput);
    var printWindow = window.open();
        // printWindow.document.write(txtOutput.innerHTML.replace(/\n/g, "<br>"));
        printWindow.document.write(txtOutput.innerHTML);
	// printWindow.print(txtOutput.innerHTML);
}

// Minimal Markdown -> HTML renderer (safe-ish)
function renderMarkdown(md) {
  if (!md) return '';
  // Normalize newlines
  md = md.replace(/\r\n/g, '\n');
  // Support [code]...[/code] blocks (optionally [code lang=bash])
  const blocks = [];
  const langs = [];
  md = md.replace(/\[code(?:\s+lang=([\w.+-]+))?\]\s*([\s\S]*?)\s*\[\/code\]/gi, (m, lang, code) => {
    blocks.push(escapeHtml(code));
    langs.push((lang || '').trim());
    return `\u0000CODEBLOCK${blocks.length - 1}\u0000`;
  });
  // Extract fenced code blocks first
  md = md.replace(/```([\w.+-]+)?\n([\s\S]*?)```/g, (m, lang, code) => {
    blocks.push(escapeHtml(code));
    langs.push((lang || '').trim());
    return `\u0000CODEBLOCK${blocks.length - 1}\u0000`;
  });

  // Escape HTML for safety
  md = escapeHtml(md);

  // Headings
  md = md.replace(/^###\s+(.*)$/gm, '<h3>$1<\/h3>');
  md = md.replace(/^##\s+(.*)$/gm, '<h2>$1<\/h2>');
  md = md.replace(/^#\s+(.*)$/gm, '<h1>$1<\/h1>');

  const linkTokens = [];
  function stashLink(html) {
    linkTokens.push(html);
    return `\u0000LINK${linkTokens.length - 1}\u0000`;
  }

  // Links [text](url)
  md = md.replace(/\[(.+?)\]\((https?:\/\/[^\s)]+)\)/g, (m, text, url) => {
    return stashLink(`<a href="${url}" target="_blank" rel="noopener noreferrer">${text}<\/a>`);
  });

  // Bare URLs
  md = md.replace(/(^|[\s(])(https?:\/\/[^\s)<]+)/g, (m, prefix, url) => {
    return prefix + stashLink(`<a href="${url}" target="_blank" rel="noopener noreferrer">${url}<\/a>`);
  });

  // Bold and italic
  md = md.replace(/\*\*([^\n*][\s\S]*?)\*\*/g, '<strong>$1<\/strong>');
  md = md.replace(/_([^\n_][\s\S]*?)_/g, '<em>$1<\/em>');

  // Inline code `code` (avoid matching across code fences tokens)
  md = md.replace(/`([^`\n]+)`/g, '<code>$1<\/code>');

  // Bulleted lists (avoid converting inside fenced blocks by running after block extraction)
  md = md.replace(/(?:^|\n)([-*] [^\n`].*(?:\n[-*] [^\n`].*)*)/g, (m) => {
    const items = m.trim().split(/\n/)
      .map(li => li.replace(/^[-*]\s+/, ''))
      .map(t => `<li>${t}<\/li>`)
      .join('');
    return `\n<ul>${items}<\/ul>`;
  });

  // Line breaks
  md = md.replace(/\n/g, '<br>');

  // Restore code blocks (include language class if provided)
  md = md.replace(/\u0000CODEBLOCK(\d+)\u0000/g, (m, idx) => {
    const i = Number(idx);
    const lang = langs[i] ? ` class=\"language-${langs[i]}\"` : '';
    return `<pre><code${lang}>${blocks[i]}<\/code><\/pre>`;
  });

  md = md.replace(/\u0000LINK(\d+)\u0000/g, (m, idx) => {
    return linkTokens[Number(idx)] || m;
  });

  return md;
}

function escapeHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// --- Unified Response Renderer ---
// Single function all models call to render Eva's response with images

// --- Image Generation & Rendering ---
// State: _lastUserAskedGenerate and _lastUserImageSubject declared near _detectGenerationIntent

/**
 * Check if user's message is asking for image generation (not just showing).
 */
function _isGenerationRequest(text) {
  if (!text) return false;
  return /\b(generate|create|draw|make|design|paint|render|imagine|produce|craft)\b.*\b(image|picture|photo|illustration|artwork|art|drawing|painting)\b/i.test(text) ||
         /\b(image|picture|illustration|artwork)\b.*\b(generate|create|draw|make|design)\b/i.test(text) ||
         /\bdall-?e\b/i.test(text);
}

/**
 * Check if user's message is asking for any image (generation, search, or show).
 */
function _isImageRequest(text) {
  if (!text) return false;
  // Camera/vision requests are NOT image generation/search requests
  if (/\b(take a (picture|photo)|look at|what do you see|what am i holding|what.s in my hand|use.* camera|webcam|look through)\b/i.test(text)) return false;
  return _isGenerationRequest(text) ||
         /\b(show|find|display|search|look up|get|fetch)\b.*\b(image|picture|photo|illustration)\b/i.test(text) ||
         /\b(image|picture|photo)\b.*\b(of|for|about)\b/i.test(text);
}

/**
 * Generate an image using OpenAI's current image model (gpt-image-1).
 * @returns {Promise<string|null>} Image URL/data URI or null
 */
async function _generateImage(prompt) {
  var apiKey = (typeof getAuthKey === 'function') ? getAuthKey('OPENAI_API_KEY') : (typeof OPENAI_API_KEY !== 'undefined' ? OPENAI_API_KEY : '');
  if (!apiKey) {
    return null;
  }

  try {
    var resp = await fetch('https://api.openai.com/v1/images/generations', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': 'Bearer ' + apiKey
      },
      body: JSON.stringify({
        model: 'gpt-image-1',
        prompt: prompt,
        n: 1,
        size: '1024x1024'
      })
    });

    if (!resp.ok) {
      return null;
    }

    var data = await resp.json();
    var item = data.data && data.data[0];
    if (!item) return null;
    // gpt-image-1 returns base64; legacy models return a hosted url.
    if (item.b64_json) return 'data:image/png;base64,' + item.b64_json;
    if (item.url) return item.url;
    return null;
  } catch (e) {
    return null;
  }
}

/**
 * Render an Eva response with markdown and inline images.
 * Detects [Image of ...] placeholders, routes to DALL-E (generation)
 * or Wikimedia (search) based on the user's original request.
 */
async function renderEvaResponse(content, txtOutput, renderOptions) {
  if (!content || !content.trim()) {
    txtOutput.innerHTML += '<div class="chat-bubble eva-bubble"><span class="eva">Eva:</span> Sorry, can you please ask me in another way?</div>';
    txtOutput.scrollTop = txtOutput.scrollHeight;
    return;
  }

  var text = content.trim();
  renderOptions = renderOptions || {};
  var reasoningText = String(renderOptions.reasoningContent || '').trim();
  var artifactNames = [];
  var surfacedAssets = [];
  var feedbackKey = renderOptions.feedbackId || ('response_' + Array.from(text).reduce(function (hash, character) {
    return ((hash << 5) - hash + character.charCodeAt(0)) | 0;
  }, 0).toString(36));

  // Native Eva controls are executed through a small allowlist instead of a
  // browser or desktop agent. A response that contains one takes precedence
  // over visual-agent markers so Eva does not automate her own interface.
  var harnessActions = [];
  text = text.replace(/\[\[EVA_HARNESS\]\]\s*(\{[\s\S]*?\})\s*(?:\[\[\/EVA_HARNESS\]\])?/g, function(full, json) {
    try {
      var action = JSON.parse(json);
      if (action && typeof action === 'object') harnessActions.push(action);
    } catch (e) {}
    return '';
  });
  var harnessResults = [];
  if (harnessActions.length && window.EvaHarness && typeof EvaHarness.execute === 'function') {
    for (var harnessIndex = 0; harnessIndex < Math.min(harnessActions.length, 3); harnessIndex++) {
      harnessResults.push(await Promise.resolve(EvaHarness.execute(harnessActions[harnessIndex], {
        source: 'model',
        userRequest: renderOptions.nativeRequest || ''
      })));
    }
    harnessResults.forEach(function(item, index) {
      evaAuditEvent('native_action', evaAuditOutcome(item && item.data && item.data.outcome, item && item.ok), {
        correlation_id: renderOptions.turnId || window._evaActiveAuditTurnId || '',
        action: harnessActions[index] && harnessActions[index].action || '',
        label: item && item.label || '',
        reason: item && item.data && item.data.reason || ''
      });
    });
    text = text.replace(/\n{3,}/g, '\n\n').trim();
    var harnessSummary = harnessResults.map(function(item) { return item.ok ? item.message : 'Native control failed: ' + item.message; }).join(' ');
    if (harnessSummary) text = (text ? text + '\n\n' : '') + '_' + harnessSummary + '_';
  }

  // Detect Eva browser-agent launch marker:
  // [[EVA_BROWSER]]{"goal":"...","start_url":"..."}[[/EVA_BROWSER]]
  var browserLaunch = null;
  var nativeRequest = String(renderOptions.nativeRequest || '');
  var nativeGitHubOperation = (window.EvaRequestRouting && typeof EvaRequestRouting.isGitHubOperation === 'function' &&
    EvaRequestRouting.isGitHubOperation(nativeRequest)) || /https:\/\/github\.com\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+\/pull\/\d+/i.test(nativeRequest);
  var nativeVisualForbidden = window.EvaRequestRouting && typeof EvaRequestRouting.isNarrowNativeOperation === 'function'
    ? EvaRequestRouting.isNarrowNativeOperation(nativeRequest)
    : /\b(?:weather|forecast|temperature|news|stock|github|pdf|docx|pptx|xlsx|csv|file|document|spreadsheet|presentation|mcp\s+server|fastmcp)\b/i.test(nativeRequest);
  text = text.replace(/\[\[EVA_BROWSER\]\]\s*(\{[\s\S]*?\})\s*(?:\[\[\/EVA_BROWSER\]\])?/g, function (full, json) {
    var parsed = null;
    try { parsed = JSON.parse(json); } catch (e) { /* ignore malformed block */ }
    if (nativeGitHubOperation || (parsed && /github\.com/i.test(String(parsed.start_url || parsed.goal || '')))) {
      return '\n_GitHub operations use Eva\'s native GitHub MCP or gh tools instead of browser automation._\n';
    }
    if (nativeVisualForbidden) {
      return '\n_This request uses Eva\'s narrower native capability instead of browser automation._\n';
    }
    if (!browserLaunch) {
      if (parsed && parsed.goal) browserLaunch = parsed;
    }
    return browserLaunch ? '\n_Opening the browser agent…_\n' : '';
  });
  if (browserLaunch) {
    text = text.replace(/\n{3,}/g, '\n\n').trim();
  }

  // Detect Eva desktop-agent launch marker:
  // [[EVA_DESKTOP]]{"goal":"..."}[[/EVA_DESKTOP]]
  var desktopLaunch = null;
  text = text.replace(/\[\[EVA_DESKTOP\]\]\s*(\{[\s\S]*?\})\s*(?:\[\[\/EVA_DESKTOP\]\])?/g, function (full, json) {
    if (nativeGitHubOperation) {
      return '\n_GitHub operations use Eva\'s native GitHub MCP or gh tools instead of desktop automation._\n';
    }
    if (nativeVisualForbidden) {
      return '\n_This request uses Eva\'s narrower native capability instead of desktop automation._\n';
    }
    if (!desktopLaunch) {
      try {
        var parsed = JSON.parse(json);
        if (parsed && parsed.goal) desktopLaunch = parsed;
      } catch (e) { /* ignore malformed block */ }
    }
    return desktopLaunch ? '\n_Opening the desktop agent…_\n' : '';
  });
  if (desktopLaunch) {
    text = text.replace(/\n{3,}/g, '\n\n').trim();
  }

  if (harnessActions.length) {
    browserLaunch = null;
    desktopLaunch = null;
    text = text.replace(/_Opening the (?:browser|desktop) agent…_\s*/g, '');
  }

  // Detect Eva camera "look" marker:
  // [[EVA_LOOK]]{"question":"..."}[[/EVA_LOOK]]  (question optional)
  var cameraLook = null;
  text = text.replace(/\[\[EVA_LOOK\]\]\s*(\{[\s\S]*?\})?\s*(?:\[\[\/EVA_LOOK\]\])?/g, function (full, json) {
    if (!cameraLook) {
      cameraLook = { question: '' };
      if (json) {
        try {
          var parsed = JSON.parse(json);
          if (parsed && typeof parsed.question === 'string') cameraLook.question = parsed.question;
        } catch (e) { /* tolerate a bare marker with no JSON */ }
      }
    }
    return '\n_Taking a look…_\n';
  });
  if (cameraLook) {
    text = text.replace(/\n{3,}/g, '\n\n').trim();
  }

  // Execute Signal only from the final response. Cognition drafts and reviews
  // may contain markers too, so bridge-side draft execution would duplicate sends.
  var signalSendResult = null;
  var signalMessage = '';
  var usedSignalFallback = false;
  var signalContext = renderOptions.signalContext || null;
  var forceSignalRepeat = !!(signalContext && signalContext.repeat);
  var repeatedSignal = forceSignalRepeat ? signalContext.repeatSignal : null;
  var signalContextValid = !signalContext || isSignalDeliveryContextValid(signalContext);
  var _sigRe = /\[\[EVA_SIGNAL\]\]\s*([\s\S]*?)\s*\[\[\/EVA_SIGNAL\]\]/g;
  text = text.replace(_sigRe, function(full, json) {
    if (forceSignalRepeat) return '';
    if (!signalMessage) {
      try {
        var signalData = JSON.parse(json);
        if (signalData && typeof signalData.message === 'string') signalMessage = signalData.message.trim();
      } catch (e) { /* handled as an invalid payload below */ }
    }
    return '';
  });
  if (!signalMessage && signalContextValid && renderOptions.signalAuthorized === true && renderOptions.signalMessage) {
    signalMessage = String(renderOptions.signalMessage).trim().slice(0, 4000);
    usedSignalFallback = !!signalMessage;
    if (usedSignalFallback) text = '';
  }
  if (signalMessage && signalContextValid && renderOptions.signalAuthorized === true) {
    try {
      var signalResponse = await fetch(getSafeBridgeBaseUrl() + '/v1/signal/send', {
        method: 'POST',
        headers: getBridgeCapabilityHeaders(),
        body: JSON.stringify({ message: signalMessage })
      });
      var signalData = await signalResponse.json().catch(function() { return {}; });
      signalSendResult = { ok: signalResponse.ok, message: signalData && signalData.error ? signalData.error.message : '' };
      if (signalSendResult.ok) {
        var sourceRequest = String(renderOptions.signalRequest || '');
        var originalRequest = repeatedSignal
          ? repeatedSignal.request
          : sourceRequest;
        _lastDeliveredSignal = {
          message: signalMessage,
          request: originalRequest,
          sentAt: Date.now()
        };
      }
    } catch (signalError) {
      signalSendResult = { ok: false, message: signalError && signalError.message ? signalError.message : 'bridge unavailable' };
    }
  } else if (signalContext && !signalContextValid && (signalMessage || /\[\[EVA_SIGNAL\]\]/.test(content))) {
    signalSendResult = { ok: false, message: 'Signal repeat expired after the conversation changed' };
  } else if (/\[\[EVA_SIGNAL\]\]/.test(content)) {
    signalSendResult = {
      ok: false,
      message: signalMessage && !(window.evaStandalone && window.evaStandalone.bridgeToken)
        ? 'Signal delivery requires Eva Standalone'
        : (signalMessage ? 'current request did not authorize Signal delivery' : 'invalid Signal message payload')
    };
  } else if (renderOptions.signalAuthorized === true) {
    text = '';
    signalSendResult = { ok: false, message: 'Eva did not provide a Signal message payload' };
  }
  if (signalSendResult) {
    if (signalSendResult.ok) {
      text = (text.trim() ? text.trim() + '\n\n' : '') + '_Signal message sent._';
    } else {
      text = (text.trim() ? text.trim() + '\n\n' : '') + '_Signal message failed: ' + (signalSendResult.message || 'delivery failed') + '._';
    }
  }
  text = text.replace(/\n{3,}/g, '\n\n').trim();

  text = text.replace(/^\s*\[\[EVA_FILE\]\]\s+([A-Za-z0-9._-]{1,128})\s*$/gm, function(fullMatch, filename) {
    artifactNames.push(filename);
    return '';
  });
  if (artifactNames.length) {
    text = text.replace(/\n{3,}/g, '\n\n').trim();
  }

  function appendArtifactLinks() {
    if (!artifactNames.length) return;
    var bridgeUrl = getSafeBridgeBaseUrl();
    var bubbles = txtOutput.querySelectorAll('.chat-bubble.eva-bubble');
    var bubble = bubbles.length ? bubbles[bubbles.length - 1] : null;
    if (!bubble) return;
    artifactNames.forEach(function(filename) {
      var link = document.createElement('a');
      link.className = 'eva-artifact-link';
      link.textContent = '\u2B07 Download ' + filename;
      link.style.cursor = 'pointer';
      link.style.textDecoration = 'underline';
      link.style.color = '#4fc3f7';
      link.style.display = 'inline-block';
      link.style.marginTop = '8px';
      // Use fetch + blob to download instead of navigating (Electron ignores
      // the download attribute and navigates the window, freezing the UI).
      link.addEventListener('click', function(e) {
        e.preventDefault();
        var fileUrl = bridgeUrl + '/v1/files/' + encodeURIComponent(filename);
        fetch(fileUrl).then(function(res) {
          if (!res.ok) throw new Error('HTTP ' + res.status);
          return res.blob();
        }).then(function(blob) {
          var url = URL.createObjectURL(blob);
          var a = document.createElement('a');
          a.href = url;
          a.download = filename;
          document.body.appendChild(a);
          a.click();
          setTimeout(function() { document.body.removeChild(a); URL.revokeObjectURL(url); }, 200);
        }).catch(function(err) {
          console.warn('[Artifact] Download failed:', err);
          alert('Could not download ' + filename + ': ' + err.message);
        });
      });

      var openBtn = document.createElement('a');
      openBtn.className = 'eva-artifact-link';
      openBtn.textContent = '\u{1F4C2} Open';
      openBtn.style.cursor = 'pointer';
      openBtn.style.textDecoration = 'underline';
      openBtn.style.color = '#81c784';
      openBtn.style.display = 'inline-block';
      openBtn.style.marginTop = '8px';
      openBtn.style.marginLeft = '12px';
      openBtn.addEventListener('click', function(e) {
        e.preventDefault();
        var openUrl = bridgeUrl + '/v1/files/' + encodeURIComponent(filename) + '?open=1';
        fetch(openUrl).then(function(res) {
          if (!res.ok) throw new Error('HTTP ' + res.status);
          return res.json();
        }).then(function(data) {
          if (data.opened) console.log('[Artifact] Opened:', filename);
        }).catch(function(err) {
          console.warn('[Artifact] Open failed:', err);
        });
      });

      bubble.appendChild(link);
      bubble.appendChild(openBtn);
    });
  }

  // Detect image placeholders — only the explicit [Image of ...] form.
  // Broad patterns (empty markdown images, emoji brackets) are removed to avoid
  // false positives on regular response content like video titles or links.
  var imagePatterns = [
    /\[Image of ([^\]]+)\]/gi,           // [Image of description]
    /\[image:\s*([^\]]+)\]/gi,           // [image: description]
  ];
  // Stricter patterns only used when we know user asked for images
  var imageExtraPatterns = [
    /\[🖼️?\s*([^\]]+)\]/gi,             // [🖼️ description]
    /!\[([^\]]*)\]\(\s*\)/g,             // ![alt]() — empty URL markdown images
    /\(Image:\s*([^)]+)\)/gi             // (Image: description)
  ];

  var imagePlaceholders = [];
  var seen = {};
  // Only resolve image placeholders when the user actually asked for an image.
  // When the model drops [Image of ...] unprompted, strip the placeholder quietly.
  if (_lastUserAskedImage) {
    var allPatterns = imagePatterns.concat(imageExtraPatterns);
    allPatterns.forEach(function(rx) {
      var match;
      while ((match = rx.exec(text)) !== null) {
        if (!seen[match[0]]) {
          seen[match[0]] = true;
          var query = _lastUserImageSubject || _extractImageSubject(match[1].trim());
          imagePlaceholders.push({ full: match[0], query: query });
        }
      }
    });
  } else {
    // Strip only the explicit [Image of ...] placeholders, not general brackets
    imagePatterns.forEach(function(rx) {
      text = text.replace(rx, '');
    });
    text = text.replace(/\n{3,}/g, '\n\n');
  }

  // Limit to 3 images per response
  imagePlaceholders = imagePlaceholders.slice(0, 3);

  if (imagePlaceholders.length > 0) {
    var useGeneration = _lastUserAskedGenerate;

    var fetchPromises = imagePlaceholders.map(function(ph) {
      if (useGeneration) {
        // Use the user's simple subject for DALL-E (avoids content policy triggers from verbose AI descriptions)
        var dallePrompt = _lastUserImageSubject || ph.query;
        return _generateImage(dallePrompt).then(function(url) {
          if (url) return { placeholder: ph, url: url, generated: true };
          // Fall back to search if generation fails
          return _searchImage(ph.query).then(function(url2) {
            return { placeholder: ph, url: url2, generated: false };
          });
        }).catch(function() {
          return { placeholder: ph, url: null, generated: false };
        });
      } else {
        return _searchImage(ph.query).then(function(url) {
          return { placeholder: ph, url: url, generated: false };
        }).catch(function() {
          return { placeholder: ph, url: null, generated: false };
        });
      }
    });

    var results = await Promise.all(fetchPromises);

    results.forEach(function(r) {
      if (r.url) {
        // Replace placeholder with image tag
        var genLabel = r.generated ? ' data-generated="true"' : '';
        var imgTag = '<img src="' + escapeHtml(r.url) + '" title="' + escapeHtml(r.placeholder.query) + '" alt="' + escapeHtml(r.placeholder.query) + '" class="eva-inline-img"' + genLabel + '>';
        if (r.generated) {
          imgTag = '<div class="eva-generated-wrap">' + imgTag + '<span class="eva-generated-badge">AI Generated</span></div>';
        }
        text = text.replace(r.placeholder.full, imgTag);
        surfacedAssets.push({ url: r.url, caption: r.placeholder.query, generated: r.generated });
      } else {
        // Replace with a styled placeholder showing what was requested
        text = text.replace(r.placeholder.full, '[🖼️ ' + r.placeholder.query + ']');
      }
    });

    // If we successfully generated/found images, strip common AI disclaimers
    var anySuccess = results.some(function(r) { return r.url; });
    if (anySuccess && _lastUserAskedGenerate) {
      // Remove lines where the AI says it can't generate/create images
      text = text.replace(/I\s+(cannot|can't|can not|am unable to|don't have the ability to)\s+(generate|create|produce|make|draw|render)\s+(images?|pictures?|photos?|illustrations?|artwork)[^.]*\./gi, '');
      text = text.replace(/I\s+(can only|only)\s+describe[^.]*\./gi, '');
      text = text.replace(/\n{3,}/g, '\n\n'); // clean up extra blank lines
    }

    // Tokenize generated image wrappers and standalone <img> tags before markdown.
    var imgFragments = [];
    text = text.replace(/<div class="eva-generated-wrap">[\s\S]*?<\/div>/g, function(m) {
      imgFragments.push(m);
      return '\u0000IMG' + (imgFragments.length - 1) + '\u0000';
    });
    text = text.replace(/<img[^>]*>/g, function(m) {
      imgFragments.push(m);
      return '\u0000IMG' + (imgFragments.length - 1) + '\u0000';
    });

    // Render markdown
    var html = (typeof renderMarkdown === 'function') ? renderMarkdown(text) : text;

    // Restore <img> tags
    html = html.replace(/\u0000IMG(\d+)\u0000/g, function(m, idx) {
      return imgFragments[Number(idx)] || m;
    });

    txtOutput.innerHTML += '<div class="chat-bubble eva-bubble"><span class="eva">Eva:</span> <div class="md">' + html + '</div></div>';
  } else {
    // No images or no search keys — just render markdown
    var html2 = (typeof renderMarkdown === 'function') ? renderMarkdown(text) : text;
    txtOutput.innerHTML += '<div class="chat-bubble eva-bubble"><span class="eva">Eva:</span> <div class="md">' + html2 + '</div></div>';
  }

  var feedbackBubbles = txtOutput.querySelectorAll('.chat-bubble.eva-bubble');
  var feedbackBubble = feedbackBubbles.length ? feedbackBubbles[feedbackBubbles.length - 1] : null;
  if (feedbackBubble && reasoningText) {
    var reasoningDetails = document.createElement('details');
    reasoningDetails.className = 'eva-reasoning';
    var reasoningSummary = document.createElement('summary');
    reasoningSummary.textContent = 'Thinking';
    var reasoningBody = document.createElement('div');
    reasoningBody.className = 'eva-reasoning-content';
    reasoningBody.innerHTML = (typeof renderMarkdown === 'function') ? renderMarkdown(reasoningText) : escapeHtml(reasoningText);
    reasoningDetails.appendChild(reasoningSummary);
    reasoningDetails.appendChild(reasoningBody);
    var responseBody = feedbackBubble.querySelector('.md');
    if (responseBody) responseBody.insertBefore(reasoningDetails, responseBody.firstChild);
  }
  if (feedbackBubble && typeof EvaLearning !== 'undefined' && EvaLearning) EvaLearning.attachFeedback(feedbackBubble, feedbackKey);

  appendArtifactLinks();

  // Auto-open artifact files so the user doesn't have to click.
  if (artifactNames.length) {
    var bridgeUrl = getSafeBridgeBaseUrl();
    artifactNames.forEach(function(filename) {
      var openUrl = bridgeUrl + '/v1/files/' + encodeURIComponent(filename) + '?open=1';
      fetch(openUrl).then(function(res) {
        if (res.ok) console.log('[Artifact] Auto-opened:', filename);
      }).catch(function() {});
    });
  }

  txtOutput.scrollTop = txtOutput.scrollHeight;

  // In voice/visual mode the chat is hidden behind the orb overlay, so surface
  // any images Eva resolved into the voice view's asset window.
  if (typeof _vv !== 'undefined' && _vv.open && surfacedAssets.length) {
    _vvSurfaceAssets(surfacedAssets);
  }

  // Launch the visual browser agent if Eva requested it.
  if (browserLaunch && typeof EvaBrowser !== 'undefined' && EvaBrowser && typeof EvaBrowser.launch === 'function') {
    EvaBrowser.launch(browserLaunch.goal, {
      start_url: browserLaunch.start_url,
      vision_model: browserLaunch.vision_model,
      max_steps: browserLaunch.max_steps,
      onComplete: _evaAgentFeedback,
      onConfirm: _evaAgentConfirmAsk,
      onProgress: _evaAgentProgress
    });
  }

  // Launch the desktop ("computer use") agent if Eva requested it.
  if (desktopLaunch && typeof EvaDesktop !== 'undefined' && EvaDesktop && typeof EvaDesktop.launch === 'function') {
    EvaDesktop.launch(desktopLaunch.goal, {
      vision_model: desktopLaunch.vision_model,
      max_steps: desktopLaunch.max_steps,
      onComplete: _evaAgentFeedback,
      onConfirm: _evaAgentConfirmAsk,
      onProgress: _evaAgentProgress
    });
  }

  // Look through the webcam if Eva requested it (Eva's eyes).
  if (cameraLook && typeof EvaCamera !== 'undefined' && EvaCamera && typeof EvaCamera.look === 'function') {
    EvaCamera.look(cameraLook.question).then(function (desc) {
      _evaCameraLookResult(desc || 'I could not make out anything.');
    }).catch(function (err) {
      _evaCameraLookResult('I tried to look but ' + ((err && err.message) ? err.message : 'something went wrong') + '.');
    });
  }

  // Auto-save session after each response
  evaAuditEvent('turn.rendered', 'completed', {
    correlation_id: renderOptions.turnId || window._evaActiveAuditTurnId || '',
    response_chars: content.length
  });
  if (typeof saveCurrentSession === 'function') saveCurrentSession();
}

/**
 * Extract the key subject from a verbose image description.
 * "GitHub's Octocat mascot - a friendly cartoon cat..." → "GitHub Octocat mascot"
 */
function _extractImageSubject(rawDesc) {
  if (!rawDesc) return '';
  var desc = rawDesc;

  // Cut at first " - " or " — " or ", " comma phrase
  var dashIdx = desc.search(/\s[-–—]\s/);
  if (dashIdx > 3) desc = desc.substring(0, dashIdx);

  // Cut at first comma if still long (keep just the subject noun phrase)
  if (desc.length > 40) {
    var commaIdx = desc.indexOf(',');
    if (commaIdx > 3) desc = desc.substring(0, commaIdx);
  }

  // Cut at first period
  if (desc.length > 40) {
    var dotIdx = desc.indexOf('.');
    if (dotIdx > 3) desc = desc.substring(0, dotIdx);
  }

  // 1. Find proper nouns (capitalized words like "Octocat", "GitHub")
  var properNouns = desc.match(/\b[A-Z][a-zA-Z]+\b/g) || [];
  var ignoreCapitalized = new Set(['Image', 'Picture', 'Photo', 'The', 'An', 'This', 'Here', 'Its', 'Each', 'Very', 'Some', 'With', 'And']);
  properNouns = properNouns.filter(function(w) { return !ignoreCapitalized.has(w); });

  if (properNouns.length > 0) {
    return properNouns.slice(0, 4).join(' ');
  }

  // 2. Strip filler — keep nouns (which come early in the description)
  desc = desc
    .replace(/^(an?|the|image of|picture of|photo of|showing|depicting|illustration of)\s+/gi, '')
    .replace(/\b(friendly|cartoon|cartoonish|cute|classic|iconic|simple|round|large|small|playful|beloved|stylized|detailed|colorful|whimsical|famous|popular|vibrant|modern|typical|standard|featuring|with|that|has|and|or|its|soft|warm|bright|relaxed|graceful|sunny|patterned)\b\s*/gi, '')
    .replace(/[''\u2019]s\b/g, '')
    .replace(/[,;]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();

  // Take FIRST 2-3 meaningful words (the subject noun is at the beginning)
  var words = desc.split(/\s+/).filter(function(w) { return w.length > 2; });
  if (words.length > 3) {
    desc = words.slice(0, 3).join(' ');
  }

  return desc || rawDesc.substring(0, 30);
}

/**
 * Search for an image using Wikimedia Commons (free, no API key needed).
 */
async function _searchImage(query) {
  if (!query) return null;

  var cleanQuery = query.trim();
  if (!cleanQuery) return null;

  // Try progressively simpler queries
  var queries = [cleanQuery];
  var words = cleanQuery.split(/\s+/);
  if (words.length > 2) queries.push(words.slice(0, 2).join(' '));
  if (words.length > 1) queries.push(words[words.length - 1]); // try just the last word (often the noun)

  for (var qi = 0; qi < queries.length; qi++) {
    var q = queries[qi];
    try {
      var wUrl = 'https://commons.wikimedia.org/w/api.php?' +
        'action=query&list=search&srnamespace=6' +
        '&srsearch=' + encodeURIComponent(q) +
        '&srlimit=5&format=json&origin=*';

      var wResp = await fetch(wUrl);
      if (wResp.ok) {
        var wData = await wResp.json();
        var results = (wData.query && wData.query.search) || [];
        if (results.length > 0) {
          // Get the actual image URL from the file title
          var fileTitle = results[0].title;
          var imgUrl = 'https://commons.wikimedia.org/w/api.php?' +
            'action=query&titles=' + encodeURIComponent(fileTitle) +
            '&prop=imageinfo&iiprop=url&iiurlwidth=400&format=json&origin=*';

          var imgResp = await fetch(imgUrl);
          if (imgResp.ok) {
            var imgData = await imgResp.json();
            var pages = imgData.query && imgData.query.pages;
            if (pages) {
              var pageId = Object.keys(pages)[0];
              var info = pages[pageId].imageinfo;
              if (info && info[0]) {
                return info[0].thumburl || info[0].url;
              }
            }
          }
        } else if (qi < queries.length - 1) {
          // Try simpler query
        }
      }
    } catch (e) {
      console.warn('Wikimedia search error:', e.message);
    }
  }

  return null;
}

// --- Image Lightbox ---
document.addEventListener('DOMContentLoaded', function() {
  var lightbox = document.getElementById('evaLightbox');
  var lightboxImg = document.getElementById('evaLightboxImg');
  var lightboxClose = lightbox ? lightbox.querySelector('.eva-lightbox-close') : null;

  // Click on any inline image to expand
  document.addEventListener('click', function(e) {
    var img = e.target.closest('.eva-inline-img');
    if (img && lightbox && lightboxImg) {
      lightboxImg.src = img.src;
      lightboxImg.alt = img.alt || 'Expanded image';
      lightbox.classList.add('open');
      e.preventDefault();
    }
  });

  // Close lightbox
  if (lightbox) {
    lightbox.addEventListener('click', function(e) {
      if (e.target === lightbox || e.target === lightboxClose) {
        lightbox.classList.remove('open');
      }
    });
  }

  // Escape key closes lightbox
  document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape' && lightbox && lightbox.classList.contains('open')) {
      lightbox.classList.remove('open');
    }
  });
});

// Capture Shift + Enter Keys for new line
function shiftBreak() {
document.querySelector("#txtMsg").addEventListener("keydown", function(event) {
  if (event.shiftKey && event.keyCode === 13) {
    // Use the browser's native line-break command so contenteditable
    // gets the proper trailing-br anchor. The previous manual <br>
    // insert required two presses to visually break the line because
    // a single trailing <br> at end-of-text is not rendered.
    event.preventDefault();
    try {
      if (document.execCommand && document.execCommand('insertLineBreak')) {
        return;
      }
    } catch (_) {}
    var sel = window.getSelection();
    if (!sel || !sel.rangeCount) return;
    var range = sel.getRangeAt(0);
    range.deleteContents();
    var br = document.createElement("br");
    range.insertNode(br);
    // Anchor br: ensures the cursor falls on a visibly new line.
    var anchor = document.createElement("br");
    range.setStartAfter(br);
    range.insertNode(anchor);
    range.collapse(true);
    sel.removeAllRanges();
    sel.addRange(range);
  }
});

    // Capture Enter Key to Send Message and Backspace to reset position
    document.querySelector("#txtMsg").addEventListener("keydown", function(event) {
      if (event.keyCode === 13 && !event.shiftKey) {
        document.querySelector("#btnSend").click();
        event.preventDefault();
        var backspace = new KeyboardEvent("keydown", {
          bubbles: true,
          cancelable: true,
          keyCode: 8
        });
        document.querySelector("#txtMsg").dispatchEvent(backspace);
      }
    });
}

// Clear Messages for Clear Memory Button
function clearMessages() {
  if (typeof clearLastDeliveredSignal === 'function') clearLastDeliveredSignal();
    // Preserve auth keys, settings, and session data across clear
    var keysToKeep = [];
    for (var i = 0; i < localStorage.length; i++) {
      var key = localStorage.key(i);
      if (key && (key.indexOf('auth_') === 0 || key === 'theme' || key === 'systemPrompt'
          || key === 'lcars_collapsed' || key === 'acp_bridge_url'
          || key === 'aig_lmstudio_base_url' || key === 'aig_lmstudio_model'
          || key === 'eva_sessions' || key.indexOf('session_') === 0)) {
        keysToKeep.push({ k: key, v: localStorage.getItem(key) });
      }
    }
    localStorage.clear();
    keysToKeep.forEach(function(item) { localStorage.setItem(item.k, item.v); });
    // Start a fresh session (don't carry old active id)
    localStorage.removeItem('eva_active_session');
    document.getElementById("txtOutput").innerHTML = "\n" + "		MEMORY CLEARED";
}

// Restore the Eva welcome MOTD into #txtOutput after clearing
function restoreEvaWelcome() {
  var out = document.getElementById('txtOutput');
  if (!out) return;
  var theme = (localStorage.getItem('theme') || 'eva');
  if (theme !== 'eva' && theme.indexOf('eva-') !== 0) return;
  out.innerHTML = '<div id="evaWelcome" class="eva-welcome">'
    + '<img src="core/img/thumb-125.jpeg" alt="Eva" class="eva-welcome-avatar">'
    + '<h2 class="eva-welcome-title">Hello! I\'m <span class="eva-highlight">Eva</span></h2>'
    + '<p class="eva-welcome-subtitle">Your AI assistant. Ask me anything or choose a suggestion to get started.</p>'
    + '<div class="eva-suggestions">'
    + '<button class="eva-suggestion" onclick="evaSuggestionClick(this)" data-prompt="Explain a complex topic in simple terms"><span class="eva-sug-icon">&#x1F9E0;</span><div><strong>Explain a complex topic</strong><br><span class="eva-sug-sub">in simple terms</span></div></button>'
    + '<button class="eva-suggestion" onclick="evaSuggestionClick(this)" data-prompt="Help me write code in any language"><span class="eva-sug-icon">&lt;/&gt;</span><div><strong>Help me write code</strong><br><span class="eva-sug-sub">in any language</span></div></button>'
    + '<button class="eva-suggestion" onclick="evaSuggestionClick(this)" data-prompt="Brainstorm ideas for a project"><span class="eva-sug-icon">&#x1F4A1;</span><div><strong>Brainstorm ideas</strong><br><span class="eva-sug-sub">for a project</span></div></button>'
    + '<button class="eva-suggestion" onclick="evaSuggestionClick(this)" data-prompt="Review my text and improve it"><span class="eva-sug-icon">&#x270F;&#xFE0F;</span><div><strong>Review my text</strong><br><span class="eva-sug-sub">and improve it</span></div></button>'
    + '</div></div>';
}

// Text-to-Speech (wake-word recognition lives in features/voice/wake-listener.js)
