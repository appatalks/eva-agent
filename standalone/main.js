const { app, BrowserWindow, clipboard, dialog, ipcMain, Menu, safeStorage, session, shell } = require('electron');
const http = require('http');
const net = require('net');
const path = require('path');
const fs = require('fs');
const crypto = require('crypto');
const { spawn } = require('child_process');
const { fileURLToPath, pathToFileURL } = require('url');
const { buildContextMenuTemplate } = require('./context-menu');
const { TerminalBroker } = require('./terminal-broker');
const { redactKnownPaths } = require('./workspace-projection');

if (process.platform === 'win32') {
  app.commandLine.appendSwitch('autoplay-policy', 'no-user-gesture-required');
}

let bridgeProcess = null;
let readyBridgeProcess = null;
let bridgeStopTimer = null;
let bridgeStoppingProcess = null;
let localVoicesProcess = null;
let localSpeechBaseUrl = '';
let localSpeechToken = '';
let localSpeechProfileId = '';
let bridgeCapabilityToken = '';
let workspaceCapabilityToken = '';
let shuttingDown = false;
let stoppingBridge = false;
let mainWindow = null;
let terminalBroker = null;
let bridgeBaseUrl = '';

const BRIDGE_READY_TIMEOUT_MS = 60000;
const LOCAL_VOICES_READY_TIMEOUT_MS = 10000;
const BRIDGE_PORT_RETRY_LIMIT = 2;
const ADDRESS_IN_USE_PATTERN = /Address already in use|EADDRINUSE/i;
const AUTH_STORE_KEYS = ['OPENAI_API_KEY', 'GITHUB_PAT', 'GOOGLE_GL_KEY', 'GOOGLE_VISION_KEY'];

function workspaceTerminalEnabled() {
  return process.env.EVA_WORKSPACE_TERMINAL_V1 === '1' || process.argv.includes('--eva-workspace-terminal-v1');
}

function getTerminalAssetUrls() {
  if (!workspaceTerminalEnabled()) return {};
  return {
    xterm: pathToFileURL(require.resolve('@xterm/xterm')).href,
    css: pathToFileURL(require.resolve('@xterm/xterm/css/xterm.css')).href,
    fit: pathToFileURL(require.resolve('@xterm/addon-fit')).href,
    search: pathToFileURL(require.resolve('@xterm/addon-search')).href,
    webLinks: pathToFileURL(require.resolve('@xterm/addon-web-links')).href
  };
}

function initializeTerminalBroker() {
  if (!workspaceTerminalEnabled() || terminalBroker) return terminalBroker;
  terminalBroker = new TerminalBroker({ pty: require('node-pty') });
  terminalBroker.registerRoot('app-root', getAppRoot(), { allowSymlinks: true });
  terminalBroker.on('data', function(payload) {
    if (mainWindow && !mainWindow.isDestroyed()) mainWindow.webContents.send('terminal:data', payload);
  });
  terminalBroker.on('exit', function(payload) {
    if (mainWindow && !mainWindow.isDestroyed()) mainWindow.webContents.send('terminal:exit', payload);
  });
  return terminalBroker;
}

function requireTerminalBroker(event) {
  if (!isTrustedEvaRenderer(event)) throw new Error('Unauthorized renderer.');
  if (!terminalBroker) throw new Error('Workspace terminal is disabled.');
  return terminalBroker;
}

function requireWorkspaceFeature(event) {
  if (!isTrustedEvaRenderer(event)) throw new Error('Unauthorized renderer.');
  if (!workspaceTerminalEnabled()) throw new Error('Coding workspaces are disabled.');
}

function validWorkspaceId(value) {
  return typeof value === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value);
}

function requestWorkspaceBridge(pathname, method, payload) {
  if (!bridgeBaseUrl || !bridgeCapabilityToken) return Promise.reject(new Error('Workspace bridge is unavailable.'));
  const body = payload === undefined ? null : Buffer.from(JSON.stringify(payload), 'utf8');
  return new Promise(function(resolve, reject) {
    const request = http.request(bridgeBaseUrl + pathname, {
      method: method,
      headers: {
        'Authorization': 'Bearer ' + bridgeCapabilityToken,
        'X-Eva-Workspace-Capability': workspaceCapabilityToken,
        'Content-Type': 'application/json',
        'Content-Length': body ? String(body.length) : '0'
      },
      timeout: 30000
    }, function(response) {
      const chunks = [];
      let byteLength = 0;
      response.on('data', function(chunk) {
        byteLength += chunk.length;
        if (byteLength <= 2 * 1024 * 1024) chunks.push(chunk);
      });
      response.on('end', function() {
        if (byteLength > 2 * 1024 * 1024) {
          reject(new Error('Workspace bridge response was too large.'));
          return;
        }
        let data;
        try {
          data = JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}');
        } catch (_) {
          reject(new Error('Workspace bridge returned invalid JSON.'));
          return;
        }
        if (response.statusCode < 200 || response.statusCode >= 300) {
          reject(new Error((data.error && data.error.message) || 'Workspace bridge returned HTTP ' + response.statusCode));
          return;
        }
        resolve(data);
      });
    });
    request.setTimeout(30000, function() { request.destroy(new Error('Workspace bridge request timed out.')); });
    request.on('error', reject);
    if (body) request.write(body);
    request.end();
  });
}

function registerWorkspaceRoot(rootId, rootPath) {
  if (!terminalBroker || !validWorkspaceId(rootId) || typeof rootPath !== 'string') return;
  terminalBroker.registerRoot(rootId, rootPath);
}

async function ensureTerminalRoot(rootId) {
  if (!terminalBroker || rootId === 'app-root') return;
  if (!validWorkspaceId(rootId)) throw new Error('Invalid workspace checkout ID.');
  const response = await requestWorkspaceBridge('/v1/workspaces/checkouts/' + encodeURIComponent(rootId) + '/status', 'GET');
  const checkout = response.checkout;
  if (!checkout || checkout.id !== rootId || checkout.lifecycle !== 'active' || !['source', 'worktree'].includes(checkout.kind) || typeof checkout.path !== 'string') {
    throw new Error('Workspace checkout is unavailable for a terminal.');
  }
  registerWorkspaceRoot(rootId, checkout.path);
}

async function ensureEvaReadyWorkspace() {
  const response = await requestWorkspaceBridge('/v1/workspaces/eva-ready', 'POST');
  const project = response.project;
  if (!project || !validWorkspaceId(project.id) || typeof project.path !== 'string') {
    throw new Error('Workspace bridge returned an invalid Eva-ready project.');
  }
  return workspaceProjectForRenderer(project);
}

async function dispatchPendingWorkspaceRuns() {
  if (process.env.EVA_WORKSPACE_AGENT_AUTODISPATCH === '0') return;
  const response = await requestWorkspaceBridge('/v1/workspaces/runs', 'GET');
  const runs = Array.isArray(response.runs) ? response.runs : [];
  for (const run of runs) {
    if (run.status !== 'active') continue;
    try {
      await requestWorkspaceBridge('/v1/workspaces/runs/' + encodeURIComponent(run.id) + '/dispatch', 'POST');
    } catch (error) {
      console.error('Workspace agent dispatch failed for ' + run.id + ': ' + (error.message || error));
    }
  }
}

function workspaceCheckoutForRenderer(checkout) {
  if (!checkout || typeof checkout !== 'object') return null;
  return {
    id: checkout.id,
    projectId: checkout.project_id,
    kind: checkout.kind,
    branch: checkout.branch,
    baseRevision: checkout.base_revision,
    lifecycle: checkout.lifecycle,
    dirtyFileCount: checkout.dirty_file_count,
    ownerRefs: checkout.owner_refs
  };
}

