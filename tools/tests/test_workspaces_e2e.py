#!/usr/bin/env python3
"""HTTP integration test for Eva's durable workspace bridge endpoints."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
from bridge import core as bridge

BRIDGE_TEST_TOKEN = "test-token"


def git(directory, *args):
    return subprocess.run(
        ["git", *args], cwd=directory, check=True, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout.strip()


def request(base_url, method, route, body=None, workspace_capability=True):
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + BRIDGE_TEST_TOKEN,
    }
    if workspace_capability:
        headers["X-Eva-Workspace-Capability"] = "workspace-test-capability"
    request_object = urllib.request.Request(
        base_url + route,
        data=payload,
        method=method,
        headers=headers,
    )
    try:
        with urllib.request.urlopen(request_object, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def main():
    sandbox = Path(tempfile.mkdtemp(prefix="eva-workspaces-http-"))
    previous_config_dir = bridge._cfg.EVA_CONFIG_DIR
    previous_store = bridge._st.workspace_store
    previous_worker = bridge._subagent_worker
    previous_bridge_token = os.environ.get("EVA_BRIDGE_TOKEN")
    previous_workspace_capability = os.environ.get("EVA_WORKSPACE_CAPABILITY")
    server = None
    try:
        repository = sandbox / "repository"
        repository.mkdir()
        git(repository, "init", "-b", "main")
        git(repository, "config", "user.name", "Eva Test")
        git(repository, "config", "user.email", "eva-test@example.invalid")
        (repository / "README.md").write_text("# bridge workspace test\n", encoding="utf-8")
        (repository / "mcp.json").write_text(
            json.dumps({"mcpServers": {"project-docs": {
                "command": "example-mcp", "args": ["--docs"], "env": {"EXAMPLE_TOKEN": "not-rendered"}
            }}}),
            encoding="utf-8",
        )
        git(repository, "add", "README.md", "mcp.json")
        git(repository, "commit", "-m", "Initial commit")

        bridge._cfg.EVA_CONFIG_DIR = str(sandbox / "eva-config")
        bridge._st.workspace_store = None
        bridge._st.subagent_tasks = {}
        observed_workspace_mcp_configs = []

        def fake_workspace_worker(task_id, prompt, label, model="", *unused):
            with bridge._st.subagent_lock:
                task = bridge._st.subagent_tasks[task_id]
                task["status"] = "running"
                assigned_cwd = Path(task["_cwd"])
            observed_workspace_mcp_configs.append(task.get("_workspace_mcp_config"))
            bridge._st.workspace_store.update_agent_run(task_id, "running", "Creating requested file")
            (assigned_cwd / "workspace-agent-created.txt").write_text("created by workspace agent\n", encoding="utf-8")
            with bridge._st.subagent_lock:
                task["status"] = "done"
                task["result"] = "Created workspace-agent-created.txt"
                task["ended_at"] = bridge.datetime.datetime.now(bridge.datetime.timezone.utc).isoformat()
            bridge._st.workspace_store.update_agent_run(task_id, "done", task["result"])

        bridge._subagent_worker = fake_workspace_worker
        os.environ["EVA_BRIDGE_TOKEN"] = BRIDGE_TEST_TOKEN
        os.environ["EVA_WORKSPACE_CAPABILITY"] = "workspace-test-capability"
        server = bridge.ThreadingHTTPServer(("127.0.0.1", 0), bridge.BridgeHandler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        base_url = "http://127.0.0.1:" + str(server.server_port)

        status, payload = request(base_url, "GET", "/v1/workspaces/projects", workspace_capability=False)
        assert status == 403 and "workspace authorization" in payload["error"]["message"].lower(), payload
        status, payload = request(base_url, "POST", "/v1/workspaces/eva-ready", {})
        assert status == 200 and payload["project"]["name"] == "Eva Ready Workspace", payload
        assert Path(payload["project"]["path"]).is_dir()
        status, payload = request(base_url, "POST", "/v1/workspaces/projects", {"path": str(repository)})
        assert status == 201, payload
        project = payload["project"]
        assert project["path"] == str(repository.resolve())
        assert project["mcp_servers"]["servers"] == [{
            "name": "project-docs", "transport": "stdio", "enabled": False
        }]
        assert "command" not in project["mcp_servers"]["servers"][0]
        status, payload = request(
            base_url,
            "POST",
            "/v1/workspaces/projects/" + project["id"] + "/mcp-servers/project-docs",
            {"enabled": True},
        )
        assert status == 200 and payload["project"]["mcp_servers"]["servers"][0]["enabled"] is True, payload

        swapped_run = bridge._st.workspace_store.create_run(project["id"], "Reject swapped dispatch root")
        swapped_path = Path(swapped_run["checkout"]["path"])
        shutil.rmtree(swapped_path)
        swapped_path.symlink_to(repository, target_is_directory=True)
        try:
            bridge._dispatch_workspace_run(swapped_run)
            raise AssertionError("dispatch should reject a symlink-swapped checkout")
        except bridge.WorkspaceError:
            pass
        swapped_path.unlink()
        assert bridge._st.workspace_store.discard_run(swapped_run["id"])["status"] == "discarded"

        status, payload = request(base_url, "POST", "/v1/workspaces/runs", {
            "project_id": project["id"],
            "objective": "Prove workspace endpoints",
            "primary_session_id": "sess_http",
        })
        assert status == 201, payload
        run = payload["run"]
        checkout = run["checkout"]
        assert Path(checkout["path"]).is_dir()
        deadline = time.time() + 5
        while time.time() < deadline:
            status, run_payload = request(base_url, "GET", "/v1/workspaces/runs/" + run["id"])
            if run_payload.get("run", {}).get("agent", {}).get("status") == "done":
                break
            time.sleep(0.05)
        assert run_payload["run"]["agent"]["status"] == "done", run_payload
        assert run_payload["run"]["status"] == "completed", run_payload
        assert (Path(checkout["path"]) / "workspace-agent-created.txt").is_file()
        assert observed_workspace_mcp_configs == [{
            "workspace-" + project["id"].replace("-", "")[:12] + "-project-docs": {
                "command": "example-mcp", "args": ["--docs"], "env": {"EXAMPLE_TOKEN": "not-rendered"}
            }
        }]
        status, assets_payload = request(base_url, "GET", "/v1/workspaces/assets")
        workspace_assets = [asset for asset in assets_payload.get("assets", []) if asset.get("run_id") == run["id"]]
        assert status == 200 and any(asset["relative_path"] == "workspace-agent-created.txt" for asset in workspace_assets), assets_payload
        status, resolved = request(base_url, "POST", "/v1/workspaces/assets/resolve", {
            "run_id": run["id"], "relative_path": "workspace-agent-created.txt"
        })
        assert status == 200 and resolved["path"] == str(Path(checkout["path"]) / "workspace-agent-created.txt"), resolved
        status, rejected = request(base_url, "POST", "/v1/workspaces/assets/resolve", {
            "run_id": run["id"], "relative_path": "../README.md"
        })
        assert status == 404 and "invalid" in rejected["error"]["message"].lower(), rejected
        status, overview = request(base_url, "GET", "/v1/agents/overview?include_graph=0")
        linked = [agent for agent in overview.get("agents", []) if agent.get("coding_run_id") == run["id"]]
        assert status == 200 and len(linked) == 1 and linked[0]["status"] == "done", overview

        status, payload = request(base_url, "GET", "/v1/workspaces/runs?project_id=" + project["id"])
        listed_runs = {listed["id"]: listed for listed in payload.get("runs", [])}
        assert status == 200 and listed_runs[run["id"]]["status"] == "completed", payload
        assert listed_runs[swapped_run["id"]]["status"] == "discarded", payload

        (Path(checkout["path"]) / "dirty.txt").write_text("needs confirmation\n", encoding="utf-8")
        status, payload = request(base_url, "GET", "/v1/workspaces/checkouts/" + checkout["id"] + "/status")
        assert status == 200 and payload["checkout"]["dirty_file_count"] >= 2, payload

        status, payload = request(base_url, "POST", "/v1/workspaces/runs/" + run["id"] + "/discard", {})
        assert status == 400 and "dirty" in payload["error"]["message"].lower(), payload
        status, payload = request(base_url, "POST", "/v1/workspaces/runs/" + run["id"] + "/discard", {"confirm_dirty": True})
        assert status == 200 and payload["run"]["status"] == "discarded", payload
        assert not Path(checkout["path"]).exists()

        print("workspace bridge E2E tests: PASS")
    finally:
        if server:
            server.shutdown()
        if bridge._st.workspace_store is not None:
            bridge._st.workspace_store.close()
        bridge._st.workspace_store = previous_store
        bridge._subagent_worker = previous_worker
        bridge._cfg.EVA_CONFIG_DIR = previous_config_dir
        if previous_bridge_token is None:
            os.environ.pop("EVA_BRIDGE_TOKEN", None)
        else:
            os.environ["EVA_BRIDGE_TOKEN"] = previous_bridge_token
        if previous_workspace_capability is None:
            os.environ.pop("EVA_WORKSPACE_CAPABILITY", None)
        else:
            os.environ["EVA_WORKSPACE_CAPABILITY"] = previous_workspace_capability
        shutil.rmtree(sandbox, ignore_errors=True)


if __name__ == "__main__":
    main()
