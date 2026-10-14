#!/usr/bin/env python3
"""One durable workflow graph per task: implement + concurrent investigations -> review -> correct -> release.

    # create the graph (fixed shape; the correction ceiling comes from the project's own gate)
    python3 direct-workflow/task_graph.py init --task-id 42 --correction-ceiling 2 \
        [--investigators 1] [--max-concurrency 2] [--no-release]

    # what is ready, running, blocked, and the next valid action
    python3 direct-workflow/task_graph.py status --task-id 42 [--json]
    python3 direct-workflow/task_graph.py next --task-id 42

    # explicit transitions (run ids and evidence are references, never copies)
    python3 direct-workflow/task_graph.py start    --task-id 42 --node implement --run-id <run-id> --model claude-opus-5-5
    python3 direct-workflow/task_graph.py complete --task-id 42 --node review-1 --outcome fail --evidence <path>
    python3 direct-workflow/task_graph.py fail     --task-id 42 --node implement --reason "..."
    python3 direct-workflow/task_graph.py block    --task-id 42 --node release --reason "..."
    python3 direct-workflow/task_graph.py unblock  --task-id 42 --node release
    python3 direct-workflow/task_graph.py skip     --task-id 42 --node investigate-1 --reason "..."
    python3 direct-workflow/task_graph.py link     --task-id 42 --node implement --run-id <id> --evidence <path>

Store. `<workflow-dir>/graphs/<task-id>.json` (`--graph-dir` / TASK_GRAPH_DIR
moves only the graphs; `--workflow-dir` / TASK_WORKFLOW_DIR as in
task_checkpoint.py). Local state, not tracked.

Rules this module keeps:

  * **Fixed shape, no rewriting.** `init` lays out every node the workflow can
    need: investigators, one implement, review-1, then correct-k/review-(k+1)
    up to the correction ceiling, then release. Nodes are never added or
    removed afterwards; unneeded ones are skipped.
  * **Shape versions.** New graphs are `shape_version` 2: implement has no
    dependencies and review-1 depends on implement and every investigator, so
    read-only investigation runs alongside the writer and its findings reach
    the first review. A graph without `shape_version` (or with 1) is the
    legacy shape: implement waits for every investigator. Each version is
    validated by its own rules and is never rewritten into the other.
  * **Stored states** pending, running, completed, failed, blocked, skipped.
    "ready" is derived: a pending node whose dependencies are all completed or
    skipped (release additionally needs a review recorded as pass).
  * **One writer.** At most one non-investigator (implement, correct, review,
    release) runs at a time. Version 2: investigators may overlap any node and
    `max_concurrency` (default 2) caps every running node, writer included.
    Version 1: non-investigators run alone and only investigators overlap,
    up to `max_concurrency` (default 1).
  * **No authority.** The graph records what happened and derives the next
    action. It never grants authorization, approves a review, or moves a
    ticket/routing gate; a ticket blocker refuses every start.
  * **Atomic, single writer.** Every change is lock -> load -> validate ->
    write temp + fsync + os.replace, with a revision counter.

Standard library only. Exit codes: 0 ok, 2 usage, refused or unreadable input.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import task_checkpoint as tc  # noqa: E402

KIND = "task-graph/v1"
SHAPE_VERSIONS = (1, 2)
SHAPE_VERSION = 2  # what `init` lays out; a stored graph without the key is version 1
DEFAULT_CONCURRENCY = {1: 1, 2: 2}
TYPES = ("investigate", "implement", "review", "correct", "release")
ROLES = {"investigate": "investigator", "implement": "writer", "review": "reviewer",
         "correct": "correction", "release": "release"}
STATES = ("pending", "running", "completed", "failed", "blocked", "skipped")
DONE = ("completed", "skipped")
REVIEW_OUTCOMES = ("pass", "fail")
WRITER_TYPES = ("implement", "correct")
NODE_KEYS = ("id", "type", "role", "state", "deps", "model", "run_ids", "evidence_refs",
             "outcome", "reason", "started_at", "completed_at")

MAX_INVESTIGATORS = 8
MAX_CONCURRENCY = 8
MAX_CEILING = 5


# One refusal type across the package: a refused transition and a refused value exit 2 alike.
Refused = tc.Refused


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# shape
# --------------------------------------------------------------------------


def _node(node_id: str, node_type: str, deps: list[str]) -> dict:
    return {"id": node_id, "type": node_type, "role": ROLES[node_type], "state": "pending",
            "deps": deps, "model": None, "run_ids": [], "evidence_refs": [], "outcome": None,
            "reason": None, "started_at": None, "completed_at": None}


def _check_shape_version(value: Any) -> int:
    if type(value) is not int or value not in SHAPE_VERSIONS:  # bool and float are refused too
        raise Refused(f"shape_version must be one of {', '.join(map(str, SHAPE_VERSIONS))}, not {value!r}")
    return value


def shape_version(graph: dict) -> int:
    """The stored layout version; a graph from before versions existed is 1."""
    return _check_shape_version(graph.get("shape_version", 1))


def new_graph(task_id: str, *, correction_ceiling: int, investigators: int = 1,
              max_concurrency: int | None = None, release: bool = True,
              shape_version: int = SHAPE_VERSION) -> dict:
    """The fixed workflow shape for one task."""
    version = _check_shape_version(shape_version)
    if max_concurrency is None:
        max_concurrency = DEFAULT_CONCURRENCY[version]
    if not 0 <= investigators <= MAX_INVESTIGATORS:
        raise Refused(f"investigators must be 0..{MAX_INVESTIGATORS}")
    if not 1 <= max_concurrency <= MAX_CONCURRENCY:
        raise Refused(f"max_concurrency must be 1..{MAX_CONCURRENCY}")
    if not 0 <= correction_ceiling <= MAX_CEILING:
        raise Refused(f"correction_ceiling must be 0..{MAX_CEILING}")
    nodes = [_node(f"investigate-{i}", "investigate", []) for i in range(1, investigators + 1)]
    found = [n["id"] for n in nodes]
    if version == 1:
        nodes.append(_node("implement", "implement", found))
        nodes.append(_node("review-1", "review", ["implement"]))
    else:  # the writer does not wait for investigation; the first review waits for its findings
        nodes.append(_node("implement", "implement", []))
        nodes.append(_node("review-1", "review", ["implement", *found]))
    for k in range(1, correction_ceiling + 1):
        nodes.append(_node(f"correct-{k}", "correct", [f"review-{k}"]))
        nodes.append(_node(f"review-{k + 1}", "review", [f"correct-{k}"]))
    if release:
        nodes.append(_node("release", "release", [n["id"] for n in nodes if n["type"] in ("review", "correct")]))
    now = _now()
    # Version 1 is exactly the legacy layout, which never carried the key.
    versioned = {"shape_version": version} if version != 1 else {}
    return {"kind": KIND, **versioned, "task_id": task_id, "revision": 1, "created_at": now, "updated_at": now,
            "config": {"max_concurrency": max_concurrency, "correction_ceiling": correction_ceiling},
            "nodes": nodes}


def validate(graph: Any, task_id: str) -> dict:
    """Refuse anything that is not a well-formed graph for this task."""
    if not isinstance(graph, dict) or graph.get("kind") != KIND or str(graph.get("task_id")) != task_id:
        raise Refused(f"not a {KIND} graph for task {task_id!r}")
    version = shape_version(graph)
    config = graph.get("config")
    if not isinstance(config, dict):
        raise Refused("config missing")
    mc, cc = config.get("max_concurrency"), config.get("correction_ceiling")
    if not (isinstance(mc, int) and 1 <= mc <= MAX_CONCURRENCY and isinstance(cc, int) and 0 <= cc <= MAX_CEILING):
        raise Refused("config: max_concurrency or correction_ceiling out of range")
    if not isinstance(graph.get("revision"), int) or not isinstance(graph.get("nodes"), list):
        raise Refused("revision or nodes missing")
    seen: set[str] = set()
    for n in graph["nodes"]:
        if not isinstance(n, dict) or set(n) != set(NODE_KEYS):
            raise Refused(f"node {n.get('id') if isinstance(n, dict) else n!r}: unexpected keys")
        if n["id"] in seen or not tc.TASK_ID_RE.match(str(n["id"])):
            raise Refused(f"node {n['id']!r}: duplicate or invalid id")
        if n["type"] not in TYPES or n["state"] not in STATES or n["role"] != ROLES[n["type"]]:
            raise Refused(f"node {n['id']}: invalid type, state or role")
        if not isinstance(n["deps"], list) or any(d not in seen for d in n["deps"]):
            raise Refused(f"node {n['id']}: dependencies must name earlier nodes")
        if n["type"] == "review" and n["state"] == "completed" and n["outcome"] not in REVIEW_OUTCOMES:
            raise Refused(f"node {n['id']}: completed review needs outcome pass or fail")
        seen.add(n["id"])
    if sum(n["type"] == "implement" for n in graph["nodes"]) != 1:
        raise Refused("exactly one implement node is required")
    running = [n for n in graph["nodes"] if n["state"] == "running"]
    if len([n for n in running if n["type"] != "investigate"]) > 1:
        raise Refused("more than one non-investigator node is running")
    if version >= 2 and len(running) > mc:
        raise Refused(f"{len(running)} nodes running exceeds max_concurrency {mc}")
    # The stored layout must be exactly the fixed shape its own config and version produce.
    expected = new_graph(task_id, correction_ceiling=cc, max_concurrency=mc, shape_version=version,
                         investigators=sum(n["type"] == "investigate" for n in graph["nodes"]),
                         release=any(n["type"] == "release" for n in graph["nodes"]))["nodes"]
    shape = [[n[k] for k in ("id", "type", "role", "deps")] for n in graph["nodes"]]
    if shape != [[n[k] for k in ("id", "type", "role", "deps")] for n in expected]:
        raise Refused("nodes, order or dependencies differ from the fixed workflow shape")
    # A node that has started (or ended) must have had its gates satisfied.
    states = {n["id"]: n for n in graph["nodes"]}
    for n in graph["nodes"]:
        if n["state"] not in ("running", "completed", "failed"):
            continue
        if any(states[d]["state"] not in DONE for d in n["deps"]):
            raise Refused(f"node {n['id']}: {n['state']} before its dependencies finished")
        if n["type"] == "correct" and states[n["deps"][0]]["outcome"] != "fail":
            raise Refused(f"node {n['id']}: a correction needs {n['deps'][0]} recorded as fail")
        if n["type"] == "release" and not review_passed(graph):
            raise Refused(f"node {n['id']}: release needs a review recorded as pass")
        if n["type"] == "review" and n["state"] == "completed" and not n["evidence_refs"]:
            raise Refused(f"node {n['id']}: a completed review needs an evidence ref")
        if version >= 2 and n["type"] == "review" and n["state"] == "completed" and not own_review_refs(graph, n):
            raise Refused(f"node {n['id']}: investigator findings are not the review's own evidence")
    return graph


# --------------------------------------------------------------------------
# derived views
# --------------------------------------------------------------------------


def node(graph: dict, node_id: str) -> dict:
    for n in graph["nodes"]:
        if n["id"] == node_id:
            return n
    raise Refused(f"no node {node_id!r} (nodes: {', '.join(n['id'] for n in graph['nodes'])})")


def investigator_findings(graph: dict) -> dict:
    """What investigators handed to review: deduplicated refs and run ids, by reference only."""
    refs: list[str] = []
    runs: list[str] = []
    states = {}
    for n in graph["nodes"]:
        if n["type"] != "investigate":
            continue
        states[n["id"]] = n["state"]
        refs += [r for r in n["evidence_refs"] if r not in refs]
        runs += [r for r in n["run_ids"] if r not in runs]
    return {"investigators": states, "evidence_refs": refs, "run_ids": runs}


def own_review_refs(graph: dict, review: dict) -> list[str]:
    """A review's evidence that is its own; refs inherited from investigators do not count."""
    inherited = set(investigator_findings(graph)["evidence_refs"])
    return [r for r in review["evidence_refs"] if r not in inherited]