function workspaceProjectForRenderer(project) {
  if (!project || typeof project !== 'object') return null;
  const mcp = project.mcp_servers && typeof project.mcp_servers === 'object' ? project.mcp_servers : {};
  return {
    id: project.id,
    name: project.name,
    createdAt: project.created_at,
    updatedAt: project.updated_at,
    activeRunCount: project.active_run_count,
    sourceCheckout: workspaceCheckoutForRenderer(project.source_checkout),
    mcpServers: {
      source: typeof mcp.source === 'string' ? mcp.source : 'mcp.json',
      state: typeof mcp.state === 'string' ? mcp.state : 'missing',
      message: typeof mcp.message === 'string' ? mcp.message : '',
      servers: Array.isArray(mcp.servers) ? mcp.servers.map(function(server) {
        return {
          name: server.name,
          transport: server.transport,
          enabled: server.enabled === true,
          digest: typeof server.digest === 'string' ? server.digest : '',
          command: typeof server.command === 'string' ? server.command : '',
          args: Array.isArray(server.args) ? server.args.filter(function(argument) { return typeof argument === 'string'; }) : [],
          url: typeof server.url === 'string' ? server.url : '',
          envKeys: Array.isArray(server.env_keys) ? server.env_keys.filter(function(key) { return typeof key === 'string'; }) : [],
          headerKeys: Array.isArray(server.header_keys) ? server.header_keys.filter(function(key) { return typeof key === 'string'; }) : []
        };
      }) : []
    }
  };
}

function workspaceRunForRenderer(run) {
  if (!run || typeof run !== 'object') return null;
  return {
    id: run.id,
    projectId: run.project_id,
    project: run.project ? { id: run.project.id, name: run.project.name } : null,
    checkout: workspaceCheckoutForRenderer(run.checkout),
    objective: run.objective,
    status: run.status,
    primarySessionId: run.primary_session_id,
    modelPolicy: run.model_policy,
    finalDisposition: run.final_disposition,
    createdAt: run.created_at,
    updatedAt: run.updated_at,
    archivedAt: run.archived_at,
    agent: run.agent ? {
      id: run.agent.id,
      status: run.agent.status,
      report: redactKnownPaths(run.agent.report, [
        run.checkout && run.checkout.path,
        run.project && run.project.path
      ]),
      capabilityPolicy: run.agent.capability_policy,
      createdAt: run.agent.created_at,
      updatedAt: run.agent.updated_at
    } : null
  };
}

async function workspaceListProjects(event) {
  requireWorkspaceFeature(event);
  const response = await requestWorkspaceBridge('/v1/workspaces/projects', 'GET');
  const projects = Array.isArray(response.projects) ? response.projects : [];
  return projects.map(workspaceProjectForRenderer).filter(Boolean);
}

async function workspaceSelectProject(event) {
  requireWorkspaceFeature(event);
  const selection = await dialog.showOpenDialog(mainWindow, {
    title: 'Choose a Git project',
    properties: ['openDirectory']
  });
  if (selection.canceled || !selection.filePaths[0]) return { canceled: true };
  let response;
  try {
    response = await requestWorkspaceBridge('/v1/workspaces/projects', 'POST', {
      path: selection.filePaths[0]
    });
  } catch (error) {
    return { canceled: false, error: error.message || 'The selected directory is not a Git repository.' };
  }
  const project = response.project;
  if (!project || !validWorkspaceId(project.id) || typeof project.path !== 'string') {
    throw new Error('Workspace bridge returned an invalid project record.');
  }
  return { canceled: false, project: workspaceProjectForRenderer(project) };
}

async function workspaceImportGitHub(event, repositoryUrl) {
  requireWorkspaceFeature(event);
  if (typeof repositoryUrl !== 'string' || repositoryUrl.length > 2048) {
    throw new Error('A valid GitHub repository URL is required.');
  }
  const response = await requestWorkspaceBridge('/v1/workspaces/github-import', 'POST', { url: repositoryUrl });
  const project = response.project;
  if (!project || !validWorkspaceId(project.id)) throw new Error('Workspace bridge returned an invalid imported project.');
  return workspaceProjectForRenderer(project);
}

async function workspaceSetMcpServer(event, projectId, serverName, enabled, approvedDigest) {
  requireWorkspaceFeature(event);
  if (!validWorkspaceId(projectId) || typeof serverName !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$/.test(serverName)) {
    throw new Error('Invalid workspace MCP server.');
  }
  if (typeof enabled !== 'boolean') throw new Error('Workspace MCP server state must be enabled or disabled.');
  const response = await requestWorkspaceBridge(
    '/v1/workspaces/projects/' + encodeURIComponent(projectId) + '/mcp-servers/' + encodeURIComponent(serverName),
    'POST',
    { enabled: enabled, approved_digest: typeof approvedDigest === 'string' ? approvedDigest : '' }
  );
  const project = response.project;
  if (!project || !validWorkspaceId(project.id)) throw new Error('Workspace bridge returned an invalid project record.');
  return workspaceProjectForRenderer(project);
}

async function workspaceCreateRun(event, request) {
  requireWorkspaceFeature(event);
  const input = request && typeof request === 'object' ? request : {};
  if (!validWorkspaceId(input.projectId)) throw new Error('Invalid project ID.');
  const response = await requestWorkspaceBridge('/v1/workspaces/runs', 'POST', {
    project_id: input.projectId,
    objective: typeof input.objective === 'string' ? input.objective : '',
    primary_session_id: typeof input.primarySessionId === 'string' ? input.primarySessionId : '',
    base_ref: typeof input.baseRef === 'string' ? input.baseRef : 'HEAD',
    model_policy: typeof input.modelPolicy === 'string' ? input.modelPolicy : ''
  });
  const run = response.run;
  if (!run || !run.checkout || !validWorkspaceId(run.checkout.id) || typeof run.checkout.path !== 'string') {
    throw new Error('Workspace bridge returned an invalid coding run.');
  }
  const projected = workspaceRunForRenderer(run);
  projected.dispatchError = typeof response.dispatch_error === 'string' ? response.dispatch_error : '';
  return projected;
}

async function workspaceListRuns(event, projectId) {
  requireWorkspaceFeature(event);
  const suffix = projectId ? '?project_id=' + encodeURIComponent(projectId) : '';
  const response = await requestWorkspaceBridge('/v1/workspaces/runs' + suffix, 'GET');
  const runs = Array.isArray(response.runs) ? response.runs : [];
  return runs.map(workspaceRunForRenderer).filter(Boolean);
}

async function workspaceListProjectFiles(event, projectId) {
  requireWorkspaceFeature(event);
  if (!validWorkspaceId(projectId)) throw new Error('Invalid project ID.');
  const response = await requestWorkspaceBridge(
    '/v1/workspaces/projects/' + encodeURIComponent(projectId) + '/files', 'GET'
  );
  return {
    files: Array.isArray(response.files) ? response.files.filter(function(file) { return typeof file === 'string'; }) : [],
    truncated: response.truncated === true
  };
}

async function workspaceOpenProjectFile(event, projectId, relativePath) {
  requireWorkspaceFeature(event);
  if (!validWorkspaceId(projectId) || typeof relativePath !== 'string') throw new Error('Invalid workspace file.');
  const response = await requestWorkspaceBridge('/v1/workspaces/projects/files/resolve', 'POST', {
    project_id: projectId,
    relative_path: relativePath
  });
  const pathValue = response.path;
  if (typeof pathValue !== 'string' || !path.isAbsolute(pathValue)) throw new Error('Workspace file could not be resolved.');
  const openError = await shell.openPath(pathValue);
  if (openError) throw new Error(openError);
  return { opened: true };
}

async function workspaceCheckoutStatus(event, checkoutId) {
  requireWorkspaceFeature(event);
  if (!validWorkspaceId(checkoutId)) throw new Error('Invalid workspace checkout ID.');
  const response = await requestWorkspaceBridge(
    '/v1/workspaces/checkouts/' + encodeURIComponent(checkoutId) + '/status', 'GET'
  );
  return workspaceCheckoutForRenderer(response.checkout);
}

