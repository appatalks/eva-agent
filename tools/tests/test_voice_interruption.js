#!/usr/bin/env node
'use strict';

const assert = require('assert');
const fs = require('fs');

const source = fs.readFileSync('core/js/options.js', 'utf8');
const speakStart = source.indexOf('function speakText()');
const speakEnd = source.indexOf('\n}', speakStart);
const speakBody = source.slice(speakStart, speakEnd);
assert.match(speakBody, /_vvStopTTS\(\)/, 'new speech must stop prior TTS');
assert.match(source, /average > 38 && peak > 90/, 'barge-in must require speech-like energy');
assert.match(source, /_bargeEnergyFrames >= 8/, 'barge-in must require sustained energy');
assert.match(source, /function _vvSendCommand[\s\S]*?_vvStopTTS\(\)/, 'compact voice input must interrupt current TTS');

console.log('voice interruption tests: PASS');