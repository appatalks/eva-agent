#!/usr/bin/env node
const assert = require('assert');
const { execFileSync, spawn } = require('child_process');
const fs = require('fs');
const net = require('net');
const os = require('os');
const path = require('path');
const { chromium } = require('../../standalone/node_modules/playwright-core');

const root = path.resolve(__dirname, '..', '..');
const electronPath = process.env.EVA_ELECTRON_BINARY || path.join(root, 'standalone', 'dist', 'linux-unpacked', 'eva-standalone');

function git(directory, args) {
  return execFileSync('git', args, { cwd: directory, encoding: 'utf8' }).trim();
}

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
    function tryConnect() {
      chromium.connectOverCDP(endpoint).then(function(browser) {
        const pages = browser.contexts().flatMap(function(context) { return context.pages(); });
        const page = pages.find(function(candidate) { return candidate.url().endsWith('/index.html'); });
        if (page) {
          resolve({ browser: browser, page: page });
        } else {
          browser.close().then(retry, retry);
        }
      }, retry);
    }
    function retry() {
      if (Date.now() >= deadline) {
        reject(new Error('Timed out waiting for the packaged Eva renderer.'));
        return;
      }
      setTimeout(tryConnect, 500);
    }
    tryConnect();
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

function processExists(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return error && error.code !== 'ESRCH';
  }
}

function waitForProcessExit(pid) {
  const deadline = Date.now() + 10000;
  return new Promise(function(resolve, reject) {
    function check() {
      if (!processExists(pid)) {
        resolve();
        return;
      }
      if (Date.now() >= deadline) {
        reject(new Error('Discard retained background process ' + pid));
        return;
      }
      setTimeout(check, 100);
    }
    check();
  });
}

