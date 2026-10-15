#!/usr/bin/env python3
"""Task 8 phase 3: archive/restore, activity promotion and completion display.

Archive is visibility only (ids, paths, history, graphs, approvals and Inspect
kept; all-or-none bulk; stale revisions refused); a confirmed running recorder
moves an open, authorized, unheld task to In Progress and never touches a DONE
one; a DONE card says whether the workflow records still verify it.
Temporary workflow directories, fake run data and an ephemeral loopback server only.

    python3 -B -m unittest discover -s task-board/tests -p 'test_*.py'
"""

from __future__ import annotations

import json
import unittest
from unittest import mock

import test_task_lifecycle as tl
from test_approval_bridge import ApprovalFixture
from test_task_lifecycle import JSDOM, graph_ticket

import store  # noqa: E402
from test_board_observability import server  # noqa: E402


def confirmed(task_id, run_id="run-1", role="writer", *, ok=True, state="running") -> dict:
    """A live_runs entry as run_observability reports it; `ok` is the recorder proof."""
    return {"run_id": run_id, "task_id": str(task_id), "role": role, "role_label": role, "provider": "claude",
            "state": state, "started_at": "2026-10-13T00:00:00Z", "elapsed_ms": 1000,
            "active": {"confirmed": ok, "basis": "fixture proof"}}


class ArchiveFixture(ApprovalFixture):
    def revision(self, task_id: int) -> int:
        return self.board()[task_id]["board"]["revision"]

    def snapshot(self) -> dict:
        return {p.name: p.read_bytes() for p in sorted((self.workflow / "tickets").glob("*.json"))}

    def auto_closed(self, task_id: int) -> None:
        self.write(graph_ticket(task_id))
        self.run_graph(task_id)
        self.assertEqual(self.board()[task_id]["closure"]["kind"], "automatic")


