![Eva splash](core/img/Eva-splash.png)

# Eva AI Assistant

[Website](https://appatalks.github.io/eva-agent/) | [Documentation](README-2.md) | [Issues](https://github.com/appatalks/eva-agent/issues) | License: MIT

A voice-first AI assistant for conversation, memory, automation, and local or cloud-backed work. Eva can use voice, camera, browser, desktop, email, MCP tools, skills, and coding workspaces with optional auto approval, including GitHub device-login recovery when an authorized action needs additional access, while keeping control and configuration on your machine. Email supports governed mailbox access, internal delivery, and explicitly confirmed best-effort submission through a local mail system; SMTP acceptance is never presented as verified final delivery. Her complete native harness manifest is available when interpreting voice requests, including governed Skill creation, updates, enable/disable, deletion, and verified external links stored in active Skills; normal authorization and confirmation gates still apply.

## In Use

<table>
	<tr>
		<td width="50%"><img src="core/img/eva-screen-1.png" alt="Eva chat workspace" width="100%"></td>
		<td width="50%"><img src="core/img/eva-screen-2.png" alt="Eva voice interface" width="100%"></td>
	</tr>
	<tr>
		<td width="50%"><img src="core/img/eva-screen-3.png" alt="Eva settings workspace" width="100%"></td>
		<td width="50%"><img src="core/img/eva-screen-4.png" alt="Eva agent operations" width="100%"></td>
	</tr>
	<tr>
		<td width="50%"><img src="core/img/eva-screen-5.png" alt="Eva coding workspaces" width="100%"></td>
		<td width="50%"><img src="core/img/eva-screen-6.png" alt="Eva skills and memory" width="100%"></td>
	</tr>
</table>

## Quick install

```bash
curl -fsSL https://appatalks.github.io/eva-agent/get-eva.sh | bash
```

Then launch:

```bash
eva
```

Eva is also added to your system application menu.

Current standalone release: `Eva Standalone-5.6.4.AppImage`.

On first launch, choose **Eva (AIG)** in the model menu for the integrated experience. Eva works with local SQLite memory by default; the Memory view lets you inspect record provenance, associations, history, and corrections before records are changed or removed from recall. Explicit user-stated facts recognized by Eva's deterministic extractor are committed before acknowledgement; ordinary questions do not create facts, and mixed fact-plus-request messages continue through normal response handling. Provider keys, Copilot, and other optional capabilities are configured in Settings.

LM Studio responses show a live thinking state during long local generations. When a model returns a separate reasoning layer, Eva keeps it available in a collapsed **Thinking** section above the final answer.

Morning briefing requests refresh bounded news, location-aware weather, market news, mail, and memory sources. Dated news and market entries older than 36 hours are excluded. Market news is labeled as coverage of the most recently completed U.S. trading session, which may be the previous business day. Weather uses the location Eva has learned from explicit conversation and requires verified current conditions plus today's forecast; U.S. city/state locations prefer the National Weather Service. A briefing is marked partial when weather is unavailable.

GitHub MCP uses GitHub's hosted HTTPS service and the GitHub PAT configured in Settings > Auth; Docker is not required. Local MCP servers that do not support Eva's modern discovery extension can use standard MCP `2025-06-18` initialization.

Current stock quote requests use a verified local receipt when available: a configured loopback provider first, then the local `ticker.sh` Yahoo Finance tool, before the bounded Google Finance fallback. Eva reports unavailable quotes instead of asking a model to infer a current price.

For a source build, platform-specific packaging, Local Voices, providers, memory, MCP, or workspace setup, use the [technical documentation](README-2.md).

## Bounded document abilities

Eva ships native DOCX, PDF, PPTX, XLSX, and MCP Builder abilities. They perform
bounded local operations through the bridge, validate outputs before reporting
success, and never fall back to browser, desktop, terminal, package installation,
or network access. Office-format dependencies are optional and are checked by
`install.sh`; missing packages produce an actionable runtime receipt.

## Documentation

- [README-2.md](README-2.md): setup, providers, memory, voice, MCP, workspaces, architecture, and roadmap
- [standalone/README.md](standalone/README.md): AppImage build and runtime
- [docs/ai-development-guide.md](docs/ai-development-guide.md): focused development workflow, ownership map, and validation bundles
- [docs/frontend-ownership.md](docs/frontend-ownership.md): browser feature owners, collaborators, and classic-script migration rules
- [docs/testing-contracts.md](docs/testing-contracts.md): behavior, security, and compatibility test policy for refactors
- [docs/eva_default_skills/README.md](docs/eva_default_skills/README.md): canonical default Skills catalog and category taxonomy
- [docs/community_skills/README.md](docs/community_skills/README.md): inactive community Skills staging and review contract
- [docs/deprecation-inventory.md](docs/deprecation-inventory.md): active compatibility paths and evidence required before removal
- [docs/contracts/provider-routing.md](docs/contracts/provider-routing.md): selectable model routing and provider-mapping contract
- [Website](https://appatalks.github.io/eva-agent/): features, comparison, install guide

