const { app, BrowserWindow, dialog, ipcMain, session, shell } = require('electron');
const crypto = require('crypto');
const fs = require('fs');
const http = require('http');
const https = require('https');
const net = require('net');
const path = require('path');
const { pathToFileURL } = require('url');
const { spawn } = require('child_process');
const securityPolicy = require('./security-policy');
const bridgeReadiness = require('./bridge-readiness');
const launchCapability = require('./launch-capability');

// Independent per-process authorities: HTTP clients know only bridgeToken;
// only Electron main and the exact bridge child know launchCapabilitySecret.
const bridgeToken = crypto.randomBytes(32).toString('base64url');
const launchCapabilitySecret = crypto.randomBytes(32).toString('base64url');
const rawEgressMode = String(process.env.EVA_EGRESS_MODE || '').trim().toLowerCase();
let egressMode = 'cloud';
let egressModeError = null;
try { egressMode = securityPolicy.normalizeEgressMode(rawEgressMode); }
catch (err) { egressModeError = err; }

let bridgeProcess = null;
let readyBridgeProcess = null;
let bridgeStopTimer = null;
let bridgeStoppingProcess = null;
let shuttingDown = false;
let stoppingBridge = false;
let quitAfterBridgeStops = false;

const BRIDGE_READY_TIMEOUT_MS = 60000;
const BRIDGE_PORT_RETRY_LIMIT = 2;
const ADDRESS_IN_USE_PATTERN = /Address already in use|EADDRINUSE/i;
const PROVIDER_RESPONSE_MAX_BYTES = 32 * 1024 * 1024;
const PROVIDER_REQUEST_MAX_BYTES = 16 * 1024 * 1024;

function canonicalProviderHostname(hostname) {
  const lower = String(hostname || '').toLowerCase();
  return lower.endsWith('.') ? lower.slice(0, -1) : lower;
}

function providerHostnameAllowed(hostname) {
  const host = canonicalProviderHostname(hostname);
  return host === 'api.openai.com' || host === 'models.github.ai' ||
    host === 'models.inference.ai.azure.com' ||
    host === 'generativelanguage.googleapis.com' ||
    host === 'vision.googleapis.com' || host === 'api.elevenlabs.io' ||
    /^polly\.[a-z0-9-]+\.amazonaws\.com$/.test(host);
}

function providerHostUrl(rawUrl) {
  try { return providerHostnameAllowed(new URL(String(rawUrl || '')).hostname); }
  catch (_) { return false; }
}

function providerUrlAllowed(rawUrl) {
  let parsed;
  try { parsed = new URL(String(rawUrl || '')); }
  catch (_) { return false; }
  if (parsed.protocol !== 'https:' || parsed.username || parsed.password ||
      parsed.port || parsed.hash) return false;
  const host = canonicalProviderHostname(parsed.hostname);
  if (host === 'api.openai.com') return parsed.pathname.startsWith('/v1/');
  if (host === 'models.github.ai') return parsed.pathname.startsWith('/inference/');
  if (host === 'models.inference.ai.azure.com') return parsed.pathname.startsWith('/');
  if (host === 'generativelanguage.googleapis.com') return parsed.pathname.startsWith('/v1');
  if (host === 'vision.googleapis.com') return parsed.pathname.startsWith('/v1/');
  if (host === 'api.elevenlabs.io') return parsed.pathname.startsWith('/v1/');
  return /^polly\.[a-z0-9-]+\.amazonaws\.com$/.test(host) &&
    parsed.pathname === '/v1/speech';
}

