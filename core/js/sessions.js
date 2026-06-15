// sessions.js — Session persistence and explorer panel
// Uses IndexedDB (via idb-store.js) for session snapshots.
// Active conversation state stays in localStorage for backward compat with provider JS files.

var SESSION_INDEX_KEY = 'eva_sessions';
var SESSION_ACTIVE_KEY = 'eva_active_session';

// All provider message keys
var SESSION_MSG_KEYS = ['messages', 'copilotMessages', 'copilotACPMessages', 'geminiMessages', 'openLLMessages', 'aigMessages'];

function _getSessionIndex() {
  try { return JSON.parse(localStorage.getItem(SESSION_INDEX_KEY)) || []; }
  catch(e) { return []; }
}

function _saveSessionIndex(index) {
  localStorage.setItem(SESSION_INDEX_KEY, JSON.stringify(index));
}

function _activeSessionId() {
  return localStorage.getItem(SESSION_ACTIVE_KEY) || null;
}

/** Snapshot current conversation state into a session object */
function _snapshotSession() {
  var data = {};
  SESSION_MSG_KEYS.forEach(function(key) {
    var raw = localStorage.getItem(key);
    if (raw) data[key] = raw;
  });
  data._masterOutput = localStorage.getItem('masterOutput') || '';
  data._model = (document.getElementById('selModel') || {}).value || '';
  data._htmlSnapshot = (document.getElementById('txtOutput') || {}).innerHTML || '';
  return data;
}

/** Restore a session snapshot into localStorage and DOM */
function _restoreSession(data) {
  // Clear existing messages
  SESSION_MSG_KEYS.forEach(function(key) { localStorage.removeItem(key); });
  localStorage.removeItem('masterOutput');

  // Write stored keys back
  Object.keys(data).forEach(function(key) {
    if (key.charAt(0) === '_') return; // skip meta keys
    localStorage.setItem(key, data[key]);
  });
  if (data._masterOutput) {
    localStorage.setItem('masterOutput', data._masterOutput);
    if (typeof masterOutput !== 'undefined') masterOutput = data._masterOutput;
  }

  // Restore DOM
  var txtOutput = document.getElementById('txtOutput');
  if (txtOutput && data._htmlSnapshot) {
    txtOutput.innerHTML = data._htmlSnapshot;
    txtOutput.scrollTop = txtOutput.scrollHeight;
  }

  // Restore model selection
  if (data._model) {
    var sel = document.getElementById('selModel');
    if (sel) {
      sel.value = data._model;
      if (typeof updateButton === 'function') updateButton();
    }
  }
}

/** Derive a display name from the first user message */
function _sessionTitle(data) {
  for (var i = 0; i < SESSION_MSG_KEYS.length; i++) {
    var raw = data[SESSION_MSG_KEYS[i]];
    if (!raw) continue;
    try {
      var msgs = JSON.parse(raw);
      for (var j = 0; j < msgs.length; j++) {
        if (msgs[j].role === 'user') {
          var txt = typeof msgs[j].content === 'string' ? msgs[j].content : '';
          if (!txt && Array.isArray(msgs[j].content)) {
            msgs[j].content.forEach(function(p) { if (p.text) txt += p.text; });
          }
          var _prev; do { _prev = txt; txt = txt.replace(/<[^>]*>/g, ''); } while (txt !== _prev);
          txt = txt.replace(/[<>]/g, '').trim();
          if (txt) return txt.length > 50 ? txt.substring(0, 47) + '...' : txt;
        }
      }
    } catch(e) {}
  }
  return 'Untitled';
}

/** Count user messages in a snapshot */
function _sessionMsgCount(data) {
  var count = 0;
  SESSION_MSG_KEYS.forEach(function(key) {
    try {
      var msgs = JSON.parse(data[key] || '[]');
      msgs.forEach(function(m) { if (m.role === 'user') count++; });
    } catch(e) {}
  });
  return count;
}

