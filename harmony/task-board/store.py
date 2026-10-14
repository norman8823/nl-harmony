#!/usr/bin/env python3
"""Board metadata and the manual task lifecycle over `.workflow/tickets/<id>.json`.

There is no second task list. A ticket file is the single durable record; this
module reads it leniently and, under one lock and one revision counter, writes
back only:

  * `board` -- visibility metadata (stage, owner, notes, links, the
    `archived` flag a human sets on a DONE task and only Restore clears, ...);
  * three canonical lifecycle fields: `title` (the first title is kept as
    `lifecycle.original_title`), `scope.summary` (the task's details) and
    `status` (DONE only as a labelled closure, leaving DONE only by reopening);
  * `lifecycle` (origin, closure record, completion-cycle markers) and an
    appended `history` audit list;
  * `approval_requests`, `relations` and -- only through a human decision on a
    matching, revision-bound approval request -- the decided approval's
    `approvals` entry, its one `authorization` flag, the blocker that request
    named and the graph's release gate (see the approval bridge below);
    and the execution evidence an agent records on an approved commit, merge
    or publish request after performing and verifying that action.

Every other key -- authorization, blocker, acceptance criteria, routing,
approvals, evidence, unknown future fields -- is otherwise carried through
untouched and is refused if a client names it. Historical records that predate the current
schema stay readable and are written back with their contents preserved.

Board stage is a *visibility label*. It is not execution authorization, it is
not a routing event, and moving a card is never a review or an approval. A
DONE task always shows in Done and only a DONE task does.

Manual closure is administrative: it records no review, approval,
authorization or release. Only a positively completed task graph -- every node
finished, a passed review, a completed release with release approval where
release is required, explicit execution authorization, no blocker and every
declared approval positively approved -- closes a task automatically, once per
graph completion cycle. Nothing here starts, merges or publishes anything.

Archive is visibility only: the ticket keeps its path, id, history, evidence,
approvals and graph, nothing is archived automatically and nothing is deleted
or renumbered. It never freezes Reopen, Edit or activity sync, and it stays set
across status changes until a human Restores the card; an Archive/Restore
alone does not make a pending approval binding stale. A confirmed running recorder for a task moves an open,
authorized, unheld task to In Progress (`sync_activity`); it never touches a
DONE task.

Standard library only.
"""

from __future__ import annotations

import contextlib
import copy
import datetime as _dt
import fcntl
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path

# The graph rules are not reimplemented here: completion is judged on a graph
# the shared task_graph module validates.
try:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "direct-workflow"))
    import task_graph  # noqa: E402
except ImportError:  # pragma: no cover - without it nothing closes automatically
    task_graph = None

BOARD_VERSION = 1

STAGES = ("Next", "In Progress", "Blocked", "Done")

# Lifecycle status -> a read-only lane guess, used only when a ticket carries no
# board metadata at all. A guess is always reported as unverified.
STAGE_FROM_STATUS = {
    "BACKLOG": "Next",
    "READY": "Next",
    "INVESTIGATING": "In Progress",
    "IMPLEMENTING": "In Progress",
    "REVIEW": "In Progress",
    "BLOCKED": "Blocked",
    "DONE": "Done",
}

UNKNOWN = "unknown"

# Only these keys may ever be written, and only inside `board`. Everything else
# in a ticket -- id, status, routing, authorization, grants, acceptance -- is
# out of reach of this module and of the HTTP API on top of it.
EDITABLE_FIELDS = (
    "stage",
    "display_title",
    "next_action",
    "owner",
    "gate",
    "verification",
    "notes",
    "links",
    "linked_defects",
    "priority",
)

VERIFICATIONS = ("verified", "unknown")

# Written only by the creation path, never accepted from a client payload, so the
# warning that an unassessed card is not execution authorization cannot be edited
# away. It is displayed next to -- not instead of -- the editable business gate.
SYSTEM_FIELDS = ("intake_notice",)

INTAKE_NOTICE = (
    "Unassessed at intake and therefore BLOCKED: no assessment, acceptance criteria "
    "or authorization exist. Next means planning visibility only and implies no "
    "permission to begin work."
)

LINK_KINDS = ("pr", "issue", "evidence", "path", "doc")

# A link is either an http(s) URL or a repository-relative path shown as
# copyable text. Nothing else is accepted, so no executable scheme can reach
# an anchor element.
_HTTP = re.compile(r"^https?://[^\s<>\"']+$")
_SAFE_PATH = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._/#:\- ]*$")

CANONICAL_TICKET = re.compile(r"^(\d+)\.json$")
# Any file named for a task id: canonical tickets, archives (`12.closeout.json`),
# graphs and checkpoints. An id seen anywhere is never allocated again.
TASK_ID_PREFIX = re.compile(r"^(\d+)(?:\.|$)")

STATUSES = ("BACKLOG", "READY", "INVESTIGATING", "IMPLEMENTING", "REVIEW", "BLOCKED", "DONE")
REOPEN_STATUSES = ("BACKLOG", "READY")

# The canonical fields the board may change, each under its own rule. `details`
# is the ticket's `scope.summary`; board `notes` stay board-only.
LIFECYCLE_FIELDS = ("title", "details", "status")

# Named so the refusal says why: gates, grants, routing, evidence and the audit
# record itself are never written from a client payload.
PROTECTED_FIELDS = (
    "id", "authorization", "blocker", "acceptance_criteria", "routing", "review", "reviews",
    "release", "release_granted", "approvals", "required_approvals", "gates", "grants",
    "evidence_refs", "source_refs", "history", "lifecycle", "scope", "board", "schema_version",
    "updated_at", "created_at", "graph", "checkpoint", "intake_notice", "revision", "derived",
    "updated_by", "board_version", "approval_requests", "relations", "commit_granted", "merge_granted",
    "publish_granted",
)

NEW_TASK_FIELDS = ("title", "details", "owner", "next_action", "gate", "notes")

MANUAL_CLOSURE = (
    "Manual closure: administrative only. No review, approval, authorization or release is "
    "recorded; any blocking release gate and the graph's release stay as they are."
)
AUTOMATIC_CLOSURE = (
    "Closed automatically: the task graph completed with a passed review and every declared "
    "gate positively approved."
)
RECORDED_CLOSURE = "DONE as recorded on the ticket; the board holds no closure record for it."
MANUAL_AUTHORIZATION = "Manual board task: nothing has authorized execution."

SYNC_ACTOR = "graph-sync"
_POSITIVE = ("approved", "granted", "pass", "passed")


def utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class StoreError(Exception):
    """A request that cannot be honoured. `code` is the HTTP status to use."""

    def __init__(self, message: str, code: int = 400):
        super().__init__(message)
        self.message = message
        self.code = code


class StaleEdit(StoreError):
    """Someone else wrote this card since the editor loaded it."""

    def __init__(self, task_id: int, expected: int, actual: int):
        super().__init__(
            f"Task #{task_id} changed since you loaded it "
            f"(you have revision {expected}, stored revision is {actual}). "
            "Reload the card and re-apply your edit.",
            code=409,
        )
        self.expected = expected
        self.actual = actual


# --------------------------------------------------------------------------
# locking and atomic replacement
# --------------------------------------------------------------------------


class _Lock:
    """Advisory lock over the whole ticket directory for board writes.

    Board writes are rare and human-paced, so one coarse lock is simpler and
    safer than per-file locks and cannot deadlock against itself.
    """

    def __init__(self, path: Path):
        self.path = path
        self._handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a+")
        fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None
        return False


def _replace_atomically(path: Path, document: dict) -> None:
    """Write a whole ticket, then swap it in. A crash leaves the old file."""
    text = json.dumps(document, indent=2) + "\n"
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp", delete=False
    )
    try:
        with handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(handle.name)
        raise
    _fsync_directory(path.parent)


def _create_exclusively(path: Path, document: dict) -> None:
    """Write a whole new ticket under `path`, failing if that name exists.

    The complete document is written to a temporary file first and then hard
    linked into place, which fails with FileExistsError instead of replacing an
    existing ticket: a crash never leaves a half-written or overwritten record.
    """
    text = json.dumps(document, indent=2) + "\n"
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp", delete=False
    )
    try:
        with handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(handle.name, path)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(handle.name)
    _fsync_directory(path.parent)


