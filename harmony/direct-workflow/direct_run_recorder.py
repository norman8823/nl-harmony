#!/usr/bin/env python3
"""Per-run record for a direct Claude CLI invocation.

This is not a launcher. `run` executes exactly the `claude ...` argv it is
given (same binary from PATH, same flags, same environment, same normal Claude
login), tees its stdout unchanged, and afterwards writes one JSON record for
that run. No daemon, no UI, no retries, no model choice of its own.

    python3 direct-workflow/direct_run_recorder.py run \
        --task-id 39 --role writer -- \
        claude -p "$(cat prompt.md)" --model claude-opus-5-5 \
            --output-format stream-json --verbose \
            --settings direct-workflow/release-settings.json

Writer and correction runs are refused unless `--settings` names this
package's `release-settings.json` (the PR guard). Each run atomically reserves
a unique `<run-id>.json` / `<run-id>.stream.jsonl` pair before `claude`
starts; an id collision is refused and starts nothing.

    # after review, record what the CLI cannot know
    python3 direct-workflow/direct_run_recorder.py annotate \
        --record .direct-runs/<run-id>.json --review-rounds 1 --corrections 0 \
        --review-outcome pass

    # build a record from an already-captured stream (offline)
    python3 direct-workflow/direct_run_recorder.py parse \
        --task-id 28 --role writer --model claude-opus-5 --stream run.stream.jsonl

    # a resumed run: give the previous result of the same session so this
    # invocation's estimated list cost can be taken as a checked delta
    python3 direct-workflow/direct_run_recorder.py run \
        --task-id 39 --role correction \
        --prior-result .direct-runs/<previous-run-id>.stream.jsonl -- \
        claude -p "..." --resume <session-id> --model claude-opus-5-5 ...

Field provenance is explicit. Every value carries the `source` it was read
from, and every value the run did not report is `null` and listed under
`unknowns`. Four money/usage notions are kept apart and never derived from
each other:

  * tokens                      — this invocation's counters, from the result
                                  event's `usage`
  * estimated_list_cost_usd     — this invocation's share of the CLI's own
                                  client-side list-price estimate; not a charge
  * actual_provider_charge_usd  — only from an operator-supplied source
                                  (invoice, console); otherwise unknown
  * subscription_credits        — only from an operator-supplied source;
                                  otherwise unknown

Scope matters. On observed resumed runs of one session, `modelUsage`
and `total_cost_usd` grew from invocation to invocation (one result's
`modelUsage` equals the previous result's plus its own `usage`, exactly),
while `usage` held only the current invocation. So `usage` is treated as
per-invocation and `modelUsage`/`total_cost_usd` as session-cumulative. The
record therefore keeps `modelUsage`/`total_cost_usd` under
`session_cumulative`, and fills the per-invocation estimate only when it can
be checked: either a `--prior-result` snapshot of the same session whose
`modelUsage` delta equals this invocation's `usage` exactly (cost = the
`total_cost_usd` delta), or a run known not to resume a session whose
cumulative counters equal its `usage` exactly. Otherwise it is unknown, with
the reason recorded.

Codex runs (planner / reviewer). `run` also records a `codex exec --json`
invocation with an explicit model; Codex cannot take the writer or correction
role, which stays an explicit-model Claude run behind the PR guard:

    python3 direct-workflow/direct_run_recorder.py run \
        --task-id 39 --role reviewer -- \
        codex exec --json --sandbox read-only -m gpt-5.5 - < review-prompt.md

Codex exec JSON reports no model identity, no cost and no context occupancy,
and its usage only on `turn.completed`; the record says so (requested model
only, context unknown). `input_tokens` already includes `cached_input_tokens`,
so uncached input is their difference; `reasoning_output_tokens` is kept
apart and never added to output. A resumed thread's usage scope is
unverified, so its per-invocation tokens stay unknown; so do they when the
argv is unknown (`parse`) or the run completed more than one turn. The live
telemetry follows the same scope: its cumulative counters stay unknown and
the reported turn usage is kept apart, labelled scope-unverified.

Investigator runs (read-only, may overlap the writer). `--base-commit SHA`
is required: the recorder resolves it in the repository containing
`--project-root` (default: the current directory; `nlh record` passes the
selected project), refusing a ref such as HEAD, a commit of another
repository, a `base_commit` declared on the task's ticket or graph that is
not the same commit, and any configured git filter command (smudge, clean or
process; it would run outside the sandbox at checkout). It reserves the
record in the original records directory, adds an owned detached worktree of
that commit in a temporary directory outside the checkout (hooks and
fsmonitor off), checks HEAD and a clean status, runs the argv there, and
removes that worktree afterwards: on success, failure, Ctrl-C, SIGTERM or
SIGHUP (the child is stopped first and an `interrupted` record is written;
SIGKILL cannot be handled). The record's `repository` holds base/read commit
and worktree. The worktree is the run's default repository input; it is not
a proof that every read came from that commit. The argv is parsed fail-closed:

    # Codex: explicit read-only sandbox, no -c/--config, profile or bypass
    ... run --task-id 39 --role investigator --base-commit <sha> -- \
        codex exec --json -m gpt-5.5 -s read-only - < investigate.md
    # Claude: read-only allowlist AND explicit deny list AND --restricted, no MCP,
    # no hooks/settings files, no --add-dir
    ... run --task-id 39 --role investigator --base-commit <sha> -- \
        claude -p "$(cat investigate.md)" --model claude-sonnet-5-5 \
            --output-format stream-json --verbose --restricted --tools Read,Grep,Glob \
            --disallowedTools Write,Edit,MultiEdit,NotebookEdit,Bash,PowerShell,REPL,Agent,Task \
            --strict-mcp-config --mcp-config '{"mcpServers":{}}' \
            --settings '{"disableAllHooks":true}' --setting-sources ''

`--restricted` confines Claude's built-in file tools to the working directory
(the worktree); the deny list is what keeps writes and execution out. Codex's
read-only sandbox prevents writes; it is not a universal filesystem read
boundary.

Live snapshots. While a run is in progress its `<run-id>.json` holds
`status: "running"` and a `telemetry` snapshot rewritten at most once a
second: last event, phase, measured counters with the time they were
measured, and a capped time series. Readers that only count completed runs
skip it exactly as they skip `reserved`. Context occupancy is the prompt size
of the latest main-thread Claude API message (uncached + cache creation +
cache read of that one message), not a cumulative sum; the window size (and
so a percent) is known only once the result reports `contextWindow`. The
running sum over messages is labelled provisional; the final per-invocation
`tokens` still come only from the result. The completed record keeps the
final `telemetry` (additive to v2). Only the captured stdout of the managed
run is read; no native session file or credential is opened.

Exit codes: the wrapped process's exit code for `run`; 0 for `parse` and
`annotate`; 2 for usage or refused invocations.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA = "direct-run-record/v2"
RECORDS_DIR_ENV = "DIRECT_RUN_RECORDS_DIR"
DEFAULT_RECORDS_DIR = ".direct-runs"
ROLES = ("writer", "correction", "planner", "reviewer", "investigator", "other")
# Display names; the stored role values stay as they were for v2 readers.
ROLE_LABELS = {"writer": "Implementer", "correction": "Correction", "planner": "Planner",
               "reviewer": "Reviewer", "investigator": "Investigator", "other": "Other"}
PROVIDERS = ("claude", "codex")
# Billing would silently move from the subscription login to an API key.
API_KEY_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
CODEX_API_KEY_ENV = ("CODEX_API_KEY",)
MACHINE_OUTPUT = ("json", "stream-json")
LONG_ARG_CHARS = 200
# Roles that may write and push, so must run with the PR guard loaded.
GUARDED_ROLES = ("writer", "correction")
RELEASE_SETTINGS = Path(__file__).resolve().with_name("release-settings.json")
GUARD_SCRIPT = "direct-workflow-pr-guard.py"
# release-settings.json runs "${HARMONY_PR_GUARD:-...}"; the recorder points it
# at this package's guard so an installed package runs the real hook. If it
# resolves to no file, `python3` exits 2 and Claude Code blocks the call.
GUARD_ENV = "HARMONY_PR_GUARD"
GUARD_PATH = Path(__file__).resolve().with_name(GUARD_SCRIPT)
LIVE_INTERVAL_S = 1.0  # at most one running snapshot per second
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

SRC_MODEL_USAGE = "claude_cli.result.modelUsage"
SRC_USAGE = "claude_cli.result.usage"
SRC_TOTAL_COST = "claude_cli.result.total_cost_usd"
SRC_INIT = "claude_cli.system.init"
SRC_RESULT = "claude_cli.result"
SRC_WALL = "recorder.wall_clock"
SRC_ARGV = "recorder.argv"
SRC_OPERATOR = "operator"
SRC_CODEX_USAGE = "codex_exec.turn.completed.usage"
SRC_CODEX_THREAD = "codex_exec.thread.started"
SRC_PRIOR_DELTA = (
    "claude_cli.result.total_cost_usd minus the same field of the prior "
    "snapshot (operator-supplied --prior-result)"
)

INVOCATION_SCOPE = "this invocation only"
CUMULATIVE_SCOPE = (
    "session-cumulative: includes every earlier invocation of the same "
    "session when the run resumes one"
)
COST_BASIS = (
    "Claude CLI client-side estimate at API list price; not a charge and not "
    "subscription usage"
)
# Flags that continue an existing conversation; cumulative fields then carry
# earlier invocations. `--fork-session` is listed because it only modifies a
# resume/continue and its cumulative behaviour is unverified.
RESUME_FLAGS = ("-c", "--continue", "-r", "--resume", "--fork-session")

# modelUsage (camelCase, per model, session-cumulative) -> record field
MODEL_USAGE_FIELDS = {
    "uncached_input": "inputTokens",
    "cache_creation_input": "cacheCreationInputTokens",
    "cached_input": "cacheReadInputTokens",
    "output": "outputTokens",
}
# usage (snake_case, aggregate, this invocation) -> record field
USAGE_FIELDS = {
    "uncached_input": "input_tokens",
    "cache_creation_input": "cache_creation_input_tokens",
    "cached_input": "cache_read_input_tokens",
    "output": "output_tokens",
}


class RefusedError(Exception):
    """The invocation is not a recordable direct Claude run."""


# --- argv inspection --------------------------------------------------------


def _flag_value(argv: list[str], *names: str) -> str | None:
    for index, arg in enumerate(argv):
        for name in names:
            if arg == name and index + 1 < len(argv):
                return argv[index + 1]
            if arg.startswith(name + "="):
                return arg.split("=", 1)[1]
    return None


def _flag_values(argv: list[str], name: str) -> list[str]:
    """Every value given for `name`, in both `--flag v` and `--flag=v` forms."""
    values = []
    for index, arg in enumerate(argv):
        if arg == name and index + 1 < len(argv):
            values.append(argv[index + 1])
        elif arg.startswith(name + "="):
            values.append(arg.split("=", 1)[1])
    return values


def is_resume(argv: list[str] | None) -> bool | None:
    """Whether the argv continues an existing session; None when argv is unknown."""
    if argv is None:
        return None
    return any(
        arg in RESUME_FLAGS or arg.startswith("--resume=") for arg in argv
    )


def _registers_guard(path: Path) -> bool:
    """True when the settings file wires the PR guard as a PreToolUse Bash hook."""
    try:
        settings = json.loads(path.read_text(encoding="utf-8"))
        for entry in settings["hooks"]["PreToolUse"]:
            if entry.get("matcher") != "Bash":
                continue
            for hook in entry.get("hooks") or []:
                if GUARD_SCRIPT in str(hook.get("command") or ""):
                    return True
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False
    return False


def _require_release_settings(claude_argv: list[str], role: str, cwd: Path) -> None:
    """A writer/correction session must load the PR guard via `--settings`.

    Project settings are not relied on (a direct session may run with
    `--setting-sources ''`), so the guard is only present when the argv names
    this package's `release-settings.json`. Relative paths resolve against the
    directory `claude` will run in, which is the recorder's own cwd.
    """
    expected = RELEASE_SETTINGS.resolve()
    for value in _flag_values(claude_argv, "--settings"):
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = cwd / candidate
        if candidate.resolve() == expected and _registers_guard(expected):
            return
    raise RefusedError(
        f"a {role} run must load the PR guard: pass `--settings "
        "direct-workflow/release-settings.json` (this package's file, which "
        "registers direct-workflow-pr-guard.py)"
    )


def provider_of(argv: list[str]) -> str | None:
    """`claude` or `codex` from the wrapped binary's name; None for anything else."""
    name = os.path.basename(argv[0]) if argv else ""
    return name if name in PROVIDERS else None


