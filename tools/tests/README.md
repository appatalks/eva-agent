# Eva Validation Suite

`tools/tests/` contains development and CI validation scripts. It is separate
from production bridge and utility code under `tools/` so application work does
not need to load test-only fixtures, fake servers, or end-to-end harnesses.

## Usage

Run a focused script from the repository root:

```sh
python3 tools/tests/test_static.py
node tools/tests/test_request_routing.js
node tools/tests/test_model_catalog.js
node tools/tests/test_frontend_script_order.js
```

`test_model_catalog.js` verifies that every selectable model has an intentional
sender route and that direct GitHub Models mappings remain aligned between the
browser adapter and AIG bridge route.

`test_frontend_script_order.js` protects the ordered classic-script contracts
used while feature modules move into owned folders.

The CI workflow calls an explicit, curated set of scripts. Do not add automatic
test discovery: a validation script should run only when it is relevant to the
current change or explicitly selected by CI.

## Local Experiments

Put temporary reproductions and ad hoc regressions in `tools/tests/local/`.
That directory is ignored and must not be referenced by product code or CI.
Promote an experiment into the tracked suite only when it represents a durable
contract the project should maintain.

The packaged AppImage includes only its declared runtime resources, never this
test suite.

Use [testing contracts](../../docs/testing-contracts.md) to choose the narrowest
behavior, security, packaging, or compatibility check for a refactor.
