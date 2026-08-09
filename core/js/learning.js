// Structured learning controls. Only bounded metadata leaves the browser.
(function (global) {
  'use strict';

  var localFeedback = 'eva_feedback_index';
  var allowedVoiceEvents = { buffered: 1, merged: 1, duplicate: 1, committed: 1, interrupted: 1, error: 1, denied: 1, unsupported: 1 };

  function bridge() {
    return (typeof getSafeBridgeBaseUrl === 'function' ? getSafeBridgeBaseUrl() : 'http://localhost:8888').replace(/\/+$/, '');
  }

  function headers() {
    if (typeof getBridgeCapabilityHeaders === 'function') return getBridgeCapabilityHeaders();
    return { 'Content-Type': 'application/json', Authorization: 'Bearer ' + ((global.evaStandalone || {}).bridgeToken || '') };
  }

  function sessionId() {
    try {
      var active = localStorage.getItem('eva_active_session');
      if (active) return active.slice(0, 120);
      if (typeof ensureActiveSessionId === 'function') return ensureActiveSessionId().slice(0, 120);
      return 'browser-session';
    } catch (error) {
      return 'browser-session';
    }
  }

  function fetchJson(path, options) {
    return fetch(bridge() + path, options).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (body) {
        if (!response.ok) throw new Error((body.error && body.error.message) || ('HTTP ' + response.status));
        return body;
      });
    });
  }

  function feedbackIndex() {
    try { return JSON.parse(localStorage.getItem(localFeedback) || '{}') || {}; } catch (error) { return {}; }
  }

  function saveFeedbackIndex(index) {
    try { localStorage.setItem(localFeedback, JSON.stringify(index)); } catch (error) {}
  }

  function sendSignal(body) {
    return fetchJson('/v1/learning/signals', { method: 'POST', headers: headers(), body: JSON.stringify(body) });
  }

  function removeSignal(id) {
    if (!id) return Promise.resolve();
    var query = '?scope=session&session_id=' + encodeURIComponent(sessionId());
    return fetchJson('/v1/learning/signals/' + encodeURIComponent(id) + query, { method: 'DELETE', headers: headers() });
  }

  function feedbackStatus(button) {
    return button.getAttribute('data-feedback') || '';
  }

  function setFeedbackState(wrapper, status) {
    Array.prototype.forEach.call(wrapper.querySelectorAll('button[data-feedback]'), function (button) {
      var selected = feedbackStatus(button) === status;
      button.classList.toggle('selected', selected);
      button.setAttribute('aria-pressed', selected ? 'true' : 'false');
    });
    wrapper.setAttribute('data-selected', status || '');
  }

  function attachFeedback(bubble, responseKey) {
    if (!bubble || bubble.querySelector('.eva-feedback')) return;
    var wrapper = document.createElement('div');
    wrapper.className = 'eva-feedback';
    wrapper.setAttribute('role', 'group');
    wrapper.setAttribute('aria-label', 'Response feedback');
    var labels = [
      { status: 'helpful', symbol: '+', label: 'Helpful' },
      { status: 'unhelpful', symbol: '-', label: 'Unhelpful' },
      { status: 'misunderstood', symbol: '?', label: 'Misunderstood' }
    ];
    labels.forEach(function (item) {
      var button = document.createElement('button');
      button.type = 'button';
      button.className = 'eva-feedback-button';
      button.setAttribute('data-feedback', item.status);
      button.setAttribute('aria-label', item.label);
      button.setAttribute('aria-pressed', 'false');
      button.title = item.label;
      button.textContent = item.symbol + ' ' + item.label;
      button.addEventListener('click', function () {
        var index = feedbackIndex();
        var prior = index[responseKey];
        var status = item.status;
        var operation = prior && prior.status === status ? removeSignal(prior.id) : removeSignal(prior && prior.id).then(function () {
          return sendSignal({
            source: 'explicit-user', kind: 'feedback', status: status, value: status,
            confidence: 1, scope: 'session', session_id: sessionId(),
            permission_basis: 'explicit-user', detail: { control: status }
          });
        });
        button.disabled = true;
        operation.then(function (result) {
          if (prior && prior.status === status) {
            delete index[responseKey];
            saveFeedbackIndex(index);
            setFeedbackState(wrapper, '');
          } else {
            var signal = result && result.signal;
            index[responseKey] = { id: signal && signal.id, status: status };
            saveFeedbackIndex(index);
            setFeedbackState(wrapper, status);
          }
        }).catch(function () {
          // A denied or unavailable signal must not change the visible choice.
        }).finally(function () { button.disabled = false; });
      });
      wrapper.appendChild(button);
    });
    bubble.appendChild(wrapper);
    var saved = feedbackIndex()[responseKey];
    if (saved) setFeedbackState(wrapper, saved.status);
  }

  function recordActionOutcome(status, endpoint, title) {
    if (!status || !allowedActionStatus(status.status)) return Promise.resolve(null);
    var value = mapActionStatus(status);
    var confidence = value === 'done' ? 0.95 : 0.8;
    return sendSignal({
      source: 'action-result', kind: 'action-outcome', status: value, value: value,
      confidence: confidence, scope: 'session', session_id: sessionId(),
      permission_basis: value === 'declined' ? 'explicit-user' : 'routine-outcome',
      detail: { agent: String(title || endpoint || 'action').replace(/ agent$/i, '').slice(0, 40), operation: 'run' }
    }).catch(function () { return null; });
  }

  function allowedActionStatus(status) {
    return ['done', 'error', 'cancelled'].indexOf(status) >= 0;
  }

  function mapActionStatus(status) {
    if (!status) return '';
    return status.status === 'done' && /^Stopped: user declined/i.test(String(status.result || '')) ? 'declined' : status.status;
  }

  function minimizeVoiceEvent(event) {
    event = event || {};
    var type = allowedVoiceEvents[event.type] ? event.type : 'error';
    var detail = { event: type };
    ['provider', 'reason'].forEach(function (key) {
      if (typeof event[key] === 'string' && event[key]) detail[key] = event[key].slice(0, 40);
    });
    ['chars', 'fragments'].forEach(function (key) {
      if (typeof event[key] === 'number' && isFinite(event[key])) detail[key] = Math.max(0, Math.min(100000, Math.round(event[key])));
    });
    return { type: type, detail: detail };
  }

  function recordVoiceDiagnostic(event) {
    var minimized = minimizeVoiceEvent(event);
    var type = minimized.type;
    var detail = minimized.detail;
    return sendSignal({
      source: 'voice-inferred', kind: 'voice-diagnostic', status: 'diagnostic', value: type,
      confidence: 0.4, scope: 'session', session_id: sessionId(),
      permission_basis: 'standing-consent', detail: detail
    }).catch(function () { return null; });
  }

  function loadConsent() {
    return fetchJson('/v1/learning/consent', { headers: headers() });
  }

  function renderConsent(profile) {
    ['explicit_feedback', 'action_outcomes', 'voice_diagnostics', 'routine_tools'].forEach(function (category) {
      var input = document.getElementById('learning_' + category);
      if (input) input.checked = !!profile[category];
    });
    var retention = document.getElementById('learningRetention');
    if (retention) retention.value = profile.retention_days || 30;
  }

  function loadRecentSignals() {
    var query = '?scope=session&session_id=' + encodeURIComponent(sessionId()) + '&limit=12';
    return fetchJson('/v1/learning/signals' + query, { headers: headers() }).then(function (data) {
      var output = document.getElementById('learningRecentSignals');
      if (!output) return;
      output.replaceChildren();
      (data.signals || []).forEach(function (signal) {
        var row = document.createElement('div');
        row.className = 'learning-signal-row';
        row.textContent = signal.kind + ' / ' + signal.status + ' / ' + signal.permission_basis + ' / ' + (signal.applied && signal.applied.status || 'pending');
        output.appendChild(row);
      });
      if (!output.children.length) output.textContent = 'No retained signal metadata.';
    });
  }

  function initSettings() {
    var panel = document.querySelector('[data-stab="learning"]');
    if (!panel) return;
    var refresh = function () { loadConsent().then(renderConsent).catch(function () {}); loadRecentSignals().catch(function () {}); };
    ['explicit_feedback', 'action_outcomes', 'voice_diagnostics', 'routine_tools'].forEach(function (category) {
      var input = document.getElementById('learning_' + category);
      if (input) input.addEventListener('change', function () {
        var changes = {}; changes[category] = input.checked;
        fetchJson('/v1/learning/consent', { method: 'POST', headers: headers(), body: JSON.stringify(changes) }).then(renderConsent).catch(function () { input.checked = !input.checked; });
      });
    });
    var retention = document.getElementById('learningRetention');
    if (retention) retention.addEventListener('change', function () { fetchJson('/v1/learning/consent', { method: 'POST', headers: headers(), body: JSON.stringify({ retention_days: Number(retention.value) }) }).catch(function () { refresh(); }); });
    var revoke = document.getElementById('learningRevoke');
    if (revoke) revoke.addEventListener('click', function () { fetchJson('/v1/learning/consent', { method: 'DELETE', headers: headers() }).then(renderConsent).catch(function () {}); });
    var erase = document.getElementById('learningDelete');
    if (erase) erase.addEventListener('click', function () {
      var query = '?scope=session&session_id=' + encodeURIComponent(sessionId());
      fetchJson('/v1/learning/signals' + query, { method: 'DELETE', headers: headers() }).then(refresh).catch(function () {});
    });
    var refreshButton = document.getElementById('learningRefresh');
    if (refreshButton) refreshButton.addEventListener('click', refresh);
    refresh();
  }

  global.EvaLearning = {
    attachFeedback: attachFeedback,
    recordActionOutcome: recordActionOutcome,
    recordVoiceDiagnostic: recordVoiceDiagnostic,
    initSettings: initSettings,
    _test: { mapActionStatus: mapActionStatus, minimizeVoiceEvent: minimizeVoiceEvent }
  };
  document.addEventListener('DOMContentLoaded', initSettings);
})(typeof window !== 'undefined' ? window : globalThis);