def _codex_model(argv: list[str]) -> str | None:
    model = _flag_value(argv, "-m", "--model")
    if model:
        return model
    for value in _flag_values(argv, "-c") + _flag_values(argv, "--config"):
        key, _, raw = value.partition("=")
        if key.strip() == "model" and raw.strip():
            return raw.strip().strip("'\"")
    return None


def check_codex_invocation(argv: list[str], role: str, env: dict[str, str]) -> str:
    """A recordable Codex run: `codex exec --json` with an explicit model, in a
    role that never writes or publishes. Returns the model."""
    if role in GUARDED_ROLES:
        raise RefusedError(
            f"the {role} must be an explicit-model Claude run behind the PR guard; "
            "Codex runs record as planner, reviewer, investigator or other"
        )
    if "exec" not in argv[1:]:
        raise RefusedError("the run must be non-interactive (`codex exec ...`)")
    if "--json" not in argv:
        raise RefusedError("pass `--json`; usage cannot be recorded from text output")
    model = _codex_model(argv)
    if not model:
        raise RefusedError("the model must be explicit (`-m ...` or `-c model=...`)")
    present = [name for name in CODEX_API_KEY_ENV if env.get(name)]
    if present:
        raise RefusedError(
            f"{', '.join(present)} is set; recorded Codex runs use the normal Codex "
            "login, not an API key. Unset it and retry."
        )
    return model


def check_invocation(
    claude_argv: list[str], role: str, env: dict[str, str], cwd: Path | None = None
) -> str:
    """Refuse anything that is not an explicit-model, machine-readable direct
    Claude CLI run on the subscription login, and any writer/correction run
    that does not load the release-settings PR guard. A `codex` argv is
    checked by `check_codex_invocation`. Returns the model."""
    if claude_argv and provider_of(claude_argv) == "codex":
        return check_codex_invocation(claude_argv, role, env)
    if not claude_argv or os.path.basename(claude_argv[0]) != "claude":
        raise RefusedError("the wrapped command must be the `claude` or `codex` CLI itself")
    if not any(arg in ("-p", "--print") for arg in claude_argv):
        raise RefusedError("the run must be non-interactive (`-p`/`--print`)")
    output_format = _flag_value(claude_argv, "--output-format")
    if output_format not in MACHINE_OUTPUT:
        raise RefusedError(
            "pass `--output-format stream-json --verbose` (or `json`); usage "
            "cannot be recorded from text output"
        )
    model = _flag_value(claude_argv, "--model")
    if not model:
        raise RefusedError("the model must be explicit (`--model ...`)")
    if role in GUARDED_ROLES:
        _require_release_settings(claude_argv, role, cwd or Path.cwd())
    present = [name for name in API_KEY_ENV if env.get(name)]
    if present:
        raise RefusedError(
            f"{', '.join(present)} is set; direct runs use the normal Claude "
            "(subscription) login, not an API key. Unset it and retry."
        )
    return model


# --- read-only investigators --------------------------------------------------
#
# An investigator may run while the writer edits, so its argv is parsed fail-closed:
# every option must be known and allowed, each at most once, and the isolation
# options must all be present. Nothing is inferred from an option that is absent.

INVESTIGATOR_ROLE = "investigator"
CLAUDE_READ_TOOLS = ("Read", "Grep", "Glob")
CLAUDE_DENIED_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit", "Bash", "PowerShell", "REPL", "Agent", "Task")
CLAUDE_EMPTY_MCP = {"mcpServers": {}}
CLAUDE_NO_HOOKS = {"disableAllHooks": True}
CLAUDE_PERMISSION_MODES = ("default", "manual", "plan", "dontAsk")
TOOL_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
# option -> (canonical name, "flag" | "value" | "variadic"); variadic options take
# every following non-option argument too, exactly as the Claude CLI parses them.
CLAUDE_INVESTIGATOR_OPTIONS = {
    "-p": ("print", "flag"), "--print": ("print", "flag"), "--verbose": ("verbose", "flag"),
    "--strict-mcp-config": ("strict-mcp-config", "flag"),
    "--no-session-persistence": ("no-session-persistence", "flag"),
    "--include-partial-messages": ("include-partial-messages", "flag"),
    "--exclude-dynamic-system-prompt-sections": ("exclude-dynamic-system-prompt-sections", "flag"),
    "--disable-slash-commands": ("disable-slash-commands", "flag"),
    "--restricted": ("restricted", "flag"), "--no-chrome": ("no-chrome", "flag"),
    "--model": ("model", "value"), "--effort": ("effort", "value"),
    "--output-format": ("output-format", "value"), "--input-format": ("input-format", "value"),
    "--append-system-prompt": ("append-system-prompt", "value"), "--system-prompt": ("system-prompt", "value"),
    "--max-budget-usd": ("max-budget-usd", "value"), "--json-schema": ("json-schema", "value"),
    "-n": ("name", "value"), "--name": ("name", "value"),
    "--permission-mode": ("permission-mode", "value"), "--permission-prompts": ("permission-prompts", "value"),
    "--settings": ("settings", "value"), "--setting-sources": ("setting-sources", "value"),
    "--tools": ("tools", "variadic"), "--mcp-config": ("mcp-config", "variadic"),
    "--disallowedTools": ("disallowedTools", "variadic"), "--disallowed-tools": ("disallowedTools", "variadic"),
}
CLAUDE_INVESTIGATOR_REFUSED = {
    "--dangerously-skip-permissions": "bypasses permission checks",
    "--allow-dangerously-skip-permissions": "allows bypassing permission checks",
    "--allowedTools": "pre-approves tools; an investigator gets an explicit --tools allowlist and deny list",
    "--allowed-tools": "pre-approves tools; an investigator gets an explicit --tools allowlist and deny list",
    "--add-dir": "widens --restricted file access beyond the isolated worktree",
    "--plugin-dir": "loads a plugin", "--plugin-url": "loads a plugin",
    "--agents": "defines agents", "--agent": "selects an agent with its own tools",
    "-c": "continues a session", "--continue": "continues a session", "-r": "resumes a session",
    "--resume": "resumes a session", "--fork-session": "forks a resumed session", "--from-pr": "resumes a session",
    "--teleport": "resumes a session", "--session-id": "reuses a session id",
    "-w": "creates its own worktree", "--worktree": "creates its own worktree",
    "--bare": "switches authentication away from the subscription login",
    "--ide": "connects an IDE", "--chrome": "enables browser control", "--brief": "adds a messaging tool",
    "--bg": "detaches from the recorder", "--background": "detaches from the recorder",
    "--cloud": "runs elsewhere", "--remote-control": "enables remote control",
    "--fallback-model": "changes the explicit model", "--betas": "API key users only",
}
CLAUDE_COMMANDS = ("agents", "attach", "auth", "auto-mode", "doctor", "gateway", "import", "install", "logs",
                   "mcp", "plugin", "plugins", "purge", "respawn", "rm", "setup-token", "stop", "kill",
                   "ultrareview", "update", "upgrade")