class Archive(ArchiveFixture):
    def test_archive_and_restore_keep_id_path_history_graph_approvals_and_inspect(self):
        self.auto_closed(20)
        graph = (self.workflow / "graphs" / "20.json").read_bytes()
        before = self.ticket(20)
        status, body = self.call("POST", "/api/tasks/20/archive", {"expected_revision": self.revision(20)})
        self.assertEqual(status, 200, body)
        doc = self.ticket(20)
        self.assertEqual((doc["id"], doc["status"], doc["board"]["archived"]), (20, "DONE", True))
        for key, value in before.items():
            if key not in ("board", "history"):
                self.assertEqual(doc[key], value, key)
        self.assertEqual(doc["history"][:-1], before["history"])
        self.assertEqual((doc["history"][-1]["action"], doc["history"][-1]["board_fields"]), ("archive", ["archived"]))
        self.assertEqual((self.workflow / "graphs" / "20.json").read_bytes(), graph)
        payload = self.call("GET", "/api/board")[1]
        card = {c["id"]: c for c in payload["cards"]}[20]  # still served, so Inspect still finds it
        self.assertTrue(card["board"]["archived"])
        self.assertEqual((card["completion"]["label"], card["history"][-1]["action"]), ("Verified complete", "archive"))
        self.assertEqual((payload["archived"], payload["counts"]["Done"]), (1, 3))
        status, body = self.call("POST", "/api/tasks/20/restore", {"expected_revision": card["board"]["revision"]})
        self.assertEqual((status, body["card"]["board"]["archived"]), (200, False))
        self.assertEqual([e["action"] for e in self.ticket(20)["history"]], ["auto_close", "archive", "restore"])
        self.assertEqual(self.board()[20]["status"], "DONE")
        self.assertEqual(sorted(p.name for p in (self.workflow / "tickets").glob("*.json")),
                         ["1.json", "2.json", "20.json", "3.json", "4.json"])  # nothing renumbered or added

    def test_refusals_write_nothing(self):
        self.call("POST", "/api/tasks/1/archive", {"expected_revision": 0})
        raw = self.snapshot()
        cases = [("POST", "/api/tasks/2/archive", {"expected_revision": 0}, 409),   # not DONE
                 ("POST", "/api/tasks/1/archive", {"expected_revision": 1}, 409),   # already archived
                 ("POST", "/api/tasks/3/restore", {"expected_revision": 0}, 409),   # not archived
                 ("POST", "/api/tasks/3/archive", {"expected_revision": 7}, 409),   # stale
                 ("POST", "/api/tasks/3/archive", {}, 400),                         # no revision
                 ("POST", "/api/tasks/3/archive", {"expected_revision": 0, "status": "BACKLOG"}, 400),
                 ("PATCH", "/api/tasks/3", {"archived": True, "expected_revision": 0}, 400),
                 ("POST", "/api/tasks/99/archive", {"expected_revision": 0}, 404)]
        for method, path, body, code in cases:
            with self.subTest(path=path, body=body):
                status, answer = self.call(method, path, body)
                self.assertEqual(status, code, answer)
                self.assertEqual(self.snapshot(), raw)
        self.assertTrue(self.call("POST", "/api/tasks/3/archive", {"expected_revision": 7})[1]["stale"])

    def test_bulk_archive_is_all_or_none(self):
        raw = self.snapshot()
        good = [{"id": 1, "expected_revision": 0}, {"id": 3, "expected_revision": 0}, {"id": 4, "expected_revision": 0}]
        for tasks, code in (([*good[:2], {"id": 4, "expected_revision": 5}], 409),  # one stale revision
                            ([*good, {"id": 2, "expected_revision": 0}], 409),       # one open task
                            ([*good, {"id": 1, "expected_revision": 0}], 400),       # named twice
                            ([*good, {"id": 77, "expected_revision": 0}], 404),
                            ([], 400), ([{"id": 1}], 400)):
            with self.subTest(tasks=tasks):
                self.assertEqual(self.call("POST", "/api/tasks/archive", {"tasks": tasks})[0], code)
                self.assertEqual(self.snapshot(), raw)
        self.assertEqual(self.call("POST", "/api/tasks/archive", {"tasks": good, "x": 1})[0], 400)
        status, body = self.call("POST", "/api/tasks/archive", {"tasks": good})
        self.assertEqual((status, sorted(c["id"] for c in body["cards"])), (200, [1, 3, 4]))
        for task_id in (1, 3, 4):
            doc = self.ticket(task_id)
            self.assertEqual((doc["id"], doc["status"], doc["board"]["archived"], doc["history"][-1]["action"]),
                             (task_id, "DONE", True, "archive"))

    def test_bulk_write_failure_puts_back_what_was_written(self):
        before = {n: self.ticket(n) for n in (1, 3)}
        real, calls = store._replace_atomically, []

        def flaky(path, document):
            calls.append(path.name)
            if len(calls) == 2:
                raise OSError("disk full")
            real(path, document)

        with mock.patch.object(store, "_replace_atomically", flaky):
            with self.assertRaises(OSError):
                store.Board(self.workflow).archive_many([{"id": 1, "expected_revision": 0},
                                                         {"id": 3, "expected_revision": 0}], actor="t", now="n")
        self.assertEqual({n: self.ticket(n) for n in (1, 3)}, before)

    def test_reads_never_archive_and_status_changes_keep_it_archived_until_restore(self):
        self.auto_closed(20)
        for _ in range(3):
            self.assertFalse(self.board()[20]["board"]["archived"])
        self.call("POST", "/api/tasks/3/archive", {"expected_revision": 0})
        self.write({**self.ticket(3), "status": "READY"})  # changed outside the board
        for _ in range(2):
            self.assertTrue(self.board()[3]["board"]["archived"])
        self.assertTrue(self.ticket(3)["board"]["archived"])

    def test_archive_never_freezes_reopen_edit_or_restore(self):
        self.call("POST", "/api/tasks/1/archive", {"expected_revision": 0})
        status, body = self.call("POST", "/api/tasks/1/reopen", {"expected_revision": 1})
        self.assertEqual(status, 200, body)
        doc = self.ticket(1)
        self.assertNotEqual(doc["status"], "DONE")
        self.assertTrue(doc["board"]["archived"])  # reopening is not a Restore
        status, body = self.call("PATCH", "/api/tasks/1", {"notes": "still hidden", "expected_revision": 2})
        self.assertEqual((status, body["card"]["board"]["archived"]), (200, True), body)
        # Restore works on an archived card that is no longer DONE.
        status, body = self.call("POST", "/api/tasks/1/restore", {"expected_revision": 3})
        self.assertEqual((status, body["card"]["board"]["archived"]), (200, False), body)
        self.assertEqual([e["action"] for e in self.ticket(1)["history"]], ["archive", "reopen", "edit", "restore"])

    def test_archive_and_restore_keep_pending_approvals_and_grants_but_edits_stale_them(self):
        self.auto_closed(20)
        commit = self.ask(20, "commit")
        self.call("POST", "/api/tasks/20/archive", {"expected_revision": self.revision(20)})
        self.assertEqual(self.board()[20]["approvals"][0]["effective_state"], "pending")
        self.call("POST", "/api/tasks/20/restore", {"expected_revision": self.revision(20)})
        self.assertEqual(self.board()[20]["approvals"][0]["effective_state"], "pending")
        status, body = self.call("POST", "/api/tasks/20/approval/decide", self.decide_body(commit))
        self.assertEqual(status, 200, body)
        granted = self.ticket(20)["approvals"]["commit"]
        self.call("POST", "/api/tasks/20/archive", {"expected_revision": self.revision(20)})
        doc = self.ticket(20)
        self.assertEqual((doc["approvals"]["commit"], doc["approval_requests"][0]["state"]), (granted, "approved"))
        # A real edit still stales a pending request.
        self.call("POST", "/api/tasks/20/restore", {"expected_revision": self.revision(20)})
        merge = self.ask(20, "merge")
        self.call("PATCH", "/api/tasks/20", {"notes": "edited", "expected_revision": self.revision(20)})
        states = {a["id"]: a["effective_state"] for a in self.board()[20]["approvals"]}
        self.assertEqual(states[merge["id"]], "stale")
        self.assertEqual(self.call("POST", "/api/tasks/20/approval/decide", self.decide_body(merge))[0], 409)

    def test_cli_archive_and_restore_use_the_same_store(self):
        self.assertEqual(self.nlh("task", "archive", "3")[0], 0)
        self.assertTrue(self.ticket(3)["board"]["archived"])
        code, _, err = self.nlh("task", "restore", "3", "--expected-revision", "0")
        self.assertEqual(code, 2)
        self.assertIn("changed since you loaded it", err)
        self.assertEqual(self.nlh("task", "restore", "3", "--expected-revision", "1")[0], 0)
        self.assertEqual(self.nlh("task", "archive", "2")[0], 2)  # open task
        self.assertEqual(self.nlh("task", "archive", "99")[0], 1)
        self.assertEqual([e["action"] for e in self.ticket(3)["history"]], ["archive", "restore"])