function bridgeControlRequest(baseUrl, route, payload, expectedStatus) {
  return new Promise(function(resolve, reject) {
    const body = Buffer.from(JSON.stringify(payload), 'utf8');
    const parsed = new URL(baseUrl.replace(/\/+$/, '') + route);
    const req = http.request({
      protocol: 'http:', hostname: parsed.hostname, port: parsed.port,
      path: parsed.pathname, method: 'POST', agent: false,
      headers: {
        'Authorization': 'Bearer ' + bridgeToken,
        'Content-Type': 'application/json', 'Content-Length': String(body.length),
        'Origin': 'file://'
      }
    }, function(response) {
      const chunks = [];
      let total = 0;
      response.on('error', reject);
      response.on('data', function(chunk) {
        total += chunk.length;
        if (total > 64 * 1024) {
          req.destroy(new Error('Bridge control response exceeded its limit.'));
          return;
        }
        chunks.push(chunk);
      });
      response.on('end', function() {
        if (response.statusCode !== expectedStatus) {
          reject(new Error('Bridge provider admission was denied.'));
          return;
        }
        try { resolve(JSON.parse(Buffer.concat(chunks).toString('utf8'))); }
        catch (_) { reject(new Error('Bridge control response was invalid.')); }
      });
    });
    req.setTimeout(10000, function() {
      req.destroy(new Error('Bridge control request timed out.'));
    });
    req.on('error', reject);
    req.end(body);
  });
}

function validateProviderRequest(raw) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw) ||
      Object.keys(raw).some(function(key) {
        return !['url', 'method', 'headers', 'body'].includes(key);
      }) || !providerUrlAllowed(raw.url)) {
    throw new Error('Provider request is invalid.');
  }
  const method = String(raw.method || 'GET').toUpperCase();
  if (method !== 'GET' && method !== 'POST') {
    throw new Error('Provider request method is unavailable.');
  }
  const headers = {};
  const allowedHeaders = new Set([
    'accept', 'authorization', 'content-type', 'x-goog-api-key'
  ]);
  if (!raw.headers || typeof raw.headers !== 'object' || Array.isArray(raw.headers) ||
      Object.keys(raw.headers).length > 16) {
    throw new Error('Provider request headers are invalid.');
  }
  Object.keys(raw.headers).forEach(function(name) {
    const lower = String(name).toLowerCase();
    const value = raw.headers[name];
    if (!allowedHeaders.has(lower) || typeof value !== 'string' ||
        /[\r\n\0]/.test(value) || value.length > 8192) {
      throw new Error('Provider request header is invalid.');
    }
    headers[lower] = value;
  });
  const body = raw.body == null ? '' : raw.body;
  if (typeof body !== 'string' || Buffer.byteLength(body, 'utf8') > PROVIDER_REQUEST_MAX_BYTES ||
      (method === 'GET' && body)) {
    throw new Error('Provider request body is invalid.');
  }
  const canonicalUrl = new URL(String(raw.url));
  canonicalUrl.hostname = canonicalProviderHostname(canonicalUrl.hostname);
  return {
    url: canonicalUrl.toString(), method: method, headers: headers, body: body
  };
}

function boundedProviderRequest(request) {
  return new Promise(function(resolve, reject) {
    const parsed = new URL(request.url);
    const body = Buffer.from(request.body, 'utf8');
    const headers = Object.assign({}, request.headers);
    if (body.length) headers['content-length'] = String(body.length);
    const req = https.request({
      protocol: 'https:', hostname: parsed.hostname, port: 443,
      path: parsed.pathname + parsed.search, method: request.method,
      headers: headers, agent: false, servername: parsed.hostname,
      rejectUnauthorized: true
    }, function(response) {
      response.on('error', reject);
      if (response.statusCode >= 300 && response.statusCode < 400) {
        response.resume();
        reject(new Error('Provider redirects are not allowed.'));
        return;
      }
      const rawLength = response.headers['content-length'];
      if (rawLength !== undefined && (!/^[0-9]+$/.test(String(rawLength)) ||
          Number(rawLength) > PROVIDER_RESPONSE_MAX_BYTES)) {
        req.destroy(new Error('Provider response exceeded its limit.'));
        return;
      }
      const chunks = [];
      let total = 0;
      response.on('data', function(chunk) {
        total += chunk.length;
        if (total > PROVIDER_RESPONSE_MAX_BYTES) {
          req.destroy(new Error('Provider response exceeded its limit.'));
          return;
        }
        chunks.push(chunk);
      });
      response.on('end', function() {
        resolve({
          status: response.statusCode, statusText: response.statusMessage || '',
          headers: {
            'content-type': String(response.headers['content-type'] || ''),
            'content-length': String(total)
          },
          bodyBase64: Buffer.concat(chunks).toString('base64')
        });
      });
    });
    req.setTimeout(180000, function() {
      req.destroy(new Error('Provider request timed out.'));
    });
    req.on('error', reject);
    if (body.length) req.write(body);
    req.end();
  });
}