# Codex investigator: `codex exec` with these options only.
CODEX_INVESTIGATOR_OPTIONS = {
    "--json": ("json", "flag"), "-m": ("model", "value"), "--model": ("model", "value"),
    "-s": ("sandbox", "value"), "--sandbox": ("sandbox", "value"), "--color": ("color", "value"),
    "--skip-git-repo-check": ("skip-git-repo-check", "flag"), "--ephemeral": ("ephemeral", "flag"),
}
CODEX_INVESTIGATOR_REFUSED = {
    "--dangerously-bypass-approvals-and-sandbox": "bypasses the sandbox", "--yolo": "bypasses the sandbox",
    "--full-auto": "selects a writable sandbox", "-c": "overrides configuration (sandbox, model, tools)",
    "--config": "overrides configuration (sandbox, model, tools)", "-p": "loads a profile that may widen the sandbox",
    "--profile": "loads a profile that may widen the sandbox", "-C": "moves the working root off the worktree",
    "--cd": "moves the working root off the worktree", "--add-dir": "adds writable directories",
    "--enable": "enables features", "--disable": "changes features", "--oss": "changes the provider",
    "--local-provider": "changes the provider", "-o": "writes a file", "--output-last-message": "writes a file",
}
CODEX_COMMANDS = ("resume", "fork", "review", "apply", "help")


def _parse_options(argv: list[str], start: int, allowed: dict[str, tuple[str, str]],
                   refused: dict[str, str], tool: str) -> tuple[dict[str, list[str]], list[str]]:
    """Each allowed option once, as {canonical: values}, plus the positionals; anything else is refused."""
    options: dict[str, list[str]] = {}
    positionals: list[str] = []
    index = start
    while index < len(argv):
        arg = argv[index]
        if arg == "--":
            raise RefusedError(f"an investigator {tool} argv may not use `--`; put the prompt before options")
        if not arg.startswith("-") or arg == "-":
            positionals.append(arg)
            index += 1
            continue
        name, eq, inline = arg.partition("=") if arg.startswith("--") else (arg, "", "")
        if name in refused:
            raise RefusedError(f"{name} is not allowed for an investigator: it {refused[name]}")
        if name not in allowed:
            raise RefusedError(f"{name} is not an allowed investigator {tool} option (the argv is checked fail-closed)")
        canonical, kind = allowed[name]
        if canonical in options:
            raise RefusedError(f"{name} is given more than once; an investigator's options must be unambiguous")
        values: list[str] = []
        if kind == "flag":
            if eq:
                raise RefusedError(f"{name} takes no value")
        elif eq:
            values.append(inline)
        else:
            if index + 1 >= len(argv) or (argv[index + 1].startswith("-") and argv[index + 1] != ""):
                raise RefusedError(f"{name} needs a value")
            index += 1
            values.append(argv[index])
        if kind == "variadic":  # the CLI keeps consuming arguments up to the next option
            while index + 1 < len(argv) and not argv[index + 1].startswith("-"):
                index += 1
                values.append(argv[index])
        options[canonical] = values
        index += 1
    return options, positionals


def _tool_names(values: list[str]) -> list[str]:
    return [name for value in values for name in re.split(r"[\s,]+", value) if name]


def _inline_json(option: str, values: list[str] | None, expected: dict) -> None:
    if values is None:
        raise RefusedError(f"an investigator must pass {option} {json.dumps(expected)} inline")
    if len(values) != 1:
        raise RefusedError(f"{option} must be exactly one inline value, {json.dumps(expected)}")
    try:
        value = json.loads(values[0])
    except ValueError:
        value = None
    if value != expected:
        raise RefusedError(f"{option} must be exactly {json.dumps(expected)} inline (no file, no other keys)")


def check_claude_investigator(argv: list[str]) -> None:
    """A Claude investigator reads only: an explicit Read/Grep/Glob (or empty) tool set,
    an explicit deny of every write/exec tool, `--restricted`, no MCP servers, no hooks,
    no settings files, no `--add-dir`. `--restricted` confines the built-in file tools to
    the working directory (the pinned worktree); it does not replace the deny list."""
    options, positionals = _parse_options(argv, 1, CLAUDE_INVESTIGATOR_OPTIONS, CLAUDE_INVESTIGATOR_REFUSED,
                                          "claude")
    if "restricted" not in options:
        raise RefusedError("an investigator must pass --restricted so the file tools stay in its worktree "
                           "(in addition to, not instead of, the explicit tool deny list)")
    if len(positionals) > 1 or (positionals and positionals[0] in CLAUDE_COMMANDS):
        raise RefusedError("an investigator claude argv takes at most one positional prompt (no subcommand); "
                           "a variadic option such as --tools must not swallow it")
    if "tools" not in options:
        raise RefusedError(f"an investigator must pass --tools limited to {','.join(CLAUDE_READ_TOOLS)} "
                           "(or \"\" for none); an allowlist is required, not only a deny list")
    tools = _tool_names(options["tools"])
    extra = [t for t in tools if t not in CLAUDE_READ_TOOLS]
    if extra:
        raise RefusedError(f"--tools may name only {', '.join(CLAUDE_READ_TOOLS)}; not {', '.join(extra)}")
    if "disallowedTools" not in options:
        raise RefusedError("an investigator must pass --disallowedTools denying "
                           f"{','.join(CLAUDE_DENIED_TOOLS)}; an allowlist alone is not enough")
    denied = _tool_names(options["disallowedTools"])
    malformed = [t for t in denied if not TOOL_NAME_RE.match(t)]
    if malformed:
        raise RefusedError(f"--disallowedTools must name whole tools, not patterns: {', '.join(malformed)}")
    missing = [t for t in CLAUDE_DENIED_TOOLS if t not in denied]
    if missing:
        raise RefusedError(f"--disallowedTools must also deny {', '.join(missing)}")
    if "strict-mcp-config" not in options:
        raise RefusedError("an investigator must pass --strict-mcp-config so no external MCP server loads")
    _inline_json("--mcp-config", options.get("mcp-config"), CLAUDE_EMPTY_MCP)
    _inline_json("--settings", options.get("settings"), CLAUDE_NO_HOOKS)
    if options.get("setting-sources") != [""]:
        raise RefusedError("an investigator must pass --setting-sources '' so no user/project/local settings "
                           "(and so no repository or user hooks) load")
    mode = options.get("permission-mode")
    if mode is not None and mode[0] not in CLAUDE_PERMISSION_MODES:
        raise RefusedError(f"--permission-mode {mode[0]} widens permissions; an investigator may use "
                           f"{', '.join(CLAUDE_PERMISSION_MODES)}")


def check_codex_investigator(argv: list[str]) -> None:
    """A Codex investigator runs `codex exec` in an explicit read-only sandbox, with no overrides."""
    if len(argv) < 2 or argv[1] != "exec":
        raise RefusedError("an investigator codex argv starts `codex exec`")
    options, positionals = _parse_options(argv, 2, CODEX_INVESTIGATOR_OPTIONS, CODEX_INVESTIGATOR_REFUSED, "codex")
    if len(positionals) > 1 or (positionals and positionals[0] in CODEX_COMMANDS):
        raise RefusedError("an investigator codex argv takes at most one prompt (or `-`), no subcommand")
    if options.get("sandbox") != ["read-only"]:
        raise RefusedError("an investigator must pass -s/--sandbox read-only explicitly")
    if "model" not in options:
        raise RefusedError("an investigator must name its model with -m/--model")


def check_investigator(argv: list[str]) -> None:
    """Refuse an investigator argv that could write, execute, or load outside configuration."""
    if provider_of(argv) == "codex":
        check_codex_investigator(argv)
    else:
        check_claude_investigator(argv)


# --- investigator repository isolation -------------------------------------------

SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
SRC_GIT = "recorder.git"
# Repository hooks and an executable (or daemon) fsmonitor never run for the recorder's own git calls.
GIT = ("git", "-c", f"core.hooksPath={os.devnull}", "-c", "core.fsmonitor=false")
FILTER_KEY_RE = re.compile(r"^filter\.(.+)\.(smudge|clean|process)$")


def _git_env() -> dict[str, str]:
    """Preparation uses repository config, not machine-wide checkout filters/settings.

    Child-only overrides: no user settings are changed. Commits are checked
    out as recorded, without global LFS/conversion helpers or credential config.
    """
    return dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")


def _git(cwd: Path, *args: str) -> str:
    """One git call without a shell; hooks and fsmonitor are disabled for it."""
    try:
        result = subprocess.run([*GIT, *args], cwd=cwd, env=_git_env(), capture_output=True, text=True)
    except OSError as exc:
        raise RefusedError(f"git is not available: {exc}") from exc
    if result.returncode != 0:
        raise RefusedError(f"git {' '.join(args)} failed: {result.stderr.strip() or result.returncode}")
    return result.stdout.strip()


def refuse_filter_drivers(cwd: Path) -> None:
    """Refuse when any configured filter driver has a command: checkout (smudge/process) or
    status (clean) would run it outside the investigator's sandbox. Names only, never commands."""
    try:
        result = subprocess.run([*GIT, "config", "--null", "--get-regexp", r"^filter\..*\.(smudge|clean|process)$"],
                                cwd=cwd, env=_git_env(), capture_output=True, text=True)
    except OSError as exc:
        raise RefusedError(f"git is not available: {exc}") from exc
    if result.returncode not in (0, 1):  # 1: no such key
        raise RefusedError(f"cannot read git filter configuration: {result.stderr.strip() or result.returncode}")
    drivers = set()
    for entry in result.stdout.split("\0"):
        key, _, value = entry.partition("\n")
        match = FILTER_KEY_RE.match(key)
        if match and value.strip():
            drivers.add(match.group(1))
    if drivers:
        raise RefusedError(f"git filter driver(s) {', '.join(sorted(drivers))} have configured commands, which a "
                           "checkout or status would run outside the investigator's sandbox; an investigator "
                           "refuses rather than run them")


def _resolve_commit(repo: Path, value: str) -> str:
    try:
        return _git(repo, "rev-parse", "--verify", "--quiet", f"{value}^{{commit}}")
    except RefusedError:
        raise RefusedError(f"{value} is not a commit in {repo}") from None


def _declared_base_commits(workflow_dir: Path, task_id: str) -> list[tuple[str, Any]]:
    """`base_commit` declared on the canonical ticket or graph, as (file, value); files are only read."""
    declared = []
    for kind in ("tickets", "graphs"):
        path = workflow_dir / kind / f"{task_id}.json"
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RefusedError(f"cannot read {path} to check its base_commit: {exc}") from exc
        if isinstance(data, dict) and data.get("base_commit") is not None:
            declared.append((str(path), data["base_commit"]))
    return declared


