// Agent Operations dashboard: live agent sessions and memory graph topology.

var EvaAgents = (function() {
  var AGENT_ACTIVE_POLL_MS = 2000;
  var AGENT_IDLE_POLL_MS = 5000;
  var AGENT_STALE_MS = 10000;
  var state = {
    open: false,
    data: null,
    selectedId: '',
    pollTimer: null,
    animationFrame: null,
    nodes: [],
    edges: [],
    nodeMap: {},
    hoverNode: null,
    dragNode: null,
    focusNode: null,
    graphPointerStart: null,
    entry: 'agents',
    canvasWidth: 0,
    canvasHeight: 0,
    lastFrame: 0,
    refreshController: null,
    refreshSequence: 0,
    graphFetchedAt: 0,
    view: 'floor',
    lastSyncAt: 0,
    telemetryError: '',
    reportsSequence: 0,
    securitySequence: 0,
    sitrailsClearBusy: false,
    sitrailsClearNotice: '',
    sitrailsSequence: 0,
    securityUrlOffset: 0,
    securityBlockOffset: 0,
    primaryTurns: new Map(),
    primaryTurnSequence: 0,
    clockTimer: null,
    actionBusy: {},
    securityAnalysis: '',
    parallaxX: 0,
    parallaxY: 0,
    parallaxTargetX: 0,
    parallaxTargetY: 0
  };

  function bridgeUrl() {
    var value = (typeof getACPBridgeUrl === 'function') ? getACPBridgeUrl() : 'http://localhost:8888';
    return String(value || '').replace(/\/+$/, '');
  }

  function isActive(status) {
    return ['starting', 'waiting', 'running', 'steering', 'cancelling', 'finalizing', 'awaiting_confirmation', 'awaiting_input'].indexOf(String(status || '').toLowerCase()) !== -1;
  }

  function statusLabel(status) {
    var labels = {
      online: 'CONNECTED', local_only: 'LOCAL MODE', running: 'WORKING',
      waiting: 'WAITING', steering: 'DIRECTION QUEUED', finalizing: 'FINALIZING',
      cancelling: 'STOPPING', awaiting_confirmation: 'NEEDS APPROVAL',
      awaiting_input: 'NEEDS INPUT', done: 'FINISHED', error: 'FAILED'
    };
    return labels[status] || String(status || 'unknown').replace(/_/g, ' ').toUpperCase();
  }

  function telemetryStale() {
    return !state.lastSyncAt || !!state.telemetryError || Date.now() - state.lastSyncAt > AGENT_STALE_MS;
  }

  function agentStatus(agent) {
    return (telemetryStale() ? 'LAST REPORTED: ' : '') + statusLabel(agent.status);
  }

  function element(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function setView(view) {
    state.view = ['floor', 'memory', 'cards', 'workspaces', 'reports', 'security'].indexOf(view) === -1 ? 'floor' : view;
    var views = { floor: ['agentsFloorTab', 'agentsFloor'], memory: ['agentsMemoryTab', 'agentsMemory'], cards: ['agentsCardsTab', 'agentsGrid'],
      workspaces: ['agentsWorkspacesTab', 'agentsWorkspaces'], reports: ['agentsReportsTab', 'agentsReports'],
      security: ['agentsSecurityTab', 'agentsSecurity'] };
    Object.keys(views).forEach(function(key) {
      var tab = document.getElementById(views[key][0]);
      var panel = document.getElementById(views[key][1]);
      if (tab) {
        tab.setAttribute('aria-selected', String(key === state.view));
        tab.tabIndex = key === state.view ? 0 : -1;
      }
      if (panel) panel.hidden = key !== state.view;
    });
    try { localStorage.setItem('evaAgentObservatoryView', state.view); } catch (_) {}
    var fullWidth = state.view === 'workspaces' || state.view === 'reports' || state.view === 'security';
    if (fullWidth) closeDetail();
    document.getElementById('agentsView').dataset.view = state.view;
    if (window.EvaWorkspaces && state.open) {
      if (state.view === 'workspaces') window.EvaWorkspaces.openWorkbench(true);
      else window.EvaWorkspaces.closeWorkbench(true);
    }
    if (state.open && state.view === 'reports') refreshReports();
    if (state.open && state.view === 'security') refreshSecurity();
    if (typeof EvaBridgeScene !== 'undefined') EvaBridgeScene.setActive(state.open && state.view === 'floor');
    if (state.open && state.view === 'memory') {
      startGraph();
      if (state.data) render();
    } else stopGraph();
  }

  function updateTelemetry() {
    var stale = telemetryStale();
    var view = document.getElementById('agentsView');
    if (view) view.dataset.stale = String(stale);
    setText('agentsTelemetryState', state.telemetryError
      ? 'Disconnected: showing last snapshot. Controls paused.'
      : (stale ? 'Awaiting fresh telemetry. Controls paused.' : 'Synced ' + Math.max(0, Math.floor((Date.now() - state.lastSyncAt) / 1000)) + 's ago'));
    if (stale) setText('agentsUpdatedAt', state.telemetryError ? 'BRIDGE OFFLINE' : 'STALE SNAPSHOT');
    return stale;
  }

  function tick() {
    if (!state.open) return;
    updateTelemetry();
    var agents = primaryAgentView(state.data && state.data.agents || []);
    renderFloor(agents);
    agents.forEach(function(agent) {
      var card = Array.prototype.find.call(document.querySelectorAll('.agent-card'), function(item) {
        return item.dataset.agentId === agent.id;
      });
      if (card) updateAgentCard(card, agent);
    });
    if (state.selectedId) renderDetail(state.selectedId);
    state.clockTimer = setTimeout(tick, 1000);
  }

  function kindLabel(kind) {
    var labels = { eva: 'PRIMARY AGENT', security: 'SECURITY OFFICER', subagent: 'ACP SUBAGENT', browser: 'BROWSER', desktop: 'DESKTOP', background: 'BACKGROUND' };
    return labels[kind] || String(kind || 'AGENT').toUpperCase();
  }

  function modelLabel(model) {
    return typeof evaDisplayModelLabel === 'function' ? evaDisplayModelLabel(model) : String(model || '');
  }

  function elapsed(agent) {
    var start = Date.parse(agent.started_at || '');
    var end = Date.parse(agent.ended_at || '') || (telemetryStale() ? state.lastSyncAt : Date.now());
    if (!start) return '--';
    var seconds = Math.max(0, Math.round((end - start) / 1000));
    if (seconds < 60) return seconds + 's';
    var minutes = Math.floor(seconds / 60);
    return minutes < 60 ? minutes + 'm ' + (seconds % 60) + 's' : Math.floor(minutes / 60) + 'h ' + (minutes % 60) + 'm';
  }

  function setEntry(entry) {
    state.entry = 'agents';
    var view = document.getElementById('agentsView');
    var agentsButton = document.getElementById('evaAgentsBtn');
    var lcarsButton = document.getElementById('lcarsAgentsBtn');
    var kicker = document.getElementById('agentsViewKicker');
    var title = document.getElementById('agentsViewTitle');
    if (view) view.setAttribute('aria-label', 'Agent Observatory');
    if (agentsButton) agentsButton.classList.toggle('active', state.open);
    if (lcarsButton) lcarsButton.classList.toggle('active', state.open);
    if (kicker) kicker.textContent = 'LIVE ORCHESTRATION';
    if (title) title.textContent = 'Agent Observatory';
  }

  function open(entry) {
    if (state.open) {
      setEntry(entry);
      return refresh(true);
    }
    if (typeof closeVoiceView === 'function' && typeof _vv !== 'undefined' && _vv.open) closeVoiceView();
    if (window.EvaWorkspaces && typeof window.EvaWorkspaces.closeWorkbench === 'function') window.EvaWorkspaces.closeWorkbench(true);
    if (window.EvaAssets && typeof window.EvaAssets.close === 'function') window.EvaAssets.close();
    if (window.EvaSkills && typeof window.EvaSkills.close === 'function') window.EvaSkills.close();
    closeSidePanels();
    state.open = true;
    setEntry(entry);
    document.body.classList.add('agents-view-open');
    var view = document.getElementById('agentsView');
    if (view) view.setAttribute('aria-hidden', 'false');
    var refreshPromise = refreshAndSchedule();
    setView(state.view);
    tick();
    return refreshPromise;
  }

  function close() {
    state.open = false;
    state.reportsSequence++;
    if (window.EvaWorkspaces && typeof window.EvaWorkspaces.closeWorkbench === 'function') window.EvaWorkspaces.closeWorkbench(true);
    document.body.classList.remove('agents-view-open', 'agent-workspace-open');
    var view = document.getElementById('agentsView');
    var button = document.getElementById('evaAgentsBtn');
    var lcarsButton = document.getElementById('lcarsAgentsBtn');
    if (view) view.setAttribute('aria-hidden', 'true');
    if (button) button.classList.remove('active');
    if (lcarsButton) lcarsButton.classList.remove('active');
    if (state.pollTimer) clearTimeout(state.pollTimer);
    state.pollTimer = null;
    if (state.clockTimer) clearTimeout(state.clockTimer);
    state.clockTimer = null;
    state.refreshSequence++;
    if (state.refreshController) state.refreshController.abort();
    state.refreshController = null;
    stopGraph();
    if (typeof EvaBridgeScene !== 'undefined') EvaBridgeScene.setActive(false);
  }

  function nextPollDelay() {
    return state.data && (state.data.active_total || 0) > 0
      ? AGENT_ACTIVE_POLL_MS : AGENT_IDLE_POLL_MS;
  }

  function scheduleNextRefresh() {
    if (!state.open) return;
    if (state.pollTimer) clearTimeout(state.pollTimer);
    state.pollTimer = setTimeout(refreshAndSchedule, nextPollDelay());
  }

  function refreshAndSchedule() {
    return Promise.resolve(refresh()).finally(scheduleNextRefresh);
  }

  function toggle() {
    if (state.open && state.entry === 'agents') close(); else open('agents');
  }

  function openWorkspace() {
    return openWorkspaces();
  }

  function openWorkspaces() {
    var ready = open();
    setView('workspaces');
    return ready;
  }

  async function deleteMissionLogEntry(scope, button) {
    var security = scope === 'security' || scope === 'urls' || scope.indexOf('block:') === 0;
    var prompt = scope === 'reports' ? 'Permanently delete all mission debriefs?' :
      scope === 'security' ? 'Permanently delete all permission blocks and URL observations?' :
      scope === 'urls' ? 'Permanently delete all URL observations? Permission blocks and logging settings will be kept; new observations may appear.' :
      'Permanently delete this entry?';
    if (!confirm(prompt)) return;
    button.disabled = true;
    try {
      var options = {
        method: 'POST',
        headers: typeof getBridgeCapabilityHeaders === 'function' ? getBridgeCapabilityHeaders() : { 'Content-Type': 'application/json' },
        signal: AbortSignal.timeout(8000)
      };
      var prepared = await fetch(bridgeUrl() + '/v1/agents/reports/delete/prepare', Object.assign({}, options, {
        body: JSON.stringify({ scope: scope })
      }));
      var confirmation = await prepared.json();
      if (!prepared.ok || confirmation.scope !== scope || !confirmation.confirmation) throw new Error('Deletion could not be prepared.');
      var response = await fetch(bridgeUrl() + '/v1/agents/reports/delete', Object.assign({}, options, {
        signal: AbortSignal.timeout(8000),
        body: JSON.stringify({ scope: scope, confirmation: confirmation.confirmation })
      }));
      var receipt = await response.json();
      if (!response.ok || typeof receipt.deleted !== 'number') throw new Error('Deletion was not confirmed.');
      if (security) {
        state.securityUrlOffset = 0;
        if (scope !== 'urls') state.securityBlockOffset = 0;
        await refreshSecurity();
        setText('securityLogStatus', 'Deleted ' + receipt.deleted + (scope === 'urls' ? ' URL observations.' : ' security entries.'));
      } else {
        state.reportsSequence++;
        await refreshReports();
        setText('missionLogStatus', 'Deleted ' + receipt.deleted + ' debrief entries.');
      }
    } catch (error) {
      setText(security ? 'securityLogStatus' : 'missionLogStatus', error.message || 'Deletion failed.');
    } finally { button.disabled = false; }
  }

  async function setMissionResearchPermission(allowed) {
    if (allowed && !confirm('Allow unverified background search/fetch tools? Their labels do not guarantee read-only behavior, and remote services may receive request data.')) return;
    var allow = document.getElementById('missionLogAllowResearch');
    var deny = document.getElementById('missionLogDenyResearch');
    allow.disabled = deny.disabled = true;
    try {
      var response = await fetch(bridgeUrl() + '/v1/agents/reports/research-permission', {
        method: 'POST',
        headers: typeof getBridgeCapabilityHeaders === 'function' ? getBridgeCapabilityHeaders() : { 'Content-Type': 'application/json' },
        body: JSON.stringify({ allowed: allowed }),
        signal: AbortSignal.timeout(8000)
      });
      var receipt = await response.json();
      if (!response.ok || receipt.allowed !== allowed) throw new Error('Permission change was not confirmed.');
      await refreshSecurity();
    } catch (error) {
      setText('securityLogStatus', error.message || 'Permission change failed.');
    } finally { allow.disabled = deny.disabled = false; }
  }

  async function setSecurityUrlLogging(enabled) {
    if (enabled && !confirm('Record visible URLs locally until cleared? Query strings, fragments, and userinfo are removed, but hostnames and paths can still be sensitive.')) return;
    var enable = document.getElementById('securityUrlEnable');
    var disable = document.getElementById('securityUrlDisable');
    enable.disabled = disable.disabled = true;
    try {
      var response = await fetch(bridgeUrl() + '/v1/security/url-logging', {
        method: 'POST', headers: getBridgeCapabilityHeaders(),
        body: JSON.stringify({ enabled: enabled }), signal: AbortSignal.timeout(8000)
      });
      var result = await response.json();
      if (!response.ok || result.enabled !== enabled) throw new Error('URL logging preference was not confirmed.');
      await refreshSecurity();
    } catch (error) {
      setText('securityLogStatus', error.message || 'URL logging preference could not be saved.');
    } finally { enable.disabled = disable.disabled = false; }
  }

  function debriefText(text) {
    try {
      var parsed = JSON.parse(text);
      if (parsed && !Array.isArray(parsed) && typeof parsed === 'object') {
        for (var key of ['summary', 'message', 'text', 'result']) {
          if (typeof parsed[key] === 'string' && parsed[key].trim()) return parsed[key];
        }
      }
    } catch (_) {}
    return text || 'No report text was returned.';
  }

  async function refreshReports() {
    var sequence = ++state.reportsSequence;
    try {
      var response = await fetch(bridgeUrl() + '/v1/agents/reports', { signal: AbortSignal.timeout(8000) });
      if (!response.ok) throw new Error('Report archive returned ' + response.status);
      var payload = await response.json();
      if (sequence !== state.reportsSequence) return;
      if (!Array.isArray(payload.reports)) throw new Error('Invalid report archive.');
      var host = document.getElementById('missionLogReports');
      var existing = {};
      host.querySelectorAll('[data-report-id]').forEach(function(node) { existing[node.dataset.reportId] = node; });
      payload.reports.forEach(function(report, index) {
        var card = existing[report.id];
        if (!card) {
          card = element('article', 'mission-log-report');
          card.dataset.reportId = report.id;
          var label = element('label', 'mission-log-check');
          var check = element('input', '');
          check.type = 'checkbox';
          check.setAttribute('aria-label', 'Mark report reviewed: ' + report.label);
          label.append(check, element('strong', '', report.label));
          var detail = element('details', '');
          detail.append(element('summary', '', 'Read debrief'), element('pre', 'mission-log-body', debriefText(report.report)));
          card.append(label, element('p', 'mission-log-meta', statusLabel(report.status) + ' / ' + new Date(report.ended_at).toLocaleString()), detail);
          var remove = element('button', '', 'Delete');
          remove.type = 'button';
          remove.addEventListener('click', function() { deleteMissionLogEntry('report:' + report.id, remove); });
          card.append(remove);
          check.addEventListener('change', async function() {
            var requested = check.checked;
            check.disabled = true;
            try {
              var saved = await fetch(bridgeUrl() + '/v1/agents/reports/review', {
                method: 'POST',
                headers: typeof getBridgeCapabilityHeaders === 'function' ? getBridgeCapabilityHeaders() : { 'Content-Type': 'application/json' },
                body: JSON.stringify({ id: report.id, reviewed: requested }),
                signal: AbortSignal.timeout(8000)
              });
              var receipt = await saved.json();
              if (!saved.ok || receipt.id !== report.id || receipt.reviewed !== requested) throw new Error('Checklist update was not confirmed.');
              state.reportsSequence++;
              card.dataset.reviewed = String(requested);
              setText('missionLogStatus', requested ? 'Report marked reviewed. Original debrief retained.' : 'Report returned to your checklist.');
            } catch (error) {
              check.checked = !requested;
              setText('missionLogStatus', error.message || 'Checklist update failed.');
            } finally { check.disabled = false; }
          });
        }
        var checkbox = card.querySelector('input');
        if (!checkbox.disabled) {
          checkbox.checked = report.reviewed === true;
          card.dataset.reviewed = String(report.reviewed === true);
        }
        if (host.children[index] !== card) host.insertBefore(card, host.children[index] || null);
        delete existing[report.id];
      });
      Object.keys(existing).forEach(function(id) { existing[id].remove(); });
      setText('missionLogStatus', 'Showing up to 100 recent debriefs; stored locally.');
    } catch (error) {
      if (sequence === state.reportsSequence) setText('missionLogStatus', 'Archive unavailable. Last loaded reports retained. ' + error.message);
    }
  }

  async function refreshSecurity() {
    var sequence = ++state.securitySequence;
    refreshSitrails();
    try {
      var response = await fetch(bridgeUrl() + '/v1/security/log?url_offset=' + state.securityUrlOffset +
        '&block_offset=' + state.securityBlockOffset, { headers: getBridgeCapabilityHeaders(), signal: AbortSignal.timeout(8000) });
      if (!response.ok) throw new Error('Security log returned ' + response.status);
      var payload = await response.json();
      if (sequence !== state.securitySequence) return;
      if (!Array.isArray(payload.urls) || !Array.isArray(payload.blocks)) throw new Error('Invalid security log.');
      setText('missionLogResearchStatus', payload.unverified_research_allowed
        ? 'Unverified background research: allowed for future requests.'
        : 'Unverified background research: denied for future requests.');
      setText('securityUrlLoggingStatus', payload.url_logging_enabled
        ? 'URL logging is enabled. Existing observations remain until cleared.'
        : 'URL logging is paused. Existing observations remain until cleared.');
      var body = document.getElementById('securityLogUrlBody');
      body.replaceChildren();
      payload.urls.forEach(function(item) {
        var row = document.createElement('tr');
        [item.method || 'UNKNOWN', item.url || '', String(item.hits || 0),
          String(item.observation || '').replace(/_/g, ' '),
          item.last_seen ? new Date(item.last_seen).toLocaleString() : ''].forEach(function(value) {
          row.appendChild(element('td', '', value));
        });
        body.appendChild(row);
      });
      if (!payload.urls.length) {
        var empty = document.createElement('tr');
        var cell = element('td', '', 'No URL observations on this page.');
        cell.colSpan = 5;
        empty.appendChild(cell);
        body.appendChild(empty);
      }
      var blocksHost = document.getElementById('missionLogBlocks');
      blocksHost.replaceChildren();
      payload.blocks.forEach(function(block) {
        var card = element('article', 'mission-log-report');
        card.append(element('strong', '', (block.source === 'workspace' ? 'Workspace' : 'Background') +
          ' ' + String(block.tool_kind || 'action') + ' blocked'));
        card.append(element('p', 'mission-log-meta', new Date(block.created_at).toLocaleString()));
        card.append(element('p', 'mission-log-body', 'Reason: ' + String(block.reason || 'unknown').replace(/_/g, ' ')));
        var remove = element('button', '', 'Delete');
        remove.type = 'button';
        remove.addEventListener('click', function() { deleteMissionLogEntry('block:' + block.id, remove); });
        card.append(remove);
        blocksHost.append(card);
      });
      if (!payload.blocks.length) blocksHost.append(element('p', 'mission-log-status', 'No permission blocks on this page.'));
      document.getElementById('securityUrlPrevious').disabled = state.securityUrlOffset === 0;
      document.getElementById('securityUrlNext').disabled = !payload.urls_more;
      document.getElementById('securityBlockPrevious').disabled = state.securityBlockOffset === 0;
      document.getElementById('securityBlockNext').disabled = !payload.blocks_more;
      setText('securityLogStatus', 'URLs ' + (payload.urls.length ? state.securityUrlOffset + 1 : 0) + '–' +
        (payload.urls.length ? state.securityUrlOffset + payload.urls.length : 0) +
        '; blocks ' + (payload.blocks.length ? state.securityBlockOffset + 1 : 0) + '–' +
        (payload.blocks.length ? state.securityBlockOffset + payload.blocks.length : 0) + '. Stored until cleared.');
    } catch (error) {
      if (sequence === state.securitySequence) setText('securityLogStatus', 'Security log unavailable. ' + error.message);
    }
  }

  async function refreshSitrails() {
    var sequence = ++state.sitrailsSequence;
    try {
      var response = await fetch(bridgeUrl() + '/v1/security/sitrails', {
        headers: getBridgeCapabilityHeaders(), signal: AbortSignal.timeout(8000)
      });
      if (!response.ok) throw new Error('Status unavailable.');
      var data = await response.json();
      if (sequence !== state.sitrailsSequence) return;
      var input = document.getElementById('sitrailsUrl');
      if (document.activeElement !== input) input.value = data.url;
      setText('sitrailsStatus', data.status);
      document.getElementById('sitrailsEnable').disabled = !!data.enabled;
      document.getElementById('sitrailsDisable').disabled = !data.enabled;
      var clear = data.clear || {};
      document.getElementById('sitrailsClear').disabled = state.sitrailsClearBusy || clear.state === 'running';
      if (!state.sitrailsClearBusy && clear.state === 'running') {
        state.sitrailsClearNotice = '';
        setText('sitrailsClearStatus', 'Resolving selected alerts: ' + (clear.resolved || 0) +
          ' confirmed, ' + (clear.unconfirmed || 0) + ' unconfirmed, ' + (clear.skipped || 0) +
          ' skipped; ' + (clear.total || 0) + ' selected.');
      } else if (!state.sitrailsClearBusy && state.sitrailsClearNotice) {
        setText('sitrailsClearStatus', state.sitrailsClearNotice);
      } else if (!state.sitrailsClearBusy && clear.state === 'completed') {
        setText('sitrailsClearStatus', 'Resolved ' + (clear.resolved || 0) + ' alert(s); ' +
          (clear.unconfirmed || 0) + ' unconfirmed and ' + (clear.skipped || 0) + ' changed or no longer confirmed open.' +
          (clear.more ? ' Additional or changing alerts may remain.' : '') +
          (clear.local_cleanup_failed ? ' Some local review records could not be removed.' : ''));
      } else if (!state.sitrailsClearBusy && clear.state === 'stopped') {
        setText('sitrailsClearStatus', 'Clear stopped after settings changed: ' + (clear.resolved || 0) +
          ' resolved, ' + (clear.unconfirmed || 0) + ' unconfirmed, ' + (clear.skipped || 0) + ' skipped.' +
          (clear.local_cleanup_failed ? ' Some local review records could not be removed.' : ''));
      } else if (!state.sitrailsClearBusy && clear.state === 'error') {
        setText('sitrailsClearStatus', 'Clear did not complete: ' + (clear.resolved || 0) +
          ' confirmed resolved, ' + (clear.unconfirmed || 0) + ' unconfirmed, ' + (clear.skipped || 0) +
          ' skipped. Check Sitrails for current alert statuses before trying again.' +
          (clear.local_cleanup_failed ? ' Some local review records could not be removed.' : ''));
      }
      var body = document.getElementById('sitrailsAlertBody');
      body.replaceChildren();
      (data.alerts || []).forEach(function(alert) {
        var row = document.createElement('tr');
        row.appendChild(element('td', '', alert.timestamp ? new Date(alert.timestamp).toLocaleString() : ''));
        var alertCell = document.createElement('td');
        alertCell.append(element('strong', '', alert.rule), element('p', '', alert.description));
        alertCell.appendChild(element('small', '', 'Alert ID: ' + alert.id));
        row.appendChild(alertCell);
        row.appendChild(element('td', '', alert.severity + ' / ' + alert.confidence + ' confidence'));
        row.appendChild(element('td', '', alert.recommendation));
        var actions = document.createElement('td');
        actions.appendChild(element('p', '', alert.assessment || 'Awaiting automatic Security Officer review.'));
        var dismiss = element('button', '', 'Dismiss alert');
        dismiss.type = 'button';
        dismiss.disabled = state.sitrailsClearBusy || clear.state === 'running' ||
          alert.recommendation === 'pending' || !alert.review_token;
        dismiss.setAttribute('aria-label', 'Dismiss this reviewed Sitrails alert');
        dismiss.addEventListener('click', function() {
          dismiss.disabled = true;
          clearSitrailsObservations(alert);
        });
        actions.appendChild(dismiss);
        var link = element('a', '', 'Open Sitrails');
        link.href = data.url + '/#alerts';
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
        actions.appendChild(link);
        row.appendChild(actions);
        body.appendChild(row);
      });
      if (!data.alerts || !data.alerts.length) {
        var empty = document.createElement('tr');
        var cell = element('td', '', data.enabled ? 'No open alerts loaded.' : 'Monitoring is paused.');
        cell.colSpan = 5;
        empty.appendChild(cell);
        body.appendChild(empty);
      }
    } catch (error) {
      if (sequence === state.sitrailsSequence) setText('sitrailsStatus', 'Sitrails monitoring status unavailable.');
    }
  }

  async function clearSitrailsObservations(alert) {
    var button = document.getElementById('sitrailsClear');
    if (state.sitrailsClearBusy) return;
    state.sitrailsClearBusy = true;
    state.sitrailsClearNotice = '';
    button.disabled = true;
    setText('sitrailsClearStatus', 'Finding open alerts in Sitrails...');
    try {
      var options = {
        method: 'POST', headers: getBridgeCapabilityHeaders(), signal: AbortSignal.timeout(60000)
      };
      var prepared = await fetch(bridgeUrl() + '/v1/security/sitrails/clear/prepare',
        Object.assign({}, options, { body: JSON.stringify(alert
          ? { id: alert.id, review_token: alert.review_token } : {}) }));
      var plan = await prepared.json();
      if (!prepared.ok || typeof plan.count !== 'number' || !plan.confirmation) {
        throw new Error((plan.error && plan.error.message) || 'Could not find open Sitrails alerts.');
      }
      if (!plan.count) {
        state.sitrailsClearNotice = 'No open Sitrails alerts were found.';
        return;
      }
      if (alert && plan.count !== 1) throw new Error('This alert is no longer available for dismissal.');
      var prompt = (alert
        ? 'Dismiss this reviewed alert by marking it resolved in Sitrails and removing its Eva review?'
        : 'Resolve ' + plan.count + ' open alert(s) in Sitrails and remove their Eva reviews?') +
        '\n\nRecorded evidence and trails will remain. This is not a security assessment.' +
        '\nNew or changed alerts may remain. Sitrails cannot make this operation atomic, so statuses can change during it.' +
        '\nPausing or changing settings stops further updates; a request already in flight may finish.' +
        (plan.more ? '\nAdditional alerts may remain; repeat the action if needed.' : '');
      if (!window.confirm(prompt)) {
        state.sitrailsClearNotice = 'Clear cancelled; no alert statuses changed.';
        return;
      }
      var response = await fetch(bridgeUrl() + '/v1/security/sitrails/clear',
        Object.assign({}, options, { body: JSON.stringify({ confirmation: plan.confirmation }) }));
      var result = await response.json();
      if (!response.ok || result.started !== true) {
        throw new Error((result.error && result.error.message) || 'Could not start clearing alerts.');
      }
      await refreshSitrails();
    } catch (error) {
      state.sitrailsClearNotice = error.message || (alert ? 'Alert dismissal failed.' : 'Sitrails clear failed.');
    } finally {
      state.sitrailsClearBusy = false;
      button.disabled = false;
      await refreshSitrails();
    }
  }

  async function setSitrailsMonitoring(enabled) {
    var enable = document.getElementById('sitrailsEnable');
    var disable = document.getElementById('sitrailsDisable');
    enable.disabled = disable.disabled = true;
    try {
      var response = await fetch(bridgeUrl() + '/v1/security/sitrails', {
        method: 'POST', headers: Object.assign({ 'Content-Type': 'application/json' }, getBridgeCapabilityHeaders()),
        body: JSON.stringify({ enabled: enabled, url: document.getElementById('sitrailsUrl').value.trim() }),
        signal: AbortSignal.timeout(8000)
      });
      var data = await response.json();
      if (!response.ok) throw new Error((data.error && data.error.message) || 'Settings could not be saved.');
      await refreshSitrails();
    } catch (error) {
      setText('sitrailsStatus', error.message);
      enable.disabled = disable.disabled = false;
    }
  }

  async function refresh(forceGraph) {
    var sequence = ++state.refreshSequence;
    if (state.refreshController) state.refreshController.abort();
    var controller = new AbortController();
    state.refreshController = controller;
    var timedOut = false;
    var timeout = setTimeout(function() { timedOut = true; controller.abort(); }, 8000);
    try {
      var includeGraph = forceGraph === true || (state.view === 'memory' && (!state.data || !state.data.graph || Date.now() - state.graphFetchedAt >= 30000));
      var sessionId = typeof ensureActiveSessionId === 'function' ? ensureActiveSessionId() : '';
      var response = await fetch(bridgeUrl() + '/v1/agents/overview?include_graph=' + (includeGraph ? '1' : '0') + '&session_id=' + encodeURIComponent(sessionId), { signal: controller.signal });
      if (!response.ok) throw new Error('Bridge returned ' + response.status);
      var data = await response.json();
      if (sequence !== state.refreshSequence) return;
      if (!data || !Array.isArray(data.agents)) throw new Error('Runtime returned no agent snapshot.');
      if (data.graph) state.graphFetchedAt = Date.now();
      else if (state.data && state.data.graph) data.graph = state.data.graph;
      state.data = data;
      state.lastSyncAt = Date.now();
      state.telemetryError = '';
      render();
      if (state.open && state.view === 'reports') refreshReports();
      if (state.open && state.view === 'security') refreshSecurity();
    } catch (error) {
      if (sequence !== state.refreshSequence) return;
      if (error && error.name === 'AbortError' && !timedOut) return;
      renderUnavailable(error.message || String(error));
    } finally {
      clearTimeout(timeout);
    }
  }

  function renderUnavailable(message) {
    state.telemetryError = message;
    updateTelemetry();
    renderFloor(state.data && state.data.agents || []);
    if (state.selectedId) renderDetail(state.selectedId);
    var grid = document.getElementById('agentsGrid');
    var updated = document.getElementById('agentsUpdatedAt');
    if (updated) updated.textContent = 'BRIDGE OFFLINE';
    if (grid && !grid.children.length) {
      var empty = document.createElement('div');
      empty.className = 'agents-empty';
      empty.textContent = 'Agent runtime unavailable. ' + message;
      grid.appendChild(empty);
    }
  }

  function render() {
    var data = state.data || {};
    var agents = primaryAgentView(data.agents || []);
    var graph = data.graph || { nodes: [], edges: [] };
    var agentById = {};
    agents.forEach(function(agent) { agentById['agent-' + agent.id] = agent; });
    (graph.nodes || []).forEach(function(node) {
      var liveAgent = agentById[node.id];
      if (!liveAgent) return;
      node.status = liveAgent.status;
      node.model = liveAgent.model || node.model || 'default';
      node.result = liveAgent.result || '';
    });
    var localPrimary = state.primaryTurns.size > 0 && !(data.agents || []).some(function(agent) { return agent.kind === 'eva' && isActive(agent.status); });
    setText('agentsActiveCount', (data.active_total || 0) + (localPrimary ? 1 : 0));
    setText('agentsCapacity', (data.subagents_active || 0) + ' / ' + (data.capacity || 4));
    setText('agentsNodeCount', data.graph ? (graph.nodes || []).length : '--');
    setText('agentGraphEdgeCount', (graph.edges || []).length + ' relations');
    setText('agentsBackgroundState', data.background && data.background.running ? 'ONLINE' : 'IDLE');
    setText('agentsUpdatedAt', 'SYNC ' + new Date(data.generated_at || Date.now()).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }));
    updateTelemetry();
    var badge = document.getElementById('evaAgentsBadge');
    if (badge) {
      badge.textContent = (data.active_total || 0) + (localPrimary ? 1 : 0);
      badge.classList.toggle('active', (data.active_total || 0) + (localPrimary ? 1 : 0) > 0);
    }
    renderCards(agents);
    renderFloor(agents);
    updateGraph(graph.nodes || [], graph.edges || []);
    if (state.selectedId && !agents.some(function(agent) { return agent.id === state.selectedId; })) closeDetail();
    if (state.selectedId) renderDetail(state.selectedId);
  }

  function setText(id, value) {
    var element = document.getElementById(id);
    if (element) element.textContent = value;
  }

  function updateCrewBubble(station, agent, stale) {
    var events = [];
    if (agent.activity) events.push({ kind: 'Activity', text: agent.activity, at: Date.parse(agent.last_activity_at) || 0 });
    if (typeof agent.result === 'string' && agent.result.trim() && agent.last_output_at) {
      events.push({ kind: 'Output', text: agent.result.trim().slice(-600), at: Date.parse(agent.last_output_at) || 0 });
    }
    if (Array.isArray(agent.received_from) && agent.received_from.length && agent.last_handoff_at) {
      events.push({ kind: 'Handoff', text: 'Received outputs from ' + agent.received_from.map(function(source) {
        return String(source.label || 'Agent');
      }).join(', '), at: Date.parse(agent.last_handoff_at) || 0 });
    }
    events.sort(function(a, b) { return b.at - a.at; });
    var event = events[0];
    var bubble = station.querySelector('.bridge-crew-bubble');
    if (!event || !isActive(agent.status) || stale) {
      station._bubbleUntil = 0;
      bubble.hidden = true;
      return;
    }
    var text = String(event.text).replace(/\s+/g, ' ').trim();
    var signature = JSON.stringify([event.kind, text, event.at]);
    if (signature !== station._bubbleSignature) {
      station._bubbleSignature = signature;
      station._bubbleUntil = Math.min(Date.now(), event.at || Date.now()) + 10000;
      bubble.textContent = event.kind + ': ' + (text.length > 110 ? text.slice(0, 107) + '...' : text);
      bubble.dataset.kind = event.kind.toLowerCase();
    }
    bubble.hidden = !(station._bubbleUntil > Date.now());
  }

  function renderFloor(agents) {
    agents = primaryAgentView(agents);
    var floor = document.getElementById('agentsFloorGroups');
    if (!floor) return;
    var stale = telemetryStale();
    var crew = agents.filter(function(agent) { return agent.kind !== 'eva'; });
    var activeCrew = crew.filter(function(agent) { return isActive(agent.status); });
    setText('bridgeCrewCount', crew.length + ' DELEGATED SESSION' + (crew.length === 1 ? '' : 'S'));
    setText('bridgeMission', stale ? 'Telemetry unavailable / last reported crew' :
      (activeCrew.length ? activeCrew.length + ' active / select a station to inspect' : 'Standing by / no active delegated tasks'));
    setText('bridgeActivity', stale ? 'Activity indicators paused until telemetry returns.' :
      (activeCrew.length ? activeCrew.map(function(agent) {
        return (agent.label || 'Agent') + ': ' + (agent.activity || statusLabel(agent.status));
      }).join('  /  ') : (agents.some(function(agent) { return agent.kind === 'eva'; })
        ? 'Eva is present on the bridge. Empty consoles are unassigned, not hidden agents.' : 'No agent sessions reported. Empty consoles are unassigned.')));
    var existingStations = {};
    Array.prototype.forEach.call(floor.querySelectorAll('.agent-workstation'), function(node) {
      existingStations[node.dataset.agentId] = node;
    });
    var empty = floor.querySelector('.agents-empty');
    if (!agents.length) {
      if (!empty) {
        empty = element('p', 'agents-empty');
        floor.appendChild(empty);
      }
      empty.textContent = state.telemetryError ? 'Runtime unavailable. No sessions can be verified.' : 'No agent sessions reported.';
    } else if (empty) empty.remove();
    agents.forEach(function(agent, stationIndex) {
      var station = existingStations[agent.id];
      if (!station) {
        station = element('button', 'agent-workstation');
        station.type = 'button';
        station.dataset.agentId = agent.id;
        var meta = element('span', 'bridge-agent-meta');
        meta.append(element('span', 'agent-station-activity'), element('span', 'agent-station-duration'));
        var bubble = element('span', 'bridge-crew-bubble');
        bubble.hidden = true;
        station.append(element('strong', 'agent-station-label'), element('span', 'agent-station-status'), meta, bubble);
        station.addEventListener('click', function(event) { openAgentDetail(event.currentTarget._agent); });
      }
      station._agent = agent;
      station.dataset.status = String(agent.status || 'unknown');
      station.dataset.kind = agent.kind || 'unknown';
      station.dataset.motion = !stale && agent.status === 'running' ? 'working' : 'still';
      station.setAttribute('aria-pressed', String(state.selectedId === agent.id));
      station.setAttribute('aria-label', 'Inspect ' + (agent.label || 'agent') + ': ' + agentStatus(agent));
      station.querySelector('.agent-station-label').textContent = agent.label || 'Agent session';
      station.querySelector('.agent-station-status').textContent = agentStatus(agent);
      station.querySelector('.agent-station-activity').textContent = agent.activity || agent.detail || 'No activity reported.';
      station.querySelector('.agent-station-duration').textContent = (agent.kind === 'eva' ? 'SESSION UPTIME ' : 'ELAPSED ') + elapsed(agent);
      updateCrewBubble(station, agent, stale);
      if (floor.children[stationIndex] !== station) floor.insertBefore(station, floor.children[stationIndex] || null);
      delete existingStations[agent.id];
    });
    Object.keys(existingStations).forEach(function(key) { existingStations[key].remove(); });
    if (typeof EvaBridgeScene !== 'undefined') EvaBridgeScene.update(agents, { stale: stale, selectedId: state.selectedId });
  }

  function renderCards(agents) {
    var grid = document.getElementById('agentsGrid');
    if (!grid) return;
    if (!agents.length) {
      if (grid.querySelector('.agents-empty') && grid.children.length === 1) return;
      grid.replaceChildren();
      var empty = document.createElement('div');
      empty.className = 'agents-empty';
      empty.innerHTML = '<strong>NO AGENT SESSIONS</strong><span>No sessions reported in this snapshot.</span>';
      grid.appendChild(empty);
      return;
    }
    var emptyState = grid.querySelector('.agents-empty');
    if (emptyState) emptyState.remove();
    var existing = {};
    Array.prototype.forEach.call(grid.children, function(child) {
      if (child.classList.contains('agent-card') && child.dataset.agentId) {
        existing[child.dataset.agentId] = child;
      }
    });
    agents.forEach(function(agent, index) {
      var card = existing[agent.id] || createAgentCard(agent, index);
      updateAgentCard(card, agent);
      delete existing[agent.id];
      var current = grid.children[index];
      if (current !== card) grid.insertBefore(card, current || null);
    });
    Object.keys(existing).forEach(function(agentId) { existing[agentId].remove(); });
  }

  function createAgentCard(agent, index) {
    var card = document.createElement('div');
    card.className = 'agent-card agent-card-enter';
    card.dataset.agentId = agent.id;
    card.setAttribute('role', 'button');
    card.tabIndex = 0;
    card.style.setProperty('--agent-index', index);
    card.innerHTML =
      '<span class="agent-card-head">' +
        '<span class="agent-kind" data-field="kind"></span>' +
        '<span class="agent-status"><i></i><span data-field="status"></span></span>' +
      '</span>' +
      '<button class="agent-card-dismiss" type="button" data-field="dismiss" title="Dismiss agent" aria-label="Dismiss agent">&times;</button>' +
      '<strong class="agent-card-title" data-field="title"></strong>' +
      '<span class="agent-card-model" data-field="model"></span>' +
      '<span class="agent-card-detail" data-field="detail"></span>' +
      '<span class="agent-card-signal" data-field="signal"></span>' +
      '<span class="agent-card-foot"><span data-field="duration"></span><span data-field="identifier"></span></span>';
    card.addEventListener('animationend', function() { card.classList.remove('agent-card-enter'); }, { once: true });
    card.addEventListener('click', function(event) {
      if (event.target.closest('.agent-card-dismiss')) return;
      openAgentDetail(card._agent);
    });
    card.addEventListener('keydown', function(event) {
      if ((event.key === 'Enter' || event.key === ' ') && !event.target.closest('.agent-card-dismiss')) {
        event.preventDefault();
        openAgentDetail(card._agent);
      }
    });
    card.querySelector('[data-field="dismiss"]').addEventListener('click', function(event) {
      event.stopPropagation();
      dismissAgent(card._agent, event.currentTarget);
    });
    return card;
  }

  function updateAgentCard(card, agent) {
    card._agent = agent;
    Array.prototype.slice.call(card.classList).forEach(function(name) {
      if (name.indexOf('status-') === 0) card.classList.remove(name);
    });
    card.classList.add('status-' + String(agent.status || 'unknown').replace(/[^a-z_]/g, ''));
    card.setAttribute('aria-label', 'Open ' + (agent.label || 'agent') + ' session');
    card.setAttribute('aria-pressed', String(state.selectedId === agent.id));
    card.querySelector('[data-field="kind"]').textContent = kindLabel(agent.kind);
    card.querySelector('[data-field="status"]').textContent = agentStatus(agent);
    card.querySelector('[data-field="title"]').textContent = agent.label || 'Agent session';
    var model = card.querySelector('[data-field="model"]');
    model.textContent = modelLabel(agent.model);
    model.hidden = !agent.model;
    card.querySelector('[data-field="detail"]').textContent = agent.detail || 'Waiting for runtime detail';
    var signal = card.querySelector('[data-field="signal"]');
    signal.className = 'agent-card-signal' + (agent.signal_status ? ' signal-' + agent.signal_status : '');
    signal.textContent = agent.signal_status ? 'SIGNAL ' + String(agent.signal_status).toUpperCase() : '';
    signal.hidden = !agent.signal_status;
    card.querySelector('[data-field="duration"]').textContent = elapsed(agent);
    card.querySelector('[data-field="identifier"]').textContent = agent.step ? 'STEP ' + agent.step : agent.id;
    var dismiss = card.querySelector('[data-field="dismiss"]');
    dismiss.hidden = agent.kind !== 'subagent' || ['done', 'error', 'cancelled'].indexOf(agent.status) === -1;
    dismiss.disabled = telemetryStale();
    dismiss.setAttribute('aria-label', 'Dismiss ' + (agent.label || 'agent'));
  }

  async function dismissAgent(agent, button) {
    if (!agent || agent.kind !== 'subagent') return;
    button.disabled = true;
    try {
      var response = await fetch(bridgeUrl() + '/v1/subagent/' + encodeURIComponent(agent.id), { method: 'DELETE' });
      var payload = await response.json().catch(function() { return {}; });
      if (!response.ok) throw new Error(payload.error && payload.error.message ? payload.error.message : 'Dismiss failed (' + response.status + ')');
      if (state.selectedId === agent.id) closeDetail();
      if (state.data && Array.isArray(state.data.agents)) {
        state.data.agents = state.data.agents.filter(function(item) { return item.id !== agent.id; });
      }
      var card = document.querySelector('.agent-card[data-agent-id="' + CSS.escape(agent.id) + '"]');
      if (card) card.remove();
      state.graphFetchedAt = 0;
      await refresh(true);
    } catch (error) {
      button.disabled = false;
      if (typeof setStatus === 'function') setStatus('error', error.message || String(error));
    }
  }

  function openAgentSession(agent) {
    agent = (state.data && state.data.agents || []).find(function(item) { return item.id === agent.id; });
    if (!agent) return;
    if (agent.session_id && typeof loadSession === 'function') {
      Promise.resolve(loadSession(agent.session_id)).then(function(loaded) {
        if (loaded) close();
        else openAgentDetail(agent, 'Linked chat session is unavailable.');
      });
      return;
    }
    openAgentDetail(agent);
  }

  function openAgentDetail(agent, message) {
    if (!agent) return;
    state.selectedId = agent.id;
    renderDetail(agent.id);
    renderFloor(state.data && state.data.agents || []);
    resizeCanvas();
    if (message) {
      var content = document.getElementById('agentDetailContent');
      var notice = document.createElement('p');
      notice.className = 'agent-detail-notice';
      notice.textContent = message;
      if (content) content.prepend(notice);
    }
  }

  function detailStatusText(agent) {
    return agentStatus(agent) + '  ' + elapsed(agent) + (agent.model ? '  ' + modelLabel(agent.model) : '') +
      (agent.signal_status ? '  SIGNAL ' + String(agent.signal_status).toUpperCase() : '');
  }

  function updateDetail(agent, content) {
    var status = content.querySelector('[data-agent-detail-status]');
    var activity = content.querySelector('[data-agent-detail-activity]');
    var result = content.querySelector('[data-agent-detail-result]');
    var objective = content.querySelector('[data-agent-detail-objective]');
    if (!status || !activity || !result) return false;
    status.className = 'agent-detail-status status-' + agent.status;
    status.textContent = detailStatusText(agent);
    activity.textContent = agent.activity || 'No activity reported.';
    result.textContent = agent.kind === 'security' && state.securityAnalysis
      ? state.securityAnalysis : agent.result || 'No output reported.';
    if (objective) objective.textContent = agent.detail || 'No objective detail reported.';
    var title = content.querySelector('h2');
    if (title) title.textContent = agent.label || 'Agent session';
    var steerInput = content.querySelector('#agentSteerInput');
    var steerButton = content.querySelector('.agent-steer-form button');
    var busy = state.actionBusy[agent.id];
    var blocked = telemetryStale() || busy || ['finalizing', 'cancelling'].indexOf(agent.status) !== -1;
    if (steerInput) steerInput.disabled = !!blocked;
    if (steerButton) {
      steerButton.disabled = !!blocked;
      steerButton.textContent = busy ? 'SENDING...' : (isActive(agent.status) ? 'SEND DIRECTION' : 'RESUME WITH DIRECTION');
    }
    var interrupt = content.querySelector('.agent-interrupt');
    if (interrupt) {
      interrupt.hidden = !agent.coding_run_id || !window.evaStandalone || typeof window.evaStandalone.workspaceAgentControl !== 'function';
      interrupt.disabled = telemetryStale() || !!busy || ['starting', 'waiting', 'running', 'steering'].indexOf(agent.status) === -1;
    }
    var session = content.querySelector('.agent-session-link');
    if (session) session.hidden = !agent.session_id || typeof loadSession !== 'function';
    return true;
  }

  function renderDetail(agentId) {
    var agents = primaryAgentView((state.data && state.data.agents) || []);
    var agent = agents.filter(function(item) { return item.id === agentId; })[0];
    var panel = document.getElementById('agentDetail');
    var content = document.getElementById('agentDetailContent');
    if (!panel || !content || !agent) return;
    if (content.dataset.agentId === agent.id && updateDetail(agent, content)) {
      panel.setAttribute('aria-hidden', 'false');
      return;
    }
    content.replaceChildren();
    content.dataset.agentId = agent.id;
    var kicker = document.createElement('div');
    kicker.className = 'agents-kicker';
    kicker.textContent = kindLabel(agent.kind) + ' / ' + agent.id;
    var title = document.createElement('h2');
    title.textContent = agent.label || 'Agent session';
    var status = document.createElement('div');
    status.className = 'agent-detail-status status-' + agent.status;
    status.dataset.agentDetailStatus = 'true';
    status.textContent = detailStatusText(agent);
    var promptLabel = document.createElement('h3');
    promptLabel.textContent = 'CURRENT OBJECTIVE';
    var prompt = document.createElement('p');
    prompt.dataset.agentDetailObjective = 'true';
    prompt.textContent = agent.detail || 'No objective detail reported.';
    var activityLabel = document.createElement('h3');
    activityLabel.textContent = 'LATEST ACTIVITY';
    var activity = document.createElement('p');
    activity.dataset.agentDetailActivity = 'true';
    activity.textContent = agent.activity || 'No activity reported.';
    var resultLabel = document.createElement('h3');
    resultLabel.textContent = 'LATEST OUTPUT';
    var result = document.createElement('pre');
    result.dataset.agentDetailResult = 'true';
    result.textContent = agent.result || 'No output reported.';
    content.append(kicker, title, status, promptLabel, prompt, activityLabel, activity, resultLabel, result);
    if (agent.kind === 'subagent') content.appendChild(buildSteerForm(agent));
    var actions = element('div', 'agent-inspector-actions');
    var session = element('button', 'agent-session-link', 'Open linked chat');
    session.type = 'button';
    session.addEventListener('click', function() { openAgentSession(agent); });
    actions.appendChild(session);
    if (agent.kind === 'subagent') {
      var interrupt = element('button', 'agent-interrupt', 'Interrupt this agent');
      interrupt.type = 'button';
      interrupt.addEventListener('click', function() { controlAgent(agent.id, 'stop'); });
      actions.appendChild(interrupt);
    }
    if (agent.kind === 'security') {
      var approve = element('button', 'agent-security-approve', 'Review local sandbox image');
      approve.type = 'button';
      approve.addEventListener('click', async function() {
        approve.disabled = true;
        var feedback = content.querySelector('.agent-inspector-feedback');
        try {
          var candidateResponse = await fetch(bridgeUrl() + '/v1/security/image', { headers: getBridgeCapabilityHeaders() });
          var candidate = await candidateResponse.json();
          if (!candidateResponse.ok || !candidate.image_id) throw new Error((candidate.error || {}).message || 'Image unavailable.');
          if (!window.confirm('Approve this local sandbox image for offline workspace runs?\n\nSHA-256: ' + candidate.image_id)) return;
          var response = await fetch(bridgeUrl() + '/v1/security/approve-image', {
            method: 'POST', headers: getBridgeCapabilityHeaders(), body: JSON.stringify({ image_id: candidate.image_id })
          });
          var result = await response.json();
          if (!response.ok) throw new Error((result.error || {}).message || 'Image approval failed.');
          if (feedback) feedback.textContent = 'Local sandbox image approved.';
          refresh(true);
        } catch (error) {
          if (feedback) feedback.textContent = error.message || 'Image approval failed.';
        } finally {
          approve.disabled = false;
        }
      });
      actions.appendChild(approve);
      var analyze = element('button', 'agent-security-analyze', 'Analyze recent security events');
      analyze.type = 'button';
      analyze.addEventListener('click', async function() {
        analyze.disabled = true;
        var feedback = content.querySelector('.agent-inspector-feedback');
        if (feedback) feedback.textContent = 'Analyzing sanitized event counts with the selected model...';
        var backend = String((document.getElementById('selAIGBackend') || {}).value || 'lmstudio');
        var local = backend === 'lmstudio';
        try {
          var response = await fetch(bridgeUrl() + '/v1/security/analyze', {
            method: 'POST',
            headers: getBridgeCapabilityHeaders(),
            body: JSON.stringify({
              model: backend,
              si_runtime: local && typeof getSIHarnessRuntime === 'function' ? getSIHarnessRuntime() : 'direct',
              lmstudio_base_url: local && typeof getLmStudioBaseUrl === 'function' ? getLmStudioBaseUrl() : '',
              lmstudio_model: local && typeof getLmStudioModel === 'function' ? getLmStudioModel() : '',
              openai_api_key: backend.indexOf('openai:') === 0 && typeof getAuthKey === 'function'
                ? getAuthKey('OPENAI_API_KEY') : ''
            })
          });
          var result = await response.json();
          if (!response.ok) throw new Error((result.error || {}).message || 'Security analysis failed.');
          var message = result.analysis;
          if (typeof message !== 'string' || !message.trim()) throw new Error('The selected model returned no analysis.');
          state.securityAnalysis = 'Model analysis (advisory, unverified):\n\n' + message.slice(0, 4000);
          updateDetail(agent, content);
          if (feedback) feedback.textContent = 'Analysis complete.';
        } catch (error) {
          if (feedback) feedback.textContent = error.message || 'Security analysis failed.';
        } finally {
          analyze.disabled = false;
        }
      });
      actions.appendChild(analyze);
      var test = element('button', 'agent-security-test', 'Test offline sandbox');
      test.type = 'button';
      test.addEventListener('click', async function() {
        test.disabled = true;
        var feedback = content.querySelector('.agent-inspector-feedback');
        if (feedback) feedback.textContent = 'Testing sandbox isolation...';
        try {
          var response = await fetch(bridgeUrl() + '/v1/security/self-test', {
            method: 'POST', headers: getBridgeCapabilityHeaders(), body: '{}'
          });
          var result = await response.json();
          if (!response.ok || !result.passed) throw new Error((result.error || {}).message || 'Sandbox test failed.');
          if (feedback) feedback.textContent = result.message;
        } catch (error) {
          if (feedback) feedback.textContent = error.message || 'Sandbox test failed.';
        } finally {
          test.disabled = false;
        }
      });
      actions.appendChild(test);
    }
    content.appendChild(actions);
    var dependencies = element('div', 'agent-dependencies');
    (agent.depends_on || []).forEach(function(id) {
      var dependency = agents.find(function(item) { return item.id === id; });
      var link = element('button', 'agent-dependency-link', 'Depends on: ' + (dependency && dependency.label || id));
      link.type = 'button';
      link.disabled = !dependency;
      if (dependency) link.addEventListener('click', function() { openAgentDetail(dependency); });
      dependencies.appendChild(link);
    });
    content.appendChild(dependencies);
    var feedback = element('p', 'agent-inspector-feedback');
    feedback.setAttribute('role', 'status');
    content.appendChild(feedback);
    updateDetail(agent, content);
    panel.setAttribute('aria-hidden', 'false');
  }

  async function controlAgent(agentId, action, instruction) {
    var agent = (state.data && state.data.agents || []).find(function(item) { return item.id === agentId; });
    if (!agent || telemetryStale() || state.actionBusy[agentId]) return false;
    if (agent.kind !== 'subagent' || ['finalizing', 'cancelling'].indexOf(agent.status) !== -1) return false;
    if (action === 'stop' && (!agent.coding_run_id || ['starting', 'waiting', 'running', 'steering'].indexOf(agent.status) === -1)) return false;
    if (action === 'steer' && (!String(instruction || '').trim() || String(instruction).length > 2000)) return false;
    var content = document.getElementById('agentDetailContent');
    state.actionBusy[agentId] = true;
    var pendingFeedback = content && content.dataset.agentId === agentId && content.querySelector('.agent-inspector-feedback');
    if (pendingFeedback) {
      pendingFeedback.dataset.error = 'false';
      pendingFeedback.textContent = action === 'stop' ? 'Requesting interruption...' : 'Sending direction...';
    }
    if (state.selectedId === agentId) renderDetail(agentId);
    try {
      var receipt;
      if (agent.coding_run_id && window.evaStandalone && typeof window.evaStandalone.workspaceAgentControl === 'function') {
        receipt = await window.evaStandalone.workspaceAgentControl(agent.coding_run_id, action, instruction || '');
      } else if (action === 'steer') {
        var response = await fetch(bridgeUrl() + '/v1/subagent/steer', {
          method: 'POST',
          headers: typeof getBridgeCapabilityHeaders === 'function' ? getBridgeCapabilityHeaders() : { 'Content-Type': 'application/json' },
          body: JSON.stringify({ id: agentId, instruction: instruction })
        });
        receipt = await response.json().catch(function() { return {}; });
        if (!response.ok) throw new Error(receipt.error && receipt.error.message || 'Steering failed (' + response.status + ')');
      } else throw new Error('This agent does not support interruption here.');
      if (!receipt || !receipt.task || receipt.task.id !== agentId) throw new Error('The runtime did not confirm this agent action.');
      var feedback = content && content.dataset.agentId === agentId && content.querySelector('.agent-inspector-feedback');
      if (feedback) {
        feedback.dataset.error = 'false';
        feedback.textContent = action === 'stop' ? 'Interruption accepted. Waiting for the reported stop.' :
          (receipt.queued ? 'Direction queued for this agent.' : 'Agent resumed with the accepted direction.');
      }
      await refresh();
      return true;
    } catch (error) {
      var notice = content && content.dataset.agentId === agentId && content.querySelector('.agent-inspector-feedback');
      if (notice) {
        notice.dataset.error = 'true';
        notice.textContent = error.message || String(error);
      }
      return false;
    } finally {
      delete state.actionBusy[agentId];
      if (state.selectedId === agentId) renderDetail(agentId);
    }
  }

  function buildSteerForm(agent) {
    var form = document.createElement('form');
    form.className = 'agent-steer-form';
    var label = document.createElement('label');
    label.htmlFor = 'agentSteerInput';
    label.textContent = 'STEER SESSION';
    var row = document.createElement('div');
    var input = document.createElement('input');
    input.id = 'agentSteerInput';
    input.type = 'text';
    input.maxLength = 2000;
    input.placeholder = 'Add direction';
    var button = document.createElement('button');
    button.type = 'submit';
    button.textContent = 'SEND';
    row.append(input, button);
    form.append(label, row);
    form.onsubmit = async function(event) {
      event.preventDefault();
      var instruction = input.value.trim();
      if (!instruction) return;
      if (await controlAgent(agent.id, 'steer', instruction)) input.value = '';
    };
    return form;
  }

  function closeDetail() {
    state.selectedId = '';
    var panel = document.getElementById('agentDetail');
    if (panel) panel.setAttribute('aria-hidden', 'true');
    renderFloor(state.data && state.data.agents || []);
    resizeCanvas();
  }

  function graphUnit(value) {
    var text = String(value || '');
    var hash = 2166136261;
    for (var index = 0; index < text.length; index++) {
      hash ^= text.charCodeAt(index);
      hash = Math.imul(hash, 16777619);
    }
    return (hash >>> 0) / 4294967295;
  }

  function graphHomes(nodes, edges) {
    var homes = { 'eva-root': { x: 0.5, y: 0.5 } };
    var entities = nodes.filter(function(node) { return node.type === 'entity'; });
    var agents = nodes.filter(function(node) { return node.type === 'agent'; });
    var children = {};
    edges.forEach(function(edge) {
      if (edge.type !== 'memory') return;
      children[edge.source] = children[edge.source] || [];
      children[edge.source].push(edge.target);
    });
    entities.forEach(function(node, index) {
      var angle = -Math.PI / 2 + (Math.PI * 2 * index / Math.max(entities.length, 1));
      var radius = 0.22 + graphUnit(node.id) * 0.08;
      homes[node.id] = { x: 0.5 + Math.cos(angle) * radius, y: 0.5 + Math.sin(angle) * radius };
    });
    agents.forEach(function(node, index) {
      var angle = -Math.PI / 2 + (Math.PI * 2 * index / Math.max(agents.length, 1));
      homes[node.id] = { x: 0.5 + Math.cos(angle) * 0.38, y: 0.5 + Math.sin(angle) * 0.31 };
    });
    Object.keys(children).forEach(function(parentId) {
      var parent = homes[parentId] || homes['eva-root'];
      children[parentId].forEach(function(childId, index) {
        var angle = graphUnit(childId) * Math.PI * 2 + index * 0.46;
        var radius = 0.07 + (index % 3) * 0.025;
        homes[childId] = {
          x: Math.max(0.06, Math.min(0.94, parent.x + Math.cos(angle) * radius)),
          y: Math.max(0.08, Math.min(0.92, parent.y + Math.sin(angle) * radius))
        };
      });
    });
    nodes.forEach(function(node) {
      if (homes[node.id]) return;
      var angle = graphUnit(node.id) * Math.PI * 2;
      homes[node.id] = { x: 0.5 + Math.cos(angle) * 0.18, y: 0.5 + Math.sin(angle) * 0.18 };
    });
    return homes;
  }

  function updateGraph(sourceNodes, sourceEdges) {
    var keep = selectGraphNodes(sourceNodes, 90);
    var allowed = {};
    keep.forEach(function(node) { allowed[node.id] = true; });
    var visibleEdges = sourceEdges.filter(function(edge) { return allowed[edge.source] && allowed[edge.target]; });
    var homes = graphHomes(keep, visibleEdges);
    var connectionCounts = {};
    visibleEdges.forEach(function(edge) {
      connectionCounts[edge.source] = (connectionCounts[edge.source] || 0) + 1;
      connectionCounts[edge.target] = (connectionCounts[edge.target] || 0) + 1;
    });
    var maximumConnections = Math.max.apply(Math, [1].concat(Object.keys(connectionCounts).map(function(id) {
      return connectionCounts[id];
    })));
    state.nodes = keep.map(function(raw, index) {
      var existing = state.nodeMap[raw.id];
      var home = homes[raw.id] || { x: 0.5, y: 0.5 };
      var confidence = raw.type === 'core' ? 1 : (raw.type === 'agent' ? 0.86 : Number(raw.confidence || 0.58));
      var influence = (connectionCounts[raw.id] || 0) / maximumConnections;
      var depth = Math.max(0.12, Math.min(1, confidence * 0.7 + influence * 0.3));
      if (existing) {
        Object.keys(raw).forEach(function(key) { existing[key] = raw[key]; });
        existing.homeX = home.x;
        existing.homeY = home.y;
        existing.depth = depth;
        existing.influence = influence;
        if (raw.id === 'eva-root') { existing.x = 0.5; existing.y = 0.5; existing.vx = 0; existing.vy = 0; }
        return existing;
      }
      var node = {
        id: raw.id,
        label: raw.label,
        type: raw.type,
        x: home.x,
        y: home.y,
        homeX: home.x,
        homeY: home.y,
        vx: 0,
        vy: 0,
        depth: depth,
        influence: influence,
        neuralPhase: graphUnit(raw.id + ':neural') * Math.PI * 2
      };
      Object.keys(raw).forEach(function(key) { node[key] = raw[key]; });
      state.nodeMap[raw.id] = node;
      return node;
    });
    state.edges = visibleEdges;
    if (state.focusNode && !allowed[state.focusNode.id]) state.focusNode = null;
    var empty = document.getElementById('agentGraphEmpty');
    if (empty) empty.hidden = state.nodes.length > 0;
  }

  function selectGraphNodes(sourceNodes, limit) {
    var agents = sourceNodes.filter(function(node) { return node.type === 'agent'; });
    var core = sourceNodes.filter(function(node) { return node.type === 'core'; });
    var memory = sourceNodes.filter(function(node) { return node.type !== 'agent' && node.type !== 'core'; });
    var priority = core.concat(agents);
    return priority.slice(0, limit).concat(memory.slice(0, Math.max(0, limit - priority.length)));
  }

  function resizeCanvas() {
    var canvas = document.getElementById('agentGraphCanvas');
    if (!canvas) return;
    var rect = canvas.getBoundingClientRect();
    var ratio = Math.min(window.devicePixelRatio || 1, 2);
    state.canvasWidth = Math.max(1, rect.width);
    state.canvasHeight = Math.max(1, rect.height);
    canvas.width = Math.round(state.canvasWidth * ratio);
    canvas.height = Math.round(state.canvasHeight * ratio);
    var context = canvas.getContext('2d');
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
  }

  function projectedNode(node, width, height) {
    var depth = Number(node.depth || 0.5);
    var scale = 0.72 + depth * 0.38;
    return {
      x: width / 2 + (node.x - 0.5) * width * scale + state.parallaxX * (depth - 0.35) * 34,
      y: height / 2 + (node.y - 0.5) * height * scale + state.parallaxY * (depth - 0.35) * 24,
      scale: scale,
      alpha: 0.3 + depth * 0.7
    };
  }

  function simulate() {
    var nodes = state.nodes;
    var width = state.canvasWidth;
    var height = state.canvasHeight;
    if (!width || !height) return;
    state.edges.forEach(function(edge) {
      var source = state.nodeMap[edge.source];
      var target = state.nodeMap[edge.target];
      if (!source || !target) return;
      var dx = target.x - source.x;
      var dy = target.y - source.y;
      var distance = Math.sqrt(dx * dx + dy * dy) || 0.01;
      var force = (distance - 0.16) * 0.0018;
      source.vx += dx / distance * force;
      source.vy += dy / distance * force;
      target.vx -= dx / distance * force;
      target.vy -= dy / distance * force;
    });
    for (var i = 0; i < nodes.length; i++) {
      for (var j = i + 1; j < nodes.length; j++) {
        var dx = nodes[j].x - nodes[i].x;
        var dy = nodes[j].y - nodes[i].y;
        var distanceSq = Math.max(dx * dx + dy * dy, 0.001);
        var repel = Math.min(0.00006 / distanceSq, 0.002);
        nodes[i].vx -= dx * repel;
        nodes[i].vy -= dy * repel;
        nodes[j].vx += dx * repel;
        nodes[j].vy += dy * repel;
      }
    }
    nodes.forEach(function(node) {
      if (node.id === 'eva-root') {
        node.x = 0.5; node.y = 0.5; node.vx = 0; node.vy = 0;
        return;
      }
      if (state.dragNode === node) return;
      node.vx += ((node.homeX === undefined ? 0.5 : node.homeX) - node.x) * 0.00055;
      node.vy += ((node.homeY === undefined ? 0.5 : node.homeY) - node.y) * 0.00055;
      node.vx *= 0.91;
      node.vy *= 0.91;
      var horizontalMargin = node.type === 'agent' || node.type === 'core' ? 100 : 55;
      node.x = Math.max(horizontalMargin / width, Math.min(1 - horizontalMargin / width, node.x + node.vx));
      node.y = Math.max(34 / height, Math.min(1 - 34 / height, node.y + node.vy));
    });
  }

  function drawGraph(time) {
    var canvas = document.getElementById('agentGraphCanvas');
    if (!canvas || !state.open) return;
    var context = canvas.getContext('2d');
    var width = state.canvasWidth;
    var height = state.canvasHeight;
    var reducedMotion = !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
    state.parallaxX += (state.parallaxTargetX - state.parallaxX) * (reducedMotion ? 1 : 0.055);
    state.parallaxY += (state.parallaxTargetY - state.parallaxY) * (reducedMotion ? 1 : 0.055);
    context.clearRect(0, 0, width, height);
    drawGrid(context, width, height, time);
    simulate();
    state.nodes.forEach(function(node) {
      var projected = projectedNode(node, width, height);
      node.renderX = projected.x;
      node.renderY = projected.y;
      node.renderScale = projected.scale;
      node.renderAlpha = projected.alpha;
    });
    state.nodes.filter(function(node) { return node.type === 'entity'; }).forEach(function(node) {
      var x = node.renderX;
      var y = node.renderY;
      context.beginPath();
      context.arc(x, y, (30 + node.depth * 16) + Math.sin(time * 0.0008 + node.neuralPhase) * 2, 0, Math.PI * 2);
      context.strokeStyle = 'rgba(196,160,255,' + (0.035 + node.depth * 0.075) + ')';
      context.lineWidth = 1;
      context.stroke();
    });
    state.edges.forEach(function(edge, index) {
      var source = state.nodeMap[edge.source];
      var target = state.nodeMap[edge.target];
      if (!source || !target) return;
      var sx = source.renderX;
      var sy = source.renderY;
      var tx = target.renderX;
      var ty = target.renderY;
      var focused = state.focusNode && (state.focusNode.id === source.id || state.focusNode.id === target.id);
      var bend = (graphUnit(edge.source + ':' + edge.target) - 0.5) * 42;
      var middleX = (sx + tx) / 2;
      var middleY = (sy + ty) / 2;
      var distance = Math.hypot(tx - sx, ty - sy) || 1;
      var controlX = middleX - (ty - sy) / distance * bend;
      var controlY = middleY + (tx - sx) / distance * bend;
      context.beginPath();
      context.moveTo(sx, sy);
      context.quadraticCurveTo(controlX, controlY, tx, ty);
      var dependency = edge.type === 'dependency';
      var orchestration = edge.type === 'orchestration';
      context.strokeStyle = dependency ? 'rgba(167,139,250,' + (focused ? '0.92' : '0.48') + ')' :
                (orchestration ? 'rgba(104,162,255,' + (focused ? '0.86' : '0.42') + ')' : 'rgba(104,235,215,' + (focused ? '0.7' : 0.1 + edge.confidence * 0.16) + ')');
      context.lineWidth = dependency ? (focused ? 2.4 : 1.7) : (orchestration ? (focused ? 2 : 1.3) : 0.6 + edge.confidence * 0.8 + (focused ? 0.8 : 0));
      context.globalAlpha = focused ? 1 : Math.min(source.renderAlpha, target.renderAlpha);
      context.stroke();
      context.globalAlpha = 1;
      if (edge.type === 'memory') {
        context.beginPath();
        context.arc(controlX, controlY, focused ? 2.5 : 1.4, 0, Math.PI * 2);
        context.fillStyle = focused ? 'rgba(197,255,246,0.88)' : 'rgba(104,235,215,0.24)';
        context.fill();
      }
      if (focused && edge.label) {
        context.font = '600 9px monospace';
        context.fillStyle = 'rgba(224,255,248,0.78)';
        context.textAlign = 'center';
        context.fillText(String(edge.label).replace(/_/g, ' '), controlX, controlY - 7);
        context.textAlign = 'left';
      }
    });
    state.nodes.slice().sort(function(left, right) { return left.depth - right.depth; }).forEach(function(node, index) {
      var x = node.renderX;
      var y = node.renderY;
      var entity = node.type === 'entity';
      var agent = node.type === 'agent';
      var core = node.type === 'core';
      var doneAgent = agent && node.status === 'done';
      var radius = (core ? 12 : (entity ? 7 : (agent ? 9 : 5))) * (0.72 + node.depth * 0.42);
      var pulse = reducedMotion || (agent && (telemetryStale() || node.status !== 'running'))
        ? 1 : 1 + Math.sin(time * 0.0014 + index) * 0.1;
      var color = core ? '#ffcb82' : (entity ? '#c4a0ff' : (doneAgent ? '#76ead9' : (agent ? '#68a2ff' : (node.review_required ? '#ffbd66' : '#68ebd7'))));
      var haloColor = core ? 'rgba(255,203,130,0.28)' : (entity ? 'rgba(196,160,255,0.28)' : (agent ? 'rgba(104,162,255,0.28)' : 'rgba(104,235,215,0.28)'));
      context.globalAlpha = node.renderAlpha;
      context.beginPath();
      context.arc(x, y, radius * 2.5 * pulse, 0, Math.PI * 2);
      context.fillStyle = core ? 'rgba(255,203,130,0.12)' : (entity ? 'rgba(196,160,255,0.1)' : (agent ? 'rgba(104,162,255,0.1)' : 'rgba(104,235,215,0.07)'));
      context.fill();
      context.save();
      context.shadowColor = color;
      context.shadowBlur = state.focusNode === node || state.hoverNode === node ? 18 : 9;
      context.beginPath();
      if (agent && !doneAgent) {
        context.roundRect(x - radius, y - radius, radius * 2, radius * 2, 3);
      } else if (!core && !entity && !agent) {
        context.roundRect(x - radius, y - radius, radius * 2, radius * 2, 3);
      } else {
        context.arc(x, y, radius, 0, Math.PI * 2);
      }
      context.fillStyle = color;
      context.fill();
      context.shadowBlur = 0;
      context.strokeStyle = 'rgba(240,255,252,0.7)';
      context.lineWidth = state.focusNode === node ? 2 : 1;
      context.stroke();
      context.restore();
      context.globalAlpha = 1;
      if (state.focusNode === node || state.hoverNode === node) {
        context.beginPath();
        context.arc(x, y, radius + 7, 0, Math.PI * 2);
        context.strokeStyle = haloColor;
        context.lineWidth = 1;
        context.stroke();
      }
      if (core) {
        context.beginPath();
        context.arc(x, y, radius + 7, 0, Math.PI * 2);
        context.strokeStyle = 'rgba(255,203,130,0.3)';
        context.lineWidth = 1;
        context.stroke();
      }
      if (doneAgent) {
        context.font = '700 9px monospace'; context.fillStyle = '#07100f'; context.textAlign = 'center';
        context.fillText('✓', x, y + 3); context.textAlign = 'left';
      }
      if (!core && (agent || state.hoverNode === node || state.focusNode === node || (entity && node.depth > 0.6))) {
        var statusSuffix = agent ? ' · ' + statusLabel(node.status) : '';
        var label = (node.label || node.id).slice(0, 24) + statusSuffix;
        context.font = (entity || agent) ? '600 10px monospace' : '10px monospace';
        var labelWidth = context.measureText(label).width;
        var labelX = x + radius + 9;
        if (labelX + labelWidth + 14 > width - 8) labelX = x - radius - 9 - labelWidth - 14;
        context.fillStyle = 'rgba(7,20,24,0.86)';
        context.beginPath();
        context.roundRect(labelX, y - 10, labelWidth + 14, 20, 7);
        context.fill();
        context.strokeStyle = 'rgba(132,240,224,0.12)';
        context.stroke();
        context.fillStyle = agent ? 'rgba(218,231,255,0.95)' : 'rgba(226,255,251,0.92)';
        context.fillText(label, labelX + 7, y + 3);
      }
      if (core) {
        context.font = '700 9px monospace';
        context.fillStyle = 'rgba(255,235,201,0.9)';
        context.textAlign = 'center';
        context.fillText('EVA', x, y - radius - 13);
        context.textAlign = 'left';
      }
    });
    state.animationFrame = requestAnimationFrame(drawGraph);
  }

  function drawGrid(context, width, height, time) {
    context.save();
    context.fillStyle = 'rgba(132,240,224,0.055)';
    var offset = 18;
    for (var x = offset; x < width; x += 32) {
      for (var y = offset; y < height; y += 32) {
        context.fillRect(x, y, 1, 1);
      }
    }
    context.restore();
  }

  function pointerPosition(event) {
    var canvas = document.getElementById('agentGraphCanvas');
    var rect = canvas.getBoundingClientRect();
    return { x: event.clientX - rect.left, y: event.clientY - rect.top };
  }

  function nearestNode(position) {
    var nearest = null;
    var best = 18 * 18;
    state.nodes.forEach(function(node) {
      var dx = (node.renderX === undefined ? node.x * state.canvasWidth : node.renderX) - position.x;
      var dy = (node.renderY === undefined ? node.y * state.canvasHeight : node.renderY) - position.y;
      var distance = dx * dx + dy * dy;
      if (distance < best) { best = distance; nearest = node; }
    });
    return nearest;
  }

  function bindGraphEvents() {
    var canvas = document.getElementById('agentGraphCanvas');
    if (!canvas || canvas.dataset.bound) return;
    canvas.dataset.bound = 'true';
    canvas.addEventListener('pointermove', function(event) {
      var position = pointerPosition(event);
      state.parallaxTargetX = (position.x / Math.max(state.canvasWidth, 1) - 0.5) * 2;
      state.parallaxTargetY = (position.y / Math.max(state.canvasHeight, 1) - 0.5) * 2;
      if (state.dragNode) {
        state.dragNode.x = position.x / state.canvasWidth;
        state.dragNode.y = position.y / state.canvasHeight;
        state.dragNode.vx = 0;
        state.dragNode.vy = 0;
      }
      state.hoverNode = nearestNode(position);
      canvas.style.cursor = state.hoverNode ? 'grab' : 'default';
      var tooltip = document.getElementById('agentGraphTooltip');
      if (tooltip && state.hoverNode) {
        tooltip.textContent = graphTooltipText(state.hoverNode);
        tooltip.style.transform = 'translate(' + Math.max(8, Math.min(position.x + 14, state.canvasWidth - 330)) + 'px,' + Math.max(8, Math.min(position.y - 34, state.canvasHeight - 130)) + 'px)';
        tooltip.setAttribute('aria-hidden', 'false');
      } else if (tooltip) {
        tooltip.setAttribute('aria-hidden', 'true');
      }
    });
    canvas.addEventListener('pointerdown', function(event) {
      state.dragNode = nearestNode(pointerPosition(event));
      state.graphPointerStart = pointerPosition(event);
      if (state.dragNode) canvas.setPointerCapture(event.pointerId);
    });
    canvas.addEventListener('pointerup', function(event) {
      var node = state.dragNode;
      var start = state.graphPointerStart;
      var end = pointerPosition(event);
      if (node && start && Math.hypot(end.x - start.x, end.y - start.y) < 5) {
        state.focusNode = state.focusNode === node ? null : node;
        if (node.type === 'fact' && node.memory_id && window.EvaMemoryInspector &&
            typeof window.EvaMemoryInspector.openAtomById === 'function') {
          window.EvaMemoryInspector.openAtomById(node.memory_id);
        }
      }
      state.dragNode = null;
      state.graphPointerStart = null;
    });
    canvas.addEventListener('pointerleave', function() {
      state.dragNode = null;
      state.graphPointerStart = null;
      state.hoverNode = null;
      state.parallaxTargetX = 0;
      state.parallaxTargetY = 0;
      var tooltip = document.getElementById('agentGraphTooltip');
      if (tooltip) tooltip.setAttribute('aria-hidden', 'true');
    });
  }

  function graphTooltipText(node) {
    var connected = state.edges.filter(function(edge) { return edge.source === node.id || edge.target === node.id; });
    var lines = [node.label || node.id];
    if (node.type === 'core') lines.push('Eva cognitive root · orchestrates agent sessions');
    else if (node.type === 'agent') {
      lines.push('Agent session · ' + agentStatus(node));
      lines.push('Model: ' + (node.model || 'default'));
      if (node.result) lines.push('Latest: ' + String(node.result).replace(/\s+/g, ' ').slice(0, 150));
    } else if (node.type === 'fact') {
      lines.push((node.source_label || 'Memory') + ' → ' + (node.relation || 'related fact'));
      lines.push(node.full_label || node.label || '');
      if (node.context) lines.push('Context: ' + node.context);
      if (node.confidence) lines.push('Confidence: ' + Math.round(node.confidence * 100) + '%');
      lines.push('Neural influence: ' + Math.round((node.influence || 0) * 100) + '%');
      if (node.trust) lines.push('Trust: ' + String(node.trust).replace(/_/g, ' '));
      lines.push(node.memory_id ? 'Select to inspect or edit this record.' : 'Imported legacy fact · review in the full Memory workspace.');
    } else {
      lines.push(node.description || 'Remembered entity');
    }
    var orchestrates = [];
    var orchestratedBy = [];
    var feeds = [];
    var receives = [];
    var memoryLinks = 0;
    connected.forEach(function(edge) {
      var otherId = edge.source === node.id ? edge.target : edge.source;
      var other = state.nodeMap[otherId];
      var otherLabel = other ? (other.label || other.id) : otherId;
      if (edge.type === 'orchestration') {
        if (edge.source === node.id) orchestrates.push(otherLabel);
        else orchestratedBy.push(otherLabel);
      } else if (edge.type === 'dependency') {
        if (edge.source === node.id) feeds.push(otherLabel);
        else receives.push(otherLabel);
      } else {
        memoryLinks++;
      }
    });
    if (orchestrates.length) lines.push('Orchestrates: ' + orchestrates.join(', '));
    if (orchestratedBy.length) lines.push('Orchestrated by: ' + orchestratedBy.join(', '));
    if (feeds.length) lines.push('Feeds: ' + feeds.join(', '));
    if (receives.length) lines.push('Receives from: ' + receives.join(', '));
    if (memoryLinks) lines.push('Memory links: ' + memoryLinks);
    return lines.join('\n');
  }

  function startGraph() {
    if (!state.open || state.view !== 'memory') return;
    resizeCanvas();
    bindGraphEvents();
    window.addEventListener('resize', resizeCanvas);
    if (!state.animationFrame) state.animationFrame = requestAnimationFrame(drawGraph);
  }

  function stopGraph() {
    window.removeEventListener('resize', resizeCanvas);
    if (state.animationFrame) cancelAnimationFrame(state.animationFrame);
    state.animationFrame = null;
  }

  function init() {
    var refreshButton = document.getElementById('agentsRefreshBtn');
    var closeButton = document.getElementById('agentsCloseBtn');
    var detailClose = document.getElementById('agentDetailClose');
    var memoryReviewButton = document.getElementById('agentGraphMemoryReview');
    var clearMissionLog = document.getElementById('missionLogClear');
    var clearSecurityLog = document.getElementById('securityLogClear');
    var clearSecurityUrls = document.getElementById('securityUrlClear');
    var allowMissionResearch = document.getElementById('missionLogAllowResearch');
    var denyMissionResearch = document.getElementById('missionLogDenyResearch');
    var enableSecurityUrls = document.getElementById('securityUrlEnable');
    var disableSecurityUrls = document.getElementById('securityUrlDisable');
    var workbench = document.getElementById('workspaceWorkbench');
    var workspacesHost = document.getElementById('agentsWorkspaces');
    if (workbench && workspacesHost) workspacesHost.appendChild(workbench);
    try { state.view = localStorage.getItem('evaAgentObservatoryView') || 'floor'; } catch (_) {}
    setView(state.view);
    var tabIds = ['agentsFloorTab', 'agentsMemoryTab', 'agentsCardsTab', 'agentsWorkspacesTab', 'agentsReportsTab', 'agentsSecurityTab'];
    var viewNames = ['floor', 'memory', 'cards', 'workspaces', 'reports', 'security'];
    tabIds.forEach(function(id, index) {
      var tab = document.getElementById(id);
      if (!tab) return;
      tab.addEventListener('click', function() { setView(viewNames[index]); refresh(state.view === 'memory'); });
      tab.addEventListener('keydown', function(event) {
        var next;
        if (event.key === 'ArrowRight') next = (index + 1) % tabIds.length;
        else if (event.key === 'ArrowLeft') next = (index + tabIds.length - 1) % tabIds.length;
        else if (event.key === 'Home') next = 0;
        else if (event.key === 'End') next = tabIds.length - 1;
        else return;
        event.preventDefault();
        document.getElementById(tabIds[next]).click();
        document.getElementById(tabIds[next]).focus();
      });
    });
    if (refreshButton) refreshButton.addEventListener('click', function() { refresh(true); });
    if (closeButton) closeButton.addEventListener('click', close);
    if (detailClose) detailClose.addEventListener('click', closeDetail);
    if (clearMissionLog) clearMissionLog.addEventListener('click', function() { deleteMissionLogEntry('reports', clearMissionLog); });
    if (clearSecurityLog) clearSecurityLog.addEventListener('click', function() { deleteMissionLogEntry('security', clearSecurityLog); });
    if (clearSecurityUrls) clearSecurityUrls.addEventListener('click', function() { deleteMissionLogEntry('urls', clearSecurityUrls); });
    [
      ['securityUrlPrevious', 'securityUrlOffset', -100], ['securityUrlNext', 'securityUrlOffset', 100],
      ['securityBlockPrevious', 'securityBlockOffset', -100], ['securityBlockNext', 'securityBlockOffset', 100]
    ].forEach(function(item) {
      var button = document.getElementById(item[0]);
      if (button) button.addEventListener('click', function() {
        state[item[1]] = Math.max(0, state[item[1]] + item[2]);
        refreshSecurity();
      });
    });
    if (allowMissionResearch) allowMissionResearch.addEventListener('click', function() { setMissionResearchPermission(true); });
    if (denyMissionResearch) denyMissionResearch.addEventListener('click', function() { setMissionResearchPermission(false); });
    if (enableSecurityUrls) enableSecurityUrls.addEventListener('click', function() { setSecurityUrlLogging(true); });
    if (disableSecurityUrls) disableSecurityUrls.addEventListener('click', function() { setSecurityUrlLogging(false); });
    document.getElementById('sitrailsEnable').addEventListener('click', function() { setSitrailsMonitoring(true); });
    document.getElementById('sitrailsDisable').addEventListener('click', function() { setSitrailsMonitoring(false); });
    document.getElementById('sitrailsClear').addEventListener('click', function() { clearSitrailsObservations(); });
    if (memoryReviewButton) memoryReviewButton.addEventListener('click', function() {
      if (window.EvaMemoryInspector && typeof window.EvaMemoryInspector.openFromAgents === 'function') {
        window.EvaMemoryInspector.openFromAgents();
      }
    });
    document.addEventListener('keydown', function(event) {
      if (event.key === 'Escape' && state.open) {
        if (state.selectedId) closeDetail(); else close();
      }
    });
    refresh();
  }

  function openAgent(agentId) {
    var refreshPromise = state.open ? refreshAndSchedule() : open();
    return Promise.resolve(refreshPromise).then(function() {
      var agent = ((state.data && state.data.agents) || []).filter(function(item) { return item.id === agentId; })[0];
      if (agent) openAgentDetail(agent);
    });
  }

  function primaryAgentView(agents) {
    if (!state.primaryTurns.size) return agents;
    var activity = Array.from(state.primaryTurns.values()).at(-1);
    return agents.map(function(agent) {
      return agent.kind === 'eva' ? Object.assign({}, agent, {
        status: 'running', activity: 'Processing current chat request',
        last_activity_at: activity.started_at, model: activity.model || agent.model
      }) : agent;
    });
  }

  function beginPrimaryTurn() {
    var identifier = ++state.primaryTurnSequence;
    var backend = String((document.getElementById('selAIGBackend') || {}).value || '');
    state.primaryTurns.set(identifier, {
      started_at: new Date().toISOString(),
      model: backend === 'lmstudio' && typeof getLmStudioModel === 'function' ? getLmStudioModel() : backend
    });
    if (state.data) render();
    return identifier;
  }

  function endPrimaryTurn(identifier) {
    state.primaryTurns.delete(identifier);
    if (state.data) render();
    if (state.open) refresh();
  }

  document.addEventListener('DOMContentLoaded', init);
  function invalidateGraph() {
    state.graphFetchedAt = 0;
    if (state.open) refresh(true);
  }

  async function describe() {
    await refresh();
    var data = state.data || {};
    var agents = primaryAgentView(Array.isArray(data.agents) ? data.agents : []);
    var active = agents.filter(function(agent) { return isActive(agent.status); });
    if (!agents.length) return 'There are no active or recent agent sessions right now.';
    var labels = active.slice(0, 6).map(function(agent) {
      return (agent.label || 'Agent session') + ' (' + statusLabel(agent.status) + ')';
    });
    return 'There ' + (active.length === 1 ? 'is 1 active agent' : 'are ' + active.length + ' active agents') +
      ' out of ' + agents.length + ' recent session' + (agents.length === 1 ? '' : 's') +
      (labels.length ? ': ' + labels.join(', ') + '.' : '.');
  }

  return {
    open: open,
    beginPrimaryTurn: beginPrimaryTurn,
    endPrimaryTurn: endPrimaryTurn,
    openWorkspace: openWorkspace,
    openWorkspaces: openWorkspaces,
    close: close,
    toggle: toggle,
    refresh: refresh,
    describe: describe,
    openAgent: openAgent,
    invalidateGraph: invalidateGraph,
    _selectGraphNodes: selectGraphNodes
  };
})();