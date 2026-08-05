# Eva Workspaces and Terminal Plan

Status: proposed implementation plan.

## Product decision

Eva should treat a coding task as a durable workspace run, not as an unusually
long chat session. A run can have a source repository, an isolated Git
checkout, one primary coding conversation, one or more child agents, terminal
tabs, browser runs, artifacts, and a review/merge outcome. The user can leave
the run, work elsewhere, and return to the exact state without losing its
relationships.

The first release target is Eva Standalone. It can safely offer a real local
PTY through Electron. The static browser UI remains supported, but receives a
non-interactive or explicitly configured bridge-backed experience rather than
pretending it has unrestricted local terminal access.

This is a coding-agent workspace layered on Eva's existing chat and AIG
features. It does not replace ordinary conversations or require every user to
choose a repository.

## Current foundation

The plan builds on existing, useful pieces rather than starting over:

- Browser chat sessions persist in IndexedDB and have a stable frontend
  `session_id`.
- AIG can spawn up to four ACP subagents, preserve their originating
  `session_id`, expose their status in Agent Operations, and accept steering.
- ACP conversations are bounded per frontend conversation, avoiding an
  unbounded hidden context in one warm Copilot CLI session.
- The Electron shell already owns privileged APIs behind a narrow preload
  bridge and starts the local ACP bridge.
- The current terminal panel is only an input that sends a chat prompt to ACP.
  The ACP client also has an old, disabled shell-command handler. Neither is
  suitable as a real terminal or an agent-execution boundary.
- Eva already has browser-agent status and artifact handling that can become
  run attachments.

The critical missing model is a durable association among project, checkout,
agent run, terminal, and chat session. Filling that gap first prevents the UI
from becoming another collection of disconnected panels.

## Goals

1. Give a user a fast, keyboard-first terminal that is genuinely interactive:
   PTY streaming, resize, ANSI colors, tabs, search, copy/paste, command
   history, and reconnect after renderer refresh.
2. Make a project and an isolated checkout first-class. A child coding agent
   works in its own Git worktree by default, so simultaneous runs do not fight
   over one working tree.
3. Make a coding run independently navigable from Chat Sessions, Agent
   Operations, Terminal tabs, Browser runs, files, and review screens.
4. Let a parent delegate to child agents with distinct goals, models, bounded
   ACP context, and explicit workspace permissions. Their terminal activity,
   diffs, tests, and final reports remain inspectable.
5. Present safe review and handoff flows: inspect diff, test evidence, commit,
   merge or export patch, discard, and cleanup. Eva must never silently merge
   an agent branch into a user branch.
6. Add a distinctive ASCII visual layer without making the terminal less
   readable, usable, or accessible.

## Non-goals for the first release

- A cloud-hosted arbitrary-code sandbox or multi-user collaboration service.
- Replacing the Copilot CLI, its model selection, or its authentication.
- Implicit execution of shell commands on behalf of an agent.
- Automatic push, pull-request creation, merge, credential forwarding, or
  committing without an explicit user action.
- Docker/container execution in the first cut. The execution-broker interface
  must allow it later, but local Git worktrees are the initial runtime.
- A visual clone of GitHub App or Amp. The useful primitives are worktrees,
  durable runs, and navigation; Eva's layout and capabilities remain its own.

## Domain model

Store these records in a bridge-owned SQLite database under `EVA_CONFIG_DIR`.
The renderer may cache summaries, but the bridge is the source of truth so
records survive browser storage clearing and packaged-app restarts.

