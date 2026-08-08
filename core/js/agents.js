// Agent Operations dashboard: live agent sessions and memory graph topology.

var EvaAgents = (function() {
  var AGENT_ACTIVE_POLL_MS = 2000;
  var AGENT_IDLE_POLL_MS = 20000;
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
    canvasWidth: 0,
    canvasHeight: 0,
    lastFrame: 0,
    refreshController: null,
    refreshSequence: 0,
    graphFetchedAt: 0
  };

  function bridgeUrl() {
    var value = (typeof getACPBridgeUrl === 'function') ? getACPBridgeUrl() : 'http://localhost:8888';
    return String(value || '').replace(/\/+$/, '');
  }

  function isActive(status) {
    return ['starting', 'waiting', 'running', 'steering', 'finalizing', 'awaiting_confirmation', 'awaiting_input'].indexOf(status) !== -1;
  }

  function statusLabel(status) {
    return String(status || 'unknown').replace(/_/g, ' ').toUpperCase();
  }

  function kindLabel(kind) {
    var labels = { subagent: 'ACP SUBAGENT', browser: 'BROWSER', desktop: 'DESKTOP', background: 'BACKGROUND' };
    return labels[kind] || String(kind || 'AGENT').toUpperCase();
  }

  function elapsed(agent) {
    var start = Date.parse(agent.started_at || '');
    var end = Date.parse(agent.ended_at || '') || Date.now();
    if (!start) return '--';
    var seconds = Math.max(0, Math.round((end - start) / 1000));
    if (seconds < 60) return seconds + 's';
    var minutes = Math.floor(seconds / 60);
    return minutes < 60 ? minutes + 'm ' + (seconds % 60) + 's' : Math.floor(minutes / 60) + 'h ' + (minutes % 60) + 'm';
  }

  function open() {
    if (state.open) return Promise.resolve();
    if (typeof closeVoiceView === 'function' && typeof _vv !== 'undefined' && _vv.open) closeVoiceView();
    closeSidePanels();
    state.open = true;
    document.body.classList.add('agents-view-open');
    var view = document.getElementById('agentsView');
    var button = document.getElementById('evaAgentsBtn');
    if (view) view.setAttribute('aria-hidden', 'false');
    if (button) button.classList.add('active');
    var refreshPromise = refreshAndSchedule();
    startGraph();
    return refreshPromise;
  }

  function close() {
    state.open = false;
    document.body.classList.remove('agents-view-open');
    var view = document.getElementById('agentsView');
    var button = document.getElementById('evaAgentsBtn');
    if (view) view.setAttribute('aria-hidden', 'true');
    if (button) button.classList.remove('active');
    if (state.pollTimer) clearTimeout(state.pollTimer);
    state.pollTimer = null;
    state.refreshSequence++;
    if (state.refreshController) state.refreshController.abort();
    state.refreshController = null;
    stopGraph();
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
    if (state.open) close(); else open();
  }

  async function refresh(forceGraph) {
    var sequence = ++state.refreshSequence;
    if (state.refreshController) state.refreshController.abort();
    state.refreshController = new AbortController();
    try {
      var includeGraph = forceGraph === true || !state.data || !state.data.graph || Date.now() - state.graphFetchedAt >= 30000;
      var response = await fetch(bridgeUrl() + '/v1/agents/overview?include_graph=' + (includeGraph ? '1' : '0'), { signal: state.refreshController.signal });
      if (!response.ok) throw new Error('Bridge returned ' + response.status);
      var data = await response.json();
      if (sequence !== state.refreshSequence) return;
      if (data.graph) state.graphFetchedAt = Date.now();
      else if (state.data && state.data.graph) data.graph = state.data.graph;
      state.data = data;
      render();
    } catch (error) {
      if (error && error.name === 'AbortError') return;
      renderUnavailable(error.message || String(error));
    }
  }

  function renderUnavailable(message) {
    var grid = document.getElementById('agentsGrid');
    var updated = document.getElementById('agentsUpdatedAt');
    if (updated) updated.textContent = 'BRIDGE OFFLINE';
    if (grid && !grid.children.length) {
      var empty = document.createElement('div');
      empty.className = 'agents-empty';
      empty.textContent = message;
      grid.appendChild(empty);
    }
  }

  function render() {
    var data = state.data || {};
    var agents = data.agents || [];
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
    setText('agentsActiveCount', data.active_total || 0);
    setText('agentsCapacity', (data.subagents_active || 0) + ' / ' + (data.capacity || 4));
    setText('agentsNodeCount', (graph.nodes || []).length);
    setText('agentGraphEdgeCount', (graph.edges || []).length + ' relations');
    setText('agentsBackgroundState', data.background && data.background.running ? 'ONLINE' : 'IDLE');
    setText('agentsUpdatedAt', 'SYNC ' + new Date(data.generated_at || Date.now()).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }));
    var badge = document.getElementById('evaAgentsBadge');
    if (badge) {
      badge.textContent = data.active_total || 0;
      badge.classList.toggle('active', (data.active_total || 0) > 0);
    }
    renderCards(agents);
    updateGraph(graph.nodes || [], graph.edges || []);
    if (state.selectedId) renderDetail(state.selectedId);
  }

  function setText(id, value) {
    var element = document.getElementById(id);
    if (element) element.textContent = value;
  }

  function renderCards(agents) {
    var grid = document.getElementById('agentsGrid');
    if (!grid) return;
    if (!agents.length) {
      if (grid.querySelector('.agents-empty') && grid.children.length === 1) return;
      grid.replaceChildren();
      var empty = document.createElement('div');
      empty.className = 'agents-empty';
      empty.innerHTML = '<strong>NO AGENT SESSIONS</strong><span>Runtime is ready.</span>';
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
      openAgentSession(card._agent);
    });
    card.addEventListener('keydown', function(event) {
      if ((event.key === 'Enter' || event.key === ' ') && !event.target.closest('.agent-card-dismiss')) {
        event.preventDefault();
        openAgentSession(card._agent);
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
    card.querySelector('[data-field="kind"]').textContent = kindLabel(agent.kind);
    card.querySelector('[data-field="status"]').textContent = statusLabel(agent.status);
    card.querySelector('[data-field="title"]').textContent = agent.label || 'Agent session';
    var model = card.querySelector('[data-field="model"]');
    model.textContent = agent.model || '';
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
    state.selectedId = agent.id;
    renderDetail(agent.id);
    if (message) {
      var content = document.getElementById('agentDetailContent');
      var notice = document.createElement('p');
      notice.className = 'agent-detail-notice';
      notice.textContent = message;
      if (content) content.prepend(notice);
    }
  }

  function detailStatusText(agent) {
    return statusLabel(agent.status) + '  ' + elapsed(agent) + (agent.model ? '  ' + agent.model : '') +
      (agent.signal_status ? '  SIGNAL ' + String(agent.signal_status).toUpperCase() : '');
  }

  function updateDetail(agent, content) {
    var status = content.querySelector('[data-agent-detail-status]');
    var activity = content.querySelector('[data-agent-detail-activity]');
    var result = content.querySelector('[data-agent-detail-result]');
    if (!status || !activity || !result) return false;
    status.className = 'agent-detail-status status-' + agent.status;
    status.textContent = detailStatusText(agent);
    activity.textContent = agent.activity || (isActive(agent.status) ? 'Working...' : 'No activity reported.');
    result.textContent = agent.result || (isActive(agent.status) ? 'Agent is working...' : 'No output reported.');
    return true;
  }

  function renderDetail(agentId) {
    var agents = (state.data && state.data.agents) || [];
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
    prompt.textContent = agent.detail || 'No objective detail reported.';
    var activityLabel = document.createElement('h3');
    activityLabel.textContent = 'LATEST ACTIVITY';
    var activity = document.createElement('p');
    activity.dataset.agentDetailActivity = 'true';
    activity.textContent = agent.activity || (isActive(agent.status) ? 'Working...' : 'No activity reported.');
    var resultLabel = document.createElement('h3');
    resultLabel.textContent = 'LATEST OUTPUT';
    var result = document.createElement('pre');
    result.dataset.agentDetailResult = 'true';
    result.textContent = agent.result || (isActive(agent.status) ? 'Agent is working...' : 'No output reported.');
    content.append(kicker, title, status, promptLabel, prompt, activityLabel, activity, resultLabel, result);
    if (agent.kind === 'subagent') content.appendChild(buildSteerForm(agent));
    panel.setAttribute('aria-hidden', 'false');
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
      button.disabled = true;
      try {
        var response = await fetch(bridgeUrl() + '/v1/subagent/steer', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ id: agent.id, instruction: instruction })
        });
        if (!response.ok) throw new Error('Steering failed (' + response.status + ')');
        input.value = '';
        await refresh();
      } catch (error) {
        input.value = error.message || String(error);
      } finally {
        button.disabled = false;
      }
    };
    return form;
  }

  function closeDetail() {
    state.selectedId = '';
    var panel = document.getElementById('agentDetail');
    if (panel) panel.setAttribute('aria-hidden', 'true');
  }

  function updateGraph(sourceNodes, sourceEdges) {
    var keep = selectGraphNodes(sourceNodes, 90);
    var allowed = {};
    var agentNodes = keep.filter(function(node) { return node.type === 'agent'; });
    var agentPositions = {};
    agentNodes.forEach(function(node, index) {
      var angle = -Math.PI / 2 + (Math.PI * 2 * index / Math.max(agentNodes.length, 1));
      agentPositions[node.id] = { x: 0.5 + Math.cos(angle) * 0.28, y: 0.5 + Math.sin(angle) * 0.3 };
    });
    state.nodes = keep.map(function(raw, index) {
      allowed[raw.id] = true;
      var existing = state.nodeMap[raw.id];
      if (existing) {
        Object.keys(raw).forEach(function(key) { existing[key] = raw[key]; });
        if (raw.id === 'eva-root') { existing.x = 0.5; existing.y = 0.5; existing.vx = 0; existing.vy = 0; }
        return existing;
      }
      var angle = index * 2.39996;
      var initial = raw.id === 'eva-root' ? { x: 0.5, y: 0.5 } :
                    (agentPositions[raw.id] || { x: 0.5 + Math.cos(angle) * 0.22, y: 0.5 + Math.sin(angle) * 0.34 });
      var node = {
        id: raw.id,
        label: raw.label,
        type: raw.type,
        x: initial.x,
        y: initial.y,
        vx: 0,
        vy: 0
      };
      Object.keys(raw).forEach(function(key) { node[key] = raw[key]; });
      state.nodeMap[raw.id] = node;
      return node;
    });
    state.edges = sourceEdges.filter(function(edge) { return allowed[edge.source] && allowed[edge.target]; });
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
      node.vx += (0.5 - node.x) * 0.00012;
      node.vy += (0.5 - node.y) * 0.00012;
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
    context.clearRect(0, 0, width, height);
    drawGrid(context, width, height, time);
    simulate();
    state.edges.forEach(function(edge, index) {
      var source = state.nodeMap[edge.source];
      var target = state.nodeMap[edge.target];
      if (!source || !target) return;
      var sx = source.x * width;
      var sy = source.y * height;
      var tx = target.x * width;
      var ty = target.y * height;
      context.beginPath();
      context.moveTo(sx, sy);
      context.lineTo(tx, ty);
      var dependency = edge.type === 'dependency';
      var orchestration = edge.type === 'orchestration';
      context.strokeStyle = dependency ? 'rgba(167, 139, 250, 0.62)' :
                            (orchestration ? 'rgba(91, 154, 255, 0.5)' : 'rgba(74, 222, 199, ' + (0.12 + edge.confidence * 0.2) + ')');
      context.lineWidth = dependency ? 2 : (orchestration ? 1.4 : 0.7 + edge.confidence);
      context.stroke();
      if (!target.status || isActive(target.status)) {
        var progress = ((time * 0.00012) + index * 0.173) % 1;
        context.beginPath();
        context.arc(sx + (tx - sx) * progress, sy + (ty - sy) * progress, 1.7, 0, Math.PI * 2);
        context.fillStyle = dependency ? 'rgba(196, 181, 253, 0.9)' : 'rgba(137, 255, 234, 0.82)';
        context.fill();
      }
    });
    state.nodes.forEach(function(node, index) {
      var x = node.x * width;
      var y = node.y * height;
      var entity = node.type === 'entity';
      var agent = node.type === 'agent';
      var core = node.type === 'core';
      var doneAgent = agent && node.status === 'done';
      var radius = core ? 9 : (entity ? 5.5 : (agent ? 7 : 3.4));
      var pulse = 1 + Math.sin(time * 0.002 + index) * 0.18;
      context.beginPath();
      context.arc(x, y, radius * 2.4 * pulse, 0, Math.PI * 2);
      context.fillStyle = core ? 'rgba(255, 190, 92, 0.16)' : (entity ? 'rgba(255, 190, 92, 0.07)' : (agent ? 'rgba(91, 154, 255, 0.11)' : 'rgba(74, 222, 199, 0.055)'));
      context.fill();
      context.beginPath();
      if (agent && !doneAgent) {
        context.rect(x - radius, y - radius, radius * 2, radius * 2);
      } else {
        context.arc(x, y, radius, 0, Math.PI * 2);
      }
      context.fillStyle = core ? '#ffbe5c' : (entity ? '#ffbe5c' : (doneAgent ? '#4adec7' : (agent ? '#5b9aff' : '#4adec7')));
      context.fill();
      context.strokeStyle = (core || entity) ? 'rgba(255,224,168,0.8)' : (agent ? 'rgba(195,218,255,0.9)' : 'rgba(184,255,245,0.72)');
      context.lineWidth = 1;
      context.stroke();
      if (core) {
        context.beginPath();
        context.arc(x, y, radius + 5, 0, Math.PI * 2);
        context.strokeStyle = 'rgba(255,190,92,0.42)';
        context.lineWidth = 1.5;
        context.stroke();
        context.font = '700 10px monospace';
        context.fillStyle = 'rgba(255,235,201,0.98)';
        context.textAlign = 'center';
        context.fillText('EVA CORE', x, y - radius - 10);
        context.textAlign = 'left';
      }
      if (doneAgent) {
        context.font = '700 9px monospace'; context.fillStyle = '#07100f'; context.textAlign = 'center';
        context.fillText('✓', x, y + 3); context.textAlign = 'left';
      }
      if (!core && (entity || agent || state.hoverNode === node)) {
        var statusSuffix = agent ? ' · ' + statusLabel(node.status) : '';
        var label = (node.label || node.id).slice(0, 24) + statusSuffix;
        context.font = (core || entity || agent) ? '600 10px monospace' : '10px monospace';
        context.fillStyle = (core || entity) ? 'rgba(255,235,201,0.92)' : (agent ? 'rgba(218,231,255,0.95)' : 'rgba(219,255,250,0.9)');
        var labelWidth = context.measureText(label).width;
        var labelX = x + radius + 6;
        if (labelX + labelWidth > width - 8) labelX = x - radius - 6 - labelWidth;
        context.fillText(label, Math.max(8, labelX), y + 3);
      }
    });
    state.animationFrame = requestAnimationFrame(drawGraph);
  }

  function drawGrid(context, width, height, time) {
    context.save();
    context.strokeStyle = 'rgba(74, 222, 199, 0.035)';
    context.lineWidth = 1;
    var offset = (time * 0.004) % 28;
    for (var x = offset; x < width; x += 28) {
      context.beginPath(); context.moveTo(x, 0); context.lineTo(x, height); context.stroke();
    }
    for (var y = offset; y < height; y += 28) {
      context.beginPath(); context.moveTo(0, y); context.lineTo(width, y); context.stroke();
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
      var dx = node.x * state.canvasWidth - position.x;
      var dy = node.y * state.canvasHeight - position.y;
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
      if (state.dragNode) canvas.setPointerCapture(event.pointerId);
    });
    canvas.addEventListener('pointerup', function() { state.dragNode = null; });
    canvas.addEventListener('pointerleave', function() {
      state.dragNode = null;
      state.hoverNode = null;
      var tooltip = document.getElementById('agentGraphTooltip');
      if (tooltip) tooltip.setAttribute('aria-hidden', 'true');
    });
  }

  function graphTooltipText(node) {
    var connected = state.edges.filter(function(edge) { return edge.source === node.id || edge.target === node.id; });
    var lines = [node.label || node.id];
    if (node.type === 'core') lines.push('Eva cognitive root · orchestrates agent sessions');
    else if (node.type === 'agent') {
      lines.push('Agent session · ' + statusLabel(node.status));
      lines.push('Model: ' + (node.model || 'default'));
      if (node.result) lines.push('Latest: ' + String(node.result).replace(/\s+/g, ' ').slice(0, 150));
    } else if (node.type === 'fact') {
      lines.push((node.source_label || 'Memory') + ' → ' + (node.relation || 'related fact'));
      lines.push(node.full_label || node.label || '');
      if (node.confidence) lines.push('Confidence: ' + Math.round(node.confidence * 100) + '%');
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
    if (refreshButton) refreshButton.addEventListener('click', function() { refresh(true); });
    if (closeButton) closeButton.addEventListener('click', close);
    if (detailClose) detailClose.addEventListener('click', closeDetail);
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

  document.addEventListener('DOMContentLoaded', init);
  function invalidateGraph() {
    state.graphFetchedAt = 0;
    if (state.open) refresh(true);
  }

  return {
    open: open,
    close: close,
    toggle: toggle,
    refresh: refresh,
    openAgent: openAgent,
    invalidateGraph: invalidateGraph,
    _selectGraphNodes: selectGraphNodes
  };
})();