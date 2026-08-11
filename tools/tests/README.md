# Eva Validation Suite

`tools/tests/` contains development and CI validation scripts. It is separate
from production bridge and utility code under `tools/` so application work does
not need to load test-only fixtures, fake servers, or end-to-end harnesses.

## Usage

Run a focused script from the repository root:

```sh
python3 tools/tests/test_static.py
node tools/tests/test_request_routing.js
```

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
