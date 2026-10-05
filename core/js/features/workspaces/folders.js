var EvaWorkspaceFolders = (function() {
  var current = null;
  var request = 0;
  var removeListener = null;

  function node(id) { return document.getElementById(id); }

  function message(text, error) {
    node('workspaceFolderMessage').textContent = text || '';
    node('workspaceFolderMessage').dataset.kind = error ? 'error' : '';
  }

  function controls() {
    var disabled = !current || current.busy;
    ['workspaceFolderPath', 'workspaceFolderGo', 'workspaceFolderHome', 'workspaceFolderUp', 'workspaceFolderHidden', 'workspaceFolderName', 'workspaceFolderCancel'].forEach(function(id) {
      node(id).disabled = disabled;
    });
    node('workspaceFolderChoose').disabled = disabled || current.loading || !current.valid;
    node('workspaceFolderList').inert = disabled;
  }

  function close() {
    var focus = current && current.focus;
    current = null;
    request++;
    node('workspaceFolderPicker').setAttribute('aria-hidden', 'true');
    node('workspaceFolderList').replaceChildren();
    node('workspaceFolderPath').value = '';
    node('workspaceFolderName').value = '';
    requestAnimationFrame(function() {
      if (focus && focus.isConnected && !focus.disabled) focus.focus();
      else if (node('workspaceLiveTab')) node('workspaceLiveTab').focus();
    });
  }

  async function cancel() {
    if (!current || current.busy) return;
    try {
      await window.evaStandalone.workspaceFolderCancel(current.id);
      close();
    } catch (error) {
      message(error.message || 'The folder browser could not be closed.', true);
    }
  }

  async function browse(path) {
    if (!current || current.busy) return;
    var session = current;
    var sequence = ++request;
    session.loading = true;
    session.valid = false;
    controls();
    message('Loading folders...');
    node('workspaceFolderList').replaceChildren();
    try {
      var result = await window.evaStandalone.workspaceFolderBrowse(session.id, path, node('workspaceFolderHidden').checked);
      if (current !== session || sequence !== request) return;
      session.path = result.path;
      session.parent = result.parent;
      session.valid = true;
      node('workspaceFolderPath').value = result.path;
      result.directories.forEach(function(folder) {
        var button = document.createElement('button');
        button.type = 'button';
        button.className = 'workspace-folder-row';
        button.textContent = folder.name;
        button.title = 'Open ' + folder.name;
        button.addEventListener('click', function() { browse(folder.path); });
        node('workspaceFolderList').appendChild(button);
      });
      message(result.truncated ? 'Showing the first 1,000 folders. Enter a path to navigate directly.' :
        result.directories.length ? 'Select a folder to open it, or use this location.' : 'No subfolders. You can use this location.');
    } catch (error) {
      if (current !== session || sequence !== request) return;
      message(error.message || 'The folder could not be loaded.', true);
    } finally {
      if (current === session && sequence === request) {
        session.loading = false;
        controls();
      }
    }
  }

  async function complete(event) {
    event.preventDefault();
    if (!current || current.loading || current.busy || !current.valid) return;
    var session = current;
    session.busy = true;
    controls();
    message(session.mode === 'create' ? 'Creating the folder and initializing Git...' : 'Importing this Git workspace...');
    try {
      var result = await window.evaStandalone.workspaceFolderComplete(session.id, node('workspaceFolderName').value);
      if (current !== session) return;
      if (result.error) {
        session.busy = false;
        controls();
        message(result.error, true);
      } else if (result.ok === true) {
        close();
      } else {
        throw new Error('Eva did not confirm the workspace operation.');
      }
    } catch (error) {
      if (current !== session) return;
      session.busy = false;
      controls();
      message(error.message || 'The workspace could not be added.', true);
    }
  }

  function open(payload) {
    if (!payload || !payload.id || current) {
      if (typeof setStatus === 'function') setStatus('error', 'The workspace folder browser could not be opened.');
      return;
    }
    current = { id: payload.id, mode: payload.mode, busy: false, loading: false, valid: false, focus: document.activeElement };
    var creating = payload.mode === 'create';
    node('workspaceFolderTitle').textContent = creating ? 'New workspace' : 'Import workspace';
    node('workspaceFolderHelp').textContent = creating ? 'Choose a parent folder. Eva will create a new named folder with Git and a starter README.' :
      'Choose an existing Git repository. Its files stay in their current location.';
    node('workspaceFolderNameRow').hidden = !creating;
    node('workspaceFolderName').required = creating;
    node('workspaceFolderName').value = '';
    node('workspaceFolderChoose').textContent = creating ? 'Create workspace here' : 'Import this workspace';
    node('workspaceFolderPicker').setAttribute('aria-hidden', 'false');
    browse(payload.path);
    requestAnimationFrame(function() { (creating ? node('workspaceFolderName') : node('workspaceFolderPath')).focus(); });
  }

  document.addEventListener('DOMContentLoaded', function() {
    var api = window.evaStandalone;
    if (!api || typeof api.onWorkspaceFolderPicker !== 'function') return;
    removeListener = api.onWorkspaceFolderPicker(open);
    node('workspaceFolderForm').addEventListener('submit', complete);
    node('workspaceFolderCancel').addEventListener('click', cancel);
    node('workspaceFolderGo').addEventListener('click', function() { browse(node('workspaceFolderPath').value); });
    node('workspaceFolderHome').addEventListener('click', function() { browse('~'); });
    node('workspaceFolderUp').addEventListener('click', function() { if (current && current.parent) browse(current.parent); });
    node('workspaceFolderHidden').addEventListener('change', function() { if (current) browse(current.path); });
    node('workspaceFolderPath').addEventListener('keydown', function(event) {
      if (event.key === 'Enter') { event.preventDefault(); browse(node('workspaceFolderPath').value); }
    });
    node('workspaceFolderPicker').addEventListener('keydown', function(event) {
      if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); cancel(); }
      if (event.key !== 'Tab') return;
      var fields = Array.from(node('workspaceFolderForm').querySelectorAll('input, button')).filter(function(field) {
        return !field.disabled && field.getClientRects().length > 0;
      });
      if (!fields.length) return;
      if (event.shiftKey && document.activeElement === fields[0]) { event.preventDefault(); fields[fields.length - 1].focus(); }
      if (!event.shiftKey && document.activeElement === fields[fields.length - 1]) { event.preventDefault(); fields[0].focus(); }
    });
    window.addEventListener('beforeunload', function() { if (removeListener) removeListener(); }, { once: true });
  });

  return { cancel: cancel };
})();