def resolve_investigator_base(project_root: Path, base_commit: str | None, task_id: str,
                              workflow_dir: Path | None) -> dict[str, Any]:
    """The repository facts an investigator run is pinned to; refuses before anything starts.

    The repository is the one containing `project_root` (the selected project,
    not necessarily the current directory). The operator names the commit
    explicitly; nothing is guessed from HEAD or the writer's (possibly dirty)
    checkout. A base declared on the task's ticket or graph must be the same
    commit. Configured git filter commands are refused (`refuse_filter_drivers`).
    """
    if not base_commit:
        raise RefusedError("an investigator run needs --base-commit SHA: the commit it reads, in an isolated "
                           "worktree (nothing is guessed from HEAD or the working tree)")
    if not SHA_RE.match(base_commit):
        raise RefusedError(f"--base-commit {base_commit!r} must be a commit SHA (refs such as HEAD or branch "
                           "names are not accepted)")
    if not RUN_ID_RE.match(task_id):
        raise RefusedError(f"task id {task_id!r} is not safe for an investigator run")
    try:
        repo = Path(_git(project_root, "rev-parse", "--show-toplevel")).resolve()
    except RefusedError:
        raise RefusedError(f"an investigator run needs a Git repository; {project_root} is not in one") from None
    refuse_filter_drivers(repo)
    full = _resolve_commit(repo, base_commit)
    if not full.lower().startswith(base_commit.lower()):
        raise RefusedError(f"--base-commit {base_commit} resolved to unrelated {full}")
    workflow = workflow_dir or Path(os.environ.get("TASK_WORKFLOW_DIR") or repo / ".workflow")
    declared = _declared_base_commits(workflow.expanduser().resolve(), task_id)
    for path, value in declared:
        if not isinstance(value, str) or not SHA_RE.match(value) or _resolve_commit(repo, value) != full:
            raise RefusedError(f"{path} declares base_commit {value!r}, not --base-commit {full}")
    return {
        "source": SRC_GIT,
        "repository": str(repo),
        "base_commit": full,
        "base_commit_source": "operator --base-commit"
        + (f"; matches base_commit declared in {', '.join(p for p, _ in declared)}" if declared else
           "; the task declares no base_commit"),
        "read_commit": None,
        "isolated_worktree": None,
        "worktree_clean_at_launch": None,
        "isolation": "detached git worktree at base_commit outside the writer's checkout; removed after the run",
    }


@contextlib.contextmanager
def investigator_worktree(repo: Path, base: str, owned: dict[str, Path] | None = None):
    """A detached worktree of `base`, owned by this run, outside `repo`; always removed afterwards.

    Only the worktree this call added is removed (by its own path, also when
    the add itself was interrupted); no other worktree is pruned or touched,
    and no existing path is reused. `owned["worktree"]` names it once chosen.
    """
    parent = Path(tempfile.mkdtemp(prefix="harmony-investigator-")).resolve()
    path = parent / "worktree"
    if owned is not None:
        owned["worktree"] = path
    attempted = False
    try:
        if path == repo or repo in path.parents:
            raise RefusedError(f"the temporary directory {parent} is inside the writer's checkout {repo}")
        refuse_filter_drivers(repo)  # again, right before the checkout
        attempted = True
        _git(repo, "worktree", "add", "--detach", str(path), base)
        yield path
    finally:
        if attempted:
            removed = subprocess.run([*GIT, "worktree", "remove", "--force", str(path)], cwd=repo,
                                     env=_git_env(), capture_output=True, text=True)
            if removed.returncode != 0 and (path.exists() or worktree_registered(repo, path) is not False):
                print(f"direct_run_recorder: could not remove investigator worktree {path}: "
                      f"{removed.stderr.strip()}", file=sys.stderr)
        shutil.rmtree(parent, ignore_errors=True)


def worktree_registered(repo: Path, path: Path) -> bool | None:
    """Whether `repo` still lists `path` as a worktree; None when git cannot say."""
    try:
        listed = _git(repo, "worktree", "list", "--porcelain")
    except RefusedError:
        return None
    return f"worktree {path}" in listed.splitlines()


class Terminated(BaseException):
    """SIGTERM/SIGHUP turned into an unwind, so an investigator's child and worktree are cleaned up."""

    def __init__(self, signum: int):
        super().__init__(signum)
        self.signum = signum