| Record | Responsibility | Key relationships |
| --- | --- | --- |
| `Project` | A user-approved, canonical local Git root and display metadata. | Has `Checkout` records. |
| `Checkout` | A source working tree or an Eva-managed Git worktree. Includes branch, base revision, path, lifecycle, and dirty-state summary. | Belongs to `Project`; assigned to `CodingRun`. |
| `CodingRun` | The durable unit of a coding task. Holds objective, status, timestamps, source `session_id`, model policy, and final disposition. | Uses one primary `Checkout`; has `AgentRun`, `TerminalSession`, and attachments. |
| `AgentRun` | A parent or child execution with its own ACP conversation key, checkout, capability policy, prompt digest, status, report, and parent link. | Belongs to `CodingRun`; may parent other `AgentRun` records. |
| `TerminalSession` | A named interactive PTY attached to a checkout and optionally an agent/run. Persists metadata and scrollback checkpoints, not inherited secrets. | Belongs to `Checkout` and optionally `CodingRun`/`AgentRun`. |
| `RunAttachment` | A browser run, artifact, diff snapshot, test result, or review note associated with a run. | Belongs to `CodingRun` or `AgentRun`. |
| `Approval` | An append-only decision for access, command execution, network, writes, commit, and merge. | References a run, agent, or terminal action. |

Use opaque UUIDs for new IDs. Preserve the current short `sess_*` chat IDs as
external links; do not migrate or reinterpret old IndexedDB snapshots.

Recommended defaults:

- A new coding run starts from a user-selected project and base branch.
- Eva creates `eva/run-<short-id>` in a new worktree below a managed runtime
  root, never inside the source repository's working tree.
- A child receives another worktree and branch unless the user explicitly
  chooses collaborative/shared-checkout mode.
- A run may link many chat sessions, but one `primary_session_id` is used for
  direct navigation from the existing session explorer.
- Completed worktrees are retained until the user discards or archives the
  run. Cleanup must refuse when local changes are present unless confirmed.

## Architecture

### 1. Execution broker

Introduce an Electron-main-process execution broker. It owns local PTYs,
process groups, filesystem picker results, and worktree operations. Renderer
code never receives arbitrary `child_process` access; it invokes a fixed IPC
contract through `preload.js`.

Use maintained, Electron-compatible versions of `node-pty` for PTY creation
and `@xterm/xterm` plus its fit, search, and web-links addons for rendering.
Keep Eva framework-free: the terminal view can be a small vanilla JavaScript
module that mounts xterm into the existing DOM. Build and package native
dependencies in the Electron release pipeline for Linux, macOS, and Windows.

The broker API should include only structured operations:

```text
project.select | project.inspect | checkout.create | checkout.status
checkout.diff  | checkout.commit | checkout.dispose
terminal.create | terminal.write | terminal.resize | terminal.close | terminal.replay
run.open | run.list | run.approve | run.cancel | run.finalize
```

Events flow from main process to renderer as `terminal:data`, `terminal:exit`,
`run:changed`, and `approval:requested`. Every request includes an opaque
record ID, never a renderer-provided arbitrary filesystem path after project
selection. The broker resolves canonical paths, rejects traversal, and keeps
process groups available for reliable cancellation.

### 2. Workspace service

Add a bridge module such as `tools/bridge/workspaces.py` with repository
metadata, run persistence, Git validation, diff/test summaries, cleanup, and
the HTTP endpoints needed by the regular browser UI. It must not invoke an
unbounded shell string. Git calls use argument arrays with `cwd` restricted to
a registered canonical project or worktree root.

Electron supplies privileged operations through a capability-token-authenticated
local endpoint or explicit IPC forwarding. The static browser variant uses the
same records and REST endpoints but can only access a bridge process configured
with an approved local workspace root. The Electron implementation remains the
reference experience.

### 3. Agent execution

Replace the current in-memory-only subagent task shape with an adapter backed
by `AgentRun` records. Existing `/v1/subagent/*` endpoints can keep their
response shape during migration, adding `run_id`, `workspace_id`,
`checkout_id`, and `parent_agent_id` fields.

Each agent gets:

- a unique ACP conversation key, such as `agent:<agent-run-id>`, so context is
  never shared accidentally with its parent or sibling;