def review_passed(graph: dict) -> bool:
    return any(n["type"] == "review" and n["state"] == "completed" and n["outcome"] == "pass"
               for n in graph["nodes"])


def ceiling_exhausted(graph: dict) -> dict | None:
    """The last review, if it failed with no correction left."""
    reviews = [n for n in graph["nodes"] if n["type"] == "review"]
    last = reviews[-1]
    if last["state"] == "completed" and last["outcome"] == "fail":
        return last
    return None


def ready_nodes(graph: dict) -> list[dict]:
    states = {n["id"]: n["state"] for n in graph["nodes"]}
    out = []
    for n in graph["nodes"]:
        if n["state"] != "pending" or any(states[d] not in DONE for d in n["deps"]):
            continue
        if n["type"] == "release" and not review_passed(graph):
            continue
        out.append(n)
    return out


def concurrency_refusal(graph: dict, n: dict) -> str | None:
    """Why `n` may not start now given what is running, or None."""
    running = [r for r in graph["nodes"] if r["state"] == "running" and r["id"] != n["id"]]
    if shape_version(graph) >= 2:
        cap = graph["config"]["max_concurrency"]
        if len(running) >= cap:
            return f"max_concurrency {cap} reached; running: {', '.join(r['id'] for r in running)}"
        others = [r["id"] for r in running if r["type"] != "investigate"]
        if n["type"] != "investigate" and others:
            return f"{n['id']} ({n['role']}): one non-investigator at a time; running: {', '.join(others)}"
        return None
    if n["type"] != "investigate":
        if running:
            return f"{n['id']} ({n['role']}) runs alone; running: {', '.join(r['id'] for r in running)}"
        return None
    others = [r["id"] for r in running if r["type"] != "investigate"]
    if others:
        return f"investigators may not overlap {', '.join(others)}"
    if len(running) >= graph["config"]["max_concurrency"]:
        return f"max_concurrency {graph['config']['max_concurrency']} reached"
    return None


