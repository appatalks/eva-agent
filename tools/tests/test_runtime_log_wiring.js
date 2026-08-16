#!/usr/bin/env node
'use strict';

const assert = require('assert');
const fs = require('fs');
const path = require('path');

const root = path.resolve(__dirname, '..', '..');
const main = fs.readFileSync(path.join(root, 'standalone/main.js'), 'utf8');
const pkg = JSON.parse(fs.readFileSync(path.join(root, 'standalone/package.json'), 'utf8'));
const audit = fs.readFileSync(path.join(root, 'tools/bridge/audit.py'), 'utf8');

assert(pkg.build.files.includes('runtime-logger.js'), 'runtime logger must ship in the AppImage');
assert(main.includes("require('./runtime-logger')"), 'Electron main must load the runtime logger');
assert(main.includes("app.getPath('userData'), 'eva-runtime.log'"), 'aggregate log must live under userData');
assert(main.includes('runtimeLogger.installProcessStreams()'), 'main stdout/stderr must be captured');
assert(main.includes('runtimeLogger.attachRenderer(mainWindow.webContents)'), 'renderer console must be captured');
assert(main.includes("EVA_RUNTIME_AUDIT_STDOUT: '1'"), 'Standalone must enable sanitized audit forwarding');
assert(main.includes("runtimeLogger.event('bridge', 'process_started'"), 'bridge lifecycle must be captured');
assert(main.includes("runtimeLogger.event('local-voices', 'process_started'"), 'local voice lifecycle must be captured');
assert(main.includes("app.on('will-quit', function() { runtimeLogger.close(); })"), 'logger must close on quit');
assert(/forceKillBridgeSync\(\);[\s\S]{0,100}runtimeLogger\.close\(\);[\s\S]{0,60}process\.exit\(1\)/.test(main),
	'fatal exits must flush and close the runtime logger');
assert(audit.includes('EVA_RUNTIME_AUDIT_STDOUT'), 'audit forwarding must be opt-in');
assert(audit.includes('json.dumps(record'), 'only the sanitized audit record may be forwarded');
assert(!main.includes("terminalBroker.on('data', function(payload) {\n    runtimeLogger"), 'PTY content must not enter aggregate logs');

console.log('runtime log wiring tests: PASS');