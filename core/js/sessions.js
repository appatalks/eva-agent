// sessions.js — Session persistence and explorer panel
// Uses IndexedDB (via idb-store.js) for session snapshots.
// Active conversation state stays in localStorage for backward compat with provider JS files.

var SESSION_INDEX_KEY = 'eva_sessions';
var SESSION_ACTIVE_KEY = 'eva_active_session';
var SESSION_PANEL_TAB_KEY = 'eva_session_panel_tab';
var SESSION_TITLE_MAX_LENGTH = 140;

// All provider message keys
var SESSION_MSG_KEYS = ['messages', 'copilotMessages', 'copilotACPMessages', 'geminiMessages', 'openLLMessages', 'aigMessages'];

function closeAgentOperationsForNavigation() {
  if (typeof EvaAgents !== 'undefined' && EvaAgents.close) EvaAgents.close();
}

function _getSessionIndex() {
  try { return JSON.parse(localStorage.getItem(SESSION_INDEX_KEY)) || []; }
  catch(e) { return []; }
}

function _saveSessionIndex(index) {
  localStorage.setItem(SESSION_INDEX_KEY, JSON.stringify(index));
}

function getAllSessions() {
  return Promise.resolve(_getSessionIndex().map(function(entry) {
    return {
      id: entry.id,
      title: entry.title || 'Untitled',
      createdAt: entry.created || 0,
      updatedAt: entry.updated || entry.created || 0,
      pinned: !!entry.pinned
    };
  }));
}

function _activeSessionId() {
  return localStorage.getItem(SESSION_ACTIVE_KEY) || null;
}

function _newSessionId() {
  if (globalThis.crypto && typeof globalThis.crypto.randomUUID === 'function') {
    return 'sess_' + globalThis.crypto.randomUUID();
  }
  if (globalThis.crypto && typeof globalThis.crypto.getRandomValues === 'function') {
    var bytes = new Uint8Array(16);
    globalThis.crypto.getRandomValues(bytes);
    return 'sess_' + Array.prototype.map.call(bytes, function(byte) {
      return byte.toString(16).padStart(2, '0');
    }).join('');
  }
  throw new Error('Secure session identifiers require Web Crypto support.');
}

function ensureActiveSessionId() {
  var id = _activeSessionId();
  if (id) return id;

  id = _newSessionId();
  localStorage.setItem(SESSION_ACTIVE_KEY, id);
  return id;
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
    if (key === 'id' || key.charAt(0) === '_') return; // skip record/meta keys
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
  } else if (txtOutput) {
    _restoreLegacySessionOutput(txtOutput, data);
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

function _restoreLegacySessionOutput(output, data) {
  output.replaceChildren();
  var bestMessages = [];
  SESSION_MSG_KEYS.forEach(function(key) {
    try {
      var messages = JSON.parse(data[key] || '[]');
      if (Array.isArray(messages) && messages.length > bestMessages.length) bestMessages = messages;
    } catch (_) {}
  });
  bestMessages.forEach(function(message) {
    if (!message || message.role === 'system' || message.role === 'developer') return;
    var content = typeof message.content === 'string' ? message.content : '';
    if (!content && Array.isArray(message.content)) {
      message.content.forEach(function(part) { if (part && part.text) content += part.text; });
    }
    if (!content) return;
    var line = document.createElement('div');
    line.className = 'message ' + (message.role === 'user' ? 'user' : 'eva');
    line.textContent = (message.role === 'user' ? 'You: ' : 'Eva: ') + content;
    output.appendChild(line);
  });
  if (!output.childNodes.length && data._masterOutput) {
    var transcript = document.createElement('div');
    transcript.className = 'message eva';
    transcript.textContent = data._masterOutput;
    output.appendChild(transcript);
  }
  if (!output.childNodes.length) {
    var unavailable = document.createElement('div');
    unavailable.className = 'session-empty';
    unavailable.textContent = 'This session has no restorable transcript.';
    output.appendChild(unavailable);
  }
  output.scrollTop = output.scrollHeight;
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
          if (txt) return txt.length > SESSION_TITLE_MAX_LENGTH
            ? txt.substring(0, SESSION_TITLE_MAX_LENGTH - 3) + '...' : txt;
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
  if (_sessionMsgCount(snapshot) === 0) return Promise.resolve();

  var id = _activeSessionId();
  var index = _getSessionIndex();

  if (!id) {
    // First save — create a new session
    id = _newSessionId();
    localStorage.setItem(SESSION_ACTIVE_KEY, id);
    index.unshift({ id: id, title: _sessionTitle(snapshot), created: Date.now(), updated: Date.now() });
  } else {
    // Update existing
    var found = false;
    for (var i = 0; i < index.length; i++) {
      if (index[i].id === id) {
        if (!index[i].customTitle) index[i].title = _sessionTitle(snapshot);
        index[i].updated = Date.now();
        found = true;
        break;
      }
    }
    if (!found) {
      index.unshift({ id: id, title: _sessionTitle(snapshot), created: Date.now(), updated: Date.now() });
    }
  }

  // Save to IndexedDB (async, non-blocking)
  var savePromise = idbSaveSession(id, snapshot).catch(function(e) {
    console.error('[Sessions] IDB save failed:', e);
  });
  _saveSessionIndex(index);
  renderSessionList();
  return savePromise;
}

/** Start a brand new session */
function newSession() {
  closeAgentOperationsForNavigation();
  if (typeof clearLastDeliveredSignal === 'function') clearLastDeliveredSignal();
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
    if (typeof restoreEvaWelcome === 'function') restoreEvaWelcome();
    else if (typeof showWelcome === 'function') showWelcome();
    else txtOutput.innerHTML = '';
  }

  renderSessionList();
}

