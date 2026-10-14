#!/usr/bin/env python3
"""Manual task lifecycle on the canonical tickets.

Create without an intake allocator, edit the canonical title/details/status,
Mark Done, Reopen, the history audit, protected fields, optimistic
concurrency, the gates automatic completion needs, and no repeat closure.
Temporary workflow directories and an ephemeral loopback server only.

    python3 -B -m unittest discover -s task-board/tests -p 'test_*.py'
"""

from __future__ import annotations

import json
import subprocess
import threading
import unittest
from pathlib import Path
from unittest import mock

from test_board_observability import BoardFixture, server  # noqa: F401  (server: the module under test)
from test_companion_ui import HARNESS, JSDOM, NODE_PATH

import store  # noqa: E402
import task_checkpoint as tc  # noqa: E402
import task_graph  # noqa: E402


def graph_ticket(task_id: int, **extra) -> dict:
    """A ticket every gate of which is positively granted, plus unknown fields to preserve."""
    doc = {"schema_version": 2, "id": task_id, "title": f"Graph task {task_id}", "status": "IMPLEMENTING",
           "scope": {"summary": "fixture details"}, "acceptance_criteria": ["fixture"],
           "authorization": {"granted": True, "release_granted": True, "source": "fixture"},
           "blocker": None, "evidence_refs": ["ev/plan.md"], "x_future_field": {"kept": True}}
    doc.update(extra)
    return doc


class LifecycleFixture(BoardFixture):
    def path(self, task_id: int) -> Path:
        return self.workflow / "tickets" / f"{task_id}.json"

    def ticket(self, task_id: int) -> dict:
        return json.loads(self.path(task_id).read_text())

    def write(self, doc: dict) -> None:
        self.path(doc["id"]).write_text(json.dumps(doc))

    def call(self, method: str, path: str, body: dict | None = None):
        status, data = self.request(method, path, body)
        return status, json.loads(data)

    def board(self) -> dict:
        return {card["id"]: card for card in self.call("GET", "/api/board")[1]["cards"]}

    def graphs(self) -> task_graph.GraphStore:
        return task_graph.GraphStore(tc.Store(self.repo, self.workflow, None))

    def run_graph(self, task_id: int, *, review: str = "pass", release: bool = True,
                  finish_release: bool = True) -> Path:
        """Drive a real graph through the shared task_graph transitions."""
        gstore = self.graphs()
        gstore.create(str(task_id), correction_ceiling=0, investigators=0, release=release)
        step = lambda *a, **k: gstore.transition(str(task_id), *a, **k)  # noqa: E731
        step("start", "implement")
        step("complete", "implement", run_ids=[f"{task_id}-writer"])
        step("start", "review-1")
        step("complete", "review-1", run_ids=[f"{task_id}-review"], evidence=["ev/review.md"], outcome=review)
        if release and review == "pass" and finish_release:
            step("start", "release")
            step("complete", "release", evidence=["ev/release.md"])
        return gstore.path(str(task_id))