class ActivityPromotion(ArchiveFixture):
    def test_confirmed_run_moves_an_authorized_open_task_once_per_run(self):
        self.write(graph_ticket(40, status="READY"))
        board = store.Board(self.workflow)
        self.assertTrue(board.sync_activity("40", confirmed(40)))
        doc = self.ticket(40)
        self.assertEqual((doc["status"], doc["board"]["stage"]), ("IMPLEMENTING", "In Progress"))
        entry = doc["history"][-1]
        self.assertEqual((entry["action"], entry["by"], entry["run_id"], entry["changes"]["status"]),
                         ("activity_start", "activity-sync", "run-1", {"from": "READY", "to": "IMPLEMENTING"}))
        self.assertFalse(board.sync_activity("40", confirmed(40)))
        # A human moving it back holds for that run; only a new confirmed run moves it again.
        self.call("PATCH", "/api/tasks/40", {"stage": "Next", "expected_revision": doc["board"]["revision"]})
        self.assertFalse(board.sync_activity("40", confirmed(40)))
        self.assertEqual(self.ticket(40)["board"]["stage"], "Next")
        self.assertTrue(board.sync_activity("40", confirmed(40, "run-2")))
        self.assertEqual(self.ticket(40)["board"]["stage"], "In Progress")
        self.write(graph_ticket(41, status="BACKLOG"))
        self.assertTrue(board.sync_activity("41", confirmed(41, role="reviewer")))
        self.assertEqual(self.ticket(41)["status"], "REVIEW")
        self.assertFalse(self.ticket(41)["status"] == "DONE")

    def test_holds_unconfirmed_unbound_unauthorized_and_done_never_move(self):
        self.auto_closed(50)
        self.write(graph_ticket(51, status="READY", authorization={"granted": False}))
        self.write(graph_ticket(52, status="READY", blocker={"reason": "waiting"}))
        self.write({**graph_ticket(53, status="READY"), "board": {"stage": "Blocked", "revision": 2}})
        self.write(graph_ticket(54, status="BLOCKED"))
        self.write(graph_ticket(55, status="READY", approval_requests=[{"id": "55-commit-1", "state": "pending"}]))
        self.write(graph_ticket(56, status="READY"))
        self.write({**graph_ticket(57, status="DONE"), "board": {"stage": "Done", "archived": True, "revision": 1}})
        raw = self.snapshot()
        board = store.Board(self.workflow)
        cases = [(50, confirmed(50)), (51, confirmed(51)), (52, confirmed(52)), (53, confirmed(53)),
                 (54, confirmed(54)), (55, confirmed(55)), (56, confirmed(56, ok=False)),
                 (56, confirmed(56, state="interrupted")), (56, confirmed(99)), (56, {**confirmed(56), "run_id": None}),
                 (57, confirmed(57)), (56, {"task_id": "56", "state": "running"})]
        for task_id, run in cases:
            with self.subTest(task=task_id, run=run):
                self.assertFalse(board.sync_activity(str(task_id), run))
                self.assertEqual(self.snapshot(), raw)
        self.assertEqual(self.ticket(50)["status"], "DONE")

    def test_an_archived_task_once_reopened_moves_on_proof_but_holds_still_protect(self):
        archived = {"stage": "Next", "archived": True, "revision": 1}
        self.write({**graph_ticket(70, status="READY"), "board": archived})
        self.write({**graph_ticket(71, status="READY", blocker={"reason": "waiting"}), "board": archived})
        self.write({**graph_ticket(72, status="READY"), "board": {**archived, "stage": "Blocked"}})
        self.write({**graph_ticket(73, status="READY", approval_requests=[{"id": "73-commit-1", "state": "pending"}]),
                    "board": archived})
        board = store.Board(self.workflow)
        raw = {n: self.path(n).read_bytes() for n in (71, 72, 73)}
        for task_id in (71, 72, 73):
            with self.subTest(task=task_id):
                self.assertFalse(board.sync_activity(str(task_id), confirmed(task_id)))
                self.assertEqual(self.path(task_id).read_bytes(), raw[task_id])
        self.assertFalse(board.sync_activity("70", confirmed(70, ok=False)))
        self.assertTrue(board.sync_activity("70", confirmed(70)))
        doc = self.ticket(70)
        self.assertEqual((doc["status"], doc["board"]["stage"], doc["board"]["archived"]),
                         ("IMPLEMENTING", "In Progress", True))  # still archived until a human Restores it

    def test_board_read_promotes_only_confirmed_runs_and_never_closes(self):
        self.write(graph_ticket(42, status="READY"))
        self.write(graph_ticket(43, status="READY"))
        live = [confirmed(42), confirmed(43, ok=False), confirmed(1)]  # 1 is DONE: never reopened
        with mock.patch.object(server.run_observability, "live_runs", return_value=live):
            cards = self.board()
        self.assertEqual((cards[42]["status"], cards[42]["board"]["stage"]), ("IMPLEMENTING", "In Progress"))
        self.assertEqual((cards[43]["status"], cards[43]["board"]["stage"]), ("READY", "Next"))
        self.assertEqual((cards[1]["status"], cards[1]["board"]["stage"]), ("DONE", "Done"))
        self.assertFalse(cards[42]["auto_done"]["eligible"])