/** Auto-save the current session (call on every send and periodically) */
function saveCurrentSession() {
  var snapshot = _snapshotSession();
  // Only save if there's actual content
  if (_sessionMsgCount(snapshot) === 0) return;

  var id = _activeSessionId();
  var index = _getSessionIndex();

  if (!id) {
    // First save — create a new session
    id = 'sess_' + Date.now() + '_' + Math.random().toString(36).substring(2, 6);
    localStorage.setItem(SESSION_ACTIVE_KEY, id);
    index.unshift({ id: id, title: _sessionTitle(snapshot), created: Date.now(), updated: Date.now() });
  } else {
    // Update existing
    for (var i = 0; i < index.length; i++) {
      if (index[i].id === id) {
        index[i].title = _sessionTitle(snapshot);
        index[i].updated = Date.now();
        break;
      }
    }
  }

  // Save to IndexedDB (async, non-blocking)
  idbSaveSession(id, snapshot).catch(function(e) {
    console.error('[Sessions] IDB save failed:', e);
  });
  _saveSessionIndex(index);
  renderSessionList();
}

/** Start a brand new session */
function newSession() {
  // Auto-save current first
  saveCurrentSession();

  // Clear active
  localStorage.removeItem(SESSION_ACTIVE_KEY);
  SESSION_MSG_KEYS.forEach(function(key) { localStorage.removeItem(key); });
  localStorage.removeItem('masterOutput');
  if (typeof masterOutput !== 'undefined') masterOutput = '';
  if (typeof lastResponse !== 'undefined') lastResponse = '';

  var txtOutput = document.getElementById('txtOutput');
  if (txtOutput) {
    if (typeof showWelcome === 'function') showWelcome();
    else txtOutput.innerHTML = '';
  }

  renderSessionList();
}

/** Load a session by id */
function loadSession(id) {
  // Save current first
  saveCurrentSession();

  idbLoadSession(id).then(function(data) {
    if (!data) return;
    _restoreSession(data);
    localStorage.setItem(SESSION_ACTIVE_KEY, id);
    renderSessionList();
  }).catch(function(e) {
    console.error('Failed to load session:', e);
  });
}

/** Delete a session */
function deleteSession(id) {
  var index = _getSessionIndex();
  index = index.filter(function(s) { return s.id !== id; });
  _saveSessionIndex(index);
  idbDeleteSession(id).catch(function(e) {
    console.error('[Sessions] IDB delete failed:', e);
  });

  // If deleting the active session, start fresh
  if (_activeSessionId() === id) {
    localStorage.removeItem(SESSION_ACTIVE_KEY);
  }
  renderSessionList();
}

/** Render the session list in the panel */
function renderSessionList() {
  var ul = document.getElementById('sessionList');
  if (!ul) return;

  var index = _getSessionIndex();
  var activeId = _activeSessionId();

  ul.innerHTML = '';

  if (index.length === 0) {
    ul.innerHTML = '<li class="session-empty">No saved sessions</li>';
    return;
  }

  // Sort: pinned first (by updated desc), then unpinned (by updated desc)
  var sorted = index.slice().sort(function(a, b) {
    if (a.pinned && !b.pinned) return -1;
    if (!a.pinned && b.pinned) return 1;
    return (b.updated || b.created || 0) - (a.updated || a.created || 0);
  });

  sorted.forEach(function(entry) {
    var li = document.createElement('li');
    li.className = 'session-item' + (entry.id === activeId ? ' active' : '') + (entry.pinned ? ' pinned' : '');

    var titleSpan = document.createElement('span');
    titleSpan.className = 'session-title';
    titleSpan.textContent = (entry.pinned ? '\u{1F4CC} ' : '') + (entry.title || 'Untitled');
    titleSpan.title = entry.title || 'Untitled';

    var timeSpan = document.createElement('span');
    timeSpan.className = 'session-time';
    var d = new Date(entry.updated || entry.created);
    timeSpan.textContent = d.toLocaleDateString() + ' ' + d.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});

    var btnWrap = document.createElement('span');
    btnWrap.className = 'session-actions';

    var pinBtn = document.createElement('button');
    pinBtn.className = 'session-pin' + (entry.pinned ? ' active' : '');
    pinBtn.textContent = '\u{1F4CC}';
    pinBtn.title = entry.pinned ? 'Unpin session' : 'Pin session';
    pinBtn.onclick = function(e) {
      e.stopPropagation();
      togglePinSession(entry.id);
    };

    var delBtn = document.createElement('button');
    delBtn.className = 'session-delete';
    delBtn.textContent = '\u00d7';
    delBtn.title = 'Delete session';
    delBtn.onclick = function(e) {
      e.stopPropagation();
      deleteSession(entry.id);
    };

    btnWrap.appendChild(pinBtn);
    btnWrap.appendChild(delBtn);

    li.appendChild(titleSpan);
    li.appendChild(timeSpan);
    li.appendChild(btnWrap);
    li.onclick = function() { loadSession(entry.id); };

    ul.appendChild(li);
  });
}

