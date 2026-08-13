# Testing Contracts

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
the same change. Record the old invariant and replacement validation in issue
#158 when the change is part of the modularization sprint.

## Refactor Completion

Before calling a refactor slice complete:

1. Name the preserved behavior and owning module.
2. Run the focused test that could disprove the change.
3. Run the static suite and any required integration or packaged test.
4. Perform a rubber-duck review of load order, public globals, stored values,
   request paths, permission checks, and error behavior.
5. Add a progress comment to issue #158 with commands, results, and residual
   risk.