def startable(graph: dict) -> list[dict]:
    """Ready nodes that can start together now, within the concurrency rule.

    Version 2 puts a ready non-investigator (the writer first of all) ahead of
    investigators, so free room never goes to investigation while the writer
    waits; version 1 keeps graph order.
    """
    running = [n for n in graph["nodes"] if n["state"] == "running"]
    ready = ready_nodes(graph)
    if not ready:
        return []
    if shape_version(graph) >= 2:
        room = graph["config"]["max_concurrency"] - len(running)
        picks = []
        if not any(r["type"] != "investigate" for r in running):
            picks += [n for n in ready if n["type"] != "investigate"][:1]
        picks += [n for n in ready if n["type"] == "investigate"]
        return picks[:max(room, 0)]
    first = ready[0]
    if first["type"] != "investigate":
        return [] if running else [first]
    if any(r["type"] != "investigate" for r in running):
        return []
    room = graph["config"]["max_concurrency"] - len(running)
    return [n for n in ready if n["type"] == "investigate"][:max(room, 0)]


def next_action(graph: dict, ticket: dict | None = None) -> str:
    """One deterministic next step for a fresh session."""
    if ticket and ticket.get("blocker"):
        return f"Ticket blocker wins: {ticket['blocker']}; resolve it through the ticket's own gate before any node starts"
    failed = [n for n in graph["nodes"] if n["state"] == "failed"]
    running = [n for n in graph["nodes"] if n["state"] == "running"]
    starts = startable(graph)
    exhausted = ceiling_exhausted(graph)
    if exhausted:
        return (f"Correction ceiling ({graph['config']['correction_ceiling']}) reached: {exhausted['id']} failed. "
                "Escalate to the operator; the graph cannot add correction rounds")
    if failed:
        return f"Inspect failed {failed[0]['id']} ({failed[0]['reason']}); restart it with `start` or `block` it"
    if shape_version(graph) >= 2 and (starts or running):
        return _next_action_v2(graph, starts, running)
    if starts:
        ids = ", ".join(n["id"] for n in starts)
        first = starts[0]
        if first["type"] == "investigate":
            return f"Start read-only investigator(s) {ids} (at most {graph['config']['max_concurrency']} at once)"
        if first["type"] == "review":
            return f"Start {ids}: independent reviewer in a fresh context, not the writer's session"
        if first["type"] == "release":
            return ("Start release only after the project's ticket/routing release gates pass; "
                    "the graph grants no authorization")
        return f"Start {ids} ({first['role']}) as the only running writer"
    if running:
        return f"Wait for {', '.join(n['id'] for n in running)}; record the result with `complete` or `fail`"
    blocked = [n for n in graph["nodes"] if n["state"] == "blocked"]
    if blocked:
        return f"Resolve block on {blocked[0]['id']}: {blocked[0]['reason']}; then `unblock`"
    rel = [n for n in graph["nodes"] if n["type"] == "release"]
    if rel and rel[0]["state"] == "completed":
        return "Graph complete: release recorded; nothing further"
    if all(n["state"] in DONE for n in graph["nodes"]):
        return "Graph complete: every node completed or skipped; nothing further"
    return "No node can start; inspect the graph"