class ManualCreate(LifecycleFixture):
    def test_create_is_canonical_backlog_unauthorized_with_history(self):
        status, body = self.call("POST", "/api/tasks", {"title": "  Write the lifecycle docs ",
                                                        "details": "Cover create, edit, Done and reopen",
                                                        "owner": "Norman", "notes": "board-only note"})
        self.assertEqual(status, 201)
        self.assertEqual((body["allocated_id"], body["created_by"]), (5, "canonical"))
        doc = self.ticket(5)
        self.assertEqual((doc["id"], doc["title"], doc["status"]), (5, "Write the lifecycle docs", "BACKLOG"))
        self.assertEqual(doc["scope"], {"summary": "Cover create, edit, Done and reopen"})
        self.assertIs(doc["authorization"]["granted"], False)
        self.assertIsNone(doc["blocker"])
        for invented in ("acceptance_criteria", "review", "approvals", "routing"):
            self.assertNotIn(invented, doc)
        self.assertEqual(doc["lifecycle"]["origin"], "manual")
        self.assertEqual([entry["action"] for entry in doc["history"]], ["create"])
        self.assertEqual((doc["board"]["stage"], doc["board"]["revision"], doc["board"]["notes"]),
                         ("Next", 1, "board-only note"))
        card = body["card"]
        # Canonical details and board notes stay distinct.
        self.assertEqual((card["details"], card["board"]["notes"]), ("Cover create, edit, Done and reopen",
                                                                      "board-only note"))
        self.assertEqual((card["status"], card["closure"], card["board"]["owner"]), ("BACKLOG", None, "Norman"))
        self.assertFalse(card["auto_done"]["eligible"])

    def test_ids_skip_archives_graphs_and_checkpoints(self):
        (self.workflow / "tickets" / "9.closeout.json").write_text("{}")
        (self.workflow / "graphs").mkdir()
        (self.workflow / "graphs" / "12.json").write_text("{}")
        (self.workflow / "checkpoints").mkdir()
        (self.workflow / "checkpoints" / "15.json").write_text("{}")
        self.assertEqual(self.call("POST", "/api/tasks", {"title": "After the archives"})[1]["allocated_id"], 16)

    def test_exclusive_create_never_replaces_an_existing_ticket(self):
        before = {n: self.path(n).read_bytes() for n in (1, 2, 3, 4)}
        # A stale view of used ids (as if another writer raced ahead): every taken name is skipped.
        with mock.patch.object(store.Board, "_used_ids", return_value={1}):
            status, body = self.call("POST", "/api/tasks", {"title": "Raced allocation"})
        self.assertEqual((status, body["allocated_id"]), (201, 5))
        self.assertEqual({n: self.path(n).read_bytes() for n in (1, 2, 3, 4)}, before)
        self.assertEqual([p.name for p in self.path(1).parent.glob(".*.tmp")], [])

    def test_concurrent_creates_get_unique_ids(self):
        results = []
        threads = [threading.Thread(target=lambda i=i: results.append(
            self.call("POST", "/api/tasks", {"title": f"Parallel task {i}"}))) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        ids = sorted(body["allocated_id"] for status, body in results if status == 201)
        self.assertEqual(ids, list(range(5, 13)))
        self.assertEqual({self.ticket(n)["title"] for n in ids}, {f"Parallel task {i}" for i in range(8)})

    def test_create_refuses_protected_and_unknown_fields_before_writing(self):
        for payload, code in (({"title": "Granted task", "authorization": {"granted": True}}, 403),
                              ({"title": "Accepted task", "acceptance_criteria": ["x"]}, 403),
                              ({"title": "Ready task", "status": "READY"}, 400),
                              ({"title": "ab"}, 400)):
            with self.subTest(payload=payload):
                self.assertEqual(self.call("POST", "/api/tasks", payload)[0], code)
        self.assertEqual(sorted(p.name for p in (self.workflow / "tickets").glob("*.json")),
                         ["1.json", "2.json", "3.json", "4.json"])


class ManualEdit(LifecycleFixture):
    def setUp(self) -> None:
        super().setUp()
        self.write({**graph_ticket(6), "authorization": {"granted": False, "source": "fixture"},
                    "blocker": {"reason": "release", "human_action": "Human release approval"},
                    "board": {"stage": "In Progress", "display_title": "Intake label", "revision": 3}})

    def test_edit_title_details_status_keeps_original_history_and_unknown_fields(self):
        before = self.ticket(6)
        status, body = self.call("PATCH", "/api/tasks/6", {"title": "Better title", "details": "New details",
                                                           "status": "REVIEW", "notes": "n",
                                                           "expected_revision": 3})
        self.assertEqual(status, 200, body)
        doc = self.ticket(6)
        self.assertEqual((doc["title"], doc["scope"]["summary"], doc["status"]),
                         ("Better title", "New details", "REVIEW"))
        self.assertEqual(doc["lifecycle"]["original_title"], "Graph task 6")
        for kept in ("authorization", "blocker", "acceptance_criteria", "evidence_refs", "x_future_field", "id"):
            self.assertEqual(doc[kept], before[kept], kept)
        entry = doc["history"][-1]
        self.assertEqual(entry["action"], "edit")
        self.assertEqual(entry["changes"]["title"], {"from": "Graph task 6", "to": "Better title"})
        self.assertEqual(entry["changes"]["details"], {"from": "fixture details", "to": "New details"})
        self.assertEqual(entry["changes"]["status"], {"from": "IMPLEMENTING", "to": "REVIEW"})
        self.assertEqual(entry["changes"]["board.display_title"], {"from": "Intake label", "to": None})
        self.assertIn("notes", entry["board_fields"])
        self.assertEqual((body["card"]["title"], body["card"]["board"]["revision"]), ("Better title", 4))
        # A second rename keeps the first title as the original.
        self.call("PATCH", "/api/tasks/6", {"title": "Third title", "expected_revision": 4})
        doc = self.ticket(6)
        self.assertEqual((doc["title"], doc["lifecycle"]["original_title"]), ("Third title", "Graph task 6"))
        self.assertEqual(len(doc["history"]), 2)

    def test_refusals_change_nothing(self):
        raw = self.path(6).read_bytes()
        for payload, code in (({"blocker": None}, 403), ({"acceptance_criteria": []}, 403),
                              ({"authorization": {"granted": True}}, 403), ({"history": []}, 403),
                              ({"lifecycle": {}}, 403), ({"scope": {"summary": "x"}}, 403),
                              ({"routing": {}}, 403), ({"approvals": {}}, 403), ({"evidence_refs": []}, 403),
                              ({"title": "Valid title", "links": [{"target": "javascript:alert(1)"}]}, 400),
                              ({"title": "x"}, 400), ({"status": "SHIPPED"}, 400),
                              ({"status": "DONE", "stage": "Next"}, 409),
                              ({"stage": "Done"}, 409)):
            with self.subTest(payload=payload):
                status, body = self.call("PATCH", "/api/tasks/6", {**payload, "expected_revision": 3})
                self.assertEqual(status, code, body)
                self.assertEqual(self.path(6).read_bytes(), raw)

    def test_stale_edit_409_for_patch_done_and_reopen(self):
        self.assertEqual(self.call("PATCH", "/api/tasks/6", {"notes": "a", "expected_revision": 3})[0], 200)
        status, body = self.call("PATCH", "/api/tasks/6", {"title": "Lost update", "expected_revision": 3})
        self.assertEqual((status, body["stale"], body["stored_revision"]), (409, True, 4))
        self.assertEqual(self.call("POST", "/api/tasks/6/done", {"expected_revision": 3})[0], 409)
        self.assertEqual(self.call("POST", "/api/tasks/6/done", {"expected_revision": 4})[0], 200)
        self.assertEqual(self.call("POST", "/api/tasks/6/reopen", {"expected_revision": 4})[0], 409)
        self.assertEqual(self.call("POST", "/api/tasks/6/reopen", {})[0], 400)
        self.assertEqual(self.ticket(6)["status"], "DONE")

    def test_unsupported_shapes_refuse_canonical_edits_but_board_edits_work(self):
        self.write({**graph_ticket(7), "history": "free text log", "scope": "a string scope"})
        self.assertEqual(self.call("PATCH", "/api/tasks/7", {"details": "x", "expected_revision": 0})[0], 409)
        self.assertEqual(self.call("PATCH", "/api/tasks/7", {"title": "New title", "expected_revision": 0})[0], 409)
        self.assertEqual(self.call("PATCH", "/api/tasks/7", {"notes": "fine", "expected_revision": 0})[0], 200)
        doc = self.ticket(7)
        self.assertEqual((doc["history"], doc["scope"], doc["title"]), ("free text log", "a string scope", "Graph task 7"))


class ManualCloseAndReopen(LifecycleFixture):
    def test_mark_done_is_a_labelled_administrative_closure_leaving_gates_and_graph(self):
        self.write(graph_ticket(7))
        graph = self.run_graph(7, finish_release=False)
        # The blocking human release gate is recorded after the review passed.
        self.write(graph_ticket(7, authorization={"granted": True, "release_granted": False},
                                blocker={"required_resolution": "Human release approval"},
                                review={"status": "pending"}))
        graph_bytes, before = graph.read_bytes(), self.ticket(7)
        status, body = self.call("POST", "/api/tasks/7/done", {"expected_revision": 0, "note": "Shipped by hand"})
        self.assertEqual(status, 200, body)
        doc, card = self.ticket(7), body["card"]
        self.assertEqual((doc["status"], doc["board"]["stage"], card["board"]["stage"]), ("DONE", "Done", "Done"))
        closure = doc["lifecycle"]["closure"]
        self.assertEqual((closure["kind"], closure["previous_status"], closure["note"]),
                         ("manual", "IMPLEMENTING", "Shipped by hand"))
        self.assertIn("administrative only", closure["label"])
        self.assertIn("release", closure["label"])
        for kept in ("authorization", "blocker", "review", "acceptance_criteria", "evidence_refs"):
            self.assertEqual(doc[kept], before[kept], kept)
        self.assertEqual(graph.read_bytes(), graph_bytes)  # the graph's release stays pending
        self.assertEqual(json.loads(graph_bytes)["nodes"][-1]["state"], "pending")
        self.assertEqual(doc["history"][-1]["action"], "manual_close")
        self.assertEqual(card["board"]["gate"], "Human release approval")  # the gate still shows
        self.assertEqual(self.call("POST", "/api/tasks/7/done", {"expected_revision": 1})[0], 409)
        status, body = self.call("PATCH", "/api/tasks/7", {"stage": "Next", "expected_revision": 1})
        self.assertEqual(status, 409)
        self.assertIn("Reopen", body["error"])

    def test_reopen_is_backlog_or_ready_without_any_grant(self):
        # Fixture task 3: DONE, not authorized -> BACKLOG.
        before = self.ticket(3)
        status, body = self.call("POST", "/api/tasks/3/reopen", {"expected_revision": 0})
        self.assertEqual((status, body["card"]["status"], body["card"]["board"]["stage"]), (200, "BACKLOG", "Next"))
        doc = self.ticket(3)
        self.assertEqual(doc["authorization"], before["authorization"])
        self.assertIsNone(doc["lifecycle"]["closure"])
        self.assertEqual(doc["history"][-1]["action"], "reopen")
        self.assertEqual(doc["history"][-1]["changes"]["closure"]["from"]["kind"], "recorded")
        # Granted, unblocked, with acceptance criteria -> READY; nothing is granted on the way.
        self.write(graph_ticket(8, status="DONE"))
        self.assertEqual(self.call("POST", "/api/tasks/8/reopen", {"expected_revision": 0})[1]["card"]["status"],
                         "READY")
        self.write(graph_ticket(9, status="DONE", blocker={"reason": "waiting"}))
        self.assertEqual(self.call("POST", "/api/tasks/9/reopen", {"expected_revision": 0})[1]["card"]["status"],
                         "BACKLOG")
        self.write(graph_ticket(10, status="DONE"))
        self.assertEqual(self.call("POST", "/api/tasks/10/reopen", {"expected_revision": 0, "status": "IMPLEMENTING"})[0],
                         400)
        self.assertEqual(self.call("POST", "/api/tasks/10/reopen", {"expected_revision": 0, "status": "BACKLOG"})[1]
                         ["card"]["status"], "BACKLOG")
        self.assertEqual(self.call("POST", "/api/tasks/2/reopen", {"expected_revision": 0})[0], 409)
        self.assertEqual(self.call("POST", "/api/tasks/2/done", {"expected_revision": 0, "status": "DONE"})[0], 400)

    def test_editor_status_done_and_back_persist_as_closure_and_reopen(self):
        status, body = self.call("PATCH", "/api/tasks/2", {"status": "DONE", "stage": "Done", "expected_revision": 0})
        self.assertEqual((status, body["card"]["closure"]["kind"]), (200, "manual"))
        self.assertEqual(self.ticket(2)["history"][-1]["action"], "manual_close")
        self.assertEqual(self.call("PATCH", "/api/tasks/2", {"status": "IMPLEMENTING", "expected_revision": 1})[0], 400)
        status, body = self.call("PATCH", "/api/tasks/2", {"status": "READY", "stage": "In Progress",
                                                           "expected_revision": 1})
        self.assertEqual((status, body["card"]["status"], body["card"]["board"]["stage"]), (200, "READY", "In Progress"))
        self.assertEqual(self.ticket(2)["history"][-1]["action"], "reopen")

    def test_lane_never_contradicts_the_canonical_status(self):
        self.write({**graph_ticket(11, status="DONE"), "board": {"stage": "Next", "revision": 2}})
        self.write({**graph_ticket(12), "board": {"stage": "Done", "revision": 1}})
        cards = self.board()
        self.assertEqual((cards[11]["board"]["stage"], cards[11]["closure"]["kind"]), ("Done", "recorded"))
        self.assertEqual(cards[12]["board"]["stage"], "In Progress")

    def test_no_lifecycle_action_starts_any_process(self):
        refused = mock.Mock(side_effect=AssertionError("a lifecycle action started a process"))
        with mock.patch.object(subprocess, "run", refused), mock.patch.object(subprocess, "Popen", refused):
            created = self.call("POST", "/api/tasks", {"title": "No execution", "details": "d"})[1]["allocated_id"]
            self.assertEqual(self.call("PATCH", f"/api/tasks/{created}", {"title": "Still none", "status": "READY",
                                                                           "expected_revision": 1})[0], 200)
            self.assertEqual(self.call("POST", f"/api/tasks/{created}/done", {"expected_revision": 2})[0], 200)
            self.assertEqual(self.call("POST", f"/api/tasks/{created}/reopen", {"expected_revision": 3})[0], 200)
            self.board()
        refused.assert_not_called()


class AutomaticCompletion(LifecycleFixture):
    def test_positively_completed_graph_closes_once_with_a_record(self):
        self.write(graph_ticket(20))
        before = self.ticket(20)
        self.run_graph(20)
        card = self.board()[20]
        self.assertEqual((card["status"], card["board"]["stage"], card["closure"]["kind"]), ("DONE", "Done", "automatic"))
        doc = self.ticket(20)
        for kept in ("authorization", "blocker", "acceptance_criteria", "evidence_refs", "x_future_field"):
            self.assertEqual(doc[kept], before[kept], kept)
        self.assertEqual([entry["action"] for entry in doc["history"]], ["auto_close"])
        self.assertEqual(doc["lifecycle"]["auto_done_cycle"], doc["lifecycle"]["closure"]["cycle"])
        revision = doc["board"]["revision"]
        self.board()
        self.assertEqual((len(self.ticket(20)["history"]), self.ticket(20)["board"]["revision"]), (1, revision))

    def test_unknown_or_unapproved_gates_never_close(self):
        cases = {
            21: ("no task graph", None, {}),
            22: ("not complete", {"finish_release": False}, {}),
            23: ("no review recorded as pass", {"review": "fail"}, {}),
            24: ("execution authorization", {}, {"authorization": {"granted": "yes", "release_granted": True}}),
            25: ("release approval", {}, {"authorization": {"granted": True}}),
            26: ("required approval 'security'", {}, {"required_approvals": ["security"],
                                                      "approvals": {"security": "pending"}}),
            27: ("approvals have an unsupported shape", {}, {"approvals": ["security"]}),
            28: ("gate 'qa' is not positively passed", {}, {"gates": {"qa": None}}),
            29: ("review is not positively passed", {}, {"review": {"outcome": "unknown"}}),
        }
        for task_id, (_, graph, extra) in cases.items():
            self.write(graph_ticket(task_id))
            if graph is not None:
                self.run_graph(task_id, **graph)
            self.write(graph_ticket(task_id, **extra))
        # A blocker added after the graph finished, and a release skipped by hand.
        self.write(graph_ticket(30))
        self.run_graph(30)
        self.write(graph_ticket(30, blocker="Waiting for the operator"))
        self.write(graph_ticket(31))
        self.run_graph(31, finish_release=False)
        self.graphs().transition("31", "skip", "release", reason="not releasing")
        cases[30] = ("the ticket has a blocker", None, None)
        cases[31] = ("release is required and not completed", None, None)
        # A graph the shared validator refuses (missing keys).
        self.write(graph_ticket(32))
        (self.workflow / "graphs" / "32.json").write_text(json.dumps({"kind": "task-graph/v1", "task_id": "32",
                                                                       "nodes": [{"id": "implement"}]}))
        cases[32] = ("task graph unreadable or invalid", None, None)
        cards = self.board()
        for task_id, (reason, _, _) in cases.items():
            with self.subTest(task=task_id):
                self.assertNotEqual(cards[task_id]["status"], "DONE")
                self.assertFalse(cards[task_id]["auto_done"]["eligible"])
                self.assertTrue(any(reason in r for r in cards[task_id]["auto_done"]["reasons"]),
                                cards[task_id]["auto_done"]["reasons"])
                self.assertNotIn("history", self.ticket(task_id))

    def test_moving_a_card_is_never_review_or_approval(self):
        self.write(graph_ticket(33))
        self.run_graph(33, review="fail")
        for stage, code in (("In Progress", 200), ("Blocked", 200), ("Done", 409)):
            revision = self.board()[33]["board"]["revision"]
            self.assertEqual(self.call("PATCH", "/api/tasks/33", {"stage": stage, "expected_revision": revision})[0],
                             code)
        self.assertEqual(self.board()[33]["status"], "IMPLEMENTING")

    def test_manual_closure_is_never_reopened_by_sync(self):
        self.write(graph_ticket(34))
        self.run_graph(34, finish_release=False)
        self.call("POST", "/api/tasks/34/done", {"expected_revision": 0})
        gstore = self.graphs()
        gstore.transition("34", "start", "release")
        gstore.transition("34", "complete", "release", evidence=["ev/release.md"])
        for _ in range(3):
            card = self.board()[34]
        self.assertEqual((card["status"], card["closure"]["kind"]), ("DONE", "manual"))
        self.assertEqual([e["action"] for e in self.ticket(34)["history"]], ["manual_close"])

    def test_reopened_completion_waits_for_a_genuinely_new_graph_cycle(self):
        self.write(graph_ticket(35))
        graph = self.run_graph(35)
        self.assertEqual(self.board()[35]["closure"]["kind"], "automatic")
        revision = self.ticket(35)["board"]["revision"]
        status, body = self.call("POST", "/api/tasks/35/reopen", {"expected_revision": revision})
        self.assertEqual((status, body["card"]["status"]), (200, "READY"))
        # A link raises the graph revision without advancing the workflow: still not a new cycle.
        self.graphs().transition("35", "link", "release", evidence=["ev/extra.md"])
        for _ in range(2):
            card = self.board()[35]
        self.assertEqual(card["status"], "READY")
        self.assertIn("waiting for a new graph cycle", " ".join(card["auto_done"]["reasons"]))
        # A genuinely new cycle: the graph is laid out again and completes later.
        graph.unlink()
        with mock.patch.object(task_graph, "_now", return_value="2099-01-01T00:00:00+00:00"):
            self.run_graph(35)
        card = self.board()[35]
        self.assertEqual((card["status"], card["closure"]["kind"]), ("DONE", "automatic"))
        self.assertEqual([e["action"] for e in self.ticket(35)["history"]], ["auto_close", "reopen", "auto_close"])


class ReviewCorrections(LifecycleFixture):
    """Review findings: declared approvals, conflicting decisions and invalid skips fail closed."""

    def eligible(self, task_id: int, **extra) -> bool:
        self.write(graph_ticket(task_id))
        self.run_graph(task_id)
        self.write(graph_ticket(task_id, **extra))
        card = self.board()[task_id]
        self.assertEqual(card["status"] == "DONE", card["auto_done"]["eligible"] or card["closure"] is not None)
        return card["status"] == "DONE"

    def test_declared_approvals_fail_closed_unless_explicitly_optional(self):
        cases = [  # (extra ticket fields, closes automatically)
            ({"approvals": {"security": "pending"}}, False),        # exact repro
            ({"approvals": {"security": "rejected"}}, False),
            ({"approvals": {"security": None}}, False),
            ({"approvals": {"security": {"source": "ops"}}}, False),  # metadata only: no decision
            ({"approvals": {"security": "approved"}}, True),
            ({"approvals": {"docs": {"required": False, "status": "pending"}}}, True),  # explicitly optional
            ({"approvals": {"docs": {"required": False, "status": "pending"}},
              "required_approvals": ["docs"]}, False),                # named required wins
            ({"required_approvals": ["security"],                      # legacy declared shape
              "approvals": {"security": {"approved": True, "required": True, "source": "ops"}}}, True),
            ({"required_approvals": ["security"], "approvals": {}}, False),
        ]
        for offset, (extra, closes) in enumerate(cases):
            with self.subTest(extra=extra):
                self.assertIs(self.eligible(40 + offset, **extra), closes)

    def test_conflicting_decision_fields_fail_closed(self):
        cases = [
            ({"gates": {"security": {"approved": True, "decision": "rejected"}}}, False),  # exact repro
            ({"approvals": {"security": {"granted": True, "status": "pending"}}}, False),
            ({"gates": {"qa": {"passed": True, "approved": False}}}, False),
            ({"review": {"outcome": "pass", "decision": "unknown"}}, False),
            ({"gates": {"qa": {"passed": True, "outcome": "pass", "name": "qa", "source": "ci"}}}, True),
        ]
        for offset, (extra, closes) in enumerate(cases):
            with self.subTest(extra=extra):
                self.assertIs(self.eligible(60 + offset, **extra), closes)

    def persisted(self, task_id: int, states: dict, *, ceiling: int = 0, investigators: int = 0) -> list[str]:
        """Write a graph record the shared validator accepts, then read the board's reasons."""
        self.write(graph_ticket(task_id, authorization={"granted": True}))
        graph = task_graph.new_graph(str(task_id), correction_ceiling=ceiling, investigators=investigators,
                                     release=False)
        for node in graph["nodes"]:
            state, outcome = states[node["id"]]
            node.update(state=state, outcome=outcome, completed_at="2026-10-01T00:00:00+00:00")
            if node["type"] == "review" and state == "completed":
                node["evidence_refs"] = ["ev/review.md"]
        task_graph.validate(graph, str(task_id))
        (self.workflow / "graphs").mkdir(exist_ok=True)
        (self.workflow / "graphs" / f"{task_id}.json").write_text(json.dumps(graph))
        card = self.board()[task_id]
        self.assertEqual(card["status"] == "DONE", card["auto_done"]["eligible"] or card["closure"] is not None)
        return [] if card["status"] == "DONE" else card["auto_done"]["reasons"]

    def test_skipped_implementation_or_unpassed_skips_never_close(self):
        # Exact repro: no-release graph, implementation skipped, passing review.
        reasons = self.persisted(80, {"implement": ("skipped", None), "review-1": ("completed", "pass")})
        self.assertIn("implement was skipped; the implementation never completed", reasons)
        # A correction skipped before any passing review.
        reasons = self.persisted(81, {"implement": ("completed", None), "review-1": ("completed", "fail"),
                                      "correct-1": ("skipped", None), "review-2": ("completed", "pass")}, ceiling=1)
        self.assertIn("correct-1 was skipped without an earlier passing review", reasons)
        # Legitimate skips: an investigation, and the rounds a passing review made unnecessary.
        self.assertEqual(self.persisted(82, {"investigate-1": ("skipped", None), "implement": ("completed", None),
                                             "review-1": ("completed", "pass"), "correct-1": ("skipped", None),
                                             "review-2": ("skipped", None)}, ceiling=1, investigators=1), [])

    def test_skipped_release_still_needs_completion_and_approval(self):
        self.write(graph_ticket(83))
        self.run_graph(83, finish_release=False)
        self.graphs().transition("83", "skip", "release", reason="not releasing")
        self.assertNotEqual(self.board()[83]["status"], "DONE")


@unittest.skipUnless(JSDOM, "node with jsdom not available (set HARMONY_JSDOM_PATH)")
class LifecyclePage(LifecycleFixture):
    def page(self, hash_: str, responses: dict, steps: list) -> dict:
        source = {"html": self.request("GET", "/")[1].decode(), "script": self.request("GET", "/app.js")[1].decode(),
                  "hash": hash_, "responses": responses, "steps": steps}
        done = subprocess.run(["node", str(HARNESS)], input=json.dumps(source), capture_output=True, text=True,
                              timeout=60, env={**__import__("os").environ, "NODE_PATH": NODE_PATH}, check=True)
        out = json.loads(done.stdout)
        self.assertEqual(out["errors"], [])
        return out["snapshots"]

    def real(self) -> dict:
        return {"/api/observability": {"body": self.call("GET", "/api/observability")[1]},
                "/api/board": {"body": self.call("GET", "/api/board")[1]},
                "/api/quota": {"body": {"ok": True, "providers": {}}}}

    def test_cards_offer_add_task_mark_done_and_reopen(self):
        self.call("POST", "/api/tasks", {"title": "Manual page task", "details": "Canonical details"})
        responses = self.real()
        done = self.call("POST", "/api/tasks/5/done", {"expected_revision": 1})[1]
        responses["/api/tasks/5/done"] = {"body": done}
        shots = self.page("#kanban", responses, [{"snapshot": "before"},
                                                 {"click": '#lanes .card[data-id="5"] .lifecycle'},
                                                 {"snapshot": "after"}])
        before, after = shots["before"]["kanban"], shots["after"]["kanban"]
        self.assertEqual(before["addTask"], {"text": "Add task", "disabled": False})
        self.assertEqual(before["lifecycle"], {"1": "Reopen", "2": "Mark Done", "3": "Reopen", "4": "Reopen",
                                               "5": "Mark Done"})
        self.assertEqual(before["lanes"]["5"], "Next")
        self.assertEqual(shots["after"]["requests"], [{"path": "/api/tasks/5/done", "method": "POST",
                                                       "body": {"expected_revision": 1}}])
        self.assertEqual((after["lifecycle"]["5"], after["lanes"]["5"]), ("Reopen", "Done"))
        self.assertIn("Closed manually · Unverified", after["flags"]["5"])
        self.assertIn("Manual closure: administrative only", after["flagTitles"]["5"]["Closed manually · Unverified"])
        self.assertIn("marked Done: manual closure only", shots["after"]["status"]["text"])

    def test_move_to_done_is_mark_done_and_editor_sends_only_changed_canonical_fields(self):
        self.call("POST", "/api/tasks", {"title": "Editor task", "details": "Keep these details"})
        responses = self.real()
        responses["/api/tasks/5"] = {"body": self.call("PATCH", "/api/tasks/5",
                                                       {"title": "Edited title", "expected_revision": 1})[1]}
        select = '#lanes .card[data-id="2"] .move-select'
        shots = self.page("#kanban", responses, [
            {"eval": f"const s = document.querySelector('{select}'); s.value = 'Done';"
                     "s.dispatchEvent(new Event('change'))"},
            {"snapshot": "moved"},
            {"click": '#lanes .card[data-id="5"] .edit'}, {"snapshot": "editor"},
            {"eval": "const f = document.getElementById('f-status'); f.value = 'DONE';"
                     "f.dispatchEvent(new Event('change'))"}, {"snapshot": "synced"},
            {"eval": "const g = document.getElementById('f-status'); g.value = 'BACKLOG';"
                     "g.dispatchEvent(new Event('change')); document.getElementById('f-title').value = 'Edited title'"},
            {"click": "#editor-save"}, {"snapshot": "saved"}])
        # Choosing Done in Move is Mark Done (the canned answer is missing, so it fails visibly, unchanged).
        self.assertEqual(shots["moved"]["requests"][0]["path"], "/api/tasks/2/done")
        editor = shots["editor"]["editor"]
        self.assertEqual((editor["open"], editor["status"], editor["details"]), (True, "BACKLOG", "Keep these details"))
        self.assertEqual(editor["statusOptions"], ["BACKLOG", "READY", "INVESTIGATING", "IMPLEMENTING", "REVIEW",
                                                   "BLOCKED", "DONE"])
        self.assertEqual(shots["synced"]["editor"]["stage"], "Done")
        patch = shots["saved"]["requests"][-1]
        self.assertEqual((patch["path"], patch["method"]), ("/api/tasks/5", "PATCH"))
        self.assertEqual(patch["body"]["title"], "Edited title")
        self.assertNotIn("details", patch["body"])
        self.assertNotIn("status", patch["body"])
        self.assertEqual(patch["body"]["stage"], "Next")
        self.assertFalse(shots["saved"]["editor"]["open"])

    def test_inspect_without_runs_shows_the_canonical_record(self):
        self.call("POST", "/api/tasks", {"title": "Inspect me", "details": "Stored details"})
        self.call("PATCH", "/api/tasks/5", {"title": "Inspect me now", "expected_revision": 1})
        self.call("POST", "/api/tasks/5/done", {"expected_revision": 2})
        text = self.page("#inspect/5", self.real(), [{"snapshot": "s"}])["s"]["inspect"]
        for expected in ("Task #5 — Inspect me now", "No runs recorded for this task yet.", "StatusDONE",
                         "DetailsStored details", "Manual closure: administrative only", "Original titleInspect me",
                         "History", "· create — title: none → Inspect me", "· manual close — status: BACKLOG → DONE"):
            self.assertIn(expected, text)


if __name__ == "__main__":
    unittest.main()