async function workspaceListAssets(event) {
  requireWorkspaceFeature(event);
  const response = await requestWorkspaceBridge('/v1/workspaces/assets', 'GET');
  const assets = Array.isArray(response.assets) ? response.assets : [];
  return assets.map(function(asset) {
    return {
      id: asset.id,
      source: 'workspace',
      runId: asset.run_id,
      checkoutId: asset.checkout_id,
      projectName: asset.project_name,
      objective: asset.objective,
      name: asset.name,
      relativePath: asset.relative_path,
      size: asset.size,
      modified: asset.modified,
      agentStatus: asset.agent_status
    };
  });
}

async function workspaceOpenAsset(event, runId, relativePath) {
  requireWorkspaceFeature(event);
  if (!validWorkspaceId(runId) || typeof relativePath !== 'string') throw new Error('Invalid workspace asset.');
  const response = await requestWorkspaceBridge('/v1/workspaces/assets/resolve', 'POST', {
    run_id: runId,
    relative_path: relativePath
  });
  const pathValue = response.path;
  if (typeof pathValue !== 'string' || !path.isAbsolute(pathValue)) throw new Error('Workspace asset could not be resolved.');
  const openError = await shell.openPath(pathValue);
  if (openError) throw new Error(openError);
  return { opened: true };
}

async function workspaceRunAction(event, runId, action, options) {
  requireWorkspaceFeature(event);
  if (!validWorkspaceId(runId) || (action !== 'archive' && action !== 'discard')) throw new Error('Invalid workspace action.');
  if (action === 'discard' && terminalBroker) {
    const requestedCheckoutId = options && options.checkoutId;
    if (!validWorkspaceId(requestedCheckoutId)) throw new Error('Invalid workspace checkout ID.');
    const current = await requestWorkspaceBridge('/v1/workspaces/runs/' + encodeURIComponent(runId), 'GET');
    const checkoutId = current.run && current.run.checkout && current.run.checkout.id;
    if (!validWorkspaceId(checkoutId) || checkoutId !== requestedCheckoutId) {
      throw new Error('Workspace checkout no longer matches this run.');
    }
    const agentStatus = current.run && current.run.agent && current.run.agent.status;
    if (['starting', 'running', 'steering'].includes(agentStatus)) {
      throw new Error('The workspace agent is still running. Wait for completion before discard.');
    }
    await terminalBroker.terminateByRoot(requestedCheckoutId);
    if (terminalBroker.list().some(function(session) { return session.rootId === requestedCheckoutId; })) {
      throw new Error('Workspace terminal did not terminate; discard was cancelled.');
    }
  }
  const response = await requestWorkspaceBridge('/v1/workspaces/runs/' + encodeURIComponent(runId) + '/' + action, 'POST', {
    confirm_dirty: !!(options && options.confirmDirty)
  });
  if (action === 'discard' && terminalBroker && response.run && response.run.status === 'discarded') {
    const checkoutId = response.run.checkout && response.run.checkout.id;
    if (validWorkspaceId(checkoutId)) {
      await terminalBroker.terminateByRoot(checkoutId);
      if (terminalBroker.list().some(function(session) { return session.rootId === checkoutId; })) {
        throw new Error('Workspace terminal did not terminate after discard.');
      }
      terminalBroker.unregisterRoot(checkoutId);
    }
  }
  return workspaceRunForRenderer(response.run);
}

function loadEncryptedAuth() {
  if (!safeStorage.isEncryptionAvailable()) return {};
  const authPath = path.join(app.getPath('userData'), 'auth.enc.json');
  try {
    const encrypted = JSON.parse(fs.readFileSync(authPath, 'utf8'));
    const result = {};
    AUTH_STORE_KEYS.forEach(function(key) {
      if (typeof encrypted[key] !== 'string') return;
      result[key] = safeStorage.decryptString(Buffer.from(encrypted[key], 'base64'));
    });
    return result;
  } catch (_) {
    return {};
  }
}

function saveEncryptedAuth(values) {
  if (!safeStorage.isEncryptionAvailable() || !values || typeof values !== 'object') return false;
  const encrypted = {};
  AUTH_STORE_KEYS.forEach(function(key) {
    const value = typeof values[key] === 'string' ? values[key].trim() : '';
    if (value) encrypted[key] = safeStorage.encryptString(value).toString('base64');
  });
  const authPath = path.join(app.getPath('userData'), 'auth.enc.json');
  const temporaryPath = authPath + '.tmp';
  fs.writeFileSync(temporaryPath, JSON.stringify(encrypted), { mode: 0o600 });
  fs.renameSync(temporaryPath, authPath);
  return true;
}

function isTrustedEvaRenderer(event) {
  try {
    const rendererPath = fileURLToPath(event.senderFrame.url);
    return path.resolve(rendererPath) === path.resolve(getAppRoot(), 'index.html');
  } catch (_) {
    return false;
  }
}

function clearBridgeStopTimer() {
  if (bridgeStopTimer) {
    clearTimeout(bridgeStopTimer);
    bridgeStopTimer = null;
  }
}

function groupSignal(child, signal) {
  if (!child) return;
  if (process.platform === 'win32') {
    if (child.pid) {
      try {
        const taskkill = spawn('taskkill.exe', ['/pid', String(child.pid), '/t', '/f'], {
          windowsHide: true,
          stdio: 'ignore'
        });
        taskkill.unref();
        return;
      } catch (_) {}
    }
    try {
      child.kill(signal);
    } catch (_) {}
    return;
  }
  try {
    if (child.pid) {
      process.kill(-child.pid, signal);
      return;
    }
  } catch (_) {}
  try {
    child.kill(signal);
  } catch (_) {}
}

function createAddressInUseError(port) {
  const err = new Error('ACP bridge could not bind to 127.0.0.1:' + port + ' because the port was already in use. Retrying with a new local port.');
  err.code = 'EADDRINUSE';
  err.port = port;
  return err;
}

function createPortRetryError() {
  const attempts = BRIDGE_PORT_RETRY_LIMIT + 1;
  return new Error('ACP bridge could not bind to a localhost port after ' + attempts + ' attempts because the selected ports were already in use. Close the process using the port or restart Eva Standalone.');
}

function formatExitDetails(code, signal) {
  return 'exit code ' + (code === null ? 'none' : code) + ', signal ' + (signal === null ? 'none' : signal);
}

function getStartupErrorTitle(err) {
  return err && err.code === 'ENOENT' ? 'Python 3 is required' : 'Eva Standalone could not start';
}

function getStartupErrorMessage(err) {
  if (err && err.code === 'ENOENT') {
    return 'Eva Standalone needs Python 3.12 or newer to start the bundled ACP bridge. Install Python and try again.';
  }
  return err && err.message ? err.message : String(err);
}

function logFatalError(label, err) {
  console.error(label, err && err.stack ? err.stack : err);
}

function exitAfterFatalError(label, err) {
  logFatalError(label, err);
  try {
    forceKillBridgeSync();
  } finally {
    process.exit(1);
  }
}

function getAppRoot() {
  if (app.isPackaged) {
    return path.join(process.resourcesPath, 'app');
  }
  return path.resolve(__dirname, '..');
}

function getLocalVoicesDirectory() {
  if (process.platform === 'win32') {
    return path.join(process.env.LOCALAPPDATA || app.getPath('userData'), 'Eva Standalone', 'local-voices', 'voices');
  }
  return path.join(process.env.HOME || '', '.local', 'share', 'eva', 'local-voices', 'voices');
}

function getLocalSpeechAcknowledgementCacheDirectory() {
  return path.join(path.dirname(getLocalVoicesDirectory()), 'acknowledgements');
}

function getWindowsRuntime() {
  if (process.platform !== 'win32') return null;
  const manifestPath = path.join(process.env.LOCALAPPDATA || app.getPath('userData'), 'Eva Standalone', 'runtime', 'runtime.json');
  try {
    const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8').replace(/^\uFEFF/, ''));
    return manifest && typeof manifest === 'object' ? manifest : null;
  } catch (_) {
    return null;
  }
}

