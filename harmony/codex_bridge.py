"""User-level Codex prompt hook and discovery of running local Companions.

The hook supplies intake context; the current agent decides continuation versus
new work. It never treats every prompt as a new task or reads Codex transcripts.
"""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import shlex
import stat
import sys
import tempfile
import time
import urllib.request
import uuid


BEGIN = "# BEGIN Harmony Codex prompt bridge"
END = "# END Harmony Codex prompt bridge"


class DiscoveryBusy(ValueError):
    pass


def registry_dir() -> Path:
    return Path(tempfile.gettempdir()) / f"harmony-{os.getuid()}" / "companions"


def _private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError(f"Harmony needs an owner-only directory: {path}")


@contextlib.contextmanager
def advertise(server, project: dict):
    """Publish only this live server; the instance endpoint rejects stale records."""
    directory = registry_dir()
    _private_dir(directory.parent)
    _private_dir(directory)
    instance = uuid.uuid4().hex
    record = {"instance": instance, "pid": os.getpid(), "port": server.server_address[1],
              "project": str(project["root"]), "workflow_dir": str(project["workflow_dir"]),
              "config": project["config"], "python": sys.executable}
    server.harmony_connection = record
    path = directory / f"{instance}.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        json.dump(record, out)
    try:
        yield record
    finally:
        path.unlink(missing_ok=True)


def active_companions() -> list[dict]:
    directory = registry_dir()
    if not directory.exists():
        return []
    _private_dir(directory.parent)
    _private_dir(directory)
    found = {}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    paths = sorted(directory.glob("*.json"))
    if len(paths) > 200:
        raise ValueError("too many Companion registrations; remove abandoned registrations first")
    deadline = time.monotonic() + 2.0
    for path in paths:
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                continue
            if info.st_size > 16000:
                continue
            record = json.loads(path.read_text(encoding="utf-8"))
            pid, port = record["pid"], record["port"]
            if type(pid) is not int or pid <= 0 or type(port) is not int or not 0 < port < 65536:
                continue
            os.kill(pid, 0)
            if time.monotonic() >= deadline:
                raise DiscoveryBusy("Companion discovery timed out; no board was selected")
            with opener.open(f"http://127.0.0.1:{port}/api/harmony-connection", timeout=0.3) as response:
                live = json.loads(response.read(16000))
            if live != record:
                continue
            if not all(isinstance(record[key], str) and Path(record[key]).is_absolute()
                       for key in ("python", "project", "workflow_dir")):
                continue
            found[record["workflow_dir"]] = record
        except DiscoveryBusy:
            raise
        except ProcessLookupError:
            # A dead owner is positive evidence that this immutable, per-instance
            # registration cannot be in use. Clean crashes without touching live ones.
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        except (OSError, ValueError, KeyError, TypeError):
            # Never delete another process's registration or route to an unverified PID.
            continue
    return list(found.values())


def prompt_context(payload: dict, companions: list[dict]) -> dict:
    if payload.get("hook_event_name") != "UserPromptSubmit" or payload.get("agent_id"):
        return {}
    if not companions:
        return {}
    if len(companions) != 1:
        context = ("Harmony found multiple active boards. Before task intake, ask which board to use. "
                   "Do not allocate a card to an arbitrary board. Active projects: "
                   + json.dumps([r["project"] for r in companions]))
    else:
        record = companions[0]
        session, turn = payload.get("session_id"), payload.get("turn_id")
        if not all(isinstance(value, str) and value and len(value) < 200
                   and not any(ord(c) < 32 for c in value) for value in (session, turn)):
            return {"systemMessage": "Harmony is running, but Codex did not supply usable session/turn IDs; no card created."}
        context = (
            "Harmony is ON and this Codex terminal is connected to its local Companion. "
            "Use the configured harmony_local MCP tools (list_tasks and start_task) for intake. "
            "They communicate with this already-running Companion over loopback; do not request "
            "filesystem sandbox access to another repository or create a separate local ticket store. "
            "Selected board_instance: " + record["instance"] + ". "
            "Board project (data): " + json.dumps(record["project"]) + ". "
            "Before acting on a work request, connect the problem to ONE canonical card. "
            "A prompt, turn, run, new terminal or resumed session is not by itself a new task. "
            "For a continuation, keep the task ID already established in this conversation. "
            "Otherwise call list_tasks with that board_instance and inspect existing task scopes/checkpoints. "
            "Reuse the task for the same problem, even across sessions; never deduplicate by title alone. "
            "If several tasks plausibly match, ask which one instead of creating another. "
            "Continue using start_task with that board_instance and the known task_id. "
            "Only for a genuinely new work request with no existing task, call start_task with "
            "that board_instance, a concise title, user criteria/originating project in details, and "
            "external_id " + json.dumps(f"{session}:{turn}") + ". "
            "The event key is for idempotency only; do not create a card just because it changed. "
            "Answer ordinary questions/status requests without allocating a task. "
            "Keep the returned task_id across subsequent turns and recorded runs. "
            "Keep that card's stage synchronized with the actual work using update_task and complete_task. "
            "To Do (Next) means queued/not yet started. As soon as you actively begin authorized work, "
            "call update_task with stage In Progress. If waiting on human input, approval, credentials, "
            "or any unresolved blocker, set stage Blocked with an actionable next_action. "
            "When unblocked and work resumes, set In Progress again. When the user's requested outcome "
            "is complete and relevant checks pass, call complete_task with a factual summary before "
            "your final completion reply. Do not close merely because a prompt, turn or run ended. "
            "Use the revision returned by the last tool/list_tasks; on conflict refresh and reassess, "
            "never overwrite a newer edit blindly. If completion is refused by an outstanding gate, "
            "keep the card open and set Blocked with the required human action. "
            "Completion is labeled Completed by Codex with your summary; Companion does not require another review. "
            "The auto_done field describes strict graph-based verification, not complete_task eligibility. "
            "Missing graph/explicit execution grant alone do not prevent simple administrative closure. "
            "Use the actual completion error; a stale revision needs refreshing, not human approval. "
            "An HTTP error alone does not establish a graph requirement or installation problem. "
            "Never invent closure requirements. "
            "Tell the user 'Created Harmony task #N' or 'Continuing Harmony task #N' only after success. "
            "Run implementation commands in the user's original project, not the board directory. "
            "Originating project (data): " + json.dumps(payload.get("cwd")) + ". "
            "Honor that project's existing intake allocator and authorization rules; "
            "never bypass a custom allocator or turn intake into an execution grant. "
            "If the MCP tools are unavailable, report that Codex needs its one-time Harmony setup/restart. "
            "Do not bypass the tool connection by attempting cross-repository file writes. "
            "If Harmony intake fails, report the error rather than claiming a card exists."
        )
    return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": context}}