class CompletionDisplay(ArchiveFixture):
    def test_verified_automatic_completion_until_the_records_change(self):
        self.auto_closed(20)
        completion = self.board()[20]["completion"]
        self.assertEqual((completion["kind"], completion["verified"], completion["label"], completion["reasons"]),
                         ("automatic", True, "Verified complete", []))
        self.write({**self.ticket(20), "approvals": {"security": "pending"}})
        completion = self.board()[20]["completion"]
        self.assertEqual((completion["verified"], completion["label"]), (False, "Closed by graph · records changed"))
        self.assertIn("required approval 'security' is not positively approved", completion["reasons"])
        self.assertEqual([e["action"] for e in self.ticket(20)["history"]], ["auto_close"])  # history kept

    def test_manual_and_recorded_closures_are_unverified_unless_the_records_hold(self):
        self.write(graph_ticket(7))
        self.run_graph(7, finish_release=False)
        self.write(graph_ticket(7, authorization={"granted": True, "release_granted": False}))
        before = self.ticket(7)
        self.call("POST", "/api/tasks/7/done", {"expected_revision": 0})
        completion = self.board()[7]["completion"]
        self.assertEqual((completion["kind"], completion["label"], completion["release_unauthorized"]),
                         ("manual", "Closed manually · Unverified", True))
        # Mark Done grants nothing: no release, commit, merge or publication.
        self.assertEqual((self.ticket(7)["authorization"], self.ticket(7).get("approvals")),
                         (before["authorization"], None))
        # A board record check is not verification.
        self.call("PATCH", "/api/tasks/7", {"verification": "verified", "expected_revision": 1})
        self.assertEqual(self.board()[7]["completion"]["label"], "Closed manually · Unverified")
        self.assertEqual(self.board()[1]["completion"]["label"], "Done as recorded · Unverified")
        self.assertIsNone(self.board()[2]["completion"])
        # Records that fully hold: a manual closure made before the board's sync saw the graph.
        self.write(graph_ticket(8))
        self.run_graph(8)
        store.Board(self.workflow).close(8, expected_revision=0, actor="t", now="2026-10-13T00:00:00Z")
        self.assertEqual(self.board()[8]["completion"]["label"], "Closed manually · Verified")

    def test_agent_claims_and_missing_or_failed_tests_never_close(self):
        claims = {"agent_claims": {"status": "done", "summary": "All tests pass"}, "result": "Task complete"}
        self.write(graph_ticket(60, **claims, board={"stage": "In Progress", "verification": "verified",
                                                      "revision": 1}))
        self.run_graph(60, finish_release=False)
        self.assertNotEqual(self.board()[60]["status"], "DONE")
        cases = {61: ({"testing_required": True}, "testing is required"),
                 62: ({"testing_required": True, "gates": {"tests": "failed"}}, "gate 'tests' is not positively passed"),
                 63: ({"testing_required": True, "gates": [{"name": "lint", "status": "passed"}]},
                      "testing is required")}
        for task_id, (extra, reason) in cases.items():
            with self.subTest(task=task_id):
                self.write(graph_ticket(task_id, **extra))
                self.run_graph(task_id)
                card = self.board()[task_id]
                self.assertNotEqual(card["status"], "DONE")
                self.assertTrue(any(reason in r for r in card["auto_done"]["reasons"]), card["auto_done"]["reasons"])
        self.write(graph_ticket(64, testing_required=True, gates={"tests": "passed"}))
        self.run_graph(64)
        self.assertEqual(self.board()[64]["completion"]["label"], "Verified complete")


