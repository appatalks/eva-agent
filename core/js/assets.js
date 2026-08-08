var EvaAssets = (function() {
  var state = {
    open: false,
    loading: false,
    filter: 'all',
    assets: [],
    selectedId: ''
  };

  function bridgeUrl() {
    var value = typeof getACPBridgeUrl === 'function' ? getACPBridgeUrl() : 'http://localhost:8888';
    return String(value || '').replace(/\/+$/, '');
  }

  function formatSize(bytes) {
    var value = Number(bytes || 0);
    if (value < 1024) return value + ' B';
    if (value < 1024 * 1024) return (value / 1024).toFixed(1) + ' KB';
    return (value / (1024 * 1024)).toFixed(1) + ' MB';
  }

  function sourceLabel(asset) {
    return asset.source === 'workspace' ? 'WORKSPACE' : 'GENERATED';
  }

  async function refresh() {
    if (state.loading) return;
    state.loading = true;
    var updated = document.getElementById('assetsViewUpdated');
    if (updated) updated.textContent = 'REFRESHING';
    try {
      var generatedRequest = fetch(bridgeUrl() + '/v1/files').then(function(response) {
        if (!response.ok) throw new Error('Generated assets returned ' + response.status);
        return response.json();
      }).then(function(payload) {
        return (payload.files || []).map(function(file) {
          return {
            id: 'generated:' + file.name,
            source: 'generated',
            name: file.name,
            relativePath: file.name,
            size: file.size,
            modified: Number(file.modified || 0),
            projectName: 'Eva generated assets',
            objective: 'Generated artifact'
          };
        });
      }).catch(function() { return []; });
      var workspaceRequest = window.evaStandalone && typeof window.evaStandalone.workspaceListAssets === 'function'
        ? window.evaStandalone.workspaceListAssets().catch(function() { return []; })
        : Promise.resolve([]);
      var results = await Promise.all([generatedRequest, workspaceRequest]);
      state.assets = results[0].concat(results[1]).sort(function(left, right) {
        return Number(right.modified || 0) - Number(left.modified || 0);
      });
      if (state.selectedId && !state.assets.some(function(asset) { return asset.id === state.selectedId; })) state.selectedId = '';
      if (!state.selectedId && state.assets.length) state.selectedId = state.assets[0].id;
      render();
      if (updated) updated.textContent = 'UPDATED ' + new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    } catch (error) {
      renderError(error.message || String(error));
      if (updated) updated.textContent = 'UNAVAILABLE';
    } finally {
      state.loading = false;
    }
  }

  function filteredAssets() {
    if (state.filter === 'all') return state.assets;
    return state.assets.filter(function(asset) { return asset.source === state.filter; });
  }

  function render() {
    var list = document.getElementById('assetsViewList');
    var count = document.getElementById('assetsViewCount');
    if (!list) return;
    list.replaceChildren();
    var assets = filteredAssets();
    if (count) count.textContent = assets.length + ' file' + (assets.length === 1 ? '' : 's');
    if (!assets.length) {
      var empty = document.createElement('div');
      empty.className = 'assets-view-empty';
      empty.textContent = state.filter === 'workspace' ? 'No changed workspace files' : 'No assets in this view';
      list.appendChild(empty);
      renderDetail();
      return;
    }
    var lastGroup = '';
    assets.forEach(function(asset) {
      var group = asset.source === 'workspace' ? (asset.projectName || 'Workspace') : 'Generated assets';
      if (group !== lastGroup) {
        lastGroup = group;
        var heading = document.createElement('div');
        heading.className = 'assets-view-group';
        heading.textContent = group;
        list.appendChild(heading);
      }
      var row = document.createElement('button');
      row.type = 'button';
      row.className = 'assets-view-row';
      if (asset.id === state.selectedId) row.classList.add('active');
      var source = document.createElement('span');
      source.className = 'assets-view-source';
      source.textContent = sourceLabel(asset);
      var name = document.createElement('strong');
      name.textContent = asset.relativePath || asset.name;
      var meta = document.createElement('span');
      meta.className = 'assets-view-meta';
      var modified = asset.modified ? new Date(asset.modified * 1000).toLocaleString() : '';
      meta.textContent = formatSize(asset.size) + (modified ? ' | ' + modified : '');
      row.append(source, name, meta);
      row.addEventListener('click', function() {
        state.selectedId = asset.id;
        render();
      });
      row.addEventListener('dblclick', function() { openAsset(asset); });
      list.appendChild(row);
    });
    renderDetail();
  }

  function selectedAsset() {
    return state.assets.find(function(asset) { return asset.id === state.selectedId; });
  }

  function detailButton(label, handler) {
    var button = document.createElement('button');
    button.type = 'button';
    button.textContent = label;
    button.addEventListener('click', handler);
    return button;
  }

  function renderDetail() {
    var detail = document.getElementById('assetsViewDetail');
    if (!detail) return;
    detail.replaceChildren();
    var asset = selectedAsset();
    if (!asset) {
      var empty = document.createElement('div');
      empty.className = 'assets-view-empty';
      empty.textContent = 'Select an asset';
      detail.appendChild(empty);
      return;
    }
    var source = document.createElement('div');
    source.className = 'assets-detail-source';
    source.textContent = sourceLabel(asset);
    var heading = document.createElement('h2');
    heading.textContent = asset.name;
    var path = document.createElement('p');
    path.className = 'assets-detail-path';
    path.textContent = asset.relativePath || asset.name;
    var context = document.createElement('p');
    context.className = 'assets-detail-context';
    context.textContent = asset.source === 'workspace' ? asset.objective : 'Eva generated artifact';
    var facts = document.createElement('dl');
    [['Size', formatSize(asset.size)], ['Source', asset.projectName || 'Generated'], ['Agent', asset.agentStatus || 'n/a']].forEach(function(pair) {
      var term = document.createElement('dt');
      term.textContent = pair[0];
      var value = document.createElement('dd');
      value.textContent = pair[1];
      facts.append(term, value);
    });
    var actions = document.createElement('div');
    actions.className = 'assets-detail-actions';
    actions.appendChild(detailButton('Open', function() { openAsset(asset); }));
    if (asset.source === 'workspace' && asset.checkoutId && typeof openWorkspaceTerminal === 'function') {
      actions.appendChild(detailButton('Open terminal', function() {
        openWorkspaceTerminal(asset.checkoutId, (asset.projectName || 'Workspace') + ' | ' + (asset.relativePath || asset.name));
      }));
    }
    detail.append(source, heading, path, context, facts, actions);
  }

  async function openAsset(asset) {
    try {
      if (asset.source === 'workspace') {
        await window.evaStandalone.workspaceOpenAsset(asset.runId, asset.relativePath);
      } else {
        var response = await fetch(bridgeUrl() + '/v1/files/' + encodeURIComponent(asset.name) + '?open=1');
        if (!response.ok) throw new Error('Open failed (' + response.status + ')');
      }
    } catch (error) {
      if (typeof setStatus === 'function') setStatus('error', error.message || 'Asset could not be opened');
    }
  }

  function renderError(message) {
    var list = document.getElementById('assetsViewList');
    if (!list) return;
    list.replaceChildren();
    var error = document.createElement('div');
    error.className = 'assets-view-empty assets-view-error';
    error.textContent = message;
    list.appendChild(error);
  }

  function open() {
    if (typeof closeAgentOperationsForNavigation === 'function') closeAgentOperationsForNavigation();
    if (window.EvaWorkspaces && typeof window.EvaWorkspaces.closeWorkbench === 'function') window.EvaWorkspaces.closeWorkbench();
    if (window.EvaSkills && typeof window.EvaSkills.close === 'function') window.EvaSkills.close();
    if (typeof closeSidePanels === 'function') closeSidePanels();
    state.open = true;
    document.body.classList.add('assets-view-open');
    var view = document.getElementById('assetsView');
    if (view) view.setAttribute('aria-hidden', 'false');
    refresh();
  }

  function close() {
    state.open = false;
    document.body.classList.remove('assets-view-open');
    var view = document.getElementById('assetsView');
    if (view) view.setAttribute('aria-hidden', 'true');
  }

  function init() {
    var closeButton = document.getElementById('assetsViewClose');
    var refreshButton = document.getElementById('assetsViewRefresh');
    if (closeButton) closeButton.addEventListener('click', close);
    if (refreshButton) refreshButton.addEventListener('click', refresh);
    document.querySelectorAll('[data-asset-filter]').forEach(function(button) {
      button.addEventListener('click', function() {
        state.filter = button.dataset.assetFilter || 'all';
        document.querySelectorAll('[data-asset-filter]').forEach(function(item) {
          item.setAttribute('aria-pressed', item === button ? 'true' : 'false');
        });
        render();
      });
    });
  }

  document.addEventListener('DOMContentLoaded', init);
  return { open: open, close: close, refresh: refresh };
})();