/** Toggle the session panel visibility */
function toggleSessionPanel() {
  var panel = document.getElementById('sessionPanel');
  if (!panel) return;
  var visible = panel.getAttribute('aria-hidden') !== 'true';
  panel.setAttribute('aria-hidden', visible ? 'true' : 'false');
  if (!visible) renderSessionList();
}

/** Wire up session panel buttons + auto-save on page unload */
function initSessions() {
  // Button bindings
  var sessBtn = document.getElementById('sidebarSessionsBtn');
  if (sessBtn) sessBtn.addEventListener('click', toggleSessionPanel);

  var closeBtn = document.getElementById('sessionPanelClose');
  if (closeBtn) closeBtn.addEventListener('click', toggleSessionPanel);

  var newBtn = document.getElementById('sessionNewBtn');
  if (newBtn) newBtn.addEventListener('click', function() { newSession(); });

  var exportBtn = document.getElementById('sessionExportBtn');
  if (exportBtn) exportBtn.addEventListener('click', function() { exportCurrentSession(); });

  // Assets panel close button
  var assetsClose = document.getElementById('assetsPanelClose');
  if (assetsClose) assetsClose.addEventListener('click', toggleAssetsPanel);

  // Terminal panel close button
  var termClose = document.getElementById('terminalPanelClose');
  if (termClose) termClose.addEventListener('click', toggleTerminalPanel);

  // Migrate localStorage sessions to IndexedDB, then restore active session
  idbMigrateFromLocalStorage().then(function() {
    var activeId = _activeSessionId();
    if (activeId) {
      idbLoadSession(activeId).then(function(data) {
        if (data) _restoreSession(data);
      }).catch(function() {});
    }
  }).catch(function() {
    // Fallback: try restoring from active localStorage state
    var activeId = _activeSessionId();
    if (activeId) {
      var raw = localStorage.getItem('session_' + activeId);
      if (raw) {
        try { _restoreSession(JSON.parse(raw)); } catch(e) {}
      }
    }
  });

  // Auto-save on unload
  window.addEventListener('beforeunload', function() {
    saveCurrentSession();
  });

  // Periodic auto-save every 30s
  setInterval(saveCurrentSession, 30000);

  renderSessionList();
}

// ── Session Pinning ──────────────────────────────────────────

/** Toggle pin state for a session */
function togglePinSession(id) {
  var index = _getSessionIndex();
  for (var i = 0; i < index.length; i++) {
    if (index[i].id === id) {
      index[i].pinned = !index[i].pinned;
      break;
    }
  }
  _saveSessionIndex(index);
  renderSessionList();
}

// ── Session Export ───────────────────────────────────────────

/** Export the current active session as a markdown file */
function exportCurrentSession() {
  var id = _activeSessionId();
  if (!id) {
    alert('No active session to export.');
    return;
  }
  saveCurrentSession();
  idbLoadSession(id).then(function(data) {
    if (!data) { alert('Session data not found.'); return; }
    var index = _getSessionIndex();
    var meta = null;
    for (var i = 0; i < index.length; i++) {
      if (index[i].id === id) { meta = index[i]; break; }
    }
    var title = (meta && meta.title) || 'Untitled';
    var lines = ['# ' + title, ''];
    if (meta) {
      lines.push('**Date:** ' + new Date(meta.created).toLocaleString());
      if (meta.updated) lines.push('**Updated:** ' + new Date(meta.updated).toLocaleString());
      lines.push('');
    }
    SESSION_MSG_KEYS.forEach(function(key) {
      var raw = data[key];
      if (!raw) return;
      try {
        var msgs = JSON.parse(raw);
        msgs.forEach(function(m) {
          if (m.role === 'system' || m.role === 'developer') return;
          var content = typeof m.content === 'string' ? m.content : '';
          if (!content && Array.isArray(m.content)) {
            m.content.forEach(function(p) { if (p.text) content += p.text; });
          }
          if (!content) return;
          var label = m.role === 'user' ? '**You:**' : '**Eva:**';
          lines.push(label);
          lines.push(content);
          lines.push('');
        });
      } catch(e) {}
    });
    var blob = new Blob([lines.join('\n')], { type: 'text/markdown;charset=utf-8' });
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url;
    a.download = title.replace(/[^a-zA-Z0-9 _-]/g, '').substring(0, 50).trim() + '.md';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }).catch(function(e) {
    console.error('[Sessions] Export failed:', e);
    alert('Export failed: ' + e.message);
  });
}