def _review_waits_on_investigators(graph: dict) -> str | None:
    """Version 2: the first review is held for investigator findings the writer did not wait for."""
    review = node(graph, "review-1")
    waiting = [n for n in graph["nodes"] if n["id"] in review["deps"] and n["type"] == "investigate"
               and n["state"] not in DONE]
    if review["state"] != "pending" or not waiting or node(graph, "implement")["state"] not in ("running", "completed"):
        return None
    return ("review-1 waits for investigator findings from "
            + ", ".join(f"{n['id']} ({n['state']})" for n in waiting)
            + "; complete them, or `skip` one that is not needed")


def _next_action_v2(graph: dict, starts: list[dict], running: list[dict]) -> str:
    """Version 2 next step: what may start now, the free room, and what the first review waits for."""
    room = f"{len(running)} of max_concurrency {graph['config']['max_concurrency']} running"
    parts = []
    writer = next((n for n in starts if n["type"] != "investigate"), None)
    investigators = ", ".join(n["id"] for n in starts if n["type"] == "investigate")
    if writer is not None:
        if writer["type"] == "review":
            parts.append(f"Start {writer['id']}: independent reviewer in a fresh context, not the writer's session")
        elif writer["type"] == "release":
            parts.append("Start release only after the project's ticket/routing release gates pass; "
                         "the graph grants no authorization")
        else:
            parts.append(f"Start {writer['id']} ({writer['role']}) as the only running writer")
    if investigators:
        alongside = ([writer["id"]] if writer is not None else []) + [n["id"] for n in running]
        parts.append(("start" if parts else "Start") + f" read-only investigator(s) {investigators}"
                     + (f" alongside {', '.join(alongside)}" if alongside else ""))
    held = [n["id"] for n in ready_nodes(graph) if n not in starts]
    if parts:
        text = "; ".join(parts)
    else:
        text = f"Wait for {', '.join(n['id'] for n in running)}; record the result with `complete` or `fail`"
    if investigators or running or held:  # a lone writer start on an idle graph needs no room note
        text += (f" ({room}"
                 + (f"; ready but held by the cap or the one-writer rule: {', '.join(held)}" if held else "")
                 + ("; investigators may overlap any node, one non-investigator at a time" if investigators else "")
                 + ")")
    wait = _review_waits_on_investigators(graph)
    return text + (f". R{wait[1:]}" if wait else "")


