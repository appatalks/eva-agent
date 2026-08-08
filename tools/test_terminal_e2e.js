#!/usr/bin/env node
const assert = require('assert');
const { spawn } = require('child_process');
const fs = require('fs');
const net = require('net');
const os = require('os');
const path = require('path');
const { chromium } = require('../standalone/node_modules/playwright-core');

const root = path.resolve(__dirname, '..');
const configuredEndpoint = process.env.EVA_ELECTRON_CDP || '';
const electronPath = process.env.EVA_ELECTRON_BINARY || path.join(root, 'standalone', 'dist', 'linux-unpacked', 'eva-standalone');
const artifactDirectory = process.env.EVA_E2E_ARTIFACTS || path.join(__dirname, '..', 'standalone', 'dist');

function reservePort() {
  return new Promise(function(resolve, reject) {
    const server = net.createServer();
    server.on('error', reject);
    server.listen(0, '127.0.0.1', function() {
      const address = server.address();
      server.close(function() { resolve(address.port); });
    });
  });
}

function waitForPage(endpoint) {
  const deadline = Date.now() + 90000;
  return new Promise(function(resolve, reject) {
    function connect() {
      chromium.connectOverCDP(endpoint).then(function(browser) {
        const pages = browser.contexts().flatMap(function(context) { return context.pages(); });
        const page = pages.find(function(candidate) { return candidate.url().endsWith('/index.html'); });
        if (page) resolve({ browser: browser, page: page });
        else browser.close().then(retry, retry);
      }, retry);
    }
    function retry() {
      if (Date.now() >= deadline) {
        reject(new Error('Timed out waiting for the packaged Eva renderer.'));
        return;
      }
      setTimeout(connect, 500);
    }
    connect();
  });
}

function stopProcess(child) {
  return new Promise(function(resolve) {
    if (!child || child.exitCode !== null) {
      resolve();
      return;
    }
    const timeout = setTimeout(function() {
      try { child.kill('SIGKILL'); } catch (_) {}
    }, 5000);
    child.once('exit', function() {
      clearTimeout(timeout);
      resolve();
    });
    child.kill('SIGTERM');
  });
}

async function terminalText(page) {
  return page.locator('.workspace-terminal-host').innerText();
}

async function openTerminal(page) {
  const panel = page.locator('#terminalPanel');
  if (await panel.getAttribute('aria-hidden') === 'true') {
    await page.locator('#evaTerminalBtn').click();
  }
  await page.locator('.workspace-terminal').waitFor({ state: 'visible' });
  await page.locator('.workspace-terminal-status').filter({ hasText: 'CONNECTED' }).waitFor();
}

async function verifyDisabledWorkspaceLaunch() {
  const configDirectory = fs.mkdtempSync(path.join(os.tmpdir(), 'eva-disabled-workspace-'));
  const port = await reservePort();
  const environment = Object.assign({}, process.env, { EVA_CONFIG_DIR: configDirectory });
  delete environment.EVA_WORKSPACE_TERMINAL_V1;
  const disabledChild = spawn(electronPath, ['--remote-debugging-port=' + port], {
    env: environment,
    stdio: ['ignore', 'ignore', 'ignore']
  });
  let disabledBrowser = null;
  try {
    const attached = await waitForPage('http://127.0.0.1:' + port);
    disabledBrowser = attached.browser;
    const result = await attached.page.evaluate(async function() {
      try {
        await window.evaStandalone.workspaceListProjects();
        return { enabled: window.evaStandalone.workspaceTerminalV1, rejected: false };
      } catch (error) {
        return {
          enabled: window.evaStandalone.workspaceTerminalV1,
          rejected: /disabled/i.test(String(error && error.message || error))
        };
      }
    });
    assert.deepStrictEqual(result, { enabled: false, rejected: true });
  } finally {
    if (disabledBrowser) await disabledBrowser.close();
    await stopProcess(disabledChild);
    assert.strictEqual(fs.existsSync(path.join(configDirectory, 'projects', 'eva-ready')), false,
      'Disabled workspace startup provisioned Eva Ready Workspace');
    fs.rmSync(configDirectory, { recursive: true, force: true });
  }
}

