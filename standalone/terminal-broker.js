const { EventEmitter } = require('events');
const { spawn, spawnSync } = require('child_process');
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

const CREATE_FIELDS = new Set(['rootId', 'cols', 'rows']);
const INTERNAL_ENV_KEYS = new Set(['EVA_BRIDGE_TOKEN', 'EVA_LOCAL_SPEECH_TOKEN']);

function boundedInteger(value, fallback, minimum, maximum, label) {
  const number = value === undefined ? fallback : Number(value);
  if (!Number.isInteger(number) || number < minimum || number > maximum) {
    throw new Error('Invalid terminal ' + label + '.');
  }
  return number;
}

function defaultShell(platform, environment) {
  if (platform === 'win32') return environment.ComSpec || 'powershell.exe';
  return environment.SHELL || '/bin/sh';
}

function terminalEnvironment(environment) {
  const result = {};
  Object.keys(environment || {}).forEach(function(key) {
    if (!INTERNAL_ENV_KEYS.has(key)) result[key] = environment[key];
  });
  result.TERM = result.TERM || 'xterm-256color';
  return result;
}

function trimUtf8Tail(value, maximumBytes) {
  const bytes = Buffer.from(value, 'utf8');
  if (bytes.length <= maximumBytes) return value;
  return bytes.subarray(bytes.length - maximumBytes).toString('utf8').replace(/^\uFFFD+/, '');
}

class TerminalBroker extends EventEmitter {
  constructor(options) {
    super();
    const config = options || {};
    if (!config.pty || typeof config.pty.spawn !== 'function') {
      throw new Error('A PTY implementation is required.');
    }
    this.pty = config.pty;
    this.platform = config.platform || process.platform;
    this.environment = terminalEnvironment(config.environment || process.env);
    this.shell = config.shell || defaultShell(this.platform, this.environment);
    this.idFactory = config.idFactory || crypto.randomUUID;
    this.signalProcess = config.signalProcess || process.kill;
    this.spawnProcess = config.spawnProcess || spawn;
    this.spawnSyncProcess = config.spawnSyncProcess || spawnSync;
    this.terminationGraceMs = boundedInteger(
      config.terminationGraceMs,
      1500,
      100,
      10000,
      'termination grace period'
    );
    this.maxScrollbackBytes = boundedInteger(
      config.maxScrollbackBytes,
      1024 * 1024,
      1,
      16 * 1024 * 1024,
      'scrollback limit'
    );
    this.roots = new Map();
    this.sessions = new Map();
  }

  registerRoot(rootId, directory, options) {
    if (typeof rootId !== 'string' || !rootId.trim()) throw new Error('Invalid approved root ID.');
    const strict = !(options && options.allowSymlinks);
    const resolved = path.resolve(directory);
    if (strict) this._assertNoSymlinkComponents(resolved);
    const canonical = fs.realpathSync(resolved);
    if (!fs.statSync(canonical).isDirectory()) throw new Error('Approved root must be a directory.');
    if (strict && canonical !== resolved) throw new Error('Approved root cannot resolve through a symlink.');
    this.roots.set(rootId, { path: canonical, strict: strict });
    return { id: rootId, path: canonical };
  }

  unregisterRoot(rootId) {
    for (const session of this.sessions.values()) {
      if (session.rootId === rootId) throw new Error('Approved root is in use.');
    }
    return this.roots.delete(rootId);
  }

  hasRoot(rootId) {
    return this.roots.has(rootId);
  }

  _assertNoSymlinkComponents(directory) {
    const parsed = path.parse(directory);
    let current = parsed.root;
    const parts = directory.slice(parsed.root.length).split(path.sep).filter(Boolean);
    parts.forEach(function(part) {
      current = path.join(current, part);
      if (fs.lstatSync(current).isSymbolicLink()) throw new Error('Approved root cannot contain symlinks.');
    });
  }

  _resolveRegisteredRoot(rootId) {
    const registered = this.roots.get(rootId);
    if (!registered) throw new Error('Unknown approved root.');
    if (registered.strict) {
      this._assertNoSymlinkComponents(registered.path);
      const canonical = fs.realpathSync(registered.path);
      if (canonical !== registered.path || !fs.statSync(canonical).isDirectory()) {
        throw new Error('Approved root changed after registration.');
      }
    }
    return registered.path;
  }