async function main() {
  const sandbox = fs.mkdtempSync(path.join(os.tmpdir(), 'eva-workspace-electron-'));
  const repository = path.join(sandbox, 'project');
  const configDirectory = path.join(sandbox, 'eva-config');
  const artifactDirectory = path.join(root, 'standalone', 'dist');
  let child = null;
  let browser = null;
  let stderr = '';
  try {
    fs.mkdirSync(repository);
    git(repository, ['init', '-b', 'main']);
    git(repository, ['config', 'user.name', 'Eva E2E']);
    git(repository, ['config', 'user.email', 'eva-e2e@example.invalid']);
    fs.writeFileSync(path.join(repository, 'README.md'), '# packaged workspace test\n');
    git(repository, ['add', 'README.md']);
    git(repository, ['commit', '-m', 'Initial commit']);
    execFileSync('python3', [path.join(root, 'tools', 'tests', 'test_workspace_electron_setup.py'), configDirectory, repository], { encoding: 'utf8' });

    const debuggingPort = await reservePort();
    child = spawn(electronPath, ['--eva-workspace-terminal-v1', '--remote-debugging-port=' + debuggingPort], {
      env: Object.assign({}, process.env, { EVA_CONFIG_DIR: configDirectory, EVA_WORKSPACE_AGENT_AUTODISPATCH: '0' }),
      stdio: ['ignore', 'ignore', 'pipe']
    });
    child.stderr.on('data', function(chunk) { stderr = (stderr + chunk.toString()).slice(-4000); });
    const attached = await waitForPage('http://127.0.0.1:' + debuggingPort);
    browser = attached.browser;
    const page = attached.page;
    await page.setViewportSize({ width: 1280, height: 900 });

    await page.locator('#evaWorkspacesBtn').click();
    await page.locator('#agentsView').waitFor({ state: 'visible' });
    assert.strictEqual(await page.locator('#agentsViewTitle').innerText(), 'Workspace', 'Workspace tab did not open the agentic-session workspace');
    assert.strictEqual(await page.locator('#evaWorkspacesBtn').evaluate(function(button) { return button.classList.contains('active'); }), true, 'Workspace tab is not active for the agentic-session workspace');
    await page.evaluate(function() { window.EvaWorkspaces.openWorkbench(); });
    await page.locator('#workspaceWorkbench').waitFor({ state: 'visible' });
    await page.waitForFunction(function() {
      const workbenchHeader = document.querySelector('#workspaceWorkbench .workspace-workbench-header').getBoundingClientRect();
      const titleBar = document.querySelector('#evaTitleBar').getBoundingClientRect();
      return workbenchHeader.top >= titleBar.bottom;
    });
    await page.setViewportSize({ width: 480, height: 760 });
    const narrowOverflow = await page.evaluate(function() {
      const workbench = document.querySelector('#workspaceWorkbench');
      return workbench.scrollWidth > workbench.clientWidth || workbench.scrollHeight > workbench.clientHeight;
    });
    assert.strictEqual(narrowOverflow, false, 'Workspace monitor overflows in the narrow layout');
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.locator('#workspaceMonitorNewBtn').click();
    await page.locator('#workspacePanel').waitFor({ state: 'visible' });
    await page.waitForFunction(function() {
      const workspaceHeader = document.querySelector('#workspacePanel .session-panel-header').getBoundingClientRect();
      const titleBar = document.querySelector('#evaTitleBar').getBoundingClientRect();
      return workspaceHeader.top >= titleBar.bottom;
    });
    await page.locator('.workspace-project-item').filter({ hasText: 'project' }).waitFor();
    assert.strictEqual(await page.locator('#workspaceProjectSelect option').filter({ hasText: 'Eva Ready Workspace' }).count(), 1, 'Automatic Eva-ready project is unavailable');
    await page.locator('.workspace-project-item').filter({ hasText: 'project' }).click();
    await page.locator('#workspaceObjective').fill('E2E workspace run');
    await page.locator('#workspaceRunForm').evaluate(function(form) { form.requestSubmit(); });
    await page.locator('.workspace-run-item').filter({ hasText: 'E2E workspace run' }).waitFor();
    await page.locator('#workspaceOpenWorkbenchBtn').click();
    await page.locator('#workspaceWorkbench').waitFor({ state: 'visible' });
    const monitoredRun = page.locator('.workspace-monitor-run').filter({ hasText: 'E2E workspace run' });
    await monitoredRun.waitFor();
    await monitoredRun.click();
    await page.locator('#workspaceMonitorFeed').filter({ hasText: 'Eva monitor:' }).waitFor();
    const terminalAction = page.locator('.workspace-monitor-detail-actions button', { hasText: 'Open terminal' });
    await terminalAction.evaluate(function(element) { element.scrollIntoView({ block: 'center' }); });
    const terminalActionVisible = await terminalAction.evaluate(function(element) {
      const bounds = element.getBoundingClientRect();
      return bounds.top >= 0 && bounds.bottom <= window.innerHeight && bounds.left >= 0 && bounds.right <= window.innerWidth;
    });
    assert.strictEqual(terminalActionVisible, true, 'Workspace terminal action is unreachable in the main monitor');
    await terminalAction.click({ force: true });
    await page.locator('.workspace-terminal-status').filter({ hasText: 'CONNECTED' }).waitFor();
    assert.strictEqual(await page.locator('#terminalPanel').evaluate(function(panel) { return panel.classList.contains('terminal-panel-docked'); }), true, 'Workspace terminal did not use the monitor dock');
    assert.strictEqual(await page.locator('#workspaceWorkbench').isVisible(), true, 'Opening a terminal hid Workspace Monitor');
    const dockGeometry = await page.locator('#terminalPanel').evaluate(function(panel) {
      const bounds = panel.getBoundingClientRect();
      return { top: bounds.top, height: bounds.height, viewport: window.innerHeight };
    });
    assert.ok(dockGeometry.top >= dockGeometry.viewport * 0.4 && dockGeometry.height <= dockGeometry.viewport * 0.55, 'Terminal dock does not occupy the lower half');
    await page.locator('.workspace-terminal-host').click();
    await page.keyboard.type('pwd');
    await page.keyboard.press('Enter');
    await page.waitForFunction(function() {
      return document.querySelector('.workspace-terminal-host').innerText.includes('/worktrees/');
    });
    const terminalText = await page.locator('.workspace-terminal-host').innerText();
    assert.match(terminalText, /\/worktrees\//);
    const worktreePath = terminalText.split(/\r?\n/).find(function(line) { return line.includes('/worktrees/'); }).trim();
    await page.keyboard.type("sh -c 'trap \"\" HUP; sleep 60 & echo EVA_E2E_CHILD:$!'");
    await page.keyboard.press('Enter');
    await page.waitForFunction(function() {
      return document.querySelector('.workspace-terminal-host').innerText.includes('EVA_E2E_CHILD:');
    });
    const childMarkers = Array.from((await page.locator('.workspace-terminal-host').innerText()).matchAll(/EVA_E2E_CHILD:\s*(\d+)/g));
    const childPid = Number(childMarkers[childMarkers.length - 1] && childMarkers[childMarkers.length - 1][1]);
    assert.ok(Number.isInteger(childPid) && childPid > 0, 'Expected a background child PID');
    assert.strictEqual(processExists(childPid), true, 'Expected background child to start');
    await page.keyboard.type("printf '#!/bin/sh\\necho workspace asset\\n' > review-script.sh");
    await page.keyboard.press('Enter');
    assert.strictEqual(git(repository, ['branch', '--show-current']), 'main');
    assert.strictEqual(git(repository, ['status', '--porcelain']), '');
    const terminalSessions = await page.evaluate(async function() { return window.evaStandalone.terminalList(); });
    assert.strictEqual(terminalSessions.length, 1, 'Expected one live workspace terminal before discard');
    const terminalSessionId = terminalSessions[0].id;
    const visibleRuns = await page.evaluate(async function() { return window.evaStandalone.workspaceListRuns(); });
    const selectedRun = visibleRuns.find(function(run) { return run.objective === 'E2E workspace run'; });
    assert.ok(selectedRun && selectedRun.checkout, 'Expected the selected run checkout to remain available');
    assert.strictEqual(terminalSessions[0].rootId, selectedRun.checkout.id, 'Terminal root does not match the selected worktree');
    await page.screenshot({ path: path.join(artifactDirectory, 'workspace-e2e-desktop.png'), fullPage: true });

    await page.locator('#terminalPanelClose').click();
    await page.waitForFunction(function() {
      return document.querySelector('#terminalPanel').getAttribute('aria-hidden') === 'true';
    });
    await page.locator('#workspaceWorkbench').waitFor({ state: 'visible' });
    await page.locator('#evaAssetsBtn').click();
    await page.locator('#assetsView').waitFor({ state: 'visible' });
    await page.locator('.assets-view-row').filter({ hasText: 'review-script.sh' }).waitFor();
    await page.locator('.assets-view-row').filter({ hasText: 'review-script.sh' }).click();
    await page.locator('#assetsViewDetail').filter({ hasText: 'E2E workspace run' }).waitFor();
    assert.strictEqual(await page.locator('#workspaceWorkbench').isVisible(), false, 'Assets did not become the primary main view');
    await page.locator('#evaWorkspacesBtn').click();
    await page.locator('#agentsView').waitFor({ state: 'visible' });
    assert.strictEqual(await page.locator('#agentsViewTitle').innerText(), 'Workspace', 'Workspace tab did not restore the agentic-session workspace');
    await page.evaluate(function() { window.EvaWorkspaces.openWorkbench(); });
    await page.locator('#workspaceWorkbench').waitFor({ state: 'visible' });
    await page.locator('#evaTerminalBtn').click();
    await page.locator('#terminalPanel[aria-hidden="false"]').waitFor();
    assert.strictEqual(await page.locator('#workspaceWorkbench').isVisible(), true, 'Terminal sidebar navigation returned to chat');
    assert.strictEqual(await page.locator('#terminalPanel').evaluate(function(panel) { return panel.classList.contains('terminal-panel-docked'); }), true, 'Sidebar terminal was not docked');
    await page.locator('#terminalPanelClose').click();
    await page.evaluate(async function(run) {
      await window.evaStandalone.workspaceRunAction(run.id, 'discard', {
        confirmDirty: true,
        checkoutId: run.checkout.id
      });
    }, selectedRun);
    await page.waitForFunction(async function(runId) {
      const runs = await window.evaStandalone.workspaceListRuns();
      const run = runs.find(function(item) { return item.id === runId; });
      return run && run.status === 'discarded';
    }, selectedRun.id);
    const remainingSessions = await page.evaluate(async function() { return window.evaStandalone.terminalList(); });
    assert.strictEqual(remainingSessions.some(function(session) { return session.id === terminalSessionId; }), false, 'Discard retained its workspace terminal: ' + JSON.stringify(remainingSessions));
    await waitForProcessExit(childPid);
    const rootRejected = await page.evaluate(async function(rootId) {
      try {
        await window.evaStandalone.terminalCreate({ rootId: rootId, cols: 80, rows: 24 });
        return false;
      } catch (_) {
        return true;
      }
    }, selectedRun.checkout.id);
    assert.strictEqual(rootRejected, true, 'Discarded checkout retained terminal-root access');
    assert.strictEqual(await page.evaluate(function() { return 'workspaceCapabilityToken' in window.evaStandalone; }), false, 'Workspace capability leaked through preload');
    assert.strictEqual(fs.existsSync(worktreePath), false, 'Discard retained the managed worktree directory');
    assert.strictEqual(git(repository, ['worktree', 'list', '--porcelain']).includes('worktree ' + worktreePath + '\n'), false, 'Discard retained the managed worktree registration');
    console.log('workspace Electron E2E: PASS');
  } finally {
    if (browser) await browser.close();
    await stopProcess(child);
    fs.rmSync(sandbox, { recursive: true, force: true });
    if (child && child.exitCode && child.exitCode !== 0) process.stderr.write(stderr);
  }
}

main().catch(function(error) {
  console.error(error && error.stack ? error.stack : error);
  process.exit(1);
});