/** Load a session by id */
function loadSession(id) {
  // Finish saving the current session before reading another record. This
  // avoids a read/restore race when users switch sessions quickly.
  return Promise.resolve(saveCurrentSession()).then(function() {
    return idbLoadSession(id);
  }).then(function(data) {
    if (!data) {
      var legacy = localStorage.getItem('session_' + id);
      if (legacy) {
        try {
          data = JSON.parse(legacy);
          idbSaveSession(id, data).then(function() {
            localStorage.removeItem('session_' + id);
          }).catch(function() {});
        } catch (error) {
          data = null;
        }
      }
    }
    if (!data) {
      var indexEntry = _getSessionIndex().find(function(entry) { return entry.id === id; });
      var output = document.getElementById('txtOutput');
      if (output) {
        output.replaceChildren();
        var notice = document.createElement('div');
        notice.className = 'session-restore-unavailable';
        var heading = document.createElement('strong');
        heading.textContent = indexEntry && indexEntry.title ? indexEntry.title : 'Saved session';
        var message = document.createElement('span');
        message.textContent = 'The session name is still indexed, but its transcript snapshot is unavailable on this installation.';
        notice.append(heading, message);
        output.appendChild(notice);
      }
      var unavailablePanel = document.getElementById('sessionPanel');
      if (unavailablePanel) unavailablePanel.setAttribute('aria-hidden', 'true');
      if (window.EvaWorkspaces && typeof window.EvaWorkspaces.closeWorkbench === 'function') window.EvaWorkspaces.closeWorkbench();
      if (typeof setStatus === 'function') setStatus('error', 'This saved session is unavailable. Its index entry was preserved.');
      return false;
    }
    _restoreSession(data);
    if (typeof clearLastDeliveredSignal === 'function') clearLastDeliveredSignal();
    localStorage.setItem(SESSION_ACTIVE_KEY, id);
    renderSessionList();
    var panel = document.getElementById('sessionPanel');
    if (panel) panel.setAttribute('aria-hidden', 'true');
    if (typeof EvaAgents !== 'undefined' && EvaAgents.close) EvaAgents.close();
    if (window.EvaWorkspaces && typeof window.EvaWorkspaces.closeWorkbench === 'function') window.EvaWorkspaces.closeWorkbench();
    if (typeof setStatus === 'function') setStatus('info', 'Session loaded.');
    return true;
  }).catch(function(e) {
    console.error('Failed to load session:', e);
    return false;
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

/** Rename a session; custom titles are preserved by later autosaves. */
async function renameSession(id) {
  var index = _getSessionIndex();
  var entry = index.filter(function(item) { return item.id === id; })[0];
  if (!entry) return;
  var title = await evaTextPrompt('Session title', entry.title || 'Untitled', { maxLength: SESSION_TITLE_MAX_LENGTH });
  if (title === null) return;
  title = title.trim().replace(/[\r\n\t]+/g, ' ').slice(0, SESSION_TITLE_MAX_LENGTH);
  if (!title) return;
  entry.title = title;
  entry.customTitle = true;
  entry.updated = Date.now();
  _saveSessionIndex(index);
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
    li.dataset.sessionId = entry.id;
    li.setAttribute('role', 'button');
    li.tabIndex = 0;

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

    var renameBtn = document.createElement('button');
    renameBtn.className = 'session-pin';
    renameBtn.textContent = '\u270E';
    renameBtn.title = 'Rename session';
    renameBtn.onclick = function(e) {
      e.stopPropagation();
      renameSession(entry.id);
    };

    var delBtn = document.createElement('button');
    delBtn.className = 'session-delete';
    delBtn.textContent = '\u00d7';
    delBtn.title = 'Delete session';
    delBtn.onclick = function(e) {
      e.stopPropagation();
      deleteSession(entry.id);
    };

    btnWrap.appendChild(renameBtn);
    btnWrap.appendChild(pinBtn);
    btnWrap.appendChild(delBtn);

    li.appendChild(titleSpan);
    li.appendChild(timeSpan);
    li.appendChild(btnWrap);
    ul.appendChild(li);
  });
}

function activateSessionListItem(event) {
  var target = event.target;
  if (!target || !target.closest) return;
  if (target.closest('.session-actions button')) return;
  var item = target.closest('.session-item[data-session-id]');
  if (!item || !item.closest('#sessionList')) return;
  loadSession(item.dataset.sessionId);
}

function _sessionPanelTab() {
  return localStorage.getItem(SESSION_PANEL_TAB_KEY) === 'active' ? 'active' : 'chats';
}

function setSessionPanelTab(tab) {
  var selected = tab === 'active' ? 'active' : 'chats';
  localStorage.setItem(SESSION_PANEL_TAB_KEY, selected);
  var chatsTab = document.getElementById('sessionChatsTab');
  var activeTab = document.getElementById('sessionActiveTab');
  var chatsView = document.getElementById('sessionChatsView');
  var activeView = document.getElementById('sessionActiveView');
  var actions = document.querySelector('#sessionPanel .session-panel-actions');
  if (chatsTab) chatsTab.setAttribute('aria-selected', selected === 'chats' ? 'true' : 'false');
  if (activeTab) activeTab.setAttribute('aria-selected', selected === 'active' ? 'true' : 'false');
  if (chatsTab) chatsTab.tabIndex = selected === 'chats' ? 0 : -1;
  if (activeTab) activeTab.tabIndex = selected === 'active' ? 0 : -1;
  if (chatsView) chatsView.hidden = selected !== 'chats';
  if (activeView) activeView.hidden = selected !== 'active';
  if (actions) actions.hidden = selected !== 'chats';
  if (selected === 'active') refreshActiveSessionList();
  else renderSessionList();
}

function _activeSessionStatusLabel(status) {
  return String(status || 'unknown').replace(/_/g, ' ').toUpperCase();
}

function _activeSessionKindLabel(kind) {
  var labels = { subagent: 'SUBAGENT', browser: 'BROWSER AGENT', desktop: 'DESKTOP AGENT', background: 'BACKGROUND' };
  return labels[kind] || String(kind || 'AGENT').toUpperCase();
}

function refreshActiveSessionList() {
  var list = document.getElementById('activeSessionList');
  if (!list) return;
  list.textContent = '';
  var loading = document.createElement('li');
  loading.className = 'session-empty';
  loading.textContent = 'Loading active sessions...';
  list.appendChild(loading);
  var base = (typeof getACPBridgeUrl === 'function') ? getACPBridgeUrl() : 'http://localhost:8888';
  fetch(String(base).replace(/\/+$/, '') + '/v1/agents/overview?include_graph=0').then(function(response) {
    if (!response.ok) throw new Error('Bridge returned ' + response.status);
    return response.json();
  }).then(function(data) {
    var active = (data.agents || []).filter(function(agent) {
      return ['starting', 'waiting', 'running', 'steering', 'finalizing', 'awaiting_confirmation', 'awaiting_input'].indexOf(agent.status) !== -1;
    });
    list.replaceChildren();
    if (!active.length) {
      var empty = document.createElement('li');
      empty.className = 'session-empty';
      empty.textContent = 'No active agent sessions';
      list.appendChild(empty);
      return;
    }
    var summary = document.createElement('li');
    summary.className = 'session-active-summary';
    summary.textContent = active.length + ' active agents; ' + (data.subagents_active || 0) +
      ' / ' + (data.capacity || 0) + ' subagent slots';
    list.appendChild(summary);
    active.forEach(function(agent) {
      var item = document.createElement('li');
      item.className = 'session-item active-session-item';
      item.tabIndex = 0;
      item.setAttribute('role', 'button');
      item.setAttribute('aria-label', 'Open ' + (agent.label || 'agent') + ' details');
      var title = document.createElement('span');
      title.className = 'session-title';
      title.textContent = agent.label || 'Agent session';
      title.title = title.textContent;
      var kind = document.createElement('span');
      kind.className = 'active-session-kind';
      kind.textContent = _activeSessionKindLabel(agent.kind) + (agent.model ? ' · ' + agent.model : '');
      var status = document.createElement('span');
      status.className = 'active-session-status';
      status.textContent = _activeSessionStatusLabel(agent.status) + (agent.activity ? ' · ' + agent.activity : '');
      var open = function() {
        if (typeof EvaAgents !== 'undefined' && EvaAgents.openAgent) EvaAgents.openAgent(agent.id);
      };
      item.addEventListener('click', open);
      item.addEventListener('keydown', function(event) {
        if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); open(); }
      });
      item.append(title, kind, status);
      list.appendChild(item);
    });
  }).catch(function(error) {
    list.replaceChildren();
    var unavailable = document.createElement('li');
    unavailable.className = 'session-empty';
    unavailable.textContent = 'Active sessions are unavailable: ' + (error.message || error);
    list.appendChild(unavailable);
  });
}

