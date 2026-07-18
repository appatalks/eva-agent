const { app, BrowserWindow, dialog, ipcMain, session, shell } = require('electron');
const http = require('http');
const net = require('net');
const path = require('path');
const fs = require('fs');
const { spawn } = require('child_process');

let bridgeProcess = null;
let readyBridgeProcess = null;
let bridgeStopTimer = null;
let bridgeStoppingProcess = null;
let localVoicesProcess = null;
let shuttingDown = false;
let stoppingBridge = false;

const BRIDGE_READY_TIMEOUT_MS = 60000;
const LOCAL_VOICES_READY_TIMEOUT_MS = 10000;
const BRIDGE_PORT_RETRY_LIMIT = 2;
const ADDRESS_IN_USE_PATTERN = /Address already in use|EADDRINUSE/i;

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
  return path.join(process.env.HOME || '', '.local', 'share', 'eva', 'local-voices', 'voices');
}

function getLocalVoiceProfiles() {
  const profiles = [{ id: 'eva', label: 'Eva (bundled)', bundled: true }];
  const directory = getLocalVoicesDirectory();
  try {
    fs.mkdirSync(directory, { recursive: true });
    fs.readdirSync(directory).filter(function(name) {
      return name.toLowerCase().endsWith('.wav');
    }).sort().forEach(function(name) {
      profiles.push({
        id: 'custom:' + name,
        label: path.basename(name, path.extname(name)),
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
  fs.mkdirSync(directory, { recursive: true });
  const base = path.basename(source, path.extname(source)).replace(/[^a-zA-Z0-9._-]+/g, '-').replace(/^-+|-+$/g, '') || 'voice';
  let name = base + '.wav';
  let index = 2;
  while (fs.existsSync(path.join(directory, name))) {
    name = base + '-' + index + '.wav';
    index += 1;
  }
  fs.copyFileSync(source, path.join(directory, name));
  return { canceled: false, selected: 'custom:' + name, profiles: getLocalVoiceProfiles() };
}

function resolveLocalVoiceReference(voiceId) {
  if (!voiceId || voiceId === 'eva') return path.join(getAppRoot(), 'core', 'audio', 'eva-voice.wav');
  if (!voiceId.startsWith('custom:')) throw new Error('Unknown Local Voices profile.');
  const name = voiceId.slice('custom:'.length);
  if (path.basename(name) !== name || !name.toLowerCase().endsWith('.wav')) throw new Error('Invalid Local Voices profile.');
  const reference = path.join(getLocalVoicesDirectory(), name);
  if (!fs.existsSync(reference)) throw new Error('The selected Local Voices profile is unavailable.');
  return reference;
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

function startBridge(port) {
  const appRoot = getAppRoot();
  const bridgePath = path.join(appRoot, 'tools', 'acp_bridge.py');
  const args = [bridgePath, '--bind', '127.0.0.1', '--port', String(port), '--cwd', appRoot];
  const env = Object.assign({}, process.env, {
    EVA_ACP_PORT: String(port),
    KUSTO_DATABASE_LOCKED: '1',
    PYTHONUNBUFFERED: '1'
  });

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

  const pythonCmd = process.env.EVA_PYTHON || 'python3';
  const child = spawn(pythonCmd, args, {
    cwd: appRoot,
    env: env,
    detached: true,
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

function parseLocalVoicesUrl(baseUrl) {
  const parsed = new URL(String(baseUrl || ''));
  const hostname = parsed.hostname.toLowerCase();
  if (parsed.protocol !== 'http:' || (hostname !== 'localhost' && hostname !== '127.0.0.1')) {
    throw new Error('Local Voices can only run on an http://localhost URL.');
  }
  const port = Number(parsed.port);
  if (!Number.isInteger(port) || port < 1024 || port > 65535) {
    throw new Error('Local Voices needs a localhost port from 1024 to 65535.');
  }
  return { baseUrl: parsed.origin, port: port };
}

function requestLocalVoicesHealth(baseUrl) {
  return new Promise(function(resolve, reject) {
    const req = http.get(baseUrl.replace(/\/+$/, '') + '/health', function(res) {
      let body = '';
      res.setEncoding('utf8');
      res.on('data', function(chunk) { body += chunk; });
      res.on('end', function() {
        if (res.statusCode !== 200) {
          reject(new Error('Local Voices health returned HTTP ' + res.statusCode));
          return;
        }
        try {
          const data = JSON.parse(body);
          if (data.ok === true && data.backend_available === true) resolve(data);
          else reject(new Error(data.backend_error || 'Local Voices backend is unavailable.'));
        } catch (err) {
          reject(err);
        }
      });
    });
    req.setTimeout(2000, function() {
      req.destroy(new Error('Local Voices health timed out.'));
    });
    req.on('error', reject);
  });
}

function waitForLocalVoices(baseUrl, child, timeoutMs) {
  const startedAt = Date.now();
  return new Promise(function(resolve, reject) {
    function poll() {
      if (!isChildRunning(child)) {
        reject(new Error('Local Voices stopped before it was ready (' + formatExitDetails(child.exitCode, child.signalCode) + ').'));
        return;
      }
      requestLocalVoicesHealth(baseUrl).then(resolve).catch(function(err) {
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

async function getLocalVoicesStatus(baseUrl) {
  const target = parseLocalVoicesUrl(baseUrl);
  try {
    const health = await requestLocalVoicesHealth(target.baseUrl);
    return { running: true, managed: isChildRunning(localVoicesProcess), health: health };
  } catch (_) {
    return { running: false, managed: isChildRunning(localVoicesProcess), health: null };
  }
}

async function startLocalVoices(baseUrl, pythonPath, voiceId) {
  const target = parseLocalVoicesUrl(baseUrl);
  const status = await getLocalVoicesStatus(target.baseUrl);
  if (status.running) return status;
  if (isChildRunning(localVoicesProcess)) {
    throw new Error('Local Voices is already starting on another localhost port.');
  }

  const appRoot = getAppRoot();
  const bridgePath = path.join(appRoot, 'tools', 'local_voices_bridge.py');
  const managedPython = path.join(process.env.HOME || '', '.local', 'share', 'eva', 'local-voices', '.venv', 'bin', 'python');
  const pythonCmd = String(pythonPath || process.env.LOCAL_VOICES_PYTHON || process.env.EVA_PYTHON || (fs.existsSync(managedPython) ? managedPython : 'python3')).trim() || 'python3';
  const reference = resolveLocalVoiceReference(voiceId);
  const child = spawn(pythonCmd, [bridgePath, '--host', '127.0.0.1', '--port', String(target.port), '--reference', reference], {
    cwd: appRoot,
    env: Object.assign({}, process.env, { PYTHONUNBUFFERED: '1' }),
    detached: true,
    stdio: ['ignore', 'pipe', 'pipe']
  });
  localVoicesProcess = child;

  child.stdout.on('data', function(chunk) {
    process.stdout.write('[eva-local-voices] ' + chunk.toString());
  });
  child.stderr.on('data', function(chunk) {
    process.stderr.write('[eva-local-voices] ' + chunk.toString());
  });
  child.on('exit', function() {
    if (localVoicesProcess === child) localVoicesProcess = null;
  });

  try {
    const health = await waitForLocalVoices(target.baseUrl, child, LOCAL_VOICES_READY_TIMEOUT_MS);
    return { running: true, managed: true, health: health };
  } catch (err) {
    groupSignal(child, 'SIGTERM');
    throw err;
  }
}

async function stopLocalVoices(baseUrl) {
  parseLocalVoicesUrl(baseUrl);
  const child = localVoicesProcess;
  if (!isChildRunning(child)) return { running: false, managed: false, health: null };
  groupSignal(child, 'SIGTERM');
  await waitForBridgeExit(child, 3000);
  if (isChildRunning(child)) groupSignal(child, 'SIGKILL');
  return { running: false, managed: false, health: null };
}

function stopManagedLocalVoices() {
  if (isChildRunning(localVoicesProcess)) groupSignal(localVoicesProcess, 'SIGTERM');
}

ipcMain.handle('local-voices-status', function(_event, baseUrl) {
  return getLocalVoicesStatus(baseUrl);
});
ipcMain.handle('local-voices-start', function(_event, baseUrl, pythonPath, voiceId) {
  return startLocalVoices(baseUrl, pythonPath, voiceId);
});
ipcMain.handle('local-voices-stop', function(_event, baseUrl) {
  return stopLocalVoices(baseUrl);
});
ipcMain.handle('local-voices-list', function() {
  return getLocalVoiceProfiles();
});
ipcMain.handle('local-voices-import', function() {
  return importLocalVoiceProfile();
});

function createWindow(acpBaseUrl) {
  const appRoot = getAppRoot();

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
        '--eva-version=' + app.getVersion()
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
    stopBridge();
  });

  // Open external links (http/https) in the system browser instead of
  // navigating the Electron window away from Eva's UI.
  mainWindow.webContents.on('will-navigate', function(event, url) {
    if (url.startsWith('http://') || url.startsWith('https://')) {
      // Block localhost /v1/files/ navigation — these are artifact downloads,
      // not page navigations. Without this, Electron replaces Eva's UI with
      // raw file content, making the app appear frozen.
      if (url.startsWith('http://127.0.0.1') || url.startsWith('http://localhost')) {
        if (url.indexOf('/v1/files/') !== -1) {
          event.preventDefault();
          return;
        }
        return;
      }
      event.preventDefault();
      shell.openExternal(url);
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

app.on('before-quit', stopBridge);
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