def review_handoff(graph: dict) -> dict:
    """What the next review inherits, kept apart from the review's own evidence."""
    found = investigator_findings(graph)
    return {"investigators": found["investigators"],
            "investigator_evidence_refs": found["evidence_refs"],
            "investigator_run_ids": found["run_ids"],
            "note": "investigator findings for the reviewer; not proof of a review, which links its own evidence"}


def summary(graph: dict, path: Path | None = None, ticket: dict | None = None) -> dict:
    """The compact view a checkpoint carries: position, ready/blocked, next action.

    A version 2 graph adds its version and the review handoff; a version 1
    summary keeps its old keys exactly.
    """
    done = [n for n in graph["nodes"] if n["state"] == "completed"]
    last = done[-1] if done else None
    versioned = ({"shape_version": shape_version(graph), "review_handoff": review_handoff(graph)}
                 if shape_version(graph) >= 2 else {})
    return {
        "path": str(path) if path else None,
        "revision": graph["revision"],
        "updated_at": graph["updated_at"],
        "last_completed": (f"{last['id']}" + (f" ({last['outcome']})" if last["outcome"] else "")) if last else None,
        "running": [n["id"] for n in graph["nodes"] if n["state"] == "running"],
        "ready": [n["id"] for n in ready_nodes(graph)],
        "blocked": [f"{n['id']}: {n['reason']}" for n in graph["nodes"] if n["state"] == "blocked"],
        "failed": [f"{n['id']}: {n['reason']}" for n in graph["nodes"] if n["state"] == "failed"],
        "next_action": next_action(graph, ticket),
        **versioned,
    }


# --------------------------------------------------------------------------
# transitions
# --------------------------------------------------------------------------


