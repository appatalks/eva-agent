# AIG Request Lifecycle Contract

`POST /v1/aig/chat` remains owned by `BridgeHandler._aig_chat()` in
`tools/bridge/core.py`. The handler parses HTTP input, emits errors/responses,
coordinates memory, retrieval, responder execution, streaming, reflection, and
telemetry.

`tools/bridge/aig_request.py` owns only pure normalization of an already-parsed
request. It must not perform I/O or change response formatting.

## Normalization Contract

- Default backend: `gpt-5.6-luna`.
- `openai:<model>` requires an OpenAI key and preserves the existing backend
  parser validation.
- `max_completion_tokens` uses the existing `1..128000` validation.
- `acp_reasoning_effort` accepts only the bridge allowlist or an empty string.
- When `user_message` is empty, the last user message in `messages` is used.
- Empty effective user input is rejected with `No user message provided`.
- `translation_mode` and terminal planning/candidate requests are internal and
  tool-free.
- Session/conversation IDs remain trimmed to 120 characters.

The focused executable contract is `tools/tests/test_aig_request.py`.
Do not move memory assembly, ACP preflight, provider selection, streaming,
reflection, or telemetry into this module without a separate behavior contract.