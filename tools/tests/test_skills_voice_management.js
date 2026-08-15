#!/usr/bin/env node
'use strict';

const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

const source = fs.readFileSync('core/js/features/skills/library.js', 'utf8');
const playlistUrl = 'https://www.youtube.com/watch?v=MHiK7ytWxAQ&list=PLJNK8rrU0NUHblvj8wc83xP2HJAqUc2-n';
let skills = [];
let createPayload = null;
let openedUrl = '';
let activePatchCount = 0;
let activationResponse = 'ENABLE';
let evariseCount = 0;
let createCount = 0;
let failSkillList = false;

async function backgroundBridgeRequest(path, options) {
  options = options || {};
  if (path === '/v1/skills/evarise') {
    evariseCount += 1;
    return {
      draft: {
        name: 'Play my YouTube playlist',
        description: 'Use when the user asks Eva to play their YouTube playlist.',
        instructions: 'Open the saved YouTube playlist.',
        tools: '',
        tags: 'youtube, playlist'
      }
    };
  }
  if (path === '/v1/skills' && options.method === 'POST') {
    createCount += 1;
    createPayload = JSON.parse(options.body);
    const skill = {
      SkillId: 'sk-playlist',
      Name: createPayload.name,
      Description: createPayload.description,
      Instructions: createPayload.instructions,
      Tools: createPayload.tools,
      Tags: createPayload.tags,
      Source: createPayload.source,
      Status: createPayload.status
    };
    skills = [skill];
    return { skill };
  }
  if (path === '/v1/skills' && options.method === 'GET') {
    if (failSkillList) throw new Error('Skill list unavailable');
    return { skills };
  }
  if (path.indexOf('/v1/skills/') === 0 && options.method === 'PATCH') {
    const updates = JSON.parse(options.body);
    const skill = skills.find(function(item) { return item.SkillId === path.slice('/v1/skills/'.length); });
    if (!skill) throw new Error('Skill not found');
    if (updates.status) skill.Status = updates.status;
    if (updates.status === 'active') activePatchCount += 1;
    return { skill };
  }
  throw new Error('Unexpected bridge request: ' + options.method + ' ' + path);
}

const window = {
  open(url) { openedUrl = url; },
};
const sandbox = {
  URL,
  console,
  window,
  backgroundBridgeRequest,
  evaTextPrompt() { return Promise.resolve(activationResponse); },
  document: {
    body: { classList: { contains() { return false; }, add() {}, remove() {} } },
    getElementById() { return null; },
  },
};

vm.runInNewContext(source, sandbox, { filename: 'core/js/features/skills/library.js' });

async function main() {
  const request = 'Create a new Skill to play my YouTube playlist when I ask for it ' + playlistUrl;
  const created = await window.EvaSkills.createFromRequest(request);
  assert.match(created.message, /Created draft skill/);
  assert.strictEqual(createPayload.status, 'draft');
  assert.strictEqual(createPayload.source, 'voice');
  assert.match(createPayload.tools, /eva_harness\.open_external_url/);
  assert.ok(createPayload.instructions.includes(playlistUrl));
  assert.match(createPayload.instructions, /\[\[EVA_HARNESS\]\]/);
  await assert.rejects(window.EvaSkills.setStatusByName('Play my YouTube playlist', 'active', ''), /Type ENABLE/);
  await window.EvaSkills.setStatusByName('Play my YouTube playlist', 'active', 'ENABLE');
  assert.strictEqual(activePatchCount, 1);
  const reused = await window.EvaSkills.createFromRequest(request);
  assert.strictEqual(reused.reused, true);
  assert.strictEqual(reused.status, 'active');
  assert.match(reused.message, /Reused existing active skill/);
  assert.strictEqual(evariseCount, 1);
  assert.strictEqual(createCount, 1);
  failSkillList = true;
  await assert.rejects(window.EvaSkills.createFromRequest(request), /Skill list unavailable/);
  assert.strictEqual(evariseCount, 1);
  assert.strictEqual(createCount, 1);
  failSkillList = false;
  activationResponse = '';
  await sandbox.toggleSkill('sk-playlist', 'active');
  assert.strictEqual(activePatchCount, 1);
  activationResponse = 'ENABLE';
  await sandbox.toggleSkill('sk-playlist', 'active');
  assert.strictEqual(activePatchCount, 2);

  skills.push({
    SkillId: 'sk-unrelated', Name: 'Unrelated active Skill', Description: 'An unrelated action', Status: 'active',
    Instructions: '[[EVA_HARNESS]]{"action":"open_external_url","url":"https://attacker.example/","skillName":"Unrelated active Skill"}[[/EVA_HARNESS]]'
  });
  const opened = await window.EvaSkills.runFromRequest('Play my YouTube playlist.');
  assert.match(opened, /Opened https:\/\/www\.youtube\.com/);
  assert.strictEqual(openedUrl, playlistUrl);

  await assert.rejects(
    window.EvaSkills.openExternalUrl('https://example.com/not-authorized', 'Play my YouTube playlist', 'Play my YouTube playlist.'),
    /not authorized by the named active skill/
  );
  await assert.rejects(
    window.EvaSkills.openExternalUrl(playlistUrl + '-prefix', 'Play my YouTube playlist', 'Play my YouTube playlist.'),
    /not authorized by the named active skill/
  );
  await assert.rejects(
    window.EvaSkills.openExternalUrl(playlistUrl, 'Play my YouTube playlist', 'What is my playlist?'),
    /explicit user request/
  );
  skills.push({
    SkillId: 'sk-provisional', Name: 'Play provisional radio station', Description: 'A provisional station', Status: 'provisional',
    Instructions: '[[EVA_HARNESS]]{"action":"open_external_url","url":"https://radio.example/","skillName":"Play provisional radio station"}[[/EVA_HARNESS]]'
  });
  openedUrl = '';
  await assert.rejects(window.EvaSkills.runFromRequest('Play provisional radio station.'), /No active skill matches/);
  assert.strictEqual(openedUrl, '');
  const directUrl = 'https://example.com/direct-resource';
  await window.EvaSkills.openExternalUrl(directUrl, '', 'Open ' + directUrl);
  assert.strictEqual(openedUrl, directUrl);
  console.log('voice Skill management tests: PASS');
}

main().catch(function(error) {
  console.error(error && error.stack || error);
  process.exitCode = 1;
});