- an ACP client started with its assigned worktree as `cwd`, or a pool key that
  includes the resolved worktree; and
- an execution policy: `read_only`, `ask_before_write`, `workspace_write`, or
  `workspace_write_with_network_approval`.

Do not simply enable the old ACP `terminal: true` capability. Its existing
implementation builds a shell string, trusts caller-provided cwd/environment,
and has a fixed timeout. Replace it with an ACP terminal adapter over the
execution broker only after the broker has command validation, streamed output,
worktree confinement, cancellation, approval, and audit support. During the
first coding-agent release, agents use brokered non-interactive commands;
interactive user terminals remain separate.

### 4. Permissions and audit

The user terminal is a direct local shell in its selected checkout. Agent
operations are not. Before an agent can write, execute a destructive command,
access network tools, use a credential-bearing environment variable, commit,
or merge, Eva evaluates the run policy and creates an approval record when
needed.

Minimum policy behavior:

| Action | Default | Evidence retained |
| --- | --- | --- |
| Read Git status/diff and repository files | Allow | Command/result summary |
| Build/test in assigned worktree | Allow once run starts | Command, exit code, bounded output |
| Write files in assigned worktree | Ask | Changed paths and approval |
| Network/package install | Ask | Command, host/category, approval |
| Git commit | Ask | Diff/stat, proposed message |
| Push, PR, merge, delete worktree with changes | Always ask | Explicit user decision |
| Outside assigned root or secret-bearing environment | Deny | Audit denial |

Command output, prompts, and diffs may contain sensitive data. Bound retained
output, redact known bridge credentials, do not forward `EVA_BRIDGE_TOKEN`, and
do not record raw terminal input marked secret. Approval state and audit
metadata must be viewable without exposing secrets.

### 5. Renderer experience

Replace the current Terminal side panel with a workspace view that can also
open as a focused terminal. The primary layout is a practical three-pane work
surface:

- Left: project/checkouts and a unified explorer of chats, coding runs, and
  agent runs. Entries retain status, branch, modified count, and last activity.
- Center: the selected context, usually a primary conversation, diff/review,
  or browser observation. Switching runs changes context rather than destroying
  it.
- Bottom or right dock: resizable terminal tabs. Each tab shows checkout and
  run badges, connection/exit state, search, and a clear visual distinction
  between user and agent transcript tabs.

Selecting an old ordinary chat continues to behave exactly as it does today.
Selecting a coding run restores its linked chat session, worktree, open
terminal tabs, agent tree, review state, and attachments. Agent Operations
becomes a filtered view over the same durable records, not a separate
dashboard with a separate lifecycle.

Add a Browser dock in a later UI milestone by attaching existing browser-agent
runs to `RunAttachment`. It should show current screenshot/status, permission
requests, and captured evidence; it must not silently grant browser automation
to child agents.

### 6. ASCII interactive visual layer

The terminal gets an optional, compact "Eva Field" canvas in its header or
empty state, never over terminal text. Render it with a fixed cell grid and
ASCII glyphs, not SVG. It is a decorative interaction surface with strict
layout bounds and no influence over command input.

Behavior:

- Pointer movement can gently deform a floating ASCII orb/field.
- Clicking a cell selects it; a small swatch control changes that cell's color
  from the terminal theme palette. Dragging paints cells.
- Five clicks on the same cell within a short, documented window trigger a
  local visual easter egg, such as a temporary constellation pattern and a
  single status-line acknowledgement. It runs no command and sends no data.
- State is per user/theme in localStorage, with a reset control.
- `prefers-reduced-motion`, keyboard operation, contrast, and a no-animation
  fallback are mandatory. Cap canvas work to animation frames and pause when
  hidden.

The visual treatment should feel like Eva, but the terminal remains a terminal:
copyable text, predictable focus, selection, and performance win every tie.

## Delivery sequence

### Phase 0: contracts and safety spike