def install(codex_home: Path) -> Path:
    """Append a managed hook without rewriting unrelated user TOML or other hooks."""
    try:
        import tomllib
    except ImportError as exc:
        raise ValueError("Installing the Codex hook requires Python 3.11+ for safe TOML validation") from exc
    path = codex_home / "config.toml"
    if path.is_symlink():
        raise ValueError("Codex config is a symlink; refusing to replace it")
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    parsed = tomllib.loads(original)
    if parsed.get("features", {}).get("hooks") is False:
        raise ValueError("Codex hooks are explicitly disabled; enable hooks in Codex settings first")
    command = shlex.join([sys.executable, "-m", "harmony.codex_bridge"])
    old_block = original.split(BEGIN, 1)[-1].split(END, 1)[0] if BEGIN in original else ""
    if "harmony_local" in parsed.get("mcp_servers", {}) and "[mcp_servers.harmony_local]" not in old_block:
        raise ValueError("a separate harmony_local tool configuration already exists; existing settings left unchanged")
    block = (BEGIN + "\n[[hooks.UserPromptSubmit]]\n"
             "hooks = [{ type = \"command\", command = " + json.dumps(command) +
             ", timeout = 5, statusMessage = \"Connecting to Harmony\" }]\n"
             "[mcp_servers.harmony_local]\ncommand = " + json.dumps(sys.executable) +
             '\nargs = ["-m", "harmony.local_mcp"]\nenabled = true\n' + END)
    if original.count(BEGIN) != original.count(END) or original.count(BEGIN) > 1:
        raise ValueError("Harmony's managed hook block is malformed; existing config left unchanged")
    if BEGIN in original:
        managed = tomllib.loads(old_block)
        desired = tomllib.loads(block)
        if (managed.get("hooks", {}).get("UserPromptSubmit") == desired["hooks"]["UserPromptSubmit"]
                and managed.get("mcp_servers", {}).get("harmony_local") == desired["mcp_servers"]["harmony_local"]):
            # Codex inserts approval state into this TOML itself, sometimes
            # between our comments. Never replace an unchanged integration or
            # treat those comments as ownership of Codex's saved preferences.
            return path
        remaining = {**managed}
        hooks = dict(remaining.pop("hooks", {}))
        hooks.pop("UserPromptSubmit", None)
        servers = dict(remaining.pop("mcp_servers", {}))
        servers.pop("harmony_local", None)
        if remaining or hooks or servers:
            raise ValueError("Harmony's hook settings need an update, but Codex or user settings were added "
                             "inside the managed comments. Existing configuration was preserved; move those "
                             "settings outside the Harmony comments before retrying.")
        before, rest = original.split(BEGIN, 1)
        _, after = rest.split(END, 1)
        updated = before + block + after
    else:
        updated = original.rstrip() + "\n\n" + block + "\n"
    tomllib.loads(updated)  # Refuse incompatible inline tables rather than damaging user settings.
    codex_home.mkdir(parents=True, exist_ok=True)
    if updated != original:
        if path.exists():
            backup = path.with_name(f"config.toml.harmony-backup-{uuid.uuid4().hex}")
            fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as out:
                out.write(original)
        fd, temporary = tempfile.mkstemp(prefix=".harmony-config-", dir=codex_home)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as out:
                out.write(updated)
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
    return path


def is_installed(codex_home: Path) -> bool:
    try:
        import tomllib
    except ImportError as exc:
        raise ValueError("Checking Codex hook configuration requires Python 3.11+") from exc

    path = codex_home / "config.toml"
    if not path.is_file():
        return False
    config = tomllib.loads(path.read_text(encoding="utf-8"))
    if config.get("features", {}).get("hooks") is False:
        return False
    command = shlex.join([sys.executable, "-m", "harmony.codex_bridge"])
    mcp = config.get("mcp_servers", {}).get("harmony_local", {})
    if (mcp.get("command") != sys.executable or mcp.get("args") != ["-m", "harmony.local_mcp"]
            or mcp.get("enabled") is False):
        return False
    return any(handler.get("type") == "command" and handler.get("command") == command
               for group in config.get("hooks", {}).get("UserPromptSubmit", [])
               for handler in group.get("hooks", []))


def main() -> int:
    try:
        raw = sys.stdin.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("Codex hook input is too large")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("expected a Codex hook object")
        result = prompt_context(payload, active_companions())
    except (OSError, ValueError) as exc:
        result = {"systemMessage": f"Harmony connection unavailable: {exc}. No card was created by the hook."}
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