  create(request) {
    const input = request && typeof request === 'object' ? request : {};
    Object.keys(input).forEach(function(key) {
      if (!CREATE_FIELDS.has(key)) throw new Error('Unsupported field: ' + key);
    });
    const rootId = typeof input.rootId === 'string' ? input.rootId : '';
    const cwd = this._resolveRegisteredRoot(rootId);
    const cols = boundedInteger(input.cols, 80, 20, 500, 'column count');
    const rows = boundedInteger(input.rows, 24, 5, 300, 'row count');
    const id = this.idFactory();
    if (this.sessions.has(id)) throw new Error('Terminal ID collision.');
    const child = this.pty.spawn(this.shell, [], {
      name: 'xterm-256color',
      cols: cols,
      rows: rows,
      cwd: cwd,
      env: this.environment
    });
    const session = {
      id: id,
      rootId: rootId,
      child: child,
      cols: cols,
      rows: rows,
      sequence: 0,
      scrollback: '',
      exited: false,
      exitCode: null,
      signal: null,
      termination: null,
      scope: this._terminationScope(child.pid)
    };
    this.sessions.set(id, session);
    child.onData((data) => {
      session.sequence += 1;
      session.scrollback = trimUtf8Tail(session.scrollback + data, this.maxScrollbackBytes);
      this.emit('data', { id: id, sequence: session.sequence, data: data });
    });
    child.onExit((event) => {
      session.exited = true;
      session.exitCode = Number.isInteger(event.exitCode) ? event.exitCode : null;
      session.signal = event.signal === undefined ? null : event.signal;
      this.emit('exit', {
        id: id,
        sequence: session.sequence,
        exitCode: session.exitCode,
        signal: session.signal
      });
      if (session.termination && !this._terminationPids(session.child.pid, session.termination.scope).length) {
        this._completeTermination(session);
      }
    });
    return this.describe(session);
  }

  describe(session) {
    return {
      id: session.id,
      rootId: session.rootId,
      cols: session.cols,
      rows: session.rows,
      sequence: session.sequence,
      exited: session.exited
    };
  }

  list() {
    return Array.from(this.sessions.values(), (session) => this.describe(session));
  }

  replay(id) {
    const session = this.requireSession(id);
    return Object.assign(this.describe(session), {
      data: session.scrollback,
      exitCode: session.exitCode,
      signal: session.signal
    });
  }

  write(id, data) {
    const session = this.requireRunningSession(id);
    if (typeof data !== 'string' || Buffer.byteLength(data, 'utf8') > 64 * 1024) {
      throw new Error('Invalid terminal input.');
    }
    session.child.write(data);
    return { id: id, accepted: true };
  }

  resize(id, cols, rows) {
    const session = this.requireRunningSession(id);
    session.cols = boundedInteger(cols, session.cols, 20, 500, 'column count');
    session.rows = boundedInteger(rows, session.rows, 5, 300, 'row count');
    session.child.resize(session.cols, session.rows);
    return this.describe(session);
  }

  close(id) {
    return this.terminate(id);
  }

  closeAll() {
    Array.from(this.sessions.values()).forEach((session) => {
      this._signalSession(session, 'SIGKILL', session.scope);
      this.sessions.delete(session.id);
    });
  }

  closeByRoot(rootId) {
    return this.terminateByRoot(rootId);
  }

  terminateByRoot(rootId) {
    const matchingIds = Array.from(this.sessions.values())
      .filter((session) => session.rootId === rootId)
      .map((session) => session.id);
    return Promise.all(matchingIds.map((id) => this.terminate(id))).then(() => matchingIds.length);
  }

