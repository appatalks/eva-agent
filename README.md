# Eva AI Assistant

[Website](https://appatalks.github.io/eva-agent/) | [Documentation](README-2.md) | [Issues](https://github.com/appatalks/eva-agent/issues) | License: MIT

Eva is a local-first AI assistant for conversation, durable memory, voice, automation,
and software work. It brings cloud and local models into one focused desktop experience
while keeping configuration, approvals, and personal data under your control.

![Eva splash](core/img/Eva-splash.png)

## Highlights

- **One intelligent gateway:** route work across OpenAI, GitHub Copilot ACP, LM Studio,
  and MCP tools without changing the conversation model.
- **GPT-6 Astra via Copilot:** select Astra through your GitHub Copilot subscription
  when enabled in Copilot CLI; no separate OpenAI API key is needed for this backend.
- **Durable, inspectable memory:** retain explicit facts locally with provenance
	linked to source conversation turns, lifecycle controls, corrections, bounded
	last-session recall, and a dedicated Memory view.
- **Real work, with approval:** use browser, desktop, email, files, skills, scheduled
  tasks, and coding workspaces through bounded native actions.
- **Verified automation:** browser and desktop agents can use the selected AIG vision
	backend, retain explicit clarifications, and report blocked work when completion is
	not visually verified.
- **Native research:** retrieve search results and bounded page excerpts through
  configured MCP tools, retain the chosen responder, and distinguish partial evidence
  from completed research instead of launching visual search loops.
- **Live daily context:** assemble weather, news, markets, mail, and memory into verified
  briefings that clearly identify unavailable sources.
- **Natural interaction:** combine text, voice, camera input, images, and optional local
	speech in the same desktop interface.
- **Local-first operation:** use SQLite and local models by default, with cloud services
  enabled only when configured.

## Quick Start

```bash
curl -fsSL https://appatalks.github.io/eva-agent/get-eva.sh | bash
eva
```

Eva is also added to the system application menu. On first launch, select **Eva (AIG)**
for the integrated routing, memory, and tool experience.

Current standalone release: `Eva Standalone-5.6.9.AppImage`.

## Preview

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

## Trust by Design

- Explicit user facts are committed before Eva acknowledges saving them; ordinary
  questions do not become memory writes.
- Consequential actions remain behind confirmation and authorization boundaries.
- Live answers use source receipts and fail visibly when current data is unavailable.
- Secrets, runtime state, and personal memory stay outside the repository and release
  artifacts.

## Documentation

- [Technical documentation](README-2.md): setup, providers, memory, voice, MCP, workspaces, architecture, and roadmap
- [standalone/README.md](standalone/README.md): AppImage build and runtime
- [docs/ai-development-guide.md](docs/ai-development-guide.md): focused development workflow, ownership map, and validation bundles
- [docs/testing-contracts.md](docs/testing-contracts.md): behavior, security, and compatibility test policy for refactors
- [docs/eva_default_skills/README.md](docs/eva_default_skills/README.md): canonical default Skills catalog and category taxonomy
- [Website](https://appatalks.github.io/eva-agent/): features, comparison, and install guide

