# Provider Routing Contract

Status: living contract. Last reviewed 2026-09-06 against the current Eva 5.6.9
workspace, including native research integration.

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

### Bounded native research

Explicit online research and topic-bound continuation use bridge-owned MCP search
and page retrieval, not model-generated browser markers. This route retains the
requested responder even when an automatic policy is selected: retrieval does not
require choosing an additional model or crossing a provider billing boundary.
Existing provider settings and non-research automatic routing are unchanged.

The bridge may use an active MCP manager or start only an already configured web
profile for the request. It never registers a new server or falls back to an
unconfigured model/provider. No topic or no usable sources returns an honest
clarification/unavailable response without a synthesis call. Reviewer `no_tools`
requests do not re-enter retrieval. Native research response markers cannot launch
additional actions; explicit UI interaction is a separate route.

Per-turn receipts contain source URLs, retrieval timestamps, page-vs-snippet
provenance, method attempts, and partial/unavailable status. Source text remains
untrusted data. This first slice does not provide durable research checkpoints,
cross-turn source deduplication, or a complete provider/budget grant system.

Focused validation is currently local-only under ignored `tools/tests/local/`:
`test_native_research.py`, `test_research_frontend.js`, and `test_research_aig.py`.
These checks cover safe retrieval, context/marker handling, and mocked HTTP/ACP/
LM Studio lifecycle behavior; this slice has no new curated CI contract.

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

## Capability Awareness

The bridge-owned runtime capability registry is the authoritative responder view
for native harness actions, active local MCP tools, ACP availability, memory,
and action confirmation requirements. It is exposed through the private
`/v1/runtime/capabilities` endpoint and appended to every tool-enabled AIG
responder prompt. Provider prompts may contain persona guidance, but they must
not independently claim a capability that the current registry marks
unavailable.

Specialized image generation and camera vision remain application capabilities,
but their conversational orchestration is owned by AIG or the bridge rather
than by a standalone model selector route.

Email composition is a native action, not a browser, desktop, camera, or model
provider operation. A model may prepare an exact draft only when the recipient
is explicit in the current user turn. The bridge owns the session-bound pending
draft, and a later confirmation consumes its opaque identifier exactly once;
retries return the original submission receipt instead of sending again.
For local-MTA delivery, the native email capability uses the captured Exim
queue ID and fixed read-only status endpoint to report handoff, deferral, or
failure. It must not use browser automation or claim inbox delivery from local
SMTP acceptance alone.

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