def _add_refs(graph: dict, n: dict, run_ids: list[str], evidence: list[str]) -> None:
    for rid in run_ids:
        if not tc.TASK_ID_RE.match(rid):
            raise Refused(f"run id {rid!r} is not a valid run id")
        # A reviewer must not share a run (and so a session) with the writer it reviews.
        side = "review" if n["type"] == "review" else "writer" if n["type"] in WRITER_TYPES else None
        for other in graph["nodes"]:
            if rid in other["run_ids"] and side and (
                    (side == "review" and other["type"] in WRITER_TYPES)
                    or (side == "writer" and other["type"] == "review")):
                raise Refused(f"run {rid} is already linked to {other['id']}; reviewer and writer runs must differ")
        if rid not in n["run_ids"]:
            n["run_ids"].append(rid)
    for ref in evidence:
        tc._check_string("evidence", ref)
        # A finding linked after the fact must not turn a recorded review's own evidence into inherited evidence.
        if n["type"] == "investigate" and shape_version(graph) >= 2 and any(
                ref in r["evidence_refs"] for r in graph["nodes"] if r["type"] == "review"):
            raise Refused(f"{ref} is already a review's evidence; it cannot also be an investigator finding")
        if ref not in n["evidence_refs"]:
            n["evidence_refs"].append(ref)
    if len(n["run_ids"]) > tc.MAX_ITEMS or len(n["evidence_refs"]) > tc.MAX_ITEMS:
        raise Refused(f"{n['id']}: more than {tc.MAX_ITEMS} links; reference an evidence index instead")


def apply(graph: dict, action: str, node_id: str, *, ticket: dict | None = None, run_ids: list[str] = (),
          evidence: list[str] = (), model: str | None = None, outcome: str | None = None,
          reason: str | None = None) -> dict:
    """Apply one validated transition in place and return the node."""
    n = node(graph, node_id)
    state = n["state"]
    now = _now()
    if reason is not None:
        tc._check_string("reason", reason)
    if outcome is not None:
        tc._check_string("outcome", outcome)
    if model is not None:
        tc._check_string("model", model)

    if action == "start":
        if ticket and ticket.get("blocker"):
            raise Refused(f"ticket blocker wins: {ticket['blocker']}")
        if state == "pending" and n not in ready_nodes(graph):
            waiting = [d for d in n["deps"] if node(graph, d)["state"] not in DONE]
            why = f"waiting on {', '.join(waiting)}" if waiting else "no review recorded as pass"
            raise Refused(f"{node_id} is not ready ({why})")
        if state not in ("pending", "failed"):
            raise Refused(f"{node_id}: cannot start from {state}")
        refusal = concurrency_refusal(graph, n)
        if refusal:
            raise Refused(refusal)
        n.update(state="running", started_at=now, completed_at=None, reason=None, outcome=None)
        if model is not None:
            n["model"] = model
    elif action == "complete":
        if state != "running":
            raise Refused(f"{node_id}: only a running node can complete (is {state})")
        _add_refs(graph, n, run_ids, evidence)
        if n["type"] == "review" and outcome not in REVIEW_OUTCOMES:
            raise Refused("a review completes with --outcome pass or fail, as the reviewer's evidence states")
        if n["type"] == "review" and not n["evidence_refs"]:
            raise Refused(f"{node_id}: link the reviewer's evidence (--evidence) before recording a verdict")
        if n["type"] == "review" and shape_version(graph) >= 2 and not own_review_refs(graph, n):
            raise Refused(f"{node_id}: investigator findings are not proof of a review; link the reviewer's "
                          "own evidence (--evidence) before recording a verdict")
        if n["type"] != "investigate" and not (n["run_ids"] or n["evidence_refs"]):
            raise Refused(f"{node_id}: link a run id or evidence ref before completing")
        n.update(state="completed", outcome=outcome, completed_at=now)
        if n["type"] == "review":
            _after_review(graph, n)
        return n
    elif action == "fail":
        if state != "running" or not reason:
            raise Refused(f"{node_id}: only a running node can fail, with --reason")
        n.update(state="failed", reason=reason, completed_at=now)
    elif action == "block":
        if state not in ("pending", "running", "failed") or not reason:
            raise Refused(f"{node_id}: block needs a pending/running/failed node and --reason")
        n.update(state="blocked", reason=reason)
    elif action == "unblock":
        if state != "blocked":
            raise Refused(f"{node_id}: not blocked")
        n.update(state="pending", reason=None, started_at=None)
    elif action == "skip":
        if n["type"] not in ("investigate", "release"):
            raise Refused(f"{node_id}: only investigate and release are skipped by hand; "
                          "corrections and re-reviews are skipped when a review passes")
        if state not in ("pending", "blocked") or not reason:
            raise Refused(f"{node_id}: skip needs a pending/blocked node and --reason")
        n.update(state="skipped", reason=reason, completed_at=now)
    elif action == "link":
        if not (run_ids or evidence):
            raise Refused("link needs --run-id or --evidence")
    else:
        raise Refused(f"unknown action {action!r}")
    _add_refs(graph, n, run_ids, evidence)
    return n