UNWIND_SIGNALS = tuple(s for s in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGHUP", None)) if s is not None)


@contextlib.contextmanager
def unwind_on_signals(signums=UNWIND_SIGNALS, state=None):
    """Raise `Terminated` on the first of `signums`; later ones are ignored so the cleanup it
    started is not cut short. An ignored signal (e.g. SIGHUP under nohup) stays ignored, and the
    previous handlers are restored afterwards. (SIGKILL cannot be caught; nothing here helps then.)"""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    fired: list[int] = []
    state = state if state is not None else {}

    def handler(signum, frame):
        if not fired:
            fired.append(signum)
            if state.get("launching_process"):
                # Finish binding the child before unwinding; its signal mask is unchanged.
                state["pending_signal"] = signum
                return
            raise Terminated(signum)

    previous = {signum: signal.signal(signum, handler) for signum in signums
                if signal.getsignal(signum) is not signal.SIG_IGN}
    try:
        yield
    finally:
        for signum, old in previous.items():
            signal.signal(signum, old if old is not None else signal.SIG_DFL)


def verify_investigator_worktree(path: Path, repository: dict[str, Any]) -> None:
    """HEAD is the base commit and nothing is modified or untracked; fills in what was read.
    Filter commands are refused again before `status`, which could run a clean filter."""
    refuse_filter_drivers(path)
    head = _git(path, "rev-parse", "HEAD")
    if head != repository["base_commit"]:
        raise RefusedError(f"worktree HEAD {head} is not base commit {repository['base_commit']}")
    if _git(path, "status", "--porcelain", "--untracked-files=all"):
        raise RefusedError(f"worktree {path} is not clean at launch")
    repository.update(read_commit=head, isolated_worktree=str(path), worktree_clean_at_launch=True)


def redact_argv(argv: list[str]) -> list[str]:
    """Keep flags readable; replace long values (prompts) by length + hash."""
    out = []
    for arg in argv:
        if len(arg) > LONG_ARG_CHARS:
            digest = hashlib.sha256(arg.encode()).hexdigest()[:16]
            out.append(f"<{len(arg)} chars sha256:{digest}>")
        else:
            out.append(arg)
    return out


# --- stream parsing ---------------------------------------------------------


def iter_events(text: str) -> Iterable[dict[str, Any]]:
    """Events from stream-json (one object per line) or json (one object)."""
    stripped = text.strip()
    if not stripped:
        return
    try:
        whole = json.loads(stripped)
    except ValueError:
        whole = None
    if isinstance(whole, dict):
        yield whole
        return
    if isinstance(whole, list):
        yield from (event for event in whole if isinstance(event, dict))
        return
    for line in stripped.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue  # a non-JSON line is noise, never a reason to guess
        if isinstance(event, dict):
            yield event


def last_result(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    results = [e for e in events if e.get("type") == "result"]
    return results[-1] if results else None


def load_prior_result(path: str) -> dict[str, Any]:
    """The last result event of a previously captured json/stream-json output."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise RefusedError(f"cannot read --prior-result {path!r}: {exc}") from exc
    result = last_result(list(iter_events(text)))
    if result is None:
        raise RefusedError(f"--prior-result {path!r} holds no result event")
    return result


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    return float(value)


def _known(value: Any, source: str) -> dict[str, Any]:
    return {"value": value, "source": source if value is not None else None}


def _usage_counters(result: dict[str, Any] | None) -> dict[str, int | None] | None:
    """This invocation's counters from the result's aggregate `usage`."""
    usage = (result or {}).get("usage")
    if not isinstance(usage, dict) or not usage:
        return None
    return {name: _int(usage.get(key)) for name, key in USAGE_FIELDS.items()}


def _cumulative_counters(
    result: dict[str, Any] | None,
) -> tuple[dict[str, dict[str, Any]], dict[str, int | None]] | None:
    """Per-model and summed session-cumulative counters from `modelUsage`.

    A counter is summed only when every model reports it; otherwise the total
    is null rather than a partial sum.
    """
    model_usage = (result or {}).get("modelUsage")
    if not isinstance(model_usage, dict) or not model_usage:
        return None
    by_model: dict[str, dict[str, Any]] = {}
    for model, counters in model_usage.items():
        counters = counters if isinstance(counters, dict) else {}
        by_model[model] = {
            name: _int(counters.get(key)) for name, key in MODEL_USAGE_FIELDS.items()
        }
        by_model[model]["estimated_list_cost_usd"] = _num(counters.get("costUSD"))
    totals: dict[str, int | None] = {}
    for name in MODEL_USAGE_FIELDS:
        values = [entry[name] for entry in by_model.values()]
        totals[name] = None if any(v is None for v in values) else sum(values)
    return by_model, totals


def _complete(counters: dict[str, int | None] | None) -> bool:
    return counters is not None and all(v is not None for v in counters.values())


def extract_tokens(result: dict[str, Any] | None) -> dict[str, Any]:
    """This invocation's token counters, from the result's `usage` only.

    `modelUsage` is not used here: on a resumed session it is cumulative
    across invocations (see `session_cumulative`).
    """
    counters = _usage_counters(result)
    if counters is None:
        return {"source": None, "scope": INVOCATION_SCOPE,
                **{name: None for name in USAGE_FIELDS}}
    return {"source": SRC_USAGE, "scope": INVOCATION_SCOPE, **counters}


def extract_session_cumulative(result: dict[str, Any] | None) -> dict[str, Any]:
    """`modelUsage` and `total_cost_usd`, labelled as session-cumulative."""
    cumulative = _cumulative_counters(result)
    by_model, totals = cumulative if cumulative else (None, None)
    return {
        "scope": CUMULATIVE_SCOPE,
        "source": SRC_MODEL_USAGE if cumulative else None,
        "by_model": by_model,
        "totals": totals,
        "estimated_list_cost_usd": {
            **_known(_num((result or {}).get("total_cost_usd")), SRC_TOTAL_COST),
            "basis": COST_BASIS,
        },
    }


def estimate_invocation_cost(
    result: dict[str, Any] | None,
    prior: dict[str, Any] | None,
    resumed: bool | None,
) -> tuple[dict[str, Any], str]:
    """This invocation's estimated list cost, only when it can be checked.

    Returns the cost field and a token cross-check status. The estimate is
    filled only by exact counter equality, never by assumption:

      * with a prior snapshot of the same session: the `modelUsage` delta
        must equal this invocation's `usage` exactly; the cost is then the
        `total_cost_usd` delta;
      * without one: the run must be known not to resume a session, and its
        cumulative counters must equal its `usage` exactly.
    """
    cost: dict[str, Any] = {
        "value": None, "source": None, "method": None, "basis": COST_BASIS,
        "unknown_reason": None,
    }

    def unknown(reason: str, check: str = "not_checked") -> tuple[dict[str, Any], str]:
        cost["unknown_reason"] = reason
        return cost, check

    if result is None:
        return unknown("no result event")
    usage = _usage_counters(result)
    if not _complete(usage):
        return unknown("the result's usage is missing or incomplete")
    cumulative = _cumulative_counters(result)
    totals = cumulative[1] if cumulative else None
    total_cost = _num(result.get("total_cost_usd"))

    if prior is not None:
        session = result.get("session_id")
        if not session or prior.get("session_id") != session:
            return unknown("the prior snapshot is not from this run's session")
        prior_cumulative = _cumulative_counters(prior)
        prior_totals = prior_cumulative[1] if prior_cumulative else None
        prior_cost = _num(prior.get("total_cost_usd"))
        if not (_complete(totals) and _complete(prior_totals)):
            return unknown("modelUsage counters are incomplete in this or the prior snapshot")
        delta = {name: totals[name] - prior_totals[name] for name in USAGE_FIELDS}
        if delta != usage:
            return unknown(
                f"the modelUsage delta since the prior snapshot ({delta}) does "
                f"not equal this invocation's usage ({usage}), so the cost "
                "delta cannot be attributed to this invocation",
                "prior_snapshot_delta_mismatch",
            )
        if total_cost is None or prior_cost is None or total_cost < prior_cost:
            return unknown(
                "total_cost_usd is missing or decreasing between the snapshots",
                "prior_snapshot_delta_match",
            )
        cost.update(
            value=round(total_cost - prior_cost, 10),
            source=SRC_PRIOR_DELTA,
            method="prior_snapshot_delta",
        )
        return cost, "prior_snapshot_delta_match"

    if resumed is None:
        return unknown(
            "whether this run resumed a session is unknown (no argv) and no "
            "--prior-result was given; total_cost_usd may be session-cumulative"
        )
    if resumed:
        return unknown(
            "this run resumes a session, so total_cost_usd and modelUsage are "
            "session-cumulative; pass --prior-result with the previous result "
            "of the same session to record a checked delta"
        )
    if not _complete(totals) or total_cost is None:
        return unknown("modelUsage counters or total_cost_usd are missing or incomplete")
    if totals != usage:
        return unknown(
            f"session-cumulative modelUsage ({totals}) does not equal this "
            f"invocation's usage ({usage})",
            "cumulative_differs_from_usage",
        )
    cost.update(
        value=total_cost,
        source=SRC_TOTAL_COST,
        method="fresh_session_total_matches_usage",
    )
    return cost, "cumulative_equals_usage"


def codex_counters(usage: Any) -> tuple[dict[str, int | None], str | None]:
    """Record counters from one Codex `turn.completed.usage`, plus why any is unknown.

    `input_tokens` includes `cached_input_tokens` (the cache-read subset), so
    uncached = input - cached. A positive `cache_write_input_tokens` has no
    verified relation to `input_tokens`, so uncached input is then unknown
    rather than guessed. `reasoning_output` is a subset of `output`, kept apart.
    """
    usage = usage if isinstance(usage, dict) else {}
    total_in = _int(usage.get("input_tokens"))
    cached = _int(usage.get("cached_input_tokens"))
    written = _int(usage.get("cache_write_input_tokens"))
    counters: dict[str, int | None] = {
        "uncached_input": None,
        "cache_creation_input": written,
        "cached_input": cached,
        "output": _int(usage.get("output_tokens")),
        "reasoning_output": _int(usage.get("reasoning_output_tokens")),
    }
    reason = None
    if total_in is None or cached is None or cached > total_in:
        reason = "input_tokens/cached_input_tokens missing or inconsistent"
    elif written:
        reason = "cache_write_input_tokens > 0: its relation to input_tokens is unverified"
    else:
        counters["uncached_input"] = total_in - cached
    if "cache_write_input_tokens" not in usage:
        reason = reason or "cache_write_input_tokens not reported"
    return counters, reason


def _usage_parts(usage: Any) -> dict[str, int | None] | None:
    if not isinstance(usage, dict):
        return None
    return {name: _int(usage.get(key)) for name, key in USAGE_FIELDS.items()}


def _processed(counters: dict[str, Any]) -> int | None:
    parts = [counters.get(name) for name in ("uncached_input", "cache_creation_input", "cached_input")]
    return None if any(p is None for p in parts) else sum(parts)


CODEX_PHASES = {"command_execution": "running a command", "file_change": "editing files",
                "reasoning": "reasoning", "agent_message": "responding", "mcp_tool_call": "tool call",
                "web_search": "web search", "todo_list": "planning"}
SERIES_CAP = 240


class Telemetry:
    """Measured state of one managed run, built only from its own stdout events.

    Every measurement carries the time it was measured: the event's own
    `timestamp` when the provider sends one, else the moment the recorder read
    the line (`measured_at_basis` says which). Nothing is extrapolated.
    """

    def __init__(self, provider: str, resumed: bool | None = False):
        self.provider = provider
        # Codex only: whether the run continues an earlier thread (None: argv
        # unknown). Only a fresh single-turn invocation's usage is authoritative.
        self.resumed = resumed
        self.events = 0
        self.last_event_at: str | None = None
        self.phase: str | None = None
        self.activity: dict[str, int] = {}
        self.models: list[str] = []
        self.messages: dict[str, dict[str, Any]] = {}
        # Running sums over `messages`, kept incrementally (a counter with any
        # unreported part is unknown, never a partial sum).
        self._sums = {name: 0 for name in USAGE_FIELDS}
        self._missing = {name: 0 for name in USAGE_FIELDS}
        self._changed = False
        self.context: dict[str, Any] | None = None
        self.final: dict[str, Any] | None = None  # authoritative counters, when reported
        self.final_turns = 0
        self.measured_at: str | None = None
        self.measured_basis: str | None = None
        self.series: list[dict[str, Any]] = []

    def _model(self, model: Any) -> None:
        if isinstance(model, str) and model and model not in self.models:
            self.models.append(model)

    def _count(self, name: str) -> None:
        self.activity[name] = self.activity.get(name, 0) + 1

    def _measured(self, event: dict[str, Any], read_at: str | None) -> None:
        stamp = event.get("timestamp") if isinstance(event.get("timestamp"), str) else None
        self.measured_at = stamp or read_at
        self.measured_basis = ("provider event timestamp" if stamp else
                               "recorder clock when the line was read" if read_at else None)

    def feed(self, event: dict[str, Any], read_at: str | None = None) -> bool:
        """Take one event; True when a measured counter changed."""
        self.events += 1
        stamp = event.get("timestamp") if isinstance(event.get("timestamp"), str) else None
        self.last_event_at = read_at or stamp or self.last_event_at
        self._changed = False
        if self.provider == "codex":
            self._feed_codex(event, read_at)
        else:
            self._feed_claude(event, read_at)
        if not self._changed:
            return False
        cumulative = self.cumulative()
        if "scope_unverified" in cumulative:
            # A later turn showed earlier points were not this invocation's own usage either.
            for point in self.series:
                point["processed_input"] = point["output"] = None
        sample = {"t": self.measured_at, "context_tokens": (self.context or {}).get("tokens"),
                  "processed_input": cumulative.get("processed_input"), "output": cumulative.get("output")}
        last = self.series[-1] if self.series else None
        if last and all(last[k] == sample[k] for k in ("context_tokens", "processed_input", "output")):
            return False  # same measurement repeated (one API message, several events)
        self.series.append(sample)
        if len(self.series) > SERIES_CAP:  # thin evenly, always keep the latest
            self.series = self.series[:-1:2] + self.series[-1:]
        return True

    def _feed_claude(self, event: dict[str, Any], read_at: str | None) -> None:
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            self._model(event.get("model"))
            self.phase = "started"
        elif kind == "assistant":
            message = event.get("message") or {}
            self._model(message.get("model"))
            for block in message.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    self.phase = f"tool: {block.get('name') or 'unknown'}"
                    self._count(f"tool:{block.get('name') or 'unknown'}")
                elif isinstance(block, dict) and block.get("type") in ("text", "thinking"):
                    self.phase = "responding" if block["type"] == "text" else "thinking"
            parts = _usage_parts(message.get("usage"))
            key = message.get("id")
            if parts is None or not key:
                return
            prior = self.messages.get(key)
            # One API message arrives as several events with the same usage;
            # output grows while it streams, so keep the largest seen.
            outputs = [v for v in ((prior or {}).get("output"), parts["output"]) if v is not None]
            parts["output"] = max(outputs) if outputs else None
            for name in USAGE_FIELDS:
                for counters, sign in ((prior, -1), (parts, 1)):
                    if counters is None:
                        continue
                    if counters[name] is None:
                        self._missing[name] += sign
                    else:
                        self._sums[name] += sign * counters[name]
            self.messages[key] = parts
            self._changed = True
            self._measured(event, read_at)
            if not event.get("parent_tool_use_id"):
                self.context = {"tokens": _processed(parts), "model": message.get("model"),
                                "measured_at": self.measured_at}
        elif kind == "user":
            self.phase = "tool result"
        elif kind == "result":
            self.phase = "error" if event.get("is_error") else "done"
            counters = _usage_parts(event.get("usage"))
            if counters:
                self.final = {**counters, "source": SRC_USAGE}
                self._measured(event, read_at)
                self._changed = True
            window = None
            model_usage = event.get("modelUsage") if isinstance(event.get("modelUsage"), dict) else {}
            for model in model_usage:
                self._model(model)
            if self.context and isinstance(model_usage.get(self.context.get("model")), dict):
                window = _int(model_usage[self.context["model"]].get("contextWindow"))
            if self.context is not None:
                self.context["window"] = window

    def _feed_codex(self, event: dict[str, Any], read_at: str | None) -> None:
        kind = event.get("type")
        self._model(event.get("model"))
        if kind == "thread.started":
            self.phase = "started"
        elif kind == "turn.started":
            self.phase = "turn started"
        elif kind in ("item.started", "item.updated", "item.completed"):
            item = event.get("item") if isinstance(event.get("item"), dict) else {}
            self.phase = CODEX_PHASES.get(str(item.get("type")), str(item.get("type") or "working"))
            if kind == "item.completed":
                self._count(f"item:{item.get('type') or 'unknown'}")
        elif kind == "turn.completed":
            self.final_turns += 1
            counters, _ = codex_counters(event.get("usage"))
            self.final = {**counters, "source": SRC_CODEX_USAGE}
            self.phase = "turn completed"
            self._measured(event, read_at)
            self._changed = True
        elif kind in ("turn.failed", "error"):
            self.phase = "error"

    def codex_scope(self) -> str | None:
        """Why Codex turn usage is not this invocation's own; None when it is."""
        if self.resumed is None:
            return "whether the thread was resumed is unknown (no argv)"
        if self.resumed:
            return "resumed thread: turn usage scope unverified"
        if self.final_turns > 1:
            return f"{self.final_turns} turn.completed events: scope across turns unverified"
        return None

    def cumulative(self) -> dict[str, Any]:
        if self.final is not None:
            counters = {k: v for k, v in self.final.items() if k != "source"}
            unverified = self.codex_scope() if self.provider == "codex" else None
            if unverified:
                # Keep what the provider reported, apart and labelled; the
                # invocation's own counters stay unknown, never zero.
                reported = {**counters, "processed_input": _processed(counters),
                            "turns": self.final_turns}
                return {**{name: None for name in counters}, "processed_input": None,
                        "provisional": None, "scope_unverified": reported,
                        "basis": f"unknown: {unverified}; latest {self.final['source']} kept apart"}
            basis = f"{self.final['source']}: as reported"
            provisional = False
        elif self.messages:
            counters = {name: None if self._missing[name] else self._sums[name] for name in USAGE_FIELDS}
            basis = ("running sum over distinct API messages (main thread and subagents) of "
                     "assistant-event usage; provisional until the result event")
            provisional = True
        else:
            return {"processed_input": None, "output": None, "provisional": None,
                    "basis": "not measured yet" + (" (Codex reports usage only at turn end)"
                                                     if self.provider == "codex" else "")}
        return {**counters, "processed_input": _processed(counters), "provisional": provisional,
                "basis": basis}

    def snapshot(self) -> dict[str, Any]:
        context: dict[str, Any] = {"tokens": None, "window": None, "percent": None, "model": None,
                                   "measured_at": None}
        if self.context:
            context.update(self.context)
            if context["tokens"] is not None and context.get("window"):
                context["percent"] = round(100 * context["tokens"] / context["window"], 1)
        context["basis"] = (
            "prompt size of the latest main-thread API message (uncached + cache creation + "
            "cache read of that one message); window from result.modelUsage.contextWindow"
            if self.provider == "claude" else
            "unknown: Codex exec JSON does not report context occupancy")
        return {
            "provider": self.provider,
            "events": self.events,
            "last_event_at": self.last_event_at,
            "phase": self.phase,
            "activity": dict(sorted(self.activity.items())),
            "models_seen": list(self.models),
            "last_measured_at": self.measured_at,
            "measured_at_basis": self.measured_basis,
            "context": context,
            "cumulative": self.cumulative(),
            "series": list(self.series),
        }


def codex_resumed(argv: list[str] | None) -> bool | None:
    """Whether a Codex argv continues an earlier thread; None when argv is unknown."""
    return None if argv is None else any(a in ("resume", "fork") for a in argv)


def telemetry_from_events(provider: str, events: list[dict[str, Any]],
                          resumed: bool | None = False) -> dict[str, Any]:
    telemetry = Telemetry(provider, resumed)
    for event in events:
        telemetry.feed(event)
    return telemetry.snapshot()


def detect_provider(events: list[dict[str, Any]]) -> str:
    kinds = {str(e.get("type")) for e in events}
    return "codex" if kinds & {"thread.started", "turn.started", "turn.completed"} else "claude"


def _codex_fields(events: list[dict[str, Any]], argv: list[str] | None,
                  exit_code: int | None) -> dict[str, Any]:
    """The Codex-specific parts of a record: tokens, session, model, outcome."""
    thread = next((e for e in events if e.get("type") == "thread.started"), None)
    turns = [e for e in events if e.get("type") == "turn.completed"]
    failed = any(e.get("type") in ("turn.failed", "error") for e in events)
    resumed = codex_resumed(argv)
    tokens: dict[str, Any] = {"source": None, "scope": INVOCATION_SCOPE,
                              **{name: None for name in USAGE_FIELDS}, "reasoning_output": None}
    if turns:
        tokens["provider_usage"] = turns[-1].get("usage")
    if not turns:
        check = "no turn.completed usage"
    elif resumed is not False:
        check = ("resumed thread: turn usage scope unverified" if resumed
                 else "whether the thread was resumed is unknown (no argv)")
    elif len(turns) > 1:
        check = f"{len(turns)} turn.completed events: scope across turns unverified"
    else:
        counters, reason = codex_counters(turns[0].get("usage"))
        tokens.update(counters, source=SRC_CODEX_USAGE)
        check = reason or "codex_usage_mapped"
    tokens["cross_check"] = check
    models = sorted({e["model"] for e in events if isinstance(e.get("model"), str)})
    if exit_code not in (None, 0) or failed:
        outcome = "error"
    elif turns:
        outcome = "success"
    else:
        outcome = "unknown"
    return {
        "tokens": tokens,
        "resumed": resumed,
        "session_id": (thread or {}).get("thread_id"),
        "reported": _known(models or None, "codex_exec.events.model") if models else
        {"value": None, "source": None, "unknown_reason": "Codex exec JSON reports no model identity"},
        "outcome": outcome,
        "outcome_source": SRC_CODEX_USAGE if turns else None,
    }


def build_record(
    *,
    task_id: str,
    run_id: str,
    role: str,
    model_requested: str | None,
    events: list[dict[str, Any]],
    exit_code: int | None,
    wall_duration_ms: int | None,
    started_at: str | None,
    ended_at: str | None,
    argv: list[str] | None,
    stream_path: str | None,
    api_key_env_present: bool | None,
    review_rounds: int | None = None,
    corrections: int | None = None,
    prior_result: dict[str, Any] | None = None,
    prior_result_path: str | None = None,
    provider: str = "claude",
    telemetry: dict[str, Any] | None = None,
    repository: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """`repository` (investigators only) says which commit the run read; omitted otherwise."""
    if provider == "codex":
        record = _build_codex_record(
            task_id=task_id, run_id=run_id, role=role, model_requested=model_requested,
            events=events, exit_code=exit_code, wall_duration_ms=wall_duration_ms,
            started_at=started_at, ended_at=ended_at, argv=argv, stream_path=stream_path,
            api_key_env_present=api_key_env_present, review_rounds=review_rounds,
            corrections=corrections, telemetry=telemetry,
        )
        if repository is not None:
            record["repository"] = repository
        return record
    init = next(
        (e for e in events if e.get("type") == "system" and e.get("subtype") == "init"),
        None,
    )
    result = last_result(events)

    reported: dict[str, Any] = {"value": None, "source": None}
    if result and isinstance(result.get("modelUsage"), dict) and result["modelUsage"]:
        # Identifies the model(s); the set is session-wide, like the counters.
        reported = {"value": sorted(result["modelUsage"]), "source": SRC_MODEL_USAGE,
                    "scope": CUMULATIVE_SCOPE}
    elif init and isinstance(init.get("model"), str):
        reported = _known([init["model"]], f"{SRC_INIT}.model")

    resumed = is_resume(argv)
    tokens = extract_tokens(result)
    estimated, cross_check = estimate_invocation_cost(result, prior_result, resumed)
    tokens["cross_check"] = cross_check

    if result is None:
        cli_outcome = "unknown"
    elif result.get("is_error") is True or (exit_code not in (None, 0)):
        cli_outcome = "error"
    elif result.get("subtype") == "success" and exit_code in (None, 0):
        cli_outcome = "success"
    else:
        cli_outcome = "error"

    record = {
        "schema": SCHEMA,
        "task_id": task_id,
        "run_id": run_id,
        "role": role,
        "model": {
            "requested": _known(model_requested, SRC_ARGV),
            "reported": reported,
        },
        "resumed_session": _known(resumed, SRC_ARGV),
        "prior_result": prior_result_path,
        "session_id": _known(
            (result or {}).get("session_id") or (init or {}).get("session_id"),
            SRC_RESULT,
        ),
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_ms": {
            "wall": _known(_int(wall_duration_ms), SRC_WALL),
            "reported": _known(_int((result or {}).get("duration_ms")), SRC_RESULT),
            "api_reported": _known(
                _int((result or {}).get("duration_api_ms")), SRC_RESULT
            ),
        },
        "num_turns": _known(_int((result or {}).get("num_turns")), SRC_RESULT),
        "tokens": tokens,
        "session_cumulative": extract_session_cumulative(result),
        "review_rounds": _known(_int(review_rounds), SRC_OPERATOR),
        "corrections": _known(_int(corrections), SRC_OPERATOR),
        "outcome": {
            "cli": {
                "value": cli_outcome,
                "source": SRC_RESULT if result else None,
                "exit_code": exit_code,
                "result_subtype": (result or {}).get("subtype"),
            },
            "review": {"value": None, "source": None},
        },
        "cost": {
            "estimated_list_cost_usd": estimated,
            "actual_provider_charge_usd": {"value": None, "source": None},
            "subscription_credits": {"value": None, "source": None},
        },
        "auth": {
            "api_key_source": _known(
                (init or {}).get("apiKeySource"), f"{SRC_INIT}.apiKeySource"
            ),
            "api_key_env_present": api_key_env_present,
        },
        "argv": redact_argv(argv) if argv else None,
        "stream_path": stream_path,
        "provider": "claude",
        "telemetry": telemetry if telemetry is not None else telemetry_from_events("claude", events),
    }
    if repository is not None:
        record["repository"] = repository
    record["unknowns"] = find_unknowns(record)
    return record


def unknown_repository(reason: str) -> dict[str, Any]:
    """An investigator record whose read commit was not observed: unknown, never inferred."""
    return {"source": None, "repository": None, "base_commit": None, "read_commit": None,
            "isolated_worktree": None, "worktree_clean_at_launch": None, "unknown_reason": reason}


def _build_codex_record(*, task_id: str, run_id: str, role: str, model_requested: str | None,
                        events: list[dict[str, Any]], exit_code: int | None,
                        wall_duration_ms: int | None, started_at: str | None, ended_at: str | None,
                        argv: list[str] | None, stream_path: str | None,
                        api_key_env_present: bool | None, review_rounds: int | None,
                        corrections: int | None, telemetry: dict[str, Any] | None) -> dict[str, Any]:
    """Same v2 shape as a Claude record; fields Codex exec does not report stay unknown."""
    codex = _codex_fields(events, argv, exit_code)
    unreported = {"value": None, "source": None}
    record = {
        "schema": SCHEMA,
        "task_id": task_id,
        "run_id": run_id,
        "role": role,
        "model": {"requested": _known(model_requested, SRC_ARGV), "reported": codex["reported"]},
        "resumed_session": _known(codex["resumed"], SRC_ARGV),
        "prior_result": None,
        "session_id": _known(codex["session_id"], SRC_CODEX_THREAD),
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_ms": {"wall": _known(_int(wall_duration_ms), SRC_WALL),
                        "reported": dict(unreported), "api_reported": dict(unreported)},
        "num_turns": dict(unreported),
        "tokens": codex["tokens"],
        "session_cumulative": {
            "scope": CUMULATIVE_SCOPE, "source": None, "by_model": None, "totals": None,
            "estimated_list_cost_usd": {"value": None, "source": None,
                                        "basis": "Codex exec reports no cost estimate"},
        },
        "review_rounds": _known(_int(review_rounds), SRC_OPERATOR),
        "corrections": _known(_int(corrections), SRC_OPERATOR),
        "outcome": {
            "cli": {"value": codex["outcome"], "source": codex["outcome_source"],
                    "exit_code": exit_code, "result_subtype": None},
            "review": {"value": None, "source": None},
        },
        "cost": {
            "estimated_list_cost_usd": {"value": None, "source": None, "method": None,
                                        "basis": "Codex exec reports no cost estimate",
                                        "unknown_reason": "Codex exec reports no cost estimate"},
            "actual_provider_charge_usd": {"value": None, "source": None},
            "subscription_credits": {"value": None, "source": None},
        },
        "auth": {"api_key_source": dict(unreported), "api_key_env_present": api_key_env_present},
        "argv": redact_argv(argv) if argv else None,
        "stream_path": stream_path,
        "provider": "codex",
        "telemetry": (telemetry if telemetry is not None
                      else telemetry_from_events("codex", events, codex["resumed"])),
    }
    record["unknowns"] = find_unknowns(record)
    return record


def find_unknowns(record: dict[str, Any]) -> list[str]:
    """Dotted paths of every `value`/counter the run did not establish."""
    unknown: list[str] = []

    def walk(node: Any, path: str) -> None:
        if not isinstance(node, dict):
            return
        if "value" in node and node["value"] is None:
            unknown.append(path)
        for key, child in node.items():
            if key in ("by_model", "argv", "telemetry", "provider_usage"):
                continue
            if isinstance(child, dict):
                walk(child, f"{path}.{key}" if path else key)

    walk(record, "")
    tokens = record.get("tokens") or {}
    for name in USAGE_FIELDS:
        if tokens.get(name) is None:
            unknown.append(f"tokens.{name}")
    return sorted(unknown)


def annotate(
    record: dict[str, Any],
    *,
    review_rounds: int | None = None,
    corrections: int | None = None,
    review_outcome: str | None = None,
    actual_charge_usd: float | None = None,
    actual_charge_source: str | None = None,
    subscription_credits: float | None = None,
    subscription_credits_source: str | None = None,
) -> dict[str, Any]:
    """Add what the CLI cannot know. Money fields need an explicit source."""
    if review_rounds is not None:
        record["review_rounds"] = _known(_int(review_rounds), SRC_OPERATOR)
    if corrections is not None:
        record["corrections"] = _known(_int(corrections), SRC_OPERATOR)
    if review_outcome is not None:
        record["outcome"]["review"] = _known(review_outcome, SRC_OPERATOR)
    for value, source, key in (
        (actual_charge_usd, actual_charge_source, "actual_provider_charge_usd"),
        (subscription_credits, subscription_credits_source, "subscription_credits"),
    ):
        if value is None:
            continue
        if not source:
            raise RefusedError(f"{key} needs an explicit source (invoice, console...)")
        record["cost"][key] = {"value": _num(value), "source": f"{SRC_OPERATOR}: {source}"}
    record["unknowns"] = find_unknowns(record)
    return record


# --- commands ---------------------------------------------------------------


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


def _records_dir(value: str | None) -> Path:
    return Path(value or os.environ.get(RECORDS_DIR_ENV) or DEFAULT_RECORDS_DIR)


def _write(record: dict[str, Any], path: Path) -> None:
    """Write via a sibling temp file and rename, so readers never see a partial record."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    tmp.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _default_run_id(task_id: str, role: str) -> str:
    # Timestamp for humans, random suffix for uniqueness: two runs started in
    # the same second (sequential or concurrent) never share an id.
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    return f"{task_id}-{role}-{stamp}-{secrets.token_hex(4)}"


def _reserve(records: Path, run_id: str) -> tuple[Path, Path]:
    """Atomically claim `<run_id>.json` and `<run_id>.stream.jsonl`.

    O_EXCL makes the claim exclusive across processes: a second run with the
    same id fails here and never starts `claude`, never truncates or shares
    the first run's stream. Raises FileExistsError on any collision.
    """
    record_path = records / f"{run_id}.json"
    stream_path = records / f"{run_id}.stream.jsonl"
    fd = os.open(record_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump({"schema": SCHEMA, "run_id": run_id, "status": "reserved",
                   "reserved_at": _now()}, handle)
    try:
        fd = os.open(stream_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        record_path.unlink()  # ours, just created; leave the stray stream alone
        raise
    os.close(fd)
    return record_path, stream_path


def cmd_run(args: argparse.Namespace) -> int:
    claude_argv = list(args.claude_argv)
    if claude_argv and claude_argv[0] == "--":
        claude_argv = claude_argv[1:]
    env = dict(os.environ)
    investigator = args.role == INVESTIGATOR_ROLE
    repository: dict[str, Any] | None = None
    try:
        model = check_invocation(claude_argv, args.role, env, Path.cwd())
        if investigator:
            check_investigator(claude_argv)
            repository = resolve_investigator_base(
                Path(args.project_root) if args.project_root else Path.cwd(), args.base_commit, args.task_id,
                Path(args.workflow_dir) if args.workflow_dir else None)
        elif args.base_commit or args.project_root:
            raise RefusedError("--base-commit and --project-root apply only to investigator runs")
        prior = load_prior_result(args.prior_result) if args.prior_result else None
    except RefusedError as exc:
        print(f"direct_run_recorder: refused: {exc}", file=sys.stderr)
        return 2

    run_id = (
        args.run_id if args.run_id is not None else _default_run_id(args.task_id, args.role)
    )
    if not RUN_ID_RE.match(run_id):
        print(
            f"direct_run_recorder: refused: run id {run_id!r} must match "
            f"{RUN_ID_RE.pattern}",
            file=sys.stderr,
        )
        return 2
    provider = provider_of(claude_argv) or "claude"
    state: dict[str, Any] = {"provider": provider}  # what an interrupted investigator reports

    def reserve_and_run() -> int:
        records = _records_dir(args.records_dir)
        if investigator:  # the record stays in the original directory, whatever cwd the run gets
            records = records.expanduser().resolve()
        records.mkdir(parents=True, exist_ok=True)
        try:
            record_path, stream_path = _reserve(records, run_id)
        except FileExistsError:
            print(
                f"direct_run_recorder: refused: run id {run_id!r} already exists in "
                f"{records}; nothing was started",
                file=sys.stderr,
            )
            return 2

        if provider == "claude":
            # Point release-settings.json's hook at this package's guard, whatever
            # the caller's environment held (see GUARD_ENV).
            env[GUARD_ENV] = str(GUARD_PATH)
        telemetry = Telemetry(provider, codex_resumed(claude_argv) if provider == "codex" else False)
        started_at, start = _now(), time.monotonic()
        extra = {"repository": repository} if repository is not None else {}
        state.update(record_path=record_path, stream_path=stream_path, started_at=started_at,
                     telemetry=telemetry)

        def write_running() -> None:
            _write({"schema": SCHEMA, "run_id": run_id, "task_id": args.task_id, "role": args.role,
                    "provider": provider, "status": "running", "started_at": started_at,
                    "model": {"requested": _known(model, SRC_ARGV)},
                    "recorder": {"pid": os.getpid(), "host": socket.gethostname()},
                    "snapshot_at": _now(), "telemetry": telemetry.snapshot(), **extra}, record_path)

        def launch_failed(error: str) -> None:
            _write({"schema": SCHEMA, "run_id": run_id, "task_id": args.task_id,
                    "role": args.role, "provider": provider, "status": "launch_failed",
                    "error": error, "started_at": started_at, **extra}, record_path)

        with contextlib.ExitStack() as stack:
            cwd = None
            if repository is not None:
                try:
                    cwd = stack.enter_context(investigator_worktree(Path(repository["repository"]),
                                                                    repository["base_commit"], state))
                    verify_investigator_worktree(cwd, repository)
                except RefusedError as exc:
                    launch_failed(f"isolation: {exc}")
                    print(f"direct_run_recorder: refused: {exc}; nothing was started", file=sys.stderr)
                    return 2
            return _launch(args, claude_argv, env, cwd, provider, model, prior, run_id, record_path,
                           stream_path, telemetry, started_at, start, write_running, launch_failed, repository,
                           state if investigator else None)

    if not investigator:
        return reserve_and_run()
    # An investigator owns a child and a worktree: SIGTERM/SIGHUP (like Ctrl-C) stop the child,
    # remove the worktree and leave an `interrupted` record rather than a stale `running` one.
    with unwind_on_signals(state=state):
        try:
            return reserve_and_run()
        except (KeyboardInterrupt, Terminated) as exc:
            return _record_interrupted(args, run_id, repository, state, exc)


def _record_interrupted(args: argparse.Namespace, run_id: str, repository: dict[str, Any] | None,
                        state: dict[str, Any], exc: BaseException) -> int:
    """By now the investigator's child is stopped and its worktree context has exited; record that."""
    signum = exc.signum if isinstance(exc, Terminated) else signal.SIGINT
    name = signal.Signals(signum).name
    worktree = state.get("worktree")
    removed = None  # no worktree was chosen yet
    if worktree is not None and repository is not None:
        removed = not worktree.exists() and worktree_registered(Path(repository["repository"]), worktree) is False
    try:  # a signal after the final record was written must not replace it
        status = json.loads(state["record_path"].read_text(encoding="utf-8")).get("status")
    except (KeyError, OSError, ValueError, AttributeError):
        status = None
    if "record_path" in state and status in (None, "reserved", "running"):
        _write({"schema": SCHEMA, "run_id": run_id, "task_id": args.task_id, "role": args.role,
                "provider": state["provider"], "status": "interrupted", "interrupted_by": name,
                "started_at": state["started_at"], "ended_at": _now(),
                "exit_code": getattr(exc, "child_exit", None), "stream_path": str(state["stream_path"]),
                "telemetry": state["telemetry"].snapshot(), "repository": repository,
                "isolated_worktree_removed": removed}, state["record_path"])
    print(f"direct_run_recorder: interrupted by {name}; investigator stopped"
          + ("" if removed is None else f", isolated worktree {'removed' if removed else 'NOT removed'}"),
          file=sys.stderr)
    return 128 + signum


STOP_GRACE_S = 5.0


def _stop(process: subprocess.Popen) -> int:
    """Terminate, then kill after a short grace; always waits, returns the exit code."""
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=STOP_GRACE_S)
        except subprocess.TimeoutExpired:
            process.kill()
    return process.wait()


