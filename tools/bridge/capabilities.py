"""Bridge-owned runtime capability registry for Eva responder awareness."""

import json
import os

from bridge import state as _st
from bridge.memory import _resolve_memory_backend


NATIVE_HARNESS_ACTIONS = {
    "navigate": {"description": "Open a native Eva surface.", "confirmation": "none"},
    "refresh": {"description": "Refresh a native Eva surface.", "confirmation": "none"},
    "describe_email": {"description": "Describe configured mailboxes.", "confirmation": "none"},
    "send_email": {"description": "Send one approved email.", "confirmation": "recipient-or-message"},
    "describe_workspaces": {"description": "Describe native workspaces.", "confirmation": "none"},
    "describe_assets": {"description": "Describe assets and workspace files.", "confirmation": "none"},
    "describe_skills": {"description": "Describe saved Skills.", "confirmation": "none"},
    "create_skill": {"description": "Create and activate a Skill.", "confirmation": "direct-user"},
    "update_skill": {"description": "Update a named Skill.", "confirmation": "direct-user"},
    "set_skill_status": {"description": "Enable or disable a Skill.", "confirmation": "direct-user"},
    "delete_skill": {"description": "Delete a Skill.", "confirmation": "direct-user"},
    "run_skill": {"description": "Run an active verified Skill.", "confirmation": "direct-user"},
    "run_bounded_skill": {"description": "Run a bounded document or MCP-builder operation.", "confirmation": "direct-user"},
    "open_external_url": {"description": "Open a verified external URL.", "confirmation": "direct-user"},
    "describe_sessions": {"description": "Describe saved sessions.", "confirmation": "none"},
    "describe_agents": {"description": "Describe active agents.", "confirmation": "none"},
    "describe_workspace_tools": {"description": "Describe workspace MCP tools.", "confirmation": "none"},
    "list_github_repositories": {"description": "List user-owned GitHub repositories.", "confirmation": "direct-user"},
    "continue_github_repositories": {"description": "Continue a GitHub repository listing.", "confirmation": "direct-user"},
    "authorize_github": {"description": "Start GitHub device authorization.", "confirmation": "direct-user"},
    "set_workspace_mcp_server": {"description": "Configure a workspace MCP server.", "confirmation": "direct-user"},
    "verify_workspace_mcp_server": {"description": "Verify a workspace MCP server.", "confirmation": "direct-user"},
    "retry_workspace_run": {"description": "Retry a workspace run.", "confirmation": "direct-user"},
    "run_workspace_check": {"description": "Run a requested workspace check.", "confirmation": "direct-user"},
    "run_repository_remediation": {"description": "Run requested repository remediation.", "confirmation": "direct-user"},
    "import_github": {"description": "Import an exact GitHub repository URL.", "confirmation": "direct-user"},
    "import_github_selection": {"description": "Import a selected GitHub repository.", "confirmation": "direct-user"},
    "describe_github_pull_request": {"description": "Inspect a GitHub pull request.", "confirmation": "direct-user"},
    "merge_github_pull_request": {"description": "Merge a verified pull request.", "confirmation": "typed-MERGE"},
    "delete_github_pull_request_branch": {"description": "Delete a merged pull request branch.", "confirmation": "direct-user"},
    "remove_workspace": {"description": "Remove a workspace.", "confirmation": "direct-user"},
    "run_terminal_command": {"description": "Run an exact terminal command.", "confirmation": "direct-user"},
    "type_terminal_command": {"description": "Type a terminal command for review.", "confirmation": "direct-user"},
    "plan_terminal_task": {"description": "Plan a direct terminal task.", "confirmation": "direct-user"},
    "consider_terminal_task": {"description": "Consider terminal applicability.", "confirmation": "direct-user"},
    "inspect_form": {"description": "Inspect a native confirmation form.", "confirmation": "none"},
    "set_field": {"description": "Set a native confirmation form field.", "confirmation": "direct-user"},
    "submit_form": {"description": "Submit a native confirmation form.", "confirmation": "direct-user"},
    "cancel_form": {"description": "Cancel a native confirmation form.", "confirmation": "direct-user"},
    "new_chat": {"description": "Start a new chat.", "confirmation": "direct-user"},
    "voice_control": {"description": "Enable or disable voice control.", "confirmation": "direct-user"},
}


