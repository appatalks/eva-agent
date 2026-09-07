#!/usr/bin/env node
'use strict';

const assert = require('assert');
const fs = require('fs');

const source = fs.readFileSync('core/js/features/voice/view.js', 'utf8');
const optionsSource = fs.readFileSync('core/js/options.js', 'utf8');
const speakStart = optionsSource.indexOf('function speakText()');
const speakEnd = optionsSource.indexOf('\n}', speakStart);
const speakBody = optionsSource.slice(speakStart, speakEnd);
assert.match(speakBody, /_vvStopTTS\(\)/, 'new speech must stop prior TTS');
assert.match(source, /average > 40 && peak > 95/, 'barge-in must require speech-like energy');
assert.match(source, /level > 60/, 'barge-in must require voiced frequency bands');
assert.match(source, /_bargeEnergyFrames >= 5/, 'barge-in must respond after half a second of sustained speech');
assert.match(source, /function _vvSendCommand[\s\S]*?_vvStopTTS\(\)/, 'compact voice input must interrupt current TTS');
assert.match(source, /if \(_vv\._capture\) _vv\._capture\.discardOnStop = true;/, 'uninterrupted TTS capture must be discarded');
assert.match(source, /discardOnStop: _vv\.phase === 'speaking'/, 'captures started during TTS must be discarded by default');
assert.match(source, /if \(capture\.discardOnStop\)/, 'discarded capture must not be transcribed');
assert.match(source, /_vv\.phase === 'speaking'[\s\S]*?!_vvWakeWordMatch\(transcript\)/, 'direct transcript interruption must require the wake name');

console.log('voice interruption tests: PASS');