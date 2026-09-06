#!/usr/bin/env node
const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

const source = fs.readFileSync('core/js/features/automation/camera.js', 'utf8');
const renderer = fs.readFileSync('core/js/options.js', 'utf8');
const sandbox = { window: {} };
vm.runInNewContext(source, sandbox, { filename: 'core/js/features/automation/camera.js' });

const camera = sandbox.window.EvaCamera;
assert.ok(camera, 'camera controller must export EvaCamera');
['enable', 'disable', 'isEnabled', 'look', 'status'].forEach((name) => {
  assert.strictEqual(typeof camera[name], 'function', `EvaCamera.${name} must remain available`);
});
['/v1/camera/start', '/v1/camera/stop', '/v1/camera/status', '/v1/camera/frame', '/v1/vision/look'].forEach((endpoint) => {
  assert.ok(source.includes(endpoint), `${endpoint} contract must remain in the Camera controller`);
});
assert.match(renderer, /EvaRequestRouting\.isExplicitCameraRequest\(nativeRequest\)/);
assert.match(renderer, /if \(!nativeCameraRequest\) return/);

console.log('camera API tests: PASS');