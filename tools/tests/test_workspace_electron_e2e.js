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
    fs.mkdirSync(path.join(repository, 'src', 'features'), { recursive: true });
    fs.writeFileSync(path.join(repository, 'src', 'features', 'index.js'), 'module.exports = {};\n');
    const workspaceMcpConfig = JSON.stringify({
      mcpServers: { 'project-docs': { command: 'example-mcp', args: ['--docs'] } }
    });
    fs.writeFileSync(path.join(repository, 'mcp.json'), workspaceMcpConfig);
    git(repository, ['add', 'README.md', 'mcp.json', 'src/features/index.js']);
    git(repository, ['commit', '-m', 'Initial commit']);
    execFileSync('python3', [path.join(root, 'tools', 'tests', 'test_workspace_electron_setup.py'), configDirectory, repository], { encoding: 'utf8' });

    const debuggingPort = await reservePort();
    child = spawn(electronPath, [
      '--eva-workspace-terminal-v1',
      '--remote-debugging-port=' + debuggingPort,
      '--user-data-dir=' + path.join(sandbox, 'electron-profile')
    ], {
      env: Object.assign({}, process.env, { EVA_CONFIG_DIR: configDirectory, EVA_WORKSPACE_AGENT_AUTODISPATCH: '0' }),
      stdio: ['ignore', 'ignore', 'pipe']
    });
    child.stderr.on('data', function(chunk) { stderr = (stderr + chunk.toString()).slice(-4000); });
    let attached;
    try {
      attached = await waitForPage('http://127.0.0.1:' + debuggingPort);
    } catch (error) {
      throw new Error(error.message + (stderr ? '\nElectron stderr:\n' + stderr : ''));
    }
    browser = attached.browser;
    const page = attached.page;
    await page.setViewportSize({ width: 1280, height: 900 });

    await page.locator('#evaWorkspacesBtn').click();
    await page.locator('#workspaceWorkbench').waitFor({ state: 'visible' });
    assert.strictEqual(await page.locator('#workspaceWorkbench h1').innerText(), 'Workspaces', 'Workspace tab did not open the coding workspace dashboard');
    assert.strictEqual(await page.evaluate(function() { return document.body.classList.contains('workspace-workbench-open'); }), true, 'Workspace dashboard is not active');
    await page.evaluate(function() { window.EvaWorkspaces.closeWorkbench(); });
    await page.evaluate(function() { document.getElementById('lcarsWorkspacesBtn').click(); });
    await page.locator('#workspaceWorkbench').waitFor({ state: 'visible' });
    assert.strictEqual(await page.evaluate(function() { return document.body.classList.contains('workspace-workbench-open'); }), true, 'LCARS Workspaces click was closed by sidebar bubbling');
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
    assert.strictEqual(await page.locator('#authGitHubCliBtn').count(), 1, 'GitHub device authorization command is missing from Settings > Auth');
    const githubAuthSurface = await page.evaluate(function() {
      return {
        start: typeof window.evaStandalone.workspaceGitHubAuthStart,
        status: typeof window.evaStandalone.workspaceGitHubAuthStatus,
        authorize: typeof window.EvaWorkspaces.authorizeGitHub,
      };
    });
    assert.deepStrictEqual(githubAuthSurface, { start: 'function', status: 'function', authorize: 'function' }, 'GitHub device authorization APIs are unavailable');
    const projectWorkspace = page.locator('#workspaceWorkbenchProjects .workspace-monitor-run').filter({ hasText: 'project' });
    await projectWorkspace.waitFor();
    await projectWorkspace.click();
    await page.locator('#workspaceProjectFiles .workspace-project-file').filter({ hasText: 'README.md' }).waitFor();
    let sourceFolder = page.locator('#workspaceProjectFiles > details.workspace-tree-folder').filter({ hasText: 'src' });
    await sourceFolder.waitFor();
    assert.strictEqual(await sourceFolder.evaluate(function(folder) { return folder.open; }), false, 'Project folders should start collapsed');
    const nestedFile = page.locator('#workspaceProjectFiles .workspace-project-file').filter({ hasText: 'index.js' });
    assert.strictEqual(await nestedFile.isVisible(), false, 'Nested file was visible before its folder expanded');
    await sourceFolder.locator(':scope > summary').click();
    let featuresFolder = sourceFolder.locator(':scope > .workspace-tree-children > details.workspace-tree-folder').filter({ hasText: 'features' });
    await featuresFolder.waitFor({ state: 'visible' });
    assert.strictEqual(await featuresFolder.evaluate(function(folder) { return folder.open; }), false, 'Nested folders should start collapsed');
    await featuresFolder.locator(':scope > summary').click();
    await nestedFile.waitFor({ state: 'visible' });
    const readyWorkspace = page.locator('#workspaceWorkbenchProjects .workspace-monitor-run').filter({ hasText: 'Eva Ready Workspace' });
    await readyWorkspace.click();
    await projectWorkspace.click();
    sourceFolder = page.locator('#workspaceProjectFiles > details.workspace-tree-folder').filter({ hasText: 'src' });
    await sourceFolder.waitFor();
    assert.strictEqual(await sourceFolder.evaluate(function(folder) { return folder.open; }), true, 'Project folder expansion was not preserved');
    featuresFolder = sourceFolder.locator(':scope > .workspace-tree-children > details.workspace-tree-folder').filter({ hasText: 'features' });
    assert.strictEqual(await featuresFolder.evaluate(function(folder) { return folder.open; }), true, 'Nested folder expansion was not preserved');
    await nestedFile.waitFor({ state: 'visible' });
    await page.locator('#workspaceImportGitHubBtn').click();
    await page.locator('#evaTextPrompt[aria-hidden="false"]').waitFor({ state: 'visible' });
    await page.locator('#evaTextPromptCancel').click();
    await page.locator('#evaTextPrompt[aria-hidden="true"]').waitFor({ state: 'hidden' });
    assert.strictEqual(await page.locator('#workspaceAddProjectWorkbenchBtn').isEnabled(), true, 'Import workspace stayed disabled after cancelling GitHub import');
    assert.strictEqual(await page.locator('#workspaceImportGitHubBtn').isEnabled(), true, 'Import GitHub stayed disabled after cancelling its prompt');
    const structuredImportError = await page.evaluate(async function() {
      const originalPrompt = window.evaTextPrompt;
      try {
        let resolvePrompt;
        window.evaTextPrompt = function() {
          return new Promise(function(resolve) { resolvePrompt = resolve; });
        };
        const importAttempt = window.EvaWorkspaces.importGitHub('https://github.com/example/invalid/path');
        while (!resolvePrompt) await new Promise(function(resolve) { setTimeout(resolve, 0); });
        const message = document.getElementById('workspaceWorkbenchStatus').textContent;
        resolvePrompt(null);
        await importAttempt;
        return message;
      } finally {
        window.evaTextPrompt = originalPrompt;
      }
    });
    assert.match(structuredImportError, /GitHub workspace import failed/, 'GitHub import did not render the structured IPC failure');
    assert.doesNotMatch(structuredImportError, /Error invoking remote method/, 'GitHub import exposed Electron IPC wrapper text');
    const voicePromptValue = await page.evaluate(async function() {
      const pending = window.evaTextPrompt('GitHub repository URL', '', { maxLength: 2048 });
      window.evaTextPromptConsumeVoice('https colon slash slash github dot com slash example slash packaged-test');
      return pending;
    });
    assert.strictEqual(voicePromptValue, 'https://github.com/example/packaged-test', 'Voice did not resolve the native GitHub URL prompt');
    assert.strictEqual(await page.locator('#evaTextPrompt').getAttribute('aria-hidden'), 'true', 'Voice left the GitHub URL prompt open');
    const correctedVoicePromptValue = await page.evaluate(async function() {
      const pending = window.evaTextPrompt('Correct GitHub repository URL', 'https://github.com/example/wrong-repo', { maxLength: 2048 });
      if (!window.evaTextPromptConsumeVoice('you misspelled it')) throw new Error('Correction guidance was not handled');
      if (!window.evaTextPromptIsOpen()) throw new Error('Correction guidance submitted as the URL');
      window.evaTextPromptConsumeVoice("it's packaged dash repo");
      return pending;
    });
    assert.strictEqual(correctedVoicePromptValue, 'https://github.com/example/packaged-repo', 'Voice did not correct the repository segment');
    const nativeFieldValue = await page.evaluate(async function() {
      const pending = window.evaTextPrompt('GitHub repository URL', '', { maxLength: 2048 });
      const output = document.createElement('div');
      await window.renderEvaResponse('[[EVA_HARNESS]]{"action":"set_field","field":"github_repository_url","value":"https://github.com/example/native-field","submit":true}[[/EVA_HARNESS]]', output);
      if (!window.evaTextPromptIsOpen()) throw new Error('Model output submitted a native field');
      const direct = window.EvaHarness.execute({ action: 'set_field', field: 'github_repository_url', value: 'https://github.com/example/native-field', submit: true });
      return { value: await pending, text: output.textContent, direct: direct };
    });
    assert.strictEqual(nativeFieldValue.value, 'https://github.com/example/native-field', 'Harness field control did not submit the GitHub URL');
    assert.strictEqual(nativeFieldValue.direct.ok, true, 'Direct native field control failed');
    assert.match(nativeFieldValue.text, /requires direct user interaction/);
    const spelledRepositoryValue = await page.evaluate(async function() {
      const pending = window.evaTextPrompt('Correct GitHub repository URL', 'https://github.com/appatalks/wrong-name', { maxLength: 2048 });
      window.evaTextPromptConsumeVoice('my repository name is spelled A P P A T A L K S');
      return pending;
    });
    assert.strictEqual(spelledRepositoryValue, 'https://github.com/appatalks/APPATALKS', 'Conversational spelling polluted the GitHub owner');
    const workspaceDescription = await page.evaluate(function() { return window.EvaWorkspaces.describe(); });
    assert.match(workspaceDescription, /I can access \d+ coding workspaces?:/);
    assert.match(workspaceDescription, /project/);
    const sourceCheckout = await page.evaluate(async function() {
      const projects = await window.evaStandalone.workspaceListProjects();
      return projects.find(function(project) { return project.name === 'project'; }).sourceCheckout;
    });
    await page.locator('#workspaceWorkbenchDetail .workspace-monitor-detail-actions button', { hasText: 'Open project terminal' }).click();
    await page.locator('.workspace-terminal-status').filter({ hasText: 'CONNECTED' }).waitFor();
    const sourceTerminals = await page.evaluate(async function() { return window.evaStandalone.terminalList(); });
    const sourceTerminal = sourceTerminals.find(function(terminal) { return terminal.rootId === sourceCheckout.id; });
    assert.ok(sourceTerminal, 'Project terminal did not use the selected source checkout');
    await page.evaluate(async function(terminalId) { await window.evaStandalone.terminalClose(terminalId); }, sourceTerminal.id);
    await page.locator('#terminalPanelClose').click();
    const mcpToggle = page.locator('#workspaceWorkbenchDetail .workspace-mcp-row input');
    page.once('dialog', function(dialog) { return dialog.accept(); });
    await mcpToggle.check();
    await page.waitForFunction(async function() {
      const projects = await window.evaStandalone.workspaceListProjects();
      return projects.some(function(project) {
        return project.name === 'project' && project.mcpServers.servers.some(function(server) {
          return server.name === 'project-docs' && server.enabled;
        });
      });
    });
    const objectiveInput = page.locator('#workspaceWorkbenchDetail .workspace-workbench-run-form textarea');
    await objectiveInput.fill('E2E workspace run');
    const monitorChangeTerminal = await page.evaluate(async function(rootId) {
      return window.evaStandalone.terminalCreate({ rootId: rootId, cols: 80, rows: 24 });
    }, sourceCheckout.id);
    fs.writeFileSync(path.join(repository, 'mcp.json'), JSON.stringify({
      mcpServers: { 'project-docs': { command: 'changed-mcp', args: [] } }
    }));
    await page.waitForFunction(function() {
      const toggle = document.querySelector('#workspaceWorkbenchDetail .workspace-mcp-row input');
      return toggle && !toggle.checked;
    }, null, { timeout: 20000 });
    assert.strictEqual(await objectiveInput.inputValue(), 'E2E workspace run', 'Workspace monitor state change cleared the coding objective');
    fs.writeFileSync(path.join(repository, 'mcp.json'), workspaceMcpConfig);
    await page.waitForFunction(async function() {
      const projects = await window.evaStandalone.workspaceListProjects();
      const project = projects.find(function(item) { return item.name === 'project'; });
      const server = project && project.mcpServers.servers.find(function(item) { return item.name === 'project-docs'; });
      return server && server.command === 'example-mcp' && !server.enabled;
    }, null, { timeout: 20000 });
    assert.strictEqual(await mcpToggle.isChecked(), false, 'Restoring an old MCP digest silently restored approval');
    assert.strictEqual(await objectiveInput.inputValue(), 'E2E workspace run', 'MCP revocation refresh cleared the coding objective');
    page.once('dialog', function(dialog) { return dialog.accept(); });
    await mcpToggle.click();
    await page.waitForFunction(async function() {
      const projects = await window.evaStandalone.workspaceListProjects();
      return projects.some(function(project) {
        return project.name === 'project' && project.mcpServers.servers.some(function(server) {
          return server.name === 'project-docs' && server.enabled;
        });
      });
    });
    await page.evaluate(async function(terminalId) { await window.evaStandalone.terminalClose(terminalId); }, monitorChangeTerminal.id);
    await page.locator('#workspaceWorkbenchDetail .workspace-workbench-run-form').evaluate(function(form) { form.requestSubmit(); });
    const monitoredRun = page.locator('#workspaceWorkbenchRuns .workspace-monitor-run').filter({ hasText: 'E2E workspace run' });
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
      return /EVA_E2E_CHILD:\s*\d+/.test(document.querySelector('.workspace-terminal-host').innerText);
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
    await page.locator('#workspaceWorkbench').waitFor({ state: 'visible' });
    assert.match(await page.locator('#workspaceWorkbenchProjects').innerText(), /1 MCP available/, 'Imported workspace MCP modules are not visible in the project summary');
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
