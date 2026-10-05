const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const { randomUUID } = require('node:crypto');

class WorkspaceFolderBrowser {
  constructor(actions) {
    this.actions = actions;
    this.active = null;
    this.lastPath = os.homedir();
  }

  open(owner, mode) {
    if (!['import', 'create'].includes(mode)) throw new Error('Invalid workspace folder action.');
    if (this.active) throw new Error('A workspace folder browser is already open.');
    return new Promise((resolve, reject) => {
      const state = { id: randomUUID(), owner, mode, path: '', valid: false, browsing: false, busy: false, revision: 0, resolve };
      state.destroyed = () => this.close(state);
      this.active = state;
      owner.once('destroyed', state.destroyed);
      try {
        owner.send('workspace:folder-picker', { id: state.id, mode, path: this.lastPath });
      } catch (error) {
        this.active = null;
        owner.removeListener('destroyed', state.destroyed);
        reject(error);
      }
    });
  }

  session(owner, id) {
    if (!this.active || this.active.owner !== owner || this.active.id !== id) throw new Error('This folder browser is no longer open.');
    return this.active;
  }

  async browse(owner, id, requestedPath, showHidden) {
    const state = this.session(owner, id);
    if (state.busy) throw new Error('Workspace creation or import is already in progress.');
    const revision = ++state.revision;
    state.valid = false;
    state.browsing = false;
    if (typeof requestedPath !== 'string' || !requestedPath.trim() || requestedPath.length > 4096) throw new Error('Enter a folder path.');
    let candidate = requestedPath.trim();
    if (candidate === '~') candidate = os.homedir();
    else if (candidate.startsWith('~/') || candidate.startsWith('~\\')) candidate = path.join(os.homedir(), candidate.slice(2));
    if (!path.isAbsolute(candidate)) throw new Error('Enter an absolute folder path, or use ~ for your home folder.');
    state.browsing = true;
    try {
      const directory = await fs.realpath(candidate);
      if (!(await fs.stat(directory)).isDirectory()) throw new Error('Choose a folder, not a file.');
      const entries = await fs.readdir(directory, { withFileTypes: true });
      if (this.active !== state || revision !== state.revision) throw new Error('A newer folder request replaced this one.');
      state.path = directory;
      state.valid = true;
      const directories = entries.filter(entry => entry.isDirectory() && (showHidden === true || !entry.name.startsWith('.')))
        .map(entry => ({ name: entry.name, path: path.join(directory, entry.name) }))
        .sort((left, right) => left.name.localeCompare(right.name, undefined, { numeric: true, sensitivity: 'base' }));
      return { path: directory, parent: path.dirname(directory), directories: directories.slice(0, 1000), truncated: directories.length > 1000 };
    } catch (error) {
      if (['ENOENT', 'ENOTDIR', 'EACCES', 'EPERM'].includes(error.code)) throw new Error('That folder is unavailable or cannot be read.');
      throw error;
    } finally {
      if (revision === state.revision) state.browsing = false;
    }
  }

  async complete(owner, id, name) {
    const state = this.session(owner, id);
    if (!state.path || !state.valid || state.browsing || state.busy) throw new Error('Wait for the folder to load before continuing.');
    state.busy = true;
    try {
      const project = state.mode === 'create'
        ? await this.actions.create(state.path, name)
        : await this.actions.import(state.path);
      this.lastPath = state.path;
      this.close(state, { canceled: false, project });
      return { ok: true };
    } catch (error) {
      state.busy = false;
      return { error: error.message || 'The workspace could not be added.' };
    }
  }

  cancel(owner, id) {
    const state = this.session(owner, id);
    if (state.busy) throw new Error('Wait for the current workspace operation to finish.');
    this.close(state);
  }

  close(state, result) {
    if (this.active !== state) return;
    this.active = null;
    state.owner.removeListener('destroyed', state.destroyed);
    state.resolve(result || { canceled: true });
  }
}

module.exports = { WorkspaceFolderBrowser };