def _fsync_directory(directory_path: Path) -> None:
    directory = os.open(str(directory_path), os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


# --------------------------------------------------------------------------
# validation of the only thing we accept from a client
# --------------------------------------------------------------------------


def _clean_text(value: object, label: str, limit: int = 2000) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise StoreError(f"{label} must be text")
    # Control characters are stripped rather than rejected: they carry no
    # meaning on a card and would only ever confuse a terminal or a log.
    text = "".join(ch for ch in value if ch >= " " or ch == "\n").strip()
    if len(text) > limit:
        raise StoreError(f"{label} is too long (limit {limit} characters)")
    return text


def _clean_link(raw: object) -> dict:
    if not isinstance(raw, dict):
        raise StoreError("each link must be an object")
    target = _clean_text(raw.get("target"), "link target", limit=500)
    if not target:
        raise StoreError("a link needs a target")
    label = _clean_text(raw.get("label"), "link label", limit=200) or target
    kind = raw.get("kind") or "evidence"
    if kind not in LINK_KINDS:
        raise StoreError(f"link kind must be one of {', '.join(LINK_KINDS)}")
    if _HTTP.match(target):
        return {"label": label, "target": target, "kind": kind, "external": True}
    if _SAFE_PATH.match(target) and ":" not in target.split("/")[0]:
        # A repository-relative path. It is shown as copyable text; this server
        # never reads or serves it, so no path can leak a file's contents.
        return {"label": label, "target": target, "kind": kind, "external": False}
    raise StoreError(
        "a link must be an http(s) URL or a plain repository-relative path; "
        "other schemes are refused"
    )


def _clean_defect(raw: object) -> dict:
    if not isinstance(raw, dict):
        raise StoreError("each linked defect must be an object")
    number = raw.get("id")
    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
        raise StoreError("a linked defect needs a positive integer id")
    return {
        "type": "github_issue",
        "id": number,
        "state": _clean_text(raw.get("state"), "defect state", limit=40) or UNKNOWN,
        "title": _clean_text(raw.get("title"), "defect title", limit=300) or UNKNOWN,
        "url": _clean_link({"target": raw["url"], "kind": "issue"})["target"] if raw.get("url") else None,
        "note": _clean_text(raw.get("note"), "defect note", limit=500) or None,
    }


def _refuse_fields(raw: dict, allowed: tuple[str, ...]) -> None:
    keys = set(raw) - {"expected_revision"}
    protected = sorted(keys & set(PROTECTED_FIELDS))
    if protected:
        raise StoreError(
            "protected fields are never changed from the board (gates, grants, routing, evidence "
            "and the audit record have their own owners): " + ", ".join(protected),
            code=403,
        )
    unknown = sorted(keys - set(allowed))
    if unknown:
        raise StoreError("these fields are not editable from the board: " + ", ".join(unknown))


def _clean_title(value: object) -> str:
    title = _clean_text(value, "title", limit=500)
    if len(title) < 3:
        raise StoreError("a title needs at least 3 characters")
    return title


def clean_new_task(raw: object) -> dict:
    """The fields a new manual task may start with. Nothing that grants or gates."""
    if not isinstance(raw, dict):
        raise StoreError("expected a JSON object")
    _refuse_fields(raw, NEW_TASK_FIELDS)
    fields = {"title": _clean_title(raw.get("title"))}
    for field, limit in (("details", 4000), ("owner", 200), ("next_action", 2000), ("gate", 2000), ("notes", 4000)):
        fields[field] = _clean_text(raw.get(field), field, limit=limit)
    return fields


def clean_patch(raw: object) -> dict:
    """Reduce a client payload to the board fields and lifecycle fields it may change.

    Protected fields are refused with 403 and anything else unknown with 400,
    before anything is read or written.
    """
    if not isinstance(raw, dict):
        raise StoreError("expected a JSON object")
    _refuse_fields(raw, EDITABLE_FIELDS + LIFECYCLE_FIELDS)
    patch: dict = {}
    if "title" in raw:
        patch["title"] = _clean_title(raw["title"])
    if "details" in raw:
        patch["details"] = _clean_text(raw["details"], "details", limit=4000)
    if "status" in raw:
        if raw["status"] not in STATUSES:
            raise StoreError(f"status must be one of {', '.join(STATUSES)}")
        patch["status"] = raw["status"]
    if "stage" in raw:
        if raw["stage"] not in STAGES:
            raise StoreError(f"stage must be one of {', '.join(STAGES)}")
        patch["stage"] = raw["stage"]
    if "verification" in raw:
        if raw["verification"] not in VERIFICATIONS:
            raise StoreError(f"verification must be one of {', '.join(VERIFICATIONS)}")
        patch["verification"] = raw["verification"]
    for field, limit in (
        ("next_action", 2000),
        ("owner", 200),
        ("gate", 2000),
        ("notes", 4000),
        ("display_title", 500),
    ):
        if field in raw:
            patch[field] = _clean_text(raw[field], field, limit=limit)
    if "priority" in raw:
        value = raw["priority"]
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 999:
            raise StoreError("priority must be an integer between 0 and 999")
        patch["priority"] = value
    for field, cleaner in (("links", _clean_link), ("linked_defects", _clean_defect)):
        if field in raw:
            items = raw[field]
            if not isinstance(items, list):
                raise StoreError(f"{field} must be a list")
            if len(items) > 30:
                raise StoreError(f"{field} is limited to 30 entries")
            patch[field] = [cleaner(item) for item in items]
    return patch


# --------------------------------------------------------------------------
# the store
# --------------------------------------------------------------------------


def _blocker_gate(blocker: object) -> str | None:
    """The human gate a blocker names, read without assuming its shape.

    A blocker object names it in `required_resolution` or `human_action`; an
    older record may hold the blocker as plain text, which is shown as is.
    Anything else is not guessed at: the caller reports it as unknown.
    """
    if isinstance(blocker, dict):
        return blocker.get("required_resolution") or blocker.get("human_action")
    if isinstance(blocker, str) and blocker.strip():
        return blocker.strip()
    return None


def default_board(ticket: dict) -> dict:
    """A read-only lane guess for a ticket nobody has curated yet.

    Every field it cannot know says so. `derived` marks the whole card as a
    guess; the first edit persists real metadata and clears the flag.
    """
    status = ticket.get("status")
    gate = _blocker_gate(ticket.get("blocker"))
    return {
        "board_version": BOARD_VERSION,
        "derived": True,
        "stage": STAGE_FROM_STATUS.get(status, "Next"),
        "display_title": None,
        "next_action": UNKNOWN,
        "owner": UNKNOWN,
        "gate": gate or UNKNOWN,
        "intake_notice": None,
        "verification": UNKNOWN,
        "notes": "",
        "links": [],
        "linked_defects": [],
        "priority": 500,
        "archived": False,
        "revision": 0,
        "updated_at": None,
        "updated_by": None,
    }


def _normalise_board(stored: object, ticket: dict) -> dict:
    """Merge stored board metadata onto the derived shape, preserving extras."""
    derived = default_board(ticket)
    if not isinstance(stored, dict):
        return derived
    # Unrecognised keys written by a future version of the board survive a
    # round trip unchanged rather than being dropped on read.
    board = {**derived, **stored, "derived": False}
    for key, fallback in derived.items():
        if board.get(key) is None and fallback is not None:
            board[key] = fallback
    if board.get("stage") not in STAGES:
        board["stage"] = derived["stage"]
    if not isinstance(board.get("revision"), int) or isinstance(board.get("revision"), bool):
        board["revision"] = 1
    board["stage"] = effective_stage(ticket.get("status"), board["stage"])
    # Visibility only: the flag stays across status changes until a human Restores the card.
    board["archived"] = board.get("archived") is True
    return board


def effective_stage(status: object, stored: str) -> str:
    """The lane a card shows: a DONE task is in Done, and only a DONE task is.

    A stored stage that contradicts the canonical status (an older card moved
    to Done by hand, a DONE ticket last seen in Next) is read as the status
    says; the next write stores the corrected stage.
    """
    if status == "DONE":
        return "Done"
    if stored == "Done":
        return STAGE_FROM_STATUS.get(status, "Next") if isinstance(status, str) else "Next"
    return stored


def _dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


# --------------------------------------------------------------------------
# automatic completion: only from a positively completed graph and gates
# --------------------------------------------------------------------------


def _has_blocker(blocker: object) -> bool:
    return blocker not in (None, False, "", {}, [])


_DECISION_FLAGS = ("approved", "granted", "passed")
_DECISION_WORDS = ("status", "state", "outcome", "decision")


def _positive(value: object) -> bool:
    """An explicit approval or pass. Anything else, including unknown, is not.

    In an object, every recognised decision field present must be positive and
    at least one must be present; a conflict (`approved: true` with `decision:
    "rejected"`) fails closed. Other keys (`source`, `name`, `required`, notes)
    are metadata and ignored.
    """
    if value is True:
        return True
    if isinstance(value, str):
        return value.strip().lower() in _POSITIVE
    if isinstance(value, dict):
        flags = [value[key] for key in _DECISION_FLAGS if key in value]
        words = [value[key] for key in _DECISION_WORDS if key in value]
        if not flags and not words:
            return False
        return (all(flag is True for flag in flags)
                and all(isinstance(word, str) and word.strip().lower() in _POSITIVE for word in words))
    return False


def _gate_problems(ticket: dict, graph: object = None, *, executed: bool = True) -> list[str]:
    """Declared approvals, gates and a ticket-level review not positively passed.

    With `executed` (completion), a declared commit, merge or publish approval
    also needs verified successful execution recorded on its approved request:
    an approval is permission, never the result. Approval prerequisites pass
    False and judge permissions only, so a workflow approval may come first.
    """
    problems = []
    approvals = ticket.get("approvals")
    if approvals is not None and not isinstance(approvals, dict):
        problems.append("approvals have an unsupported shape")
        approvals = {}
    approvals = approvals or {}
    required = ticket.get("required_approvals")
    if required is not None and not (isinstance(required, list) and all(isinstance(n, str) and n for n in required)):
        problems.append("required_approvals have an unsupported shape")
        required = []
    # Every declared approval counts unless it is explicitly optional (`required: false`) and
    # not named in required_approvals; a named one missing from approvals is not approved.
    names = list(required or [])
    names += [name for name, value in approvals.items()
              if name not in names and not (isinstance(value, dict) and value.get("required") is False)]
    problems += [f"required approval {name!r} is not positively approved"
                 for name in names if not _positive(approvals.get(name))]
    gates = ticket.get("gates")
    entries: list | None = []
    if gates is not None:
        if isinstance(gates, dict):
            entries = list(gates.items())
        elif isinstance(gates, list):
            entries = [(_dict(gate).get("name", index), gate) for index, gate in enumerate(gates)]
        else:
            entries = None
            problems.append("gates have an unsupported shape")
        problems += [f"gate {name!r} is not positively passed" for name, gate in entries or [] if not _positive(gate)]
    # Declared testing needs a recorded, passed `tests` gate; a missing one blocks like a failed one.
    if ticket.get("testing_required") is True:
        if not any(name == "tests" for name, _ in entries or []):
            problems.append("testing is required and no passed 'tests' gate is recorded")
    if ticket.get("review") is not None and not _positive(ticket["review"]):
        problems.append("the ticket's review is not positively passed")
    if executed:
        problems += execution_problems(ticket, [name for name in names if name in EXECUTED_ACTIONS], graph)
    return problems


def finished_cycle(graph: dict | None) -> dict | None:
    """The completion cycle of a graph whose every node finished, else None.

    A cycle is the graph's creation time plus its latest node completion time.
    The revision is deliberately not part of it: `link` raises the revision
    without advancing the workflow, and must not count as a new completion.
    """
    if graph is None or any(n["state"] not in ("completed", "skipped") for n in graph["nodes"]):
        return None
    stamps = [n["completed_at"] for n in graph["nodes"] if isinstance(n.get("completed_at"), str)]
    return {"graph_created_at": graph.get("created_at"), "completed_at": max(stamps) if stamps else None}


def _skip_problems(graph: dict) -> list[str]:
    """Skipped nodes the workflow does not allow to count as finished work.

    Investigations may be skipped. Implementation never may. A correction or
    re-review counts as skipped only after an earlier review passed (the skip
    a pass records). A skipped release is refused by the release rule.
    """
    problems = []
    passed = False
    for n in graph["nodes"]:
        if n["state"] == "skipped":
            if n["type"] == "implement":
                problems.append(f"{n['id']} was skipped; the implementation never completed")
            elif n["type"] in ("correct", "review") and not passed:
                problems.append(f"{n['id']} was skipped without an earlier passing review")
        if n["type"] == "review" and n["state"] == "completed" and n["outcome"] == "pass":
            passed = True
    return problems


def completion_check(ticket: dict, graph: dict | None, graph_problem: str | None = None) -> dict:
    """Whether the task graph positively completed this task, with every reason it did not.

    Unknown is never success: no graph, an invalid graph, no passed review, a
    required release without release approval, a missing grant, a blocker or
    an unreadable gate each keep the task open. A completion cycle already
    closed automatically, or reopened by a human, never closes it again.
    """
    reasons = []
    if graph is None:
        reasons.append(graph_problem or "no task graph")
    else:
        unfinished = [n["id"] for n in graph["nodes"] if n["state"] not in ("completed", "skipped")]
        if unfinished:
            reasons.append("task graph not complete: " + ", ".join(unfinished))
        reviews = [n for n in graph["nodes"] if n["type"] == "review" and n["state"] == "completed"]
        if not reviews or reviews[-1]["outcome"] != "pass":
            reasons.append("no review recorded as pass")
        reasons += _skip_problems(graph)
    auth = _dict(ticket.get("authorization"))
    if auth.get("granted") is not True:
        reasons.append("execution authorization is not explicitly granted on the ticket")
    if _has_blocker(ticket.get("blocker")):
        reasons.append("the ticket has a blocker")
    release = next((n for n in graph["nodes"] if n["type"] == "release"), None) if graph else None
    if release is not None or "release_granted" in auth or ticket.get("release_required") is True:
        if graph is not None and (release is None or release["state"] != "completed"):
            reasons.append("release is required and not completed in the task graph")
        if auth.get("release_granted") is not True:
            reasons.append("release approval (authorization.release_granted) is not recorded")
    reasons += _gate_problems(ticket, graph)
    cycle = finished_cycle(graph)
    reasons += _workflow_scope_problems(ticket, cycle)
    lifecycle = _dict(ticket.get("lifecycle"))
    if cycle is not None and cycle in (lifecycle.get("suppressed_cycle"), lifecycle.get("auto_done_cycle")):
        reasons.append("this graph completion was already closed or reopened; waiting for a new graph cycle")
    if ticket.get("status") == "DONE":
        reasons.insert(0, "already DONE")
    return {"eligible": not reasons, "reasons": reasons, "cycle": cycle,
            "graph_revision": graph.get("revision") if graph else None}


_BOOKKEEPING_REASONS = ("already DONE", "this graph completion was already closed or reopened")


def completion_view(ticket: dict, check: dict) -> dict | None:
    """How a DONE card labels its completion, judged again from the workflow records now.

    `verified` only while the same rules automatic completion uses still hold
    (validated graph, passed review, gates, approvals, execution evidence) and,
    for an automatic closure, for the graph cycle it closed. A board checkbox,
    a manual closure or an agent's claim never verifies anything. Read only.
    """
    if ticket.get("status") != "DONE":
        return None
    closure = _dict(_dict(ticket.get("lifecycle")).get("closure")) or {"kind": "recorded"}
    kind = closure.get("kind") if closure.get("kind") in ("manual", "automatic") else "recorded"
    reasons = [r for r in check["reasons"] if not r.startswith(_BOOKKEEPING_REASONS)]
    verified = not reasons and (kind != "automatic" or closure.get("cycle") == check["cycle"])
    if kind == "automatic":
        label = "Verified complete" if verified else "Closed by graph · records changed"
    else:
        label = ("Closed manually" if kind == "manual" else "Done as recorded") + (
            " · Verified" if verified else " · Unverified")
    # Authorizations the current canonical records still lack, named for a compact badge.
    pending = [action for action, marks in _AUTHORIZATION_MARKS if any(m in r for m in marks for r in reasons)]
    return {"kind": kind, "verified": verified, "label": label,
            "detail": closure.get("label") or RECORDED_CLOSURE, "reasons": reasons,
            "release_unauthorized": "release" in pending, "pending_authorization": pending}


_AUTHORIZATION_MARKS = (
    ("release", ("release is required", "release approval", "required approval 'release'",
                 "required approval 'workflow_complete'")),
    ("commit", ("required approval 'commit'", "approved commit ")),
    ("merge", ("required approval 'merge'", "approved merge ")),
    ("publish", ("required approval 'publish'", "approved publish ")),
)


# A running record moves a task only through this one check and only with the
# recorder proof (run_observability's `active.confirmed`); the store does not
# re-derive it.
ACTIVITY_ACTOR = "activity-sync"
ACTIVE_STATUSES = ("INVESTIGATING", "IMPLEMENTING", "REVIEW")
ACTIVITY_STATUS = {"investigator": "INVESTIGATING", "planner": "INVESTIGATING", "reviewer": "REVIEW"}


def activity_hold(ticket: dict, board: dict, run: object) -> str | None:
    """Why a running record must not move this task to In Progress, or None."""
    run = _dict(run)
    if (run.get("state") != "running" or _dict(run.get("active")).get("confirmed") is not True
            or not run.get("run_id")):
        return "the run is not confirmed active"
    if str(run.get("task_id")) != str(ticket.get("id")):
        return "the run is bound to another task"
    status = ticket.get("status")
    if status == "DONE":
        return "the task is DONE; a running record never reopens it"
    # Archived is visibility only: an explicitly reopened, archived task moves like any other.
    if status not in ("BACKLOG", "READY") + ACTIVE_STATUSES:
        return f"status {status!r} holds the task"
    if _dict(ticket.get("authorization")).get("granted") is not True:
        return "execution is not authorized on the ticket"
    if _has_blocker(ticket.get("blocker")):
        return "the ticket has a blocker"
    if board.get("stage") == "Blocked":
        return "the card is held in Blocked"
    requests = ticket.get("approval_requests")
    if isinstance(requests, list) and any(isinstance(r, dict) and r.get("state") == "pending" for r in requests):
        return "a human approval decision is pending"
    if _dict(_dict(ticket.get("lifecycle")).get("activity_run")).get("run_id") == run.get("run_id"):
        return "already moved for this run"
    if status in ACTIVE_STATUSES and board.get("stage") == "In Progress":
        return "already in progress"
    return None


def reopen_status(ticket: dict) -> str:
    """READY only for a granted, unblocked task with acceptance criteria; else BACKLOG."""
    criteria = ticket.get("acceptance_criteria")
    if (_dict(ticket.get("authorization")).get("granted") is True and not _has_blocker(ticket.get("blocker"))
            and isinstance(criteria, list) and criteria):
        return "READY"
    return "BACKLOG"


def manual_ticket(task_id: int, fields: dict, *, actor: str, now: str) -> dict:
    """A new canonical ticket for a manually added task.

    BACKLOG, not authorized, no blocker, and no acceptance criteria or review
    invented on anyone's behalf.
    """
    changes = {"title": {"from": None, "to": fields["title"]}, "status": {"from": None, "to": "BACKLOG"}}
    document: dict = {"id": task_id, "title": fields["title"], "status": "BACKLOG"}
    if fields.get("details"):
        document["scope"] = {"summary": fields["details"]}
        changes["details"] = {"from": None, "to": fields["details"]}
    document.update({
        "authorization": {"granted": False, "source": MANUAL_AUTHORIZATION},
        "blocker": None,
        "updated_at": now,
        "lifecycle": {"origin": "manual", "created_at": now, "created_by": actor, "closure": None},
        "history": [{"at": now, "by": actor, "action": "create", "revision": 1, "changes": changes}],
    })
    board = default_board(document)
    board.update(
        derived=False, stage="Next", owner=fields.get("owner") or UNKNOWN,
        next_action=fields.get("next_action") or UNKNOWN, gate=fields.get("gate") or UNKNOWN,
        notes=fields.get("notes") or "", revision=1, updated_at=now, updated_by=actor,
    )
    document["board"] = board
    return document


# --------------------------------------------------------------------------
# conversation approval bridge: one revision-bound request and decision record
# --------------------------------------------------------------------------
#
# An agent that reaches a human gate records an approval request on the
# canonical ticket (`approval_requests`) for one explicit task, one gate and one
# typed action, bound to the task revision, the ticket's gate-relevant content
# and the task graph's revision and content. The human answers Approve, Request
# Changes or Defer -- in the conversation (`nlh approval decide`) or in Inspect
# -- and both paths call the same `Board.decide`. A decision must name the
# pending request, its task, gate, action and binding exactly; anything stale,
# ambiguous or mismatched is refused before anything is written. Nothing here
# runs git, a worker or a shell: an approved commit, merge or publish is a
# recorded permission the existing agent then acts on with its ordinary tools.
# The agent executes it at once, verifies the result and records execution
# evidence on that exact approved request (`nlh approval execute`); only a
# recorded, verified success counts toward completion, never the approval.

APPROVAL_ACTIONS = ("task_close", "workflow_complete", "commit", "merge", "publish")
DECISIONS = ("approve", "request_changes", "defer")
DECIDED_STATE = {"approve": "approved", "request_changes": "changes_requested", "defer": "deferred"}
# The one explicit permission each approved action records. release_granted is
# written only by workflow_complete; commit, merge and publish never imply it.
GRANT_FLAGS = {"workflow_complete": "release_granted", "commit": "commit_granted",
               "merge": "merge_granted", "publish": "publish_granted"}
ACTION_SCOPES = {
    "task_close": "close the task administratively (the existing manual closure); records no review, "
                  "release, commit, merge or publication",
    "workflow_complete": "complete the task graph's ready release gate and record release approval "
                         "(authorization.release_granted); the task then closes automatically if every "
                         "other gate holds. Nothing is committed, merged or published",
    "commit": "permit the existing agent to make one git commit of the bound content; this record "
              "performs no git operation",
    "merge": "permit the existing agent to merge the bound, already committed content; this record "
             "performs no git operation",
    "publish": "permit the existing agent to push/publish the bound, already committed content; this "
               "record performs no git operation",
}
TASK_ID_TEXT = re.compile(r"^[1-9][0-9]{0,8}$")
_GATE_TEXT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
# Written by requests and decisions themselves (and the board revision is bound
# separately), so they are not part of the gate-relevant ticket content.
_UNBOUND = ("board", "history", "approval_requests", "updated_at")
_REQUEST_KEYS = ("id", "task_id", "action", "gate_id", "scope", "binding", "binding_token", "summary",
                 "evidence", "content_ref", "resolves_blocker", "requested_at", "requested_by", "via",
                 "state", "decision", "execution")
# Approved actions the existing agent must then perform, verify and record.
EXECUTED_ACTIONS = ("commit", "merge", "publish")
EXECUTION_OUTCOMES = ("succeeded", "failed")
_COMMIT_SHA = re.compile(r"^[0-9a-f]{7,64}$")


def parse_task_id(value: object) -> int:
    """A canonical task id given explicitly: a positive integer, never guessed or allocated."""
    text = str(value).strip() if isinstance(value, (str, int)) and not isinstance(value, bool) else ""
    if not TASK_ID_TEXT.match(text):
        raise StoreError(f"an explicit existing task id (a positive integer) is required, not {value!r}")
    return int(text)


def default_gate(action: str) -> str:
    return "release" if action == "workflow_complete" else action


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]