async function run() {
  let browser = null;
  let child = null;
  let temporaryConfig = '';
  try {
    let endpoint = configuredEndpoint;
    if (!endpoint) {
      await verifyDisabledWorkspaceLaunch();
      const port = await reservePort();
      temporaryConfig = fs.mkdtempSync(path.join(os.tmpdir(), 'eva-terminal-electron-'));
      child = spawn(electronPath, ['--eva-workspace-terminal-v1', '--remote-debugging-port=' + port], {
        env: Object.assign({}, process.env, { EVA_CONFIG_DIR: temporaryConfig }),
        stdio: ['ignore', 'ignore', 'ignore']
      });
      endpoint = 'http://127.0.0.1:' + port;
    }
    const attached = await waitForPage(endpoint);
    browser = attached.browser;
    const page = attached.page;

  const errors = [];
  page.on('pageerror', function(error) {
    if (/xterm|terminal/i.test(error.stack || error.message)) errors.push(error.message);
  });
  page.on('requestfailed', function(request) {
    if (/xterm|terminal/i.test(request.url())) {
      const failure = request.failure();
      errors.push(request.url() + ': ' + (failure ? failure.errorText : 'request failed'));
    }
  });

  await page.setViewportSize({ width: 1280, height: 900 });
  await openTerminal(page);
  await page.locator('.workspace-terminal-host').click();
  await page.keyboard.type("printf '\\033[31mEVA_E2E_ANSI\\033[0m\\n'");
  await page.keyboard.press('Enter');
  await page.waitForFunction(function() {
    return document.querySelector('.workspace-terminal-host').innerText.includes('EVA_E2E_ANSI');
  });

  const beforeReload = await terminalText(page);
  assert.match(beforeReload, /EVA_E2E_ANSI/);
  assert.match(beforeReload.replace(/\s/g, ''), /resources\/app/);
  await page.locator('.workspace-terminal-search').fill('EVA_E2E_ANSI');
  await page.locator('.workspace-terminal-tool', { hasText: 'Find' }).click();
  await page.screenshot({
    path: path.join(artifactDirectory, 'terminal-e2e-desktop.png'),
    fullPage: true
  });

  await page.reload({ waitUntil: 'domcontentloaded' });
  await openTerminal(page);
  await page.waitForFunction(function() {
    return document.querySelector('.workspace-terminal-host').innerText.includes('EVA_E2E_ANSI');
  });
  assert.match(await terminalText(page), /EVA_E2E_ANSI/);

  await page.locator('.workspace-terminal-tool', { hasText: 'Restart' }).click();
  await page.locator('.workspace-terminal-status').filter({ hasText: 'CONNECTED' }).waitFor();
  await page.locator('.workspace-terminal-host').click();
  await page.keyboard.type("printf 'EVA_E2E_RESTARTED\\n'");
  await page.keyboard.press('Enter');
  await page.waitForFunction(function() {
    return document.querySelector('.workspace-terminal-host').innerText.includes('EVA_E2E_RESTARTED');
  });

  await page.setViewportSize({ width: 480, height: 760 });
  await page.screenshot({
    path: path.join(artifactDirectory, 'terminal-e2e-narrow.png'),
    fullPage: true
  });
  const overflow = await page.evaluate(function() {
    const toolbar = document.querySelector('.workspace-terminal-toolbar');
    return toolbar.scrollWidth > toolbar.clientWidth || toolbar.scrollHeight > toolbar.clientHeight + 2;
  });
  assert.strictEqual(overflow, false, 'Terminal toolbar overflows in the narrow layout');

  await page.setViewportSize({ width: 1280, height: 900 });
  await page.locator('#terminalPanelClose').click();
  await page.evaluate(async function() {
    const id = 'sess_legacy_e2e';
    localStorage.setItem('eva_sessions', JSON.stringify([{
      id: id,
      title: 'Legacy session restore',
      created: Date.now() - 1000,
      updated: Date.now()
    }]));
    await idbSaveSession(id, {
      messages: JSON.stringify([
        { role: 'user', content: 'Restore this old question' },
        { role: 'assistant', content: 'Restored legacy answer' }
      ]),
      _masterOutput: 'Restored legacy answer'
    });
    renderSessionList();
  });
  await page.locator('#evaChatsBtn').click();
  await page.locator('.session-item[data-session-id="sess_legacy_e2e"]').click();
  await page.waitForFunction(function() {
    return document.querySelector('#txtOutput').innerText.includes('Restored legacy answer');
  });
  assert.match(await page.locator('#txtOutput').innerText(), /Restore this old question/);

  var skillsLoaded = page.waitForResponse(function(response) {
    return response.request().method() === 'GET' && /\/v1\/skills(?:\?|$)/.test(response.url());
  });
  await page.locator('#evaSkillsBtn').click();
  await page.locator('#skillsPanel').waitFor({ state: 'visible' });
  await skillsLoaded;
  assert.strictEqual(await page.locator('body').evaluate(function(body) { return body.classList.contains('skills-view-open'); }), true, 'Skills did not open as a main view');
  await page.evaluate(function() {
    _skillsState.skills = [
      { SkillId: 'skill-alpha', Name: 'Alpha Formatter', Description: 'Formats alpha reports', Status: 'active', Tools: 'file.download', Tags: 'format, report', Source: 'paste', UpdatedAt: '2026-08-08T10:00:00Z' },
      { SkillId: 'skill-beta', Name: 'Beta Research', Description: 'Researches beta topics', Status: 'disabled', Tools: 'browser', Tags: 'research, web', Source: 'github:owner/repository', UpdatedAt: '2026-08-07T10:00:00Z' },
      { SkillId: 'skill-draft', Name: 'Draft Analyzer', Description: 'A pending auto-learned capability', Status: 'draft', Tools: 'think', Tags: 'draft, learned', Source: 'file:draft-skill.md', UpdatedAt: '2026-08-06T10:00:00Z' }
    ];
    renderSkillsList();
  });
  assert.strictEqual(await page.locator('.skill-card').count(), 3, 'Skills library did not render cards');
  await page.locator('#skillsSearch').fill('research');
  assert.strictEqual(await page.locator('.skill-card').count(), 1, 'Skills search did not filter cards');
  assert.match(await page.locator('.skill-card').innerText(), /Beta Research/);
  await page.locator('#skillsSearch').fill('');
  await page.locator('#skillsStatusFilter').selectOption('active');
  assert.strictEqual(await page.locator('.skill-card').count(), 1, 'Skills status filter did not organize cards');
  assert.match(await page.locator('#skillsViewSummary').innerText(), /1 shown \| 1 active \| 3 total/);
  await page.locator('#skillsStatusFilter').selectOption('draft');
  assert.strictEqual(await page.locator('.skill-card').count(), 1, 'Draft skills could not be isolated');
  assert.match(await page.locator('.skill-card').innerText(), /Draft Analyzer/);
  await page.locator('#skillsStatusFilter').selectOption('all');
  await page.locator('#skillsSourceFilter').selectOption('github');
  assert.strictEqual(await page.locator('.skill-card').count(), 1, 'GitHub provenance did not match the source category');
  assert.match(await page.locator('.skill-card').innerText(), /github:owner\/repository/);
  await page.locator('.skill-card .background-inline-button', { hasText: 'Edit' }).click();
  assert.strictEqual(await page.locator('#skillDraftName').inputValue(), 'Beta Research', 'Edit did not populate the relocated editor');
  await page.locator('#skillDraftCancelButton').click();
  await page.locator('#skillsSourceFilter').selectOption('all');
  await page.locator('#skillsSort').selectOption('name');
  assert.match(await page.locator('.skill-card').first().innerText(), /Alpha Formatter/);
  await page.setViewportSize({ width: 480, height: 760 });
  assert.strictEqual(await page.locator('#skillsSourceFilter').isVisible(), true, 'Source filter is unavailable on mobile');
  assert.strictEqual(await page.locator('#skillsSort').isVisible(), true, 'Sort control is unavailable on mobile');
  var skillsToolbarOverflow = await page.locator('.skills-view-toolbar').evaluate(function(toolbar) {
    return toolbar.scrollWidth > toolbar.clientWidth;
  });
  assert.strictEqual(skillsToolbarOverflow, false, 'Skills toolbar overflows on mobile');
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.locator('#evaAssetsBtn').click();
  await page.locator('#assetsView').waitFor({ state: 'visible' });
  assert.strictEqual(await page.locator('body').evaluate(function(body) { return body.classList.contains('skills-view-open'); }), false, 'Skills remained layered under Assets');

    assert.deepStrictEqual(errors, [], 'Renderer errors: ' + errors.join('\n'));
    console.log('terminal Electron E2E: PASS');
  } finally {
    if (browser) await browser.close();
    await stopProcess(child);
    if (temporaryConfig) fs.rmSync(temporaryConfig, { recursive: true, force: true });
  }
}

run().catch(function(error) {
  console.error(error && error.stack ? error.stack : error);
  process.exit(1);
});
