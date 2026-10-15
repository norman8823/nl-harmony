"""`nlh` — Harmony's command line: companion, task inspection and approvals.

    nlh companion [--no-browser] [--port N]
    nlh inspect <task-id>
    nlh record --task-id ID --role ROLE [--base-commit SHA] -- claude ... | codex exec --json ...
    nlh approval request|decide|execute|show <task-id> ...   revision-bound human approvals
    nlh task relate <task-id> --follow-up-of <id> | nlh task resolve [--task-id ID]
    nlh task archive|restore <task-id> [--expected-revision N]   board visibility only
    nlh task list [--all] [--json]
    nlh task start [--task-id ID | --new] [--title TITLE] [--source codex|terminal]
                   [--external-id KEY] [--json] [-- COMMAND ...]

Every command resolves one project: `--config <harmony.json>`, else the nearest
`harmony.json` at or above `--project` (default: the current directory), else
that directory with default paths. Paths in the config are relative to the
config file's directory. The commands reuse the bundled scripts; nothing here
schedules, publishes or authorizes anything.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import importlib.util
import json
import os
import subprocess
import sys
import webbrowser
from pathlib import Path
from typing import Any

from . import __version__

PACKAGE = Path(__file__).resolve().parent
WORKFLOW_PKG = PACKAGE / "direct-workflow"
BOARD_PKG = PACKAGE / "task-board"
CONFIG_NAME = "harmony.json"

if str(WORKFLOW_PKG) not in sys.path:
    sys.path.insert(0, str(WORKFLOW_PKG))


class ConfigError(Exception):
    pass


def find_config(start: Path) -> Path | None:
    for directory in (start, *start.parents):
        candidate = directory / CONFIG_NAME
        if candidate.is_file():
            return candidate
    return None


def resolve_project(project: Path | None, config: Path | None) -> dict[str, Any]:
    start = (project or Path.cwd()).resolve()
    path = config.resolve() if config else find_config(start)
    raw: dict[str, Any] = {}
    root = start
    if path is not None:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ConfigError(f"cannot read {path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError(f"{path}: expected a JSON object")
        root = path.parent

    def rel(value: Any, default: str | None) -> Path | None:
        if value is None:
            value = default
        if value is None:
            return None
        if not isinstance(value, str) or not value:
            raise ConfigError("paths in harmony.json must be non-empty strings")
        candidate = Path(value).expanduser()
        return (candidate if candidate.is_absolute() else root / candidate).resolve()

    roles = raw.get("roles") or {}
    if not isinstance(roles, dict):
        raise ConfigError("roles must be an object")
    board = raw.get("board") or {}
    return {
        "config": str(path) if path else None,
        "root": root,
        "workflow_dir": rel(raw.get("workflow_dir"), ".workflow"),
        "runs_dir": rel(raw.get("runs_dir"), ".direct-runs"),
        "roles": roles,
        "port": int(board.get("port", 8765)),
    }


def _load_board_server():
    spec = importlib.util.spec_from_file_location("harmony_task_board_server", BOARD_PKG / "server.py")
    if spec is None or spec.loader is None:  # pragma: no cover - packaging error
        raise ConfigError("task board is missing from this installation")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# -- commands ------------------------------------------------------------------


def cmd_companion(project: dict[str, Any], args: argparse.Namespace) -> int:
    server = _load_board_server()
    (project["workflow_dir"] / "tickets").mkdir(parents=True, exist_ok=True)
    port = project["port"] if args.port is None else args.port
    try:
        httpd = server.build_server(
            project["root"], project["workflow_dir"], port=port, session="nlh-companion", actor="ui",
            runs_dir=project["runs_dir"],
        )
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            raise ConfigError(f"Companion port {port} is already in use. Stop the existing Companion "
                              "with Ctrl+C in its terminal, then restart using the same board directory. "
                              "To identify the listener on macOS/Linux, run: "
                              f"lsof -nP -iTCP:{port} -sTCP:LISTEN") from None
        raise
    httpd.url = f"http://127.0.0.1:{httpd.server_address[1]}"
    print(f"Harmony companion on {httpd.url}  (Ctrl-C to stop)", flush=True)
    print(f"  project:     {project['root']}", flush=True)
    print(f"  config:      {project['config'] or 'none (defaults)'}", flush=True)
    print(f"  workflow:    {project['workflow_dir']}", flush=True)
    print(f"  run records: {project['runs_dir']}", flush=True)
    for role, value in sorted(project["roles"].items()):
        print(f"  role {role}: {json.dumps(value, sort_keys=True)}", flush=True)
    if not args.no_browser:
        webbrowser.open(httpd.url)
    try:
        from .codex_bridge import advertise
        with advertise(httpd, project):
            httpd.start_quota()  # weekly allowance is collected from startup
            httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
    return 0


def cmd_codex(args: argparse.Namespace) -> int:
    from . import codex_bridge

    home = (args.codex_home or Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))).expanduser()
    try:
        if args.codex_cmd == "install":
            probe = subprocess.run(["codex", "features", "list"], capture_output=True, text=True, timeout=10)
            if probe.returncode or not any(line.split()[:1] == ["hooks"] for line in probe.stdout.splitlines()):
                raise ConfigError("this Codex CLI does not expose supported lifecycle hooks; update Codex first. "
                                  "No configuration was changed")
            path = codex_bridge.install(home)
            print(f"Harmony prompt hook installed in {path}. Restart existing Codex sessions once. "
                  "With Companion running, new Codex prompts receive task intake automatically.")
        else:
            installed = codex_bridge.is_installed(home)
            active = codex_bridge.active_companions()
            print(json.dumps({"hook_installed": installed, "active_boards": [
                {"project": item["project"], "workflow_dir": item["workflow_dir"], "port": item["port"]}
                for item in active], "ready": installed and len(active) == 1}))
        return 0
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        raise ConfigError(f"Codex connection: {exc}") from exc




def cmd_inspect(project: dict[str, Any], args: argparse.Namespace) -> int:
    import run_observability
    import task_checkpoint
    import task_graph

    task_id = args.task_id
    workflow = project["workflow_dir"]
    common = ["--task-id", task_id, "--project-root", str(project["root"]), "--workflow-dir", str(workflow)]
    graph_dirs: list[str] = []
    graph_dir, checkpoint_dir = workflow / "graphs", workflow / "checkpoints"
    checkpoint_dirs: list[str] = []
    records, _ = run_observability.load_task_records(project["runs_dir"], task_id) \
        if project["runs_dir"].is_dir() else ([], [])
    recorded = [workflow / "tickets" / f"{task_id}.json", checkpoint_dir / f"{task_id}.json",
                graph_dir / f"{task_id}.json"]
    if not records and not any(path.is_file() for path in recorded):
        print(f"nlh inspect: nothing recorded for task {task_id!r} under {workflow}", file=sys.stderr)
        return 1
    sections = [
        ("checkpoint", task_checkpoint.main, ["bootstrap", *common, *checkpoint_dirs, *graph_dirs]),
        ("graph", task_graph.main,
         ["status", *common, *graph_dirs, "--records-dir", str(project["runs_dir"])]),
        (f"runs (direct run records in {project['runs_dir']})",
         run_observability.main,
         ["summarize-task", "--task-id", task_id, "--records-dir", str(project["runs_dir"])]),
    ]
    for title, main, argv in sections:
        print(f"== {title}", flush=True)
        with contextlib.suppress(SystemExit):
            main(argv)
        sys.stdout.flush()
        print()
    return 0


def cmd_record(project: dict[str, Any], args: argparse.Namespace) -> int:
    """Run one managed `claude`/`codex exec` invocation through the recorder.

    Exactly the recorder's `run`, with this project's run-record directory, so
    the companion shows the run live. Every recorder refusal (explicit model,
    machine output, explicit-model Claude writer behind the PR guard, no API-key env,
    read-only investigator with an explicit --base-commit) applies. An investigator's
    base commit is resolved in this project's repository (`--project-root`), never
    in the current directory's.
    """
    import direct_run_recorder

    _check_record_task(project, args.task_id)
    argv = ["run", "--task-id", args.task_id, "--role", args.role,
            "--records-dir", str(project["runs_dir"])]
    if args.run_id:
        argv += ["--run-id", args.run_id]
    if args.prior_result:
        argv += ["--prior-result", args.prior_result]
    if args.base_commit:
        argv += ["--base-commit", args.base_commit]
    if args.role == direct_run_recorder.INVESTIGATOR_ROLE:  # the base commit is resolved in this project
        argv += ["--project-root", str(project["root"]), "--workflow-dir", str(project["workflow_dir"])]
    command = list(args.wrapped)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise ConfigError("give the command to record after `--`, e.g. `-- codex exec --json -m MODEL -`")
    return direct_run_recorder.main(argv + ["--", *command])


def _check_record_task(project: dict[str, Any], task_id: str) -> None:
    """A recorded run continues an explicitly named task; nothing here creates or guesses one.

    A numeric id is a canonical task and its ticket must exist -- also when the
    project has no tickets directory at all. A plain non-numeric id is
    a legacy standalone record and is kept working. Anything else is refused.
    """
    import task_checkpoint

    if not task_checkpoint.TASK_ID_RE.match(task_id) or len(task_id) > 128:
        raise ConfigError(f"unsafe task id {task_id!r}: use an existing task's id")
    tickets = project["workflow_dir"] / "tickets"
    if task_id.isdigit() and not (tickets / f"{task_id}.json").is_file():
        raise ConfigError(
            f"Task #{task_id} does not exist in {tickets}. A run, review or correction continues an "
            "existing task by its explicit id; a new task is created only explicitly (Add task in the "
            "companion). Nothing was recorded.")


def _store():
    if str(BOARD_PKG) not in sys.path:
        sys.path.insert(0, str(BOARD_PKG))
    import store

    return store


def _now() -> str:
    import datetime as dt

    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def cmd_approval(project: dict[str, Any], args: argparse.Namespace) -> int:
    """Record or decide a revision-bound approval request; the same store call the companion makes."""
    store = _store()
    board = store.Board(project["workflow_dir"])
    try:
        if args.action_cmd == "request":
            result = board.request_approval(
                args.task_id, action=args.action, gate=args.gate, summary=args.summary, evidence=args.evidence,
                content_ref=args.content_ref, resolves_blocker=args.resolves_blocker, actor=args.by,
                now=_now(), via="cli")
            if args.json:
                print(json.dumps({k: v for k, v in result.items() if k != "card"}, indent=2))
            else:
                if not result["created"]:
                    print("(This exact request is already pending; nothing was written.)")
                for old in result.get("superseded") or []:
                    print(f"(Superseded stale request {old}; it stays in the ticket's history.)")
                print(result["offer"])
            return 0
        if args.action_cmd == "decide":
            decision = "approve" if args.approve else "request_changes" if args.request_changes else "defer"
            result = board.decide(
                args.task_id, request_id=args.request, action=args.action, gate=args.gate, binding=args.binding,
                decision=decision, note=args.note, actor=args.by, now=_now(), via="cli")
            if args.json:
                print(json.dumps({k: v for k, v in result.items() if k != "card"}, indent=2))
            else:
                request = result["request"]
                print(f"Task #{request['task_id']}: {request['id']} {request['state']} "
                      f"({request['action']} at gate {request['gate_id']}, binding {request['binding_token']})")
                for line in result["performed"]:
                    print(f"  recorded: {line}")
                print(result["next_step"])
            return 0
        if args.action_cmd == "execute":
            result = board.record_execution(
                args.task_id, request_id=args.request, action=args.action, binding=args.binding,
                outcome="succeeded" if args.succeeded else "failed", evidence=args.evidence,
                result_ref=args.result_ref, note=args.note, actor=args.by, now=_now(), via="cli")
            if args.json:
                print(json.dumps({k: v for k, v in result.items() if k != "card"}, indent=2))
            else:
                execution = result["execution"]
                print(f"Task #{result['request']['task_id']}: {execution['action']} {execution['outcome']} "
                      f"recorded on {execution['request_id']}" + (f" ({execution['result_ref']})"
                                                                  if execution["result_ref"] else ""))
                print(result["next_step"])
            return 0
        card = board.card(store.parse_task_id(args.task_id))
        if args.json:
            print(json.dumps(card["approvals"], indent=2))
            return 0
        if not card["approvals"]:
            print(f"Task #{card['id']}: no approval requests")
        for request in card["approvals"]:
            print(f"{request['id']}  {request['effective_state']}  {request['action']} at gate {request['gate_id']}"
                  f"  binding {request['binding_token']}")
            if request["effective_state"] == "pending":
                print(store.approval_offer(card["id"], card["ticket_title"], request))
        return 0
    except store.StoreError as exc:
        print(f"nlh approval: {exc.message}", file=sys.stderr)
        return 1 if exc.code == 404 else 2


def cmd_task(project: dict[str, Any], args: argparse.Namespace) -> int:
    """Explicit task identity: relate a follow-up to its earlier task, or resolve which task is meant."""
    store = _store()
    board = store.Board(project["workflow_dir"])
    try:
        if args.task_cmd == "list":
            tasks = []
            for task_id in board.ticket_ids():
                ticket = board.read_ticket(task_id)
                if not args.all and ticket.get("status") == "DONE":
                    continue
                tasks.append({"task_id": task_id, "title": ticket.get("title"),
                              "status": ticket.get("status"), "scope": ticket.get("scope"),
                              "lifecycle": ticket.get("lifecycle")})
            if args.json:
                print(json.dumps({"ok": True, "tasks": tasks}))
            else:
                for item in tasks:
                    print(f"#{item['task_id']} [{item['status']}] {item['title']}")
            return 0
        if args.task_cmd == "start":
            given = args.task_id or (None if args.new else os.environ.get("HARMONY_TASK_ID"))
            source = args.source or ("codex" if os.environ.get("CODEX_THREAD_ID") else "terminal")
            external_id = args.external_id
            # An explicit task wins over session context: one thread can work
            # on several distinct problems. Only bind a supplied external id
            # when the caller has already selected the task.
            if external_id is None and source == "codex" and not args.new and not given:
                external_id = os.environ.get("CODEX_THREAD_ID")
            command = list(args.wrapped)
            if command and command[0] == "--":
                command.pop(0)
            if args.json and command:
                raise ConfigError("--json cannot be combined with a wrapped command")
            # Do not bypass a project's custom intake allocator with a local allocation.
            if not given and _load_board_server().intake_path(project["root"]).is_file():
                raise ConfigError("this project uses an intake allocator; create through Companion first, "
                                  "then pass --task-id")
            card, created = board.start_work(
                {"title": args.title, "details": args.details}, source=source, external_id=external_id,
                task_id=store.parse_task_id(given) if given else None, actor="nlh-start", now=_now())
            result = {"ok": True, "task_id": card["id"], "created": created,
                      "status": card["status"], "title": card["ticket_title"]}
            if args.json:
                print(json.dumps(result))
            else:
                print(f"{'Created' if created else 'Reusing'} Task #{card['id']} "
                      f"[{card['status']}] {card['ticket_title']}", flush=True)
            if command:
                try:
                    result_code = subprocess.run(command, cwd=project["root"],
                                                 env={**os.environ, "HARMONY_TASK_ID": str(card["id"])}).returncode
                except OSError as exc:
                    raise ConfigError(f"Task #{card['id']} retained; cannot launch command: {exc}") from exc
                return result_code if result_code >= 0 else 128 - result_code
            return 0
        if args.task_cmd == "relate":
            card = board.relate(args.task_id, follow_up_of=args.follow_up_of, actor=args.by, now=_now(),
                                note=args.note)
            print(f"Task #{card['id']} is recorded as a follow-up of Task #{args.follow_up_of}; "
                  f"Task #{args.follow_up_of} itself is unchanged.")
            return 0
        if args.task_cmd in ("archive", "restore"):
            task_id = store.parse_task_id(args.task_id)
            revision = args.expected_revision
            if revision is None:
                revision = board.card(task_id)["board"]["revision"]
            card = getattr(board, args.task_cmd)(task_id, expected_revision=revision, actor=args.by, now=_now())
            print(f"Task #{card['id']} {'archived' if args.task_cmd == 'archive' else 'restored to the board'} "
                  f"(revision {card['board']['revision']}); its ticket, id, history and graph are unchanged.")
            return 0
        # resolve: an explicit id (or HARMONY_TASK_ID), never a guess or a text match.
        given = args.task_id or os.environ.get("HARMONY_TASK_ID")
        if not given:
            open_ids = [(i, board.read_ticket(i)) for i in board.ticket_ids()]
            print("nlh task resolve: no task id given and none in HARMONY_TASK_ID. Ask which task this "
                  "work continues; nothing is guessed. Open tasks:", file=sys.stderr)
            for task_id, ticket in open_ids:
                if ticket.get("status") != "DONE":
                    print(f"  #{task_id} [{ticket.get('status')}] {ticket.get('title')}", file=sys.stderr)
            return 2
        card = board.card(store.parse_task_id(given))
        print(f"Task #{card['id']} [{card['status']}] {card['ticket_title']}")
        return 0
    except store.StoreError as exc:
        print(f"nlh task: {exc.message}", file=sys.stderr)
        return 1 if exc.code == 404 else 2






def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nlh", description="Harmony by NL Intelligence")
    parser.add_argument("--version", action="version", version=f"nlh {__version__}")
    parser.add_argument("--project", type=Path, default=None, help="project directory (default: cwd)")
    parser.add_argument("--config", type=Path, default=None, help=f"explicit {CONFIG_NAME}")
    sub = parser.add_subparsers(dest="command", required=True)

    comp = sub.add_parser("companion", help="start the local board for this project and open it")
    comp.add_argument("--port", type=int, default=None, help="default: board.port or 8765; 0 picks a free port")
    comp.add_argument("--no-browser", action="store_true", help="do not open a browser")

    codex = sub.add_parser("codex", help="connect ordinary Codex prompts to the running Companion")
    codex_sub = codex.add_subparsers(dest="codex_cmd", required=True)
    for name in ("install", "status"):
        connection = codex_sub.add_parser(name)
        connection.add_argument("--codex-home", type=Path, help="default: CODEX_HOME or ~/.codex")

    ins = sub.add_parser("inspect", help="checkpoint, graph and run summary for one task")
    ins.add_argument("task_id")

    import direct_run_recorder

    rec = sub.add_parser(
        "record", help="run one claude/codex invocation and record it live for the companion",
        epilog=("Writer and correction runs must be explicit-model Claude runs that load the PR guard: "
                f"--settings {direct_run_recorder.RELEASE_SETTINGS}. Codex runs record as planner, reviewer, "
                "investigator or other. Investigator runs need --base-commit SHA (a commit of this project's "
                "repository) and a read-only argv: Codex `-s read-only`; Claude `--restricted --tools "
                "Read,Grep,Glob`, `--disallowedTools "
                f"{','.join(direct_run_recorder.CLAUDE_DENIED_TOOLS)}`, `--strict-mcp-config --mcp-config "
                "'{\"mcpServers\":{}}'`, `--settings '{\"disableAllHooks\":true}' --setting-sources ''`, "
                "no --add-dir. Configured git filter commands are refused."))
    rec.add_argument("--task-id", required=True)
    rec.add_argument("--role", required=True, choices=direct_run_recorder.ROLES,
                     help="writer = Implementer; correction = Correction")
    rec.add_argument("--run-id")
    rec.add_argument("--prior-result", help="previous result of the same resumed Claude session")
    rec.add_argument("--base-commit", help="investigator only (required): a commit SHA of the selected "
                     "project's repository, checked out as the run's clean temporary worktree outside this "
                     "checkout; never guessed from HEAD")
    rec.add_argument("wrapped", nargs=argparse.REMAINDER, metavar="-- COMMAND ...")

    appr = sub.add_parser("approval", help="request or decide a revision-bound human approval for one task")
    appr_sub = appr.add_subparsers(dest="action_cmd", required=True)
    req = appr_sub.add_parser("request", help="record a request and print the Approve / Request Changes / Defer offer")
    req.add_argument("task_id", help="the explicit existing task id")
    req.add_argument("--action", required=True, choices=("task_close", "workflow_complete", "commit", "merge", "publish"))
    req.add_argument("--gate", help="default: release for workflow_complete, else the action name")
    req.add_argument("--summary", required=True, help="completion summary the human is deciding on")
    req.add_argument("--evidence", action="append", default=[], help="verification evidence path or URL")
    req.add_argument("--content-ref", help="what exactly is bound (e.g. a diff digest or commit id)")
    req.add_argument("--resolves-blocker", action="store_true",
                     help="workflow_complete only: approving this gate is exactly what clears the ticket's blocker")
    dec = appr_sub.add_parser("decide", help="record the human's decision on one pending request")
    dec.add_argument("task_id")
    dec.add_argument("--request", help="request id (required when it would otherwise be ambiguous)")
    dec.add_argument("--action", required=True, choices=("task_close", "workflow_complete", "commit", "merge", "publish"))
    dec.add_argument("--gate", required=True)
    dec.add_argument("--binding", required=True, help="the binding token the offer showed")
    choice = dec.add_mutually_exclusive_group(required=True)
    choice.add_argument("--approve", action="store_true")
    choice.add_argument("--request-changes", action="store_true")
    choice.add_argument("--defer", action="store_true")
    dec.add_argument("--note", help="feedback (at most 1000 characters); required with --request-changes")
    exe = appr_sub.add_parser(
        "execute", help="after performing and verifying an approved commit/merge/publish, record its result",
        epilog="Records evidence only; nothing is executed. The approval itself is never evidence of success.")
    exe.add_argument("task_id")
    exe.add_argument("--request", required=True, help="the approved request id")
    exe.add_argument("--action", required=True, choices=("commit", "merge", "publish"))
    exe.add_argument("--binding", required=True, help="the approved request's binding token")
    outcome = exe.add_mutually_exclusive_group(required=True)
    outcome.add_argument("--succeeded", action="store_true", help="performed and verified")
    outcome.add_argument("--failed", action="store_true")
    exe.add_argument("--result-ref", help="the result, e.g. the commit SHA (required with --succeeded)")
    exe.add_argument("--evidence", action="append", default=[], help="verification evidence path or URL")
    exe.add_argument("--note")
    show = appr_sub.add_parser("show", help="approval requests on one task and their current state")
    show.add_argument("task_id")
    for p in (req, dec, exe, show):
        p.add_argument("--json", action="store_true")
    for p in (req, dec, exe):
        p.add_argument("--by", default="conversation", help="who decided or asked (recorded on the ticket)")

    task = sub.add_parser("task", help="explicit task identity (follow-up relation, id resolution) and archive")
    task_sub = task.add_subparsers(dest="task_cmd", required=True)
    start = task_sub.add_parser("start", help="create or reuse a work card, optionally launch a command with its id")
    start.add_argument("--title", help="required for a new task; never used for deduplication")
    start.add_argument("--details", default="")
    identity = start.add_mutually_exclusive_group()
    identity.add_argument("--task-id", help="continue an existing task (default: HARMONY_TASK_ID)")
    identity.add_argument("--new", action="store_true", help="separate problem: ignore inherited task/thread ids")
    start.add_argument("--source", choices=("codex", "terminal"),
                       help="default: codex when CODEX_THREAD_ID is set, otherwise terminal")
    start.add_argument("--external-id", help="stable work identity; codex defaults to CODEX_THREAD_ID")
    start.add_argument("--json", action="store_true", help="print task_id and created as JSON; no wrapped command")
    start.add_argument("wrapped", nargs=argparse.REMAINDER, metavar="-- COMMAND ...")
    listing = task_sub.add_parser("list", help="inspect existing tasks before starting or continuing work")
    listing.add_argument("--all", action="store_true", help="include DONE and archived tickets")
    listing.add_argument("--json", action="store_true")
    rel = task_sub.add_parser("relate", help="record that a task is a follow-up of an earlier one")
    rel.add_argument("task_id")
    rel.add_argument("--follow-up-of", required=True)
    rel.add_argument("--note")
    rel.add_argument("--by", default="nlh-cli")
    res = task_sub.add_parser("resolve", help="confirm an explicit task id; with none, list tasks and ask")
    res.add_argument("--task-id")
    for name, text in (("archive", "hide a DONE task in the board's Archived view (visibility only)"),
                       ("restore", "show an archived task on the board again")):
        vis = task_sub.add_parser(name, help=text)
        vis.add_argument("task_id")
        vis.add_argument("--expected-revision", type=int, help="board revision you saw (default: the current one)")
        vis.add_argument("--by", default="nlh-cli")

    args = parser.parse_args(argv)
    try:
        if args.command == "codex":
            return cmd_codex(args)
        project = resolve_project(args.project, args.config)
        if args.command == "companion":
            return cmd_companion(project, args)
        if args.command == "record":
            return cmd_record(project, args)
        if args.command == "approval":
            return cmd_approval(project, args)
        if args.command == "task":
            return cmd_task(project, args)
        return cmd_inspect(project, args)
    except ConfigError as exc:
        print(f"nlh: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
