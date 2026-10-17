#!/usr/bin/env python3
"""Local task board: one command, one loopback URL, no dependencies.

    python3 task-board/server.py        ->  http://127.0.0.1:8765

Standard library only. Loopback-bound, no scheduler and no
task runner: it serves a Kanban view of the Tasks in `.workflow/tickets` and
writes back only the board key and the manual lifecycle fields `store.py`
allows (title, details, status, closure record, history). It never executes,
merges or publishes a Task, never authorizes one, and never reaches the network.

Endpoints
  GET  /                  the board
  GET  /api/board         every canonical Task as a card; a DONE Task, and an
                          active one with run records, also carries
                          `observability`: its compact run line computed by
                          direct-workflow/run_observability.py
  GET  /api/observability per task with run records: per-invocation usage by
                          provider, exact model id and role (fresh input,
                          cache reads/writes, output, cached share), running
                          snapshots with a confirmed-active proof (context,
                          cumulative tokens, time series, last measured
                          time) and graph phases; read-only
  GET  /api/quota         weekly allowance remaining per provider, from official
                          sources only (direct-workflow/quota_telemetry.py):
                          Codex app-server, read by one background worker at
                          start and every minute (requests never wait for
                          it), and Claude's CLI /usage, read every 5 minutes
                          by one Companion per installation, with the Claude
                          status-line snapshot as fallback; read-only
  POST /api/tasks         add a Task: through the repo's intake allocator when
                          it is installed (unassessed => BLOCKED at intake),
                          otherwise a canonical BACKLOG ticket under the next
                          unused id, with no authorization
  PATCH /api/tasks/<id>   update board metadata and the canonical title,
                          details (scope.summary) and status
  POST /api/tasks/<id>/done    Mark Done: a labelled administrative closure
  POST /api/tasks/<id>/reopen  reopen a DONE Task to BACKLOG or READY
  POST /api/tasks/<id>/archive|restore  hide a DONE Task in Archived, or show it
                          again; visibility only (id, ticket, history kept)
  POST /api/tasks/archive archive several DONE Tasks, all or none, each at its
                          expected revision
  POST /api/tasks/<id>/approval/request  record a revision-bound approval request
                          (the same store call as `nlh approval request`)
  POST /api/tasks/<id>/approval/decide   Approve / Request Changes / Defer exactly
                          one pending request (as `nlh approval decide`); an
                          approved commit, merge or publish is a permission
                          only: nothing here runs git or any other command
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import store  # noqa: E402

# Companion and the CLI share the same side-effect-free readiness checks.
sys.path.insert(0, str(HERE.parent.parent))
from harmony import setup as setup_checks  # noqa: E402

# The run-record summary is not reimplemented here: the board imports the
# same library the `run_observability.py` CLI uses.
OBSERVABILITY_DIR = HERE.parent / "direct-workflow"
try:
    sys.path.insert(0, str(OBSERVABILITY_DIR))
    import run_observability  # noqa: E402
except ImportError:  # pragma: no cover - the board still works without it
    run_observability = None
try:
    import quota_telemetry  # noqa: E402
except ImportError:  # pragma: no cover
    quota_telemetry = None

HOST = "127.0.0.1"
PORT = 8765

STATIC = HERE / "static"
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
}

MAX_BODY = 256 * 1024


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def allowed_origins(port: int) -> set[str]:
    return {f"http://{host}:{port}" for host in ("127.0.0.1", "localhost", "[::1]")}


def allowed_hosts(port: int) -> set[str]:
    return {f"{host}:{port}" for host in ("127.0.0.1", "localhost", "[::1]")}


# --------------------------------------------------------------------------
# creating a Task: the existing allocator, never max+1
# --------------------------------------------------------------------------


class IntakeUnavailable(store.StoreError):
    pass


def intake_path(repo: Path) -> Path:
    return repo / "agent-orchestrator" / "intake.py"


def allocate_task(repo: Path, workflow: Path, request_text: str, session: str) -> dict:
    """Create a Task through `agent-orchestrator/intake.py`.

    Identity comes from intake's durable, locked high-water allocator -- never
    from max+1 and never from a second sequence. With no assessment attached the
    new Task is deliberately unassessed and therefore BLOCKED at intake: the
    board is asking for planning visibility, and a human title is not an
    assessment. It fabricates no acceptance criteria, no grant and no routing.
    """
    intake = intake_path(repo)
    if not intake.exists():
        raise IntakeUnavailable(
            f"The intake allocator at {intake} is not installed here; Add task then creates "
            "the ticket in the canonical store instead.",
            code=501,
        )

    sys.path.insert(0, str(repo / "agent-orchestrator"))
    import intake as intake_module  # noqa: PLC0415

    # A human title that merely mentions an existing Task must not be parsed as
    # a continuation of it: that would silently attach the card to someone
    # else's record and allocate nothing.
    references = intake_module.parse_references(request_text)
    if references.task_ids:
        named = ", ".join(f"Task #{number}" for number in references.task_ids)
        raise store.StoreError(
            f"This title names an existing Task ({named}), which intake reads as a "
            "continuation rather than a new card, so nothing would be allocated. "
            "Rephrase without the '#' reference (write 'task 12' or describe the work), "
            "or edit that Task's existing card instead.",
            code=409,
        )

    completed = subprocess.run(
        [
            sys.executable,
            str(intake),
            "create",
            "--request",
            request_text,
            "--session",
            session,
            "--repo",
            str(repo),
            "--workflow-dir",
            str(workflow),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    # 0 = READY, 2 = allocated but implementation must not begin. Both mean a
    # Task exists; only 2 is expected here, because the board never assesses.
    if completed.returncode not in (0, 2):
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        raise store.StoreError(
            "intake refused to allocate a Task: " + (" | ".join(detail[:6]) or "no detail"),
            code=400,
        )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        raise store.StoreError("intake produced output the board could not read", code=500) from None
    return payload


# --------------------------------------------------------------------------
# completed-card metrics: the shared run-record summary, nothing computed here
# --------------------------------------------------------------------------


def completed_card_metrics(card: dict, runs_dir: Path | None, live: list | None = None) -> dict | None:
    """Token/duration line for a completed or active Task, or None.

    A DONE Task gets the totals `run_observability.card_metrics` marks safe
    (every run reported them). Any other Task gets a line only when it has
    run records attributed to it (completed or running); running runs add
    their role and elapsed time, never provisional tokens. Run records are
    matched by `task_id` equal to the card's id.
    """
    if runs_dir is None or run_observability is None:
        return None
    try:
        summary = run_observability.summarize_task_dir(runs_dir, str(card["id"]))
    except run_observability.ObservabilityError:
        return None
    if card.get("status") == "DONE":
        return run_observability.card_metrics(summary)
    mine = [run for run in (live or []) if run.get("task_id") == str(card["id"])]
    return run_observability.card_metrics(summary, mine)


def read_graph(workflow: Path, task_id: str) -> dict | None:
    # The id comes from a run record; only a plain id may name a file.
    if not run_observability.recorder.RUN_ID_RE.match(task_id):
        return None
    try:
        graph = json.loads((workflow / "graphs" / f"{task_id}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return graph if isinstance(graph, dict) else None


def observability_payload(workflow: Path, runs_dir: Path | None, cards: list[dict]) -> dict:
    """Per-task observability from the shared library; the server computes nothing itself."""
    if runs_dir is None or run_observability is None:
        return {"available": False, "tasks": []}
    titles = {str(card["id"]): card for card in cards}
    task_ids = run_observability.task_ids_with_records(runs_dir)
    tasks = []
    for task_id in task_ids:
        view = run_observability.task_observability(runs_dir, task_id, graph=read_graph(workflow, task_id))
        card = titles.get(task_id)
        view["title"] = card["title"] if card else None
        view["status"] = card["status"] if card else None
        tasks.append(view)
    # Running work first, then the most recently started.
    def latest(view: dict) -> str:
        return max([run.get("started_at") or "" for run in view["live"] + view["summary"]["runs"]] or [""])

    tasks.sort(key=latest, reverse=True)
    tasks.sort(key=lambda view: any(run["state"] != "interrupted" for run in view["live"]), reverse=True)
    return {"available": True, "runs_dir": str(runs_dir), "tasks": tasks}


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "TaskBoard/1.0"
    protocol_version = "HTTP/1.1"

    # -- helpers ---------------------------------------------------------

    @property
    def board(self) -> store.Board:
        return self.server.board  # type: ignore[attr-defined]

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        # No remote origin, no inline script, no framing: the page only ever
        # loads its own two static files.
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; img-src 'none'; form-action 'none'; frame-ancestors 'none'",
        )
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, payload: dict) -> None:
        self._send(code, json.dumps(payload, indent=2).encode("utf-8"), "application/json; charset=utf-8")

    def _error(self, code: int, message: str, **extra) -> None:
        self._json(code, {"ok": False, "error": message, **extra})

    def _guard(self) -> bool:
        """Refuse anything that did not come from this loopback page.

        A Host or Origin check is what stops a page in another tab from driving
        this server through the browser (DNS rebinding, cross-site POST).
        """
        port = self.server.server_address[1]
        host = (self.headers.get("Host") or "").strip().lower()
        if host not in allowed_hosts(port):
            self._error(403, f"refused: unexpected Host header {host!r}; open {self.server.url}")
            return False
        origin = self.headers.get("Origin")
        if origin and origin.strip().lower() not in allowed_origins(port):
            self._error(403, f"refused: cross-origin request from {origin!r}")
            return False
        if self.command in ("POST", "PATCH"):
            kind = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if kind != "application/json":
                self._error(415, "refused: this API only accepts application/json")
                return False
        return True

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise store.StoreError("a valid Content-Length is required") from None
        if length <= 0:
            raise store.StoreError("the request body is empty")
        if length > MAX_BODY:
            raise store.StoreError("the request body is too large", code=413)
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise store.StoreError(f"the request body is not valid JSON: {exc}") from None
        if not isinstance(payload, dict):
            raise store.StoreError("the request body must be a JSON object")
        return payload

    # -- routes ----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        if not self._guard():
            return
        path = self.path.split("?")[0]
        if path in STATIC_FILES:
            name, content_type = STATIC_FILES[path]
            try:
                body = (STATIC / name).read_bytes()
            except OSError:
                self._error(500, f"missing static asset {name}")
                return
            self._send(200, body, content_type)
            return
        if path == "/api/harmony-connection":
            record = getattr(self.server, "harmony_connection", None)
            if record is None:
                self._error(404, "this Companion has not enabled Codex prompt discovery")
            else:
                self._json(200, record)
            return
        if path == "/api/setup":
            self._json(200, {"ok": True, **setup_checks.report()})
            return
        if path == "/api/board":
            self._json(200, self._board_payload())
            return
        if path == "/api/observability":
            payload = observability_payload(self.board.workflow, self.server.runs_dir,  # type: ignore[attr-defined]
                                            self.board.cards())
            self._json(200, {"ok": True, "generated_at": now_iso(), **payload})
            return
        if path == "/api/quota":
            source = self.server.quota  # type: ignore[attr-defined]
            payload = source.payload() if source is not None else {"providers": None}
            self._json(200, {"ok": True, "generated_at": now_iso(), **payload})
            return
        # There is deliberately no file-serving route: evidence paths are shown
        # as copyable text, so no repository path can be fetched through this
        # server.
        self._error(404, f"no such path {path!r}")

    do_HEAD = do_GET

    def do_POST(self) -> None:  # noqa: N802
        if not self._guard():
            return
        if self.path == "/api/tasks/start":
            try:
                data = self._body()
                store._refuse_fields(data, ("title", "details", "task_id", "source", "external_id"))
                if data.get("task_id") is None and intake_path(self.server.repo).is_file():
                    raise store.StoreError("This project uses its intake allocator; create through Add task first, "
                                           "then supply task_id", code=409)
                card, created = self.board.start_work(
                    {"title": data.get("title"), "details": data.get("details", "")},
                    source=data.get("source", "codex"), external_id=data.get("external_id"),
                    task_id=data.get("task_id"), actor="codex-intake", now=now_iso())
            except store.StoreError as exc:
                self._error(exc.code, exc.message)
                return
            self._json(200, {"ok": True, "card": card, "created": created})
            return
        path = self.path.split("?")[0]
        parts = [part for part in path.strip("/").split("/") if part]
        if (len(parts) == 4 and parts[:2] == ["api", "tasks"] and parts[2].isdigit()
                and parts[3] in ("done", "complete", "reopen", "archive", "restore")):
            self._lifecycle_action(int(parts[2]), parts[3])
            return
        if parts == ["api", "tasks", "archive"]:
            self._archive_many()
            return
        if (len(parts) == 5 and parts[:2] == ["api", "tasks"] and parts[3] == "approval"
                and parts[4] in ("request", "decide")):
            self._approval(parts[2], parts[4])
            return
        if path != "/api/tasks":
            self._error(404, f"no such path {self.path!r}")
            return
        try:
            fields = store.clean_new_task(self._body())
            result = self._create(fields)
        except store.StoreError as exc:
            self._error(exc.code, exc.message)
            return
        self._json(201, result)

    def _lifecycle_action(self, task_id: int, action: str) -> None:
        """Mark Done, Reopen, Archive or Restore. Only the ticket is written; nothing is started."""
        try:
            payload = self._body()
            allowed = {"done": {"expected_revision", "note"}, "complete": {"expected_revision", "note"},
                       "reopen": {"expected_revision", "status"}}.get(
                action, {"expected_revision"})
            unknown = sorted(set(payload) - allowed)
            if unknown:
                raise store.StoreError(f"{action} accepts only {', '.join(sorted(allowed))}: " + ", ".join(unknown))
            common = {"expected_revision": payload.get("expected_revision"),
                      "actor": self.server.actor, "now": now_iso()}  # type: ignore[attr-defined]
            if action == "done":
                card = self.board.close(task_id, note=store._clean_text(payload.get("note"), "note", limit=500),
                                        **common)
            elif action == "complete":
                card = self.board.complete_work(task_id, note=payload.get("note"),
                                                **{**common, "actor": "codex-lifecycle"})
            elif action == "reopen":
                card = self.board.reopen(task_id, status=payload.get("status"), **common)
            else:
                card = getattr(self.board, action)(task_id, **common)
        except store.StaleEdit as exc:
            self._error(exc.code, exc.message, stale=True, stored_revision=exc.actual)
            return
        except store.StoreError as exc:
            self._error(exc.code, exc.message)
            return
        self._json(200, {"ok": True, "card": card})

    def _archive_many(self) -> None:
        """Archive completed Tasks all or none; one stale or open Task refuses the whole request."""
        try:
            payload = self._body()
            store._refuse_fields(payload, ("tasks",))
            cards = self.board.archive_many(payload.get("tasks"), actor=self.server.actor,  # type: ignore[attr-defined]
                                            now=now_iso())
        except store.StaleEdit as exc:
            self._error(exc.code, exc.message, stale=True, stored_revision=exc.actual)
            return
        except store.StoreError as exc:
            self._error(exc.code, exc.message)
            return
        self._json(200, {"ok": True, "cards": cards})

    def _approval(self, task_id: str, kind: str) -> None:
        """The same store request/decision the `nlh approval` commands use. Nothing is executed."""
        allowed = ({"action", "gate_id", "summary", "evidence", "content_ref", "resolves_blocker"} if kind == "request"
                   else {"request_id", "action", "gate_id", "binding", "decision", "note"})
        try:
            payload = self._body()
            # Grants, approvals and gates are written only by the decision itself, never named by a client.
            store._refuse_fields(payload, tuple(allowed))
            common = {"actor": self.server.actor, "now": now_iso(), "via": "companion"}  # type: ignore[attr-defined]
            if kind == "request":
                result = self.board.request_approval(
                    task_id, action=payload.get("action"), gate=payload.get("gate_id"),
                    summary=payload.get("summary"), evidence=payload.get("evidence"),
                    content_ref=payload.get("content_ref"),
                    resolves_blocker=payload.get("resolves_blocker", False), **common)
            else:
                result = self.board.decide(
                    task_id, request_id=payload.get("request_id"), action=payload.get("action"),
                    gate=payload.get("gate_id"), binding=payload.get("binding"),
                    decision=payload.get("decision"), note=payload.get("note"), **common)
        except store.StoreError as exc:
            self._error(exc.code, exc.message)
            return
        self._json(201 if kind == "request" and result["created"] else 200, {"ok": True, **result})

    def do_PATCH(self) -> None:  # noqa: N802
        if not self._guard():
            return
        parts = [part for part in self.path.split("?")[0].strip("/").split("/") if part]
        if len(parts) != 3 or parts[:2] != ["api", "tasks"] or not parts[2].isdigit():
            self._error(404, f"no such path {self.path!r}")
            return
        try:
            payload = self._body()
            patch = store.clean_patch(payload)
            if not patch:
                raise store.StoreError("nothing to change")
            card = self.board.update(
                int(parts[2]),
                patch,
                expected_revision=payload.get("expected_revision"),
                actor=self.server.actor,  # type: ignore[attr-defined]
                now=now_iso(),
            )
        except store.StaleEdit as exc:
            self._error(exc.code, exc.message, stale=True, stored_revision=exc.actual)
            return
        except store.StoreError as exc:
            self._error(exc.code, exc.message)
            return
        self._json(200, {"ok": True, "card": card})






    # -- payloads --------------------------------------------------------

    def _board_payload(self) -> dict:
        runs_dir = self.server.runs_dir  # type: ignore[attr-defined]
        live = run_observability.live_runs(runs_dir) if runs_dir is not None and run_observability else []
        # Only the recorder proof moves a task to In Progress; the store re-checks it and every hold.
        for run in live:
            if run.get("state") == "running" and (run.get("active") or {}).get("confirmed") is True:
                self.board.sync_activity(run.get("task_id"), run)
        cards = self.board.cards()
        for card in cards:
            card["observability"] = completed_card_metrics(card, runs_dir, live)
        return {
            "ok": True,
            "generated_at": now_iso(),
            "stages": list(store.STAGES),
            "workflow_dir": str(self.board.workflow),
            # Add task uses the repo's intake allocator when it is installed,
            # otherwise the canonical store's own locked allocation.
            "intake_available": intake_path(self.server.repo).is_file(),  # type: ignore[attr-defined]
            "create_mode": "intake" if intake_path(self.server.repo).is_file() else "canonical",  # type: ignore[attr-defined]
            "cards": cards,
            "counts": {stage: sum(1 for c in cards if c["board"]["stage"] == stage and not c["board"]["archived"])
                       for stage in store.STAGES},
            "archived": sum(1 for c in cards if c["board"]["archived"]),
            "note": (
                "A stage is a visibility label only. It is not execution authorization and "
                "does not record a routing event. Mark Done is an administrative closure: it "
                "records no review, approval or release."
            ),
        }

    def _create(self, fields: dict) -> dict:
        repo = self.server.repo  # type: ignore[attr-defined]
        if not intake_path(repo).is_file():
            card = self.board.create(fields, actor=self.server.actor, now=now_iso())  # type: ignore[attr-defined]
            return {
                "ok": True,
                "card": card,
                "allocated_id": card["id"],
                "created_by": "canonical",
                "note": (
                    "Created in the canonical ticket store under the next unused id: BACKLOG, "
                    "not authorized, no acceptance criteria or review recorded. Nothing was started."
                ),
            }
        title = fields["title"]
        request_text = title
        extra = "\n\n".join(text for text in (fields["details"], fields["notes"]) if text)
        if extra:
            request_text = f"{title}\n\n{extra}"
        result = allocate_task(repo, self.board.workflow, request_text, self.server.session)  # type: ignore[attr-defined]
        task_id = result["task"]["id"]
        board_patch = store.clean_patch(
            {
                "stage": "Next",
                # The card shows exactly what the human typed. The ticket title
                # intake derives from the raw request (prefixed UNASSESSED, and
                # padded with the request body) stays the durable record and is
                # still reported as ticket_title.
                "display_title": title,
                "owner": fields["owner"] or store.UNKNOWN,
                "next_action": fields["next_action"] or store.UNKNOWN,
                # The gate the user typed is kept as their own business gate. The
                # mandatory intake warning is a separate system field, so neither
                # one can quietly replace the other.
                "gate": fields["gate"] or store.UNKNOWN,
                "verification": store.UNKNOWN,
                "notes": fields["notes"],
                "priority": 500,
            }
        )
        card = self.board.update(
            task_id,
            board_patch,
            expected_revision=0,
            actor=self.server.actor,  # type: ignore[attr-defined]
            now=now_iso(),
            system={"intake_notice": store.INTAKE_NOTICE},
        )
        return {
            "ok": True,
            "card": card,
            "allocated_id": task_id,
            "created_by": "intake",
            "intake_status": result["task"]["status"],
            "intake_reason": result.get("reason"),
            "raw_request_saved_to": result["task"].get("request"),
            "note": (
                "Identity came from the existing durable allocator. The raw request text is "
                "kept verbatim and the Task is BLOCKED until a real assessment exists."
            ),
        }

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


class BoardServer(ThreadingHTTPServer):
    """Threaded on purpose.

    The page keeps its HTTP/1.1 connection alive between requests. On a
    single-threaded server one idle keep-alive connection blocks the accept loop,
    so the next asset or API request stalls until the browser gives up. A thread
    per connection is the standard-library fix; board writes are still serialised
    by the store's file lock and its revision check.
    """

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address, repo: Path, workflow: Path, session: str, actor: str,
                 runs_dir: Path | None = None, quota=None):
        super().__init__(address, Handler)
        self.quota = quota if quota is not None else default_quota(workflow)
        self.repo = repo
        self.runs_dir = runs_dir
        self.board = store.Board(workflow)
        self.session = session
        self.actor = actor
        self.url = f"http://{address[0]}:{address[1]}"

    def start_quota(self) -> None:
        """Start background quota collection (the default source's Codex worker)."""
        start = getattr(self.quota, "start", None)
        if start is not None:
            start()

    def server_close(self) -> None:
        super().server_close()
        # TCPServer calls this during failed construction, before quota exists.
        close = getattr(getattr(self, "quota", None), "close", None)
        if close is not None:
            close()  # stops the worker and any app-server read in progress


def default_quota(workflow: Path):
    """Codex via its app-server, read by a background worker once started (off
    with HARMONY_CODEX_QUOTA=0); Claude via the CLI's /usage, read every 5
    minutes by one Companion per installation, whichever repository it serves
    (shared owner-only state in the temp dir; off with
    HARMONY_CLAUDE_CLI_QUOTA=0), with the status-line snapshot from
    $HARMONY_CLAUDE_QUOTA_FILE, else <workflow>/telemetry/claude-quota.json, as
    the fallback."""
    if quota_telemetry is None:
        return None
    env = os.environ.get(quota_telemetry.CLAUDE_QUOTA_ENV)
    claude = Path(env) if env else workflow / "telemetry" / "claude-quota.json"
    codex = None if os.environ.get(quota_telemetry.CODEX_QUOTA_ENV) == "0" else quota_telemetry.CodexQuota()
    worker = quota_telemetry.QuotaWorker(codex) if codex is not None else None
    cli = None
    if os.environ.get(quota_telemetry.CLAUDE_CLI_ENV) != "0":
        root, state_dir = quota_telemetry.install_state()
        cli = quota_telemetry.ClaudeCliQuota(state_dir, cwd=workflow.parent, root=root)
    cli_worker = quota_telemetry.QuotaWorker(cli, interval=quota_telemetry.CLAUDE_TICK) if cli is not None else None
    return quota_telemetry.QuotaSource(claude, codex, worker=worker, claude_cli=cli, claude_worker=cli_worker)


def build_server(repo: Path, workflow: Path, *, host: str = HOST, port: int = PORT, session: str, actor: str,
                 runs_dir: Path | None = None, quota=None):
    return BoardServer((host, port), repo, workflow, session, actor, runs_dir, quota)


def main(argv: list[str] | None = None) -> int:
    repo_default = HERE.parent
    parser = argparse.ArgumentParser(prog="task-board/server.py", description=__doc__.splitlines()[0])
    parser.add_argument("--repo", type=Path, default=repo_default)
    parser.add_argument("--workflow-dir", type=Path, default=None, help="defaults to <repo>/.workflow")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--host", default=HOST, help="loopback only; refuses any other address")
    parser.add_argument("--session", default="task-board-ui", help="session identity recorded by intake")
    parser.add_argument(
        "--runs-dir", type=Path, default=None,
        help="direct-workflow run records; defaults to $DIRECT_RUN_RECORDS_DIR, else <repo>/.direct-runs",
    )
    args = parser.parse_args(argv)

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        parser.error("the board binds loopback only; --host must be 127.0.0.1, localhost or ::1")

    repo = args.repo.resolve()
    workflow = (args.workflow_dir or repo / ".workflow").resolve()
    board = store.Board(workflow)
    env_runs = os.environ.get("DIRECT_RUN_RECORDS_DIR")
    runs_dir = (args.runs_dir or (Path(env_runs) if env_runs else repo / ".direct-runs")).resolve()

    httpd = build_server(repo, workflow, host=args.host, port=args.port, session=args.session, actor="ui",
                         runs_dir=runs_dir)
    httpd.start_quota()
    print(f"Task board on {httpd.url}  (Ctrl-C to stop)", flush=True)
    print(f"  tickets: {board.tickets}", flush=True)
    print(f"  run records: {runs_dir}"
          + ("" if run_observability else " (run_observability.py not found; no card metrics)"), flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