// ── Assets Panel ─────────────────────────────────────────────

function toggleAssetsPanel() {
  var panel = document.getElementById('assetsPanel');
  if (!panel) return;
  var visible = panel.getAttribute('aria-hidden') !== 'true';
  panel.setAttribute('aria-hidden', visible ? 'true' : 'false');
  if (!visible) loadAssetsList();
}

function _formatFileSize(bytes) {
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}

function _assetIcon(name) {
  var ext = (name.split('.').pop() || '').toLowerCase();
  var icons = { pdf: '\u{1F4C4}', md: '\u{1F4DD}', csv: '\u{1F4CA}', json: '\u{1F4CB}', txt: '\u{1F4C3}' };
  return icons[ext] || '\u{1F4C4}';
}

function loadAssetsList() {
  var ul = document.getElementById('assetsList');
  if (!ul) return;
  ul.innerHTML = '<li class="session-empty">Loading...</li>';
  var base = (typeof getACPBridgeUrl === 'function') ? getACPBridgeUrl() : 'http://localhost:8888';
  fetch(base + '/v1/files').then(function(r) { return r.json(); }).then(function(data) {
    ul.innerHTML = '';
    if (!data.files || data.files.length === 0) {
      ul.innerHTML = '<li class="session-empty">No assets yet</li>';
      return;
    }
    data.files.forEach(function(f) {
      var li = document.createElement('li');
      li.className = 'session-item asset-item';

      var titleSpan = document.createElement('span');
      titleSpan.className = 'session-title';
      titleSpan.textContent = _assetIcon(f.name) + ' ' + f.name;
      titleSpan.title = f.name;

      var infoSpan = document.createElement('span');
      infoSpan.className = 'session-time';
      var d = new Date(f.modified * 1000);
      infoSpan.textContent = _formatFileSize(f.size) + ' \u00b7 ' + d.toLocaleDateString() + ' ' + d.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});

      var btnWrap = document.createElement('span');
      btnWrap.className = 'session-actions';

      var openBtn = document.createElement('button');
      openBtn.className = 'session-pin';
      openBtn.textContent = '\u{1F4C2}';
      openBtn.title = 'Open with system viewer';
      openBtn.onclick = function(e) {
        e.stopPropagation();
        fetch(base + '/v1/files/' + encodeURIComponent(f.name) + '?open=1');
      };

      var dlBtn = document.createElement('button');
      dlBtn.className = 'session-pin';
      dlBtn.textContent = '\u2913';
      dlBtn.title = 'Download';
      dlBtn.onclick = function(e) {
        e.stopPropagation();
        var a = document.createElement('a');
        a.href = base + '/v1/files/' + encodeURIComponent(f.name);
        a.download = f.name;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
      };

      btnWrap.appendChild(openBtn);
      btnWrap.appendChild(dlBtn);

      li.appendChild(titleSpan);
      li.appendChild(infoSpan);
      li.appendChild(btnWrap);
      li.onclick = function() {
        fetch(base + '/v1/files/' + encodeURIComponent(f.name) + '?open=1');
      };

      ul.appendChild(li);
    });
  }).catch(function(e) {
    ul.innerHTML = '<li class="session-empty">Could not load assets: ' + (e.message || e) + '</li>';
  });
}

function purgeAssets() {
  if (!confirm('Delete all generated assets?')) return;
  var base = (typeof getACPBridgeUrl === 'function') ? getACPBridgeUrl() : 'http://localhost:8888';
  fetch(base + '/v1/files/purge', { method: 'POST' }).then(function() {
    loadAssetsList();
  }).catch(function(e) {
    alert('Purge failed: ' + (e.message || e));
  });
}