Define JSON schemas and storage migrations for the records above. Add a
feature flag, `eva_workspace_terminal_v1`, disabled by default. Validate
`node-pty` prebuild/rebuild behavior in the current Electron/AppImage pipeline
on supported operating systems. Prototype xterm mounting, PTY resize, close,
and renderer reconnect with no agent integration. In parallel, implement a
read-only run-monitor stream and persisted monitor-tab layout fixture. A monitor
may render agent status, events, selected attachments, and bounded output, but
cannot create, resize, write to, kill, or revive a PTY.

Exit criteria: a standalone-only test view can launch `/bin/sh` or the platform
default shell in a fixed approved directory, stream ANSI output, resize, kill
the process group, and never expose unrestricted spawn through preload.

### Phase 1: projects, worktrees, and durable runs

Implement the workspace SQLite schema and migrations, project picker,
canonical path checks, Git inspection, worktree creation, status/diff, and
safe cleanup. Add a simple Run Explorer with create/open/archive actions.
Link a run to its existing primary chat `session_id` but make no changes to
ordinary session persistence.

Exit criteria: two runs from one repository can have independent branches and
worktrees; closing/reopening Eva restores both records; cleanup cannot remove a
dirty checkout without confirmation.

### Phase 2: production terminal

Ship the full Electron PTY broker, preload contract, xterm renderer, tab/dock
UI, scrollback checkpointing, reconnect, command/search/copy controls, and
focus shortcuts. Retire `_buildSimpleTerminal()` only behind the feature flag;
retain a clear bridge-status fallback for static usage.

Exit criteria: real interactive programs work, terminal state is correctly
attached to a checkout, exit and cancellation propagate reliably, and no
terminal regression breaks normal chat sessions.

### Monitoring invariant

Persisted tabs and split views are appropriate from Phase 0 when they are
observation-only. They let a user silently monitor several agents or runs at
once, restore that monitoring layout after a reload, and move between views
without altering any agent's execution state.

An observation surface consumes the durable run event stream and attachment
references. It has no terminal broker write capability. A user explicitly
opens an interactive terminal surface when they intend to send input or take
ownership of a PTY. The renderer must clearly label monitor and interactive
surfaces, while allowing both to appear in the same workspace layout.

Automated coverage must prove that:

- Opening, closing, duplicating, splitting, or restoring monitor tabs never
  creates a PTY, sends terminal input, changes PTY dimensions, or restarts an
  agent.
- A hidden monitor continues to receive bounded run-status updates and marks
  its view stale/reconnects cleanly after bridge or renderer recovery.
- Multiple monitor tabs can observe one agent without duplicate polling or
  duplicate event delivery; one shared subscription fans out to every view.
- Restored layouts preserve stable `run_id`/`agent_run_id` content identities
  and degrade to an unavailable state when the underlying run is gone, rather
  than recreating it.
- An interactive terminal tab can be opened only through an explicit user
  action and receives its own permission and lifecycle checks.

### Phase 3: coding agents and child runs

Create `CodingRun` and `AgentRun` orchestration. Migrate existing subagent
status/steering APIs through a compatibility adapter. Give agents isolated
worktrees, ACP conversation keys, approval policy, brokered command execution,
and event/audit streams. Add spawn controls for research, implementation,
tests, and review roles; a reviewer sees the implementation checkout as
read-only by default.

Exit criteria: a parent can spawn two sibling agents against one project,
navigate from each result to its worktree/session/terminal/audit, and obtain a
reviewable diff without either agent modifying the parent's checkout.

### Phase 4: review, browser evidence, and handoff

Add a durable diff/review screen, changed-file navigation, test evidence,
artifact links, browser-run attachment, commit proposal, patch export, and
explicit merge or discard. Add run search, filters, and branch/worktree health
indicators to the explorer.

Exit criteria: a completed child run can be reviewed, committed, exported as a
patch, merged after confirmation, or discarded; every outcome remains visible
in the run history.

