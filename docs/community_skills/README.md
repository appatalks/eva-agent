# Community Skills Staging

This directory is an inactive staging and import contract, not a runtime skill
source. Files placed here must not be loaded, seeded, packaged as active
catalog content, or made available to cognition automatically.

Before a community skill can be promoted into `docs/eva_default_skills/`, a
maintainer must record:

- Provenance: author, original project or URL, retrieval date, and the exact
  source revision or checksum.
- License: SPDX-compatible license, attribution requirements, and confirmation
  that redistribution and modification are permitted.
- Security review: prompt-injection screening, tool and URL audit, secret and
  credential handling review, data-access boundaries, and destructive-action
  confirmation behavior.
- Functional review: category assignment, prerequisites, preferred tools,
  allowed fallbacks, bounded instructions, tests, and an explicit maintainer
  approval.

Imported material is untrusted data until it passes review. Reviewers must
normalize it into the canonical manifest schema, remove instructions that try
to alter Eva's authority or safeguards, and preserve the original provenance
outside the executable instructions. Community files do not replace or edit
the canonical default catalog by themselves.