function getPythonInvocation() {
  if (process.env.EVA_PYTHON) return { command: process.env.EVA_PYTHON, args: [] };
  const windowsRuntime = getWindowsRuntime();
  if (windowsRuntime && typeof windowsRuntime.python === 'string' && Array.isArray(windowsRuntime.pythonArgs)) {
    return { command: windowsRuntime.python, args: windowsRuntime.pythonArgs };
  }
  if (process.platform === 'win32') return { command: 'py', args: ['-3'] };
  return { command: 'python3', args: [] };
}

function getCopilotInvocation() {
  const windowsRuntime = getWindowsRuntime();
  if (windowsRuntime && typeof windowsRuntime.copilot === 'string' && fs.existsSync(windowsRuntime.copilot)) {
    return windowsRuntime.copilot;
  }
  return null;
}

function getLocalSpeechInvocation(pythonPath) {
  if (pythonPath) return { command: pythonPath, args: [] };
  if (process.env.LOCAL_VOICES_PYTHON) return { command: process.env.LOCAL_VOICES_PYTHON, args: [] };
  if (process.env.EVA_PYTHON) return { command: process.env.EVA_PYTHON, args: [] };
  const windowsRuntime = getWindowsRuntime();
  if (windowsRuntime && typeof windowsRuntime.localSpeechPython === 'string' && fs.existsSync(windowsRuntime.localSpeechPython)) {
    return { command: windowsRuntime.localSpeechPython, args: [] };
  }
  const managedPython = path.join(process.env.HOME || '', '.local', 'share', 'eva', 'local-voices', '.venv', 'bin', 'python');
  if (fs.existsSync(managedPython)) return { command: managedPython, args: [] };
  return getPythonInvocation();
}

const DEFAULT_LOCAL_VOICE_PROFILE = 'bundled:eva-english';
const LOCAL_SPEECH_ACK_CACHE_VERSION = 'v1';
const MAX_LOCAL_SPEECH_ACK_CACHE_ENTRIES = 96;
const LOCAL_SPEECH_ACK_TIMEOUT_MS = 20000;
const acknowledgementSynthesisJobs = new Map();
const acknowledgementSynthesisQueue = { running: false, urgent: [], background: [] };
const BUNDLED_LOCAL_VOICE_PROFILES = [
  { id: 'bundled:eva-english', label: 'Eva English', language: 'en', file: 'eva_voice_profile-english.wav' },
  { id: 'bundled:eva-korean', label: 'Eva Korean', language: 'ko', file: 'eva_voice_profile-korean.wav' },
  { id: 'bundled:appatalks-english', label: 'AppaTalks English', language: 'en', file: 'appatalks_voice_profile-english.wav' }
];

function resolveBundledLocalVoiceReference(voiceId) {
  const profile = BUNDLED_LOCAL_VOICE_PROFILES.find(function(item) { return item.id === voiceId; });
  if (!profile) return null;
  const directory = path.join(getAppRoot(), 'core', 'audio');
  const reference = path.join(directory, profile.file);
  const stat = fs.lstatSync(reference);
  if (!stat.isFile() || stat.isSymbolicLink() || fs.realpathSync(reference) !== reference) {
    throw new Error('The selected bundled Local Voices profile is unavailable.');
  }
  return reference;
}

function getLocalVoiceProfiles() {
  const profiles = BUNDLED_LOCAL_VOICE_PROFILES.filter(function(profile) {
    try { return !!resolveBundledLocalVoiceReference(profile.id); } catch (_) { return false; }
  }).map(function(profile) {
    return { id: profile.id, label: profile.label, language: profile.language, bundled: true };
  });
  const directory = getLocalVoicesDirectory();
  try {
    fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
    fs.chmodSync(directory, 0o700);
    fs.readdirSync(directory).filter(function(name) {
      return name.toLowerCase().endsWith('.wav');
    }).sort().forEach(function(name) {
      profiles.push({
        id: 'custom:' + name,
        label: path.basename(name, path.extname(name)),
        language: null,
        bundled: false
      });
    });
  } catch (_) {}
  return profiles;
}

function readWavDuration(filePath) {
  const buffer = fs.readFileSync(filePath);
  if (buffer.length < 44 || buffer.toString('ascii', 0, 4) !== 'RIFF' || buffer.toString('ascii', 8, 12) !== 'WAVE') {
    throw new Error('Choose a PCM WAV file.');
  }
  let offset = 12;
  let byteRate = 0;
  let dataLength = 0;
  while (offset + 8 <= buffer.length) {
    const chunkId = buffer.toString('ascii', offset, offset + 4);
    const chunkLength = buffer.readUInt32LE(offset + 4);
    const dataOffset = offset + 8;
    if (chunkId === 'fmt ' && chunkLength >= 16) {
      if (buffer.readUInt16LE(dataOffset) !== 1) throw new Error('Choose an uncompressed PCM WAV file.');
      byteRate = buffer.readUInt32LE(dataOffset + 8);
    } else if (chunkId === 'data') {
      dataLength = chunkLength;
      break;
    }
    offset = dataOffset + chunkLength + (chunkLength % 2);
  }
  if (!byteRate || !dataLength) throw new Error('Choose a valid PCM WAV file.');
  return dataLength / byteRate;
}

async function importLocalVoiceProfile() {
  const result = await dialog.showOpenDialog({
    title: 'Add Local Voice',
    properties: ['openFile'],
    filters: [{ name: 'WAV audio', extensions: ['wav'] }]
  });
  if (result.canceled || !result.filePaths[0]) return { canceled: true, profiles: getLocalVoiceProfiles() };
  const source = result.filePaths[0];
  const duration = readWavDuration(source);
  if (duration > 10.01) throw new Error('Voice samples must be 10 seconds or shorter.');
  if (duration < 5) throw new Error('Voice samples must be at least 5 seconds long.');
  const directory = getLocalVoicesDirectory();
  fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
  fs.chmodSync(directory, 0o700);
  const base = path.basename(source, path.extname(source)).replace(/[^a-zA-Z0-9._-]+/g, '-').replace(/^-+|-+$/g, '') || 'voice';
  let name = base + '.wav';
  let index = 2;
  while (fs.existsSync(path.join(directory, name))) {
    name = base + '-' + index + '.wav';
    index += 1;
  }
  const destination = path.join(directory, name);
  fs.copyFileSync(source, destination);
  fs.chmodSync(destination, 0o600);
  return { canceled: false, selected: 'custom:' + name, profiles: getLocalVoiceProfiles() };
}

function resolveLocalVoiceReference(voiceId) {
  const bundledReference = resolveBundledLocalVoiceReference(voiceId);
  if (bundledReference) return bundledReference;
  if (!voiceId.startsWith('custom:')) throw new Error('Unknown Local Voices profile.');
  const name = voiceId.slice('custom:'.length);
  if (path.basename(name) !== name || !name.toLowerCase().endsWith('.wav')) throw new Error('Invalid Local Voices profile.');
  const directory = fs.realpathSync(getLocalVoicesDirectory());
  const reference = path.join(directory, name);
  const stat = fs.lstatSync(reference);
  if (!stat.isFile() || stat.isSymbolicLink() || fs.realpathSync(reference) !== reference) {
    throw new Error('The selected Local Voices profile is unavailable.');
  }
  return reference;
}

function normalizeLocalSpeechLanguage(value, allowAuto) {
  const language = String(value || 'auto').trim().toLowerCase();
  if (allowAuto && language === 'auto') return language;
  if (language !== 'en' && language !== 'ko') {
    throw new Error('Unsupported local speech language. Choose Automatic, English, or Korean.');
  }
  return language;
}

function detectLocalSpeechLanguage(text) {
  const value = String(text || '');
  const korean = (value.match(/[\uac00-\ud7a3]/g) || []).length;
  const latin = (value.match(/[A-Za-z]/g) || []).length;
  return korean > latin ? 'ko' : 'en';
}

