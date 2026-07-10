'use strict';

const crypto = require('crypto');

const READY_PREFIX = 'EVA_BRIDGE_BOUND ';
const READY_CONTEXT = 'eva-bridge-bound-v1';
const MAX_STDOUT_LINE_BUFFER = 64 * 1024;

function proofMessage(nonce, pid, host, port) {
  return [READY_CONTEXT, String(nonce), String(pid), String(host), String(port)].join(':');
}

function computeBridgeBindProof(token, nonce, pid, host, port) {
  return crypto.createHmac('sha256', String(token))
    .update(proofMessage(nonce, pid, host, port), 'utf8')
    .digest('hex');
}

function secureHexEqual(left, right) {
  if (!/^[0-9a-f]{64}$/.test(String(left)) || !/^[0-9a-f]{64}$/.test(String(right))) {
    return false;
  }
  return crypto.timingSafeEqual(Buffer.from(left, 'hex'), Buffer.from(right, 'hex'));
}

function verifyBridgeBindProofLine(line, expected) {
  const text = String(line || '').replace(/\r$/, '');
  if (text.indexOf(READY_PREFIX) !== 0) return null;

  let proof;
  try {
    proof = JSON.parse(text.slice(READY_PREFIX.length));
  } catch (err) {
    throw new Error('ACP bridge emitted malformed bind proof JSON.');
  }

  if (!proof || proof.version !== 1 ||
      proof.pid !== expected.pid || proof.host !== expected.host ||
      proof.port !== expected.port) {
    throw new Error('ACP bridge bind proof identity did not match the spawned process.');
  }

  const digest = computeBridgeBindProof(
    expected.token, expected.nonce, expected.pid, expected.host, expected.port
  );
  if (!secureHexEqual(proof.proof, digest)) {
    throw new Error('ACP bridge bind proof authentication failed.');
  }

  return Object.freeze({
    version: 1,
    pid: proof.pid,
    host: proof.host,
    port: proof.port
  });
}

function createChildProofTracker(childProcess, expected) {
  let lineBuffer = '';

  function fail(err) {
    if (childProcess.evaBindProof || childProcess.evaBindProofError) return;
    childProcess.evaBindProofError = err;
    childProcess.emit('eva-bind-proof-error', err);
  }

  function processLine(line) {
    if (line.indexOf(READY_PREFIX) !== 0) return;
    try {
      const proof = verifyBridgeBindProofLine(line, expected);
      if (!proof || childProcess.evaBindProof) return;
      childProcess.evaBindProof = proof;
      childProcess.emit('eva-bind-proof', proof);
    } catch (err) {
      fail(err);
    }
  }

  return Object.freeze({
    push: function(chunk) {
      if (childProcess.evaBindProof || childProcess.evaBindProofError) return;
      lineBuffer += Buffer.isBuffer(chunk) ? chunk.toString('utf8') : String(chunk || '');
      if (lineBuffer.length > MAX_STDOUT_LINE_BUFFER && lineBuffer.indexOf('\n') < 0) {
        lineBuffer = '';
        fail(new Error('ACP bridge stdout line exceeded the readiness limit.'));
        return;
      }
      let newline;
      while ((newline = lineBuffer.indexOf('\n')) >= 0) {
        const line = lineBuffer.slice(0, newline);
        lineBuffer = lineBuffer.slice(newline + 1);
        processLine(line);
      }
    },
    clear: function() { lineBuffer = ''; }
  });
}

function waitForVerifiedBridge(options) {
  const childProcess = options.childProcess;
  const baseUrl = options.baseUrl;
  const requestHealth = options.requestHealth;
  const timeoutMs = options.timeoutMs;
  const pollIntervalMs = options.pollIntervalMs || 500;
  const startedAt = Date.now();

  return new Promise(function(resolve, reject) {
    let settled = false;
    let pollTimer = null;
    const deadlineTimer = setTimeout(function() {
      const stage = childProcess.evaBindProof ? 'health response' : 'authenticated bind proof';
      finish(reject, new Error('Timed out waiting for ACP bridge ' + stage + '.'));
    }, timeoutMs);

    function cleanup() {
      clearTimeout(deadlineTimer);
      if (pollTimer) clearTimeout(pollTimer);
      childProcess.off('exit', onExit);
      childProcess.off('error', onError);
      childProcess.off('eva-address-in-use', onAddressInUse);
      childProcess.off('eva-bind-proof', onProof);
      childProcess.off('eva-bind-proof-error', onProofError);
    }

    function finish(fn, value) {
      if (settled) return;
      settled = true;
      cleanup();
      fn(value);
    }

    function onAddressInUse(err) { finish(reject, err); }

    function onProofError(err) { finish(reject, err); }

    function onError(err) {
      childProcess.evaSpawnError = err;
      finish(reject, err);
    }

    function onExit(code, signal) {
      if (childProcess.evaAddressInUseError) {
        finish(reject, childProcess.evaAddressInUseError);
        return;
      }
      finish(reject, new Error(
        'ACP bridge exited before it was ready (exit code ' +
        (code === null ? 'none' : code) + ', signal ' +
        (signal === null ? 'none' : signal) + ').'
      ));
    }

    function pollHealth() {
      if (settled || !childProcess.evaBindProof) return;
      Promise.resolve().then(function() {
        return requestHealth(baseUrl);
      }).then(function(data) {
        if (!childProcess.evaBindProof) {
          finish(reject, new Error('ACP bridge lost its bind proof before health completed.'));
          return;
        }
        finish(resolve, data);
      }).catch(function(err) {
        if (settled) return;
        if (Date.now() - startedAt >= timeoutMs) {
          finish(reject, new Error('Timed out waiting for ACP bridge: ' + err.message));
          return;
        }
        pollTimer = setTimeout(pollHealth, pollIntervalMs);
      });
    }

    function onProof() { pollHealth(); }

    childProcess.on('exit', onExit);
    childProcess.on('error', onError);
    childProcess.on('eva-address-in-use', onAddressInUse);
    childProcess.on('eva-bind-proof', onProof);
    childProcess.on('eva-bind-proof-error', onProofError);

    if (childProcess.evaSpawnError) {
      finish(reject, childProcess.evaSpawnError);
    } else if (childProcess.evaAddressInUseError) {
      finish(reject, childProcess.evaAddressInUseError);
    } else if (childProcess.evaBindProofError) {
      finish(reject, childProcess.evaBindProofError);
    } else if (childProcess.evaBindProof) {
      pollHealth();
    }
  });
}

module.exports = Object.freeze({
  READY_PREFIX,
  computeBridgeBindProof,
  verifyBridgeBindProofLine,
  createChildProofTracker,
  waitForVerifiedBridge
});
