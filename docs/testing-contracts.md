# Testing Contracts

Status: living contract. Last reviewed 2026-08-15 against `.github/workflows/eva-ci.yml`.

Eva uses focused contracts to preserve behavior while modules move into clearer
ownership boundaries. A test should protect a user-visible, security-sensitive,
or compatibility-sensitive outcome rather than merely reward a function living
in one historical file.

## Choose The Narrowest Useful Contract

| Change | Preferred contract |
| --- | --- |
| Pure validation or transformation | Unit-style Python or Node test with controlled inputs |
| Browser request payload or response handling | Node VM test with a mocked DOM and `fetch`/XHR |
| Bridge HTTP behavior | Local fake-provider or HTTP integration test |
| Electron IPC, PTY, workspace paths, or packaged UI | Focused Electron/workspace test |
| Security, packaging, script order, or public persisted values | Static contract check |

Run the focused contract immediately after an edit. Run
`python3 tools/tests/test_static.py` after a completed source slice. Use a
packaged Electron check when a change touches privileged IPC, UI lifecycle,
workspace confinement, or bundled resources.

Bounded skills use `python3 tools/tests/test_skills_document_ops.py` for local
fixture creation, read, validation, malformed-input, confinement, dependency,
receipt, and MCP scaffold contracts. These tests use temporary directories and
mocked imports only; they never install packages or access the network. Run
them with the managed bridge interpreter at
`~/.local/share/eva/runtime/.venv/bin/python` so the document dependencies
resolve without touching the system Python.

## The Curated CI Set

CI runs an explicit list, not test discovery. `tools/tests/` intentionally holds
more contracts than CI executes; the rest are focused checks you run locally for
the area you changed. The `eva-ci.yml` unit-test job currently runs:

```text
python3 tools/tests/test_static.py
python3 tools/tests/test_local_mcp_modern.py
python3 tools/tests/test_mcp_official_python.py
EVA_OFFICIAL_MCP_TYPESCRIPT_ROOT=/tmp/eva-mcp-typescript-v2 \
  python3 tools/tests/test_mcp_official_typescript.py
python3 tools/tests/test_openai_aig.py
python3 tools/tests/test_pr_diff_secret_scan.py
python3 tools/tests/test_acp_sessions.py
python3 tools/tests/test_acp_mail_consent.py
python3 tools/tests/test_email_policy.py
python3 tools/tests/test_email_accounts.py
python3 tools/tests/test_email_service.py
python3 tools/tests/test_email_routes.py
python3 tools/tests/test_mailbox_imap.py
python3 tools/tests/test_oauth_client.py
python3 tools/tests/test_remote_mcp.py
python3 tools/tests/test_mail_oauth.py
python3 tools/tests/test_device_flow.py
node tools/tests/test_cognition_provider.js
node tools/tests/test_provider_token_budget.js
node tools/tests/test_harness_control.js
node tools/tests/test_skills_voice_management.js
python3 tools/tests/test_skills_sqlite_latest.py
```

Earlier jobs in the same workflow additionally scan for hardcoded secrets,
validate HTML structure, syntax-check every JavaScript and Python file, verify
model-selector consistency, assert the config templates contain no real values,
and verify gitignore coverage.

Add a test to this list only when it is fast, hermetic, and network-free. Update
this section in the same change that edits the workflow.

## Local-Only Validation

Some checks are deliberately excluded from CI because they need hardware, a
live provider, a packaged build, or a maintainer decision:

| Location | Purpose | Committed |
| --- | --- | --- |
| `tools/local-tests/` | Memory-intelligence and Kusto end-to-end checks kept out of CI at the maintainer's request | Yes |
| `tools/tests/local/` | Ad hoc regression scripts for the current investigation | No, ignored |
| `tools/tests/test_workspace_electron_e2e.js`, `test_terminal_e2e.js` | Packaged Electron and PTY lifecycle | Yes, run manually |
| `tools/tests/test_protected_memory.py` | Vault behavior; hardware provider paths need an enrolled key | Yes, run manually |
| `tools/eval/run.py` | Offline response evaluation against recorded fixtures | Yes, run manually |

Promote a local regression into the committed suite only when the user asks for
a CI contract. Never make the application package test files.

## Static Tests Are Intentional In These Cases

Keep source/static assertions when they protect:

- secrets and generated-data exclusion;
- private bridge and workspace capability boundaries;
- script ordering for classic globals;
- packaged-resource declarations;
- stable selector values and compatible routing surfaces; or
- a narrowly documented implementation boundary where no practical runtime
  contract can prevent a security regression.

When ownership changes, update a static assertion to inspect the new owner. Do
not weaken the invariant just to make a move pass.

## Replace Brittle Location Checks Carefully

A check such as "this string exists in this file" should become a behavior
contract only when the replacement can fail for the same harmful regression.
For example:

- Model routing is protected by selector classification, sender dispatch, and
  browser/bridge mapping agreement.
- Goals, Cron, Runtime Settings, and Skill auto-learning use mocked DOM/bridge
  contracts to protect payloads and user-facing validation.
- AIG request normalization uses pure validation tests plus fake-provider HTTP
  integration coverage.

Do not delete a static check until its replacement has been run successfully in
the same change. Record the old invariant and its replacement validation in the
change description or the tracking issue for the effort.

## Refactor Completion

Before calling a refactor slice complete:

1. Name the preserved behavior and owning module.
2. Run the focused test that could disprove the change.
3. Run the static suite and any required integration or packaged test.
4. Perform a rubber-duck review of load order, public globals, stored values,
   request paths, permission checks, and error behavior.
5. Record commands, results, and residual risk where the work is reviewable.