function resolveLocalVoiceForSynthesis(voiceId, language, automatic) {
  const selected = getLocalVoiceProfiles().find(function(profile) { return profile.id === voiceId; });
  let profile = selected;
  if (!profile || profile.language !== language) {
    // Imported recordings are intentionally unclassified. An explicit language
    // selection is the user's declaration that this reference is compatible.
    if (!automatic && profile && profile.language === null) {
      return { reference: resolveLocalVoiceReference(profile.id), language: language, profileId: profile.id };
    }
    if (!automatic) {
      throw new Error('The selected voice profile does not match the requested speech language. Choose a matching profile or Automatic.');
    }
    profile = BUNDLED_LOCAL_VOICE_PROFILES.find(function(item) { return item.language === language; });
  }
  if (!profile) throw new Error('No local voice profile is available for the requested speech language.');
  return { reference: resolveLocalVoiceReference(profile.id), language: profile.language, profileId: profile.id };
}

function acknowledgementCachePath(profile, text) {
  const cacheKey = [LOCAL_SPEECH_ACK_CACHE_VERSION, profile.profileId, profile.language, text].join('\0');
  const name = crypto.createHash('sha256').update(cacheKey, 'utf8').digest('hex') + '.wav';
  return path.join(getLocalSpeechAcknowledgementCacheDirectory(), name);
}

function readAcknowledgementCache(cachePath) {
  try {
    const stat = fs.lstatSync(cachePath);
    if (!stat.isFile() || stat.isSymbolicLink() || stat.size < 44 || stat.size > 16 * 1024 * 1024) return null;
    const audio = fs.readFileSync(cachePath);
    try { fs.utimesSync(cachePath, new Date(), stat.mtime); } catch (_) {}
    return audio;
  } catch (_) {
    return null;
  }
}

function writeAcknowledgementCache(cachePath, audio) {
  const directory = getLocalSpeechAcknowledgementCacheDirectory();
  fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
  try { fs.chmodSync(directory, 0o700); } catch (_) {}
  const temporary = cachePath + '.' + process.pid + '.' + crypto.randomBytes(4).toString('hex') + '.tmp';
  try {
    fs.writeFileSync(temporary, audio, { mode: 0o600 });
    if (fs.existsSync(cachePath)) return;
    fs.renameSync(temporary, cachePath);
    try { fs.chmodSync(cachePath, 0o600); } catch (_) {}
  } finally {
    try { fs.unlinkSync(temporary); } catch (_) {}
  }
  try {
    const entries = fs.readdirSync(directory).filter(function(name) {
      return /^[a-f0-9]{64}\.wav$/.test(name);
    }).map(function(name) {
      const filePath = path.join(directory, name);
      return { path: filePath, mtime: fs.statSync(filePath).mtimeMs };
    }).sort(function(left, right) { return right.mtime - left.mtime; });
    entries.slice(MAX_LOCAL_SPEECH_ACK_CACHE_ENTRIES).forEach(function(entry) { fs.unlinkSync(entry.path); });
  } catch (_) {}
}

async function synthesizeLocalSpeech(payload, cacheAcknowledgement, timeoutMs) {
  payload = typeof payload === 'string' ? { input: payload } : (payload || {});
  const value = String(payload.input || '').trim();
  const maxLength = cacheAcknowledgement ? 160 : 12000;
  if (!value || value.length > maxLength) throw new Error('Invalid speech input.');
  const requestedLanguage = normalizeLocalSpeechLanguage(payload.language, true);
  const language = requestedLanguage === 'auto' ? detectLocalSpeechLanguage(value) : requestedLanguage;
  const languageMode = normalizeLocalSpeechLanguage(payload.languageMode, true);
  const profile = resolveLocalVoiceForSynthesis(
    String(payload.profileId || DEFAULT_LOCAL_VOICE_PROFILE),
    language,
    languageMode === 'auto'
  );
  const cachePath = cacheAcknowledgement ? acknowledgementCachePath(profile, value) : '';
  const cached = cachePath ? readAcknowledgementCache(cachePath) : null;
  if (cached) return { audio: cached, cached: true };
  const audio = await requestLocalSpeech('/v1/speech', 'POST', JSON.stringify({
    input: value,
    language: language,
    reference: profile.reference
  }), 'application/json', timeoutMs);
  if (cachePath) writeAcknowledgementCache(cachePath, audio);
  return { audio: audio, cached: false };
}

function queueAcknowledgementSynthesis(payload, urgent) {
  payload = typeof payload === 'string' ? { input: payload } : (payload || {});
  const value = String(payload.input || '').trim();
  const requestedLanguage = normalizeLocalSpeechLanguage(payload.language, true);
  const language = requestedLanguage === 'auto' ? detectLocalSpeechLanguage(value) : requestedLanguage;
  const languageMode = normalizeLocalSpeechLanguage(payload.languageMode, true);
  const profile = resolveLocalVoiceForSynthesis(
    String(payload.profileId || DEFAULT_LOCAL_VOICE_PROFILE),
    language,
    languageMode === 'auto'
  );
  const cachePath = acknowledgementCachePath(profile, value);
  const cached = readAcknowledgementCache(cachePath);
  if (cached) return Promise.resolve({ audio: cached, cached: true });
  if (acknowledgementSynthesisJobs.has(cachePath)) return acknowledgementSynthesisJobs.get(cachePath);

  let resolveJob;
  let rejectJob;
  const promise = new Promise(function(resolve, reject) {
    resolveJob = resolve;
    rejectJob = reject;
  });
  acknowledgementSynthesisJobs.set(cachePath, promise);
  const job = function() {
    return synthesizeLocalSpeech(payload, true, LOCAL_SPEECH_ACK_TIMEOUT_MS).then(resolveJob, rejectJob).finally(function() {
      acknowledgementSynthesisJobs.delete(cachePath);
    });
  };
  if (urgent) acknowledgementSynthesisQueue.urgent.push(job);
  else acknowledgementSynthesisQueue.background.push(job);
  runAcknowledgementSynthesisQueue();
  return promise;
}

function runAcknowledgementSynthesisQueue() {
  if (acknowledgementSynthesisQueue.running) return;
  const job = acknowledgementSynthesisQueue.urgent.shift() || acknowledgementSynthesisQueue.background.shift();
  if (!job) return;
  acknowledgementSynthesisQueue.running = true;
  job().finally(function() {
    acknowledgementSynthesisQueue.running = false;
    runAcknowledgementSynthesisQueue();
  });
}

function getFreeLocalPort() {
  return new Promise(function(resolve, reject) {
    const server = net.createServer();
    server.unref();
    server.on('error', reject);
    server.listen(0, '127.0.0.1', function() {
      const address = server.address();
      const port = address && address.port;
      server.close(function() {
        if (port) {
          resolve(port);
        } else {
          reject(new Error('Unable to allocate a localhost port.'));
        }
      });
    });
  });
}

function requestBridgeHealth(baseUrl) {
  return new Promise(function(resolve, reject) {
    const req = http.get(baseUrl.replace(/\/+$/, '') + '/health', function(res) {
      let body = '';
      res.setEncoding('utf8');
      res.on('data', function(chunk) { body += chunk; });
      res.on('end', function() {
        if (res.statusCode !== 200) {
          reject(new Error('Bridge health returned HTTP ' + res.statusCode));
          return;
        }
        try {
          const data = JSON.parse(body);
          if (data.status === 'ok') {
            resolve(data);
          } else {
            reject(new Error('Bridge health status is ' + data.status));
          }
        } catch (err) {
          reject(err);
        }
      });
    });
    req.setTimeout(2000, function() {
      req.destroy(new Error('Bridge health timed out.'));
    });
    req.on('error', reject);
  });
}