def _after_review(graph: dict, review: dict) -> None:
    """A pass skips the rounds it made unnecessary; a final fail blocks release."""
    later = graph["nodes"][graph["nodes"].index(review) + 1:]
    if review["outcome"] == "pass":
        for n in later:
            if n["type"] in ("correct", "review") and n["state"] in ("pending", "blocked"):
                n.update(state="skipped", reason=f"not needed: {review['id']} passed", completed_at=_now())
    elif ceiling_exhausted(graph) is review:
        for n in later:
            if n["type"] == "release" and n["state"] == "pending":
                n.update(state="blocked", reason=(
                    f"correction ceiling {graph['config']['correction_ceiling']} reached: {review['id']} failed; "
                    "escalate to the operator"))


# --------------------------------------------------------------------------
# store
# --------------------------------------------------------------------------


class GraphStore:
    def __init__(self, cstore: tc.Store, graph_dir: Path | None = None):
        self.cstore = cstore
        self.graph_dir = (graph_dir or cstore.workflow_dir / "graphs").expanduser().resolve()

    def path(self, task_id: str) -> Path:
        return self.graph_dir / f"{task_id}.json"

    def load(self, task_id: str) -> dict | None:
        path = self.path(task_id)
        if not path.is_file():
            return None
        return validate(tc._read_json(path), task_id)

    def write(self, graph: dict) -> None:
        path = self.path(graph["task_id"])
        text = json.dumps(graph, indent=2, ensure_ascii=False) + "\n"
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

    @contextlib.contextmanager
    def locked(self, task_id: str):
        """Exclusive lock for one load-modify-write; concurrent writers queue."""
        self.graph_dir.mkdir(parents=True, exist_ok=True)
        with open(self.graph_dir / f".{task_id}.lock", "w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def create(self, task_id: str, **kwargs: Any) -> dict:
        with self.locked(task_id):
            if self.path(task_id).exists():
                raise Refused(f"{self.path(task_id)} exists; a graph is never re-laid out")
            graph = new_graph(task_id, **kwargs)
            self.write(graph)
            return graph

    def transition(self, task_id: str, action: str, node_id: str, **kwargs: Any) -> dict:
        ticket = self.cstore.load_ticket(task_id)
        with self.locked(task_id):
            graph = self.load(task_id)
            if graph is None:
                raise Refused(f"no graph at {self.path(task_id)}; run `init` first")
            apply(graph, action, node_id, ticket=ticket, **kwargs)
            graph["revision"] += 1
            graph["updated_at"] = _now()
            validate(graph, task_id)
            self.write(graph)
            return graph

    def summary(self, task_id: str) -> dict | None:
        graph = self.load(task_id)
        if graph is None:
            return None
        return summary(graph, self.path(task_id), self.cstore.load_ticket(task_id))


def run_link_status(records_dir: Path, run_id: str) -> str:
    """recorded / reserved / launch_failed / missing for a recorder run id; contents are not copied."""
    path = records_dir / f"{run_id}.json"
    if not path.is_file():
        return "missing"
    try:
        status = json.loads(path.read_text(encoding="utf-8")).get("status")
    except (OSError, ValueError, AttributeError):
        return "unreadable"
    return status or "recorded"


def render_status(graph: dict, s: dict, gstore: GraphStore, records_dir: Path) -> str:
    cfg = graph["config"]
    lines = [f"# Task {graph['task_id']} graph (rev {graph['revision']}, {graph['updated_at']})",
             f"- file: {s['path']}"]
    if shape_version(graph) >= 2:
        handoff = s["review_handoff"]
        lines += [f"- shape v2: max concurrency {cfg['max_concurrency']} (every running node; one "
                  f"non-investigator at a time); correction ceiling {cfg['correction_ceiling']} "
                  "(must match the project's gate)",
                  "- implement does not wait for investigators; review-1 waits for every investigator",
                  "- review handoff (investigator findings, not the review's own evidence): "
                  + (", ".join(handoff["investigator_evidence_refs"]) or "none yet")
                  + (f"; runs {', '.join(handoff['investigator_run_ids'])}" if handoff["investigator_run_ids"] else "")]
    else:
        lines.append(f"- max concurrency {cfg['max_concurrency']} (investigators only); correction ceiling "
                     f"{cfg['correction_ceiling']} (must match the project's gate)")
    lines += [f"- NEXT: {s['next_action']}", ""]
    ready = set(s["ready"])
    for n in graph["nodes"]:
        state = "ready" if n["id"] in ready else n["state"]
        extra = [f"deps {','.join(n['deps'])}" if n["deps"] else None,
                 f"model {n['model']}" if n["model"] else None,
                 f"outcome {n['outcome']}" if n["outcome"] else None,
                 f"reason: {n['reason']}" if n["reason"] else None]
        lines.append(f"- {n['id']} [{state}] {n['role']}" + "".join(f"; {e}" for e in extra if e))
        for rid in n["run_ids"]:
            lines.append(f"    run {rid} [{run_link_status(records_dir, rid)}]")
        for ref in n["evidence_refs"]:
            e = tc.evidence_status(gstore.cstore, ref)
            lines.append(f"    evidence [{e['status']}] {ref}")
    lines.append("\nThe graph records workflow position only; ticket/routing gates stay authoritative.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="task_graph.py", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    def common(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
        p.add_argument("--task-id", required=True)
        p.add_argument("--project-root", type=Path, default=Path.cwd())
        p.add_argument("--workflow-dir", type=Path, default=os.environ.get("TASK_WORKFLOW_DIR") or None)
        p.add_argument("--graph-dir", type=Path, default=os.environ.get("TASK_GRAPH_DIR") or None)
        return p

    init = common(sub.add_parser("init", help="lay out the fixed graph for a task"))
    init.add_argument("--correction-ceiling", type=int, required=True,
                      help="correction rounds allowed; take it from the project's gate, never exceed it")
    init.add_argument("--investigators", type=int, default=1)
    init.add_argument("--max-concurrency", type=int, default=None,
                      help=f"running nodes at once, writer included (default {DEFAULT_CONCURRENCY[SHAPE_VERSION]})")
    init.add_argument("--no-release", action="store_true")
    for name in ("show", "next"):
        common(sub.add_parser(name))
    st = common(sub.add_parser("status", help="nodes, links, ready set and next action"))
    st.add_argument("--json", action="store_true")
    st.add_argument("--records-dir", type=Path, default=os.environ.get("DIRECT_RUN_RECORDS_DIR") or None)
    for name in ("start", "complete", "fail", "block", "unblock", "skip", "link"):
        p = common(sub.add_parser(name))
        p.add_argument("--node", required=True)
        p.add_argument("--run-id", action="append", default=[])
        p.add_argument("--evidence", action="append", default=[])
        p.add_argument("--model")
        p.add_argument("--outcome")
        p.add_argument("--reason")
    args = parser.parse_args(argv)

    if not tc.TASK_ID_RE.match(args.task_id):
        print(f"task_graph: invalid task id {args.task_id!r}", file=sys.stderr)
        return 2
    gstore = GraphStore(tc.Store(args.project_root, args.workflow_dir, None), args.graph_dir)
    try:
        if args.cmd == "init":
            g = gstore.create(args.task_id, correction_ceiling=args.correction_ceiling,
                              investigators=args.investigators, max_concurrency=args.max_concurrency,
                              release=not args.no_release)
            print(f"wrote {gstore.path(args.task_id)} ({len(g['nodes'])} nodes)")
            return 0
        graph = gstore.load(args.task_id)
        if graph is None:
            raise Refused(f"no graph at {gstore.path(args.task_id)}; run `init` first")
        if args.cmd == "show":
            print(json.dumps(graph, indent=2, ensure_ascii=False))
        elif args.cmd in ("next", "status"):
            s = gstore.summary(args.task_id)
            if args.cmd == "next":
                print(s["next_action"])
            elif args.json:
                print(json.dumps(s, indent=2, ensure_ascii=False))
            else:
                rdir = Path(args.records_dir or gstore.cstore.project_root / ".direct-runs")
                if not rdir.is_absolute():
                    rdir = gstore.cstore.project_root / rdir
                print(render_status(graph, s, gstore, rdir))
        else:
            g = gstore.transition(args.task_id, args.cmd, args.node, run_ids=args.run_id, evidence=args.evidence,
                                  model=args.model, outcome=args.outcome, reason=args.reason)
            n = node(g, args.node)
            print(f"{n['id']}: {n['state']} (rev {g['revision']}); next: {next_action(g, gstore.cstore.load_ticket(args.task_id))}")
    except (Refused, OSError, ValueError) as exc:
        print(f"task_graph: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