### Phase 5: Eva Field and polish

Add the optional ASCII Field, interaction persistence, reduced-motion mode,
theme integration, and the five-click visual easter egg. Perform desktop and
mobile layout checks for the broader workspace UI, including text overflow and
terminal docking behavior.

Exit criteria: the visual layer remains under its rendering budget, can be
disabled, does not capture terminal focus, and behaves predictably with mouse,
keyboard, and reduced-motion settings.

## Validation plan

Add focused coverage as each phase lands:

- Python tests for schema migration, worktree path validation, dirty cleanup,
  branch collision handling, lifecycle transitions, and audit redaction.
- Node/Electron tests for IPC allowlists, canonical project confinement, PTY
  process-group cancellation, resize, reconnect, and no credential leakage.
- Existing bridge tests expanded for one ACP conversation per agent/worktree,
  run cancellation, steering, and backwards-compatible `/v1/subagent/*`
  payloads.
- Browser-level UI tests for session-to-run navigation, terminal tabs,
  approval prompts, diff review, and agent status updates.
- Manual packaged-AppImage smoke tests on Linux first, then macOS and Windows:
  shell launch, Ctrl+C, copy/paste, worktree cleanup, an agent write approval,
  and a rejected network/merge action.
- Accessibility and performance checks for keyboard focus, screen-reader
  labels, color contrast, reduced motion, terminal selection, and hidden-tab
  CPU use.

## First implementation slice

The smallest valuable next change is Phase 0 plus the data contract: add the
feature flag, record schema/migration, a canonical project registration API,
and a standalone Electron PTY proof of concept mounted in an isolated internal
view. It deliberately excludes worktree mutation and agent command execution.

That slice answers the highest-risk question cheaply: whether Eva's current
packaged Electron distribution can carry a secure, reliable PTY and xterm
surface across its target platforms. Once it passes, Phase 1 can build project
and worktree lifecycle on a stable boundary instead of embedding terminal
logic in chat or ACP code.

## Decisions to confirm before Phase 1

- Whether managed worktrees should live in the project Git directory via
  `git worktree add`, a user-selected location, or an Eva runtime root. The
  recommended default is Git-managed worktrees with an Eva registry and an
  optional custom location.
- Which project roots are eligible: all local Git repositories by default is
  recommended; home-directory-wide access is not.
- Whether package installs may be approved once per run or must be approved
  per command. The recommended default is once per run with a visible expiry.
- The first supported operating systems for native PTY packaging. Linux is the
  current proving ground; release-quality macOS and Windows support requires
  their own native-module CI builds.
- The exact name and visual character of the ASCII Field. Its behavior and
  safety boundary are fixed above; the artistic treatment can evolve without
  changing the execution model.

## Traycer Review: Adaptation Candidates

Reviewed: `traycerai/traycer` public repository and documentation on
2026-08-05. Traycer is MIT licensed, but Eva should adapt the underlying
patterns rather than transplant its React/Yjs/host architecture. Eva remains a
framework-free renderer with an Electron shell and Python bridge.

### Adopt

1. **Separate durable agent history from terminal scrollback.**
  Traycer persists a terminal agent's upstream provider session identifier and
  treats the PTY as an ephemeral renderer. A reopened agent restores provider
  history rather than claiming a terminal buffer is a transcript. Eva's
  `AgentRun` should store `provider_session_id`, `conversation_key`, and
  `terminal_session_id` separately. Provider history is the only source for
  an agent transcript; terminal output is bounded diagnostic evidence.

2. **Use a capability matrix for agent-to-agent operations.**
  Traycer distinguishes reference/discovery, transcript reading, and message
  delivery instead of calling all three "shared memory." Eva should expose
  the same distinctions per provider/runtime: discovery can be broad;
  transcript access depends on durable local provider history; delivery
  requires a local, active receiver with an inbox-capable adapter. Every
  dispatched message needs a correlation ID, optional reply expectation,
  timeout, and auditable delivery result. This is safer and more intelligible
  than sharing raw chat history with every child.