def runtime_capabilities():
    """Return current bridge and native capability readiness without secrets."""
    capabilities = [
        {"id": "memory", "executor": "bridge", "status": "available", "confirmation": "none",
         "description": "Persistent " + _resolve_memory_backend() + " memory with traceable atoms."},
        {"id": "native-harness", "executor": "browser", "status": "available", "confirmation": "action-specific",
         "description": "Allowlisted Eva application controls and verified Skills."},
        {"id": "browser-agent", "executor": "bridge", "status": "available", "confirmation": "action-specific",
         "description": "Browser automation for explicit interactive requests."},
        {"id": "desktop-agent", "executor": "bridge", "status": "available", "confirmation": "action-specific",
         "description": "Desktop automation for explicit interactive requests."},
        {"id": "signal-message", "executor": "bridge",
         "status": "available" if os.environ.get("EVA_BRIDGE_TOKEN") else "needs-standalone",
         "confirmation": "explicit-user", "description": "Exactly-once local Signal delivery."},
    ]
    manager = _st.local_mcp_manager
    if manager and manager.alive:
        for server_name, server in manager.servers.items():
            if not server.alive:
                continue
            for tool in server.tools:
                tool_name = str(tool.get("name") or "").strip()
                if tool_name:
                    capabilities.append({
                        "id": "mcp:" + tool_name,
                        "executor": "local-mcp:" + server_name,
                        "status": "available",
                        "confirmation": "tool-specific",
                        "description": str(tool.get("description") or "Local MCP tool.")[:240],
                    })
    if _st.acp_client and getattr(_st.acp_client, "alive", False):
        capabilities.append({"id": "acp-tools", "executor": "copilot-acp", "status": "available",
                             "confirmation": "permission-specific", "description": "Optional ACP cloud and configured MCP tools."})
    else:
        capabilities.append({"id": "acp-tools", "executor": "copilot-acp", "status": "unavailable",
                             "confirmation": "permission-specific", "description": "Optional ACP cloud and configured MCP tools."})
    try:
        from bridge.cognition import _active_skill_rows_for_decision
        active_skills = _active_skill_rows_for_decision()
    except Exception:
        active_skills = []
    for skill in active_skills[:24]:
        skill_id = str(skill.get("SkillId") or skill.get("skillId") or "").strip()
        name = str(skill.get("Name") or skill.get("name") or "Active Skill").strip()[:120]
        if not skill_id:
            continue
        config = skill.get("Config") or skill.get("config") or {}
        if isinstance(config, str):
            try:
                config = json.loads(config)
            except json.JSONDecodeError:
                config = {}
        if not isinstance(config, dict):
            config = {}
        validation = config.get("validation") or config.get("Validation") or {}
        validation_status = validation.get("status") if isinstance(validation, dict) else validation
        prerequisites = config.get("prerequisites") or config.get("dependencies") or []
        requires_url = "url" in (str(skill.get("Instructions") or "") + str(config)).lower()
        has_approved_url = bool(config.get("approved_url") or config.get("external_url") or config.get("url"))
        is_ready = (
            str(validation_status or "").lower() in {"passed", "ready", "valid"}
            and not prerequisites
            and (not requires_url or has_approved_url)
        )
        capabilities.append({
            "id": "skill:" + skill_id,
            "executor": "verified-skill",
            "status": "available" if is_ready else "needs-validation",
            "confirmation": "direct-user",
            "description": name + (
                ". Execution requires an evidence-backed receipt."
                if is_ready else ". Requires dependency and validation readiness before execution."
            ),
            "validation": "receipt-required",
        })
    return {"version": 2, "capabilities": capabilities, "native_actions": sorted(NATIVE_HARNESS_ACTIONS)}


def runtime_capability_prompt_view():
    """Produce a bounded authoritative view for every responder prompt."""
    view = runtime_capabilities()
    available = [item for item in view["capabilities"] if item["status"] == "available"]
    lines = [
        "[Runtime Capabilities - AUTHORITATIVE]",
        "Use only capabilities listed as available below. Capability awareness never bypasses confirmation or permission gates.",
    ]
    for item in available[:24]:
        lines.append("- " + item["id"] + " via " + item["executor"] + ": " + item["description"])
    lines.append("Native Eva controls and active Skills take precedence over browser or desktop automation for Eva-owned surfaces and verified Skill URLs.")
    lines.append("When a required capability is unavailable, state that receipt plainly; do not substitute a different tool or invent a result.")
    return "\n".join(lines)