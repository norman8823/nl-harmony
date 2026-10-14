#!/usr/bin/env python3
"""One compact, replace-in-place checkpoint per task, and a fresh-session bootstrap.

The checkpoint's `handoff` is the task's live context: the working agent's
compact, model-managed view (plan, phase, invariants, decisions, files,
evidence pointers, unresolved issues, next action). It may be rewritten whole
at any point during work, not only at session wrap-up.

    # print an empty handoff input to fill in
    python3 direct-workflow/task_checkpoint.py template

    # create or replace the task's checkpoint (fields given replace, omitted carry over)
    python3 direct-workflow/task_checkpoint.py update --task-id 42 --from handoff.json
    python3 direct-workflow/task_checkpoint.py update --task-id 42 --phase review \
        --next-action "Run the independent review of the stable package"

    # replace the whole live context (= update --reset --from); nothing carries over
    python3 direct-workflow/task_checkpoint.py rewrite --task-id 42 --from context.json

    # what a new session reads first
    python3 direct-workflow/task_checkpoint.py bootstrap --task-id 42
    python3 direct-workflow/task_checkpoint.py show --task-id 42 --json

Store. `<workflow-dir>/checkpoints/<task-id>.json`, where `<workflow-dir>`
defaults to `<project-root>/.workflow` (`--workflow-dir` / TASK_WORKFLOW_DIR;
`--checkpoint-dir` / TASK_CHECKPOINT_DIR moves only the checkpoints). Tickets
are read from `<workflow-dir>/tickets/<task-id>.json`. A fresh worktree points
`--workflow-dir` at the original checkout's local store.

Rules this module keeps:

  * **One file per task, replaced atomically** (temp file + fsync +
    os.replace). Lists are replaced whole, never appended: it is a
    checkpoint, not a diary.
  * **The ticket is authoritative.** Identity, title, status, goal,
    acceptance criteria, authorization and blocker are copied from the ticket
    and re-read live at bootstrap; a handoff that tries to set them is
    refused. Drift between the snapshot and the live ticket is reported, and
    the ticket wins. A checkpoint never advances a gate.
  * **Bounded, pointers only.** Every string and list is capped and the
    whole file is size-capped; diffs and multi-line dumps are refused.
    Transcripts, logs, diffs and source files belong in evidence files that
    the checkpoint references by path.
  * **The task graph is authoritative for workflow position.** When
    `<workflow-dir>/graphs/<task-id>.json` exists (task_graph.py), every
    update stores only its compact reference (revision, position,
    ready/blocked, next action) and bootstrap re-reads the graph live.
  * **Updates keep provenance.** A rewrite may compress, reorganize or
    drop stale context, but a dropped detail must stay recoverable from
    ticket / graph / run / evidence files. Any update is refused if it drops
    an evidence ref held nowhere in the new context, ticket, or graph.
  * **Unknown stays unknown.** Evidence refs are reported present / missing
    / not-a-path; bootstrap never reads their contents and never blocks
    further reading.

Standard library only. Exit codes: 0 ok, 2 usage, refused or unreadable input.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

KIND = "task-checkpoint/v1"
TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md")

MAX_STRING = 600
MAX_LINES = 4
MAX_ITEMS = 20
MAX_BYTES = 24_000

# handoff fields: name -> "str" | "list" | "findings"
HANDOFF_FIELDS = {
    "plan": "list",
    "phase": "str",
    "invariants": "list",
    "relevant_files": "list",
    "decisions": "list",
    "evidence_refs": "list",
    "changes": "list",
    "review_findings": "findings",
    "risks": "list",
    "open_questions": "list",
    "next_action": "str",
}
FINDING_KEYS = ("id", "summary", "resolution")
# Owned by the ticket when one exists; settable from the handoff only with --no-ticket.
TICKET_FIELDS = ("title", "status", "goal", "acceptance_criteria", "authorization", "blocker")
DUMP_MARKERS = ("diff --git", "\n@@ ", "\n+++ ", "\n--- ", "Traceback (most recent call last)")


class Refused(Exception):
    """Input that the checkpoint will not store; exit 2."""


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _check_string(name: str, value: Any) -> str:
    if not isinstance(value, str):
        raise Refused(f"{name}: expected a string")
    if len(value) > MAX_STRING:
        raise Refused(f"{name}: {len(value)} chars > {MAX_STRING}; store a pointer to an evidence file instead")
    if value.count("\n") >= MAX_LINES:
        raise Refused(f"{name}: more than {MAX_LINES} lines; store a pointer to an evidence file instead")
    if any(m in value for m in DUMP_MARKERS) or value.startswith(("diff --git", "@@ ")):
        raise Refused(f"{name}: looks like a diff/log dump; store a pointer to an evidence file instead")
    return value


def _check_list(name: str, value: Any, item) -> list:
    if not isinstance(value, list):
        raise Refused(f"{name}: expected a list")
    if len(value) > MAX_ITEMS:
        raise Refused(f"{name}: {len(value)} items > {MAX_ITEMS}; keep the checkpoint compact")
    return [item(f"{name}[{i}]", v) for i, v in enumerate(value)]


def _check_finding(name: str, value: Any) -> dict:
    if not isinstance(value, dict) or set(value) - set(FINDING_KEYS) or "id" not in value:
        raise Refused(f"{name}: expected an object with 'id' and optional 'summary', 'resolution'")
    return {k: _check_string(f"{name}.{k}", value[k]) for k in FINDING_KEYS if k in value}


def validate_handoff(data: Any, *, ticket_owned: bool) -> dict:
    """Check a handoff input. Unknown keys and ticket-owned keys are refused."""
    if not isinstance(data, dict):
        raise Refused("handoff input must be a JSON object")
    out: dict = {}
    for key, value in data.items():
        if key in TICKET_FIELDS:
            if ticket_owned:
                raise Refused(f"{key}: owned by the ticket; change the ticket through its own gate, not the checkpoint")
            if key == "acceptance_criteria":
                out[key] = _check_list(key, value, _check_string)
            elif key == "authorization":
                raise Refused("authorization: only a ticket can carry authorization")
            elif value is not None:
                out[key] = _check_string(key, value)
            continue
        kind = HANDOFF_FIELDS.get(key)
        if kind is None:
            raise Refused(f"{key}: unknown field (allowed: {', '.join(HANDOFF_FIELDS)})")
        if value is None:
            out[key] = None
        elif kind == "str":
            out[key] = _check_string(key, value)
        elif kind == "list":
            out[key] = _check_list(key, value, _check_string)
        else:
            out[key] = _check_list(key, value, _check_finding)
    return out


def ticket_snapshot(ticket: dict, path: Path) -> dict:
    """The authoritative, gate-bearing fields of a ticket, nothing else."""
    scope = ticket.get("scope") if isinstance(ticket.get("scope"), dict) else {}
    auth = ticket.get("authorization") if isinstance(ticket.get("authorization"), dict) else {}
    return {
        "authoritative": True,
        "source": str(path),
        "id": ticket.get("id"),
        "title": ticket.get("title"),
        "status": ticket.get("status"),
        "goal": scope.get("summary"),
        "acceptance_criteria": ticket.get("acceptance_criteria"),
        "authorization_granted": auth.get("granted"),
        "blocker": ticket.get("blocker"),
        "updated_at": ticket.get("updated_at"),
        "evidence_refs": ticket.get("evidence_refs") or [],
    }


class Store:
    def __init__(self, project_root: Path, workflow_dir: Path | None, checkpoint_dir: Path | None,
                 graph_dir: Path | None = None):
        self.project_root = project_root.resolve()
        self.workflow_dir = (workflow_dir or self.project_root / ".workflow").expanduser().resolve()
        self.checkpoint_dir = (checkpoint_dir or self.workflow_dir / "checkpoints").expanduser().resolve()
        self.tickets_dir = self.workflow_dir / "tickets"
        self.graph_dir = graph_dir

    def checkpoint_path(self, task_id: str) -> Path:
        return self.checkpoint_dir / f"{task_id}.json"

    def ticket_path(self, task_id: str) -> Path:
        return self.tickets_dir / f"{task_id}.json"

    def load_ticket(self, task_id: str) -> dict | None:
        path = self.ticket_path(task_id)
        if not path.is_file():
            return None
        ticket = _read_json(path)
        if str(ticket.get("id")) != task_id:
            raise Refused(f"{path}: ticket id {ticket.get('id')!r} does not match task {task_id!r}")
        return ticket_snapshot(ticket, path)

    def load_checkpoint(self, task_id: str) -> dict | None:
        path = self.checkpoint_path(task_id)
        if not path.is_file():
            return None
        data = _read_json(path)
        if data.get("kind") != KIND or str(data.get("task_id")) != task_id:
            raise Refused(f"{path}: not a {KIND} checkpoint for task {task_id!r}")
        return data


def graph_summary(store: Store, task_id: str) -> dict | None:
    """The task graph's compact summary, read live; None when the task has no graph."""
    import task_graph  # same package; imported here because it imports this module

    return task_graph.GraphStore(store, store.graph_dir).summary(task_id)


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Refused(f"{path}: unreadable JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise Refused(f"{path}: expected a JSON object")
    return data


def write_atomic(path: Path, data: dict) -> None:
    """Replace `path` in one step; a reader sees the old file or the new one."""
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise Refused(f"checkpoint would be {len(text.encode('utf-8'))} bytes > {MAX_BYTES}; trim it")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def update(store: Store, task_id: str, handoff_input: dict, *, no_ticket: bool, reset: bool) -> dict:
    """Build and atomically write the task's checkpoint; return it."""
    ticket = store.load_ticket(task_id)
    if ticket is None and not no_ticket:
        raise Refused(f"no ticket at {store.ticket_path(task_id)}; pass --no-ticket for a project without tickets")
    if ticket is not None and no_ticket:
        raise Refused(f"--no-ticket given but {store.ticket_path(task_id)} exists; the ticket is authoritative")
    if "task_id" in handoff_input:
        if str(handoff_input.pop("task_id")) != task_id:
            raise Refused("task_id in the input does not match --task-id")
    fresh = validate_handoff(handoff_input, ticket_owned=ticket is not None)
    previous = store.load_checkpoint(task_id)
    prior_refs = ((previous or {}).get("handoff") or {}).get("evidence_refs") or []
    if reset:
        previous = None

    handoff = {name: None for name in HANDOFF_FIELDS}
    if previous:
        handoff.update({k: v for k, v in (previous.get("handoff") or {}).items() if k in HANDOFF_FIELDS})
    handoff.update({k: v for k, v in fresh.items() if k in HANDOFF_FIELDS})

    if ticket is None:
        prior = (previous or {}).get("ticket") or {}
        ticket = {
            "authoritative": False,
            "source": None,
            "id": task_id,
            "title": fresh.get("title", prior.get("title")),
            "status": fresh.get("status", prior.get("status")),
            "goal": fresh.get("goal", prior.get("goal")),
            "acceptance_criteria": fresh.get("acceptance_criteria", prior.get("acceptance_criteria")),
            "authorization_granted": None,
            "blocker": fresh.get("blocker", prior.get("blocker")),
            "updated_at": None,
            "evidence_refs": [],
        }
    checkpoint = {
        "kind": KIND,
        "task_id": task_id,
        "updated_at": _now(),
        "ticket": ticket,
        "handoff": handoff,
    }
    graph = graph_summary(store, task_id)
    if graph is not None:
        checkpoint["graph"] = graph
    if prior_refs:
        held = (set(handoff.get("evidence_refs") or []) | set(ticket.get("evidence_refs") or [])
                | graph_refs(store, task_id))
        dropped = [r for r in prior_refs if r not in held]
        if dropped:
            raise Refused("update would lose the only recorded evidence refs; keep them in the new context, "
                          "ticket, or graph node before rewriting: " + "; ".join(dropped))
    write_atomic(store.checkpoint_path(task_id), checkpoint)
    return checkpoint


def graph_refs(store: Store, task_id: str) -> set[str]:
    import task_graph

    graph = task_graph.GraphStore(store, store.graph_dir).load(task_id)
    return {r for n in (graph or {}).get("nodes", []) for r in n.get("evidence_refs") or []}


def _looks_like_path(ref: str) -> bool:
    return bool(ref) and " " not in ref and ("/" in ref or ref.startswith("~") or Path(ref).suffix != "")


def evidence_status(store: Store, ref: str) -> dict:
    """present / missing / not-a-path for one reference; contents are never read."""
    if not _looks_like_path(ref):
        return {"ref": ref, "status": "not-a-path", "path": None}
    candidate = Path(ref).expanduser()
    bases = [candidate] if candidate.is_absolute() else [
        store.project_root / candidate, store.workflow_dir.parent / candidate]
    for path in bases:
        if path.exists():
            return {"ref": ref, "status": "present", "path": str(path)}
    return {"ref": ref, "status": "missing", "path": None}


def ticket_drift(snapshot: dict, live: dict | None) -> list[str]:
    if live is None:
        if snapshot.get("authoritative"):
            return ["ticket no longer found; checkpoint ticket fields are a stale snapshot"]
        return []
    keys = ("title", "status", "goal", "acceptance_criteria", "authorization_granted", "blocker", "updated_at")
    return [f"{k}: checkpoint {snapshot.get(k)!r} -> ticket {live.get(k)!r}"
            for k in keys if snapshot.get(k) != live.get(k)]


def bootstrap(store: Store, task_id: str) -> dict:
    checkpoint = store.load_checkpoint(task_id)
    live = store.load_ticket(task_id)
    snapshot = (checkpoint or {}).get("ticket") or {}
    ticket = live or snapshot or None
    refs: list[str] = []
    handoff_refs = ((checkpoint or {}).get("handoff") or {}).get("evidence_refs") or []
    for ref in ((ticket or {}).get("evidence_refs") or []) + handoff_refs:
        if ref not in refs:
            refs.append(ref)
    graph = graph_summary(store, task_id)
    ref = (checkpoint or {}).get("graph")
    graph_drift = []
    if ref and graph is None:
        graph_drift.append(f"checkpoint references {ref.get('path')} but no graph exists now")
    elif checkpoint and not ref and graph:
        graph_drift.append(f"checkpoint predates the graph (now rev {graph['revision']})")
    elif ref and graph and ref.get("revision") != graph["revision"]:
        graph_drift.append(f"checkpoint saw graph rev {ref.get('revision')}, graph is now rev {graph['revision']}")
    return {
        "kind": "task-bootstrap/v1",
        "task_id": task_id,
        "instructions": [str(store.project_root / n) for n in INSTRUCTION_FILES if (store.project_root / n).is_file()],
        "ticket": ticket,
        "ticket_drift": (drift := ticket_drift(snapshot, live) if checkpoint else []),
        "checkpoint_path": str(store.checkpoint_path(task_id)) if checkpoint else None,
        "checkpoint_updated_at": (checkpoint or {}).get("updated_at"),
        "handoff": (checkpoint or {}).get("handoff"),
        "graph": graph,
        "graph_drift": graph_drift,
        "context_stale": bool(drift or graph_drift),
        "evidence": [evidence_status(store, r) for r in refs],
    }


def _fmt(value: Any) -> str:
    return "unknown" if value is None else str(value)


def render_bootstrap(b: dict) -> str:
    lines = [f"# Task {b['task_id']} handoff"]
    lines.append("\n## 1. Read first: project instructions")
    lines += [f"- {p}" for p in b["instructions"]] or ["- none found at the project root"]
    t, h = b["ticket"], b["handoff"]
    lines.append("\n## 2. Live context (model-managed; section 3 wins on status, gates and workflow position)")
    if b.get("context_stale"):
        lines.append("- STALE: the ticket or graph changed since this context was written (DRIFT in section 3); "
                     "trust section 3, then rewrite the context")
    if t is not None:
        src = "ticket" if t.get("authoritative") else "checkpoint-only, not authoritative"
        lines.append(f"- goal ({src}): {_fmt(t.get('goal'))}")
        lines.append(f"- acceptance criteria ({src}):")
        lines += [f"  - {c}" for c in t.get("acceptance_criteria") or []] or ["  - unknown"]
    if h is None:
        lines.append("- no checkpoint; reconstruct state from the ticket, graph and evidence")
    else:
        lines.append(f"- file: {b['checkpoint_path']} (updated {b['checkpoint_updated_at']})")
        lines.append(f"- phase: {_fmt(h.get('phase'))}")
        for key, label in (("plan", "plan"), ("invariants", "invariants"), ("decisions", "decisions"),
                           ("changes", "changes"), ("relevant_files", "relevant files"), ("risks", "risks"),
                           ("open_questions", "unresolved issues / open questions")):
            items = h.get(key)
            lines.append(f"- {label}:" + (" unknown" if items is None else "" if items else " none"))
            lines += [f"  - {i}" for i in items or []]
        findings = h.get("review_findings")
        lines.append("- review findings:" + (" unknown" if findings is None else "" if findings else " none"))
        for f in findings or []:
            lines.append(f"  - {f.get('id')}: {f.get('summary', '')} -> {f.get('resolution', 'unresolved')}")
        lines.append(f"- next action: {_fmt(h.get('next_action'))}")
    lines.append("\n## 3. Ticket and graph metadata (authoritative; gates live here, not in the context)")
    if t is None:
        lines.append("- no ticket and no checkpoint found")
    else:
        lines.append(f"- ticket: {t.get('source') or 'none (checkpoint-only, not authoritative)'}")
        for key in ("title", "status", "authorization_granted"):
            lines.append(f"- {key}: {_fmt(t.get(key))}")
        # Only the flag is copied; its scope lives in the ticket, so never let it read as blanket authority.
        if t.get("source"):
            lines.append(f"- authorization scope: not shown; read the full `authorization` in {t['source']} "
                         "before any privileged action (a grant covers only what it names)")
        else:
            lines.append("- authorization scope: none recorded; obtain explicit approval before any privileged action")
        # An authoritative ticket's null blocker is a recorded absence, not an unknown.
        blocker = t.get("blocker")
        lines.append(f"- blocker: {'none' if blocker is None and t.get('authoritative') else _fmt(blocker)}")
    for d in b["ticket_drift"]:
        lines.append(f"- DRIFT (ticket wins): {d}")
    g = b.get("graph")
    if g is not None:
        lines.append(f"- graph (authoritative workflow position; wins over the context): {g['path']} rev {g['revision']}")
        lines.append(f"- graph next action: {g['next_action']}")
        lines.append(f"- last completed: {g['last_completed'] or 'none'}")
        for key in ("running", "ready", "blocked", "failed"):
            lines.append(f"- {key}: {', '.join(g[key]) or 'none'}")
        handoff = g.get("review_handoff")
        if handoff is not None:  # shape version 2: findings the review inherits, never its own evidence
            lines.append("- review handoff, investigator findings (not the review's own evidence): "
                         + (", ".join(handoff["investigator_evidence_refs"]) or "none yet")
                         + (f"; runs {', '.join(handoff['investigator_run_ids'])}"
                            if handoff["investigator_run_ids"] else ""))
    for d in b.get("graph_drift") or []:
        lines.append(f"- DRIFT (graph wins): {d}")
    lines.append("\n## 4. Evidence (pointers; read as needed)")
    lines += [f"- [{e['status']}] {e['ref']}" + (f" ({e['path']})" if e["path"] and e["path"] != e["ref"] else "")
              for e in b["evidence"]] or ["- none recorded"]
    lines.append("\nRead the instruction files, then open referenced evidence as the next action needs it. "
                 "This handoff does not limit further reading and does not advance any gate; "
                 "verify claims against the ticket and evidence before acting on them.")
    return "\n".join(lines)


def template() -> dict:
    return {name: ([] if kind != "str" else "") for name, kind in HANDOFF_FIELDS.items()} | {
        "review_findings": [{"id": "F1", "summary": "", "resolution": ""}]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="task_checkpoint.py", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--task-id", required=True)
        p.add_argument("--project-root", type=Path, default=Path.cwd())
        p.add_argument("--workflow-dir", type=Path, default=os.environ.get("TASK_WORKFLOW_DIR") or None)
        p.add_argument("--checkpoint-dir", type=Path, default=os.environ.get("TASK_CHECKPOINT_DIR") or None)
        p.add_argument("--graph-dir", type=Path, default=os.environ.get("TASK_GRAPH_DIR") or None)

    sub.add_parser("template", help="print an empty handoff input")
    up = sub.add_parser("update", help="create or replace the task's checkpoint")
    common(up)
    up.add_argument("--from", dest="source", help="handoff JSON file, or - for stdin")
    up.add_argument("--phase")
    up.add_argument("--next-action")
    up.add_argument("--reset", action="store_true", help="do not carry over fields from the previous checkpoint")
    up.add_argument("--no-ticket", action="store_true", help="project without tickets; fields are not authoritative")
    rw = sub.add_parser("rewrite", help="replace the whole live context (update --reset --from)")
    common(rw)
    rw.add_argument("--from", dest="source", required=True, help="full context JSON file, or - for stdin")
    rw.add_argument("--no-ticket", action="store_true", help="project without tickets; fields are not authoritative")
    common(sub.add_parser("show", help="print the stored checkpoint JSON"))
    boot = sub.add_parser("bootstrap", help="instructions, ticket, checkpoint, evidence pointers")
    common(boot)
    boot.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.cmd == "template":
        print(json.dumps(template(), indent=2))
        return 0
    if not TASK_ID_RE.match(args.task_id):
        print(f"task_checkpoint: invalid task id {args.task_id!r}", file=sys.stderr)
        return 2
    store = Store(args.project_root, args.workflow_dir, args.checkpoint_dir, args.graph_dir)
    try:
        if args.cmd in ("update", "rewrite"):
            data: dict = {}
            if args.source:
                raw = sys.stdin.read() if args.source == "-" else Path(args.source).read_text(encoding="utf-8")
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise Refused("handoff input must be a JSON object")
            if getattr(args, "phase", None) is not None:
                data["phase"] = args.phase
            if getattr(args, "next_action", None) is not None:
                data["next_action"] = args.next_action
            cp = update(store, args.task_id, data, no_ticket=args.no_ticket,
                        reset=args.cmd == "rewrite" or args.reset)
            print(f"wrote {store.checkpoint_path(args.task_id)} ({cp['updated_at']})")
        elif args.cmd == "show":
            cp = store.load_checkpoint(args.task_id)
            if cp is None:
                raise Refused(f"no checkpoint at {store.checkpoint_path(args.task_id)}")
            print(json.dumps(cp, indent=2, ensure_ascii=False))
        else:
            b = bootstrap(store, args.task_id)
            print(json.dumps(b, indent=2, ensure_ascii=False) if args.json else render_bootstrap(b))
    except (Refused, OSError, ValueError) as exc:
        print(f"task_checkpoint: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
