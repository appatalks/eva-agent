#!/usr/bin/env node
const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { TerminalBroker } = require('../../standalone/terminal-broker');

function createFakePty() {
  const calls = [];
  const processes = [];
  return {
    calls: calls,
    processes: processes,
    spawn: function(shell, args, options) {
      const listeners = {};
      const process = {
        pid: 4242,
        write: function(data) { calls.push(['write', data]); },
        resize: function(cols, rows) { calls.push(['resize', cols, rows]); },
        kill: function() { calls.push(['kill']); },
        onData: function(listener) { listeners.data = listener; return { dispose: function() {} }; },
        onExit: function(listener) { listeners.exit = listener; return { dispose: function() {} }; },
        emitData: function(data) { listeners.data(data); },
        emitExit: function(exitCode, signal) { listeners.exit({ exitCode: exitCode, signal: signal }); }
      };
      calls.push(['spawn', shell, args, options]);
      processes.push(process);
      return process;
    }
  };
}

const fakePty = createFakePty();
const approvedRoot = path.resolve(__dirname, '..', '..');
const events = [];
const groupSignals = [];
let processListings = [];
function setProcessListings(listings) {
  processListings = listings.slice();
}
const broker = new TerminalBroker({
  pty: fakePty,
  platform: 'linux',
  shell: '/bin/sh',
  environment: {
    HOME: approvedRoot,
    EVA_BRIDGE_TOKEN: 'must-not-leak',
    EVA_LOCAL_SPEECH_TOKEN: 'must-not-leak'
  },
  idFactory: function() { return 'term-test'; },
  maxScrollbackBytes: 12,
  signalProcess: function(pid, signal) { groupSignals.push([pid, signal]); },
  spawnSyncProcess: function() { return { status: 0, stdout: processListings.shift() || '' }; },
  terminationGraceMs: 100
});

broker.on('data', function(event) { events.push(['data', event]); });
broker.on('exit', function(event) { events.push(['exit', event]); });
broker.registerRoot('project-test', approvedRoot);

setProcessListings(['4242 1 4242 4242\n']);

assert.throws(function() {
  broker.create({ rootId: 'missing', cols: 80, rows: 24 });
}, /approved root/i);
assert.throws(function() {
  broker.create({ rootId: 'project-test', cwd: '/tmp', cols: 80, rows: 24 });
}, /unsupported field/i);

const created = broker.create({ rootId: 'project-test', cols: 80, rows: 24 });
assert.deepStrictEqual(created, {
  id: 'term-test',
  rootId: 'project-test',
  cols: 80,
  rows: 24,
  sequence: 0,
  exited: false
});
assert.strictEqual(fakePty.calls[0][0], 'spawn');
assert.strictEqual(fakePty.calls[0][3].cwd, approvedRoot);
assert.strictEqual(fakePty.calls[0][3].env.EVA_BRIDGE_TOKEN, undefined);
assert.strictEqual(fakePty.calls[0][3].env.EVA_LOCAL_SPEECH_TOKEN, undefined);
assert.strictEqual(fakePty.calls[0][3].env.TERM, 'xterm-256color');

fakePty.processes[0].emitData('first\n');
fakePty.processes[0].emitData('second\n');
fakePty.processes[0].emitData('third\n');
assert.strictEqual(events[2][1].sequence, 3);
assert.strictEqual(Buffer.byteLength(broker.replay('term-test').data), 12);
assert.strictEqual(broker.replay('term-test').sequence, 3);

broker.write('term-test', 'pwd\r');
broker.resize('term-test', 100, 40);
assert.deepStrictEqual(fakePty.calls.slice(-2), [
  ['write', 'pwd\r'],
  ['resize', 100, 40]
]);

fakePty.processes[0].emitExit(0, 0);
assert.strictEqual(broker.replay('term-test').exited, true);
assert.strictEqual(events[events.length - 1][0], 'exit');
assert.strictEqual(events[events.length - 1][1].exitCode, 0);