def current_binding(ticket: dict, revision: int, graph_raw: object) -> dict:
    """What a request is bound to: task revision, gate-relevant ticket content, graph revision and content."""
    return {
        "task_revision": revision,
        "ticket_sha": _digest({key: value for key, value in ticket.items() if key not in _UNBOUND}),
        "graph_revision": graph_raw.get("revision") if isinstance(graph_raw, dict) else None,
        "graph_sha": _digest(graph_raw) if graph_raw is not None else None,
    }


_SCOPE_UNBOUND = _UNBOUND + ("status", "lifecycle")


def scope_digest(ticket: dict) -> str:
    """The task's substantive content, as an approved workflow is bound to it.

    Leaves out bookkeeping (board, history, approval requests, timestamps), the
    status and closure record a completion derives, and the action approvals and
    grant flags themselves -- including the workflow approval's own record -- so
    the scope never invalidates itself and never hashes its own hash.
    """
    doc = {key: value for key, value in ticket.items() if key not in _SCOPE_UNBOUND}
    if isinstance(doc.get("approvals"), dict):
        doc["approvals"] = {k: v for k, v in doc["approvals"].items() if k not in GRANT_FLAGS}
    if isinstance(doc.get("authorization"), dict):
        doc["authorization"] = {k: v for k, v in doc["authorization"].items() if k not in GRANT_FLAGS.values()}
    return _digest(doc)


def _workflow_scope_problems(ticket: dict, cycle: dict | None) -> list[str]:
    """A managed workflow approval authorizes only the content and graph cycle it approved.

    A ticket with no approval recorded through a request keeps the legacy rule
    (an explicit release_granted). One recorded through a request counts only
    while the substantive content and the completed graph cycle are the ones
    it was granted for: a reopen with changed content or a new cycle needs a
    new approval. Request and decision bookkeeping changes neither.
    """
    approval = _dict(ticket.get("approvals")).get("workflow_complete")
    if not (isinstance(approval, dict) and approval.get("request_id")):
        return []
    scope = approval.get("scope")
    if (isinstance(scope, dict) and cycle is not None and scope.get("cycle") == cycle
            and scope.get("ticket_sha") == scope_digest(ticket)):
        return []
    return [f"the workflow approval ({approval.get('request_id')}) was granted for other task content or "
            "another graph cycle; request a new workflow_complete approval"]


def execution_scope(ticket: dict) -> str:
    """The content an approved action and its execution evidence are bound to.

    The scope digest without the blocker: a workflow approval that clears the
    gate's blocker does not change what was committed, merged or published.
    """
    return scope_digest({key: value for key, value in ticket.items() if key != "blocker"})


def work_cycle(graph: object) -> dict | None:
    """The work cycle an approved action and its execution belong to: the task graph's identity.

    Only the graph's creation time, so the expected release-gate transition
    within one graph keeps the cycle, while a rebuilt graph starts a new one.
    """
    return {"graph_created_at": graph.get("created_at")} if isinstance(graph, dict) else None


def _approved_request(ticket: dict, request_id: object) -> dict | None:
    requests = ticket.get("approval_requests")
    found = [r for r in requests if isinstance(r, dict) and r.get("id") == request_id] \
        if isinstance(requests, list) else []
    return found[0] if len(found) == 1 and found[0].get("state") == "approved" else None


