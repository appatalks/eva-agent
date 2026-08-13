#!/usr/bin/env node
'use strict';

const assert = require('assert');
const fs = require('fs');

const source = fs.readFileSync('core/js/providers/aig.js', 'utf8');
const bridge = fs.readFileSync('tools/bridge/core.py', 'utf8');
assert.match(source, /var briefingRequest = \/\\b\(\?:morning\|daily\)\\s\+briefing\\b\/i/);
assert.match(source, /if \(briefingRequest\) \{\s+cogDecision = \{ active: false, reason: 'briefing-cache' \}/);
assert.match(source, /briefingStatus\.status === 'ready' \|\| briefingStatus\.status === 'preparing'/);
assert.match(bridge, /briefing_unavailable_sources\(_briefing_status\)/);
assert.match(bridge, /\[Morning Briefing Availability\]/);
assert.match(bridge, /Never call this a complete briefing/);
assert.match(bridge, /"gpt-5\.6-luna": "openai\/gpt-5\.6-luna"/);
assert.match(bridge, /not _briefing_request and _st\.cognition_enabled/);
console.log('briefing route tests: PASS');