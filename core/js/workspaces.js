var EvaWorkspaces = (function() {
  var state = {
    projects: [],
    runs: [],
    selectedProjectId: '',
    selectedRunId: '',
    pendingDiscardRunId: '',
    loading: false,
    workbenchOpen: false,
    monitorInFlight: false,
    monitorTimer: null,
    monitorSignature: '',
    monitorRunStates: {},
    monitorActivity: [],
    lastMonitorVoiceAt: 0,
    lastPeriodicNoteAt: 0,
    lastCheckedAt: 0
  };

  function api() {
    return window.evaStandalone || null;
  }

  function supported() {
    var value = api();
    return !!(value && value.workspaceTerminalV1 && value.workspaceListProjects && value.workspaceCreateRun);
  }

  function panel() {
    return document.getElementById('workspacePanel');
  }

  function status(message, kind) {
    var element = document.getElementById('workspaceStatus');
    if (!element) return;
    element.textContent = message || '';
    element.dataset.state = kind || '';
  }

  function setBusy(busy) {
    state.loading = busy;
    ['workspaceAddProjectBtn', 'workspaceRefreshBtn', 'workspaceCreateRunBtn'].forEach(function(id) {
      var element = document.getElementById(id);
      if (!element) return;
      var needsProject = id === 'workspaceCreateRunBtn';
      element.disabled = busy || !supported() || (needsProject && !state.projects.length);
    });
    var projectSelect = document.getElementById('workspaceProjectSelect');
    if (projectSelect) projectSelect.disabled = busy || !supported() || !state.projects.length;
  }

  function formatTime(value) {
    var date = new Date(value || '');
    if (Number.isNaN(date.getTime())) return '';
    return date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
  }

  function runStatus(run) {
    if (run && run.agent && run.agent.status) return String(run.agent.status).replace(/_/g, ' ').toUpperCase();
    if (run && run.status === 'active') return 'DISPATCHING';
    return String(run && run.status || 'unknown').replace(/_/g, ' ').toUpperCase();
  }

  function checkoutLabel(checkout) {
    if (!checkout) return '';
    var branch = checkout.branch || 'detached';
    var changes = Number(checkout.dirtyFileCount || 0);
    return branch + (changes ? ' | ' + changes + ' changed' : ' | clean');
  }

  function selectProject(projectId) {
    state.selectedProjectId = projectId || '';
    var select = document.getElementById('workspaceProjectSelect');
    if (select) select.value = state.selectedProjectId;
    renderProjects();
    renderRuns();
  }

  function selectRun(runId) {
    var run = state.runs.find(function(item) { return item.id === runId; });
    state.selectedRunId = run ? run.id : '';
    if (run) state.selectedProjectId = run.projectId;
    renderProjects();
    renderRuns();
    renderDetail();
  }

  function renderProjects() {
    var list = document.getElementById('workspaceProjectList');
    var select = document.getElementById('workspaceProjectSelect');
    if (!list || !select) return;
    list.replaceChildren();
    select.replaceChildren();
    if (!state.projects.length) {
      var empty = document.createElement('li');
      empty.className = 'workspace-empty';
      empty.textContent = 'No projects yet';
      list.appendChild(empty);
      var option = document.createElement('option');
      option.textContent = 'Add a project first';
      option.value = '';
      select.appendChild(option);
      setBusy(state.loading);
      return;
    }
    state.projects.forEach(function(project) {
      var option = document.createElement('option');
      option.value = project.id;
      option.textContent = project.name;
      select.appendChild(option);

      var item = document.createElement('li');
      item.className = 'workspace-item workspace-project-item';
      item.tabIndex = 0;
      item.dataset.projectId = project.id;
      if (project.id === state.selectedProjectId) item.classList.add('active');
      var title = document.createElement('span');
      title.className = 'workspace-item-title';
      title.textContent = project.name;
      var summary = document.createElement('span');
      summary.className = 'workspace-item-summary';
      summary.textContent = (project.activeRunCount || 0) + ' active run' + (project.activeRunCount === 1 ? '' : 's');
      item.append(title, summary);
      item.addEventListener('click', function() { selectProject(project.id); });
      item.addEventListener('keydown', function(event) {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          selectProject(project.id);
        }
      });
      list.appendChild(item);
    });
    if (!state.selectedProjectId || !state.projects.some(function(project) { return project.id === state.selectedProjectId; })) {
      state.selectedProjectId = state.projects[0].id;
    }
    select.value = state.selectedProjectId;
    setBusy(state.loading);
  }

  function renderRuns() {
    var list = document.getElementById('workspaceRunList');
    if (!list) return;
    list.replaceChildren();
    var visibleRuns = state.selectedProjectId
      ? state.runs.filter(function(run) { return run.projectId === state.selectedProjectId; })
      : state.runs;
    if (!visibleRuns.length) {
      var empty = document.createElement('li');
      empty.className = 'workspace-empty';
      empty.textContent = state.selectedProjectId ? 'No coding runs for this project' : 'No coding runs yet';
      list.appendChild(empty);
      return;
    }
    visibleRuns.forEach(function(run) {
      var item = document.createElement('li');
      item.className = 'workspace-item workspace-run-item';
      item.tabIndex = 0;
      item.dataset.runId = run.id;
      if (run.id === state.selectedRunId) item.classList.add('active');
      var title = document.createElement('span');
      title.className = 'workspace-item-title';
      title.textContent = run.objective;
      var summary = document.createElement('span');
      summary.className = 'workspace-item-summary';
      summary.textContent = runStatus(run) + ' | ' + checkoutLabel(run.checkout) + (formatTime(run.updatedAt) ? ' | ' + formatTime(run.updatedAt) : '');
      item.append(title, summary);
      item.addEventListener('click', function() { selectRun(run.id); });
      item.addEventListener('keydown', function(event) {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          selectRun(run.id);
        }
      });
      list.appendChild(item);
    });
  }

  function actionButton(label, title, handler, disabled) {
    var button = document.createElement('button');
    button.type = 'button';
    button.className = 'workspace-detail-action';
    button.textContent = label;
    button.title = title;
    button.disabled = !!disabled;
    button.addEventListener('click', handler);
    return button;
  }

  function renderDetail() {
    var detail = document.getElementById('workspaceDetail');
    if (!detail) return;
    detail.replaceChildren();
    var run = state.runs.find(function(item) { return item.id === state.selectedRunId; });
    if (!run) return;
    var heading = document.createElement('h2');
    heading.textContent = run.project ? run.project.name : 'Coding run';
    var objective = document.createElement('p');
    objective.className = 'workspace-detail-objective';
    objective.textContent = run.objective;
    var facts = document.createElement('dl');
    facts.className = 'workspace-detail-facts';
    [['Status', runStatus(run)], ['Branch', run.checkout ? run.checkout.branch || 'detached' : 'unavailable'], ['Changes', run.checkout ? String(run.checkout.dirtyFileCount || 0) : 'unknown']].forEach(function(entry) {
      var term = document.createElement('dt');
      term.textContent = entry[0];
      var description = document.createElement('dd');
      description.textContent = entry[1];
      facts.append(term, description);
    });
    if (state.pendingDiscardRunId === run.id) {
      var confirmation = document.createElement('div');
      confirmation.className = 'workspace-discard-confirm';
      var message = document.createElement('p');
      var changes = Number(run.checkout && run.checkout.dirtyFileCount || 0);
      message.textContent = changes
        ? 'Discard this worktree, branch, and ' + changes + ' local change' + (changes === 1 ? '' : 's') + '?'
        : 'Discard this worktree and branch?';
      var confirmationActions = document.createElement('div');
      confirmationActions.className = 'workspace-detail-actions';
      confirmationActions.append(
        actionButton('Cancel', 'Keep this coding run', function() {
          state.pendingDiscardRunId = '';
          renderDetail();
        }),
        actionButton('Discard worktree', 'Confirm removal of this managed worktree', function() {
          applyRunAction(run, 'discard');
        })
      );
      confirmation.append(message, confirmationActions);
      detail.append(heading, objective, facts, confirmation);
      return;
    }
    var actions = document.createElement('div');
    actions.className = 'workspace-detail-actions';
    var unavailable = !run.checkout || run.checkout.lifecycle !== 'active';
    actions.appendChild(actionButton('Terminal', 'Open this worktree in the local terminal', function() {
      if (typeof openWorkspaceTerminal === 'function') {
        openWorkspaceTerminal(run.checkout.id, (run.project ? run.project.name + ' | ' : '') + (run.checkout.branch || 'worktree'));
      }
    }, unavailable));
    if (run.primarySessionId && typeof loadSession === 'function') {
      actions.appendChild(actionButton('Chat', 'Open this run\'s primary chat', function() { loadSession(run.primarySessionId); }));
    }
    var agentActive = run.agent && ['starting', 'running', 'steering'].indexOf(run.agent.status) !== -1;
    if ((run.status === 'active' || run.status === 'completed') && !agentActive) {
      actions.appendChild(actionButton('Archive', 'Keep this run and hide it from active work', function() { applyRunAction(run, 'archive'); }));
      actions.appendChild(actionButton('Discard', 'Review removal of this managed worktree', function() {
        state.pendingDiscardRunId = run.id;
        renderDetail();
      }));
    }
    detail.append(heading, objective, facts, actions);
  }

  function monitorSignature(runs, terminals) {
    return runs.map(function(run) {
      return [run.id, run.status, run.checkout && run.checkout.dirtyFileCount, run.checkout && run.checkout.lifecycle,
        run.agent && run.agent.status, run.agent && run.agent.updatedAt, run.agent && run.agent.report].join(':');
    }).sort().join('|') + '//' + terminals.map(function(terminal) {
      return [terminal.rootId, terminal.id, terminal.exited].join(':');
    }).sort().join('|');
  }

  function narrateRunChanges(runs) {
    var nextStates = {};
    runs.forEach(function(run) {
      var agent = run.agent || {};
      var current = {
        status: agent.status || '',
        report: String(agent.report || ''),
        changes: Number(run.checkout && run.checkout.dirtyFileCount || 0)
      };
      nextStates[run.id] = current;
      var prior = state.monitorRunStates[run.id];
      var name = run.project ? run.project.name : run.objective;
      if (!prior && current.status) {
        addMonitorActivity('Eva dispatched ' + (agent.id || 'a workspace agent') + ' for ' + name + '.', 'change', true);
      } else if (prior && prior.status !== current.status) {
        if (current.status === 'done') {
          addMonitorActivity('Eva completed "' + run.objective + '" with ' + current.changes + ' changed file' + (current.changes === 1 ? '.' : 's.'), 'change', true);
        } else if (current.status === 'error') {
          addMonitorActivity('Eva could not complete "' + run.objective + '". The run remains available for retry.', 'error', true);
        } else {
          addMonitorActivity('Eva moved "' + run.objective + '" to ' + current.status + '.', 'change', true);
        }
      }
      if (prior && current.status === 'running' && current.report && current.report !== prior.report) {
        var update = current.report.replace(/\s+/g, ' ').trim();
        if (update) addMonitorActivity('Eva update: ' + update.slice(-240), 'info', false);
      }
    });
    state.monitorRunStates = nextStates;
  }

  function activeRuns() {
    return state.runs.filter(function(run) { return run.status === 'active'; });
  }

  function monitorSummary() {
    var ready = activeRuns();
    if (!ready.length) return 'Eva monitor: no ready coding workspaces.';
    var run = ready.find(function(item) { return item.id === state.selectedRunId; }) || ready[0];
    var changes = Number(run.checkout && run.checkout.dirtyFileCount || 0);
    var agentStatus = run.agent && run.agent.status || 'starting';
    return 'Eva monitor: ' + ready.length + ' coding workspace' + (ready.length === 1 ? '' : 's') + '. ' +
      (run.project ? run.project.name + ' agent is ' + agentStatus : 'The selected agent is ' + agentStatus) +
      (changes ? ' with ' + changes + ' changed file' + (changes === 1 ? '.' : 's.') : ' and a clean worktree.') +
      (agentStatus === 'done' ? ' Review the result when ready.' : ' Eva is monitoring progress.');
  }

  function addMonitorActivity(message, kind, allowVoice) {
    var entry = {
      id: Date.now() + '-' + Math.random().toString(36).slice(2, 7),
      message: message,
      kind: kind || 'info',
      at: new Date()
    };
    state.monitorActivity.unshift(entry);
    state.monitorActivity = state.monitorActivity.slice(0, 60);
    if (allowVoice && Date.now() - state.lastMonitorVoiceAt >= 120000) {
      var autoSpeak = document.getElementById('autoSpeak');
      if (autoSpeak && autoSpeak.checked && typeof speakText === 'function') {
        state.lastMonitorVoiceAt = Date.now();
        var engine = document.getElementById('selEngine');
        if (engine && engine.value === 'local-voices' && api() && typeof api().localVoicesStatus === 'function') {
          api().localVoicesStatus().then(function(localStatus) {
            if (localStatus && localStatus.running) speakText(message);
          }).catch(function() {});
        } else {
          speakText(message);
        }
      }
    }
  }

  function renderWorkbench() {
    var runList = document.getElementById('workspaceWorkbenchRuns');
    var feed = document.getElementById('workspaceMonitorFeed');
    var detail = document.getElementById('workspaceWorkbenchDetail');
    if (!runList || !feed || !detail) return;
    runList.replaceChildren();
    var orderedRuns = state.runs.filter(function(run) { return run.status !== 'discarded'; });
    if (!orderedRuns.length) {
      var empty = document.createElement('p');
      empty.className = 'workspace-monitor-empty';
      empty.textContent = 'No coding runs';
      runList.appendChild(empty);
    }
    orderedRuns.forEach(function(run) {
      var button = document.createElement('button');
      button.type = 'button';
      button.className = 'workspace-monitor-run';
      if (run.id === state.selectedRunId) button.classList.add('active');
      var title = document.createElement('strong');
      title.textContent = run.objective;
      var meta = document.createElement('span');
      meta.textContent = (run.project ? run.project.name + ' | ' : '') + runStatus(run) + ' | ' + checkoutLabel(run.checkout);
      button.append(title, meta);
      button.addEventListener('click', function() {
        selectRun(run.id);
        renderWorkbench();
      });
      runList.appendChild(button);
    });

    feed.replaceChildren();
    if (!state.monitorActivity.length) addMonitorActivity(monitorSummary(), 'info', false);
    state.monitorActivity.forEach(function(entry) {
      var item = document.createElement('li');
      item.className = 'workspace-monitor-event';
      item.dataset.kind = entry.kind;
      var time = document.createElement('time');
      time.dateTime = entry.at.toISOString();
      time.textContent = entry.at.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
      var text = document.createElement('span');
      text.textContent = entry.message;
      item.append(time, text);
      feed.appendChild(item);
    });

    detail.replaceChildren();
    var selected = state.runs.find(function(run) { return run.id === state.selectedRunId; }) || activeRuns()[0] || orderedRuns[0];
    if (!selected) {
      var unavailable = document.createElement('p');
      unavailable.className = 'workspace-monitor-empty';
      unavailable.textContent = 'Select a run to inspect its context.';
      detail.appendChild(unavailable);
    } else {
      state.selectedRunId = selected.id;
      var heading = document.createElement('h2');
      heading.textContent = selected.objective;
      var branch = document.createElement('p');
      branch.className = 'workspace-monitor-branch';
      branch.textContent = selected.checkout ? selected.checkout.branch || 'detached' : 'checkout unavailable';
      var facts = document.createElement('dl');
      [['Status', runStatus(selected)], ['Agent', selected.agent ? selected.agent.id : 'dispatching'], ['Policy', selected.agent ? selected.agent.capabilityPolicy : 'workspace_write'], ['Changes', String(selected.checkout && selected.checkout.dirtyFileCount || 0)], ['Chat', selected.primarySessionId ? 'linked' : 'none']].forEach(function(pair) {
        var term = document.createElement('dt');
        term.textContent = pair[0];
        var value = document.createElement('dd');
        value.textContent = pair[1];
        facts.append(term, value);
      });
      var actions = document.createElement('div');
      actions.className = 'workspace-monitor-detail-actions';
      var terminalButton = document.createElement('button');
      terminalButton.type = 'button';
      terminalButton.textContent = 'Open terminal';
      terminalButton.disabled = !selected.checkout || selected.checkout.lifecycle !== 'active';
      terminalButton.addEventListener('click', function() {
        openWorkspaceTerminal(selected.checkout.id, (selected.project ? selected.project.name + ' | ' : '') + (selected.checkout.branch || 'worktree'));
      });
      actions.appendChild(terminalButton);
      if (selected.primarySessionId && typeof loadSession === 'function') {
        var chatButton = document.createElement('button');
        chatButton.type = 'button';
        chatButton.textContent = 'Open chat';
        chatButton.addEventListener('click', function() { loadSession(selected.primarySessionId); closeWorkbench(); });
        actions.appendChild(chatButton);
      }
      detail.append(heading, branch, facts, actions);
      if (selected.agent && selected.agent.report) {
        var report = document.createElement('pre');
        report.className = 'workspace-monitor-report';
        report.textContent = selected.agent.report;
        detail.appendChild(report);
      }
    }

    var active = activeRuns();
    var terminals = state.lastTerminals || [];
    var dirty = active.reduce(function(total, run) { return total + Number(run.checkout && run.checkout.dirtyFileCount || 0); }, 0);
    var values = {
      workspaceMonitorActiveRuns: active.length,
      workspaceMonitorTerminalCount: terminals.filter(function(item) { return !item.exited; }).length,
      workspaceMonitorDirtyCount: dirty,
      workspaceMonitorLastCheck: state.lastCheckedAt ? new Date(state.lastCheckedAt).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '--'
    };
    Object.keys(values).forEach(function(id) {
      var element = document.getElementById(id);
      if (element) element.textContent = values[id];
    });
    var updated = document.getElementById('workspaceMonitorUpdated');
    if (updated) updated.textContent = state.lastCheckedAt ? 'UPDATED ' + new Date(state.lastCheckedAt).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : 'STANDBY';
  }

  async function monitor() {
    if (!supported() || state.monitorInFlight) return;
    state.monitorInFlight = true;
    try {
      state.projects = await api().workspaceListProjects();
      var runs = await api().workspaceListRuns();
      var selected = runs.find(function(run) { return run.id === state.selectedRunId; });
      if (selected && selected.checkout && selected.checkout.lifecycle === 'active' && typeof api().workspaceCheckoutStatus === 'function') {
        selected.checkout = await api().workspaceCheckoutStatus(selected.checkout.id);
      }
      var terminals = await api().terminalList();
      var signature = monitorSignature(runs, terminals);
      var changed = signature !== state.monitorSignature;
      state.runs = runs;
      state.lastTerminals = terminals;
      state.lastCheckedAt = Date.now();
      narrateRunChanges(state.runs);
      if (changed) {
        state.monitorSignature = signature;
        addMonitorActivity(monitorSummary(), 'change', true);
      } else if (activeRuns().length && Date.now() - state.lastPeriodicNoteAt >= 300000) {
        state.lastPeriodicNoteAt = Date.now();
        addMonitorActivity(monitorSummary(), 'heartbeat', true);
      }
      if (state.workbenchOpen) renderWorkbench();
      var quickPanel = panel();
      if (quickPanel && quickPanel.getAttribute('aria-hidden') === 'false') {
        renderProjects();
        renderRuns();
        renderDetail();
      }
    } catch (error) {
      if (state.workbenchOpen) addMonitorActivity('Eva monitor could not refresh: ' + (error.message || error), 'error', false);
    } finally {
      state.monitorInFlight = false;
    }
  }

  function openWorkbench() {
    if (!supported()) {
      toggle();
      return;
    }
    if (typeof closeAgentOperationsForNavigation === 'function') closeAgentOperationsForNavigation();
    if (window.EvaAssets && typeof window.EvaAssets.close === 'function') window.EvaAssets.close();
    if (window.EvaSkills && typeof window.EvaSkills.close === 'function') window.EvaSkills.close();
    if (typeof closeSidePanels === 'function') closeSidePanels();
    state.workbenchOpen = true;
    document.body.classList.add('workspace-workbench-open');
    var view = document.getElementById('workspaceWorkbench');
    if (view) view.setAttribute('aria-hidden', 'false');
    monitor();
    renderWorkbench();
  }

  function closeWorkbench() {
    state.workbenchOpen = false;
    document.body.classList.remove('workspace-workbench-open');
    var view = document.getElementById('workspaceWorkbench');
    if (view) view.setAttribute('aria-hidden', 'true');
  }

  async function refresh() {
    if (!supported()) {
      renderUnavailable();
      return;
    }
    setBusy(true);
    status('Refreshing...', 'loading');
    try {
      state.projects = await api().workspaceListProjects();
      if (!state.selectedProjectId && state.projects.length) state.selectedProjectId = state.projects[0].id;
      state.runs = await api().workspaceListRuns();
      if (state.selectedRunId && !state.runs.some(function(run) { return run.id === state.selectedRunId; })) state.selectedRunId = '';
      renderProjects();
      renderRuns();
      renderDetail();
      if (state.workbenchOpen) renderWorkbench();
      status('', '');
    } catch (error) {
      status(error.message || 'Workspace refresh failed.', 'error');
    } finally {
      setBusy(false);
    }
  }

  function renderUnavailable() {
    var unavailable = document.getElementById('workspaceUnavailable');
    var form = document.getElementById('workspaceRunForm');
    if (unavailable) {
      unavailable.hidden = false;
      unavailable.textContent = 'Coding workspaces are available in Eva Standalone.';
    }
    if (form) form.hidden = true;
    setBusy(true);
  }

  async function addProject() {
    if (!supported() || state.loading) return;
    setBusy(true);
    status('Waiting for project selection...', 'loading');
    try {
      var result = await api().workspaceSelectProject();
      if (result && !result.canceled && result.project) {
        state.selectedProjectId = result.project.id;
        status('Project added.', 'success');
      } else if (result && result.error) {
        throw new Error(result.error);
      } else {
        status('', '');
      }
      await refresh();
    } catch (error) {
      status(error.message || 'Project selection failed.', 'error');
      setBusy(false);
    }
  }

  async function createRun(event) {
    event.preventDefault();
    if (!supported() || state.loading) return;
    var projectSelect = document.getElementById('workspaceProjectSelect');
    var objective = document.getElementById('workspaceObjective');
    var baseRef = document.getElementById('workspaceBaseRef');
    var project = projectSelect && state.projects.find(function(item) { return item.id === projectSelect.value; });
    if (!project) {
      status('Eva is preparing the ready workspace. Refresh in a moment.', 'error');
      return;
    }
    if (!objective || !objective.value.trim()) {
      status('Describe the coding run first.', 'error');
      objective.focus();
      return;
    }
    setBusy(true);
    status('Creating isolated worktree...', 'loading');
    try {
      var primarySessionId = typeof _activeSessionId === 'function' ? _activeSessionId() : '';
      var run = await api().workspaceCreateRun({
        projectId: projectSelect.value,
        objective: objective.value.trim(),
        primarySessionId: primarySessionId,
        baseRef: baseRef ? baseRef.value.trim() || 'HEAD' : 'HEAD'
      });
      objective.value = '';
      state.selectedProjectId = run.projectId;
      state.selectedRunId = run.id;
      await refresh();
      status(run.dispatchError ? 'Workspace ready; agent dispatch delayed: ' + run.dispatchError : 'Workspace agent dispatched.', run.dispatchError ? 'error' : 'success');
    } catch (error) {
      status(error.message || 'Could not create coding run.', 'error');
      setBusy(false);
    }
  }

  async function applyRunAction(run, action) {
    var confirmDirty = false;
    if (action === 'discard') {
      var changes = Number(run.checkout && run.checkout.dirtyFileCount || 0);
      confirmDirty = changes > 0;
    }
    setBusy(true);
    status(action === 'archive' ? 'Archiving run...' : 'Discarding run...', 'loading');
    try {
      var actionRun = run;
      if (action === 'discard') {
        var currentRuns = await api().workspaceListRuns(run.projectId);
        actionRun = currentRuns.find(function(item) { return item.id === run.id; }) || null;
        if (!actionRun || !actionRun.checkout || ['active', 'completed'].indexOf(actionRun.status) === -1) {
          throw new Error('This coding run is no longer available for discard.');
        }
        if (actionRun.agent && ['starting', 'running', 'steering'].indexOf(actionRun.agent.status) !== -1) {
          throw new Error('The workspace agent is still running. Wait for completion before discard.');
        }
        if (typeof api().terminalCloseRoot === 'function') {
          await api().terminalCloseRoot(actionRun.checkout.id);
        }
      }
      await api().workspaceRunAction(run.id, action, {
        confirmDirty: confirmDirty,
        checkoutId: actionRun && actionRun.checkout && actionRun.checkout.id
      });
      state.selectedRunId = '';
      state.pendingDiscardRunId = '';
      await refresh();
      status(action === 'archive' ? 'Run archived.' : 'Run discarded.', 'success');
    } catch (error) {
      state.pendingDiscardRunId = '';
      renderDetail();
      status(error.message || 'Workspace action failed.', 'error');
      setBusy(false);
    }
  }

  function toggle() {
    var currentPanel = panel();
    if (!currentPanel) return;
    var visible = currentPanel.getAttribute('aria-hidden') !== 'true';
    if (visible) {
      currentPanel.setAttribute('aria-hidden', 'true');
      return;
    }
    if (typeof closeAgentOperationsForNavigation === 'function') closeAgentOperationsForNavigation();
    if (typeof closeSidePanels === 'function') closeSidePanels('workspacePanel');
    currentPanel.setAttribute('aria-hidden', 'false');
    refresh();
  }

  function init() {
    var close = document.getElementById('workspacePanelClose');
    var add = document.getElementById('workspaceAddProjectBtn');
    var refreshButton = document.getElementById('workspaceRefreshBtn');
    var form = document.getElementById('workspaceRunForm');
    var projectSelect = document.getElementById('workspaceProjectSelect');
    var openWorkbenchButton = document.getElementById('workspaceOpenWorkbenchBtn');
    var monitorRefresh = document.getElementById('workspaceMonitorRefreshBtn');
    var monitorClose = document.getElementById('workspaceMonitorCloseBtn');
    var monitorNew = document.getElementById('workspaceMonitorNewBtn');
    if (close) close.addEventListener('click', toggle);
    if (add) add.addEventListener('click', addProject);
    if (refreshButton) refreshButton.addEventListener('click', refresh);
    if (form) form.addEventListener('submit', createRun);
    if (projectSelect) projectSelect.addEventListener('change', function() { selectProject(projectSelect.value); });
    if (openWorkbenchButton) openWorkbenchButton.addEventListener('click', openWorkbench);
    if (monitorRefresh) monitorRefresh.addEventListener('click', monitor);
    if (monitorClose) monitorClose.addEventListener('click', closeWorkbench);
    if (monitorNew) monitorNew.addEventListener('click', async function() {
      closeWorkbench();
      var currentPanel = panel();
      if (currentPanel) currentPanel.setAttribute('aria-hidden', 'false');
      await refresh();
      var objective = document.getElementById('workspaceObjective');
      if (objective) objective.focus();
    });
    if (!supported()) renderUnavailable();
    else {
      monitor();
      state.monitorTimer = setInterval(monitor, 10000);
    }
  }

  document.addEventListener('DOMContentLoaded', init);
  return { toggle: toggle, refresh: refresh, openWorkbench: openWorkbench, closeWorkbench: closeWorkbench, open: openWorkbench };
})();

function toggleWorkspacePanel() {
  EvaWorkspaces.toggle();
}