def approval_bound_problem(ticket: dict, action: str, approval: dict, request: dict, cycle: dict | None) -> str | None:
    """Why an approved action's permission does not hold for this task, content and work cycle, or None.

    The approval and its request must agree (task, action, binding, a positive
    decision on that binding). An approval without a recorded content scope and
    work cycle predates that binding and cannot be shown current: it needs a
    fresh approval rather than being stamped with today's content.
    """
    token = approval.get("binding_token")
    decision = _dict(request.get("decision"))
    if not (_positive(approval) and isinstance(token, str) and token and request.get("task_id") == ticket.get("id")
            and request.get("action") == action and request.get("binding_token") == token
            and decision.get("decision") == "approve" and decision.get("binding_token") == token):
        return (f"approved {action} ({approval.get('request_id')}) does not match its request (task, action, "
                f"binding or decision); request a fresh {action} approval")
    if not isinstance(approval.get("scope_sha"), str) or "cycle" not in approval:
        return (f"approved {action} ({approval.get('request_id')}) predates content and work-cycle binding; "
                f"request a fresh {action} approval")
    if approval["scope_sha"] != execution_scope(ticket) or approval["cycle"] != cycle:
        return (f"approved {action} ({approval.get('request_id')}) was granted for other task content or another "
                f"work cycle; request a fresh {action} approval")
    return None


def _execution_complete(execution: dict, approval: dict, action: str) -> bool:
    """A success record that names exactly its approval, a result and verification evidence."""
    result, evidence = execution.get("result_ref"), execution.get("evidence")
    return (execution.get("request_id") == approval.get("request_id") and execution.get("action") == action
            and execution.get("binding_token") == approval.get("binding_token")
            and execution.get("scope_sha") == approval.get("scope_sha")
            and execution.get("cycle") == approval.get("cycle")
            and isinstance(result, str) and bool(result.strip())
            and (action != "commit" or bool(_COMMIT_SHA.match(result)))
            and isinstance(evidence, list) and bool(evidence)
            and all(isinstance(item, str) and item.strip() for item in evidence))


def execution_problems(ticket: dict, actions: list[str], graph: object = None) -> list[str]:
    """Declared commit, merge or publish approvals without verified successful execution.

    Success counts only when the whole chain holds: the approval, its own
    approved request and the execution recorded on it name the same task,
    action and binding, the decision approved that binding, the approval is
    bound to the current content and work cycle, and the success names its
    result and evidence. Nothing is grandfathered: a request-less (legacy) or
    pre-binding approval, no execution, a failed, incomplete or earlier-cycle
    one leaves the task open. A legacy approval is migrated by a fresh
    approved request plus recorded, verified execution.
    """
    problems = []
    for action in actions:
        approval = _dict(ticket.get("approvals")).get(action)
        request_id = approval.get("request_id") if isinstance(approval, dict) else None
        request = _approved_request(ticket, request_id) if request_id else None
        if request is None:
            problems.append(f"approved {action} has no approved request to carry execution evidence; request "
                            f"and approve {action}, perform it, verify and record it (nlh approval execute)")
            continue
        stale = approval_bound_problem(ticket, action, approval, request, work_cycle(graph))
        if stale:
            problems.append(stale)
            continue
        execution = _dict(request.get("execution"))
        if execution.get("outcome") == "succeeded":
            if not _execution_complete(execution, approval, action):
                problems.append(f"the recorded {action} execution on {request_id} is incomplete or does not match "
                                "its approval (request, action, binding, content, work cycle, result and "
                                "evidence must all match); record a verified execution")
        elif execution.get("outcome") == "failed":
            problems.append(f"approved {action} ({request_id}) failed when executed; fix it, {action} again, "
                            "verify and record a successful result")
        else:
            problems.append(f"approved {action} ({request_id}) has no verified successful execution recorded; the "
                            f"agent must {action} now, verify it and record it (nlh approval execute)")
    return problems


def binding_holds(binding: object, ticket: dict, revision: int, graph_raw: object) -> bool:
    """Whether a request's binding still describes the task and graph exactly.

    The gate-relevant ticket content and the graph must be identical. The task
    revision may have moved only through revisions written by approval requests
    and decisions themselves (their audit entries carry those revisions), or by
    an Archive/Restore that changed board visibility only (no `changes`), so a
    second request or a view change does not make the first one stale; any
    other write does.
    """
    if not isinstance(binding, dict):
        return False
    now = current_binding(ticket, revision, graph_raw)
    if any(binding.get(key) != now[key] for key in ("ticket_sha", "graph_revision", "graph_sha")):
        return False
    bound = binding.get("task_revision")
    if not isinstance(bound, int) or isinstance(bound, bool) or bound > revision:
        return False
    history = ticket.get("history") if isinstance(ticket.get("history"), list) else []
    own = {entry.get("revision") for entry in history if isinstance(entry, dict)
           and (entry.get("action") in ("approval_request", "approval_decision", "approval_execution")
                or (entry.get("action") in ("archive", "restore") and "changes" not in entry))}
    return all(r in own for r in range(bound + 1, revision + 1))


def binding_token(binding: dict) -> str:
    """A readable identifier of the binding (not a secret): t<rev>.<ticket>-g<rev>.<graph>."""
    graph = f"g{binding['graph_revision']}.{binding['graph_sha']}" if binding.get("graph_sha") else "g-none"
    return f"t{binding['task_revision']}.{binding['ticket_sha']}-{graph}"


def _clean_evidence(items: object) -> list[str]:
    if items is None:
        return []
    if not isinstance(items, list) or len(items) > 20:
        raise StoreError("evidence must be a list of at most 20 repository paths or http(s) URLs")
    return [_clean_link({"target": item, "kind": "evidence"})["target"] for item in items]


def approval_problems(action: str, gate: str, ticket: dict, graph: dict | None, graph_problem: str | None,
                      *, resolves_blocker: object = None) -> list[str]:
    """Every prerequisite the action's gate does not meet now. Checked before anything is written."""
    problems = []
    auth = ticket.get("authorization")
    if auth is not None and not isinstance(auth, dict):
        problems.append("authorization has an unsupported shape")
    auth = _dict(auth)
    if gate != default_gate(action):
        problems.append(f"{action} is decided at gate {default_gate(action)!r}, not {gate!r}")
    if action in ("task_close", "workflow_complete") and ticket.get("status") == "DONE":
        problems.append("the task is already DONE")
    if action in ("workflow_complete", "commit", "merge", "publish") and auth.get("granted") is not True:
        problems.append("execution authorization is not explicitly granted on the ticket")
    if action in ("workflow_complete", "commit"):
        if graph is None:
            problems.append(graph_problem or "no task graph")
        else:
            reviews = [n for n in graph["nodes"] if n["type"] == "review" and n["state"] == "completed"]
            if not reviews or reviews[-1]["outcome"] != "pass":
                problems.append("no review recorded as pass")
            problems += _skip_problems(graph)
            if task_graph.node(graph, "implement")["state"] != "completed":
                problems.append("the implementation is not completed")
    if action == "workflow_complete" and graph is not None:
        release = next((n for n in graph["nodes"] if n["type"] == "release"), None)
        if release is None or release["id"] != gate:
            problems.append(f"the task graph has no release gate {gate!r}")
        else:
            if release["state"] not in ("pending", "blocked"):
                problems.append(f"release gate {gate!r} is {release['state']}, not waiting for approval")
            unfinished = [n["id"] for n in graph["nodes"] if n is not release and n["state"] not in ("completed", "skipped")]
            if unfinished:
                problems.append("required nodes not finished: " + ", ".join(unfinished))
        # Its own approval is what this decision supplies: judge every other gate as if it were given.
        prospective = copy.deepcopy(ticket)
        if ticket.get("approvals") is None or isinstance(ticket.get("approvals"), dict):
            prospective["approvals"] = {**_dict(ticket.get("approvals")), action: {"decision": "approved"}}
        problems += _gate_problems(prospective, executed=False)
        blocker = ticket.get("blocker")
        if _has_blocker(blocker) and resolves_blocker is None:
            problems.append(f"the ticket has a blocker this request does not resolve ({blocker!r}); request "
                            "with resolves_blocker only when approving this gate is exactly what clears it")
        elif resolves_blocker is not None and blocker != resolves_blocker:
            problems.append("the blocker this request resolves is no longer the ticket's blocker")
    if action in ("merge", "publish") and auth.get("commit_granted") is not True:
        problems.append("commit has not been approved (authorization.commit_granted)")
    return problems


def approval_offer(task_id: int, title: str, request: dict) -> str:
    """The human-readable Approve / Request Changes / Defer offer for one pending request."""
    base = (f"nlh approval decide {task_id} --request {request['id']} --action {request['action']} "
            f"--gate {request['gate_id']} --binding {request['binding_token']}")
    binding = request["binding"]
    lines = [
        f"Approval requested for Task #{task_id} — {title}",
        f"  Action:   {request['action']} — {request['scope']}",
        f"  Gate:     {request['gate_id']}",
        f"  Request:  {request['id']}",
        f"  Binding:  {request['binding_token']} (task revision {binding['task_revision']}, graph revision "
        f"{binding['graph_revision'] if binding['graph_sha'] else 'none'})",
        f"  Summary:  {request['summary']}",
    ]
    if request.get("content_ref"):
        lines.append(f"  Content:  {request['content_ref']}")
    lines += ["  Evidence: " + (", ".join(request["evidence"]) or "none given")]
    if request.get("resolves_blocker") is not None:
        lines.append(f"  Approving also clears this blocker: {_blocker_gate(request['resolves_blocker']) or request['resolves_blocker']}")
    lines += [
        "Reply Approve, Request Changes or Defer:",
        f"  Approve          {base} --approve",
        f"  Request Changes  {base} --request-changes --note \"...\"",
        f"  Defer            {base} --defer",
        "The same request shows in the Companion under Inspect. Nothing is approved until a human decides.",
    ]
    return "\n".join(lines)


def _request_view(request: dict, holds: bool) -> dict:
    view = {key: copy.deepcopy(request.get(key)) for key in _REQUEST_KEYS}
    state = request.get("state")
    view["effective_state"] = "stale" if state == "pending" and not holds else state
    return view


def card_meta(ticket: dict, board: dict, graph: dict | None, approvals: list[dict],
              checkpoint: dict | None) -> dict:
    """What a card shows when its board fields are unknown, and where each value came from.

    Read only: nothing here is written back, so presentation never changes a
    gate. Board values win; then a pending or decided approval request, the
    ticket blocker, the checkpoint handoff and the graph's next step. A missing
    owner is Unassigned; nothing is called approved unless a decision says so.
    """
    def pick(*candidates):
        for source, value in candidates:
            if isinstance(value, str) and value.strip() and not _is_unknown(value):
                return {"value": value.strip(), "source": source}
        return {"value": UNKNOWN, "source": "unknown"}

    board = _dict(ticket.get("board"))  # as stored: a value the board derived itself is not a board value
    pending = [a for a in approvals if a["state"] == "pending"]
    decided = [a for a in approvals if a["state"] in ("approved", "changes_requested", "deferred")]
    approval_next = approval_gate = None
    current = _dict(ticket.get("approvals"))
    awaiting = [a for a in approvals if a["state"] == "approved" and a["action"] in EXECUTED_ACTIONS
                and _dict(current.get(a["action"])).get("request_id") == a["id"]
                and _dict(a.get("execution")).get("outcome") != "succeeded"]
    if awaiting:
        a = awaiting[-1]
        failed = _dict(a.get("execution")).get("outcome") == "failed"
        approval_next = ((f"Approved {a['action']} failed when executed; fix it, " if failed else
                          f"Execute approved {a['action']} now: ")
                         + f"{a['action']} exactly the bound content with ordinary tools, verify it, then record: "
                         f"nlh approval execute {a['task_id']} --request {a['id']} --action {a['action']} "
                         f"--binding {a['binding_token']} --succeeded --result-ref <ref> --evidence <path>")
        approval_gate = f"Execution pending: approved {a['action']} ({a['id']})"
    elif pending:
        a = pending[-1]
        if a["effective_state"] == "stale":
            approval_next = (f"Approval request {a['id']} is stale (the task or graph changed); reissue it with "
                             f"nlh approval request {a['task_id']} --action {a['action']}")
            approval_gate = f"Approval stale: {a['action']} at gate {a['gate_id']}"
        else:
            approval_next = (f"Human decision needed: Approve, Request Changes or Defer {a['action']} at gate "
                             f"{a['gate_id']} (request {a['id']}, binding {a['binding_token']})")
            approval_gate = f"Approval pending: {a['action']} at gate {a['gate_id']}"
    elif decided:
        a = decided[-1]
        note = _dict(a.get("decision")).get("note")
        if a["state"] == "changes_requested":
            approval_next = (f"Changes requested on {a['action']}" + (f": {note}" if note else "")
                             + "; address them, then reissue the approval request")
        elif a["state"] == "deferred":
            approval_next = f"{a['action']} deferred" + (f": {note}" if note else "") + "; reissue the request when ready"
        elif a["action"] in EXECUTED_ACTIONS:
            execution = _dict(a.get("execution"))
            approval_next = (f"{a['action']} executed and verified ({execution.get('result_ref')}); evidence "
                             f"recorded on request {a['id']}" if execution.get("outcome") == "succeeded" else
                             f"Approved {a['action']} only (request {a['id']}): the agent may now {a['action']} "
                             "the bound content with its ordinary tools; the record performed nothing")
    blocker = ticket.get("blocker")
    blocker_next = blocker.get("human_action") if isinstance(blocker, dict) else None
    plan = _dict(_dict(checkpoint).get("handoff")).get("plan")
    graph_next = None
    if graph is not None and task_graph is not None:
        graph_next = "Graph: " + task_graph.next_action(graph)
    later = [("blocker", blocker_next), ("blocker", _blocker_gate(blocker)),
             ("checkpoint", plan[0] if isinstance(plan, list) and plan else None), ("graph", graph_next)]
    if ticket.get("status") == "DONE":  # an old handoff or graph step is not a next action for a closed task
        later = [("status", "Nothing further: the task is DONE")]
    return {
        "next_action": pick(("board", board.get("next_action")), ("approval_request", approval_next), *later),
        "gate": pick(("board", board.get("gate")), ("approval_request", approval_gate),
                     ("blocker", _blocker_gate(blocker))),
        "owner": pick(("board", board.get("owner"))) if not _is_unknown(board.get("owner"))
        else {"value": "Unassigned", "source": "missing"},
        "approval": pending[-1]["effective_state"] if pending else (decided[-1]["state"] if decided else None),
        "approval_action": (pending or decided)[-1]["action"] if (pending or decided) else None,
    }


