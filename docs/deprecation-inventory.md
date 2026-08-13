# Deprecation And Compatibility Inventory

This inventory records paths that look removable but still have active callers,
migration obligations, or deployment requirements. The modularization sprint
does not remove them without usage evidence and an explicit migration.

| Path | Current evidence | Removal gate |
| --- | --- | --- |
| Gemini compatibility provider | Visible deprecated selector and Auth field; `geminiSend()`, prompt budgeting, session storage, reflection, and model monitor remain wired | Remove selector only after opt-in usage telemetry or a release deprecation window, export/migrate `geminiMessages`, remove credentials UI, and add a saved-selection migration |
| `config.local.js` file configuration | Loaded by `index.html`; documented fallback for direct `file://` use where `config.json` fetch is blocked | Remove only if direct browser/file operation is formally dropped or replaced with a synchronous, build-free local configuration path |
| Legacy session recovery | IndexedDB migration and `session_<id>` recovery reads remain active; legacy-visible restoration prevents blank historical sessions | Remove after a versioned migration-completion marker demonstrates no legacy snapshots remain across supported upgrades |
| Legacy model/settings values | `gpt-5-mini`, old cognition keys, and old theme value remain readable for saved-profile compatibility | Remove only with explicit key/value migration and contract coverage for existing profiles |
| Localhost/same-host ACP fallback | Bridge detection intentionally tries configured, same-host, then localhost; infrastructure roadmap requires localhost fallback until single-host milestone completion | Remove only after the tracked infrastructure milestone is complete and browser/standalone deployment tests cover the replacement |
| Legacy MCP protocol initialization | Local MCP supports modern and legacy server eras with process respawn isolation | Remove only after supported-server inventory and interoperability tests show no required legacy servers |

## Evidence Rules

- A source comment that says "deprecated" is not usage evidence.
- A path may be removed only when its stored data, persisted selector values,
  and deployment callers have an explicit migration or supported replacement.
- Removal work must add a focused regression for existing profiles or sessions,
  update user documentation, and record the evidence in issue #158 or its
  successor.
- Security, authorization, and fallback behavior must not be deleted merely to
  reduce file size or context tokens.