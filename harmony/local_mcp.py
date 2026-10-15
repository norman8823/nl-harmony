"""Native Codex tool adapter for the existing loopback Companion; no SDK or daemon.

The adapter runs as Codex's configured stdio tool, so intake does not need to
write into another repository from inside the agent's filesystem sandbox.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

from . import __version__
from .codex_bridge import active_companions


class CompanionRefused(ValueError):
    """An actionable error from the selected local Companion."""


def tool_definitions():
    board = {"type": "string", "description": "Companion instance from list_boards; omit when only one board is running"}
    task = {"type": "integer", "minimum": 1}
    revision = {"type": "integer", "minimum": 1, "description": "Revision returned by start_task/list_tasks; refresh on conflict"}
    return [
        {"name": "list_boards", "description": "Find running Harmony boards on this computer. Never selects an arbitrary board.",
         "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
        {"name": "list_tasks", "description": "Inspect existing task scopes before creating a card; includes completed/archived tasks.",
         "inputSchema": {"type": "object", "properties": {"board_instance": board}, "additionalProperties": False}},
        {"name": "start_task", "description": "Continue a known task_id, or create only a genuinely new problem after checking existing tasks. "
         "Multiple prompts, turns and runs reuse the existing card. Grants no execution permission.",
         "inputSchema": {"type": "object", "additionalProperties": False,
                         "properties": {"board_instance": board, "task_id": {"type": "integer", "minimum": 1},
                                        "title": {"type": "string", "minLength": 3, "maxLength": 200},
                                        "details": {"type": "string", "maxLength": 4000},
                                        "external_id": {"type": "string", "minLength": 1, "maxLength": 500}}}},
        {"name": "update_task", "description": "Update an existing card's progress: In Progress when work begins, "
         "Blocked when waiting, Next when deferred. Does not grant execution or verify completion.",
         "inputSchema": {"type": "object", "additionalProperties": False,
                         "required": ["task_id", "expected_revision", "stage"],
                         "properties": {"board_instance": board, "task_id": task, "expected_revision": revision,
                                        "stage": {"type": "string", "enum": ["Next", "In Progress", "Blocked"]},
                                        "next_action": {"type": "string", "maxLength": 2000},
                                        "notes": {"type": "string", "maxLength": 4000}}}},
        {"name": "complete_task", "description": "Move completed work to Done with a factual summary. "
         "Refuses unresolved blockers, gates or incomplete graphs. Records Completed by Codex with your summary, "
         "without inventing review, release approval or test evidence. Do not use just because a turn/run ended.",
         "inputSchema": {"type": "object", "additionalProperties": False,
                         "required": ["task_id", "expected_revision", "summary"],
                         "properties": {"board_instance": board, "task_id": task, "expected_revision": revision,
                                        "summary": {"type": "string", "minLength": 1, "maxLength": 500}}}},
    ]


def call_tool(name: str, arguments: dict):
    definitions = {tool["name"]: tool for tool in tool_definitions()}
    if name not in definitions or not isinstance(arguments, dict):
        raise ValueError("unknown Harmony tool or invalid arguments")
    if set(arguments) - set(definitions[name]["inputSchema"]["properties"]):
        raise ValueError("unsupported Harmony tool arguments")
    if any(field not in arguments for field in definitions[name]["inputSchema"].get("required", [])):
        raise ValueError("required Harmony tool arguments are missing")
    boards = active_companions()
    if name == "list_boards":
        return {"boards": [{"instance": b["instance"], "project": b["project"]} for b in boards]}
    instance = arguments.get("board_instance")
    selected = [b for b in boards if b["instance"] == instance] if instance is not None else boards
    if len(selected) != 1:
        raise ValueError("No unique running Companion selected. Use list_boards; ask which board if several are running.")
    board = selected[0]
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    base = f"http://127.0.0.1:{board['port']}"
    method = "GET"
    if name == "list_tasks":
        path, body = "/api/board", None
    elif name in ("update_task", "complete_task"):
        task_id = arguments["task_id"]
        if type(task_id) is not int or task_id <= 0:
            raise ValueError("task_id must be a positive integer")
        data = {k: value for k, value in arguments.items() if k not in ("board_instance", "task_id")}
        if name == "complete_task":
            data["note"] = data.pop("summary")
            path, method = f"/api/tasks/{task_id}/complete", "POST"
        else:
            if data["stage"] not in ("Next", "In Progress", "Blocked"):
                raise ValueError("use complete_task for guarded completion")
            path, method = f"/api/tasks/{task_id}", "PATCH"
        body = json.dumps(data).encode()
    else:
        data = {k: value for k, value in arguments.items() if k != "board_instance"}
        data["source"] = "codex"
        path, body, method = "/api/tasks/start", json.dumps(data).encode(), "POST"
    request = urllib.request.Request(base + path, data=body, method=method, headers={"Content-Type": "application/json"})
    try:
        with opener.open(request, timeout=5) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            error = json.loads(exc.read(16384)).get("error")
        except (ValueError, AttributeError):
            error = None
        detail = error[:1000] if isinstance(error, str) else "request refused"
        raise CompanionRefused(f"Companion HTTP {exc.code}: {detail}. Refresh the task before retrying; "
                               "do not create a replacement card.") from None
    if name == "list_tasks":
        return {"board_instance": board["instance"], "project": board["project"], "tasks": [
            {"task_id": c["id"], "title": c["ticket_title"], "status": c["status"],
             "details": c.get("details"), "archived": c["board"]["archived"],
             "stage": c["board"]["stage"], "revision": c["board"]["revision"],
             "next_action": c["board"]["next_action"], "blocker": c.get("blocker_reason"),
             "approvals": c.get("approvals"), "gate": c["board"].get("gate"),
             "completion": c.get("completion"), "auto_done": c.get("auto_done")} for c in payload["cards"]]}
    card = payload["card"]
    result = {"board_instance": board["instance"], "task_id": card["id"],
              "title": card["ticket_title"], "status": card["status"],
              "stage": card["board"]["stage"], "revision": card["board"]["revision"],
              "completion": card.get("completion")}
    if "created" in payload:
        result["created"] = payload["created"]
    return result


def dispatch(message: dict):
    if not isinstance(message, dict) or "id" not in message:
        return None
    request_id = message["id"]
    method, params = message.get("method"), message.get("params", {})
    result = None
    if not isinstance(params, dict):
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": "invalid parameters"}}
    if method == "initialize":
        version = params.get("protocolVersion")
        if version not in ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"):
            version = "2024-11-05"
        result = {"protocolVersion": version, "capabilities": {"tools": {}},
                  "serverInfo": {"name": "harmony-local", "version": __version__}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": tool_definitions()}
    elif method == "tools/call":
        try:
            payload = call_tool(params.get("name"), params.get("arguments", {}))
            result = {"content": [{"type": "text", "text": json.dumps(payload)}], "isError": False}
        except CompanionRefused as exc:
            result = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        except (OSError, ValueError, KeyError, TypeError):
            result = {"content": [{"type": "text", "text": "Harmony intake failed: no unique reachable board, "
                       "invalid input, stale revision or unmet completion gate. Refresh list_tasks and inspect the task's blockers/approvals; "
                       "do not create a local fallback or claim success."}], "isError": True}
    else:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "unknown method"}}
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def main():
    while True:
        line = sys.stdin.readline(1024 * 1024 + 1)
        if not line:
            return 0
        try:
            if len(line) > 1024 * 1024:
                return 1
            response = dispatch(json.loads(line))
        except ValueError:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "invalid JSON"}}
        if response is not None:
            print(json.dumps(response), flush=True)


if __name__ == "__main__":
    sys.exit(main())
