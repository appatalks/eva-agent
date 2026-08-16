# Eva Documentation Index

These documents are living records, not archives. Each one states what Eva is
today, what remains planned, and which validation proves it. Read the smallest
document that owns the change you are making.

For repository-wide setup and architecture, start at [README.md](../README.md)
and [README-2.md](../README-2.md). This folder covers development contracts,
ownership boundaries, and multi-phase plans.

## Document Types

| Type | Meaning | Update trigger |
| --- | --- | --- |
| Guide | How to work in this repository | Working rules, boundaries, or validation habits change |
| Ownership map | Which module owns a behavior | A module is added, moved, split, or renamed |
| Contract | Behavior that must survive refactors | The described behavior or its executable test changes |
| Inventory | Compatibility paths that look removable but are not | A path gains or loses active callers |
| Plan | Multi-phase work with an implementation record | A phase slice lands, or a phase is re-scoped |

## Documents

| Document | Type | Covers |
| --- | --- | --- |
| [ai-development-guide.md](ai-development-guide.md) | Guide | Working rules, context boundaries, task bundles, ownership map, completion checklist |
| [frontend-ownership.md](frontend-ownership.md) | Ownership map | Browser module ownership, collaborators, and focused contracts |
| [testing-contracts.md](testing-contracts.md) | Contract | How to choose a validation, the curated CI set, and static-test policy |
| [contracts/provider-routing.md](contracts/provider-routing.md) | Contract | Selector values, sender routes, and GitHub Models mapping parity |
| [contracts/aig-request-lifecycle.md](contracts/aig-request-lifecycle.md) | Contract | Ownership split between the AIG handler, request normalization, and preflight planning |
| [deprecation-inventory.md](deprecation-inventory.md) | Inventory | Compatibility paths and the evidence required before removal |
| [eva-memory-intelligence-plan.md](eva-memory-intelligence-plan.md) | Plan | Memory trust boundary, layered memory model, and learning lifecycle |
| [eva-workspaces-terminal-plan.md](eva-workspaces-terminal-plan.md) | Plan | Coding workspaces, PTY broker, agent runs, and review/handoff |
| [protected-memory-plan.md](protected-memory-plan.md) | Plan | YubiKey-gated encrypted vault, protected artifacts, and release policy |
| [eva_default_skills/README.md](eva_default_skills/README.md) | Reference | Default Skills manifest and its generated Kusto seed |
| [community_skills/README.md](community_skills/README.md) | Reference | Community Skill submission expectations |

## Maintenance Rules

1. **Update the document in the same change as the behavior.** A slice is not
   complete while an ownership map, contract, or plan still describes the
   previous behavior.
2. **Record implementation status inside the plan.** A plan keeps its phases,
   but each phase states Delivered, Partial, or Planned with the shipped
   behavior named. Do not delete a phase when it lands; it becomes the
   implementation record.
3. **State the evidence, not the intent.** Write the module that owns the
   behavior and the command that proves it. Avoid claiming a capability that
   has no validation in this repository.
4. **Keep a Status and Last reviewed line at the top of every plan.** Refresh
   the date whenever the plan is re-read against the code, even if nothing
   changed.
5. **Do not document secrets, hosts, tokens, or user-specific paths.** Use
   placeholders. This applies to examples and troubleshooting notes.
6. **Prefer editing an existing document.** Add a new file only when a durable
   boundary has no owner here.
7. **When a contract loses its executable test, say so.** An unenforced
   contract is a claim, and the document must label it as one.

## Review Cadence

Refresh this folder when any of the following happens:

- a release is packaged;
- a module is added, moved, or removed under `core/js/`, `tools/bridge/`,
  `tools/skills/`, or `standalone/`;
- the curated CI test list changes;
- a plan phase reaches an exit criterion; or
- a compatibility path in the deprecation inventory gains a migration.