async function verifyTermination() {
  setProcessListings([
    '4242 1 4242 4242\n',
    '',
    ''
  ]);
  var closePromise = broker.close('term-test');
  fakePty.processes[0].emitExit(0, 0);
  await closePromise;
  assert.deepStrictEqual(broker.list(), []);

  broker.create({ rootId: 'project-test', cols: 80, rows: 24 });
  setProcessListings([
    '4242 1 4242 4242\n4243 4242 4242 4242\n4244 1 4242 4242\n',
    '4242 1 4242 4242\n4243 4242 4242 4242\n4244 1 4242 4242\n',
    ''
  ]);
  var terminatePromise = broker.terminateByRoot('project-test');
  assert.deepStrictEqual(groupSignals.slice(-3), [[4243, 'SIGTERM'], [4244, 'SIGTERM'], [-4242, 'SIGTERM']]);
  fakePty.processes[fakePty.processes.length - 1].emitExit(0, 0);
  await terminatePromise;
  assert.deepStrictEqual(broker.list(), []);

  setProcessListings([
    '4242 1 4242 4242\n4244 4242 4242 4242\n',
    '4244 1 4242 4242\n',
    '4244 1 4242 4242\n',
    '4244 1 4242 4242\n',
    ''
  ]);
  broker.create({ rootId: 'project-test', cols: 80, rows: 24 });
  var stubbornPromise = broker.terminateByRoot('project-test');
  fakePty.processes[fakePty.processes.length - 1].emitExit(0, 0);
  await stubbornPromise;
  assert.ok(groupSignals.some(function(signal) { return signal[0] === 4244 && signal[1] === 'SIGKILL'; }), 'stubborn descendant was not escalated to SIGKILL');
  assert.deepStrictEqual(broker.list(), []);

  setProcessListings([
    '4242 1 4242 4242\n4244 4242 4242 4242\n',
    '4244 1 4242 4242\n',
    '4244 1 4242 4242\n',
    '',
    ''
  ]);
  broker.create({ rootId: 'project-test', cols: 80, rows: 24 });
  fakePty.processes[fakePty.processes.length - 1].emitExit(0, 0);
  const exitedSignalStart = groupSignals.length;
  await broker.terminateByRoot('project-test');
  assert.ok(groupSignals.slice(exitedSignalStart).some(function(signal) {
    return signal[0] === 4244 && signal[1] === 'SIGTERM';
  }), 'exited PTY descendant was not terminated');
  assert.deepStrictEqual(broker.list(), []);

  setProcessListings([
    '4242 1 4242 4242\n4244 4242 4242 4242\n',
    '4244 1 4242 4242\n'
  ]);
  broker.create({ rootId: 'project-test', cols: 80, rows: 24 });
  fakePty.processes[fakePty.processes.length - 1].emitExit(0, 0);
  const signalCountBeforeShutdown = groupSignals.length;
  broker.closeAll();
  assert.ok(groupSignals.slice(signalCountBeforeShutdown).some(function(signal) { return signal[0] === 4244 && signal[1] === 'SIGKILL'; }), 'shutdown did not kill an orphaned session descendant');
  assert.deepStrictEqual(broker.list(), []);
  assert.strictEqual(broker.unregisterRoot('project-test'), true);

  const swapSandbox = fs.mkdtempSync(path.join(os.tmpdir(), 'eva-terminal-root-'));
  try {
    const directRoot = path.join(swapSandbox, 'direct-root');
    const outside = path.join(swapSandbox, 'outside');
    fs.mkdirSync(directRoot);
    fs.mkdirSync(outside);
    broker.registerRoot('swap-direct', directRoot);
    fs.rmdirSync(directRoot);
    fs.symlinkSync(outside, directRoot, 'dir');
    assert.throws(function() {
      broker.create({ rootId: 'swap-direct', cols: 80, rows: 24 });
    }, /symlink|changed/i);

    const parent = path.join(swapSandbox, 'managed-parent');
    const nestedRoot = path.join(parent, 'run');
    const outsideParent = path.join(swapSandbox, 'outside-parent');
    fs.mkdirSync(nestedRoot, { recursive: true });
    fs.mkdirSync(path.join(outsideParent, 'run'), { recursive: true });
    broker.registerRoot('swap-parent', nestedRoot);
    fs.rmSync(parent, { recursive: true });
    fs.symlinkSync(outsideParent, parent, 'dir');
    assert.throws(function() {
      broker.create({ rootId: 'swap-parent', cols: 80, rows: 24 });
    }, /symlink|changed/i);
  } finally {
    fs.rmSync(swapSandbox, { recursive: true, force: true });
  }
  console.log('terminal broker tests: PASS');
}

verifyTermination().catch(function(error) {
  console.error(error && error.stack ? error.stack : error);
  process.exit(1);
});