def _launch(args: argparse.Namespace, claude_argv: list[str], env: dict[str, str], cwd: Path | None,
            provider: str, model: str, prior: dict[str, Any] | None, run_id: str, record_path: Path,
            stream_path: Path, telemetry: Telemetry, started_at: str, start: float, write_running,
            launch_failed, repository: dict[str, Any] | None, signal_state=None) -> int:
    """Start the reserved run (in `cwd` when isolated), tee its stdout and write its record."""
    # Append to the stream file this process reserved (created empty above).
    with open(stream_path, "a", encoding="utf-8") as sink:
        process = thread = done = None
        try:
            if signal_state is not None:
                signal_state["launching_process"] = True
            try:
                process = subprocess.Popen(
                    claude_argv, stdout=subprocess.PIPE, text=True, env=env, cwd=cwd
                )
            except OSError as exc:
                launch_failed(f"{type(exc).__name__}: {exc.strerror or exc}")
                print(f"direct_run_recorder: could not start {provider}: {exc}", file=sys.stderr)
                return 127
            finally:
                if signal_state is not None:
                    signal_state["launching_process"] = False
                    pending = signal_state.pop("pending_signal", None)
                    if pending is not None:
                        raise Terminated(pending)
            lock, done, dirty = threading.Lock(), threading.Event(), threading.Event()

            def flusher() -> None:
                # At most one snapshot per interval, and only after something changed.
                while not done.wait(LIVE_INTERVAL_S):
                    if dirty.is_set():
                        with lock:
                            dirty.clear()
                            write_running()

            with lock:
                write_running()
            thread = threading.Thread(target=flusher, daemon=True)
            thread.start()
            assert process.stdout is not None
            for line in process.stdout:
                sink.write(line)
                sink.flush()
                sys.stdout.write(line)
                sys.stdout.flush()
                with lock:
                    for event in iter_events(line):
                        telemetry.feed(event, _now())
                dirty.set()
            exit_code = process.wait()
        except BaseException as exc:
            # Interrupted: an isolated run must be gone before its worktree is removed.
            if cwd is not None and process is not None:
                exc.child_exit = _stop(process)
            raise
        finally:
            if done is not None:
                done.set()
            if thread is not None and thread.is_alive():
                thread.join()
    wall_ms = int((time.monotonic() - start) * 1000)

    record = build_record(
        task_id=args.task_id,
        run_id=run_id,
        role=args.role,
        model_requested=model,
        events=list(iter_events(stream_path.read_text(encoding="utf-8"))),
        exit_code=exit_code,
        wall_duration_ms=wall_ms,
        started_at=started_at,
        ended_at=_now(),
        argv=claude_argv,
        stream_path=str(stream_path),
        api_key_env_present=False,
        review_rounds=args.review_rounds,
        corrections=args.corrections,
        prior_result=prior,
        prior_result_path=args.prior_result,
        provider=provider,
        telemetry=telemetry.snapshot(),
        repository=repository,
    )
    _write(record, record_path)
    print(f"direct_run_recorder: record {record_path}", file=sys.stderr)
    return exit_code