/** Toggle the session panel visibility */
function toggleSessionPanel() {
  var panel = document.getElementById('sessionPanel');
  if (!panel) return;
  var visible = panel.getAttribute('aria-hidden') !== 'true';
  if (visible) panel.setAttribute('aria-hidden', 'true');
  else {
    closeAgentOperationsForNavigation();
    closeSidePanels('sessionPanel');
    panel.setAttribute('aria-hidden', 'false');
  }
  if (!visible) setSessionPanelTab(_sessionPanelTab());
}

var EVA_SIDE_PANEL_IDS = ['sessionPanel', 'skillsPanel', 'memoryInspectorPanel', 'assetsPanel', 'workspacePanel', 'terminalPanel', 'profilePanel'];
var EVA_SIDE_PANEL_TRIGGER_IDS = ['evaChatsBtn', 'sidebarSessionsBtn', 'evaSkillsBtn', 'evaMemoryBtn', 'lcarsMemoryBtn', 'evaAssetsBtn', 'evaWorkspacesBtn', 'evaTerminalBtn', 'evaUserBtn'];

function closeSidePanels(exceptId) {
  EVA_SIDE_PANEL_IDS.forEach(function(id) {
    if (id === exceptId) return;
    if (id === 'memoryInspectorPanel' && window.EvaMemoryInspector && typeof window.EvaMemoryInspector.close === 'function') {
      window.EvaMemoryInspector.close();
      return;
    }
    var panel = document.getElementById(id);
    if (panel) panel.setAttribute('aria-hidden', 'true');
  });
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

  var chatsTab = document.getElementById('sessionChatsTab');
  if (chatsTab) chatsTab.addEventListener('click', function() { setSessionPanelTab('chats'); });
  var activeTab = document.getElementById('sessionActiveTab');
  if (activeTab) activeTab.addEventListener('click', function() { setSessionPanelTab('active'); });
  [chatsTab, activeTab].filter(Boolean).forEach(function(tab) {
    tab.addEventListener('keydown', function(event) {
      var tabs = [chatsTab, activeTab].filter(Boolean);
      var index = tabs.indexOf(tab);
      var nextIndex = index;
      if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') nextIndex = (index + tabs.length - 1) % tabs.length;
      else if (event.key === 'ArrowRight' || event.key === 'ArrowDown') nextIndex = (index + 1) % tabs.length;
      else if (event.key === 'Home') nextIndex = 0;
      else if (event.key === 'End') nextIndex = tabs.length - 1;
      else return;
      event.preventDefault();
      var nextTab = tabs[nextIndex];
      setSessionPanelTab(nextTab === activeTab ? 'active' : 'chats');
      nextTab.focus();
    });
  });
  var activeRefresh = document.getElementById('sessionActiveRefreshBtn');
  if (activeRefresh) activeRefresh.addEventListener('click', refreshActiveSessionList);

  var sessionList = document.getElementById('sessionList');
  if (sessionList && !sessionList.dataset.activationBound) {
    sessionList.dataset.activationBound = 'true';
    sessionList.addEventListener('click', activateSessionListItem);
    sessionList.addEventListener('keydown', function(event) {
      if (event.key !== 'Enter' && event.key !== ' ') return;
      var item = event.target && event.target.closest ? event.target.closest('.session-item[data-session-id]') : null;
      if (!item || event.target.closest('.session-actions button')) return;
      event.preventDefault();
      loadSession(item.dataset.sessionId);
    });
  }

  // Assets panel close button
  var assetsClose = document.getElementById('assetsPanelClose');
  if (assetsClose) assetsClose.addEventListener('click', toggleAssetsPanel);

  // Terminal panel close button
  var termClose = document.getElementById('terminalPanelClose');
  if (termClose) termClose.addEventListener('click', toggleTerminalPanel);
  var termExpand = document.getElementById('terminalPanelExpand');
  if (termExpand) termExpand.addEventListener('click', toggleTerminalWidth);

  // Agent Operations is a full workspace view. Any other sidebar destination
  // must first reveal the normal workspace so its panel/view is visible.
  var evaSidebar = document.getElementById('evaSidebar');
  if (evaSidebar) evaSidebar.addEventListener('click', function(event) {
    var target = event.target;
    if (target && target.closest && !target.closest('#evaAgentsBtn')) {
      closeAgentOperationsForNavigation();
    }
    if (target && target.closest && !target.closest('#evaWorkspacesBtn') && !target.closest('#evaTerminalBtn') && window.EvaWorkspaces && typeof window.EvaWorkspaces.closeWorkbench === 'function') {
      window.EvaWorkspaces.closeWorkbench();
    }
    if (target && target.closest && !target.closest('#evaAssetsBtn') && window.EvaAssets && typeof window.EvaAssets.close === 'function') {
      window.EvaAssets.close();
    }
    if (target && target.closest && !target.closest('#evaSkillsBtn') && window.EvaSkills && typeof window.EvaSkills.close === 'function') {
      window.EvaSkills.close();
    }
  });
  var lcarsSidebar = document.getElementById('lcarsSidebar');
  if (lcarsSidebar) lcarsSidebar.addEventListener('click', function(event) {
    var target = event.target;
    if (target && target.closest && !target.closest('#lcarsAgentsBtn')) {
      closeAgentOperationsForNavigation();
    }
    if (window.EvaWorkspaces && typeof window.EvaWorkspaces.closeWorkbench === 'function') {
      window.EvaWorkspaces.closeWorkbench();
    }
    if (window.EvaAssets && typeof window.EvaAssets.close === 'function') window.EvaAssets.close();
    if (window.EvaSkills && typeof window.EvaSkills.close === 'function') window.EvaSkills.close();
  });

  // Migrate localStorage sessions, preserve the previous turn, and always
  // present a fresh session when Eva launches.
  idbMigrateFromLocalStorage().then(function() {
    newSession();
  }).catch(function() {
    newSession();
  });

  document.addEventListener('click', function(event) {
    var target = event.target;
    if (!target || !target.closest) return;
    if (target.closest('#' + EVA_SIDE_PANEL_IDS.join(',#'))) return;
    if (target.closest('#' + EVA_SIDE_PANEL_TRIGGER_IDS.join(',#'))) return;
    closeSidePanels();
  }, true);

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
  if (visible) panel.setAttribute('aria-hidden', 'true');
  else {
    closeAgentOperationsForNavigation();
    closeSidePanels('assetsPanel');
    panel.setAttribute('aria-hidden', 'false');
  }
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
      if (!f || !/^[A-Za-z0-9._-]{1,128}$/.test(f.name || '')) return;
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
      dlBtn.onclick = async function(e) {
        e.stopPropagation();
        try {
          var response = await fetch(base + '/v1/files/' + encodeURIComponent(f.name));
          if (!response.ok) throw new Error('Download failed');
          var objectUrl = URL.createObjectURL(await response.blob());
          var a = document.createElement('a');
          a.href = objectUrl;
          a.download = f.name;
          document.body.appendChild(a);
          a.click();
          document.body.removeChild(a);
          URL.revokeObjectURL(objectUrl);
        } catch (error) {
          if (typeof setStatus === 'function') setStatus('error', error.message || 'Asset download failed');
        }
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
    ul.textContent = '';
    var errorItem = document.createElement('li');
    errorItem.className = 'session-empty';
    errorItem.textContent = 'Could not load assets: ' + (e.message || e);
    ul.appendChild(errorItem);
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
  panel.classList.toggle('terminal-panel-docked', document.body.classList.contains('workspace-workbench-open'));
  var visible = panel.getAttribute('aria-hidden') !== 'true';
  if (visible) {
    panel.setAttribute('aria-hidden', 'true');
    if (panel.dataset.evaReturnWorkspace === 'true') {
      panel.dataset.evaReturnWorkspace = '';
      var workspacePanel = document.getElementById('workspacePanel');
      if (workspacePanel) workspacePanel.setAttribute('aria-hidden', 'false');
    }
  }
  else {
    closeAgentOperationsForNavigation();
    closeSidePanels('terminalPanel');
    panel.setAttribute('aria-hidden', 'false');
  }
  if (!visible) initTerminal();
}

function toggleTerminalWidth() {
  var panel = document.getElementById('terminalPanel');
  var button = document.getElementById('terminalPanelExpand');
  if (!panel || !button) return;
  var expanded = panel.classList.toggle('terminal-panel-expanded');
  button.setAttribute('aria-label', expanded ? 'Restore terminal width' : 'Expand terminal');
  button.title = expanded ? 'Restore terminal width' : 'Expand terminal';
  var container = document.getElementById('terminalContainer');
  if (container && typeof container._evaWorkspaceTerminalFit === 'function') {
    setTimeout(container._evaWorkspaceTerminalFit, 0);
  }
}

var _evaWorkspaceTerminalTarget = { rootId: 'app-root', label: 'Eva app root' };

function openWorkspaceTerminal(rootId, label) {
  if (typeof rootId !== 'string' || !rootId) return;
  _evaWorkspaceTerminalTarget = { rootId: rootId, label: String(label || 'Workspace') };
  var panel = document.getElementById('terminalPanel');
  if (panel) panel.classList.toggle('terminal-panel-docked', document.body.classList.contains('workspace-workbench-open'));
  var workspacePanel = document.getElementById('workspacePanel');
  if (panel && workspacePanel && workspacePanel.getAttribute('aria-hidden') === 'false') {
    panel.dataset.evaReturnWorkspace = 'true';
  }
  if (panel && panel.getAttribute('aria-hidden') === 'true') {
    toggleTerminalPanel();
    return;
  }
  initTerminal();
  if (window.EvaTerminal && typeof window.EvaTerminal.open === 'function') {
    window.EvaTerminal.open(_evaWorkspaceTerminalTarget);
  }
}

function initTerminal() {
  var container = document.getElementById('terminalContainer');
  var fallback = document.getElementById('terminalFallback');
  var frame = document.getElementById('terminalFrame');
  if (!container) return;

  var baseUrl = (typeof getACPBridgeUrl === 'function') ? getACPBridgeUrl() : 'http://localhost:8888';

  if (frame && fallback) {
    fallback.style.display = 'none';
    if (!frame._evaTermInit) {
      frame._evaTermInit = true;
      if (window.evaStandalone && window.evaStandalone.workspaceTerminalV1) {
        _buildWorkspaceTerminal(frame).catch(function(error) {
          _buildSimpleTerminal(frame, baseUrl);
          var output = document.getElementById('terminalOutput');
          if (output) _termPrint(output, 'error', 'Local shell unavailable: ' + (error.message || error));
        });
      } else {
        _buildSimpleTerminal(frame, baseUrl);
      }
    } else if (typeof container._evaWorkspaceTerminalFit === 'function') {
      container._evaWorkspaceTerminalFit();
    }
  }
}

var _workspaceTerminalAssetsPromise = null;

function _loadWorkspaceTerminalAssets() {
  if (_workspaceTerminalAssetsPromise) return _workspaceTerminalAssetsPromise;
  _workspaceTerminalAssetsPromise = new Promise(function(resolve, reject) {
    var assets = window.evaStandalone && window.evaStandalone.terminalAssets;
    if (!assets || !assets.xterm || !assets.css || !assets.fit || !assets.search || !assets.webLinks) {
      reject(new Error('Terminal assets are unavailable.'));
      return;
    }
    if (!document.getElementById('evaXtermStyles')) {
      var stylesheet = document.createElement('link');
      stylesheet.id = 'evaXtermStyles';
      stylesheet.rel = 'stylesheet';
      stylesheet.href = assets.css;
      document.head.appendChild(stylesheet);
    }
    var scripts = [assets.xterm, assets.fit, assets.search, assets.webLinks];
    function loadNext(index) {
      if (index >= scripts.length) {
        resolve();
        return;
      }
      var script = document.createElement('script');
      script.src = scripts[index];
      script.onload = function() { loadNext(index + 1); };
      script.onerror = function() { reject(new Error('Could not load terminal renderer.')); };
      document.head.appendChild(script);
    }
    loadNext(0);
  });
  return _workspaceTerminalAssetsPromise;
}

function _workspaceTerminalColor(name, fallback) {
  var value = getComputedStyle(document.body).getPropertyValue(name).trim();
  return value || fallback;
}

async function _buildWorkspaceTerminal(frame) {
  await _loadWorkspaceTerminalAssets();
  var api = window.evaStandalone;
  var parent = frame.parentNode;
  frame.style.display = 'none';

  var surface = document.createElement('section');
  surface.className = 'workspace-terminal';
  surface.setAttribute('aria-label', 'Interactive local terminal');

  var toolbar = document.createElement('div');
  toolbar.className = 'workspace-terminal-toolbar';

  var identity = document.createElement('span');
  identity.className = 'workspace-terminal-identity';
  identity.textContent = 'LOCAL SHELL';
  identity.title = 'Interactive user terminal in the selected approved workspace';

  var rootLabel = document.createElement('span');
  rootLabel.className = 'workspace-terminal-root';
  rootLabel.textContent = _evaWorkspaceTerminalTarget.label;

  var status = document.createElement('span');
  status.className = 'workspace-terminal-status';
  status.textContent = 'CONNECTING';
  status.setAttribute('aria-live', 'polite');

  var searchInput = document.createElement('input');
  searchInput.className = 'workspace-terminal-search';
  searchInput.type = 'search';
  searchInput.placeholder = 'Find output';
  searchInput.setAttribute('aria-label', 'Find terminal output');

  var searchButton = document.createElement('button');
  searchButton.type = 'button';
  searchButton.className = 'workspace-terminal-tool';
  searchButton.textContent = 'Find';
  searchButton.title = 'Find next match';

  var restartButton = document.createElement('button');
  restartButton.type = 'button';
  restartButton.className = 'workspace-terminal-tool';
  restartButton.textContent = 'Restart';
  restartButton.title = 'Close this shell and start a new one';

  toolbar.append(identity, rootLabel, status, searchInput, searchButton, restartButton);

  var terminalHost = document.createElement('div');
  terminalHost.className = 'workspace-terminal-host';
  surface.append(toolbar, terminalHost);
  parent.appendChild(surface);

  var terminal = new Terminal({
    allowProposedApi: false,
    convertEol: false,
    cursorBlink: true,
    cursorStyle: 'bar',
    fontFamily: "'SFMono-Regular', 'Cascadia Code', 'Liberation Mono', monospace",
    fontSize: 13,
    scrollback: 5000,
    theme: {
      background: _workspaceTerminalColor('--eva-surface', '#0d0d1a'),
      foreground: _workspaceTerminalColor('--eva-text', '#eef5ff'),
      cursor: _workspaceTerminalColor('--eva-accent', '#78dce8'),
      selectionBackground: 'rgba(120, 220, 232, 0.28)'
    }
  });
  var fitAddon = new FitAddon.FitAddon();
  var searchAddon = new SearchAddon.SearchAddon();
  terminal.loadAddon(fitAddon);
  terminal.loadAddon(searchAddon);
  terminal.loadAddon(new WebLinksAddon.WebLinksAddon(function(event, uri) {
    event.preventDefault();
    window.open(uri, '_blank', 'noopener');
  }));
  terminal.open(terminalHost);

  var terminalId = '';
  var lastSequence = 0;
  var replayReady = false;
  var exited = false;
  var pendingEvents = [];
  var attachedRootId = '';

  function setStatus(value, state) {
    status.textContent = value;
    status.dataset.state = state || '';
  }

  function handleData(payload) {
    if (!terminalId || !payload || payload.id !== terminalId) return;
    if (!replayReady) {
      pendingEvents.push(payload);
      return;
    }
    if (payload.sequence > lastSequence) {
      lastSequence = payload.sequence;
      terminal.write(payload.data);
    }
  }

  var removeDataListener = api.onTerminalData(handleData);
  var removeExitListener = api.onTerminalExit(function(payload) {
    if (!payload || payload.id !== terminalId) return;
    exited = true;
    setStatus('EXIT ' + (payload.exitCode === null ? '?' : payload.exitCode), 'exited');
  });

  async function fitAndResize() {
    if (!surface.isConnected) return;
    fitAddon.fit();
    if (terminalId && !exited) {
      try {
        await api.terminalResize(terminalId, terminal.cols, terminal.rows);
      } catch (_) {}
    }
  }

  async function attachTerminal(forceNew) {
    var terminalTarget = _evaWorkspaceTerminalTarget;
    setStatus('CONNECTING', 'connecting');
    replayReady = false;
    pendingEvents = [];
    lastSequence = 0;
    exited = false;
    if (terminalId && (forceNew || attachedRootId !== terminalTarget.rootId)) {
      if (forceNew) {
        try { await api.terminalClose(terminalId); } catch (_) {}
      }
      terminalId = '';
      terminal.reset();
    }
    var sessions = await api.terminalList();
    var descriptor = forceNew ? null : sessions.find(function(item) {
      return item.rootId === terminalTarget.rootId && !item.exited;
    });
    if (!descriptor && !forceNew) {
      descriptor = sessions.find(function(item) { return item.rootId === terminalTarget.rootId; });
    }
    if (!descriptor) {
      fitAddon.fit();
      descriptor = await api.terminalCreate({
        rootId: terminalTarget.rootId,
        cols: terminal.cols,
        rows: terminal.rows
      });
    }
    terminalId = descriptor.id;
    attachedRootId = terminalTarget.rootId;
    rootLabel.textContent = terminalTarget.label;
    var replay = await api.terminalReplay(terminalId);
    if (replay.data) terminal.write(replay.data);
    lastSequence = replay.sequence;
    exited = replay.exited;
    replayReady = true;
    pendingEvents.sort(function(left, right) { return left.sequence - right.sequence; }).forEach(handleData);
    pendingEvents = [];
    setStatus(exited ? 'EXIT ' + (replay.exitCode === null ? '?' : replay.exitCode) : 'CONNECTED', exited ? 'exited' : 'connected');
    await fitAndResize();
    terminal.focus();
  }

  terminal.onData(function(data) {
    if (!terminalId || exited) return;
    api.terminalWrite(terminalId, data).catch(function(error) {
      setStatus('WRITE FAILED', 'error');
      terminal.write('\r\n\x1b[31m' + (error.message || error) + '\x1b[0m\r\n');
    });
  });
  searchButton.addEventListener('click', function() {
    if (searchInput.value) searchAddon.findNext(searchInput.value, { incremental: false });
    terminal.focus();
  });
  searchInput.addEventListener('keydown', function(event) {
    if (event.key === 'Enter') searchButton.click();
    if (event.key === 'Escape') terminal.focus();
  });
  restartButton.addEventListener('click', function() {
    attachTerminal(true).catch(function(error) { setStatus(error.message || 'FAILED', 'error'); });
  });

  var resizeObserver = new ResizeObserver(function() { fitAndResize(); });
  resizeObserver.observe(terminalHost);
  parent._evaWorkspaceTerminalFit = fitAndResize;
  window.EvaTerminal = window.EvaTerminal || {};
  window.EvaTerminal.open = function(target) {
    if (!target || typeof target.rootId !== 'string' || !target.rootId) return Promise.resolve();
    _evaWorkspaceTerminalTarget = { rootId: target.rootId, label: String(target.label || 'Workspace') };
    return attachTerminal(false);
  };
  window.addEventListener('beforeunload', function() {
    removeDataListener();
    removeExitListener();
    resizeObserver.disconnect();
  }, { once: true });

  await attachTerminal(false);
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