3. **Make terminal streams resumable and backpressure-aware.**
  Traycer's PTY contract uses a snapshot plus streamed writes, explicit
  action acknowledgements, and byte acknowledgements after xterm has parsed
  the data. Eva Phase 2 should use this shape over Electron IPC: terminal
  create/list/attach/write/resize/kill, initial bounded scrollback snapshot,
  sequence numbers, output acknowledgements, exit reason, and current cwd.
  Keep a bounded warm-session registry so switching away and back does not
  recreate a running PTY. Reattach must use the actual measured grid before
  replaying ANSI output.

4. **Treat worktree ownership and cleanup as first-class evidence.**
  Traycer records the agent owning a worktree and exposes dirty state,
  in-use status, branch position, and cleanup safety separately. Eva should
  retain the proposed `Checkout` record but add `owner_refs`, setup state,
  dirty file count, last activity, branch/base revision, and a conservative
  `safe_to_remove` result. Expensive Git/remote checks must be on-demand or
  cached; absence of proof is never evidence that deletion is safe.

5. **Unify artifacts as typed, run-scoped evidence.**
  Traycer's workspace can open chats, terminal agents, artifacts, files, and
  diffs using stable content identities. Eva's existing artifact directory,
  browser-agent captures, test output, review notes, and diff snapshots
  should all become `RunAttachment` records with a kind, source run/agent,
  timestamp, checksum or immutable revision, access policy, and renderer.
  An attachment is a reference, not automatic prompt context. Explicitly
  adding it to an agent's context preserves token and privacy boundaries.

6. **Route by declared capability and retain the rationale.**
  Traycer keeps a provider/harness catalog, model availability, reasoning
  controls, and explicit profile selection. Eva should extend its existing
  provider routing with a local `AgentRuntimeProfile`: provider/model,
  supported roles, terminal mode, transcript support, agent-inbox support,
  tools, cost/latency class, and permission policy. A selection policy can
  then choose planning, implementation, test, or review roles deliberately
  and persist the reason in each `AgentRun`, while always allowing a user
  override. Do not promise seamless cross-provider context transfer; Eva
  should summarize/export structured handoff context with provenance.

### Defer or avoid

- **Cross-device realtime collaboration and Yjs replication.** It solves a
  different product problem and would introduce identity, sync-conflict,
  encryption, and hosted-service requirements. Eva's initial boundary is one
  local desktop/bridge host.
- **A general split-pane canvas.** Traycer's tab/split system is mature but
  substantial. Eva can ship persisted, read-only monitor tabs and basic splits
  early because they are independently testable and do not control execution.
  Defer arbitrary canvas editing, interactive-terminal duplication, and other
  execution-owning layout behaviors until the PTY broker and run restoration
  are reliable.
- **Raw terminal output as shared agent context.** It is noisy, fragile, and
  can leak secrets. Share a structured final report, selected attachments,
  and an explicitly requested provider transcript instead.
- **Unlimited terminal retention.** Traycer's own registry bounds warm plain
  terminal retention. Eva must cap scrollback, session count, output bytes,
  and idle lifetimes, with agent-run evidence persisted independently.

### Plan adjustments

Phase 0's PTY proof of concept should validate the stream lifecycle above,
not merely `node-pty` launch and resize, and should prove read-only monitor-tab
restoration against a fake run stream. Phase 1 should add checkout ownership
and cleanup-proof fields. Phase 3 should introduce the capability matrix and
structured, correlated agent handoffs before autonomous agent-to-agent loops.
The workspace UI should initially use stable content IDs with monitor tabs and
basic splits; execution-owning terminal duplication and arbitrary canvas
topology remain later, separately scoped enhancements.