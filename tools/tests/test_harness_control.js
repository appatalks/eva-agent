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
let removedWorkspace = '';
let retriedRunId = '';
let remediationRequest = null;
let nativeRemediationContext = null;
let visibleUserMessages = [];
let workspaceOpenCalls = 0;
let workspaceDescriptionCalls = 0;
let assetsOpenCalls = 0;
let skillsOpenCalls = 0;
let agentsOpenCalls = 0;
let sessionsOpenCalls = 0;
let mergeRequest = null;
let pullRequestViewRequest = null;
let branchDeleteRequest = null;
const fixtureRepository = 'fixture-owner/fixture-repository';
const fixtureShortName = 'fixture-repository';
const fixtureRepositoryUrl = 'https://github.com/fixture-owner/fixture-repository';
const localStorage = {
  values: {},
  getItem(key) { return this.values[key] || null; },
  setItem(key, value) { this.values[key] = String(value); }
};
const window = {
  evaStandalone: {
    workspaceRemediationContextLoad() { return nativeRemediationContext || {}; },
    workspaceRemediationContextSave(value) { nativeRemediationContext = Object.assign({}, value); return Promise.resolve(true); },
    githubMergePullRequest(request) {
      mergeRequest = request;
      return Promise.resolve({ url: 'https://github.com/example/repository/pull/' + request.number, mergeCommit: 'a'.repeat(40) });
    },
    githubViewPullRequest(request) {
      pullRequestViewRequest = request;
      return Promise.resolve({
        number: request.number, title: 'Native routing', state: 'OPEN', draft: false, mergeState: 'CLEAN',
        url: 'https://github.com/example/repository/pull/' + request.number,
        checks: [{ name: 'static-checks', conclusion: 'SUCCESS' }]
      });
    },
    githubDeletePullRequestBranch(request) {
      branchDeleteRequest = request;
      return Promise.resolve({
        number: request.number, repository: request.repository,
        branch: 'update/native-capability-awareness',
        url: 'https://github.com/example/repository/pull/' + request.number
      });
    }
  },
  EvaWorkspaces: {
    openWorkbench() { workspaceOpenCalls += 1; },
    describe() {
      workspaceDescriptionCalls += 1;
      return Promise.resolve('I can access 2 coding workspaces: Alpha and Beta. There are 0 active coding runs.');
    },
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
    removeProjectByName(projectName) {
      removedWorkspace = projectName;
      return Promise.resolve('Removed LLM Assist Private from Eva. The source repository was preserved.');
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
    retryRun(runId) {
      retriedRunId = runId;
      return Promise.resolve('Workspace agent retry started for example/repository.');
    },
    startRepositoryRemediation(repositoryName, objective) {
      remediationRequest = { repositoryName, objective };
      return Promise.resolve({ runId: 'run-remediation', projectName: repositoryName, message: 'Started Workspace run run-remediation for ' + repositoryName + '.' });
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
  EvaAssets: {
    open() { assetsOpenCalls += 1; },
    describe() { return Promise.resolve('I can access 3 assets: report.md, notes.txt, and build.log.'); },
  },
  EvaSkills: {
    open() { skillsOpenCalls += 1; },
    describe() { return Promise.resolve('There are 2 saved skills, 1 active. Available examples: Research and Review.'); },
  },
  EvaAgents: {
    open() { agentsOpenCalls += 1; },
    describe() { return Promise.resolve('There is 1 active agent out of 2 recent sessions: Review (RUNNING).'); },
  },
};
const sessionPanel = { getAttribute() { return 'true'; } };
const sandbox = {
  window,
  EvaWorkspaces: window.EvaWorkspaces,
  EvaAssets: window.EvaAssets,
  EvaSkills: window.EvaSkills,
  EvaAgents: window.EvaAgents,
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
  document: {
    body: { classList: { contains() { return false; } } },
    getElementById(id) { return id === 'sessionPanel' ? sessionPanel : null; },
    querySelectorAll(selector) {
      return selector === '.chat-bubble.user-bubble'
        ? visibleUserMessages.map(function(text) { return { textContent: 'You: ' + text }; })
        : [];
    }
  },
  localStorage,
  describeSavedSessions() { return Promise.resolve('There are 2 saved chat sessions: Today and Planning.'); },
  toggleSessionPanel() { sessionsOpenCalls += 1; },
  evaTextPrompt() { return Promise.resolve('MERGE'); },
};
vm.runInNewContext(source, sandbox, { filename: 'core/js/harness-control.js' });
const harness = sandbox.EvaHarness;

async function main() {
  const url = 'https://github.com/example/repository';
  const workspaceCountRequest = 'Open Eva\'s Workspaces window and count the listed workspaces without making changes.';
  const workspaceCountRoute = harness.resolveNavigationRequest(workspaceCountRequest, { directUser: true });
  assert.strictEqual(workspaceCountRoute.action, 'describe_workspaces');
  const workspaceCountResult = await harness.execute(workspaceCountRoute, { source: 'voice', userRequest: workspaceCountRequest });
  assert.strictEqual(workspaceCountResult.ok, true);
  assert.strictEqual(workspaceOpenCalls, 1);
  assert.strictEqual(workspaceDescriptionCalls, 1);
  assert.match(workspaceCountResult.message, /2 coding workspaces/);
  const assetsRoute = harness.resolveNavigationRequest('List my generated assets.', { directUser: true });
  assert.strictEqual(assetsRoute.action, 'describe_assets');
  const assetsResult = await harness.execute(assetsRoute, { source: 'voice', userRequest: 'List my generated assets.' });
  assert.strictEqual(assetsResult.ok, true);
  assert.strictEqual(assetsOpenCalls, 1);
  assert.match(assetsResult.message, /3 assets/);
  const skillsRoute = harness.resolveNavigationRequest('Count my saved skills.', { directUser: true });
  assert.strictEqual(skillsRoute.action, 'describe_skills');
  const skillsResult = await harness.execute(skillsRoute, { source: 'voice', userRequest: 'Count my saved skills.' });
  assert.strictEqual(skillsResult.ok, true);
  assert.strictEqual(skillsOpenCalls, 1);
  assert.match(skillsResult.message, /2 saved skills/);
  const sessionsRoute = harness.resolveNavigationRequest('Which saved sessions are available?', { directUser: true });
  assert.strictEqual(sessionsRoute.action, 'describe_sessions');
  const sessionsResult = await harness.execute(sessionsRoute, { source: 'voice', userRequest: 'Which saved sessions are available?' });
  assert.strictEqual(sessionsResult.ok, true);
  assert.strictEqual(sessionsOpenCalls, 1);
  assert.match(sessionsResult.message, /2 saved chat sessions/);
  const agentsRoute = harness.resolveNavigationRequest('Show active agents.', { directUser: true });
  assert.strictEqual(agentsRoute.action, 'describe_agents');
  const agentsResult = await harness.execute(agentsRoute, { source: 'voice', userRequest: 'Show active agents.' });
  assert.strictEqual(agentsResult.ok, true);
  assert.strictEqual(agentsOpenCalls, 1);
  assert.match(agentsResult.message, /1 active agent/);
  const manifestActions = harness.capabilities().actions;
  ['describe_workspaces', 'describe_assets', 'describe_skills', 'describe_sessions', 'describe_agents'].forEach(function(action) {
    assert.ok(manifestActions.includes(action), action + ' must be exposed in the native action manifest');
  });
  const pullUrl = 'https://github.com/example/repository/pull/183';
  const pullViewRoute = harness.resolveNavigationRequest(pullUrl, { directUser: true });
  assert.strictEqual(pullViewRoute.action, 'describe_github_pull_request');
  assert.strictEqual(pullViewRoute.number, 183);
  const pullViewResult = await harness.execute(pullViewRoute, { source: 'voice', userRequest: pullUrl });
  assert.strictEqual(pullViewResult.ok, true);
  assert.strictEqual(pullRequestViewRequest.number, 183);
  assert.strictEqual(pullRequestViewRequest.repository, 'example/repository');
  assert.match(pullViewResult.message, /PR #183/);
  assert.match(pullViewResult.message, /static-checks: SUCCESS/);
  const correctedPullRoute = harness.resolveNavigationRequest('https://github.com/Apatox/eva-agent/pull/183', { directUser: true });
  assert.strictEqual(correctedPullRoute.repository, 'appatalks/eva-agent');
  const correctedImportRoute = harness.resolveNavigationRequest('Import repo Apatox/eva-agent.', { directUser: true });
  assert.strictEqual(correctedImportRoute.repositoryName, 'appatalks/eva-agent');
  const mergeRoute = harness.resolveNavigationRequest('Merge PR #183 into main.', { directUser: true });
  assert.strictEqual(mergeRoute.action, 'merge_github_pull_request');
  assert.strictEqual(mergeRoute.number, 183);
  const mergeResult = await harness.execute(mergeRoute, { source: 'voice', userRequest: 'Merge PR #183 into main.' });
  assert.strictEqual(mergeResult.ok, true);
  assert.strictEqual(mergeRequest.number, 183);
  assert.strictEqual(mergeRequest.repository, 'example/repository');
  assert.strictEqual(mergeRequest.confirmation, 'MERGE');
  assert.match(mergeResult.message, /Merged pull request #183/);
  const modelMerge = await harness.execute(mergeRoute, { source: 'model', userRequest: 'Merge PR #183 into main.' });
  assert.strictEqual(modelMerge.ok, false);
  assert.match(modelMerge.message, /direct user interaction/);
  const branchDeleteRoute = harness.resolveNavigationRequest('Delete the associated branch.', { directUser: true });
  assert.strictEqual(branchDeleteRoute.action, 'delete_github_pull_request_branch');
  assert.strictEqual(branchDeleteRoute.number, 183);
  assert.strictEqual(branchDeleteRoute.repository, 'example/repository');
  const branchDeleteResult = await harness.execute(branchDeleteRoute, { source: 'voice', userRequest: 'Delete the associated branch.' });
  assert.strictEqual(branchDeleteResult.ok, true);
  assert.strictEqual(branchDeleteRequest.number, 183);
  assert.strictEqual(branchDeleteRequest.repository, 'example/repository');
  assert.match(branchDeleteResult.message, /Deleted branch update\/native-capability-awareness/);
  const modelBranchDelete = await harness.execute(branchDeleteRoute, { source: 'model', userRequest: 'Delete the associated branch.' });
  assert.strictEqual(modelBranchDelete.ok, false);
  assert.match(modelBranchDelete.message, /direct user interaction/);
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
  const modelMcpResult = await harness.execute(enableMcpRoute, { source: 'model', userRequest: 'Enable MCP server project-docs for example/repository.' });
  assert.strictEqual(modelMcpResult.ok, true);
  const mismatchedModelMcp = await harness.execute(
    Object.assign({}, enableMcpRoute, { projectName: 'example/other-repository' }),
    { source: 'model', userRequest: 'Enable MCP server project-docs for example/repository.' }
  );
  assert.strictEqual(mismatchedModelMcp.ok, false);
  assert.strictEqual(harness.resolveNavigationRequest('Disable MCP server project-docs.', { directUser: true }).enabled, false);
  const retryWorkspaceRoute = harness.resolveNavigationRequest('Retry the workspace run run-123.', { directUser: true });
  assert.strictEqual(retryWorkspaceRoute.action, 'retry_workspace_run');
  const retryWorkspaceResult = await harness.execute(retryWorkspaceRoute, { source: 'model', userRequest: 'Retry the workspace run run-123.' });
  assert.strictEqual(retryWorkspaceResult.ok, true);
  assert.strictEqual(retriedRunId, 'run-123');
  const mismatchedRetry = await harness.execute(
    { action: 'retry_workspace_run' },
    { source: 'model', userRequest: 'Retry the workspace run run-123.' }
  );
  assert.strictEqual(mismatchedRetry.ok, false);
  const remediationRoute = harness.resolveNavigationRequest('Resolve Dependabot alerts in ' + fixtureRepository + '.', { directUser: true });
  assert.strictEqual(remediationRoute.action, 'run_repository_remediation');
  const remediationResult = await harness.execute(remediationRoute, { source: 'model', userRequest: 'Resolve Dependabot alerts in ' + fixtureRepository + '.' });
  assert.strictEqual(remediationResult.ok, true);
  assert.strictEqual(remediationResult.data.runId, 'run-remediation');
  assert.deepStrictEqual(remediationRequest, { repositoryName: fixtureRepository, objective: 'Resolve Dependabot alerts in ' + fixtureRepository });
  const dependabotUrlRequest = 'Eva please review the Dependabot alerts at: ' + fixtureRepositoryUrl + '/security/dependabot and then please address them in a Pull request';
  const normalizedDependabotUrlRequest = dependabotUrlRequest.replace(/^Eva\s+/i, '');
  const dependabotUrlRoute = harness.resolveNavigationRequest(dependabotUrlRequest, { directUser: true });
  assert.strictEqual(dependabotUrlRoute.action, 'run_repository_remediation');
  assert.strictEqual(dependabotUrlRoute.repositoryName, fixtureRepository);
  const dependabotUrlResult = await harness.execute(dependabotUrlRoute, { source: 'model', userRequest: dependabotUrlRequest });
  assert.strictEqual(dependabotUrlResult.ok, true);
  assert.deepStrictEqual(remediationRequest, { repositoryName: fixtureRepository, objective: normalizedDependabotUrlRequest });
  assert.deepStrictEqual(nativeRemediationContext, { repositoryName: fixtureRepository, objective: normalizedDependabotUrlRequest });
  delete localStorage.values.eva_last_repository_remediation;
  delete localStorage.values.aigMessages;
  const pullRequestFollowUp = harness.resolveNavigationRequest('Please create the PR now.', { directUser: true });
  assert.strictEqual(pullRequestFollowUp.action, 'run_repository_remediation');
  assert.strictEqual(pullRequestFollowUp.repositoryName, fixtureRepository);
  assert.match(pullRequestFollowUp.objective, /Follow-up: Please create the PR now/);
  nativeRemediationContext = null;
  delete localStorage.values.eva_last_repository_remediation;
  visibleUserMessages = [dependabotUrlRequest];
  const visiblePullRequestFollowUp = harness.resolveNavigationRequest('Go ahead and create a PR too please.', { directUser: true });
  assert.strictEqual(visiblePullRequestFollowUp.action, 'run_repository_remediation');
  assert.strictEqual(visiblePullRequestFollowUp.repositoryName, fixtureRepository);
  assert.match(visiblePullRequestFollowUp.objective, /Follow-up: Go ahead and create a PR too please/);
  const visibleWorkspaceContinuation = harness.resolveNavigationRequest('Eva please continue and ensure we have a workspace created for tracking the progress.', { directUser: true });
  assert.strictEqual(visibleWorkspaceContinuation.action, 'run_repository_remediation');
  assert.strictEqual(visibleWorkspaceContinuation.repositoryName, fixtureRepository);
  assert.match(visibleWorkspaceContinuation.objective, /Follow-up: please continue and ensure we have a workspace created/);
  visibleUserMessages = [];
  const shortRemediationRoute = harness.resolveNavigationRequest('Fix dependency alerts with ' + fixtureShortName + ' repo.', { directUser: true });
  assert.strictEqual(shortRemediationRoute.action, 'run_repository_remediation');
  assert.strictEqual(shortRemediationRoute.repositoryName, fixtureShortName);
  assert.strictEqual(harness.resolveNavigationRequest('Try to resolve some alerts with ' + fixtureShortName + ' repo.', { directUser: true }).action, 'run_repository_remediation');
  const retryRemediationRoute = harness.resolveNavigationRequest('Try again please.', { directUser: true });
  assert.strictEqual(retryRemediationRoute.action, 'run_repository_remediation');
  assert.strictEqual(retryRemediationRoute.repositoryName, fixtureShortName);
  delete localStorage.values.eva_last_repository_remediation;
  nativeRemediationContext = null;
  localStorage.setItem('aigMessages', JSON.stringify([
    { role: 'user', content: 'Try to resolve some alerts with ' + fixtureShortName + ' repo.' },
    { role: 'assistant', content: 'I will locate it.' }
  ]));
  const recoveredRetryRoute = harness.resolveNavigationRequest('Eva can you try again please. I updated your permissions.', { directUser: true });
  assert.strictEqual(recoveredRetryRoute.action, 'run_repository_remediation');
  assert.strictEqual(recoveredRetryRoute.repositoryName, fixtureShortName);
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
  const removeWorkspaceRoute = harness.resolveNavigationRequest('Remove workspace LLM Assist Private.', { directUser: true });
  assert.strictEqual(removeWorkspaceRoute.action, 'remove_workspace');
  assert.strictEqual(removeWorkspaceRoute.projectName, 'LLM Assist Private');
  const removeWorkspaceResult = await harness.execute(removeWorkspaceRoute, { source: 'voice', userRequest: 'Remove workspace LLM Assist Private.' });
  assert.strictEqual(removeWorkspaceResult.ok, true);
  assert.strictEqual(removedWorkspace, 'LLM Assist Private');
  assert.match(removeWorkspaceResult.message, /source repository was preserved/i);
  const conversationalRemovalRequest = 'Hi Eva. Please remove the ' + fixtureRepository + ' Workspaces, it is no longer needed at this time.';
  const conversationalRemovalRoute = harness.resolveNavigationRequest(conversationalRemovalRequest, { directUser: true });
  assert.strictEqual(conversationalRemovalRoute.action, 'remove_workspace');
  assert.strictEqual(conversationalRemovalRoute.projectName, fixtureRepository);
  const conversationalRemovalResult = await harness.execute(conversationalRemovalRoute, { source: 'voice', userRequest: conversationalRemovalRequest });
  assert.strictEqual(conversationalRemovalResult.ok, true);
  assert.strictEqual(removedWorkspace, fixtureRepository);
  const cleanupRemovalRequest = 'Eva please cleanup and remove the ' + fixtureRepository + ' workspaces, as its no longer needed.';
  const cleanupRemovalRoute = harness.resolveNavigationRequest(cleanupRemovalRequest, { directUser: true });
  assert.strictEqual(cleanupRemovalRoute.action, 'remove_workspace');
  assert.strictEqual(cleanupRemovalRoute.projectName, fixtureRepository);
  const modelCleanupRemoval = await harness.execute({ action: 'remove_workspace', projectName: fixtureRepository }, { source: 'model', userRequest: cleanupRemovalRequest });
  assert.strictEqual(modelCleanupRemoval.ok, true);
  assert.strictEqual(removedWorkspace, fixtureRepository);
  const mismatchedModelRemoval = await harness.execute({ action: 'remove_workspace', projectName: 'example/other' }, { source: 'model', userRequest: cleanupRemovalRequest });
  assert.strictEqual(mismatchedModelRemoval.ok, false);
  assert.match(mismatchedModelRemoval.message, /direct user interaction/);
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