  terminate(id) {
    const session = this.requireSession(id);
    if (session.termination) return session.termination.promise;
    const exitedScopePids = session.exited && this.platform !== 'win32'
      ? this._terminationPids(session.child && session.child.pid, session.scope)
      : [];
    if (session.exited && exitedScopePids.length === 0) {
      this.sessions.delete(id);
      return Promise.resolve({ id: id, closed: true });
    }
    let resolveTermination;
    const termination = {
      promise: new Promise(function(resolve) { resolveTermination = resolve; }),
      resolve: resolveTermination,
      graceTimer: null,
      forceTimer: null,
      scope: session.scope && Number.isInteger(session.scope.sid)
        ? session.scope
        : this._terminationScope(session.child.pid)
    };
    session.termination = termination;
    this._signalSession(session, 'SIGTERM', termination.scope);
    termination.graceTimer = setTimeout(() => {
      this._signalSession(session, 'SIGKILL', termination.scope);
      termination.forceTimer = setTimeout(() => this._completeTermination(session), this.terminationGraceMs);
    }, this.terminationGraceMs);
    return termination.promise;
  }

  _signalSession(session, signal, scope) {
    if (!session.child) return;
    if (this.platform === 'win32' && session.child.pid) {
      try {
        const taskkill = this.spawnProcess('taskkill.exe', ['/pid', String(session.child.pid), '/t', '/f'], {
          windowsHide: true,
          stdio: 'ignore'
        });
        taskkill.unref();
        return;
      } catch (_) {}
    }
    if (this.platform !== 'win32' && session.child.pid) {
      this._terminationPids(session.child.pid, scope).forEach((pid) => {
        try { this.signalProcess(pid, signal); } catch (_) {}
      });
      try {
        this.signalProcess(-session.child.pid, signal);
        return;
      } catch (_) {}
    }
    try {
      session.child.kill(signal);
    } catch (_) {}
  }

  _terminationScope(parentPid) {
    const processTable = this._processTable();
    const root = processTable.get(parentPid);
    return root ? { sid: root.sid } : { sid: null };
  }

  _processTable() {
    const processTable = new Map();
    if (this.platform === 'win32') return processTable;
    try {
      const result = this.spawnSyncProcess('ps', ['-eo', 'pid=,ppid=,pgid=,sid='], {
        encoding: 'utf8',
        stdio: ['ignore', 'pipe', 'ignore'],
        timeout: 1000,
        windowsHide: true
      });
      if (!result || result.status !== 0 || typeof result.stdout !== 'string') return processTable;
      result.stdout.split(/\r?\n/).forEach((line) => {
        const match = line.trim().match(/^(\d+)\s+(\d+)\s+(\d+)\s+(\d+)$/);
        if (!match) return;
        processTable.set(Number(match[1]), {
          ppid: Number(match[2]),
          pgid: Number(match[3]),
          sid: Number(match[4])
        });
      });
    } catch (_) {}
    return processTable;
  }

  _terminationPids(parentPid, scope) {
    if (!Number.isInteger(parentPid) || parentPid < 2 || this.platform === 'win32') return [];
    const processTable = this._processTable();
    const root = processTable.get(parentPid);
    const sessionId = scope && Number.isInteger(scope.sid) ? scope.sid : (root && root.sid);
    if (!sessionId) return [];
    const descendants = [];
    const pending = Array.from(processTable.entries())
      .filter((entry) => entry[1].ppid === parentPid)
      .map((entry) => entry[0]);
    while (pending.length) {
      const pid = pending.pop();
      if (pid === parentPid || descendants.includes(pid)) continue;
      descendants.push(pid);
      Array.from(processTable.entries())
        .filter((entry) => entry[1].ppid === pid)
        .forEach((entry) => pending.push(entry[0]));
    }
    processTable.forEach((details, pid) => {
      if (pid !== parentPid && details.sid === sessionId && !descendants.includes(pid)) descendants.push(pid);
    });
    return descendants;
  }

  _completeTermination(session) {
    const termination = session.termination;
    if (!termination) return;
    session.termination = null;
    if (termination.graceTimer) clearTimeout(termination.graceTimer);
    if (termination.forceTimer) clearTimeout(termination.forceTimer);
    this.sessions.delete(session.id);
    termination.resolve({ id: session.id, closed: true });
  }

  requireSession(id) {
    const session = this.sessions.get(id);
    if (!session) throw new Error('Unknown terminal session.');
    return session;
  }

  requireRunningSession(id) {
    const session = this.requireSession(id);
    if (session.exited) throw new Error('Terminal session has exited.');
    return session;
  }
}

module.exports = {
  TerminalBroker: TerminalBroker,
  terminalEnvironment: terminalEnvironment,
  trimUtf8Tail: trimUtf8Tail
};