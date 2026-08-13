# Provider Routing Contract

This contract documents the current model-selection behavior. It exists so
model additions and routing refactors preserve saved selections and provider
request behavior. The executable companion is `tools/tests/test_model_catalog.js`.

## Selector Values

The `#selModel` values in `index.html` are public persisted values. Do not rename
or remove a value without an explicit migration for existing local settings.

| Selector category | Values | Sender |
| --- | --- | --- |
| OpenAI direct | `gpt-4o`, `gpt-4o-mini`, `o1`, `o1-preview`, `o1-mini`, `o3-mini`, `latest` | `trboSend()` |
| GitHub Models | `copilot-*`, except `copilot-acp` | `copilotSend()` in GitHub Models mode |
| Copilot ACP | `copilot-acp` | `copilotSend()` in ACP bridge mode |
| Eva AIG | `aig` | `aigSend()` |
| Gemini compatibility | `gemini` | `geminiSend()` |
| Local inference | `lm-studio` | `lmsSend()` |
| Image generation | `dall-e-3` | `dalle3Send()` |

`core/js/model-routing.js` classifies browser selector values once, and both
`updateButton()` and `sendData()` in `core/js/options.js` consume that result.
A selector value must never reach the Invalid Model branch in normal use.

`gpt-5-mini` remains accepted as a legacy persisted direct-OpenAI value even
though it is not currently displayed in `#selModel`. Do not remove its route
without an explicit saved-setting migration.

`core/js/settings/model-settings.js` owns model parameter controls, AIG model
metadata, reasoning and temperature visibility, and theme-specific selector
filtering. It preserves the existing global helper names while the classic
script migration remains in progress.

## GitHub Models Mapping

GitHub Models selector values remove the `copilot-` prefix before mapping to a
publisher/model API identifier. The current mappings are intentionally present
in both places below because the browser direct-provider route and AIG bridge
route run in different processes:

- `core/js/providers/copilot.js` maps direct GitHub Models requests.
- `tools/bridge/core.py` maps AIG's GitHub Models-compatible responder route.

For every selectable direct GitHub Models value, both mappings must agree on the
publisher/model identifier. The model-catalog test enforces that subset. AIG may
support additional backend models that are not standalone selector values.

## Safe Change Sequence

1. Decide whether the model is direct OpenAI, direct GitHub Models, Copilot ACP,
   local, image-only, or an AIG backend.
2. Add the selector entry with a stable persisted value and accurate label.
3. Update the owning sender route and parameter policy.
4. Update the direct GitHub Models mapping in both processes when applicable.
5. Update model settings metadata and user-facing documentation.
6. Run `node tools/tests/test_model_catalog.js` and
   `python3 tools/tests/test_static.py`.
7. Manually verify a request through the changed route before packaging a
   release.

## Planned Direction

The current contract is deliberately test-backed before metadata is centralized.
The next implementation step may introduce one declarative catalog, but it must
retain the values and payload behavior described above while keeping browser and
bridge process boundaries explicit.