def cmd_parse(args: argparse.Namespace) -> int:
    stream = Path(args.stream)
    try:
        prior = load_prior_result(args.prior_result) if args.prior_result else None
    except RefusedError as exc:
        print(f"direct_run_recorder: refused: {exc}", file=sys.stderr)
        return 2
    events = list(iter_events(stream.read_text(encoding="utf-8")))
    provider = args.provider if args.provider != "auto" else detect_provider(events)
    if provider == "codex" and args.role in GUARDED_ROLES:
        print(f"direct_run_recorder: refused: a Codex run cannot be recorded as {args.role}",
              file=sys.stderr)
        return 2
    record = build_record(
        provider=provider,
        task_id=args.task_id,
        run_id=args.run_id or stream.name.split(".")[0],
        role=args.role,
        model_requested=args.model,
        events=events,
        exit_code=args.exit_code,
        wall_duration_ms=None,
        started_at=None,
        ended_at=None,
        argv=None,
        stream_path=str(stream),
        api_key_env_present=None,
        review_rounds=args.review_rounds,
        corrections=args.corrections,
        prior_result=prior,
        prior_result_path=args.prior_result,
        repository=unknown_repository(
            "imported from a captured stream: the commit this investigator read was not observed")
        if args.role == INVESTIGATOR_ROLE else None,
    )
    out = (
        Path(args.out)
        if args.out
        else _records_dir(args.records_dir) / f"{record['run_id']}.json"
    )
    _write(record, out)
    print(str(out))
    return 0