// ── Terminal Panel ───────────────────────────────────────────

function toggleTerminalPanel() {
  var panel = document.getElementById('terminalPanel');
  if (!panel) return;
  var visible = panel.getAttribute('aria-hidden') !== 'true';
  panel.setAttribute('aria-hidden', visible ? 'true' : 'false');
  if (!visible) initTerminal();
}

function initTerminal() {
  var container = document.getElementById('terminalContainer');
  var fallback = document.getElementById('terminalFallback');
  var frame = document.getElementById('terminalFrame');
  if (!container) return;

  var isElectron = window.evaStandalone && window.evaStandalone.isStandalone;

  // detectACPBridge is async, so resolve it first
  var baseUrl = (typeof getACPBridgeUrl === 'function') ? getACPBridgeUrl() : 'http://localhost:8888';

  if (frame && fallback) {
    fallback.style.display = 'none';
    if (!frame._evaTermInit) {
      frame._evaTermInit = true;
      _buildSimpleTerminal(frame, baseUrl);
    }
  }
}

function _buildSimpleTerminal(frame, bridgeBase) {
  // Replace iframe with a div-based terminal emulator
  var parent = frame.parentNode;
  frame.style.display = 'none';

  var termDiv = document.createElement('div');
  termDiv.className = 'eva-terminal';

  var output = document.createElement('div');
  output.className = 'eva-terminal-output';
  output.id = 'terminalOutput';

  var inputRow = document.createElement('div');
  inputRow.className = 'eva-terminal-input-row';

  var prompt = document.createElement('span');
  prompt.className = 'eva-terminal-prompt';
  prompt.textContent = 'copilot> ';

  var input = document.createElement('input');
  input.type = 'text';
  input.className = 'eva-terminal-input';
  input.id = 'terminalInput';
  input.placeholder = 'Type a message for Copilot CLI...';
  input.spellcheck = false;
  input.autocomplete = 'off';

  inputRow.appendChild(prompt);
  inputRow.appendChild(input);
  termDiv.appendChild(output);
  termDiv.appendChild(inputRow);
  parent.appendChild(termDiv);

  // Welcome message
  _termPrint(output, 'info', 'Eva Terminal - Copilot CLI Interface');
  _termPrint(output, 'info', 'Messages are sent to the ACP bridge as Copilot prompts.');
  _termPrint(output, 'info', 'Type your message and press Enter.\n');

  input.addEventListener('keydown', function(e) {
    if (e.key !== 'Enter' || !input.value.trim()) return;
    var msg = input.value.trim();
    input.value = '';
    _termPrint(output, 'user', msg);
    _termSend(output, bridgeBase, msg);
  });
}

function _termPrint(output, cls, text) {
  var line = document.createElement('div');
  line.className = 'eva-term-line eva-term-' + cls;
  if (cls === 'eva' && typeof renderMarkdown === 'function') {
    line.innerHTML = renderMarkdown(text);
  } else {
    line.textContent = text;
  }
  output.appendChild(line);
  output.scrollTop = output.scrollHeight;
}

function _termSend(output, base, message) {
  _termPrint(output, 'info', 'Thinking...');
  fetch(base + '/v1/chat/completions', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      messages: [{ role: 'user', content: message }],
      model: 'copilot-acp'
    })
  }).then(function(r) { return r.json(); }).then(function(data) {
    // Remove "Thinking..." line
    var lines = output.querySelectorAll('.eva-term-info');
    if (lines.length) {
      var last = lines[lines.length - 1];
      if (last.textContent === 'Thinking...') output.removeChild(last);
    }
    var text = '';
    if (data.choices && data.choices[0]) {
      text = data.choices[0].message ? data.choices[0].message.content : (data.choices[0].text || '');
    } else if (data.error) {
      text = 'Error: ' + (data.error.message || JSON.stringify(data.error));
    } else {
      text = JSON.stringify(data);
    }
    _termPrint(output, 'eva', text);
  }).catch(function(e) {
    var lines = output.querySelectorAll('.eva-term-info');
    if (lines.length) {
      var last = lines[lines.length - 1];
      if (last.textContent === 'Thinking...') output.removeChild(last);
    }
    _termPrint(output, 'error', 'Error: ' + (e.message || e));
  });
}