async function brokerProviderRequest(acpBaseUrl, rawRequest) {
  if (egressMode !== 'cloud') throw new Error('Cloud providers are unavailable.');
  const request = validateProviderRequest(rawRequest);
  const admission = await bridgeControlRequest(
    acpBaseUrl, '/v1/provider/admit', {}, 201
  );
  if (!admission || typeof admission.lease !== 'string' ||
      !/^[0-9a-f]{64}$/.test(admission.lease)) {
    throw new Error('Bridge provider lease was invalid.');
  }
  try {
    return await boundedProviderRequest(request);
  } finally {
    try {
      await bridgeControlRequest(
        acpBaseUrl, '/v1/provider/release', { lease: admission.lease }, 200
      );
    } catch (_) {}
  }
}

function requestAllowedByEgress(rawUrl) {
  return securityPolicy.requestAllowedByEgress(rawUrl, egressMode);
}

function clearBridgeStopTimer() {
  if (bridgeStopTimer) {
    clearTimeout(bridgeStopTimer);
    bridgeStopTimer = null;
  }
}

function groupSignal(child, signal) {
  if (!child) return;
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
    return 'Eva Standalone needs python3 to start the bundled ACP bridge. Install Python 3.12 or newer and try again.';
  }
  return err && err.message ? err.message : String(err);
}

function logFatalError(label, err) {
  console.error('[Eva] fatal runtime error');
}

function exitAfterFatalError(label, err) {
  logFatalError(label, err);
  process.exitCode = 1;
  quitAfterBridgeStops = true;
  stopBridge();
  app.quit();
}

