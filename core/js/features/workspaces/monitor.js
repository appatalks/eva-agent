var EvaWorkspaces = (function() {
  var WORKSPACE_DISPLAY_STATE_STORAGE_KEY = 'eva.workspaceMonitorDisplay.v1';
  var WORKSPACE_CHAT_DRAWER_STORAGE_KEY = 'eva.workspaceChatDrawer.open';

  function workspaceDisplayState() {
    function booleanMap(value) {
      var output = {};
      if (!value || typeof value !== 'object' || Array.isArray(value)) return output;
      Object.keys(value).forEach(function(id) {
        if (value[id] === true) output[String(id).slice(0, 120)] = true;
      });
      return output;
    }
    try {
      var saved = JSON.parse(localStorage.getItem(WORKSPACE_DISPLAY_STATE_STORAGE_KEY) || '{}');
      return {
        clearedActivityProjectIds: booleanMap(saved.clearedActivityProjectIds),
        clearedCodingRunProjectIds: booleanMap(saved.clearedCodingRunProjectIds),
        clearedResultRunIds: booleanMap(saved.clearedResultRunIds)
      };
    } catch (_) {
      return {
        clearedActivityProjectIds: {},
        clearedCodingRunProjectIds: {},
        clearedResultRunIds: {}
      };
    }
  }

  var savedDisplayState = workspaceDisplayState();
  var state = {
    projects: [],
    runs: [],
    projectFiles: {},
    projectFilesLoading: {},
    projectTreeExpanded: {},
    runDrafts: {},
    pendingPermissions: [],
    selectedProjectId: '',
    selectedRunId: '',
    pendingDiscardRunId: '',
    loading: false,
    mcpUpdating: false,
    workbenchOpen: false,
    monitorInFlight: false,
    monitorTimer: null,
    monitorSignature: '',
    permissionSignature: '',
    monitorRunStates: {},
    monitorActivity: [],
    clearedActivityProjectIds: savedDisplayState.clearedActivityProjectIds,
    clearedCodingRunProjectIds: savedDisplayState.clearedCodingRunProjectIds,
    clearedResultRunIds: savedDisplayState.clearedResultRunIds,
    lastMonitorVoiceAt: 0,
    lastPeriodicNoteAt: 0,
    lastCheckedAt: 0,
    githubRepositories: [],
    githubRepositoriesCollapsed: false,
    githubAuthTimer: null,
    githubAuthRetry: null,
    chatDrawerOpen: false,
    chatNodeOrigins: null,
    sessionSwitching: false
  };

  function api() {
    return window.evaStandalone || null;
  }

  function supported() {
    var value = api();
    return !!(value && value.workspaceTerminalV1 && value.workspaceListProjects && value.workspaceCreateRun);
  }

  function autoApprovePreference(value) {
    try {
      if (typeof value === 'boolean') localStorage.setItem('workspaceAutoApprove', value ? 'true' : 'false');
      return localStorage.getItem('workspaceAutoApprove') !== 'false';
    } catch (_) {
      return true;
    }
  }

  function chatDrawerPreference(value) {
    try {
      if (typeof value === 'boolean') localStorage.setItem(WORKSPACE_CHAT_DRAWER_STORAGE_KEY, value ? 'true' : 'false');
      var saved = localStorage.getItem(WORKSPACE_CHAT_DRAWER_STORAGE_KEY);
      return saved === null ? true : saved === 'true';
    } catch (_) {
      return true;
    }
  }

  function captureChatNodeOrigins() {
    if (state.chatNodeOrigins) return state.chatNodeOrigins;
    var output = document.getElementById('txtOutput');
    var input = document.querySelector('.chat-input-container');
    if (!output || !input) return null;
    state.chatNodeOrigins = {
      output: output,
      outputParent: output.parentNode,
      outputNext: output.nextSibling,
      input: input,
      inputParent: input.parentNode,
      inputNext: input.nextSibling
    };
    return state.chatNodeOrigins;
  }

  function restoreChatNodes() {
    var origins = state.chatNodeOrigins;
    if (!origins) return;
    if (origins.outputParent) origins.outputParent.insertBefore(origins.output, origins.outputNext && origins.outputNext.parentNode === origins.outputParent ? origins.outputNext : null);
    if (origins.inputParent) origins.inputParent.insertBefore(origins.input, origins.inputNext && origins.inputNext.parentNode === origins.inputParent ? origins.inputNext : null);
  }

  function setChatDrawerOpen(open, remember) {
    var drawer = document.getElementById('workspaceChatDrawer');
    var outputHost = document.getElementById('workspaceChatOutputHost');
    var inputHost = document.getElementById('workspaceChatInputHost');
    var toggle = document.getElementById('workspaceChatToggleBtn');
    var origins = captureChatNodeOrigins();
    open = !!(open && drawer && outputHost && inputHost && origins);
    if (open) {
      outputHost.appendChild(origins.output);
      inputHost.appendChild(origins.input);
      refreshChatSessionSelect();
      requestAnimationFrame(function() { origins.output.scrollTop = origins.output.scrollHeight; });
    }
    state.chatDrawerOpen = open;
    if (drawer) drawer.setAttribute('aria-hidden', open ? 'false' : 'true');
    if (toggle) toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    document.body.classList.toggle('workspace-chat-drawer-open', open);
    if (remember !== false) chatDrawerPreference(open);
  }

  function refreshChatSessionSelect() {
    var select = document.getElementById('workspaceChatSessionSelect');
    if (!select || typeof getAllSessions !== 'function') return Promise.resolve();
    var activeId = '';
    try { activeId = localStorage.getItem('eva_active_session') || ''; } catch (_) {}
    return Promise.resolve(getAllSessions()).then(function(sessions) {
      sessions = Array.isArray(sessions) ? sessions.slice() : [];
      sessions.sort(function(left, right) {
        if (left.pinned !== right.pinned) return left.pinned ? -1 : 1;
        return Number(right.updatedAt || 0) - Number(left.updatedAt || 0);
      });
      select.replaceChildren();
      sessions.forEach(function(session) {
        var option = document.createElement('option');
        option.value = session.id;
        option.textContent = (session.pinned ? '\u2022 ' : '') + (session.title || 'Untitled');
        option.selected = session.id === activeId;
        select.appendChild(option);
      });
      if (!sessions.length) {
        var empty = document.createElement('option');
        empty.value = activeId;
        empty.textContent = 'Current session';
        select.appendChild(empty);
      }
      select.disabled = sessions.length < 2;
    }).catch(function() {
      select.disabled = true;
    });
  }

  function switchChatSession(sessionId) {
    if (!sessionId || state.sessionSwitching || typeof loadSession !== 'function') return;
    state.sessionSwitching = true;
    var select = document.getElementById('workspaceChatSessionSelect');
    if (select) select.disabled = true;
    Promise.resolve(loadSession(sessionId, { preserveWorkspace: true })).then(function(loaded) {
      if (loaded) {
        setChatDrawerOpen(true, false);
        status('Chat session loaded.', 'success');
      }
    }).catch(function(error) {
      status(error && error.message ? error.message : 'Chat session could not be loaded.', 'error');
    }).finally(function() {
      state.sessionSwitching = false;
      refreshChatSessionSelect();
    });
  }

  function hideChatDrawerOnOutsidePointer(event) {
    if (!state.workbenchOpen || !state.chatDrawerOpen) return;
    if (event.target.closest('#workspaceChatDrawer') || event.target.closest('#workspaceChatToggleBtn')) return;
    setChatDrawerOpen(false);
  }

  function currentProjectId() {
    return state.selectedProjectId;
  }

  function persistWorkspaceDisplayState() {
    try {
      localStorage.setItem(WORKSPACE_DISPLAY_STATE_STORAGE_KEY, JSON.stringify({
        clearedActivityProjectIds: state.clearedActivityProjectIds,
        clearedCodingRunProjectIds: state.clearedCodingRunProjectIds,
        clearedResultRunIds: state.clearedResultRunIds
      }));
    } catch (_) {}
  }

  function pruneWorkspaceDisplayState() {
    var projectIds = {};
    var runIds = {};
    state.projects.forEach(function(project) { projectIds[project.id] = true; });
    state.runs.forEach(function(run) { runIds[run.id] = true; });
    [state.clearedActivityProjectIds, state.clearedCodingRunProjectIds].forEach(function(entries) {
      Object.keys(entries).forEach(function(id) {
        if (!projectIds[id]) delete entries[id];
      });
    });
    Object.keys(state.clearedResultRunIds).forEach(function(id) {
      if (!runIds[id]) delete state.clearedResultRunIds[id];
    });
    persistWorkspaceDisplayState();
  }

  function panel() {
    return document.getElementById('workspacePanel');
  }

  function status(message, kind) {
    ['workspaceStatus', 'workspaceWorkbenchStatus'].forEach(function(id) {
      var element = document.getElementById(id);
      if (!element) return;
      element.textContent = message || '';
      element.dataset.state = kind || '';
    });
  }

  function setBusy(busy) {
    state.loading = busy;
    ['workspaceAddProjectBtn', 'workspaceRefreshBtn', 'workspaceCreateRunBtn', 'workspaceAddProjectWorkbenchBtn', 'workspaceListGitHubBtn', 'workspaceImportGitHubBtn'].forEach(function(id) {
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
    var selectedRun = state.runs.find(function(run) { return run.id === state.selectedRunId; });
    if (selectedRun && selectedRun.projectId !== state.selectedProjectId) state.selectedRunId = '';
    var select = document.getElementById('workspaceProjectSelect');
    if (select) select.value = state.selectedProjectId;
    renderProjects();
    renderRuns();
    if (state.workbenchOpen) renderWorkbench();
  }

  function selectRun(runId) {
    var run = state.runs.find(function(item) { return item.id === runId; });
    state.selectedRunId = run ? run.id : '';
    if (run) {
      state.selectedProjectId = run.projectId;
      delete state.clearedResultRunIds[run.id];
      persistWorkspaceDisplayState();
    }
    renderProjects();
    renderRuns();
    renderDetail();
    if (state.workbenchOpen) renderWorkbench();
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

  async function resolveWorkspacePermission(permission, decision) {
    if (typeof backgroundBridgeRequest !== 'function' || typeof getBridgeCapabilityHeaders !== 'function') return;
    try {
      await backgroundBridgeRequest('/v1/acp/permissions/' + encodeURIComponent(permission.id), {
        method: 'POST',
        headers: getBridgeCapabilityHeaders(),
        body: JSON.stringify({ decision: decision })
      });
      state.pendingPermissions = state.pendingPermissions.filter(function(item) { return item.id !== permission.id; });
      status(decision === 'allow' ? 'Workspace execution approved once.' : 'Workspace execution rejected.', decision === 'allow' ? 'success' : 'error');
      if (state.workbenchOpen) renderWorkbench();
    } catch (error) {
      status(error.message || 'Workspace permission could not be resolved.', 'error');
    }
  }

  async function retryWorkspaceRun(run) {
    if (!run || !api() || typeof api().workspaceDispatchRun !== 'function' || state.loading) return;
    setBusy(true);
    status('Retrying workspace agent...', 'loading');
    try {
      var updated = await api().workspaceDispatchRun(run.id);
      state.selectedProjectId = updated.projectId;
      state.selectedRunId = updated.id;
      await refresh();
      status('Workspace agent retry started.', 'success');
      return updated;
    } catch (error) {
      status(error.message || 'Workspace agent retry failed.', 'error');
      setBusy(false);
    }
  }

  async function retryRunById(runId) {
    if (!state.runs.length) await refresh();
    var requestedRunId = String(runId || '').trim();
    var run = state.runs.find(function(item) { return item.id === requestedRunId; }) ||
      state.runs.find(function(item) { return item.id === state.selectedRunId; });
    if (!run) throw new Error('Select an active workspace run before retrying it.');
    if (run.status !== 'active' || (run.agent && ['starting', 'running', 'steering'].indexOf(run.agent.status) >= 0)) {
      throw new Error('This workspace run is already active or cannot be retried.');
    }
    await retryWorkspaceRun(run);
    return 'Workspace agent retry started for ' + (run.project ? run.project.name : 'the selected workspace') + '.';
  }

  function appendWorkspacePermissions(detail, run) {
    var permissions = state.pendingPermissions.filter(function(permission) { return permission.workspaceRunId === run.id; });
    if (!permissions.length) return;
    var section = document.createElement('section');
    section.className = 'workspace-workbench-section workspace-permission-section';
    var heading = document.createElement('h2');
    heading.textContent = 'EXECUTION APPROVAL';
    section.appendChild(heading);
    permissions.forEach(function(permission) {
      var message = document.createElement('p');
      var summary = permission.commandSummary ? ' Command: ' + permission.commandSummary : '';
      message.textContent = 'Eva needs approval to continue a ' + (permission.toolKind || 'tool') + ' action in this workspace.' + summary;
      var actions = document.createElement('div');
      actions.className = 'workspace-monitor-detail-actions';
      var allow = (permission.options || []).find(function(option) { return option.kind === 'allow_once'; });
      actions.append(
        actionButton('Allow once', 'Allow this workspace action once', function() { resolveWorkspacePermission(permission, 'allow'); }, !allow || permission.approvalAllowed === false),
        actionButton('Reject', 'Reject this workspace action', function() { resolveWorkspacePermission(permission, 'reject'); })
      );
      section.append(message, actions);
    });
    detail.appendChild(section);
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
    if (run.status === 'active' && run.agent && run.agent.status === 'error') {
      actions.appendChild(actionButton('Retry', 'Retry this failed workspace run', function() {
        retryWorkspaceRun(run);
      }));
    }
    if ((run.status === 'active' || run.status === 'completed') && !agentActive) {
      actions.appendChild(actionButton('Archive', 'Keep this run and hide it from active work', function() { applyRunAction(run, 'archive'); }));
      actions.appendChild(actionButton('Discard', 'Review removal of this managed worktree', function() {
        state.pendingDiscardRunId = run.id;
        renderDetail();
      }));
    }
    detail.append(heading, objective, facts, actions);
  }

  function monitorSignature(runs, terminals, projects) {
    var runSignature = runs.map(function(run) {
      return [run.id, run.status, run.checkout && run.checkout.dirtyFileCount, run.checkout && run.checkout.lifecycle,
        run.agent && run.agent.status, run.agent && run.agent.updatedAt, run.agent && run.agent.report].join(':');
    }).sort().join('|');
    var terminalSignature = terminals.map(function(terminal) {
      return [terminal.rootId, terminal.id, terminal.exited].join(':');
    }).sort().join('|');
    var projectSignature = (projects || []).map(function(project) {
      var servers = ((project.mcpServers || {}).servers || []).map(function(server) {
        return [server.name, server.digest, server.enabled].join(':');
      }).sort().join(',');
      return project.id + ':' + servers;
    }).sort().join('|');
    return runSignature + '//' + terminalSignature + '//' + projectSignature;
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
        if (current.status === 'done') {
          narrateTerminalRun(run, current);
        } else if (current.status === 'error' || current.status === 'cancelled') {
          narrateFailedRun(run);
        } else {
          var dispatchedMessage = 'Eva dispatched ' + (agent.id || 'a workspace agent') + ' for ' + name + '.';
          addMonitorActivity(dispatchedMessage, 'change', true, false, run);
        }
      } else if (prior && prior.status !== current.status) {
        if (current.status === 'done') {
          narrateTerminalRun(run, current);
        } else if (current.status === 'error' || current.status === 'cancelled') {
          narrateFailedRun(run);
        } else {
          var progressMessage = 'Eva moved "' + run.objective + '" to ' + current.status + '.';
          addMonitorActivity(progressMessage, 'change', true, false, run);
          publishRunChat(run, progressMessage, 'working');
        }
      }
      if (prior && current.status === 'running' && current.report && current.report !== prior.report) {
        var update = current.report.replace(/\s+/g, ' ').trim();
        if (update) addMonitorActivity('Eva update: ' + update.slice(-240), 'info', false, false, run);
      }
    });
    state.monitorRunStates = nextStates;
  }

  function narrateTerminalRun(run, current) {
    if (categorizeRunOutcome(run) === 'test_failure') {
      var failedCheckMessage = 'Eva completed "' + run.objective + '", but the project checks reported a failure. Review the run report for details.';
      addMonitorActivity(failedCheckMessage, 'error', true, true, run);
      publishRunChat(run, failedCheckMessage, 'error');
      return;
    }
    var completedMessage = 'Eva completed "' + run.objective + '" with ' + current.changes + ' changed file' + (current.changes === 1 ? '.' : 's.');
    addMonitorActivity(completedMessage, 'change', true, true, run);
    publishRunChat(run, completedMessage, 'completed');
  }

  function narrateFailedRun(run) {
    var category = categorizeRunOutcome(run);
    var message = runFailureMessage(run.objective, category);
    addMonitorActivity(message, 'error', true, true, run);
    publishRunChat(run, message, 'error');
  }

  function publishRunChat(run, message, kind) {
    if (!run || !run.primarySessionId || typeof _activeSessionId !== 'function') return;
    if (run.primarySessionId !== _activeSessionId()) return;
    if (typeof injectWorkspaceStatusBubble === 'function') injectWorkspaceStatusBubble(message, kind);
  }

  function categorizeRunOutcome(run) {
    var agent = run && run.agent || {};
    var report = String(agent.report || '').toLowerCase();
    if (/required execution permission|permission (?:was )?not approved|permission denied|access denied/.test(report)) return 'permission_denied';
    if (/cancelled by (?:the )?user|user cancel/.test(report)) return 'user_cancelled';
    if (agent.status === 'cancelled') return 'agent_cancelled';
    if (/acp not available|runner.{0,24}unavailable|agent capacity is full|not connected|offline|disabled/.test(report)) return 'runner_unavailable';
    if (/(?:test|tests|build|lint|typecheck|diagnostic|check).{0,48}(?:failed|failure|failing)|(?:failed|failure|failing).{0,48}(?:test|tests|build|lint|typecheck|diagnostic|check)|non[- ]zero exit|exit (?:code|status)\s*[1-9]/.test(report)) return 'test_failure';
    return 'bridge_failure';
  }

  function runFailureMessage(objective, category) {
    var prefix = 'Eva could not complete "' + objective + '". ';
    if (category === 'user_cancelled') return prefix + 'The run was cancelled by the user.';
    if (category === 'agent_cancelled') return prefix + 'The workspace agent cancelled the run.';
    if (category === 'permission_denied') return prefix + 'A sensitive action required permission and was not approved.';
    if (category === 'runner_unavailable') return prefix + 'The local workspace runner is unavailable. The run remains available for retry.';
    if (category === 'test_failure') return prefix + 'The project checks ran and reported a failure. Review the run report for details.';
    return prefix + 'The workspace bridge failed unexpectedly. The run remains available for retry.';
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

  function addMonitorActivity(message, kind, allowVoice, forceVoice, run) {
    run = run || state.runs.find(function(item) { return item.id === state.selectedRunId; });
    var entry = {
      id: Date.now() + '-' + Math.random().toString(36).slice(2, 7),
      message: message,
      kind: kind || 'info',
      at: new Date(),
      projectId: run && run.projectId || state.selectedProjectId || '',
      runId: run && run.id || state.selectedRunId || ''
    };
    state.monitorActivity.unshift(entry);
    state.monitorActivity = state.monitorActivity.slice(0, 60);
    if (allowVoice && (forceVoice || Date.now() - state.lastMonitorVoiceAt >= 120000)) {
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

  function projectById(projectId) {
    return state.projects.find(function(project) { return project.id === projectId; }) || null;
  }

  function replaceProject(project) {
    var index = state.projects.findIndex(function(item) { return item.id === project.id; });
    if (index >= 0) state.projects[index] = project;
  }

  function dismissWorkspaceContextMenu() {
    var menu = document.getElementById('workspaceContextMenu');
    if (menu) menu.hidden = true;
  }

  function showWorkspaceContextMenu(event, items) {
    if (!items || !items.length) return;
    event.preventDefault();
    var menu = document.getElementById('workspaceContextMenu');
    if (!menu) return;
    menu.replaceChildren();
    items.forEach(function(item) {
      var button = document.createElement('button');
      button.type = 'button';
      button.textContent = item.label;
      if (item.danger) button.dataset.danger = 'true';
      button.addEventListener('click', function() {
        dismissWorkspaceContextMenu();
        try {
          Promise.resolve(item.action()).catch(function(error) {
            status(error && error.message ? error.message : 'Workspace action failed.', 'error');
          });
        } catch (error) {
          status(error && error.message ? error.message : 'Workspace action failed.', 'error');
        }
      });
      menu.appendChild(button);
    });
    menu.hidden = false;
    var width = menu.offsetWidth;
    var height = menu.offsetHeight;
    menu.style.left = Math.max(8, Math.min(event.clientX, window.innerWidth - width - 8)) + 'px';
    menu.style.top = Math.max(8, Math.min(event.clientY, window.innerHeight - height - 8)) + 'px';
  }

  function clearSelectedProjectActivity() {
    var project = projectById(state.selectedProjectId);
    if (!project) return;
    state.clearedActivityProjectIds[project.id] = true;
    persistWorkspaceDisplayState();
    renderWorkbench();
    status('Cleared Eva activity for ' + project.name + '.', 'success');
  }

  function showSelectedProjectActivity() {
    var project = projectById(state.selectedProjectId);
    if (!project) return;
    delete state.clearedActivityProjectIds[project.id];
    persistWorkspaceDisplayState();
    renderWorkbench();
    status('Restored Eva activity for ' + project.name + '.', 'success');
  }

  function clearSelectedRunResult() {
    var run = state.runs.find(function(item) { return item.id === state.selectedRunId; });
    if (!run) return;
    state.clearedResultRunIds[run.id] = true;
    persistWorkspaceDisplayState();
    renderWorkbench();
    status('Cleared the displayed result for this coding run.', 'success');
  }

  function clearSelectedProjectRuns() {
    var project = projectById(state.selectedProjectId);
    if (!project) return;
    state.clearedCodingRunProjectIds[project.id] = true;
    persistWorkspaceDisplayState();
    renderWorkbench();
    status('Cleared the coding runs display for ' + project.name + '.', 'success');
  }

  function showSelectedProjectRuns() {
    var project = projectById(state.selectedProjectId);
    if (!project) return;
    delete state.clearedCodingRunProjectIds[project.id];
    persistWorkspaceDisplayState();
    renderWorkbench();
    status('Restored the coding runs display for ' + project.name + '.', 'success');
  }

  function showSelectedRunResult() {
    var run = state.runs.find(function(item) { return item.id === state.selectedRunId; });
    if (!run) return;
    delete state.clearedResultRunIds[run.id];
    persistWorkspaceDisplayState();
    renderWorkbench();
    status('Restored the displayed result for this coding run.', 'success');
  }

  function configureWorkbenchDisplayControl(id, label, title, handler, disabled) {
    var button = document.getElementById(id);
    if (!button) return;
    button.textContent = label;
    button.title = title;
    button.setAttribute('aria-label', title);
    button.disabled = !!disabled;
    button.onclick = handler;
  }

  function bindWorkbenchContextMenus() {
    var workbench = document.getElementById('workspaceWorkbench');
    if (!workbench) return;
    workbench.addEventListener('contextmenu', function(event) {
      var projectButton = event.target.closest('#workspaceWorkbenchProjects .workspace-monitor-run');
      if (projectButton) {
        var project = projectById(projectButton.dataset.projectId);
        if (project) {
          showWorkspaceContextMenu(event, [{
            label: 'Remove workspace',
            danger: true,
            action: function() { removeProject(project); }
          }]);
        }
        return;
      }
      if (event.target.closest('#workspaceMonitorFeed')) {
        if (state.selectedProjectId) {
          var activityCleared = state.clearedActivityProjectIds[state.selectedProjectId] === true;
          showWorkspaceContextMenu(event, [{
            label: activityCleared ? 'Show activity display' : 'Clear activity display',
            action: activityCleared ? showSelectedProjectActivity : clearSelectedProjectActivity
          }]);
        }
        return;
      }
      if (event.target.closest('#workspaceWorkbenchRuns') && state.selectedProjectId) {
        var runsCleared = state.clearedCodingRunProjectIds[state.selectedProjectId] === true;
        showWorkspaceContextMenu(event, [{
          label: runsCleared ? 'Show coding runs' : 'Clear coding runs display',
          action: runsCleared ? showSelectedProjectRuns : clearSelectedProjectRuns
        }]);
        return;
      }
      if (event.target.closest('#workspaceWorkbenchResults') && state.selectedRunId) {
        var resultCleared = state.clearedResultRunIds[state.selectedRunId] === true;
        showWorkspaceContextMenu(event, [{
          label: resultCleared ? 'Show run results' : 'Clear run results display',
          action: resultCleared ? showSelectedRunResult : clearSelectedRunResult
        }]);
      }
    });
    document.addEventListener('pointerdown', function(event) {
      var menu = document.getElementById('workspaceContextMenu');
      if (event.button === 0 && menu && !menu.hidden && !menu.contains(event.target)) dismissWorkspaceContextMenu();
    });
    document.addEventListener('keydown', function(event) {
      if (event.key === 'Escape') dismissWorkspaceContextMenu();
    });
    window.addEventListener('resize', dismissWorkspaceContextMenu);
  }

  function setProjectTerminalTarget(project) {
    var checkout = project && project.sourceCheckout;
    if (!checkout || !checkout.id || typeof setWorkspaceTerminalTarget !== 'function') return;
    setWorkspaceTerminalTarget(checkout.id, project.name + ' | source');
  }

  function buildProjectFileTree(files) {
    var root = { folders: Object.create(null), files: [], fileCount: 0 };
    files.forEach(function(relativePath) {
      var parts = relativePath.split('/').filter(Boolean);
      if (!parts.length) return;
      var node = root;
      node.fileCount += 1;
      parts.slice(0, -1).forEach(function(folderName) {
        if (!node.folders[folderName]) {
          node.folders[folderName] = { folders: Object.create(null), files: [], fileCount: 0 };
        }
        node = node.folders[folderName];
        node.fileCount += 1;
      });
      node.files.push({ name: parts[parts.length - 1], path: relativePath });
    });
    return root;
  }

  function treeNameCompare(left, right) {
    return left.localeCompare(right, undefined, { sensitivity: 'base', numeric: true });
  }

  function renderProjectTreeNode(container, node, project, parentPath, depth) {
    var expanded = state.projectTreeExpanded[project.id] || (state.projectTreeExpanded[project.id] = {});
    Object.keys(node.folders).sort(treeNameCompare).forEach(function(folderName) {
      var folderPath = parentPath ? parentPath + '/' + folderName : folderName;
      var folderNode = node.folders[folderName];
      var details = document.createElement('details');
      details.className = 'workspace-tree-folder';
      details.open = expanded[folderPath] === true;
      var summary = document.createElement('summary');
      summary.className = 'workspace-tree-row workspace-tree-folder-row';
      summary.style.setProperty('--workspace-tree-depth', depth);
      summary.title = folderPath;
      var chevron = document.createElement('span');
      chevron.className = 'workspace-tree-chevron';
      chevron.setAttribute('aria-hidden', 'true');
      chevron.textContent = '>';
      var name = document.createElement('span');
      name.className = 'workspace-tree-name';
      name.textContent = folderName;
      var count = document.createElement('span');
      count.className = 'workspace-tree-count';
      count.textContent = folderNode.fileCount;
      summary.append(chevron, name, count);
      details.appendChild(summary);
      var children = document.createElement('div');
      children.className = 'workspace-tree-children';
      renderProjectTreeNode(children, folderNode, project, folderPath, depth + 1);
      details.appendChild(children);
      details.addEventListener('toggle', function() {
        expanded[folderPath] = details.open;
      });
      container.appendChild(details);
    });
    node.files.sort(function(left, right) { return treeNameCompare(left.name, right.name); }).forEach(function(fileEntry) {
      var file = document.createElement('button');
      file.type = 'button';
      file.className = 'workspace-project-file workspace-tree-row';
      file.style.setProperty('--workspace-tree-depth', depth);
      file.title = 'Open ' + fileEntry.path;
      var marker = document.createElement('span');
      marker.className = 'workspace-tree-file-marker';
      marker.setAttribute('aria-hidden', 'true');
      var name = document.createElement('span');
      name.className = 'workspace-tree-name';
      name.textContent = fileEntry.name;
      file.append(marker, name);
      file.addEventListener('click', async function() {
        try {
          await api().workspaceOpenProjectFile(project.id, fileEntry.path);
          status('Opened ' + fileEntry.path + '.', 'success');
        } catch (openError) {
          status(openError.message || 'Workspace file could not be opened.', 'error');
        }
      });
      container.appendChild(file);
    });
  }

  function renderProjectFiles(container, project) {
    container.replaceChildren();
    if (state.projectFilesLoading[project.id]) {
      var loading = document.createElement('p');
      loading.className = 'workspace-monitor-empty';
      loading.textContent = 'Loading project files...';
      container.appendChild(loading);
      return;
    }
    var result = state.projectFiles[project.id];
    if (!result) {
      var pending = document.createElement('p');
      pending.className = 'workspace-monitor-empty';
      pending.textContent = 'Loading project files...';
      container.appendChild(pending);
      loadProjectFiles(project);
      return;
    }
    if (result.error) {
      var error = document.createElement('p');
      error.className = 'workspace-monitor-empty';
      error.textContent = result.error;
      container.appendChild(error);
      return;
    }
    if (!result.files.length) {
      var empty = document.createElement('p');
      empty.className = 'workspace-monitor-empty';
      empty.textContent = 'No tracked or unignored files.';
      container.appendChild(empty);
      return;
    }
    renderProjectTreeNode(container, buildProjectFileTree(result.files), project, '', 0);
    if (result.truncated) {
      var truncated = document.createElement('p');
      truncated.className = 'workspace-project-files-note';
      truncated.textContent = 'Showing the first 1,000 files.';
      container.appendChild(truncated);
    }
  }

  async function loadProjectFiles(project) {
    if (!project || state.projectFilesLoading[project.id]) return;
    if (!api() || typeof api().workspaceListProjectFiles !== 'function') return;
    state.projectFilesLoading[project.id] = true;
    var container = document.getElementById('workspaceProjectFiles');
    if (container && container.dataset.projectId === project.id) renderProjectFiles(container, project);
    try {
      state.projectFiles[project.id] = await api().workspaceListProjectFiles(project.id);
    } catch (error) {
      state.projectFiles[project.id] = { files: [], truncated: false, error: error.message || 'Project files could not be loaded.' };
    } finally {
      state.projectFilesLoading[project.id] = false;
      container = document.getElementById('workspaceProjectFiles');
      if (container && container.dataset.projectId === project.id) renderProjectFiles(container, project);
    }
  }

  function appendWorkbenchProjectBrowser(detail, project) {
    var section = document.createElement('section');
    section.className = 'workspace-workbench-section';
    var heading = document.createElement('h2');
    heading.textContent = 'PROJECT FILES';
    var actions = document.createElement('div');
    actions.className = 'workspace-monitor-detail-actions';
    var terminal = document.createElement('button');
    terminal.type = 'button';
    terminal.textContent = 'Open project terminal';
    terminal.disabled = !project.sourceCheckout || project.sourceCheckout.lifecycle !== 'active';
    terminal.addEventListener('click', function() {
      openWorkspaceTerminal(project.sourceCheckout.id, project.name + ' | source');
    });
    var remove = document.createElement('button');
    remove.type = 'button';
    remove.textContent = 'Remove workspace';
    remove.addEventListener('click', function() { removeProject(project); });
    actions.append(terminal, remove);
    var files = document.createElement('div');
    files.id = 'workspaceProjectFiles';
    files.className = 'workspace-project-files';
    files.dataset.projectId = project.id;
    section.append(heading, actions, files);
    detail.appendChild(section);
    renderProjectFiles(files, project);
  }

  function appendWorkbenchRunComposer(detail, project) {
    var draft = state.runDrafts[project.id] || (state.runDrafts[project.id] = { objective: '', baseRef: 'HEAD', autoApprove: autoApprovePreference() });
    var section = document.createElement('section');
    section.className = 'workspace-workbench-section';
    var heading = document.createElement('h2');
    heading.textContent = 'NEW CODING RUN';
    var form = document.createElement('form');
    form.className = 'workspace-workbench-run-form';
    var objectiveLabel = document.createElement('label');
    objectiveLabel.textContent = 'OBJECTIVE';
    var objective = document.createElement('textarea');
    objective.rows = 4;
    objective.maxLength = 4000;
    objective.placeholder = 'Describe the change Eva should make';
    objective.required = true;
    objective.value = draft.objective;
    var baseLabel = document.createElement('label');
    baseLabel.textContent = 'BASE REF';
    var baseRef = document.createElement('input');
    baseRef.type = 'text';
    baseRef.maxLength = 256;
    baseRef.value = draft.baseRef || 'HEAD';
    baseRef.autocomplete = 'off';
    var autoApprove = document.createElement('label');
    autoApprove.className = 'workspace-auto-approve';
    var autoApproveInput = document.createElement('input');
    autoApproveInput.type = 'checkbox';
    autoApproveInput.checked = draft.autoApprove === true;
    var autoApproveText = document.createElement('span');
    autoApproveText.textContent = 'Auto approve actions';
    autoApprove.append(autoApproveInput, autoApproveText);
    objective.addEventListener('input', function() { draft.objective = objective.value; });
    baseRef.addEventListener('input', function() { draft.baseRef = baseRef.value; });
    autoApproveInput.addEventListener('change', function() {
      draft.autoApprove = autoApproveInput.checked;
      autoApprovePreference(autoApproveInput.checked);
    });
    var submit = document.createElement('button');
    submit.type = 'submit';
    submit.textContent = 'Start isolated run';
    submit.disabled = state.loading || !supported();
    form.append(objectiveLabel, objective, baseLabel, baseRef, autoApprove, submit);
    form.addEventListener('submit', async function(event) {
      event.preventDefault();
      submit.disabled = true;
      var created = await createWorkspaceRun(project.id, objective.value, baseRef.value, { autoApprove: autoApproveInput.checked });
      if (created) {
        draft.objective = '';
        draft.baseRef = 'HEAD';
        objective.value = '';
        baseRef.value = 'HEAD';
      }
      if (!state.loading) submit.disabled = !supported();
    });
    section.append(heading, form);
    detail.appendChild(section);
  }

  async function setWorkbenchMcpServer(project, server, checkbox) {
    var standalone = api();
    if (!standalone || typeof standalone.workspaceSetMcpServer !== 'function') return;
    var previous = !checkbox.checked;
    if (checkbox.checked) {
      var launch = server.command
        ? 'Command: ' + [server.command].concat(server.args || []).join(' ')
        : 'URL: ' + server.url;
      var environment = server.envKeys && server.envKeys.length
        ? '\nEnvironment keys: ' + server.envKeys.join(', ')
        : '';
      var headers = server.headerKeys && server.headerKeys.length
        ? '\nHeader keys: ' + server.headerKeys.join(', ')
        : '';
      var approved = confirm(
        'Trust this workspace MCP server and allow it to run for coding agents?\n\n' +
        'Server: ' + server.name + '\n' + launch + environment + headers +
        '\n\nEnvironment and header values stay hidden. Any configuration change will revoke this approval.'
      );
      if (!approved) {
        checkbox.checked = false;
        return;
      }
    }
    state.mcpUpdating = true;
    checkbox.disabled = true;
    status('Updating workspace tools...', 'loading');
    try {
      var updated = await standalone.workspaceSetMcpServer(
        project.id, server.name, checkbox.checked, checkbox.checked ? server.digest : ''
      );
      replaceProject(updated);
      renderProjects();
      renderRuns();
      renderWorkbench();
      status('Workspace tools updated.', 'success');
    } catch (error) {
      checkbox.checked = previous;
      status(error.message || 'Workspace tool update failed.', 'error');
    } finally {
      state.mcpUpdating = false;
      if (state.workbenchOpen) renderWorkbench();
    }
  }

  function appendWorkbenchMcpSettings(detail, project) {
    var section = document.createElement('section');
    section.className = 'workspace-workbench-section';
    var heading = document.createElement('h2');
    heading.textContent = 'MCP SERVERS';
    var mcp = project.mcpServers || { source: 'mcp.json', state: 'missing', servers: [] };
    var source = document.createElement('p');
    source.textContent = 'Source: ' + (mcp.source || 'workspace MCP discovery') + ' | workspace-local selection';
    section.append(heading, source);
    if (mcp.state === 'invalid') {
      var invalid = document.createElement('p');
      invalid.textContent = mcp.message || 'The workspace mcp.json is invalid.';
      section.appendChild(invalid);
    } else if (!mcp.servers || !mcp.servers.length) {
      var empty = document.createElement('p');
      empty.textContent = mcp.state === 'missing' ? 'No workspace mcp.json found.' : 'No MCP servers are defined.';
      section.appendChild(empty);
    } else {
      var list = document.createElement('div');
      list.className = 'workspace-mcp-list';
      mcp.servers.forEach(function(server) {
        var row = document.createElement('label');
        row.className = 'workspace-mcp-row';
        var checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.checked = server.enabled === true;
        checkbox.disabled = state.loading || state.mcpUpdating || !api() || typeof api().workspaceSetMcpServer !== 'function';
        var name = document.createElement('strong');
        name.textContent = server.name;
        var transport = document.createElement('span');
        transport.textContent = (server.transport || 'configured') + ' | ' + (server.source || 'mcp.json');
        row.title = server.command
          ? [server.command].concat(server.args || []).join(' ')
          : server.url || server.name;
        checkbox.addEventListener('change', function() { setWorkbenchMcpServer(project, server, checkbox); });
        row.append(checkbox, name, transport);
        list.appendChild(row);
      });
      section.appendChild(list);
    }
    detail.appendChild(section);
  }

  function renderWorkbench() {
    var projectList = document.getElementById('workspaceWorkbenchProjects');
    var runList = document.getElementById('workspaceWorkbenchRuns');
    var feed = document.getElementById('workspaceMonitorFeed');
    var results = document.getElementById('workspaceWorkbenchResults');
    var detail = document.getElementById('workspaceWorkbenchDetail');
    if (!projectList || !runList || !feed || !results || !detail) return;
    projectList.replaceChildren();
    if (!state.projects.length) {
      var emptyProject = document.createElement('p');
      emptyProject.className = 'workspace-monitor-empty';
      emptyProject.textContent = 'No workspaces';
      projectList.appendChild(emptyProject);
    }
    if (!state.selectedProjectId || !projectById(state.selectedProjectId)) {
      state.selectedProjectId = state.projects.length ? state.projects[0].id : '';
    }
    state.projects.forEach(function(project) {
      var button = document.createElement('button');
      button.type = 'button';
      button.className = 'workspace-monitor-run';
      button.dataset.projectId = project.id;
      if (project.id === state.selectedProjectId) button.classList.add('active');
      var title = document.createElement('strong');
      title.textContent = project.name;
      var available = ((project.mcpServers || {}).servers || []).length;
      var enabled = ((project.mcpServers || {}).servers || []).filter(function(server) { return server.enabled; }).length;
      var meta = document.createElement('span');
      meta.textContent = (project.activeRunCount || 0) + ' active | ' + available + ' MCP available | ' + enabled + ' enabled';
      button.append(title, meta);
      button.addEventListener('click', function() { selectProject(project.id); });
      projectList.appendChild(button);
    });

    runList.replaceChildren();
    var projectRuns = state.runs.filter(function(run) {
      return run.status !== 'discarded' && run.projectId === state.selectedProjectId;
    });
    var orderedRuns = state.clearedCodingRunProjectIds[state.selectedProjectId] ? [] : projectRuns;
    if (!orderedRuns.length) {
      var empty = document.createElement('p');
      empty.className = 'workspace-monitor-empty';
      empty.textContent = state.selectedProjectId && state.clearedCodingRunProjectIds[state.selectedProjectId]
        ? 'Coding runs display cleared. Right-click here to show it again.'
        : state.selectedProjectId ? 'No coding runs' : 'Select a workspace';
      runList.appendChild(empty);
    }
    configureWorkbenchDisplayControl(
      'workspaceRunsDisplayBtn',
      state.clearedCodingRunProjectIds[state.selectedProjectId] ? 'SHOW' : 'CLEAR',
      state.clearedCodingRunProjectIds[state.selectedProjectId] ? 'Show coding runs' : 'Clear coding runs display',
      state.clearedCodingRunProjectIds[state.selectedProjectId] ? showSelectedProjectRuns : clearSelectedProjectRuns,
      !state.selectedProjectId || (!state.clearedCodingRunProjectIds[state.selectedProjectId] && !projectRuns.length)
    );
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
    var selectedProject = projectById(state.selectedProjectId);
    var activityTitle = document.getElementById('workspaceMonitorActivityTitle');
    if (activityTitle) activityTitle.textContent = selectedProject ? 'EVA ACTIVITY: ' + selectedProject.name : 'EVA ACTIVITY';
    var activityCleared = state.clearedActivityProjectIds[state.selectedProjectId] === true;
    var projectActivity = activityCleared ? [] : state.monitorActivity.filter(function(entry) {
      return entry.projectId === state.selectedProjectId;
    });
    if (!projectActivity.length) {
      var emptyActivity = document.createElement('li');
      emptyActivity.className = 'workspace-monitor-empty';
      emptyActivity.textContent = activityCleared
        ? 'Activity display cleared. Click Show to restore it.'
        : selectedProject ? 'No Eva activity recorded for this workspace.' : 'Select a workspace to view Eva activity.';
      feed.appendChild(emptyActivity);
    }
    projectActivity.forEach(function(entry) {
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
    configureWorkbenchDisplayControl(
      'workspaceActivityDisplayBtn',
      activityCleared ? 'SHOW' : 'CLEAR',
      activityCleared ? 'Show activity display' : 'Clear activity display',
      activityCleared ? showSelectedProjectActivity : clearSelectedProjectActivity,
      !selectedProject || (!activityCleared && !projectActivity.length)
    );

    detail.replaceChildren();
    var project = selectedProject;
    if (!project) {
      var unavailable = document.createElement('p');
      unavailable.className = 'workspace-monitor-empty';
      unavailable.textContent = 'Import a local Git workspace to begin.';
      detail.appendChild(unavailable);
    } else {
      setProjectTerminalTarget(project);
      appendWorkbenchProjectBrowser(detail, project);
      appendWorkbenchRunComposer(detail, project);
      appendWorkbenchMcpSettings(detail, project);
    }
    var selected = projectRuns.find(function(run) { return run.id === state.selectedRunId; }) || projectRuns[0];
    var resultCleared = !!(selected && state.clearedResultRunIds[selected.id]);
    configureWorkbenchDisplayControl(
      'workspaceResultsDisplayBtn',
      resultCleared ? 'SHOW' : 'CLEAR',
      resultCleared ? 'Show run results' : 'Clear run results display',
      resultCleared ? showSelectedRunResult : clearSelectedRunResult,
      !selected
    );
    results.replaceChildren();
    if (resultCleared) {
      var clearedResults = document.createElement('p');
      clearedResults.className = 'workspace-monitor-empty';
      clearedResults.textContent = 'Run result display cleared. Select the coding run again to restore it.';
      results.appendChild(clearedResults);
    } else if (selected) {
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
      if (selected.status === 'active' && (!selected.agent || selected.agent.status === 'error')) {
        var retryButton = document.createElement('button');
        retryButton.type = 'button';
        retryButton.textContent = 'Retry run';
        retryButton.addEventListener('click', function() { retryWorkspaceRun(selected); });
        actions.appendChild(retryButton);
      }
      if (selected.primarySessionId && typeof loadSession === 'function') {
        var chatButton = document.createElement('button');
        chatButton.type = 'button';
        chatButton.textContent = 'Open chat';
        chatButton.addEventListener('click', function() { loadSession(selected.primarySessionId); closeWorkbench(); });
        actions.appendChild(chatButton);
      }
      var runSection = document.createElement('section');
      runSection.className = 'workspace-workbench-section';
      runSection.append(heading, branch, facts, actions);
      if (selected.agent && selected.agent.report) {
        var report = document.createElement('pre');
        report.className = 'workspace-monitor-report';
        report.textContent = selected.agent.report;
        runSection.appendChild(report);
      }
      results.appendChild(runSection);
      appendWorkspacePermissions(results, selected);
    } else {
      var emptyResults = document.createElement('p');
      emptyResults.className = 'workspace-monitor-empty';
      emptyResults.textContent = 'Select a coding run to view its result.';
      results.appendChild(emptyResults);
    }

    var active = activeRuns();
    var terminals = state.lastTerminals || [];
    var dirty = active.reduce(function(total, run) { return total + Number(run.checkout && run.checkout.dirtyFileCount || 0); }, 0);
    var values = {
      workspaceMonitorProjectCount: state.projects.length,
      workspaceMonitorActiveRuns: active.length,
      workspaceMonitorTerminalCount: terminals.filter(function(item) { return !item.exited; }).length,
      workspaceMonitorDirtyCount: dirty
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
      var previousPermissionIds = state.pendingPermissions.map(function(permission) { return permission.id; });
      var workspacePermissionRelevant = runs.some(function(run) { return run.status === 'active'; }) || state.pendingPermissions.length > 0;
      if (workspacePermissionRelevant && typeof backgroundBridgeRequest === 'function' && typeof getBridgeCapabilityHeaders === 'function') {
        try {
          var permissionData = await backgroundBridgeRequest('/v1/acp/permissions', { headers: getBridgeCapabilityHeaders() });
          state.pendingPermissions = (permissionData.permissions || []).map(function(permission) {
            return {
              id: permission.id,
              workspaceRunId: permission.workspace_run_id || '',
              toolKind: permission.tool_kind || '',
              commandSummary: permission.command_summary || '',
              approvalAllowed: permission.approval_allowed !== false,
              options: permission.options || []
            };
          }).filter(function(permission) { return permission.workspaceRunId; });
        } catch (_) {}
      }
      var permissionSignature = JSON.stringify(state.pendingPermissions.map(function(permission) {
        return [permission.id, permission.workspaceRunId, permission.toolKind, (permission.options || []).map(function(option) { return option.option_id || ''; })];
      }));
      var permissionsChanged = permissionSignature !== state.permissionSignature;
      if (permissionsChanged) state.permissionSignature = permissionSignature;
      state.pendingPermissions.forEach(function(permission) {
        if (previousPermissionIds.indexOf(permission.id) >= 0) return;
        var permissionRun = runs.find(function(run) { return run.id === permission.workspaceRunId; });
        if (!permissionRun) return;
        var permissionMessage = 'Eva paused "' + permissionRun.objective + '" because a sensitive or composed action needs approval. Review it in Workspaces.';
        addMonitorActivity(permissionMessage, 'error', true, true, permissionRun);
        publishRunChat(permissionRun, permissionMessage, 'error');
      });
      var signature = monitorSignature(runs, terminals, state.projects);
      var changed = signature !== state.monitorSignature;
      var shouldRender = changed || permissionsChanged;
      state.runs = runs;
      pruneWorkspaceDisplayState();
      state.lastTerminals = terminals;
      state.lastCheckedAt = Date.now();
      narrateRunChanges(state.runs);
      if (changed) {
        state.monitorSignature = signature;
        addMonitorActivity(monitorSummary(), 'change', true);
      } else if (activeRuns().length && Date.now() - state.lastPeriodicNoteAt >= 300000) {
        state.lastPeriodicNoteAt = Date.now();
        addMonitorActivity(monitorSummary(), 'heartbeat', true);
        shouldRender = true;
      }
      if (state.workbenchOpen && shouldRender) renderWorkbench();
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
    if (typeof closeAgentOperationsForNavigation === 'function') closeAgentOperationsForNavigation();
    if (window.EvaAssets && typeof window.EvaAssets.close === 'function') window.EvaAssets.close();
    if (window.EvaSkills && typeof window.EvaSkills.close === 'function') window.EvaSkills.close();
    if (typeof closeSidePanels === 'function') closeSidePanels();
    state.workbenchOpen = true;
    document.body.classList.add('workspace-workbench-open');
    var view = document.getElementById('workspaceWorkbench');
    if (view) view.setAttribute('aria-hidden', 'false');
    setChatDrawerOpen(chatDrawerPreference(), false);
    if (!supported()) {
      status('Coding workspaces are unavailable in this Eva launch.', 'error');
      renderWorkbench();
      return;
    }
    monitor();
    renderWorkbench();
  }

  function closeWorkbench() {
    dismissWorkspaceContextMenu();
    setChatDrawerOpen(false, false);
    restoreChatNodes();
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
      pruneWorkspaceDisplayState();
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

  async function describeCurrent() {
    if (!supported()) throw new Error('Coding workspaces are unavailable in this Eva launch.');
    var results = await Promise.all([api().workspaceListProjects(), api().workspaceListRuns()]);
    state.projects = Array.isArray(results[0]) ? results[0] : [];
    state.runs = Array.isArray(results[1]) ? results[1] : [];
    renderProjects();
    renderRuns();
    renderDetail();
    if (state.workbenchOpen) renderWorkbench();
    if (!state.projects.length) return 'There are no coding workspaces imported right now.';
    var names = state.projects.slice(0, 8).map(function(project) {
      var active = Number(project.activeRunCount || 0);
      var enabledTools = ((project.mcpServers || {}).servers || []).filter(function(server) { return server.enabled; }).length;
      return project.name + (active ? ' with ' + active + ' active run' + (active === 1 ? '' : 's') : '') + (enabledTools ? ' and ' + enabledTools + ' enabled workspace tool' + (enabledTools === 1 ? '' : 's') : '');
    });
    var remaining = state.projects.length - names.length;
    var activeRuns = state.runs.filter(function(run) { return run.status === 'active' || (run.agent && ['starting', 'waiting', 'running', 'steering', 'finalizing'].indexOf(run.agent.status) !== -1); }).length;
    return 'I can access ' + state.projects.length + ' coding workspace' + (state.projects.length === 1 ? '' : 's') + ': ' + names.join(', ') + (remaining > 0 ? ', and ' + remaining + ' more' : '') + '. There ' + (activeRuns === 1 ? 'is 1 active coding run' : 'are ' + activeRuns + ' active coding runs') + '.';
  }

  async function describeProjectTools(projectName) {
    if (!supported()) throw new Error('Coding workspaces are unavailable in this Eva launch.');
    var projects = await api().workspaceListProjects();
    state.projects = Array.isArray(projects) ? projects : [];
    var query = String(projectName || '').trim().toLowerCase();
    var project = query
      ? state.projects.filter(function(item) { return String(item.name || '').trim().toLowerCase() === query; })[0]
      : projectById(state.selectedProjectId);
    if (!project) throw new Error(query ? 'No imported workspace matched "' + projectName + '".' : 'Select an imported workspace before checking its enabled tools.');
    state.selectedProjectId = project.id;
    var enabled = ((project.mcpServers || {}).servers || []).filter(function(server) {
      return server.enabled === true;
    }).map(function(server) {
      return String(server.name || '').trim();
    }).filter(Boolean);
    if (!enabled.length) return 'No workspace MCP tools are enabled for ' + project.name + '.';
    return project.name + ' has ' + enabled.length + ' enabled workspace MCP tool' + (enabled.length === 1 ? ': ' : 's: ') + enabled.join(', ') + '.';
  }

  function mcpContext() {
    var modules = [];
    state.projects.forEach(function(project) {
      ((project.mcpServers || {}).servers || []).forEach(function(server) {
        modules.push({
          project: String(project.name || '').slice(0, 120),
          module: String(server.name || '').slice(0, 120),
          source: String(server.source || 'mcp.json').slice(0, 200),
          enabled: server.enabled === true
        });
      });
    });
    if (!modules.length) return '';
    return 'WORKSPACE MCP MODULE SNAPSHOT (safe metadata only):\n' + modules.slice(0, 64).map(function(module) {
      return '- project=' + module.project + '; module=' + module.module + '; source=' + module.source + '; enabled=' + module.enabled;
    }).join('\n') + '\nUse a workspace-scoped verification run for enabled modules. Do not treat these as global MCP servers.';
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

  async function importGitHubProject(repositoryUrl, forcePrompt, authorizationRetried) {
    if (!supported() || state.loading) return;
    repositoryUrl = typeof repositoryUrl === 'string' ? repositoryUrl.trim() : '';
    if (!api() || typeof api().workspaceImportGitHub !== 'function') {
      status('GitHub workspace import is unavailable in this Eva build.', 'error');
      return;
    }
    if (forcePrompt) {
      if (typeof evaTextPrompt !== 'function') {
        status('GitHub workspace import prompt is unavailable in this Eva build.', 'error');
        return;
      }
      repositoryUrl = await evaTextPrompt('GitHub repository URL', repositoryUrl, {
        maxLength: 2048,
        placeholder: 'https://github.com/owner/repository'
      });
      if (repositoryUrl === null || !String(repositoryUrl).trim()) {
        status('GitHub workspace import cancelled.', '');
        return;
      }
      repositoryUrl = String(repositoryUrl).trim();
    }
    while (!repositoryUrl) {
      if (typeof evaTextPrompt !== 'function') {
        status('GitHub workspace import prompt is unavailable in this Eva build.', 'error');
        return;
      }
      repositoryUrl = await evaTextPrompt('GitHub repository URL', '', {
        maxLength: 2048,
        placeholder: 'https://github.com/owner/repository'
      });
      if (repositoryUrl === null || !String(repositoryUrl).trim()) {
        status('GitHub workspace import cancelled.', '');
        return;
      }
      repositoryUrl = String(repositoryUrl).trim();
    }
    while (repositoryUrl) {
      setBusy(true);
      status('Importing GitHub workspace...', 'loading');
      try {
        var importResult = await api().workspaceImportGitHub(repositoryUrl);
        if (importResult && importResult.error) throw new Error(importResult.error);
        var project = importResult;
        if (!project || !project.id) throw new Error('GitHub workspace import returned an invalid project.');
        if (project) state.selectedProjectId = project.id;
        await refresh();
        status('GitHub workspace imported.', 'success');
        if (typeof _vvIsActive === 'function' && _vvIsActive() && typeof speakText === 'function') {
          speakText('GitHub workspace imported.');
        }
        return project;
      } catch (error) {
        var message = error.message || 'GitHub workspace import failed.';
        status(message, 'error');
        setBusy(false);
        var authorizationRequired = /GitHub (?:authentication was rejected|denied access to this repository)/i.test(message);
        if (authorizationRequired && !authorizationRetried) {
          var authorizationStarted = await authorizeGitHub({ repositoryUrl: repositoryUrl, retried: true });
          if (authorizationStarted) return;
        }
        if (typeof _vvIsActive === 'function' && _vvIsActive() && typeof speakText === 'function') {
          speakText(message + ' The URL is back in the prompt so you can correct it.');
        }
        if (typeof evaTextPrompt !== 'function') return;
        repositoryUrl = await evaTextPrompt('Correct GitHub repository URL', repositoryUrl, {
          maxLength: 2048,
          placeholder: 'https://github.com/owner/repository'
        });
        if (repositoryUrl === null || !String(repositoryUrl).trim()) {
          status('GitHub workspace import cancelled.', '');
          return;
        }
        repositoryUrl = String(repositoryUrl).trim();
      }
    }
  }

  async function listGitHubRepositories() {
    if (!supported()) throw new Error('Coding workspaces are unavailable in this Eva launch.');
    if (!api() || typeof api().workspaceListGitHubRepositories !== 'function') throw new Error('GitHub repository listing is unavailable in this Eva build.');
    status('Loading GitHub repositories...', 'loading');
    try {
      var repositories = await api().workspaceListGitHubRepositories();
      repositories = Array.isArray(repositories) ? repositories : [];
      state.githubRepositories = repositories;
      state.githubRepositoriesCollapsed = false;
      renderGitHubRepositories();
      if (!repositories.length) return 'No owned GitHub repositories were returned.';
      var summary = repositories.map(function(repository) {
        return repository.fullName + (repository.private ? ' (private)' : '') + ' - ' + repository.url;
      }).join('\n');
      status('Loaded ' + repositories.length + ' GitHub repositories.', 'success');
      return 'Available GitHub repositories:\n' + summary + '\n\nChoose one URL and ask Eva to import that exact repository.';
    } finally {
      if (state.loading) setBusy(false);
    }
  }

  async function continueGitHubRepositories() {
    if (!state.githubRepositories.length) await listGitHubRepositories();
    if (!state.githubRepositories.length) return 'No owned GitHub repositories were returned.';
    state.githubRepositoriesCollapsed = false;
    renderGitHubRepositories();
    status('GitHub repositories are listed. Name the repository you want to import.', 'success');
    return 'GitHub repositories are listed in Workspaces. Name the repository you want to import.';
  }

  function showGitHubAuthState(authState) {
    var stateValue = authState && authState.state || 'failed';
    var device = document.getElementById('authGitHubDevice');
    var deviceCode = document.getElementById('authGitHubDeviceCode');
    var authStatus = document.getElementById('authGitHubCliStatus');
    var pending = stateValue === 'pending' && authState && authState.code;
    if (device) device.hidden = !pending;
    if (deviceCode) deviceCode.textContent = pending ? authState.code : '';
    if (authStatus) authStatus.textContent = authState && authState.message || '';
    if (stateValue === 'starting') {
      status(authState.message || 'Starting GitHub device authorization...', 'loading');
      return;
    }
    if (stateValue === 'pending') {
      status('Authorize GitHub at ' + authState.url + ' with code ' + authState.code + '.', 'loading');
      return;
    }
    if (stateValue === 'complete') {
      status(authState.message || 'GitHub authorization complete.', 'success');
      return;
    }
    if (stateValue === 'failed') status(authState.message || 'GitHub authorization failed.', 'error');
  }

  async function authorizeGitHub(retry) {
    if (!supported() || !api() || typeof api().workspaceGitHubAuthStart !== 'function') {
      status('GitHub CLI authorization is unavailable in this Eva build.', 'error');
      return false;
    }
    if (state.githubAuthTimer) clearInterval(state.githubAuthTimer);
    state.githubAuthRetry = retry && retry.repositoryUrl ? retry : null;
    setBusy(true);
    status('Starting GitHub device authorization...', 'loading');
    try {
      var authState = await api().workspaceGitHubAuthStart();
      showGitHubAuthState(authState);
      if (!authState || ['complete', 'failed'].indexOf(authState.state) >= 0) {
        setBusy(false);
        if (authState && authState.state === 'complete') await finishGitHubAuthorization();
        return !!(authState && authState.state === 'complete');
      }
      state.githubAuthTimer = setInterval(async function() {
        try {
          var updated = await api().workspaceGitHubAuthStatus();
          showGitHubAuthState(updated);
          if (updated && ['complete', 'failed'].indexOf(updated.state) >= 0) {
            clearInterval(state.githubAuthTimer);
            state.githubAuthTimer = null;
            setBusy(false);
            if (updated.state === 'complete') await finishGitHubAuthorization();
          }
        } catch (error) {
          clearInterval(state.githubAuthTimer);
          state.githubAuthTimer = null;
          status(error.message || 'GitHub authorization status failed.', 'error');
          setBusy(false);
        }
      }, 1000);
      return true;
    } catch (error) {
      status(error.message || 'GitHub authorization failed.', 'error');
      setBusy(false);
      return false;
    }
  }

  async function finishGitHubAuthorization() {
    var retry = state.githubAuthRetry;
    state.githubAuthRetry = null;
    try {
      await listGitHubRepositories();
      if (retry && retry.repositoryUrl) await importGitHubProject(retry.repositoryUrl, false, true);
    } catch (error) {
      status(error.message || 'GitHub authorization refresh failed.', 'error');
    }
  }

  async function importGitHubSelection(repositoryName) {
    var query = String(repositoryName || '').trim().toLowerCase();
    if (!query) throw new Error('Name the GitHub repository to import.');
    if (!state.githubRepositories.length) await listGitHubRepositories();
    var matches = state.githubRepositories.filter(function(repository) {
      return String(repository.name || '').toLowerCase() === query || String(repository.fullName || '').toLowerCase() === query;
    });
    if (!matches.length) throw new Error('No listed GitHub repository matched "' + repositoryName + '". List your repositories and use the displayed name.');
    if (matches.length > 1) throw new Error('More than one repository matched. Use the full owner/repository name.');
    return importGitHubProject(matches[0].url);
  }

  async function startRepositoryRemediation(repositoryName, objectiveValue) {
    if (!supported()) throw new Error('Workspace agent execution is unavailable in this Eva launch.');
    var repositoryQuery = String(repositoryName || '').trim().replace(/^https:\/\/github\.com\//i, '').replace(/\.git$/i, '').toLowerCase();
    if (!/^[a-z0-9_.-]+(?:\/[a-z0-9_.-]+)?$/i.test(repositoryQuery)) {
      throw new Error('Use an exact GitHub repository name such as repository or owner/repository for remediation.');
    }
    var objective = String(objectiveValue || '').trim();
    if (!objective) throw new Error('Describe the remediation objective first.');
    if (!state.projects.length) await refresh();
    var project = state.projects.filter(function(item) {
      return String(item.name || '').trim().toLowerCase() === repositoryQuery;
    })[0];
    if (!project) {
      if (!state.githubRepositories.length) await listGitHubRepositories();
      var repositories = state.githubRepositories.filter(function(repository) {
        return String(repository.fullName || '').trim().toLowerCase() === repositoryQuery ||
          String(repository.name || '').trim().toLowerCase() === repositoryQuery;
      });
      if (repositories.length !== 1) {
        throw new Error('GitHub repository "' + repositoryName + '" was not available from the authenticated repository list.');
      }
      project = await importGitHubProject(repositories[0].url);
    }
    if (!project || !project.id) throw new Error('GitHub workspace import did not return a project.');
    state.selectedProjectId = project.id;
    var run = await createWorkspaceRun(project.id, objective, 'HEAD', { autoApprove: true, throwOnError: true });
    try {
      localStorage.setItem('eva_last_repository_remediation', JSON.stringify({
        repositoryName: project.name,
        objective: objective
      }));
    } catch (_) {}
    return {
      runId: run.id,
      projectName: project.name,
      dispatchError: run.dispatchError || '',
      message: run.dispatchError
        ? 'Created Workspace run ' + run.id + ' for ' + project.name + ', but dispatch is delayed: ' + run.dispatchError
        : 'Started Workspace run ' + run.id + ' for ' + project.name + '.'
    };
  }

  async function removeProject(project) {
    if (!project || !api() || typeof api().workspaceDeleteProject !== 'function') throw new Error('Workspace removal is unavailable in this Eva build.');
    var approved = confirm('Remove ' + project.name + ' from Eva?\n\nEva will remove managed coding-run worktrees and history. The source repository will remain on disk.');
    if (!approved) return null;
    setBusy(true);
    status('Removing workspace...', 'loading');
    try {
      var removed;
      try {
        removed = await api().workspaceDeleteProject(project.id, false);
      } catch (error) {
        if (!/local changes|dirty cleanup/i.test(String(error && error.message || ''))) throw error;
        var force = confirm('Managed run worktrees contain local changes. Remove those managed worktrees anyway?\n\nThe source repository will still be preserved.');
        if (!force) return null;
        removed = await api().workspaceDeleteProject(project.id, true);
      }
      delete state.projectFiles[project.id];
      delete state.runDrafts[project.id];
      state.selectedProjectId = '';
      state.selectedRunId = '';
      await refresh();
      status('Workspace removed. Source repository preserved.', 'success');
      return removed;
    } finally {
      setBusy(false);
    }
  }

  async function removeProjectByName(projectName) {
    if (!state.projects.length) await refresh();
    var query = String(projectName || '').trim().toLowerCase();
    var project = null;
    if (query) {
      project = state.projects.filter(function(item) { return String(item.name || '').trim().toLowerCase() === query; })[0] || null;
      if (!project) {
        var basename = query.split('/').filter(Boolean).pop();
        var basenameMatches = state.projects.filter(function(item) {
          return String(item.name || '').trim().toLowerCase().split('/').filter(Boolean).pop() === basename;
        });
        if (basenameMatches.length > 1) throw new Error('More than one imported workspace matched "' + projectName + '". Use the full owner/repository name.');
        project = basenameMatches[0] || null;
      }
    } else {
      project = projectById(state.selectedProjectId);
    }
    if (!project) throw new Error(query ? 'No imported workspace matched "' + projectName + '".' : 'Select an imported workspace before removing it.');
    var removed = await removeProject(project);
    return removed ? 'Removed ' + project.name + ' from Eva. The source repository was preserved.' : 'Workspace removal was cancelled.';
  }

  async function setProjectMcpServerByName(serverName, enabled, projectName) {
    var standalone = api();
    if (!standalone || typeof standalone.workspaceSetMcpServer !== 'function') throw new Error('Workspace MCP controls are unavailable in this Eva build.');
    if (!state.projects.length) await refresh();
    var projectQuery = String(projectName || '').trim().toLowerCase();
    var project = projectQuery
      ? state.projects.filter(function(item) { return String(item.name || '').toLowerCase() === projectQuery; })[0]
      : projectById(state.selectedProjectId);
    if (!project) throw new Error(projectQuery ? 'No imported workspace matched "' + projectName + '".' : 'Select an imported workspace before enabling its MCP server.');
    var serverQuery = String(serverName || '').trim().toLowerCase();
    var matches = ((project.mcpServers || {}).servers || []).filter(function(server) {
      return String(server.name || '').toLowerCase() === serverQuery;
    });
    if (matches.length !== 1) throw new Error('No workspace MCP server named "' + serverName + '" was found for ' + project.name + '.');
    var server = matches[0];
    var updated = await standalone.workspaceSetMcpServer(project.id, server.name, enabled === true, enabled === true ? server.digest : '');
    replaceProject(updated);
    state.selectedProjectId = updated.id;
    renderProjects();
    renderRuns();
    if (state.workbenchOpen) renderWorkbench();
    status(enabled === true ? 'Workspace MCP server enabled for future coding runs.' : 'Workspace MCP server disabled.', 'success');
    return enabled === true
      ? 'Enabled workspace MCP server ' + server.name + ' for ' + updated.name + '.'
      : 'Disabled workspace MCP server ' + server.name + ' for ' + updated.name + '.';
  }

  async function verifyProjectMcpServerByName(serverName, projectName) {
    if (!state.projects.length) await refresh();
    var projectQuery = String(projectName || '').trim().toLowerCase();
    var project = projectQuery
      ? state.projects.filter(function(item) { return String(item.name || '').toLowerCase() === projectQuery; })[0]
      : projectById(state.selectedProjectId);
    if (!project) throw new Error(projectQuery ? 'No imported workspace matched "' + projectName + '".' : 'Select an imported workspace before verifying its MCP server.');
    var serverQuery = normalizeWorkspaceMcpName(serverName);
    var matches = ((project.mcpServers || {}).servers || []).filter(function(server) {
      return normalizeWorkspaceMcpName(server.name) === serverQuery;
    });
    if (matches.length !== 1) throw new Error('No workspace MCP server named "' + serverName + '" was found for ' + project.name + '.');
    if (!matches[0].enabled) throw new Error('Enable workspace MCP server ' + matches[0].name + ' before verifying it.');
    var created = await createWorkspaceRun(
      project.id,
      'Verify that workspace MCP server ' + matches[0].name + ' is registered and reachable. Use that module only as needed, make no external changes, and report the result.',
      'HEAD'
    );
    if (!created) throw new Error('Could not start the workspace MCP verification run.');
    return 'Started an isolated workspace run to verify MCP server ' + matches[0].name + ' for ' + project.name + '.';
  }

  function normalizeWorkspaceMcpName(value) {
    return String(value || '').toLowerCase().replace(/[^a-z0-9]/g, '');
  }

  function collapseGitHubRepositories() {
    state.githubRepositoriesCollapsed = true;
    renderGitHubRepositories();
  }

  function renderGitHubRepositories() {
    var container = document.getElementById('workspaceGitHubRepositories');
    var collapse = document.getElementById('workspaceCollapseGitHubBtn');
    if (!container) return;
    container.innerHTML = '';
    if (!state.githubRepositories.length || state.githubRepositoriesCollapsed) {
      container.hidden = true;
      if (collapse) collapse.hidden = true;
      return;
    }
    state.githubRepositories.forEach(function(repository) {
      var row = document.createElement('div');
      row.className = 'workspace-github-repository';
      var name = document.createElement('span');
      name.className = 'workspace-github-repository-name';
      name.textContent = repository.fullName + (repository.private ? ' (private)' : '');
      var importButton = document.createElement('button');
      importButton.type = 'button';
      importButton.className = 'workspace-monitor-btn';
      importButton.textContent = 'Import';
      importButton.title = repository.url;
      importButton.addEventListener('click', function() { importGitHubProject(repository.url); });
      row.appendChild(name);
      row.appendChild(importButton);
      container.appendChild(row);
    });
    container.hidden = false;
    if (collapse) collapse.hidden = false;
  }

  async function createWorkspaceRun(projectId, objectiveValue, baseRefValue, options) {
    if (!supported() || state.loading) return;
    var project = projectById(projectId);
    if (!project) {
      status('Eva is preparing the ready workspace. Refresh in a moment.', 'error');
      return false;
    }
    var objective = String(objectiveValue || '').trim();
    if (!objective) {
      status('Describe the coding run first.', 'error');
      return false;
    }
    setBusy(true);
    status('Creating isolated worktree...', 'loading');
    try {
      var autoApprove = options && typeof options.autoApprove === 'boolean'
        ? options.autoApprove
        : autoApprovePreference();
      var primarySessionId = typeof _activeSessionId === 'function' ? _activeSessionId() : '';
      var run = await api().workspaceCreateRun({
        projectId: project.id,
        objective: objective,
        primarySessionId: primarySessionId,
        baseRef: String(baseRefValue || '').trim() || 'HEAD',
        autoApprove: autoApprove
      });
      state.selectedProjectId = run.projectId;
      state.selectedRunId = run.id;
      await refresh();
      status(run.dispatchError ? 'Workspace ready; agent dispatch delayed: ' + run.dispatchError : 'Workspace agent dispatched.', run.dispatchError ? 'error' : 'success');
      return run;
    } catch (error) {
      status(error.message || 'Could not create coding run.', 'error');
      setBusy(false);
      if (options && options.throwOnError) throw error;
      return false;
    }
  }

  async function runSelectedCheck(objectiveValue) {
    if (!supported()) throw new Error('Workspace agent execution is unavailable in this Eva launch.');
    if (!state.projects.length) await refresh();
    var project = projectById(state.selectedProjectId);
    if (!project) throw new Error('Select an imported workspace before running project checks.');
    var objective = String(objectiveValue || '').trim();
    if (!objective) throw new Error('Describe the project check to run.');
    var run = await createWorkspaceRun(project.id, objective, 'HEAD', { throwOnError: true });
    if (run.dispatchError) {
      if (typeof api().workspaceDispatchRun === 'function') {
        try {
          await new Promise(function(resolve) { setTimeout(resolve, 500); });
          run = await api().workspaceDispatchRun(run.id);
          await refresh();
        } catch (_) {}
      }
    }
    if (!run.agent || ['starting', 'running'].indexOf(run.agent.status) === -1) {
      return {
        outcome: 'delayed', reason: 'runner_unavailable', runId: run.id,
        message: 'Created a workspace-scoped run for ' + project.name + ', but the local agent is temporarily unavailable. The run is ready to retry in Workspaces.'
      };
    }
    return {
      outcome: 'started', reason: '', runId: run.id,
      message: 'Started a workspace-scoped agent run for ' + project.name + '. Progress and results will appear in Workspaces.'
    };
  }

  async function createRun(event) {
    event.preventDefault();
    var projectSelect = document.getElementById('workspaceProjectSelect');
    var objective = document.getElementById('workspaceObjective');
    var baseRef = document.getElementById('workspaceBaseRef');
    var autoApprove = document.getElementById('workspaceAutoApprove');
    var created = await createWorkspaceRun(
      projectSelect ? projectSelect.value : '',
      objective ? objective.value : '',
      baseRef ? baseRef.value : 'HEAD',
      { autoApprove: !!(autoApprove && autoApprove.checked) }
    );
    if (created && objective) objective.value = '';
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
    var workbenchAddProject = document.getElementById('workspaceAddProjectWorkbenchBtn');
    var workbenchGitHubImport = document.getElementById('workspaceImportGitHubBtn');
    var workbenchGitHubList = document.getElementById('workspaceListGitHubBtn');
    var workbenchGitHubAuth = document.getElementById('authGitHubCliBtn');
    var githubCopyCode = document.getElementById('authGitHubCopyCodeBtn');
    var workbenchGitHubCollapse = document.getElementById('workspaceCollapseGitHubBtn');
    var monitorRefresh = document.getElementById('workspaceMonitorRefreshBtn');
    var monitorClose = document.getElementById('workspaceMonitorCloseBtn');
    var monitorNew = document.getElementById('workspaceMonitorNewBtn');
    var chatToggle = document.getElementById('workspaceChatToggleBtn');
    var chatClose = document.getElementById('workspaceChatCloseBtn');
    var chatSessionSelect = document.getElementById('workspaceChatSessionSelect');
    bindWorkbenchContextMenus();
    if (close) close.addEventListener('click', toggle);
    if (add) add.addEventListener('click', addProject);
    if (refreshButton) refreshButton.addEventListener('click', refresh);
    if (form) form.addEventListener('submit', createRun);
    if (projectSelect) projectSelect.addEventListener('change', function() { selectProject(projectSelect.value); });
    if (openWorkbenchButton) openWorkbenchButton.addEventListener('click', openWorkbench);
    if (workbenchAddProject) workbenchAddProject.addEventListener('click', addProject);
    if (workbenchGitHubImport) workbenchGitHubImport.addEventListener('click', importGitHubProject);
    if (workbenchGitHubAuth) workbenchGitHubAuth.addEventListener('click', authorizeGitHub);
    if (githubCopyCode) githubCopyCode.addEventListener('click', function() {
      var code = document.getElementById('authGitHubDeviceCode');
      if (!code || !code.textContent || !navigator.clipboard) return;
      navigator.clipboard.writeText(code.textContent).then(function() {
        status('GitHub device code copied.', 'success');
      }).catch(function() {
        status('Could not copy the GitHub device code.', 'error');
      });
    });
    if (workbenchGitHubCollapse) workbenchGitHubCollapse.addEventListener('click', collapseGitHubRepositories);
    if (workbenchGitHubList) workbenchGitHubList.addEventListener('click', function() {
      listGitHubRepositories().catch(function(error) { status(error.message || 'GitHub repository listing failed.', 'error'); });
    });
    if (monitorRefresh) monitorRefresh.addEventListener('click', monitor);
    if (monitorClose) monitorClose.addEventListener('click', closeWorkbench);
    if (chatToggle) chatToggle.addEventListener('click', function() { setChatDrawerOpen(!state.chatDrawerOpen); });
    if (chatClose) chatClose.addEventListener('click', function() { setChatDrawerOpen(false); });
    if (chatSessionSelect) chatSessionSelect.addEventListener('change', function() { switchChatSession(chatSessionSelect.value); });
    document.addEventListener('pointerdown', hideChatDrawerOnOutsidePointer);
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
      var autoApprove = document.getElementById('workspaceAutoApprove');
      if (autoApprove) {
        autoApprove.checked = autoApprovePreference();
        autoApprove.addEventListener('change', function() { autoApprovePreference(autoApprove.checked); });
      }
      monitor();
      state.monitorTimer = setInterval(monitor, 10000);
    }
  }

  document.addEventListener('DOMContentLoaded', init);
  return {
    toggle: toggle,
    refresh: refresh,
    describe: describeCurrent,
    describeProjectTools: describeProjectTools,
    mcpContext: mcpContext,
    openWorkbench: openWorkbench,
    closeWorkbench: closeWorkbench,
    open: openWorkbench,
    importGitHub: importGitHubProject,
    listGitHubRepositories: listGitHubRepositories,
    continueGitHubRepositories: continueGitHubRepositories,
    authorizeGitHub: authorizeGitHub,
    isAutoApproveEnabled: autoApprovePreference,
    currentProjectId: currentProjectId,
    removeProjectByName: removeProjectByName,
    setProjectMcpServerByName: setProjectMcpServerByName,
    verifyProjectMcpServerByName: verifyProjectMcpServerByName,
    retryRun: retryRunById,
    runSelectedCheck: runSelectedCheck,
    importGitHubSelection: importGitHubSelection,
    startRepositoryRemediation: startRepositoryRemediation,
    promptGitHubImport: function(repositoryUrl) { return importGitHubProject(repositoryUrl, true); }
  };
})();

function toggleWorkspacePanel() {
  EvaWorkspaces.toggle();
}
