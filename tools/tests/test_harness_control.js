#!/usr/bin/env node
'use strict';

const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

const source = fs.readFileSync('core/js/harness-control.js', 'utf8');
let importResult = { id: 'project-1' };
let importedUrl = '';
let submittedCommand = '';
let commandSubmitMode = null;
let plannedTask = null;
let plannedResult = null;
let selectedRepository = '';
let repositoryListCalls = 0;
let repositoryContinuationCalls = 0;
let githubAuthorizationCalls = 0;
let mcpModuleRequest = null;
let mcpVerificationRequest = null;
let workspaceCheckObjective = '';
let workspaceToolsProject = '';
const window = {
  EvaWorkspaces: {
    openWorkbench() {},
    listGitHubRepositories() {
      repositoryListCalls += 1;
      return Promise.resolve('Available GitHub repositories:\nexample/repository - https://github.com/example/repository');
    },
    continueGitHubRepositories() {
      repositoryContinuationCalls += 1;
      return Promise.resolve('GitHub repositories are listed in Workspaces. Name the repository you want to import.');
    },
    authorizeGitHub() {
      githubAuthorizationCalls += 1;
      return Promise.resolve();
    },
    describeProjectTools(projectName) {
      workspaceToolsProject = projectName;
      return Promise.resolve('LLM Assist Private has 1 enabled workspace MCP tool: work-iq.');
    },
        setProjectMcpServerByName(serverName, enabled, projectName) {
      mcpModuleRequest = { serverName, enabled, projectName };
      return Promise.resolve('Enabled workspace MCP server project-docs for example/repository.');
    },
        verifyProjectMcpServerByName(serverName, projectName) {
          mcpVerificationRequest = { serverName, projectName };
          return Promise.resolve('Started an isolated workspace run to verify MCP server project-docs for example/repository.');
        },
    runSelectedCheck(objective) {
      workspaceCheckObjective = objective;
      return Promise.resolve({ outcome: 'started', runId: 'run-check', message: 'Started a workspace-scoped agent run for example/repository. Progress and results will appear in Workspaces.' });
    },
    importGitHub(url) {
      importedUrl = url;
      return Promise.resolve(importResult);
    },
    importGitHubSelection(name) {
      selectedRepository = name;
      return Promise.resolve({ id: 'selected-project' });
    },
  },
};
const sandbox = {
  window,
  EvaWorkspaces: window.EvaWorkspaces,
  runEvaTerminalCommand(command, submit) {
    submittedCommand = command;
    commandSubmitMode = submit;
    return Promise.resolve({ submitted: submit !== false });
  },
  planEvaTerminalTask(objective, submit, allowDecline) {
    plannedTask = { objective: objective, submit: submit };
    if (allowDecline !== undefined) plannedTask.allowDecline = allowDecline;
    return Promise.resolve(plannedResult || { submitted: submit !== false });
  },
  document: { body: { classList: { contains() { return false; } } }, getElementById() { return null; } },
};
vm.runInNewContext(source, sandbox, { filename: 'core/js/harness-control.js' });
const harness = sandbox.EvaHarness;