function getAppRoot() {
  if (app.isPackaged) {
    return path.join(process.resourcesPath, 'app');
  }
  return path.resolve(__dirname, '..');
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
          if (data.status === 'ok' || (
              data.status === 'degraded' &&
              data.repair_required === true &&
              data.selected_mode === 'unknown' &&
              data.local_mode_state === 'invalid'
          )) {
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
  return bridgeReadiness.waitForVerifiedBridge({
    baseUrl: baseUrl,
    childProcess: childProcess,
    requestHealth: requestBridgeHealth,
    timeoutMs: timeoutMs,
    pollIntervalMs: 500
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

function trustedSystemPath() {
  return process.platform === 'darwin'
    ? '/opt/homebrew/bin:/usr/local/bin:/usr/local/sbin:/usr/bin:/usr/sbin:/bin:/sbin'
    : '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin';
}

function resolveTrustedExecutable(command, searchPath, label, required) {
  const text = String(command || '').trim();
  if (!text || text.indexOf('\0') !== -1) {
    if (required) throw new Error(label + ' is required');
    return '';
  }
  let candidate = '';
  if (path.isAbsolute(text)) {
    candidate = text;
  } else if (!text.includes('/') && !text.includes('\\')) {
    const entries = String(searchPath || '').split(path.delimiter).filter(Boolean);
    for (const entry of entries) {
      if (!path.isAbsolute(entry)) continue;
      const joined = path.join(entry, text);
      try {
        fs.accessSync(joined, fs.constants.X_OK);
        candidate = joined;
        break;
      } catch (_) {}
    }
  }
  if (!candidate) {
    if (required) throw new Error(label + ' was not found');
    return '';
  }
  let resolved;
  try {
    resolved = fs.realpathSync(candidate);
    const info = fs.statSync(resolved);
    fs.accessSync(resolved, fs.constants.X_OK);
    if (!info.isFile() || (info.mode & 0o022) !== 0) {
      throw new Error(label + ' is writable by another account');
    }
    const currentUid = typeof process.getuid === 'function' ? process.getuid() : null;
    if (currentUid !== null && info.uid !== 0 && info.uid !== currentUid) {
      throw new Error(label + ' has an untrusted owner');
    }
    let parent = path.dirname(resolved);
    while (parent && parent !== path.dirname(parent)) {
      const parentInfo = fs.statSync(parent);
      if ((parentInfo.mode & 0o022) !== 0 ||
          (currentUid !== null && parentInfo.uid !== 0 && parentInfo.uid !== currentUid)) {
        throw new Error(label + ' has an untrusted parent directory');
      }
      parent = path.dirname(parent);
    }
  } catch (err) {
    if (required) throw err;
    return '';
  }
  return resolved;
}

function createPrivateBridgeRuntimeDirectory() {
  const parent = path.resolve(app.getPath('userData'));
  fs.mkdirSync(parent, { recursive: true, mode: 0o700 });
  if (fs.realpathSync(parent) !== parent) {
    throw new Error('Electron userData path must not contain symbolic links');
  }
  const flags = fs.constants.O_RDONLY | fs.constants.O_DIRECTORY |
    fs.constants.O_NOFOLLOW;
  const parentFd = fs.openSync(parent, flags);
  try {
    const info = fs.fstatSync(parentFd);
    const uid = typeof process.getuid === 'function' ? process.getuid() : null;
    if (!info.isDirectory() || (uid !== null && info.uid !== uid)) {
      throw new Error('Electron userData path is not owner-controlled');
    }
    fs.fchmodSync(parentFd, 0o700);
    const descriptorRoot = process.platform === 'linux'
      ? '/proc/self/fd/' + parentFd
      : process.platform === 'darwin' ? '/dev/fd/' + parentFd : '';
    if (!descriptorRoot) {
      throw new Error('Descriptor-safe bridge runtime is unavailable');
    }
    const viaDescriptor = fs.mkdtempSync(
      path.join(descriptorRoot, 'bridge-runtime-')
    );
    const runtime = fs.realpathSync(viaDescriptor);
    const runtimeFd = fs.openSync(viaDescriptor, flags);
    try {
      const runtimeInfo = fs.fstatSync(runtimeFd);
      if (!runtimeInfo.isDirectory() || (uid !== null && runtimeInfo.uid !== uid)) {
        throw new Error('Bridge runtime directory is not owner-controlled');
      }
      fs.fchmodSync(runtimeFd, 0o700);
    } finally {
      fs.closeSync(runtimeFd);
    }
    return runtime;
  } finally {
    fs.closeSync(parentFd);
  }
}

function bridgeChildEnvironment(port, readyNonce, signalPath) {
  const allowed = [
    'HOME', 'USER', 'LOGNAME', 'LANG', 'LANGUAGE', 'LC_ALL', 'LC_CTYPE', 'TZ',
    'DISPLAY', 'WAYLAND_DISPLAY', 'XAUTHORITY', 'XDG_CONFIG_HOME',
    'XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS',
    'EVA_ADX_PROJECTION', 'EVA_ALLOWED_ORIGINS', 'EVA_CAMERA_DEVICE',
    'EVA_KUSTO_LOCKED', 'EVA_LEGACY_SKILL_AUTO_LEARN',
    'EVA_MEMORY_ANALYTICS', 'EVA_MEMORY_BACKEND', 'EVA_MEMORY_CONSOLIDATION',
    'EVA_MEMORY_DB', 'EVA_MEMORY_READ_MODE', 'EVA_MEMORY_RECALL_MODE',
    'EVA_MEMORY_SEMANTIC_MODE', 'EVA_MEMORY_SEMANTIC_QUERY_CONSENT',
    'EVA_PHASE2_MEMORY', 'EVA_PHASE3_LEARNING',
    'EVA_SIGNAL_RECIPIENT', 'EVA_SIGNAL_SENDER', 'EVA_TELEMETRY',
    'KUSTO_CLUSTER_URL', 'KUSTO_DATABASE', 'OPENAI_VISION_MODEL'
  ];
  const env = {};
  allowed.forEach(function(name) {
    const value = process.env[name];
    if (typeof value === 'string' && value.indexOf('\0') === -1 && value.length <= 16384) {
      env[name] = value;
    }
  });
  env.PATH = trustedSystemPath();
  if (signalPath) env.EVA_SIGNAL_CLI = signalPath;
  env.EVA_ACP_PORT = String(port);
  env.EVA_BRIDGE_TOKEN = bridgeToken;
  env.EVA_LAUNCH_CAPABILITY_SECRET = launchCapabilitySecret;
  env.EVA_BRIDGE_READY_NONCE = readyNonce;
  env.EVA_EGRESS_MODE = egressMode;
  env.KUSTO_DATABASE_LOCKED = '1';
  env.PYTHONUNBUFFERED = '1';
  return env;
}

function startBridge(port) {
  const appRoot = getAppRoot();
  const bridgePath = path.join(appRoot, 'tools', 'acp_bridge.py');
  const readyNonce = crypto.randomBytes(32).toString('base64url');
  const ambientPath = process.env.PATH || trustedSystemPath();
  const pythonCmd = resolveTrustedExecutable(
    process.env.EVA_PYTHON || 'python3', trustedSystemPath(), 'Python 3', true
  );
  const copilotPath = egressMode === 'cloud'
    ? resolveTrustedExecutable(
        process.env.EVA_COPILOT_PATH || 'copilot', ambientPath,
        'GitHub Copilot CLI', false
      ) : '';
  const signalPath = resolveTrustedExecutable(
    process.env.EVA_SIGNAL_CLI || 'signal-cli', ambientPath, 'signal-cli', false
  );
  const env = bridgeChildEnvironment(port, readyNonce, signalPath);
  const bridgeRuntimeCwd = createPrivateBridgeRuntimeDirectory();
  const args = [
    bridgePath, '--bind', '127.0.0.1', '--port', String(port), '--cwd', appRoot
  ];
  if (copilotPath) args.push('--copilot-path', copilotPath);
  const child = spawn(pythonCmd, args, {
    cwd: bridgeRuntimeCwd,
    env: env,
    detached: true,
    stdio: ['ignore', 'pipe', 'pipe']
  });
  let stderrBuffer = '';
  const proofTracker = bridgeReadiness.createChildProofTracker(child, {
    token: bridgeToken,
    nonce: readyNonce,
    pid: child.pid,
    host: '127.0.0.1',
    port: port
  });

  bridgeProcess = child;
  child.evaAwaitingReady = true;
  child.evaClearStderrBuffer = function() {
    stderrBuffer = '';
    proofTracker.clear();
  };

  child.stdout.on('data', function(chunk) {
    proofTracker.push(chunk);
  });
  child.stderr.on('data', function(chunk) {
    if (!child.evaAwaitingReady) return;
    stderrBuffer = (stderrBuffer + chunk.toString()).slice(-1000);
    if (!child.evaAddressInUseError && ADDRESS_IN_USE_PATTERN.test(stderrBuffer)) {
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
    if (quitAfterBridgeStops && !bridgeProcess) {
      quitAfterBridgeStops = false;
      setImmediate(function() { app.quit(); });
    }
  });
  child.once('close', function() {
    try {
      fs.rmdirSync(bridgeRuntimeCwd);
    } catch (err) {
      if (!err || err.code !== 'ENOENT') {
        process.stderr.write('[eva-acp] private bridge runtime cleanup failed\n');
      }
    }
  });

  return child;
}

function forceKillBridgeSync() {
  shuttingDown = true;
  const child = bridgeProcess;
  if (!child || !child.pid) return;

  try {
    process.kill(-child.pid, 'SIGKILL');
    return;
  } catch (err) {
    try {
      child.kill('SIGKILL');
    } catch (_) {}
  }
}

function stopBridge() {
  shuttingDown = true;
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
  }, 30000);
}

function createWindow(acpBaseUrl) {
  const appRoot = getAppRoot();
  const trustedDocumentUrl = pathToFileURL(path.join(appRoot, 'index.html')).toString();

  // Enforce offline/local-network below renderer code. Cloud mode preserves
  // direct provider behavior; restricted modes can only reach permitted hosts.
  session.defaultSession.webRequest.onBeforeRequest(
    { urls: ['http://*/*', 'https://*/*', 'ws://*/*', 'wss://*/*'] },
    function(details, callback) {
      callback({
        cancel: providerHostUrl(details.url) ||
          !requestAllowedByEgress(details.url)
      });
    }
  );

  function trustedAudioPermission(webContents, permission, details) {
    const mediaTypes = details && Array.isArray(details.mediaTypes)
      ? details.mediaTypes
      : details && typeof details.mediaType === 'string'
        ? [details.mediaType] : [];
    return permission === 'media' && webContents === mainWindow.webContents &&
      webContents.getURL() === trustedDocumentUrl && mediaTypes.length > 0 &&
      mediaTypes.every(function(mediaType) { return mediaType === 'audio'; });
  }

  // Grant microphone-only access for Web Speech API. Chromium video capture is
  // always denied; webcam frames exist only behind the native bridge capability.
  session.defaultSession.setPermissionRequestHandler(function(webContents, permission, callback, details) {
    if (trustedAudioPermission(webContents, permission, details)) {
      callback(true);
      return;
    }
    callback(false);
  });
  session.defaultSession.setPermissionCheckHandler(function(webContents, permission, _origin, details) {
    return trustedAudioPermission(webContents, permission, details);
  });

  const mainWindow = new BrowserWindow({
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
        '--eva-egress-mode=' + egressMode
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
  ipcMain.removeHandler('eva-authorize-agent-launch');
  ipcMain.removeHandler('eva-authorize-camera-look');
  ipcMain.removeHandler('eva-provider-fetch');
  ipcMain.handle('eva-provider-fetch', async function(event, request) {
    if (event.sender !== mainWindow.webContents ||
        !event.sender.getURL().startsWith('file://')) {
      throw new Error('Invalid provider broker caller.');
    }
    return brokerProviderRequest(acpBaseUrl, request);
  });
  ipcMain.handle('eva-authorize-camera-look', async function(event, request) {
    if (event.sender !== mainWindow.webContents ||
        !event.sender.getURL().startsWith('file://') ||
        !request || typeof request !== 'object' || Array.isArray(request) ||
        Object.keys(request).length !== 2 ||
        typeof request.question !== 'string' ||
        !request.question.trim() || request.question.length > 1000 ||
        !Number.isInteger(request.device) || request.device < 0 ||
        request.device > 32 ||
        /[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/.test(request.question)) {
      return { authorized: false };
    }
    let cameraSpec;
    try {
      cameraSpec = launchCapability.buildSpec('camera', request);
    } catch (_) {
      return { authorized: false };
    }
    const choice = await dialog.showMessageBox(mainWindow, {
      type: 'warning', title: 'Authorize Eva camera capture',
      message: 'Allow one webcam frame for this request?',
      detail: launchCapability.displaySummary('camera', cameraSpec),
      buttons: ['Cancel', 'Allow one frame'], defaultId: 0, cancelId: 0,
      noLink: true
    });
    if (choice.response !== 1) return { authorized: false };
    return {
      authorized: true,
      capability: launchCapability.issue(
        launchCapabilitySecret, 'camera', cameraSpec
      ),
      specification: cameraSpec
    };
  });
  ipcMain.handle('eva-authorize-agent-launch', async function(event, request) {
    if (
      event.sender !== mainWindow.webContents
      || !request || typeof request !== 'object'
      || (request.agent !== 'browser' && request.agent !== 'desktop')
    ) {
      return { authorized: false, error: 'invalid launch authorization request' };
    }
    let spec;
    try {
      spec = launchCapability.buildSpec(request.agent, request.specification, {
        vision_model: process.env.OPENAI_VISION_MODEL || 'gpt-4o'
      });
    } catch (_) {
      return { authorized: false, error: 'invalid launch specification' };
    }
    const choice = await dialog.showMessageBox(mainWindow, {
      type: 'warning',
      title: 'Authorize Eva agent run',
      message: 'Start this bounded agent run?',
      detail: launchCapability.displaySummary(request.agent, spec),
      buttons: ['Cancel', 'Authorize run'],
      defaultId: 0,
      cancelId: 0,
      noLink: true
    });
    if (choice.response !== 1) return { authorized: false };
    return {
      authorized: true,
      capability: launchCapability.issue(
        launchCapabilitySecret, request.agent, spec
      ),
      specification: spec
    };
  });

  mainWindow.once('ready-to-show', function() {
    mainWindow.show();
  });
  mainWindow.on('closed', function() {
    stopBridge();
  });

  // Open external links (http/https) in the system browser instead of
  // navigating the Electron window away from Eva's UI.
  mainWindow.webContents.on('will-navigate', function(event, url) {
    if (url === trustedDocumentUrl) return;
    event.preventDefault();
    if ((url.startsWith('http://') || url.startsWith('https://')) && requestAllowedByEgress(url)) {
      shell.openExternal(url);
    }
  });
  mainWindow.webContents.on('will-redirect', function(event, url) {
    if (url !== trustedDocumentUrl) event.preventDefault();
  });
  mainWindow.webContents.setWindowOpenHandler(function(details) {
    if ((details.url.startsWith('http://') || details.url.startsWith('https://')) && requestAllowedByEgress(details.url)) {
      shell.openExternal(details.url);
    }
    return { action: 'deny' };
  });

  // Inject bridge auth token for /v1/* requests to the bridge origin only.
  // The token never enters renderer memory — Electron main injects it at the
  // network layer so preload/JS cannot read or leak it.
  var bridgeV1Filter = { urls: [acpBaseUrl.replace(/\/+$/, '') + '/v1/*'] };
  session.defaultSession.webRequest.onBeforeSendHeaders(bridgeV1Filter, function(details, callback) {
    if (details.webContentsId === mainWindow.webContents.id && (!details.initiator || details.initiator.indexOf('file://') === 0)) {
      details.requestHeaders['Authorization'] = 'Bearer ' + bridgeToken;
      details.requestHeaders['Origin'] = 'file://';
    }
    callback({ requestHeaders: details.requestHeaders });
  });

  mainWindow.loadFile(path.join(appRoot, 'index.html'));
}

async function boot() {
  if (egressModeError) throw egressModeError;
  for (let attempt = 0; attempt <= BRIDGE_PORT_RETRY_LIMIT; attempt += 1) {
    const port = await getFreeLocalPort();
    const acpBaseUrl = 'http://127.0.0.1:' + port;
    const child = startBridge(port);
    try {
      await waitForBridge(acpBaseUrl, child, BRIDGE_READY_TIMEOUT_MS);
      readyBridgeProcess = child;
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

app.whenReady().then(function() {
  boot().catch(function(err) {
    stopBridge();
    dialog.showErrorBox(getStartupErrorTitle(err), getStartupErrorMessage(err));
    app.quit();
  });
});

app.on('before-quit', function(event) {
  if (bridgeProcess) {
    event.preventDefault();
    quitAfterBridgeStops = true;
    stopBridge();
  }
});
app.on('window-all-closed', function() {
  app.quit();
});

process.on('SIGINT', function() {
  stopBridge();
  app.quit();
});
process.on('SIGTERM', function() {
  stopBridge();
  app.quit();
});

process.on('uncaughtException', function(err) {
  exitAfterFatalError('Uncaught exception in Electron main process:', err);
});

process.on('unhandledRejection', function(reason) {
  exitAfterFatalError('Unhandled promise rejection in Electron main process:', reason);
});