function waitForBridge(baseUrl, childProcess, timeoutMs) {
  const startedAt = Date.now();
  return new Promise(function(resolve, reject) {
    let settled = false;

    function finish(fn, value) {
      if (settled) return;
      settled = true;
      childProcess.off('exit', onExit);
      childProcess.off('error', onError);
      childProcess.off('eva-address-in-use', onAddressInUse);
      fn(value);
    }

    function onAddressInUse(err) {
      finish(reject, err);
    }

    function onError(err) {
      childProcess.evaSpawnError = err;
      finish(reject, err);
    }

    function onExit(code, signal) {
      if (childProcess.evaAddressInUseError) {
        finish(reject, childProcess.evaAddressInUseError);
        return;
      }
      finish(reject, new Error('ACP bridge exited before it was ready (' + formatExitDetails(code, signal) + ').'));
    }

    function poll() {
      if (settled) return;
      requestBridgeHealth(baseUrl).then(function(data) {
        finish(resolve, data);
      }).catch(function(err) {
        if (settled) return;
        if (Date.now() - startedAt >= timeoutMs) {
          finish(reject, new Error('Timed out waiting for ACP bridge: ' + err.message));
          return;
        }
        setTimeout(poll, 500);
      });
    }

    childProcess.on('exit', onExit);
    childProcess.on('error', onError);
    childProcess.on('eva-address-in-use', onAddressInUse);
    if (childProcess.evaSpawnError) {
      finish(reject, childProcess.evaSpawnError);
      return;
    }
    if (childProcess.evaAddressInUseError) {
      finish(reject, childProcess.evaAddressInUseError);
      return;
    }
    poll();
  });
}

function waitForBridgeExit(childProcess, timeoutMs) {
  return new Promise(function(resolve) {
    if (!childProcess || childProcess.exitCode !== null || childProcess.signalCode !== null) {
      resolve();
      return;
    }
    let settled = false;
    const timer = setTimeout(done, timeoutMs);

    function done() {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      childProcess.off('exit', done);
      resolve();
    }

    childProcess.once('exit', done);
  });
}

function startBridge(port, bridgeToken, workspaceToken) {
  const appRoot = getAppRoot();
  const bridgePath = path.join(appRoot, 'tools', 'acp_bridge.py');
  const python = getPythonInvocation();
  const copilot = getCopilotInvocation();
  const args = python.args.concat([bridgePath, '--bind', '127.0.0.1', '--port', String(port), '--cwd', appRoot]);
  if (copilot) args.push('--copilot-path', copilot);
  const env = Object.assign({}, process.env, {
    EVA_ACP_PORT: String(port),
    EVA_BRIDGE_TOKEN: bridgeToken,
    EVA_WORKSPACE_CAPABILITY: workspaceToken,
    KUSTO_DATABASE_LOCKED: '1',
    PYTHONUNBUFFERED: '1'
  });
  if (process.platform === 'win32') {
    env.EVA_CONFIG_DIR = path.join(app.getPath('userData'), 'bridge');
  }

  // GUI-launched apps on macOS inherit a stripped PATH that often misses
  // Homebrew, python.org, and nvm bin directories. Augment PATH so the bridge
  // can find python3 and copilot. Harmless on Linux.
  if (process.platform === 'darwin') {
    const extraPaths = [
      '/opt/homebrew/bin',
      '/usr/local/bin',
      '/usr/local/sbin',
      path.join(process.env.HOME || '', '.local/bin'),
      path.join(process.env.HOME || '', '.npm-global/bin')
    ].filter(Boolean);
    const currentPath = env.PATH || '';
    const merged = extraPaths.concat(currentPath.split(':')).filter(function (p, i, arr) {
      return p && arr.indexOf(p) === i;
    }).join(':');
    env.PATH = merged;
  }

  const child = spawn(python.command, args, {
    cwd: appRoot,
    env: env,
    detached: process.platform !== 'win32',
    windowsHide: process.platform === 'win32',
    stdio: ['ignore', 'pipe', 'pipe']
  });
  let stderrBuffer = '';

  bridgeProcess = child;
  child.evaAwaitingReady = true;
  child.evaClearStderrBuffer = function() {
    stderrBuffer = '';
  };

  child.stdout.on('data', function(chunk) {
    process.stdout.write('[eva-acp] ' + chunk.toString());
  });
  child.stderr.on('data', function(chunk) {
    const text = chunk.toString();
    process.stderr.write('[eva-acp] ' + text);
    stderrBuffer = (stderrBuffer + text).slice(-1000);
    if (child.evaAwaitingReady && !child.evaAddressInUseError && ADDRESS_IN_USE_PATTERN.test(stderrBuffer)) {
      const err = createAddressInUseError(port);
      child.evaAddressInUseError = err;
      child.emit('eva-address-in-use', err);
      child.kill('SIGTERM');
    }
  });
  child.on('error', function(err) {
    child.evaSpawnError = err;
  });
  child.on('exit', function(code, signal) {
    const wasReady = readyBridgeProcess === child;
    if (bridgeProcess === child) {
      bridgeProcess = null;
    }
    if (wasReady) {
      readyBridgeProcess = null;
    }
    if (bridgeStoppingProcess === child) {
      clearBridgeStopTimer();
      bridgeStoppingProcess = null;
      stoppingBridge = false;
    }
    if (wasReady && !shuttingDown) {
      dialog.showErrorBox('ACP bridge stopped', 'The local ACP bridge stopped unexpectedly (' + formatExitDetails(code, signal) + '). Eva Standalone will close so it does not keep running with a broken backend. Restart Eva Standalone to continue.');
      app.quit();
    }
  });

  return child;
}

function forceKillBridgeSync() {
  shuttingDown = true;
  const child = bridgeProcess;
  if (!child || !child.pid) return;

  if (process.platform !== 'win32') {
    try {
      process.kill(-child.pid, 'SIGKILL');
      return;
    } catch (_) {}
  }
  try {
    child.kill('SIGKILL');
  } catch (_) {}
}

function stopBridge() {
  shuttingDown = true;
  stopManagedLocalVoices();
  if (stoppingBridge) return;
  if (!bridgeProcess) return;

  const child = bridgeProcess;
  stoppingBridge = true;
  bridgeStoppingProcess = child;
  groupSignal(child, 'SIGTERM');
  bridgeStopTimer = setTimeout(function() {
    if (bridgeStoppingProcess === child) {
      groupSignal(child, 'SIGKILL');
    }
  }, 3000);
}

function isChildRunning(child) {
  return !!child && child.exitCode === null && child.signalCode === null;
}

function requestLocalSpeech(pathname, method, body, contentType, timeoutMs, extraHeaders) {
  if (!localSpeechBaseUrl || !localSpeechToken) return Promise.reject(new Error('Local speech service is not running.'));
  const requestBody = body == null ? null : (Buffer.isBuffer(body) || typeof body === 'string' ? body : Buffer.from(body));
  return new Promise(function(resolve, reject) {
    const request = http.request(localSpeechBaseUrl + pathname, {
      method: method,
      headers: Object.assign({
        'Authorization': 'Bearer ' + localSpeechToken,
        'Content-Type': contentType || 'application/json',
        'Content-Length': requestBody ? Buffer.byteLength(requestBody) : 0
      }, extraHeaders || {}),
      timeout: timeoutMs || 180000
    }, function(res) {
      const chunks = [];
      res.on('data', function(chunk) { chunks.push(chunk); });
      res.on('end', function() {
        const response = Buffer.concat(chunks);
        if (res.statusCode < 200 || res.statusCode >= 300) {
          let detail = '';
          try { detail = JSON.parse(response.toString('utf8')).error || ''; } catch (_) {}
          reject(new Error('Local speech service returned HTTP ' + res.statusCode + (detail ? ': ' + detail : ''))); return;
        }
        try {
          if ((res.headers['content-type'] || '').toLowerCase().startsWith('audio/')) resolve(response);
          else resolve(JSON.parse(response.toString('utf8')));
        } catch (err) {
          reject(err);
        }
      });
    });
    request.setTimeout(timeoutMs || 180000, function() {
      request.destroy(new Error('Local speech request timed out.'));
    });
    request.on('error', reject);
    if (requestBody) request.write(requestBody);
    request.end();
  });
}

function requestLocalVoicesHealth() {
  return requestLocalSpeech('/health', 'GET', null, 'application/json', 2000);
}

function waitForLocalVoices(child, timeoutMs) {
  const startedAt = Date.now();
  return new Promise(function(resolve, reject) {
    function poll() {
      if (!isChildRunning(child)) {
        reject(new Error('Local Voices stopped before it was ready (' + formatExitDetails(child.exitCode, child.signalCode) + ').'));
        return;
      }
      requestLocalVoicesHealth().then(resolve).catch(function(err) {
        if (Date.now() - startedAt >= timeoutMs) {
          reject(new Error('Timed out waiting for Local Voices: ' + err.message));
          return;
        }
        setTimeout(poll, 300);
      });
    }
    poll();
  });
}