async function main() {
  const url = 'https://github.com/example/repository';
  const unsolicitedList = await harness.execute({ action: 'list_github_repositories' }, { source: 'model', userRequest: 'What is the weather today?' });
  assert.strictEqual(unsolicitedList.ok, false);
  assert.match(unsolicitedList.message, /direct user interaction/);

  const educationalList = await harness.execute({ action: 'list_github_repositories' }, { source: 'model', userRequest: 'Tell me about GitHub repositories.' });
  assert.strictEqual(educationalList.ok, false);
  assert.match(educationalList.message, /direct user interaction/);

  const genericList = await harness.execute({ action: 'list_github_repositories' }, { source: 'model', userRequest: 'List GitHub repositories.' });
  assert.strictEqual(genericList.ok, false);
  assert.match(genericList.message, /direct user interaction/);

  const voiceRoute = harness.resolveNavigationRequest('List GitHub repos I owned in the terminal.', { directUser: true });
  assert.strictEqual(voiceRoute.action, 'list_github_repositories');
  assert.strictEqual(harness.resolveNavigationRequest('List GitHub repos I owned in the terminal.'), null);
  const ownedRepositoriesRoute = harness.resolveNavigationRequest('Can you list repositories that I own?', { directUser: true });
  assert.strictEqual(ownedRepositoriesRoute.action, 'list_github_repositories');
  const ownedRepositoriesResult = await harness.execute(ownedRepositoriesRoute, { source: 'voice', userRequest: 'Can you list repositories that I own?' });
  assert.strictEqual(ownedRepositoriesResult.ok, true);
  assert.strictEqual(repositoryListCalls, 1);
  assert.strictEqual(plannedTask, null);
  assert.strictEqual(harness.resolveNavigationRequest('Yes, GitHub repositories that I own.', { directUser: true }).action, 'list_github_repositories');
  assert.strictEqual(harness.resolveNavigationRequest('My GitHub repositories.', { directUser: true }).action, 'list_github_repositories');
  assert.strictEqual(harness.resolveNavigationRequest('Stories that I own.', { directUser: true }), null);
  const githubContinuationRoute = harness.resolveNavigationRequest('Please continue. I think you found the correct repository to import.', { directUser: true });
  assert.strictEqual(githubContinuationRoute.action, 'continue_github_repositories');
  const githubContinuationResult = await harness.execute(githubContinuationRoute, { source: 'voice', userRequest: 'Please continue. I think you found the correct repository to import.' });
  assert.strictEqual(githubContinuationResult.ok, true);
  assert.strictEqual(repositoryContinuationCalls, 1);
  const githubAuthorizationRoute = harness.resolveNavigationRequest('Authorize GitHub so I can import private repositories.', { directUser: true });
  assert.strictEqual(githubAuthorizationRoute.action, 'authorize_github');
  const githubAuthorizationResult = await harness.execute(githubAuthorizationRoute, { source: 'voice', userRequest: 'Authorize GitHub so I can import private repositories.' });
  assert.strictEqual(githubAuthorizationResult.ok, true);
  assert.strictEqual(githubAuthorizationCalls, 1);
  const modelAuthorization = await harness.execute({ action: 'authorize_github' }, { source: 'model', userRequest: 'Please import https://github.com/example/repository.' });
  assert.strictEqual(modelAuthorization.ok, true);
  assert.strictEqual(githubAuthorizationCalls, 2);
  const unrelatedModelAuthorization = await harness.execute({ action: 'authorize_github' }, { source: 'model', userRequest: 'What is the weather today?' });
  assert.strictEqual(unrelatedModelAuthorization.ok, false);
  const enableMcpRoute = harness.resolveNavigationRequest('Enable MCP server project-docs for example/repository.', { directUser: true });
  assert.strictEqual(enableMcpRoute.action, 'set_workspace_mcp_server');
  const enableMcpResult = await harness.execute(enableMcpRoute, { source: 'voice', userRequest: 'Enable MCP server project-docs for example/repository.' });
  assert.strictEqual(enableMcpResult.ok, true);
  assert.deepStrictEqual(mcpModuleRequest, { serverName: 'project-docs', enabled: true, projectName: 'example/repository' });
  assert.strictEqual(harness.resolveNavigationRequest('Disable MCP server project-docs.', { directUser: true }).enabled, false);
  const workspaceToolsRoute = harness.resolveNavigationRequest("for the LLM Assist Private, what's the enabled tool?", { directUser: true });
  assert.strictEqual(workspaceToolsRoute.action, 'describe_workspace_tools');
  assert.strictEqual(workspaceToolsRoute.projectName, 'LLM Assist Private');
  const workspaceToolsResult = await harness.execute(workspaceToolsRoute, { source: 'voice', userRequest: "for the LLM Assist Private, what's the enabled tool?" });
  assert.strictEqual(workspaceToolsResult.ok, true);
  assert.strictEqual(workspaceToolsProject, 'LLM Assist Private');
  assert.match(workspaceToolsResult.message, /work-iq/);
  const selectedWorkspaceToolsRoute = harness.resolveNavigationRequest('Which MCP tools are enabled?', { directUser: true });
  assert.strictEqual(selectedWorkspaceToolsRoute.action, 'describe_workspace_tools');
  assert.strictEqual(selectedWorkspaceToolsRoute.projectName, '');
  const verifyMcpRoute = harness.resolveNavigationRequest('Verify MCP server project-docs for example/repository is working.', { directUser: true });
  assert.strictEqual(verifyMcpRoute.action, 'verify_workspace_mcp_server');
  const verifyMcpResult = await harness.execute(verifyMcpRoute, { source: 'voice', userRequest: 'Verify MCP server project-docs for example/repository is working.' });
  assert.strictEqual(verifyMcpResult.ok, true);
  assert.deepStrictEqual(mcpVerificationRequest, { serverName: 'project-docs', projectName: 'example/repository' });
  const naturalVerifyMcpRoute = harness.resolveNavigationRequest('I see we have the Work IQ MCP server enabled, can you verify?', { directUser: true });
  assert.strictEqual(naturalVerifyMcpRoute.action, 'verify_workspace_mcp_server');
  assert.strictEqual(naturalVerifyMcpRoute.serverName, 'Work IQ');
  assert.strictEqual(harness.resolveNavigationRequest('Do not authorize GitHub.', { directUser: true }), null);
  const smokeTestRoute = harness.resolveNavigationRequest('run a smoketest', { directUser: true });
  assert.strictEqual(smokeTestRoute.action, 'run_workspace_check');
  assert.strictEqual(smokeTestRoute.objective, 'run a smoketest');
  const smokeTestResult = await harness.execute(smokeTestRoute, { source: 'voice', userRequest: 'run a smoketest' });
  assert.strictEqual(smokeTestResult.ok, true);
  assert.strictEqual(smokeTestResult.data.outcome, 'started');
  assert.strictEqual(workspaceCheckObjective, 'run a smoketest');
  assert.strictEqual(harness.resolveNavigationRequest('Please run the tests for the selected workspace.', { directUser: true }).action, 'run_workspace_check');
  assert.strictEqual(harness.resolveNavigationRequest('Run diagnostics.', { directUser: true }).action, 'run_workspace_check');
  assert.notStrictEqual(harness.resolveNavigationRequest('Build a website.', { directUser: true }).action, 'run_workspace_check');
  const accountVoiceRoute = harness.resolveNavigationRequest('List accounts under list github repositories enter my account.', { directUser: true });
  assert.strictEqual(accountVoiceRoute.action, 'list_github_repositories');
  assert.strictEqual(harness.resolveNavigationRequest('List GitHub repositories under my account.', { directUser: true }).action, 'list_github_repositories');
  assert.strictEqual(harness.resolveNavigationRequest('List GitHub repositories for an account.', { directUser: true }).action, 'consider_terminal_task');
  assert.strictEqual(harness.resolveNavigationRequest('Do not list GitHub repositories under my account.', { directUser: true }), null);
  const selectionRoute = harness.resolveNavigationRequest('Import repo repository.', { directUser: true });
  assert.strictEqual(selectionRoute.action, 'import_github_selection');
  const selectedImport = await harness.execute(selectionRoute, { source: 'voice', userRequest: 'Import repo repository.' });
  assert.strictEqual(selectedImport.ok, true);
  assert.strictEqual(selectedRepository, 'repository');

  const commandRoute = harness.resolveNavigationRequest('Run git status in the terminal.', { directUser: true });
  assert.strictEqual(commandRoute.action, 'run_terminal_command');
  assert.strictEqual(commandRoute.command, 'git status');
  assert.strictEqual(harness.resolveNavigationRequest('Run git status in the terminal.'), null);
  const politeCommandRoute = harness.resolveNavigationRequest('Can you run git status in the terminal?', { directUser: true });
  assert.strictEqual(politeCommandRoute.action, 'run_terminal_command');
  assert.strictEqual(politeCommandRoute.command, 'git status');
  const terminalVerbRoute = harness.resolveNavigationRequest('Use the terminal to execute git status.', { directUser: true });
  assert.strictEqual(terminalVerbRoute.action, 'run_terminal_command');
  assert.strictEqual(terminalVerbRoute.command, 'git status');
  const naturalTaskRoute = harness.resolveNavigationRequest('Use the terminal to show the current git status.', { directUser: true });
  assert.strictEqual(naturalTaskRoute.action, 'plan_terminal_task');
  assert.strictEqual(naturalTaskRoute.objective, 'show the current git status');
  assert.strictEqual(naturalTaskRoute.submit, true);
  const implicitInspectionRoute = harness.resolveNavigationRequest('Can you check my disk space?', { directUser: true });
  assert.strictEqual(implicitInspectionRoute.action, 'plan_terminal_task');
  assert.strictEqual(implicitInspectionRoute.objective, 'check my disk space');
  assert.strictEqual(implicitInspectionRoute.submit, true);
  const genericQuestionRoute = harness.resolveNavigationRequest('What is disk space?', { directUser: true });
  assert.strictEqual(genericQuestionRoute.action, 'consider_terminal_task');
  assert.strictEqual(harness.resolveNavigationRequest('What is your favorite color?', { directUser: true }).action, 'consider_terminal_task');
  assert.strictEqual(harness.resolveNavigationRequest('I had a good day.', { directUser: true }), null);
  assert.strictEqual(harness.resolveNavigationRequest('Can you check this for me?', { directUser: true }).action, 'consider_terminal_task');
  const stagedTaskRoute = harness.resolveNavigationRequest('Type a command to show the current git status in the terminal.', { directUser: true });
  assert.strictEqual(stagedTaskRoute.action, 'plan_terminal_task');
  assert.strictEqual(stagedTaskRoute.submit, false);
  const typeRoute = harness.resolveNavigationRequest('Type git status in the terminal.', { directUser: true });
  assert.strictEqual(typeRoute.action, 'type_terminal_command');
  assert.strictEqual(typeRoute.command, 'git status');
  const commandDenied = await harness.execute({ action: 'run_terminal_command', command: 'git status' }, { source: 'model', userRequest: 'What is the weather today?' });
  assert.strictEqual(commandDenied.ok, false);
  const taskDenied = await harness.execute({ action: 'plan_terminal_task', objective: 'show git status', submit: true }, { source: 'model', userRequest: 'Use the terminal to show git status.' });
  assert.strictEqual(taskDenied.ok, false);
  const commandSubmitted = await harness.execute(commandRoute, { source: 'voice', userRequest: 'Run git status in the terminal.' });
  assert.strictEqual(commandSubmitted.ok, true);
  assert.strictEqual(commandSubmitted.data.outcome, 'submitted');
  assert.strictEqual(submittedCommand, 'git status');
  assert.strictEqual(commandSubmitMode, undefined);
  const commandTyped = await harness.execute(typeRoute, { source: 'voice', userRequest: 'Type git status in the terminal.' });
  assert.strictEqual(commandTyped.ok, true);
  assert.strictEqual(commandTyped.data.outcome, 'submitted');
  assert.strictEqual(submittedCommand, 'git status');
  assert.strictEqual(commandSubmitMode, false);
  const taskSubmitted = await harness.execute(naturalTaskRoute, { source: 'voice', userRequest: 'Use the terminal to show the current git status.' });
  assert.strictEqual(taskSubmitted.ok, true);
  assert.deepStrictEqual(plannedTask, { objective: 'show the current git status', submit: true });
  plannedResult = { declined: true, submitted: false, reviewRequired: false };
  const candidateDeclined = await harness.execute(genericQuestionRoute, { source: 'voice', userRequest: 'What is disk space?' });
  assert.strictEqual(candidateDeclined.ok, true);
  assert.strictEqual(candidateDeclined.data.declined, true);
  assert.deepStrictEqual(plannedTask, { objective: 'What is disk space?', submit: true, allowDecline: true });
  plannedResult = null;

  const negatedList = await harness.execute({ action: 'list_github_repositories' }, { source: 'model', userRequest: 'Do not list my GitHub repositories.' });
  assert.strictEqual(negatedList.ok, false);
  assert.match(negatedList.message, /direct user interaction/);

  const revokedList = await harness.execute({ action: 'list_github_repositories' }, { source: 'model', userRequest: "List my GitHub repositories, but don't do that." });
  assert.strictEqual(revokedList.ok, false);
  assert.match(revokedList.message, /direct user interaction/);

  const deferredList = await harness.execute({ action: 'list_github_repositories' }, { source: 'model', userRequest: 'List my GitHub repositories only after I approve.' });
  assert.strictEqual(deferredList.ok, false);
  assert.match(deferredList.message, /direct user interaction/);

  const listed = await harness.execute({ action: 'list_github_repositories' }, { source: 'model', userRequest: 'List my GitHub repositories.' });
  assert.strictEqual(listed.ok, false);
  assert.match(listed.message, /direct user interaction/);
  const directListed = await harness.execute({ action: 'list_github_repositories' }, { source: 'voice', userRequest: 'List my GitHub repositories.' });
  assert.strictEqual(directListed.ok, true);
  assert.match(directListed.message, /https:\/\/github\.com\/example\/repository/);
  assert.strictEqual(importedUrl, '');

  const denied = await harness.execute({ action: 'import_github', repository_url: url }, { source: 'model', userRequest: 'Please import this GitHub repository.' });
  assert.strictEqual(denied.ok, false);
  assert.match(denied.message, /direct user interaction/);

  const imported = await harness.execute({ action: 'import_github', repository_url: url }, { source: 'model', userRequest: 'Please import https://github.com/example/repository.' });
  assert.strictEqual(imported.ok, true);
  assert.strictEqual(imported.data.outcome, 'imported');
  assert.strictEqual(importedUrl, url);

  const gitSuffixMismatch = await harness.execute({ action: 'import_github', repository_url: url }, { source: 'model', userRequest: 'Please import https://github.com/example/repository.git.' });
  assert.strictEqual(gitSuffixMismatch.ok, false);

  importResult = null;
  const cancelled = await harness.execute({ action: 'import_github', repository_url: url }, { source: 'model', userRequest: 'Please import https://github.com/example/repository.' });
  assert.strictEqual(cancelled.ok, false);
  assert.strictEqual(cancelled.data.outcome, 'cancelled');
  assert.match(cancelled.message, /cancelled/);
  console.log('harness control tests: PASS');
}

main().catch(function(error) {
  console.error(error.stack || error);
  process.exit(1);
});