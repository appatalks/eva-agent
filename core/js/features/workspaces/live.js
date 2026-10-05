var EvaWorkspaceLive = (function() {
  var cards = Object.create(null);
  var hiddenTerminals = Object.create(null);
  var selectedKey = '';
  var focusedKey = '';
  var snapshot = null;
  var handlers = null;
  var creating = false;
  var telemetryError = '';

  function element(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function notice(message, error) {
    var node = document.getElementById('workspaceLiveNotice');
    if (!node) return;
    node.hidden = !message;
    node.textContent = message || '';
    node.dataset.kind = error ? 'error' : '';
  }

  function button(label, handler) {
    var node = element('button', 'workspace-live-action', label);
    node.type = 'button';
    node.addEventListener('click', handler);
    return node;
  }

  function select(key, focus) {
    selectedKey = key;
    if (focus) focusedKey = key;
    Object.keys(cards).forEach(function(id) {
      cards[id].node.classList.toggle('selected', id === selectedKey);
      cards[id].node.hidden = !!focusedKey && id !== focusedKey;
    });
    var grid = document.getElementById('workspaceLiveGrid');
    if (grid) grid.classList.toggle('focused', !!focusedKey);
    var restore = document.getElementById('workspaceLiveGridBtn');
    if (restore) restore.hidden = !focusedKey;
    var selected = cards[key];
    if (selected && selected.node.isConnected) {
      if (selected.descriptor) setWorkspaceTerminalTarget(selected.descriptor.rootId, selected.node.querySelector('strong').textContent);
      if (selected.run && selected.run.checkout) {
        setWorkspaceTerminalTarget(selected.run.checkout.id, checkoutLabel(selected.run));
        if (handlers && snapshot.selectedRunId !== selected.run.id) handlers.selectRun(selected.run.id);
      }
    }
    requestAnimationFrame(function() {
      var record = cards[key];
      if (record && record.view) {
        record.view.fit();
        if (focus) record.view.focus();
      }
    });
  }

  function createCard(key, kind, title) {
    var node = element('section', 'workspace-live-card');
    node.dataset.liveKey = key;
    node.dataset.kind = kind;
    var header = element('header', 'workspace-live-card-header');
    var identity = element('div', 'workspace-live-card-identity');
    identity.append(element('span', 'workspace-kicker', kind), element('strong', '', title));
    var focus = button('Focus', function() {
      select(key, true);
    });
    focus.setAttribute('aria-label', 'Focus ' + title);
    header.append(identity, focus);
    node.appendChild(header);
    node.addEventListener('pointerdown', function() {
      if (selectedKey !== key) select(key, false);
    });
    return { node: node, header: header, key: key };
  }

  function terminalCard(descriptor, label) {
    var key = 'terminal:' + descriptor.id;
    if (cards[key]) return cards[key];
    var record = createCard(key, 'terminal', label);
    record.descriptor = descriptor;
    var controls = element('div', 'workspace-live-card-controls');
    record.interrupt = button('Interrupt', async function() {
      try {
        await window.evaStandalone.terminalWrite(record.descriptor.id, '\x03');
        notice('Interrupt sent to this shell.');
      } catch (error) {
        notice(error.message || 'Terminal could not be interrupted.', true);
      }
    });
    var close = button('Close shell', async function() {
      if (!record.descriptor.exited && !confirm('Close this shell and stop any command running in it?')) return;
      close.disabled = true;
      close.textContent = 'Closing...';
      try {
        await window.evaStandalone.terminalClose(record.descriptor.id);
        hiddenTerminals[record.descriptor.id] = true;
        if (record.view) record.view.dispose();
        record.removed = true;
        record.node.remove();
        delete cards[key];
        if (focusedKey === key) focusedKey = '';
        if (selectedKey === key) selectedKey = '';
        render(snapshot, handlers);
        notice('Shell closed. Other terminals and agents were left running.');
      } catch (error) {
        close.disabled = false;
        close.textContent = 'Close shell';
        notice(error.message || 'Terminal could not be closed.', true);
      }
    });
    controls.append(record.interrupt, close);
    var host = element('div', 'workspace-live-terminal');
    record.node.append(controls, host);
    record.host = host;
    cards[key] = record;
    return record;
  }

  function mountTerminal(record, label) {
    if (record.ready) return record.ready;
    record.ready = createWorkspaceTerminalView(record.host, {
      rootId: record.descriptor.rootId, label: label
    }, { terminalId: record.descriptor.id, managed: true }).then(function(view) {
      if (record.removed) {
        view.dispose();
        return null;
      }
      record.view = view;
      return view;
    }).catch(function(error) {
      record.ready = null;
      record.host.replaceChildren();
      record.host.append(element('p', 'workspace-monitor-empty', error.message || 'Terminal view could not be opened.'));
      record.host.append(button('Retry connection', function() {
        record.host.replaceChildren();
        mountTerminal(record, label);
      }));
      notice(error.message || 'Terminal view could not be opened.', true);
      return null;
    });
    return record.ready;
  }

  function agentCard(run) {
    var key = 'agent:' + run.id;
    if (cards[key]) return cards[key];
    var record = createCard(key, 'agent', run.objective);
    record.run = run;
    record.status = element('p', 'workspace-live-agent-status');
    record.activity = element('p', 'workspace-live-agent-activity');
    record.output = element('pre', 'workspace-live-agent-output');
    record.permissions = element('div', 'workspace-live-permissions');
    record.message = element('p', 'workspace-live-agent-message');
    record.message.setAttribute('role', 'status');
    var actions = element('div', 'workspace-live-card-controls');
    record.stop = button('Interrupt agent', function() { agentAction(record, 'stop'); });
    record.stop.title = 'Interrupt this agent and clear queued directions; keep its worktree and changes.';
    record.terminal = button('Open terminal', function() {
      openWorkspaceTerminal(record.run.checkout.id, checkoutLabel(record.run));
    });
    record.details = button('Details', function() { handlers.showResults(record.run.id); });
    record.apply = button('Apply to source', function() { handlers.apply(record.run.id); });
    record.retry = button('Retry run', function() { handlers.retry(record.run); });
    actions.append(record.stop, record.terminal, record.details, record.apply, record.retry);
    var form = element('form', 'workspace-live-steer');
    var label = element('label', '', 'Adjust direction');
    record.input = element('textarea', '');
    record.input.rows = 2;
    record.input.maxLength = 2000;
    record.input.placeholder = 'Add instructions for the next turn, or resume finished work';
    record.input.setAttribute('aria-label', 'Direction for ' + run.objective);
    label.appendChild(record.input);
    record.send = element('button', 'workspace-live-action', 'Send direction');
    record.send.type = 'submit';
    form.append(label, record.send);
    form.addEventListener('submit', function(event) {
      event.preventDefault();
      if (record.input.value.trim()) agentAction(record, 'steer', record.input.value.trim());
    });
    record.node.append(record.status, record.activity, record.output, record.permissions, actions, form, record.message);
    cards[key] = record;
    return record;
  }

  function checkoutLabel(run) {
    return (run.project ? run.project.name + ' | ' : '') + (run.checkout.branch || 'worktree');
  }

  async function agentAction(record, action, instruction) {
    if (record.busy || !record.task || !handlers) return;
    record.busy = true;
    record.stop.disabled = record.send.disabled = true;
    record.message.textContent = action === 'stop' ? 'Requesting interruption...' : 'Sending direction...';
    try {
      var response = await handlers.agentAction(record.task.id, action, instruction);
      if (action === 'steer') {
        record.input.value = '';
        record.message.textContent = response.queued ? 'Direction queued for the next agent turn.' : 'Agent resumed with the new direction.';
      } else {
        record.message.textContent = 'Interruption requested. Waiting for the agent to stop; changes are kept.';
      }
      record.message.dataset.kind = '';
    } catch (error) {
      record.message.dataset.kind = 'error';
      record.message.textContent = error.message || 'Agent action failed.';
    } finally {
      record.busy = false;
      if (snapshot) render(snapshot, handlers);
    }
  }

  function updateAgent(record, run, task) {
    record.run = run;
    record.task = task || null;
    var status = task ? task.status : run.agent ? run.agent.status : 'not dispatched';
    record.status.textContent = String(status).toUpperCase() + (task && task.model ? ' | ' + task.model : '') +
      ' | ' + (run.checkout ? run.checkout.branch || 'detached' : 'checkout unavailable');
    record.activity.textContent = task && task.activity ? task.activity :
      (task ? 'Waiting for the next agent update.' : 'Live agent unavailable in this launch; showing the saved run.');
    var text = (task && task.result) || (run.agent && run.agent.report) || 'No output yet.';
    if (record.output.textContent !== text) {
      var atBottom = record.output.scrollHeight - record.output.scrollTop - record.output.clientHeight < 40;
      record.output.textContent = text;
      if (atBottom) record.output.scrollTop = record.output.scrollHeight;
    }
    var active = ['starting', 'waiting', 'running', 'steering'].indexOf(status) >= 0;
    var available = !!run.checkout && run.checkout.lifecycle === 'active';
    record.stop.disabled = record.busy || !task || !active;
    record.send.disabled = record.busy || !task || !available || status === 'cancelling' || status === 'finalizing' ||
      ['archived', 'discarded'].indexOf(run.status) >= 0;
    record.terminal.disabled = !available;
    record.apply.disabled = !available || active || status === 'cancelling' || status === 'finalizing';
    record.apply.textContent = run.applyStatus === 'applied' ? 'Applied to source' : 'Apply to source';
    record.retry.hidden = !(run.status === 'active' && (!run.agent || run.agent.status === 'error'));
    record.permissions.replaceChildren();
    if (handlers.permissions) handlers.permissions(record.permissions, run);
  }

  function roots(data) {
    var output = [];
    if (data.project && data.project.sourceCheckout && data.project.sourceCheckout.lifecycle === 'active') {
      output.push({ rootId: data.project.sourceCheckout.id, label: data.project.name + ' | source' });
    }
    data.runs.forEach(function(run) {
      if (run.checkout && run.checkout.lifecycle === 'active') {
        output.push({ rootId: run.checkout.id, label: checkoutLabel(run) });
      }
    });
    return output;
  }

  function render(data, callbacks) {
    if (!data || !callbacks) return;
    snapshot = data;
    handlers = callbacks;
    var grid = document.getElementById('workspaceLiveGrid');
    var targetSelect = document.getElementById('workspaceTerminalTargetSelect');
    if (!grid || !targetSelect) return;
    var targets = roots(data);
    var oldTarget = targetSelect.value;
    var targetSignature = JSON.stringify(targets);
    if (targetSelect.dataset.signature !== targetSignature) {
      targetSelect.replaceChildren();
      targets.forEach(function(target) {
        var option = element('option', '', target.label);
        option.value = target.rootId;
        targetSelect.appendChild(option);
      });
      if (targets.some(function(target) { return target.rootId === oldTarget; })) targetSelect.value = oldTarget;
      targetSelect.dataset.signature = targetSignature;
    }
    document.getElementById('workspaceNewTerminalBtn').disabled = creating || !targets.length;
    targetSelect.disabled = !targets.length;
    document.getElementById('workspaceNewRunBtn').disabled = !data.project || data.busy === true;
    var records = [];
    data.runs.filter(function(run) {
      return run.id === data.selectedRunId || (run.agent && ['starting', 'waiting', 'running', 'steering', 'cancelling', 'finalizing'].indexOf(run.agent.status) >= 0);
    }).forEach(function(run) {
      var record = agentCard(run);
      updateAgent(record, run, data.tasks[run.agent && run.agent.id]);
      records.push(record);
    });
    data.terminals.forEach(function(descriptor) {
      var target = targets.find(function(item) { return item.rootId === descriptor.rootId; });
      if (!target || hiddenTerminals[descriptor.id]) return;
      var record = terminalCard(descriptor, target.label);
      record.descriptor = descriptor;
      record.interrupt.disabled = descriptor.exited;
      records.push(record);
    });
    if (!records.some(function(record) { return record.key === focusedKey; })) focusedKey = '';
    Array.from(grid.children).forEach(function(node) {
      if (!records.some(function(record) { return record.node === node; })) node.remove();
    });
    records.forEach(function(record, index) {
      if (grid.children[index] !== record.node) grid.insertBefore(record.node, grid.children[index] || null);
      if (record.descriptor) mountTerminal(record, targets.find(function(target) { return target.rootId === record.descriptor.rootId; }).label);
    });
    Object.keys(cards).forEach(function(key) {
      var record = cards[key];
      if (record.descriptor && !data.allTerminalIds.includes(record.descriptor.id)) {
        record.removed = true;
        if (record.view) record.view.dispose();
        record.node.remove();
        delete cards[key];
      }
    });
    if (!records.length) {
      var empty = element('div', 'workspace-live-empty');
      empty.append(element('strong', '', data.project ? 'Your workspace is ready' : 'Select a workspace'),
        element('p', '', 'Open terminals or start an isolated coding run here. Live output and agent controls will appear side by side.'));
      grid.appendChild(empty);
    }
    select(selectedKey, false);
    if (data.error !== telemetryError) {
      telemetryError = data.error || '';
      notice(telemetryError, !!telemetryError);
    }
  }

  async function openTerminal(target, forceNew) {
    if (!handlers || !target || !target.rootId) throw new Error('Select an approved workspace before opening a terminal.');
    var label = target.label || 'Workspace';
    try {
      handlers.ensureRoot(target.rootId);
      handlers.showLive();
      var sessions = await window.evaStandalone.terminalList();
      var selected = cards[selectedKey];
      var preferredId = selected && selected.descriptor && selected.descriptor.rootId === target.rootId ? selected.descriptor.id : '';
      var descriptor = forceNew ? null : sessions.find(function(item) {
        return item.rootId === target.rootId && !item.exited && (!preferredId || item.id === preferredId);
      });
      if (!descriptor && !forceNew) descriptor = sessions.find(function(item) { return item.rootId === target.rootId && !item.exited; });
      if (!descriptor) descriptor = await window.evaStandalone.terminalCreate({ rootId: target.rootId, cols: 80, rows: 24 });
      delete hiddenTerminals[descriptor.id];
      handlers.terminalsChanged(sessions.filter(function(item) { return item.id !== descriptor.id; }).concat([descriptor]));
      var record = cards['terminal:' + descriptor.id];
      if (!record) throw new Error('The terminal does not belong to the selected workspace.');
      var view = await mountTerminal(record, label);
      if (!view) throw new Error('Terminal view is unavailable; use Retry connection.');
      focusedKey = '';
      select(record.key, false);
      view.focus();
      return view;
    } catch (error) {
      notice(error.message || 'Terminal could not be opened.', true);
      throw error;
    }
  }

  document.addEventListener('DOMContentLoaded', function() {
    document.getElementById('workspaceLiveGridBtn').addEventListener('click', function() {
      focusedKey = '';
      select(selectedKey, false);
    });
    document.getElementById('workspaceNewTerminalBtn').addEventListener('click', async function() {
      var select = document.getElementById('workspaceTerminalTargetSelect');
      if (creating || !select.value) return;
      creating = true;
      render(snapshot, handlers);
      try {
        await openTerminal({ rootId: select.value, label: select.selectedOptions[0].textContent }, true);
      } catch (error) {
        notice(error.message || 'Terminal could not be created.', true);
      } finally {
        creating = false;
        render(snapshot, handlers);
      }
    });
  });

  return {
    render: render,
    openTerminal: openTerminal,
    runCommand: async function(target, command, submit) {
      var view = await openTerminal(target, false);
      return view.runCommand(command, submit);
    },
    fit: function() {
      Object.keys(cards).forEach(function(key) {
        if (cards[key].view) cards[key].view.fit();
      });
    }
  };
})();