async function getLocalVoicesStatus() {
  try {
    const health = await requestLocalVoicesHealth();
    return { running: true, managed: isChildRunning(localVoicesProcess), health: health };
  } catch (_) {
    return { running: false, managed: isChildRunning(localVoicesProcess), health: null };
  }
}

async function startLocalVoices(pythonPath, voiceId) {
  const status = await getLocalVoicesStatus();
  // Profiles are resolved for each authenticated synthesis request. Keeping one
  // bridge process alive lets English and Korean alternate without losing STT.
  if (status.running) return status;
  if (isChildRunning(localVoicesProcess)) {
    throw new Error('Local speech service is already starting.');
  }

  const appRoot = getAppRoot();
  const bridgePath = path.join(appRoot, 'tools', 'local_voices_bridge.py');
  const localSpeech = getLocalSpeechInvocation(pythonPath);
  const pythonCmd = localSpeech.command;
  const pythonArgs = localSpeech.args;
  localSpeechBaseUrl = '';
  localSpeechToken = crypto.randomBytes(32).toString('hex');
  const args = pythonArgs.concat([bridgePath, '--host', '127.0.0.1', '--port', '0']);
  const child = spawn(pythonCmd, args, {
    cwd: appRoot,
    env: Object.assign({}, process.env, { PYTHONUNBUFFERED: '1', EVA_LOCAL_SPEECH_TOKEN: localSpeechToken }),
    detached: process.platform !== 'win32',
    windowsHide: process.platform === 'win32',
    stdio: ['ignore', 'pipe', 'pipe']
  });
  localVoicesProcess = child;
  localSpeechProfileId = '';
  let addressReady;
  const addressPromise = new Promise(function(resolve, reject) { addressReady = { resolve: resolve, reject: reject }; });

  child.stdout.on('data', function(chunk) {
    const text = chunk.toString();
    process.stdout.write('[eva-local-voices] ' + text);
    const match = text.match(/listening on (http:\/\/127\.0\.0\.1:\d+)/);
    if (match) {
      localSpeechBaseUrl = match[1];
      addressReady.resolve(localSpeechBaseUrl);
    }
  });
  child.stderr.on('data', function(chunk) {
    process.stderr.write('[eva-local-voices] ' + chunk.toString());
  });
  child.on('exit', function() {
    if (localVoicesProcess === child) {
      localVoicesProcess = null;
      localSpeechBaseUrl = '';
      localSpeechToken = '';
      localSpeechProfileId = '';
    }
    addressReady.reject(new Error('Local speech service stopped before publishing its address.'));
  });

  try {
    await Promise.race([
      addressPromise,
      new Promise(function(_resolve, reject) { setTimeout(function() { reject(new Error('Timed out waiting for Local Voices to bind.')); }, LOCAL_VOICES_READY_TIMEOUT_MS); })
    ]);
    const health = await waitForLocalVoices(child, LOCAL_VOICES_READY_TIMEOUT_MS);
    return { running: true, managed: true, health: health };
  } catch (err) {
    groupSignal(child, 'SIGTERM');
    localSpeechBaseUrl = '';
    localSpeechToken = '';
    localSpeechProfileId = '';
    throw err;
  }
}

async function stopLocalVoices() {
  const child = localVoicesProcess;
  if (!isChildRunning(child)) return { running: false, managed: false, health: null };
  groupSignal(child, 'SIGTERM');
  await waitForBridgeExit(child, 3000);
  if (isChildRunning(child)) groupSignal(child, 'SIGKILL');
  localSpeechBaseUrl = '';
  localSpeechToken = '';
  localSpeechProfileId = '';
  return { running: false, managed: false, health: null };
}

function stopManagedLocalVoices() {
  if (isChildRunning(localVoicesProcess)) groupSignal(localVoicesProcess, 'SIGTERM');
}

ipcMain.handle('local-voices-status', function(event) {
  if (!isTrustedEvaRenderer(event)) throw new Error('Unauthorized renderer.');
  return getLocalVoicesStatus();
});
ipcMain.handle('local-voices-start', function(event, pythonPath, voiceId) {
  if (!isTrustedEvaRenderer(event)) throw new Error('Unauthorized renderer.');
  return startLocalVoices(pythonPath, voiceId);
});
ipcMain.handle('local-voices-stop', function(event) {
  if (!isTrustedEvaRenderer(event)) throw new Error('Unauthorized renderer.');
  return stopLocalVoices();
});
ipcMain.handle('local-voices-list', function(event) {
  if (!isTrustedEvaRenderer(event)) throw new Error('Unauthorized renderer.');
  return getLocalVoiceProfiles();
});
ipcMain.handle('local-voices-import', function(event) {
  if (!isTrustedEvaRenderer(event)) throw new Error('Unauthorized renderer.');
  return importLocalVoiceProfile();
});
ipcMain.handle('local-speech-synthesize', async function(event, request) {
  if (!isTrustedEvaRenderer(event)) throw new Error('Unauthorized renderer.');
  const result = await synthesizeLocalSpeech(request, false);
  return new Uint8Array(result.audio);
});
ipcMain.handle('local-speech-acknowledgement', async function(event, request) {
  if (!isTrustedEvaRenderer(event)) throw new Error('Unauthorized renderer.');
  const result = await queueAcknowledgementSynthesis(request, true);
  return new Uint8Array(result.audio);
});
ipcMain.handle('local-speech-warm-acknowledgements', async function(event, request) {
  if (!isTrustedEvaRenderer(event)) throw new Error('Unauthorized renderer.');
  const payload = request || {};
  const phrases = Array.isArray(payload.phrases) ? payload.phrases : [];
  const uniquePhrases = Array.from(new Set(phrases.map(function(phrase) {
    return typeof phrase === 'string' ? phrase.trim() : '';
  }).filter(function(phrase) { return phrase && phrase.length <= 160; }))).slice(0, 32);
  let generated = 0;
  let reused = 0;
  for (const phrase of uniquePhrases) {
    const result = await queueAcknowledgementSynthesis({
      input: phrase,
      language: payload.language,
      languageMode: payload.languageMode,
      profileId: payload.profileId
    }, false);
    if (result.cached) reused += 1;
    else generated += 1;
  }
  return { generated: generated, reused: reused };
});
ipcMain.handle('local-speech-transcribe', async function(event, audio, contentType, language, liveTranslation) {
  if (!isTrustedEvaRenderer(event)) throw new Error('Unauthorized renderer.');
  const bytes = Buffer.from(audio || []);
  if (!bytes.length || bytes.length > 16 * 1024 * 1024) throw new Error('Invalid audio input.');
  const requestedLanguage = normalizeLocalSpeechLanguage(language, true);
  const headers = { 'X-Eva-Speech-Language': requestedLanguage };
  if (liveTranslation) headers['X-Eva-Live-Translation'] = '1';
  return requestLocalSpeech('/v1/audio/transcriptions', 'POST', bytes, String(contentType || ''), 180000, headers);
});
ipcMain.handle('local-speech-warm-translation', async function(event, multilingual) {
  if (!isTrustedEvaRenderer(event)) throw new Error('Unauthorized renderer.');
  const headers = multilingual ? { 'X-Eva-Live-Translation': '1' } : {};
  return requestLocalSpeech('/v1/audio/transcriptions/warm', 'POST', null, 'application/json', 180000, headers);
});
ipcMain.handle('auth-load', function(event) {
  return isTrustedEvaRenderer(event) ? loadEncryptedAuth() : {};
});
ipcMain.handle('auth-save', function(event, values) {
  return isTrustedEvaRenderer(event) && saveEncryptedAuth(values);
});
ipcMain.on('bridge-capability-token', function(event) {
  event.returnValue = isTrustedEvaRenderer(event) ? bridgeCapabilityToken : '';
});
ipcMain.handle('terminal-list', function(event) {
  return requireTerminalBroker(event).list();
});
ipcMain.handle('terminal-create', async function(event, request) {
  const broker = requireTerminalBroker(event);
  const rootId = request && request.rootId;
  await ensureTerminalRoot(rootId);
  return broker.create(request);
});
ipcMain.handle('terminal-replay', function(event, id) {
  return requireTerminalBroker(event).replay(id);
});
ipcMain.handle('terminal-write', function(event, id, data) {
  return requireTerminalBroker(event).write(id, data);
});
ipcMain.handle('terminal-resize', function(event, id, cols, rows) {
  return requireTerminalBroker(event).resize(id, cols, rows);
});
ipcMain.handle('terminal-close', function(event, id) {
  return requireTerminalBroker(event).close(id);
});
ipcMain.handle('terminal-close-root', function(event, rootId) {
  if (!validWorkspaceId(rootId)) throw new Error('Invalid workspace checkout ID.');
  return requireTerminalBroker(event).terminateByRoot(rootId).then(function(closed) { return { closed: closed }; });
});
ipcMain.handle('workspace-list-projects', workspaceListProjects);
ipcMain.handle('workspace-select-project', workspaceSelectProject);
ipcMain.handle('workspace-import-github', workspaceImportGitHub);
ipcMain.handle('workspace-set-mcp-server', workspaceSetMcpServer);
ipcMain.handle('workspace-create-run', workspaceCreateRun);
ipcMain.handle('workspace-list-runs', workspaceListRuns);
ipcMain.handle('workspace-list-project-files', workspaceListProjectFiles);
ipcMain.handle('workspace-open-project-file', workspaceOpenProjectFile);
ipcMain.handle('workspace-checkout-status', workspaceCheckoutStatus);
ipcMain.handle('workspace-list-assets', workspaceListAssets);
ipcMain.handle('workspace-open-asset', workspaceOpenAsset);
ipcMain.handle('workspace-run-action', workspaceRunAction);