@unittest.skipUnless(JSDOM, "node with jsdom not available (set HARMONY_JSDOM_PATH)")
class ArchivePage(ArchiveFixture):
    page = tl.LifecyclePage.page
    real = tl.LifecyclePage.real

    def test_codex_completion_and_legacy_card_show_summary_without_manual_or_unverified_label(self):
        for task_id in (70, 71):
            self.write({"id": task_id, "title": "Codex completed work", "status": "BACKLOG",
                        "authorization": {"granted": False}, "board": {"revision": 1},
                        "lifecycle": {}, "history": []})
        status, body = self.call("POST", "/api/tasks/70/complete", {
            "expected_revision": 1, "note": "Verified clean working tree"})
        self.assertEqual(status, 200, body)
        legacy = self.ticket(71)
        legacy["status"] = "DONE"
        legacy["lifecycle"]["closure"] = {"kind": "manual", "by": "codex-lifecycle",
            "label": "Manual closure: administrative only", "note": "Verified tracked project files"}
        self.write(legacy)
        shots = self.page("#kanban", self.real(), [{"snapshot": "board"},
            {"click": '#lanes a[data-inspect="70"]'}, {"snapshot": "inspect"}])
        for task_id in ("70", "71"):
            flags = shots["board"]["kanban"]["flags"][task_id]
            self.assertIn("Completed by Codex", flags)
            self.assertNotIn("Unverified", flags)
            self.assertNotIn("Closed manually", flags)
            self.assertEqual(shots["board"]["kanban"]["lanes"][task_id], "Done")
        self.assertIn("Completion summary", shots["inspect"]["inspect"])
        self.assertIn("Verified clean working tree", shots["inspect"]["inspect"])

    def test_lanes_badges_archive_view_restore_and_bulk(self):
        self.auto_closed(20)
        self.write(graph_ticket(7))
        self.run_graph(7, finish_release=False)
        self.write(graph_ticket(7, authorization={"granted": True, "release_granted": False}))
        self.call("POST", "/api/tasks/7/done", {"expected_revision": 0})
        self.call("PATCH", "/api/tasks/7", {"next_action": "Ship the release", "gate": "release",
                                            "verification": "verified", "expected_revision": 1})
        self.write({**graph_ticket(9, status="BLOCKED"),
                    "board": {"stage": "Blocked", "intake_notice": store.INTAKE_NOTICE, "revision": 1}})
        self.write({**graph_ticket(10, status="READY"), "board": {"stage": "Next", "archived": True, "revision": 1}})
        self.call("POST", "/api/tasks/1/archive", {"expected_revision": 0})
        responses = self.real()
        done = sorted(c["id"] for c in responses["/api/board"]["body"]["cards"]
                      if c["status"] == "DONE" and not c["board"]["archived"])
        responses["/api/tasks/1/restore"] = {"body": self.call("POST", "/api/tasks/1/restore",
                                                               {"expected_revision": 1})[1]}
        responses["/api/tasks/archive"] = {"body": {"ok": True, "cards": []}}
        shots = self.page("#kanban", responses, [
            {"snapshot": "board"}, {"click": "#archive-done"}, {"snapshot": "bulk"},
            {"click": "#show-archived"}, {"snapshot": "archived"},
            {"click": '#lanes .card[data-id="1"] .archive'}, {"snapshot": "restored"}])
        board = shots["board"]["kanban"]
        self.assertEqual(board["bulkLane"], "Done")
        self.assertFalse(board["filters"])
        self.assertEqual(board["laneTitles"], ["To Do", "In Progress", "Blocked", "Done"])
        self.assertNotIn("1", board["lanes"])
        self.assertNotIn("10", board["lanes"])
        self.assertEqual((board["archivedToggle"], board["bulk"]), ("Archived (2)", {"hidden": False, "disabled": False}))
        self.assertIn("Verified complete", board["flags"]["20"])
        self.assertTrue(board["fieldsHidden"]["20"])
        self.assertNotIn("gate", board["flags"]["20"].replace("Verified complete", ""))
        self.assertIn("Closed manually · Unverified", board["flags"]["7"])
        self.assertIn("release not authorized", board["flags"]["7"])
        self.assertIn("Manual closure: administrative only", board["flagTitles"]["7"]["Closed manually · Unverified"])
        self.assertIn("release approval", board["flagTitles"]["7"]["release not authorized"])
        # Every DONE card hides its stale next action and gate (Inspect keeps them); the record
        # checkbox never makes a manual closure verified.
        self.assertTrue(board["fieldsHidden"]["7"])
        self.assertNotIn("gate", board["flags"]["7"].replace("not authorized", ""))
        self.assertIn("Record checked", board["flags"]["7"])
        self.assertNotIn("Closed manually · Verified", board["flags"]["7"])
        self.assertFalse(board["fieldsHidden"]["2"])
        self.assertIn("unassessed at intake", board["flags"]["9"])
        self.assertIn("Unassessed at intake", board["flagTitles"]["9"]["unassessed at intake"])
        self.assertIn("Record unchecked", board["flags"]["2"])
        self.assertIn("not tests", board["flagTitles"]["2"]["Record unchecked"])
        self.assertNotIn("state not verified", json.dumps(board["flags"]))
        self.assertEqual((board["archive"]["20"], board["archive"]["2"]), ("Archive", None))
        bulk = shots["bulk"]["requests"][0]
        self.assertEqual((bulk["path"], sorted(t["id"] for t in bulk["body"]["tasks"])), ("/api/tasks/archive", done))
        archived = shots["archived"]["kanban"]
        self.assertEqual((archived["lanes"], archived["archive"]["1"], archived["bulk"]["hidden"]),
                         ({"1": "Archived", "10": "Archived"}, "Restore", True))
        self.assertEqual(archived["archive"]["10"], "Restore")  # Restore stays available once reopened
        self.assertEqual(shots["restored"]["requests"][-1], {"path": "/api/tasks/1/restore", "method": "POST",
                                                             "body": {"expected_revision": 1}})
        self.assertEqual(shots["restored"]["kanban"]["lanes"], {"10": "Archived"})

    def test_inspect_of_an_archived_task_keeps_its_history(self):
        self.auto_closed(20)
        self.call("POST", "/api/tasks/20/archive", {"expected_revision": self.revision(20)})
        text = self.page("#inspect/20", self.real(), [{"snapshot": "s"}])["s"]["inspect"]
        for expected in ("CompletionVerified complete", "BoardArchived", "History", "auto close", "· archive"):
            self.assertIn(expected, text)


if __name__ == "__main__":
    unittest.main()
