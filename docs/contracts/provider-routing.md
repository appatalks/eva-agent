# Provider Routing Contract

Status: living contract. Last reviewed 2026-08-22 against `index.html` in Eva
5.6.2.

Eva has one user-facing chat route: the `#selModel` selector contains only
`aig`, and `sendData()` calls `aigSend()`. Backend model selection belongs to
AIG settings and is a preference used by the bridge model policy.

## AIG Backend Policy

The `#selAIGBackend` selector contains approved ACP, direct OpenAI, and LM
Studio backend values. The response policy defaults to `auto-balanced`, which
actively selects a responder on every request from current availability and
request needs. Routine work favors Luna; bounded deep-reasoning signals such as
security, architecture, audit, comparison, and strategy prefer Sol when direct
OpenAI is available. `auto-fast` favors a low-latency local or direct route.
`pinned` is an explicit operator override for callers that must use the
preferred backend exactly.

A request that needs current data, GitHub operations, memory queries, or other
external capabilities is preflighted through ACP or local MCP before response
synthesis. The policy selects a tool-capable ACP route when tools are required.
A direct responder receives retrieved data as authoritative context and is not
asked to invent tool results. If the required ACP/local MCP route is unavailable
or returns no result, automatic mode fails closed instead of synthesizing a
model-only answer.

The bridge records both `requested_backend` and `selected_backend` in the turn
audit event, along with the policy reason. The runtime prompt receives the
requested preference, actual selected model, and routing path as authoritative
self-awareness data.

## Provider Boundaries

| Boundary | Purpose | Owner |
| --- | --- | --- |
| Eva AIG | All normal chat, automatic model choice, memory, tool preflight, and response synthesis | `core/js/providers/aig.js`, `tools/bridge/core.py` |
| Copilot ACP | Tool-capable Copilot CLI bridge, including direct compatibility callers and MCP access | `core/js/providers/copilot.js`, `tools/bridge/acp_client.py` |
| OpenAI API | AIG-selected direct responder when available and appropriate | `tools/bridge/core.py` |
| LM Studio | AIG-selected local responder and local MCP mode | `tools/bridge/core.py`, `tools/bridge/local_mcp.py` |
| GitHub MCP | GitHub repository, issue, pull request, and workflow operations | configured MCP server, not a model provider |

The GitHub PAT is retained only for GitHub MCP configuration and private
repository imports. The deprecated GitHub-hosted model endpoint, browser
client, model map, and top-level model values are removed.

Specialized image generation and camera vision remain application capabilities,
but their conversational orchestration is owned by AIG or the bridge rather
than by a standalone model selector route.

## Safe Change Sequence

1. Add or update an approved AIG backend value and its model metadata.
2. Update the bridge policy and provider-specific parameter handling.
3. Ensure tool-required request types select ACP or local MCP before synthesis.
4. Update this contract and the AIG-only model catalog test.
5. Run `node tools/tests/test_model_catalog.js` and
   `python3 tools/tests/test_static.py`.
6. Manually verify a normal chat, a live-data request, and a GitHub MCP request
   before packaging a release.

The browser and bridge may retain compatibility provider functions for internal
specialized workflows, but no normal user chat may bypass AIG.