function createWindow(acpBaseUrl) {
  const appRoot = getAppRoot();
  const terminalAssets = getTerminalAssetUrls();

  // Grant microphone access for Web Speech API (webkitSpeechRecognition).
  // Only allow media permissions for local file:// pages.
  session.defaultSession.setPermissionRequestHandler(function(webContents, permission, callback) {
    if (permission === 'media' && webContents.getURL().startsWith('file://')) {
      callback(true);
      return;
    }
    callback(false);
  });
  session.defaultSession.setPermissionCheckHandler(function(webContents, permission) {
    if (permission === 'media' && webContents && webContents.getURL().startsWith('file://')) {
      return true;
    }
    return false;
  });

  mainWindow = new BrowserWindow({
    width: 1280,
    height: 900,
    show: false,
    frame: false,
    transparent: true,
    backgroundColor: '#00000000',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
      allowRunningInsecureContent: false,
      additionalArguments: [
        '--eva-acp-base-url=' + acpBaseUrl,
        '--eva-version=' + app.getVersion(),
        '--eva-workspace-terminal-v1=' + (terminalBroker ? '1' : '0'),
        '--eva-xterm-url=' + (terminalAssets.xterm || ''),
        '--eva-xterm-css-url=' + (terminalAssets.css || ''),
        '--eva-xterm-fit-url=' + (terminalAssets.fit || ''),
        '--eva-xterm-search-url=' + (terminalAssets.search || ''),
        '--eva-xterm-web-links-url=' + (terminalAssets.webLinks || '')
      ]
    }
  });

  // Window control IPC
  ipcMain.on('win-minimize', function() { mainWindow.minimize(); });
  ipcMain.on('win-maximize', function() {
    if (mainWindow.isMaximized()) { mainWindow.unmaximize(); }
    else { mainWindow.maximize(); }
  });
  ipcMain.on('win-close', function() { mainWindow.close(); });

  mainWindow.once('ready-to-show', function() {
    mainWindow.show();
  });
  mainWindow.on('closed', function() {
    mainWindow = null;
    stopBridge();
  });

  mainWindow.webContents.on('context-menu', function(event, params) {
    const template = buildContextMenuTemplate(params, {
      openExternal: function(url) { shell.openExternal(url); },
      copyText: function(text) { clipboard.writeText(text); },
      copyImage: function(x, y) {
        if (mainWindow && !mainWindow.isDestroyed()) mainWindow.webContents.copyImageAt(x, y);
      }
    });

    if (template.length) Menu.buildFromTemplate(template).popup({ window: mainWindow });
  });

  // Open external links (http/https) in the system browser instead of
  // navigating the Electron window away from Eva's UI.
  mainWindow.webContents.on('will-navigate', function(event, url) {
    if (url.startsWith('http://') || url.startsWith('https://')) {
      event.preventDefault();
      if (!url.startsWith('http://127.0.0.1') && !url.startsWith('http://localhost')) shell.openExternal(url);
    }
  });
  mainWindow.webContents.setWindowOpenHandler(function(details) {
    if (details.url.startsWith('http://') || details.url.startsWith('https://')) {
      shell.openExternal(details.url);
    }
    return { action: 'deny' };
  });

  mainWindow.loadFile(path.join(appRoot, 'index.html'));
}

async function boot() {
  bridgeCapabilityToken = crypto.randomBytes(32).toString('hex');
  workspaceCapabilityToken = crypto.randomBytes(32).toString('hex');
  initializeTerminalBroker();
  for (let attempt = 0; attempt <= BRIDGE_PORT_RETRY_LIMIT; attempt += 1) {
    const port = await getFreeLocalPort();
    const acpBaseUrl = 'http://127.0.0.1:' + port;
    const child = startBridge(port, bridgeCapabilityToken, workspaceCapabilityToken);
    try {
      await waitForBridge(acpBaseUrl, child, BRIDGE_READY_TIMEOUT_MS);
      readyBridgeProcess = child;
      bridgeBaseUrl = acpBaseUrl;
      if (workspaceTerminalEnabled()) {
        await ensureEvaReadyWorkspace();
        await dispatchPendingWorkspaceRuns();
      }
      child.evaAwaitingReady = false;
      if (typeof child.evaClearStderrBuffer === 'function') child.evaClearStderrBuffer();
      createWindow(acpBaseUrl);
      return;
    } catch (err) {
      if (err && err.code === 'EADDRINUSE') {
        if (attempt < BRIDGE_PORT_RETRY_LIMIT) {
          console.error('ACP bridge port ' + port + ' was already in use. Retrying with a new local port.');
          const priorChild = child;
          await waitForBridgeExit(priorChild, 1000);
          if (priorChild.exitCode === null && priorChild.signalCode === null) {
            groupSignal(priorChild, 'SIGKILL');
          }
          continue;
        }
        throw createPortRetryError();
      }
      throw err;
    }
  }
}

const hasSingleInstanceLock = app.requestSingleInstanceLock();

if (!hasSingleInstanceLock) {
  app.quit();
} else {
  app.on('second-instance', function() {
    if (!mainWindow) return;
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.show();
    mainWindow.focus();
  });

  app.whenReady().then(function() {
    boot().catch(function(err) {
      stopBridge();
      dialog.showErrorBox(getStartupErrorTitle(err), getStartupErrorMessage(err));
      app.quit();
    });
  });

  app.on('before-quit', function() {
    if (terminalBroker) terminalBroker.closeAll();
    stopBridge();
  });
  app.on('window-all-closed', function() {
    app.quit();
  });
}

process.on('SIGINT', function() {
  if (terminalBroker) terminalBroker.closeAll();
  stopBridge();
  app.quit();
});
process.on('SIGTERM', function() {
  if (terminalBroker) terminalBroker.closeAll();
  stopBridge();
  app.quit();
});

process.on('uncaughtException', function(err) {
  exitAfterFatalError('Uncaught exception in Electron main process:', err);
});

process.on('unhandledRejection', function(reason) {
  exitAfterFatalError('Unhandled promise rejection in Electron main process:', reason);
});