def _is_unknown(value: object) -> bool:
    return not isinstance(value, str) or not value.strip() or value.strip().lower() == UNKNOWN


class Board:
    def __init__(self, workflow_dir: Path):
        self.workflow = Path(workflow_dir)
        self.tickets = self.workflow / "tickets"
        self.lock_path = self.workflow / "tickets" / ".task-board.lock"

    # -- reading ---------------------------------------------------------

    def ticket_ids(self) -> list[int]:
        """Canonical integer tickets only.

        Other files (`12.closeout.json`, `12.notes.md`, `LEGACY-A.json`) are
        archives or legacy records: they are never cards and are never touched.
        """
        if not self.tickets.is_dir():
            return []
        found = []
        for path in self.tickets.iterdir():
            match = CANONICAL_TICKET.match(path.name)
            if match:
                found.append(int(match.group(1)))
        return sorted(found)

    def read_ticket(self, task_id: int) -> dict:
        path = self.tickets / f"{task_id}.json"
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise StoreError(f"Task #{task_id} does not exist", code=404) from None
        except json.JSONDecodeError as exc:
            raise StoreError(f"Task #{task_id} is not readable JSON: {exc}", code=500) from None
        if not isinstance(document, dict):
            raise StoreError(f"Task #{task_id} is not a ticket object", code=500)
        return document

    def graph(self, task_id: int) -> tuple[dict | None, str | None]:
        """The task's graph as task_graph validates it, or None and why not."""
        path = self.workflow / "graphs" / f"{task_id}.json"
        if not path.is_file():
            return None, "no task graph"
        if task_graph is None:
            return None, "the task graph library is unavailable"
        try:
            graph = json.loads(path.read_text(encoding="utf-8"))
            task_graph.validate(graph, str(task_id))
        except (OSError, ValueError, TypeError, KeyError, AttributeError, task_graph.Refused) as exc:
            return None, f"task graph unreadable or invalid: {exc}"
        return graph, None

    def graph_raw(self, task_id: int) -> object:
        """The graph file's content as stored (for binding), or None when there is no graph."""
        path = self.workflow / "graphs" / f"{task_id}.json"
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return None
        try:
            return json.loads(data)
        except ValueError:
            return {"unreadable_sha256": hashlib.sha256(data).hexdigest()}

    def completion(self, task_id: int, ticket: dict) -> dict:
        graph, problem = self.graph(task_id)
        return completion_check(ticket, graph, problem)

    def _requests(self, task_id: int, ticket: dict) -> list:
        requests = ticket.get("approval_requests")
        if requests is None:
            return []
        if not isinstance(requests, list) or not all(isinstance(r, dict) for r in requests):
            raise StoreError(f"Task #{task_id}: its approval requests have an unsupported shape and are not "
                             "overwritten", code=409)
        return requests

    def approvals(self, task_id: int, ticket: dict, revision: int) -> list[dict]:
        """Every approval request on the ticket, with pending ones checked against the current binding."""
        try:
            requests = self._requests(task_id, ticket)
        except StoreError:
            return []
        graph_raw = self.graph_raw(task_id)
        return [_request_view(request, binding_holds(request.get("binding"), ticket, revision, graph_raw))
                for request in requests]

    def _checkpoint(self, task_id: int) -> dict | None:
        try:
            value = json.loads((self.workflow / "checkpoints" / f"{task_id}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    def card(self, task_id: int) -> dict:
        ticket = self.read_ticket(task_id)
        board = _normalise_board(ticket.get("board"), ticket)
        graph, graph_problem = self.graph(task_id)
        check = completion_check(ticket, graph, graph_problem)
        approvals = self.approvals(task_id, ticket, board["revision"])
        relations = ticket.get("relations")
        blocker = ticket.get("blocker")
        summary = _dict(ticket.get("scope")).get("summary")
        details = summary.strip() if isinstance(summary, str) and summary.strip() else None
        lifecycle = _dict(ticket.get("lifecycle"))
        history = ticket.get("history")
        status = ticket.get("status") or UNKNOWN
        closure = None
        if status == "DONE":
            closure = lifecycle.get("closure") if isinstance(lifecycle.get("closure"), dict) else {
                "kind": "recorded", "label": RECORDED_CLOSURE}
        out = {
            "id": task_id,
            "display": f"Task #{task_id}",
            # A board display title (set at intake) overrides the ticket title
            # for reading until the canonical title is edited.
            "title": board.get("display_title") or ticket.get("title") or UNKNOWN,
            "ticket_title": ticket.get("title") or UNKNOWN,
            "original_title": lifecycle.get("original_title"),
            "status": status,
            "updated_at": ticket.get("updated_at"),
            "scope_summary": details or UNKNOWN,
            # Canonical details (`scope.summary`); board notes are separate.
            "details": details,
            "authorization_granted": bool(_dict(ticket.get("authorization")).get("granted")),
            "blocker_reason": blocker.get("reason") if isinstance(blocker, dict) else None,
            "closure": closure,
            "origin": lifecycle.get("origin"),
            "history": history if isinstance(history, list) else [],
            "auto_done": check,
            "completion": completion_view(ticket, check),
            "approvals": approvals,
            "relations": [r for r in relations if isinstance(r, dict)] if isinstance(relations, list) else [],
            "followed_up_by": [],
            "meta": card_meta(ticket, board, graph, approvals, self._checkpoint(task_id)),
            "ticket_path": f".workflow/tickets/{task_id}.json",
            "source_refs": ticket.get("source_refs") or [],
            "board": board,
        }
        # A decided request keeps saying what it authorized, from its stored decision, on every reload.
        for view in approvals:
            if view.get("state") in DECIDED_STATE.values():
                problem = None
                if view.get("state") == "approved" and view.get("action") in EXECUTED_ACTIONS:
                    grant = _dict(_dict(ticket.get("approvals")).get(view["action"]))
                    problem = ("a newer approval supersedes this request" if grant.get("request_id") != view["id"]
                               else approval_bound_problem(ticket, view["action"], grant, view, work_cycle(graph)))
                view["next_step"] = ("Historical approval: " + problem + ". It does not authorize current content."
                                     if problem else _next_step(task_id, view, out))
        return out

    def cards(self) -> list[dict]:
        """Every card, after the read path's one explicit completion sync per task."""
        out = []
        for task_id in self.ticket_ids():
            self.sync_completion(task_id)
            out.append(self.card(task_id))
        # A follow-up is recorded on the later task only; the earlier one shows it read-only.
        by_id = {card["id"]: card for card in out}
        for card in out:
            for relation in card["relations"]:
                if relation.get("type") == "follow_up_of" and relation.get("task_id") in by_id:
                    by_id[relation["task_id"]]["followed_up_by"].append(card["id"])
        return out

    # -- writing ---------------------------------------------------------

    def update(
        self,
        task_id: int,
        patch: dict,
        *,
        expected_revision: object,
        actor: str,
        now: str,
        system: dict | None = None,
        action: str = "edit",
        note: str | None = None,
    ) -> dict:
        """Apply a board and lifecycle patch under lock, rejecting a stale edit.

        `expected_revision` is required: without it two editors would silently
        overwrite each other. Pass the revision the editor loaded.

        `action` is `edit`, `done` (manual closure), `reopen`, `archive` or
        `restore`. Every check runs on a copy before the single atomic write,
        so a refusal changes nothing.

        `system` carries fields the creation path owns (`SYSTEM_FIELDS`). They are
        never reachable from a client payload, because `clean_patch` refuses them.
        """
        if not isinstance(expected_revision, int) or isinstance(expected_revision, bool):
            raise StoreError("expected_revision (an integer) is required so stale edits are not lost")
        if action not in ("edit", "done", "reopen", "archive", "restore"):
            raise StoreError(f"unknown lifecycle action {action!r}")
        for field in system or {}:
            if field not in SYSTEM_FIELDS:
                raise StoreError(f"{field} is not a system field")
        with _Lock(self.lock_path):
            ticket = self.read_ticket(task_id)
            board = _normalise_board(ticket.get("board"), ticket)
            if board["revision"] != expected_revision:
                raise StaleEdit(task_id, expected_revision, board["revision"])
            document = self._apply(task_id, ticket, board, dict(patch), action=action, actor=actor, now=now,
                                   note=note, system=system or {}, revision=expected_revision + 1)
            _replace_atomically(self.tickets / f"{task_id}.json", document)
        return self.card(task_id)

    def close(self, task_id: int, *, expected_revision: object, actor: str, now: str,
              note: str | None = None) -> dict:
        """Mark Done: a labelled administrative closure. Gates, grants and the graph are untouched."""
        return self.update(task_id, {}, expected_revision=expected_revision, actor=actor, now=now,
                           action="done", note=note)

    def reopen(self, task_id: int, *, expected_revision: object, actor: str, now: str,
               status: str | None = None) -> dict:
        """Reopen a DONE task to BACKLOG or READY; nothing is granted and the graph is untouched."""
        if status is not None and status not in REOPEN_STATUSES:
            raise StoreError("a DONE task reopens to BACKLOG or READY only")
        return self.update(task_id, {"status": status} if status else {}, expected_revision=expected_revision,
                           actor=actor, now=now, action="reopen")

    def archive(self, task_id: int, *, expected_revision: object, actor: str, now: str) -> dict:
        """Hide a DONE task in Archived. Visibility only: id, path, history, graph and approvals stay."""
        return self.update(task_id, {}, expected_revision=expected_revision, actor=actor, now=now, action="archive")

    def restore(self, task_id: int, *, expected_revision: object, actor: str, now: str) -> dict:
        """Show an archived task on the board again; nothing else changes."""
        return self.update(task_id, {}, expected_revision=expected_revision, actor=actor, now=now, action="restore")

    def archive_many(self, items: object, *, actor: str, now: str) -> list[dict]:
        """Archive several DONE tasks, all or none.

        `items` is `[{"id": n, "expected_revision": r}, ...]`. Under one lock
        every task is read and checked (exists, DONE, not archived, revision
        current) before anything is written; one refusal writes nothing. A
        write failure part way puts back the tickets already written.
        """
        if not isinstance(items, list) or not items:
            raise StoreError("tasks must be a non-empty list of {id, expected_revision}")
        if len(items) > 500:
            raise StoreError("at most 500 tasks can be archived at once")
        wanted = []
        for item in items:
            if not isinstance(item, dict) or set(item) - {"id", "expected_revision"}:
                raise StoreError("each task is {id, expected_revision}")
            revision = item.get("expected_revision")
            if not isinstance(revision, int) or isinstance(revision, bool):
                raise StoreError("expected_revision (an integer) is required for every task")
            wanted.append((parse_task_id(item.get("id")), revision))
        if len({task_id for task_id, _ in wanted}) != len(wanted):
            raise StoreError("a task is named more than once")
        with _Lock(self.lock_path):
            planned = []
            for task_id, expected in wanted:
                ticket = self.read_ticket(task_id)
                board = _normalise_board(ticket.get("board"), ticket)
                if board["revision"] != expected:
                    raise StaleEdit(task_id, expected, board["revision"])
                planned.append((task_id, ticket, self._apply(task_id, ticket, board, {}, action="archive",
                                                             actor=actor, now=now, note=None, system={},
                                                             revision=expected + 1)))
            written = []
            try:
                for task_id, ticket, document in planned:
                    _replace_atomically(self.tickets / f"{task_id}.json", document)
                    written.append((task_id, ticket))
            except BaseException:
                for task_id, ticket in written:
                    _replace_atomically(self.tickets / f"{task_id}.json", ticket)
                raise
        return [self.card(task_id) for task_id, _ in wanted]

    def _apply(self, task_id: int, ticket: dict, board: dict, patch: dict, *, action: str, actor: str,
               now: str, note: str | None, system: dict, revision: int) -> dict:
        """The new ticket document for one write, or a refusal. Nothing is written here."""
        doc = copy.deepcopy(ticket)
        board = copy.deepcopy(board)
        lifecycle_patch = {field: patch.pop(field) for field in LIFECYCLE_FIELDS if field in patch}
        if action == "done":
            lifecycle_patch["status"] = "DONE"
        elif action == "reopen":
            lifecycle_patch.setdefault("status", reopen_status(doc))

        life = doc.get("lifecycle")
        life = {} if life is None else life
        history = doc.get("history")
        history = [] if history is None else history
        if lifecycle_patch:
            if not isinstance(life, dict):
                raise StoreError(f"Task #{task_id}: its lifecycle record has an unsupported shape and is "
                                 "not overwritten", code=409)
            if not isinstance(history, list):
                raise StoreError(f"Task #{task_id}: its history has an unsupported shape and is not "
                                 "overwritten", code=409)

        old_status = doc.get("status")
        new_status = lifecycle_patch.get("status", old_status)
        closing = new_status == "DONE" and old_status != "DONE"
        reopening = old_status == "DONE" and new_status != "DONE"
        if action == "done" and not closing:
            raise StoreError(f"Task #{task_id} is already DONE", code=409)
        if action == "reopen" and not reopening:
            raise StoreError(f"Task #{task_id} is not DONE; there is nothing to reopen", code=409)
        if reopening and new_status not in REOPEN_STATUSES:
            raise StoreError("a DONE task reopens to BACKLOG or READY only")
        archived = board.get("archived") is True
        if action == "archive":
            if old_status != "DONE":
                raise StoreError(f"Task #{task_id} is not DONE; only a completed task can be archived", code=409)
            if archived:
                raise StoreError(f"Task #{task_id} is already archived", code=409)
            patch["archived"] = True
        elif action == "restore":
            if not archived:
                raise StoreError(f"Task #{task_id} is not archived; there is nothing to restore", code=409)
            patch["archived"] = False

        # The lane never contradicts the canonical status, and a move is never a closure.
        requested = patch.get("stage")
        if new_status == "DONE":
            if requested not in (None, "Done"):
                raise StoreError(f"Task #{task_id} is DONE, so it stays in Done; Reopen it first", code=409)
            patch["stage"] = "Done"
        elif requested == "Done":
            raise StoreError("Done is a closure, not a move: use Mark Done. Moving a card is never a "
                             "review or an approval.", code=409)
        elif reopening and requested is None:
            patch["stage"] = STAGE_FROM_STATUS.get(new_status, "Next")

        changes: dict = {}
        if "title" in lifecycle_patch:
            title = lifecycle_patch["title"]
            if title != doc.get("title"):
                life.setdefault("original_title", doc.get("title"))
                changes["title"] = {"from": doc.get("title"), "to": title}
                doc["title"] = title
            if board.get("display_title"):
                # The card label follows the edited canonical title from now on.
                changes["board.display_title"] = {"from": board["display_title"], "to": None}
                patch["display_title"] = None
        if "details" in lifecycle_patch:
            scope = doc.get("scope")
            if scope is not None and not isinstance(scope, dict):
                raise StoreError(f"Task #{task_id}: its scope has an unsupported shape; details are not "
                                 "overwritten", code=409)
            scope = dict(scope or {})
            old, new = scope.get("summary"), lifecycle_patch["details"] or None
            if new != old:
                if new is None:
                    scope.pop("summary", None)
                else:
                    scope["summary"] = new
                doc["scope"] = scope
                changes["details"] = {"from": old, "to": new}
        if new_status != old_status:
            changes["status"] = {"from": old_status, "to": new_status}
            doc["status"] = new_status
        if closing:
            life["closure"] = {"kind": "manual", "label": MANUAL_CLOSURE, "at": now, "by": actor,
                               "previous_status": old_status, "note": note or None}
        if reopening:
            previous = life.get("closure")
            changes["closure"] = {"from": previous if isinstance(previous, dict) else
                                  {"kind": "recorded", "label": RECORDED_CLOSURE}, "to": None}
            life["closure"] = None
            life["reopened"] = {"at": now, "by": actor, "status": new_status}
            # A graph that is complete now has been seen; only a new completion cycle may close again.
            cycle = finished_cycle(self.graph(task_id)[0])
            if cycle is not None:
                life["suppressed_cycle"] = cycle

        board_fields = sorted(field for field, value in patch.items() if board.get(field) != value)
        board.update(patch)
        board.update(system)
        board["board_version"] = BOARD_VERSION
        board["derived"] = False
        board["revision"] = revision
        board["updated_at"] = now
        board["updated_by"] = actor
        doc["board"] = board
        if changes:
            doc["lifecycle"] = life
            doc["updated_at"] = now
        if (changes or board_fields) and isinstance(history, list):
            entry = {"at": now, "by": actor,
                     "action": "manual_close" if closing else "reopen" if reopening
                     else action if action in ("archive", "restore") else "edit",
                     "revision": revision}
            if changes:
                entry["changes"] = changes
            if board_fields:
                entry["board_fields"] = board_fields
            doc["history"] = history + [entry]
        return doc

    def create(self, fields: dict, *, actor: str, now: str) -> dict:
        """A new manual task in BACKLOG under the next never-used numeric id.

        Under the board lock; the id is above every id seen in tickets (archives
        included), graphs and checkpoints, and the file is created exclusively,
        so an existing ticket is never replaced and an id is never reused.
        """
        with _Lock(self.lock_path):
            self.tickets.mkdir(parents=True, exist_ok=True)
            task_id = max(self._used_ids(), default=0) + 1
            for _ in range(1000):
                try:
                    _create_exclusively(self.tickets / f"{task_id}.json",
                                        manual_ticket(task_id, fields, actor=actor, now=now))
                    break
                except FileExistsError:
                    task_id += 1
            else:
                raise StoreError("no free task id could be reserved", code=500)
        return self.card(task_id)

    def _used_ids(self) -> set[int]:
        used = set()
        for directory in (self.tickets, self.workflow / "graphs", self.workflow / "checkpoints"):
            if directory.is_dir():
                for path in directory.iterdir():
                    match = TASK_ID_PREFIX.match(path.name)
                    if match:
                        used.add(int(match.group(1)))
        return used

    def sync_completion(self, task_id: int, *, now: str | None = None) -> bool:
        """Close a task its graph positively completed. True when it wrote.

        The one explicit synchronisation, made on the board's read path: a
        cheap check first, then the same check again under the lock before
        writing. It only ever moves a task to DONE -- never out of it, so a
        manual closure is never undone -- and records the completion cycle,
        so a human reopen is not closed again until the graph completes anew.
        It touches no grant, gate, review or graph and starts nothing.
        """
        try:
            if not self.completion(task_id, self.read_ticket(task_id))["eligible"]:
                return False
            with _Lock(self.lock_path):
                ticket = self.read_ticket(task_id)
                check = self.completion(task_id, ticket)
                life = ticket.get("lifecycle")
                life = {} if life is None else copy.deepcopy(life)
                history = ticket.get("history")
                history = [] if history is None else history
                if not check["eligible"] or not isinstance(life, dict) or not isinstance(history, list):
                    return False
                board = _normalise_board(ticket.get("board"), ticket)
                stamp = now or utc_now()
                revision = board["revision"] + 1
                previous = ticket.get("status")
                life["closure"] = {"kind": "automatic", "label": AUTOMATIC_CLOSURE, "at": stamp, "by": SYNC_ACTOR,
                                   "previous_status": previous, "cycle": check["cycle"],
                                   "graph_revision": check["graph_revision"]}
                life["auto_done_cycle"] = check["cycle"]
                board.update(stage="Done", board_version=BOARD_VERSION, derived=False, revision=revision,
                             updated_at=stamp, updated_by=SYNC_ACTOR)
                doc = copy.deepcopy(ticket)
                doc.update(status="DONE", updated_at=stamp, lifecycle=life, board=board)
                doc["history"] = list(history) + [{
                    "at": stamp, "by": SYNC_ACTOR, "action": "auto_close", "revision": revision,
                    "changes": {"status": {"from": previous, "to": "DONE"}}, "cycle": check["cycle"]}]
                _replace_atomically(self.tickets / f"{task_id}.json", doc)
                return True
        except (StoreError, OSError):
            return False

    def sync_activity(self, task_id: object, run: dict, *, now: str | None = None) -> bool:
        """Move a task with a confirmed running recorder to In Progress. True when it wrote.

        `run` is a `run_observability.live_runs` entry; only one whose
        `active.confirmed` proof holds and that names this task counts. The
        canonical status becomes an active one (BACKLOG/READY -> by role,
        else kept) and the lane In Progress, once per run, unless the task is
        DONE, unauthorized, blocked, held in Blocked or waiting on a
        pending approval (`activity_hold`). Checked again under the lock.
        Nothing closes, grants or starts anything here.
        """
        try:
            task_id = parse_task_id(task_id)
            ticket = self.read_ticket(task_id)
            if activity_hold(ticket, _normalise_board(ticket.get("board"), ticket), run):
                return False
            with _Lock(self.lock_path):
                ticket = self.read_ticket(task_id)
                board = _normalise_board(ticket.get("board"), ticket)
                life = ticket.get("lifecycle")
                life = {} if life is None else copy.deepcopy(life)
                history = ticket.get("history")
                history = [] if history is None else history
                if (activity_hold(ticket, board, run) or not isinstance(life, dict)
                        or not isinstance(history, list)):
                    return False
                stamp = now or utc_now()
                revision = board["revision"] + 1
                old = ticket.get("status")
                new = old if old in ACTIVE_STATUSES else ACTIVITY_STATUS.get(run.get("role"), "IMPLEMENTING")
                changes = {"status": {"from": old, "to": new}} if new != old else {}
                fields = ["stage"] if board.get("stage") != "In Progress" else []
                life["activity_run"] = {"run_id": run["run_id"], "role": run.get("role"), "at": stamp}
                board.update(stage="In Progress", board_version=BOARD_VERSION, derived=False, revision=revision,
                             updated_at=stamp, updated_by=ACTIVITY_ACTOR)
                doc = copy.deepcopy(ticket)
                doc.update(status=new, updated_at=stamp, lifecycle=life, board=board)
                entry = {"at": stamp, "by": ACTIVITY_ACTOR, "action": "activity_start", "revision": revision,
                         "run_id": run["run_id"], "role": run.get("role"),
                         "basis": _dict(run.get("active")).get("basis")}
                if changes:
                    entry["changes"] = changes
                if fields:
                    entry["board_fields"] = fields
                doc["history"] = list(history) + [entry]
                _replace_atomically(self.tickets / f"{task_id}.json", doc)
                return True
        except (StoreError, OSError):
            return False

    # -- approval requests and decisions (one implementation for CLI and HTTP) --

    def _audit(self, task_id: int, ticket: dict) -> list:
        history = ticket.get("history")
        if history is None:
            return []
        if not isinstance(history, list):
            raise StoreError(f"Task #{task_id}: its history has an unsupported shape and is not overwritten",
                             code=409)
        return list(history)

    @staticmethod
    def _bumped(board: dict, revision: int, actor: str, now: str) -> dict:
        board = copy.deepcopy(board)
        board.update(board_version=BOARD_VERSION, derived=False, revision=revision, updated_at=now,
                     updated_by=actor)
        return board

    def request_approval(self, task_id: object, *, action: object, gate: object = None, summary: object,
                         evidence: object = None, content_ref: object = None, resolves_blocker: bool = False,
                         actor: str, now: str, via: str) -> dict:
        """Record (or return the identical pending) approval request for one explicit task.

        Prerequisites are checked first; a refusal writes nothing. The same
        bound request asked again is returned unchanged. A pending request for
        the same action and gate whose binding or content changed is kept,
        marked superseded, and a new one is issued.
        """
        task_id = parse_task_id(task_id)
        if action not in APPROVAL_ACTIONS:
            raise StoreError(f"action must be one of {', '.join(APPROVAL_ACTIONS)}")
        gate = default_gate(action) if gate in (None, "") else gate
        if not isinstance(gate, str) or not _GATE_TEXT.match(gate):
            raise StoreError("gate must be a plain identifier")
        summary = _clean_text(summary, "summary", limit=2000)
        if len(summary) < 3:
            raise StoreError("a request needs a completion summary (at least 3 characters)")
        evidence = _clean_evidence(evidence)
        content_ref = _clean_text(content_ref, "content_ref", limit=300) or None
        if not isinstance(resolves_blocker, bool):
            raise StoreError("resolves_blocker must be true or false")
        with _Lock(self.lock_path):
            ticket = self.read_ticket(task_id)
            requests = self._requests(task_id, ticket)
            history = self._audit(task_id, ticket)
            board = _normalise_board(ticket.get("board"), ticket)
            graph, graph_problem = self.graph(task_id)
            blocker = copy.deepcopy(ticket.get("blocker")) if resolves_blocker and _has_blocker(ticket.get("blocker")) else None
            if resolves_blocker and blocker is None:
                raise StoreError(f"Task #{task_id} has no blocker for this request to resolve", code=409)
            if resolves_blocker and action != "workflow_complete":
                raise StoreError("only a workflow_complete approval clears the gate's blocker", code=409)
            problems = approval_problems(action, gate, ticket, graph, graph_problem, resolves_blocker=blocker)
            if problems:
                raise StoreError(f"Task #{task_id}: {action} cannot be requested at gate {gate!r} yet: "
                                 + "; ".join(problems), code=409)
            graph_raw = self.graph_raw(task_id)
            content = {"summary": summary, "evidence": evidence, "content_ref": content_ref,
                       "resolves_blocker": blocker}
            same = [r for r in requests if r.get("state") == "pending" and r.get("action") == action
                    and r.get("gate_id") == gate]
            for request in same:
                if (binding_holds(request.get("binding"), ticket, board["revision"], graph_raw)
                        and all(request.get(k) == v for k, v in content.items())):
                    return {"created": False, "request": copy.deepcopy(request),
                            "offer": approval_offer(task_id, ticket.get("title") or UNKNOWN, request),
                            "card": self.card(task_id)}
            revision = board["revision"] + 1
            binding = current_binding(ticket, revision, graph_raw)
            request = {
                "id": f"{task_id}-{action}-{len(requests) + 1}", "task_id": task_id, "action": action,
                "gate_id": gate, "scope": ACTION_SCOPES[action], "binding": binding,
                "binding_token": binding_token(binding), **content, "requested_at": now, "requested_by": actor,
                "via": via, "state": "pending", "decision": None,
            }
            doc = copy.deepcopy(ticket)
            updated = copy.deepcopy(requests)
            superseded = []
            for old in updated:
                if old.get("state") == "pending" and old.get("action") == action and old.get("gate_id") == gate:
                    old["state"] = "superseded"
                    old["decision"] = {"decision": "superseded", "at": now, "by": actor, "by_request": request["id"]}
                    superseded.append(old["id"])
            doc["approval_requests"] = updated + [request]
            doc["board"] = self._bumped(board, revision, actor, now)
            entry = {"at": now, "by": actor, "action": "approval_request", "revision": revision, "via": via,
                     "request": copy.deepcopy(request)}
            if superseded:
                entry["superseded"] = superseded
            doc["history"] = history + [entry]
            _replace_atomically(self.tickets / f"{task_id}.json", doc)
        return {"created": True, "superseded": superseded, "request": request,
                "offer": approval_offer(task_id, ticket.get("title") or UNKNOWN, request), "card": self.card(task_id)}

    def decide(self, task_id: object, *, request_id: object, action: object, gate: object, binding: object,
               decision: object, actor: str, now: str, via: str, note: object = None) -> dict:
        """Approve, Request Changes or Defer one pending request, exactly as it was bound.

        Every check -- the request exists on this task, is pending, names the
        same gate and action, carries the same binding token, and the task and
        graph still match that binding; for Approve, every prerequisite of the
        gate -- runs before anything is written. Locks: the board lock, then the
        task's graph lock (the order every writer here uses).
        """
        task_id = parse_task_id(task_id)
        if decision not in DECISIONS:
            raise StoreError(f"decision must be one of {', '.join(DECISIONS)}")
        if action not in APPROVAL_ACTIONS:
            raise StoreError(f"action must be one of {', '.join(APPROVAL_ACTIONS)}")
        if not isinstance(gate, str) or not _GATE_TEXT.match(gate):
            raise StoreError("the gate the request names is required")
        if not isinstance(binding, str) or not binding:
            raise StoreError("the request's binding token is required")
        if request_id is not None and not (isinstance(request_id, str) and _GATE_TEXT.match(request_id)):
            raise StoreError("request id must be a plain identifier")
        note = _clean_text(note, "note", limit=1000) or None
        # Request Changes must say what to change, whichever path (CLI, companion) decides.
        if decision == "request_changes" and note is None:
            raise StoreError("Request Changes needs feedback: say what must change (1-1000 characters). "
                             "Nothing was decided.")
        graph_lock = contextlib.nullcontext()
        gstore = None
        if task_graph is not None and (self.workflow / "graphs" / f"{task_id}.json").is_file():
            gstore = task_graph.GraphStore(task_graph.tc.Store(self.workflow.parent, self.workflow, None),
                                           self.workflow / "graphs")
            graph_lock = gstore.locked(str(task_id))
        with _Lock(self.lock_path), graph_lock:
            ticket = self.read_ticket(task_id)
            requests = self._requests(task_id, ticket)
            history = self._audit(task_id, ticket)
            board = _normalise_board(ticket.get("board"), ticket)
            request = self._match_request(task_id, requests, request_id, action, gate)
            if request.get("binding_token") != binding:
                raise StoreError(f"binding {binding!r} does not match request {request['id']} "
                                 f"({request.get('binding_token')}); decide exactly what was offered", code=409)
            graph_raw = self.graph_raw(task_id)
            if not binding_holds(request.get("binding"), ticket, board["revision"], graph_raw):
                raise StoreError(f"request {request['id']} is stale: Task #{task_id} or its graph changed since it "
                                 "was requested. Nothing was decided; reissue the request.", code=409)
            graph, graph_problem = self.graph(task_id)
            if decision == "approve":
                problems = approval_problems(action, gate, ticket, graph, graph_problem,
                                             resolves_blocker=request.get("resolves_blocker"))
                if problems:
                    raise StoreError(f"Task #{task_id}: {action} cannot be approved: " + "; ".join(problems)
                                     + ". Nothing was written.", code=409)
            revision = board["revision"] + 1
            decided = copy.deepcopy(request)
            decided["state"] = DECIDED_STATE[decision]
            decided["decision"] = {"decision": decision, "at": now, "by": actor, "via": via, "note": note,
                                   "binding_token": binding}
            entry = {"at": now, "by": actor, "action": "approval_decision", "revision": revision, "via": via,
                     "request_id": request["id"], "approval_action": action, "gate_id": gate,
                     "decision": decision, "note": note}
            performed: list[str] = []
            new_graph = None
            if decision == "approve" and action == "task_close":
                doc = self._apply(task_id, ticket, board, {}, action="done", actor=actor, now=now,
                                  note=f"approved close request {request['id']}", system={}, revision=revision)
                performed.append("manual closure (status DONE); nothing released, committed, merged or published")
            else:
                doc = copy.deepcopy(ticket)
                doc["board"] = self._bumped(board, revision, actor, now)
                doc["history"] = history
            if decision == "approve" and action != "task_close":
                flag = GRANT_FLAGS[action]
                approvals = doc.get("approvals")
                if approvals is not None and not isinstance(approvals, dict):
                    raise StoreError(f"Task #{task_id}: its approvals have an unsupported shape", code=409)
                doc["authorization"] = {**_dict(doc.get("authorization")), flag: True}
                doc["approvals"] = {**(approvals or {}), action: {
                    "decision": "approved", "gate_id": gate, "request_id": request["id"],
                    "binding_token": binding, "at": now, "by": actor, "via": via}}
                if action in EXECUTED_ACTIONS:  # the content and work cycle its execution must later match
                    doc["approvals"][action]["scope_sha"] = execution_scope(doc)
                    doc["approvals"][action]["cycle"] = work_cycle(graph_raw)
                entry["grants"] = [f"authorization.{flag}", f"approvals.{action}"]
                performed.append(f"recorded authorization.{flag} and approvals.{action}")
                doc["updated_at"] = now
                if action == "workflow_complete":
                    if request.get("resolves_blocker") is not None:
                        entry["blocker"] = {"from": doc.get("blocker"), "to": None}
                        doc["blocker"] = None
                        performed.append("cleared the blocker this request named")
                    new_graph = self._complete_release(task_id, gstore, gate, request, doc)
                    # The active grant: the post-approval content and the graph cycle it completes.
                    doc["approvals"]["workflow_complete"]["scope"] = {
                        "ticket_sha": scope_digest(doc), "cycle": finished_cycle(new_graph)}
                    entry["graph"] = {"node": gate, "state": "completed", "revision": new_graph["revision"]}
                    performed.append(f"completed graph gate {gate} (graph revision {new_graph['revision']})")
            doc["approval_requests"] = [decided if r is request else copy.deepcopy(r) for r in requests]
            doc["history"] = list(doc.get("history") or []) + [entry]
            # The ticket first: if the graph write then failed, the release stays incomplete and
            # automatic completion fails closed.
            _replace_atomically(self.tickets / f"{task_id}.json", doc)
            if new_graph is not None:
                gstore.write(new_graph)
        closed = False
        if decision == "approve" and action == "workflow_complete":
            closed = self.sync_completion(task_id, now=now)
        card = self.card(task_id)
        return {"request": decided, "performed": performed, "closed_automatically": closed,
                "next_step": _next_step(task_id, decided, card), "card": card}

    def record_execution(self, task_id: object, *, request_id: object, action: object, binding: object,
                         outcome: object, evidence: object = None, result_ref: object = None, note: object = None,
                         actor: str, now: str, via: str) -> dict:
        """Record that the existing agent performed, and verified, one approved action -- or that it failed.

        Evidence is attached to the exact approved request: the request must
        exist on this task, be approved (not pending, superseded or declined),
        still be the current approval for its action, carry the same binding,
        and the task content must be the content it was approved for. A success
        names its result (a commit SHA for commit) and its verification
        evidence. Nothing is executed here. Afterwards the existing completion
        sync runs, so an already authorized workflow closes without another
        approval -- and only if every other condition holds.
        """
        task_id = parse_task_id(task_id)
        if action not in EXECUTED_ACTIONS:
            raise StoreError(f"execution is recorded for {', '.join(EXECUTED_ACTIONS)} only")
        if outcome not in EXECUTION_OUTCOMES:
            raise StoreError(f"outcome must be one of {', '.join(EXECUTION_OUTCOMES)}")
        if not (isinstance(request_id, str) and _GATE_TEXT.match(request_id)):
            raise StoreError("the approved request's id is required")
        if not isinstance(binding, str) or not binding:
            raise StoreError("the approved request's binding token is required")
        evidence = _clean_evidence(evidence)
        result_ref = _clean_text(result_ref, "result_ref", limit=300) or None
        note = _clean_text(note, "note", limit=1000) or None
        if outcome == "succeeded":
            if not result_ref:
                raise StoreError("a successful execution names its result (--result-ref, e.g. the commit SHA)")
            if action == "commit" and not _COMMIT_SHA.match(result_ref):
                raise StoreError("a commit's result reference is its hexadecimal commit SHA")
            if not evidence:
                raise StoreError("a successful execution needs its verification evidence (--evidence)")
        elif not (evidence or note):
            raise StoreError("a failed execution needs evidence or a note saying what failed")
        with _Lock(self.lock_path):
            ticket = self.read_ticket(task_id)
            requests = self._requests(task_id, ticket)
            history = self._audit(task_id, ticket)
            board = _normalise_board(ticket.get("board"), ticket)
            request = self._find_request(task_id, requests, request_id)
            if request.get("action") != action:
                raise StoreError(f"request {request_id} is for {request.get('action')}, not {action}", code=409)
            if request.get("binding_token") != binding:
                raise StoreError(f"binding {binding!r} does not match request {request_id} "
                                 f"({request.get('binding_token')}); record exactly what was approved", code=409)
            if request.get("state") != "approved":
                raise StoreError(f"request {request_id} is {request.get('state')}, not approved; nothing it "
                                 "covers may be executed or recorded", code=409)
            approval = _dict(ticket.get("approvals")).get(action)
            if not (isinstance(approval, dict) and approval.get("request_id") == request_id
                    and approval.get("binding_token") == binding and _positive(approval)):
                raise StoreError(f"request {request_id} is not the current {action} approval of Task #{task_id}; "
                                 "a superseded approval's execution is not recorded", code=409)
            stale = approval_bound_problem(ticket, action, approval, request, work_cycle(self.graph_raw(task_id)))
            if stale:
                raise StoreError(f"request {request_id} is stale: {stale}. Nothing was recorded.", code=409)
            previous = _dict(request.get("execution"))
            if previous.get("outcome") == "succeeded":
                raise StoreError(f"{action} for request {request_id} is already recorded as succeeded "
                                 f"({previous.get('result_ref')}); a new {action} needs a new approval", code=409)
            revision = board["revision"] + 1
            execution = {"outcome": outcome, "request_id": request_id, "action": action, "binding_token": binding,
                         "result_ref": result_ref, "evidence": evidence, "note": note,
                         "scope_sha": approval["scope_sha"], "cycle": copy.deepcopy(approval["cycle"]), "at": now, "by": actor, "via": via}
            updated = copy.deepcopy(request)
            updated["execution"] = execution
            doc = copy.deepcopy(ticket)
            doc["approval_requests"] = [updated if r is request else copy.deepcopy(r) for r in requests]
            doc["board"] = self._bumped(board, revision, actor, now)
            doc["history"] = history + [{"at": now, "by": actor, "action": "approval_execution",
                                         "revision": revision, "via": via, "request_id": request_id,
                                         "approval_action": action, "execution": copy.deepcopy(execution)}]
            _replace_atomically(self.tickets / f"{task_id}.json", doc)
        closed = self.sync_completion(task_id, now=now) if outcome == "succeeded" else False
        card = self.card(task_id)
        if outcome == "failed":
            step = (f"Recorded failed {action} for Task #{task_id}. The approval still stands for the same bound "
                    f"content (do not ask again): fix the cause, {action} again, verify and record the result.")
        elif closed:
            step = (f"Recorded verified {action} ({result_ref}) for Task #{task_id}; every remaining condition held, "
                    "so the task closed automatically.")
        elif card["status"] == "DONE":
            step = f"Recorded verified {action} ({result_ref}) for Task #{task_id}; the task was already DONE."
        else:
            step = (f"Recorded verified {action} ({result_ref}) for Task #{task_id}; it stays open: "
                    + "; ".join(card["auto_done"]["reasons"]))
        return {"request": updated, "execution": execution, "closed_automatically": closed, "next_step": step,
                "card": card}

    def _find_request(self, task_id: int, requests: list, request_id: str) -> dict:
        found = [r for r in requests if r.get("id") == request_id]
        if not found:
            owner = request_id.split("-", 1)[0]
            if owner.isdigit() and int(owner) != task_id:
                raise StoreError(f"request {request_id} belongs to Task #{owner}, not Task #{task_id}", code=409)
            raise StoreError(f"Task #{task_id} has no approval request {request_id}", code=404)
        if len(found) > 1:
            raise StoreError(f"Task #{task_id} has more than one request {request_id}; nothing decided", code=409)
        request = found[0]
        if request.get("task_id") != task_id:
            raise StoreError(f"request {request.get('id')} is bound to task {request.get('task_id')!r}, not "
                             f"Task #{task_id}", code=409)
        return request

    def _match_request(self, task_id: int, requests: list, request_id: str | None, action: str, gate: str) -> dict:
        if request_id is None:
            matches = [r for r in requests if r.get("state") == "pending" and r.get("action") == action
                       and r.get("gate_id") == gate]
            if not matches:
                raise StoreError(f"Task #{task_id} has no pending {action} request at gate {gate!r}", code=409)
            if len(matches) > 1:
                raise StoreError(f"Task #{task_id} has {len(matches)} pending {action} requests at gate {gate!r} "
                                 "(" + ", ".join(str(r.get("id")) for r in matches) + "); name one", code=409)
            request = matches[0]
        else:
            request = self._find_request(task_id, requests, request_id)
        if request.get("task_id") != task_id:
            raise StoreError(f"request {request.get('id')} is bound to task {request.get('task_id')!r}, not "
                             f"Task #{task_id}", code=409)
        if request.get("action") != action:
            raise StoreError(f"request {request['id']} is for {request.get('action')}, not {action}", code=409)
        if request.get("gate_id") != gate:
            raise StoreError(f"request {request['id']} is for gate {request.get('gate_id')!r}, not {gate!r}", code=409)
        if request.get("state") != "pending":
            raise StoreError(f"request {request['id']} is already {request.get('state')}; a decision is final",
                             code=409)
        return request

    def _complete_release(self, task_id: int, gstore, gate: str, request: dict, doc: dict) -> dict:
        """The graph with its release gate completed through the shared transitions; not yet written."""
        graph = gstore.load(str(task_id))
        evidence = list(request.get("evidence") or [])[:task_graph.tc.MAX_ITEMS - 1]
        evidence.append(f".workflow/tickets/{task_id}.json#approval_requests/{request['id']}")
        try:
            if task_graph.node(graph, gate)["state"] == "blocked":
                task_graph.apply(graph, "unblock", gate)
                graph["revision"] += 1
            task_graph.apply(graph, "start", gate, ticket=doc)
            graph["revision"] += 1
            task_graph.apply(graph, "complete", gate, ticket=doc, evidence=evidence)
            graph["revision"] += 1
            graph["updated_at"] = task_graph._now()
            task_graph.validate(graph, str(task_id))
        except task_graph.Refused as exc:
            raise StoreError(f"Task #{task_id}: the release gate refused the transition: {exc}. Nothing was "
                             "written.", code=409) from None
        return graph

    def relate(self, task_id: object, *, follow_up_of: object, actor: str, now: str, note: object = None) -> dict:
        """Record on a later task that it continues an earlier one. The earlier ticket is not touched."""
        task_id, earlier = parse_task_id(task_id), parse_task_id(follow_up_of)
        if task_id == earlier:
            raise StoreError("a task cannot be a follow-up of itself")
        note = _clean_text(note, "note", limit=1000) or None
        with _Lock(self.lock_path):
            self.read_ticket(earlier)
            ticket = self.read_ticket(task_id)
            relations = ticket.get("relations")
            if relations is not None and not isinstance(relations, list):
                raise StoreError(f"Task #{task_id}: its relations have an unsupported shape", code=409)
            relations = list(relations or [])
            if any(isinstance(r, dict) and r.get("type") == "follow_up_of" and r.get("task_id") == earlier
                   for r in relations):
                return self.card(task_id)
            history = self._audit(task_id, ticket)
            board = _normalise_board(ticket.get("board"), ticket)
            relation = {"type": "follow_up_of", "task_id": earlier, "at": now, "by": actor, "note": note}
            doc = copy.deepcopy(ticket)
            doc["relations"] = relations + [relation]
            doc["board"] = self._bumped(board, board["revision"] + 1, actor, now)
            doc["history"] = history + [{"at": now, "by": actor, "action": "relate",
                                         "revision": board["revision"] + 1, "relation": relation}]
            _replace_atomically(self.tickets / f"{task_id}.json", doc)
        return self.card(task_id)


def _next_step(task_id: int, request: dict, card: dict) -> str:
    """What happens next after a decision, in words an agent and a human can act on."""
    action, state, note = request["action"], request["state"], _dict(request.get("decision")).get("note")
    if state == "changes_requested":
        return (f"Changes requested on {action} for Task #{task_id}" + (f": {note}" if note else "")
                + f". Nothing was granted. Address them, then reissue: nlh approval request {task_id} "
                f"--action {action} --summary \"...\"")
    if state == "deferred":
        return (f"{action} for Task #{task_id} deferred" + (f": {note}" if note else "")
                + ". Nothing was granted, closed or run; reissue the request when ready.")
    if action == "task_close":
        return f"Task #{task_id} closed (manual closure). Nothing was released, committed, merged or published."
    if action == "workflow_complete":
        if card["status"] == "DONE":
            return (f"Release gate {request['gate_id']} completed and release approval recorded; Task #{task_id} "
                    "closed automatically. Nothing was committed, merged or published.")
        return (f"Release gate {request['gate_id']} completed and release approval recorded; Task #{task_id} stays "
                "open: " + "; ".join(card["auto_done"]["reasons"]))
    execution = _dict(request.get("execution"))
    if execution.get("outcome") == "succeeded":
        return (f"Approved {action} for Task #{task_id} was performed and recorded ({execution.get('result_ref')}). "
                "Nothing further is authorized by this request.")
    content = request.get("content_ref") or request.get("summary")
    only = (" It permits this one commit only; merge and publish each need their own approval."
            if action == "commit" else "")
    return (f"Authorized next action: the existing agent may now {action} exactly the bound content ({content}) "
            f"for Task #{task_id} with its ordinary tools. Do it now, verify it, then record the result: "
            f"nlh approval execute {task_id} --request {request['id']} --action {action} --binding "
            f"{request['binding_token']} --succeeded --result-ref <ref> --evidence <path> (or --failed). This "
            "approval record performed no git operation and is not evidence that it happened; the task cannot "
            "complete until a verified success is recorded." + only)
