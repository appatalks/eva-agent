var EvaWorkspaces = (function() {
  var state = {
    projects: [],
    runs: [],
    projectFiles: {},
    projectFilesLoading: {},
    projectTreeExpanded: {},
    runDrafts: {},
    selectedProjectId: '',
    selectedRunId: '',
    pendingDiscardRunId: '',
    loading: false,
    mcpUpdating: false,
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
    ['workspaceStatus', 'workspaceWorkbenchStatus'].forEach(function(id) {
      var element = document.getElementById(id);
      if (!element) return;
      element.textContent = message || '';
      element.dataset.state = kind || '';
    });
  }

  function setBusy(busy) {
    state.loading = busy;
    ['workspaceAddProjectBtn', 'workspaceRefreshBtn', 'workspaceCreateRunBtn', 'workspaceAddProjectWorkbenchBtn', 'workspaceImportGitHubBtn'].forEach(function(id) {
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
    if (run) state.selectedProjectId = run.projectId;
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

  function projectById(projectId) {
    return state.projects.find(function(project) { return project.id === projectId; }) || null;
  }

  function replaceProject(project) {
    var index = state.projects.findIndex(function(item) { return item.id === project.id; });
    if (index >= 0) state.projects[index] = project;
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
    actions.appendChild(terminal);
    var files = document.createElement('div');
    files.id = 'workspaceProjectFiles';
    files.className = 'workspace-project-files';
    files.dataset.projectId = project.id;
    section.append(heading, actions, files);
    detail.appendChild(section);
    renderProjectFiles(files, project);
  }

  function appendWorkbenchRunComposer(detail, project) {
    var draft = state.runDrafts[project.id] || (state.runDrafts[project.id] = { objective: '', baseRef: 'HEAD' });
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
    objective.addEventListener('input', function() { draft.objective = objective.value; });
    baseRef.addEventListener('input', function() { draft.baseRef = baseRef.value; });
    var submit = document.createElement('button');
    submit.type = 'submit';
    submit.textContent = 'Start isolated run';
    submit.disabled = state.loading || !supported();
    form.append(objectiveLabel, objective, baseLabel, baseRef, submit);
    form.addEventListener('submit', async function(event) {
      event.preventDefault();
      submit.disabled = true;
      var created = await createWorkspaceRun(project.id, objective.value, baseRef.value);
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
    source.textContent = 'Source: ' + (mcp.source || 'mcp.json') + ' | workspace-local selection';
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
        transport.textContent = server.transport || 'configured';
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
    var detail = document.getElementById('workspaceWorkbenchDetail');
    if (!projectList || !runList || !feed || !detail) return;
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
      if (project.id === state.selectedProjectId) button.classList.add('active');
      var title = document.createElement('strong');
      title.textContent = project.name;
      var enabled = ((project.mcpServers || {}).servers || []).filter(function(server) { return server.enabled; }).length;
      var meta = document.createElement('span');
      meta.textContent = (project.activeRunCount || 0) + ' active | ' + enabled + ' MCP enabled';
      button.append(title, meta);
      button.addEventListener('click', function() { selectProject(project.id); });
      projectList.appendChild(button);
    });

    runList.replaceChildren();
    var orderedRuns = state.runs.filter(function(run) {
      return run.status !== 'discarded' && run.projectId === state.selectedProjectId;
    });
    if (!orderedRuns.length) {
      var empty = document.createElement('p');
      empty.className = 'workspace-monitor-empty';
      empty.textContent = state.selectedProjectId ? 'No coding runs' : 'Select a workspace';
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
    var project = projectById(state.selectedProjectId);
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
    var selected = orderedRuns.find(function(run) { return run.id === state.selectedRunId; }) || orderedRuns[0];
    if (selected) {
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
      var runSection = document.createElement('section');
      runSection.className = 'workspace-workbench-section';
      runSection.append(heading, branch, facts, actions);
      if (selected.agent && selected.agent.report) {
        var report = document.createElement('pre');
        report.className = 'workspace-monitor-report';
        report.textContent = selected.agent.report;
        runSection.appendChild(report);
      }
      detail.appendChild(runSection);
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
      var signature = monitorSignature(runs, terminals);
      var changed = signature !== state.monitorSignature;
      var shouldRender = changed;
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
    if (!supported()) {
      status('Coding workspaces are unavailable in this Eva launch.', 'error');
      renderWorkbench();
      return;
    }
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

  async function importGitHubProject() {
    if (!supported() || state.loading) return;
    if (!api() || typeof api().workspaceImportGitHub !== 'function' || typeof evaTextPrompt !== 'function') {
      status('GitHub workspace import is unavailable in this Eva build.', 'error');
      return;
    }
    var repositoryUrl = await evaTextPrompt('GitHub repository URL', '', {
      maxLength: 2048,
      placeholder: 'https://github.com/owner/repository'
    });
    if (repositoryUrl === null || !String(repositoryUrl).trim()) return;
    setBusy(true);
    status('Importing GitHub workspace...', 'loading');
    try {
      var project = await api().workspaceImportGitHub(String(repositoryUrl).trim());
      if (project) state.selectedProjectId = project.id;
      await refresh();
      status('GitHub workspace imported.', 'success');
    } catch (error) {
      status(error.message || 'GitHub workspace import failed.', 'error');
      setBusy(false);
    }
  }

  async function createWorkspaceRun(projectId, objectiveValue, baseRefValue) {
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
      var primarySessionId = typeof _activeSessionId === 'function' ? _activeSessionId() : '';
      var run = await api().workspaceCreateRun({
        projectId: project.id,
        objective: objective,
        primarySessionId: primarySessionId,
        baseRef: String(baseRefValue || '').trim() || 'HEAD'
      });
      state.selectedProjectId = run.projectId;
      state.selectedRunId = run.id;
      await refresh();
      status(run.dispatchError ? 'Workspace ready; agent dispatch delayed: ' + run.dispatchError : 'Workspace agent dispatched.', run.dispatchError ? 'error' : 'success');
      return true;
    } catch (error) {
      status(error.message || 'Could not create coding run.', 'error');
      setBusy(false);
      return false;
    }
  }

  async function createRun(event) {
    event.preventDefault();
    var projectSelect = document.getElementById('workspaceProjectSelect');
    var objective = document.getElementById('workspaceObjective');
    var baseRef = document.getElementById('workspaceBaseRef');
    var created = await createWorkspaceRun(
      projectSelect ? projectSelect.value : '',
      objective ? objective.value : '',
      baseRef ? baseRef.value : 'HEAD'
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
    var monitorRefresh = document.getElementById('workspaceMonitorRefreshBtn');
    var monitorClose = document.getElementById('workspaceMonitorCloseBtn');
    var monitorNew = document.getElementById('workspaceMonitorNewBtn');
    if (close) close.addEventListener('click', toggle);
    if (add) add.addEventListener('click', addProject);
    if (refreshButton) refreshButton.addEventListener('click', refresh);
    if (form) form.addEventListener('submit', createRun);
    if (projectSelect) projectSelect.addEventListener('change', function() { selectProject(projectSelect.value); });
    if (openWorkbenchButton) openWorkbenchButton.addEventListener('click', openWorkbench);
    if (workbenchAddProject) workbenchAddProject.addEventListener('click', addProject);
    if (workbenchGitHubImport) workbenchGitHubImport.addEventListener('click', importGitHubProject);
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