def cmd_annotate(args: argparse.Namespace) -> int:
    path = Path(args.record)
    record = json.loads(path.read_text(encoding="utf-8"))
    try:
        annotate(
            record,
            review_rounds=args.review_rounds,
            corrections=args.corrections,
            review_outcome=args.review_outcome,
            actual_charge_usd=args.actual_charge_usd,
            actual_charge_source=args.actual_charge_source,
            subscription_credits=args.subscription_credits,
            subscription_credits_source=args.subscription_credits_source,
        )
    except RefusedError as exc:
        print(f"direct_run_recorder: refused: {exc}", file=sys.stderr)
        return 2
    _write(record, path)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="direct_run_recorder.py")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--task-id", required=True)
        p.add_argument("--role", required=True, choices=ROLES)
        p.add_argument("--run-id")
        p.add_argument("--records-dir")
        p.add_argument("--review-rounds", type=int)
        p.add_argument("--corrections", type=int)
        p.add_argument(
            "--prior-result",
            help="captured json/stream-json output of the previous invocation "
            "of the same session; enables a checked per-invocation cost delta",
        )

    run = sub.add_parser("run", help="run `claude ...` or `codex exec --json ...` unchanged and record it")
    common(run)
    run.add_argument(
        "--base-commit",
        help="investigator only (required there): the commit SHA it reads; the run starts in a clean "
        "detached worktree of that commit outside the writer's checkout, removed afterwards",
    )
    run.add_argument(
        "--project-root",
        help="investigator only: the project whose Git repository holds --base-commit "
        "(default: the current directory; `nlh record` passes the selected project)",
    )
    run.add_argument(
        "--workflow-dir",
        help="where the task's ticket/graph live, to check a declared base_commit "
        "(default: TASK_WORKFLOW_DIR or <repository>/.workflow)",
    )
    run.add_argument("claude_argv", nargs=argparse.REMAINDER)
    run.set_defaults(func=cmd_run)

    parse = sub.add_parser("parse", help="record from a captured json/stream-json file")
    common(parse)
    parse.add_argument("--model", help="model requested on the original argv")
    parse.add_argument("--stream", required=True)
    parse.add_argument("--exit-code", type=int)
    parse.add_argument("--provider", choices=("auto", *PROVIDERS), default="auto",
                       help="default: codex when the stream holds Codex exec events, else claude")
    parse.add_argument("--out")
    parse.set_defaults(func=cmd_parse)

    ann = sub.add_parser("annotate", help="add review/correction/charge facts")
    ann.add_argument("--record", required=True)
    ann.add_argument("--review-rounds", type=int)
    ann.add_argument("--corrections", type=int)
    ann.add_argument("--review-outcome")
    ann.add_argument("--actual-charge-usd", type=float)
    ann.add_argument("--actual-charge-source")
    ann.add_argument("--subscription-credits", type=float)
    ann.add_argument("--subscription-credits-source")
    ann.set_defaults(func=cmd_annotate)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
