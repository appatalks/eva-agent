# Action-Plane WIP Checkpoint — 2026-07-12

## Status

- Branch: `wip/action-plane-containment-2026-07-12`
- Base commit: `9beb674`
- Release status: **blocked / not approved for merge or deployment**
- Installed `~/.eva` copy: unchanged at the last committed checkpoint
- Phase 3 remains explicit, default-off, and shadow-only
- Desktop authority remains allowlisted GUI launch-only

## Saved Work

- Added the `eva.action-run/1` contract with typed outcomes and causal success proof.
- Added one-use, HMAC-bound native launch authorization and one-use effect gates.
- Added browser DNS-pinned public egress, exact target fingerprints, and exact handle execution.
- Added desktop launch-only containment and run-scoped process-spawn evidence.
- Added strict launch-spec canonicalization across Electron and Python.
- Added fail-closed natural-language approval classification.
- Removed tracked/automatic Playwright and `computer-use-linux` MCP routes.
- Added an exact release MCP allowlist and process-boundary revalidation.
- Added ACP isolation, per-server MCP environments, strict Kusto origins, and disabled arbitrary `web_fetch`.
- Removed tracked runtime `.data` artifacts and excluded them from packaging.
- Added deterministic action-plane tests and CI wiring.

## Last Validated State

The latest independent review reported all eight suites green:

- Static: 299 checks
- Phase 0: 119 tests
- Phase 1: 134 tests
- Phase 2: 566 checks
- Phase 2 runtime: 33 tests
- Phase 2 consolidation: 61 tests
- Phase 3: 57 tests
- Action plane: 69 tests
- Total: 1,338 checks

Focused Python compilation, Ruff checks, JavaScript syntax checks, diff hygiene,
the native Copilot CLI flag preflight, a clean AppImage build, and package
inspection also passed during the latest review.

## Current Release Blockers

The final independent review still returned **BLOCKED**. Resume with these in
order:

1. **Separate HTTP authentication from launch authority.**
   Electron currently uses the bridge HTTP bearer as the launch-capability HMAC
   authority. Introduce a distinct per-process launch secret and prove that a
   bearer-signed launch token fails.
2. **Eliminate model-text HTML execution.**
   `renderEvaResponse()` can preserve model-controlled HTML fragments while the
   renderer permits inline handlers. Build trusted capability UI only from a
   separate structured channel and safe DOM APIs; escape all model text.
3. **Use one canonical representation for approval display and hashing.**
   Normalize the frozen action and binding once, then display, hash, revalidate,
   and execute those same canonical objects/bytes. Add composed/decomposed
   Unicode tests.
4. **Fail closed on every malformed ACP handshake response.**
   Validate exact response types and protocol/session fields, and wrap the whole
   post-spawn handshake in cleanup-on-any-exception logic.
5. **Move Kusto origin validation into the Kusto MCP allowlist branch.**
   Invalid Kusto origins are currently accepted at configuration time because
   validation is misplaced. Reject before persistence, `Popen`, or ACP
   `session/new`; reject unknown Kusto environment fields.
6. **Isolate OpenAI vision executor HTTP transport.**
   Browser and desktop executor calls still use inherited `requests` transport
   behavior. Use dedicated `trust_env = False` sessions, disable redirects, and
   verify the exact OpenAI endpoint. Sanitize the Electron-to-bridge environment
   as an additional boundary.

## Resume Procedure

1. Check out `wip/action-plane-containment-2026-07-12`.
2. Read this file and the latest reviewer verdict in session history.
3. Resolve all six blockers above without weakening existing containment.
4. Run Python compile/Ruff and JavaScript syntax checks.
5. Run all eight suites and the exact CI scanner.
6. Build a clean AppImage and inspect it for runtime data, bytecode, and removed
   MCP files.
7. Request a fresh independent adversarial review.
8. Merge, push, install, and rebuild only after an **APPROVED** verdict.

## Explicit Non-Actions at This Checkpoint

- Do not merge this branch into `main`.
- Do not push a release tag.
- Do not pull this WIP into `~/.eva`.
- Do not deploy or treat the AppImage as release-ready.