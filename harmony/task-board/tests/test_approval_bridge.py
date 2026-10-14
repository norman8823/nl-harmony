#!/usr/bin/env python3
"""Conversation approval bridge, explicit task identity and card metadata fallback.

`nlh approval request|decide` and the companion's approval endpoints share one
store implementation and one canonical record on the ticket. Temporary
workflow directories and an ephemeral loopback server only.

    python3 -B -m unittest discover -s task-board/tests -p 'test_*.py'
"""

from __future__ import annotations

import contextlib
import copy
import io
import json
import sys
import unittest
from pathlib import Path

import test_task_lifecycle as tl
from test_task_lifecycle import JSDOM, graph_ticket

import store  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from harmony import cli  # noqa: E402

BLOCKER = {"required_resolution": "Human release review", "human_action": "Approve the release gate"}


class ApprovalFixture(tl.LifecycleFixture):
    def nlh(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--project", str(self.repo), *argv])
        return code, out.getvalue(), err.getvalue()

    def gated(self, task_id: int, **extra) -> None:
        """A finished graph whose release gate waits on a human, and a ticket blocked on that gate."""
        self.write(graph_ticket(task_id, authorization={"granted": True}, **extra))
        self.run_graph(task_id, finish_release=False)
        self.graphs().transition(str(task_id), "block", "release", reason="Human release gate")
        self.write({**self.ticket(task_id), "blocker": BLOCKER})

    def ask(self, task_id: int, action: str = "workflow_complete", *extra: str) -> dict:
        args = ["approval", "request", str(task_id), "--action", action, "--summary", "Verified and ready",
                "--evidence", "ev/report.md", "--json", *extra]
        if action == "workflow_complete" and "--resolves-blocker" not in extra and self.ticket(task_id).get("blocker"):
            args.append("--resolves-blocker")
        code, out, err = self.nlh(*args)
        self.assertEqual(code, 0, err)
        return json.loads(out)["request"]

    def decide_body(self, request: dict, decision: str = "approve", **changes) -> dict:
        return {"request_id": request["id"], "action": request["action"], "gate_id": request["gate_id"],
                "binding": request["binding_token"], "decision": decision, **changes}

    def files(self, task_id: int) -> tuple:
        graph = self.workflow / "graphs" / f"{task_id}.json"
        return self.path(task_id).read_bytes(), graph.read_bytes() if graph.is_file() else None


class SharedDecision(ApprovalFixture):
    def test_cli_offer_and_companion_decision_share_one_canonical_record(self):
        self.gated(20)
        code, out, _ = self.nlh("approval", "request", "20", "--action", "workflow_complete", "--summary",
                                "All verified", "--evidence", "ev/report.md", "--resolves-blocker")
        self.assertEqual(code, 0)
        request = self.ticket(20)["approval_requests"][0]
        for text in ("Approval requested for Task #20", "Gate:     release", "Request:  20-workflow_complete-1",
                     request["binding_token"], "Approve ", "Request Changes ", "Defer ", "--approve",
                     "Approving also clears this blocker: Human release review"):
            self.assertIn(text, out)
        self.assertEqual((request["state"], request["task_id"], request["action"], request["gate_id"]),
                         ("pending", 20, "workflow_complete", "release"))
        self.assertEqual(self.ticket(20)["history"][-1]["action"], "approval_request")
        # Asking again for the same bound request writes nothing.
        before = self.files(20)
        code, out, _ = self.nlh("approval", "request", "20", "--action", "workflow_complete", "--summary",
                                "All verified", "--evidence", "ev/report.md", "--resolves-blocker")
        self.assertIn("already pending; nothing was written", out)
        self.assertEqual(self.files(20), before)

        card = self.board()[20]
        self.assertEqual(card["approvals"][0]["effective_state"], "pending")
        self.assertEqual(card["meta"]["next_action"]["source"], "approval_request")
        self.assertIn("20-workflow_complete-1", card["meta"]["next_action"]["value"])
        self.assertNotEqual(card["status"], "DONE")

        status, body = self.call("POST", "/api/tasks/20/approval/decide", self.decide_body(request))
        self.assertEqual(status, 200, body)
        # Closed by the existing completion sync inside the decision: no further board read needed.
        doc = self.ticket(20)
        self.assertEqual((doc["status"], doc["blocker"], doc["lifecycle"]["closure"]["kind"]),
                         ("DONE", None, "automatic"))
        self.assertIs(doc["authorization"]["release_granted"], True)
        for flag in ("commit_granted", "merge_granted", "publish_granted"):
            self.assertNotIn(flag, doc["authorization"])
        self.assertEqual(doc["approvals"]["workflow_complete"]["request_id"], "20-workflow_complete-1")
        graph = json.loads((self.workflow / "graphs" / "20.json").read_text())
        self.assertEqual(graph["nodes"][-1]["state"], "completed")
        self.assertIn(".workflow/tickets/20.json#approval_requests/20-workflow_complete-1",
                      graph["nodes"][-1]["evidence_refs"])
        self.assertTrue(body["closed_automatically"])
        self.assertIn("Nothing was committed, merged or published", body["next_step"])
        self.assertEqual([e["action"] for e in doc["history"][-2:]], ["approval_decision", "auto_close"])
        self.assertEqual(doc["history"][-2]["via"], "companion")

        # The CLI sees the same decision, and a decision is final.
        self.assertRegex(self.nlh("approval", "show", "20")[1], r"20-workflow_complete-1\s+approved")
        before = self.files(20)
        code, _, err = self.nlh("approval", "decide", "20", "--request", request["id"], "--action",
                                "workflow_complete", "--gate", "release", "--binding", request["binding_token"],
                                "--approve")
        self.assertEqual((code, "already approved" in err), (2, True))
        self.assertEqual(self.files(20), before)


class Refusals(ApprovalFixture):
    def test_stale_ambiguous_and_wrong_scope_decisions_write_nothing(self):
        self.gated(21)
        self.gated(22)
        request = self.ask(21)
        before = {21: self.files(21), 22: self.files(22)}
        cases = [
            ("/api/tasks/21/approval/decide", self.decide_body(request, gate_id="commit"), 409, "not 'commit'"),
            ("/api/tasks/21/approval/decide", self.decide_body(request, action="commit"), 409, "not commit"),
            ("/api/tasks/21/approval/decide", self.decide_body(request, binding="t1.x-g1.y"), 409, "does not match"),
            ("/api/tasks/22/approval/decide", self.decide_body(request), 409, "belongs to Task #21"),
            ("/api/tasks/21/approval/decide", self.decide_body(request, request_id="21-workflow_complete-9"), 404,
             "no approval request"),
            ("/api/tasks/99/approval/decide", self.decide_body(request), 404, "does not exist"),
            ("/api/tasks/abc/approval/decide", self.decide_body(request), 400, "explicit existing task id"),
            ("/api/tasks/21/approval/decide", {**self.decide_body(request), "approvals": {"x": "approved"}}, 403,
             "protected"),
            ("/api/tasks/21/approval/decide", {**self.decide_body(request), "release_granted": True}, 403, "protected"),
            ("/api/tasks/21/approval/decide", {**self.decide_body(request), "authorization": {}}, 403, "protected"),
            ("/api/tasks/21/approval/decide", {**self.decide_body(request), "force": True}, 400, "not editable"),
            ("/api/tasks/21/approval/decide", self.decide_body(request, decision="grant"), 400, "decision must"),
        ]
        for path, body, code, text in cases:
            with self.subTest(path=path, body=body):
                status, reply = self.call("POST", path, body)
                self.assertEqual(status, code, reply)
                self.assertIn(text, reply["error"])
        self.assertEqual({21: self.files(21), 22: self.files(22)}, before)

        # Ambiguous: two pending requests for one gate (a hand-edited record) and no request id.
        doc = self.ticket(22)
        twin = self.ask(22)
        doc = self.ticket(22)
        doc["approval_requests"].append({**twin, "id": "22-workflow_complete-2"})
        self.write(doc)
        snapshot = self.files(22)
        status, reply = self.call("POST", "/api/tasks/22/approval/decide",
                                  {k: v for k, v in self.decide_body(twin).items() if k != "request_id"})
        self.assertEqual((status, "name one" in reply["error"]), (409, True))
        self.assertEqual(self.files(22), snapshot)

        # Stale: the graph moved after the request. Nothing is decided; a reissue supersedes, keeping the old one.
        self.graphs().transition("21", "link", "implement", evidence=["ev/extra.md"])
        snapshot = self.files(21)
        status, reply = self.call("POST", "/api/tasks/21/approval/decide", self.decide_body(request))
        self.assertEqual((status, "is stale" in reply["error"]), (409, True))
        self.assertEqual(self.files(21), snapshot)
        card = self.board()[21]
        self.assertEqual(card["approvals"][0]["effective_state"], "stale")
        self.assertIn("is stale", card["meta"]["next_action"]["value"])
        reissued = self.ask(21)
        doc = self.ticket(21)
        self.assertEqual(reissued["id"], "21-workflow_complete-2")
        self.assertEqual([(r["id"], r["state"]) for r in doc["approval_requests"]],
                         [("21-workflow_complete-1", "superseded"), ("21-workflow_complete-2", "pending")])
        self.assertEqual(doc["history"][-1]["superseded"], ["21-workflow_complete-1"])

    def test_a_second_request_or_a_deferral_does_not_stale_other_requests(self):
        self.gated(23)
        release = self.ask(23)
        commit = self.ask(23, "commit")
        self.assertEqual(self.call("POST", "/api/tasks/23/approval/decide", self.decide_body(commit, "defer"))[0], 200)
        self.assertEqual([a["effective_state"] for a in self.board()[23]["approvals"]], ["pending", "deferred"])
        # Any other write stales it: a board edit moves the revision.
        self.call("PATCH", "/api/tasks/23", {"notes": "edited", "expected_revision": self.board()[23]["board"]["revision"]})
        self.assertEqual(self.board()[23]["approvals"][0]["effective_state"], "stale")
        self.assertEqual(self.call("POST", "/api/tasks/23/approval/decide", self.decide_body(release))[0], 409)


class Decisions(ApprovalFixture):
    def test_request_changes_and_defer_record_a_decision_and_grant_nothing(self):
        self.gated(24)
        graph_before = self.files(24)[1]
        request = self.ask(24)
        status, body = self.call("POST", "/api/tasks/24/approval/decide",
                                 self.decide_body(request, "request_changes", note="Add the browser proof"))
        self.assertEqual(status, 200, body)
        self.assertIn("Changes requested on workflow_complete for Task #24: Add the browser proof", body["next_step"])
        self.assertIn("nlh approval request 24", body["next_step"])
        doc = self.ticket(24)
        self.assertEqual((doc["status"], doc["blocker"]), ("IMPLEMENTING", BLOCKER))
        self.assertNotIn("release_granted", doc["authorization"])
        self.assertNotIn("approvals", doc)
        self.assertEqual(self.files(24)[1], graph_before)
        self.assertIn("Changes requested on workflow_complete: Add the browser proof",
                      self.board()[24]["meta"]["next_action"]["value"])

        again = self.ask(24)
        code, out, _ = self.nlh("approval", "decide", "24", "--request", again["id"], "--action", "workflow_complete",
                                "--gate", "release", "--binding", again["binding_token"], "--defer", "--note", "Tomorrow")
        self.assertEqual(code, 0)
        self.assertIn("deferred: Tomorrow. Nothing was granted, closed or run", out)
        doc = self.ticket(24)
        self.assertNotIn("release_granted", doc["authorization"])
        self.assertEqual(self.files(24)[1], graph_before)
        # The audit list keeps every request and decision exactly as made.
        self.assertEqual([r["state"] for r in doc["approval_requests"]], ["changes_requested", "deferred"])
        self.assertEqual([(e["action"], e.get("decision")) for e in doc["history"] if "approval" in e["action"]],
                         [("approval_request", None), ("approval_decision", "request_changes"),
                          ("approval_request", None), ("approval_decision", "defer")])
        self.assertEqual(doc["history"][-1]["note"], "Tomorrow")
        self.assertEqual(doc["approval_requests"][0]["binding"], request["binding"])

    def test_close_commit_and_publish_are_distinct_scopes(self):
        self.write(graph_ticket(25, authorization={"granted": True}))
        self.run_graph(25, finish_release=False)
        graph_before = self.files(25)[1]
        code, _, err = self.nlh("approval", "request", "25", "--action", "publish", "--summary", "Publish it")
        self.assertEqual((code, "commit has not been approved" in err), (2, True))

        commit = self.ask(25, "commit", "--content-ref", "diff-sha256:abc123")
        code, out, _ = self.nlh("approval", "decide", "25", "--request", commit["id"], "--action", "commit",
                                "--gate", "commit", "--binding", commit["binding_token"], "--approve")
        self.assertEqual(code, 0)
        self.assertIn("Authorized next action: the existing agent may now commit exactly the bound content "
                      "(diff-sha256:abc123)", out)
        self.assertIn("performed no git operation", out)
        doc = self.ticket(25)
        self.assertEqual(doc["authorization"], {"granted": True, "commit_granted": True})
        self.assertEqual(doc["status"], "IMPLEMENTING")

        publish = self.ask(25, "publish")
        status, body = self.call("POST", "/api/tasks/25/approval/decide", self.decide_body(publish))
        self.assertEqual(status, 200, body)
        doc = self.ticket(25)
        self.assertEqual(doc["authorization"], {"granted": True, "commit_granted": True, "publish_granted": True})
        self.assertNotEqual(doc["status"], "DONE")

        close = self.ask(25, "task_close")
        status, body = self.call("POST", "/api/tasks/25/approval/decide", self.decide_body(close))
        doc = self.ticket(25)
        self.assertEqual((doc["status"], doc["lifecycle"]["closure"]["kind"]), ("DONE", "manual"))
        self.assertIn("approved close request 25-task_close-", doc["lifecycle"]["closure"]["note"])
        self.assertNotIn("release_granted", doc["authorization"])
        self.assertNotIn("task_close", doc["approvals"])
        self.assertEqual(self.files(25)[1], graph_before)  # the release gate is untouched by any of them
        self.assertIn("Nothing was released, committed, merged or published", body["next_step"])

    def test_failed_prerequisites_are_refused_before_anything_is_written(self):
        self.write(graph_ticket(26, authorization={"granted": True}))
        self.run_graph(26, review="fail")
        before = self.files(26)
        code, _, err = self.nlh("approval", "request", "26", "--action", "workflow_complete", "--summary", "ship")
        self.assertEqual(code, 2)
        self.assertIn("no review recorded as pass", err)
        code, _, err = self.nlh("approval", "request", "3", "--action", "commit", "--summary", "commit it")
        self.assertEqual((code, "no task graph" in err), (2, True))
        self.assertEqual(self.files(26), before)

        # A pending request whose gate no longer holds at decision time (written by hand here).
        self.gated(27, approvals={"security": "pending"})
        doc = self.ticket(27)
        binding = store.current_binding(doc, 0, json.loads((self.workflow / "graphs" / "27.json").read_text()))
        doc["approval_requests"] = [{"id": "27-workflow_complete-1", "task_id": 27, "action": "workflow_complete",
                                     "gate_id": "release", "scope": "x", "binding": binding,
                                     "binding_token": store.binding_token(binding), "summary": "ship",
                                     "evidence": [], "content_ref": None, "resolves_blocker": BLOCKER,
                                     "state": "pending", "decision": None}]
        self.write(doc)
        before = self.files(27)
        status, body = self.call("POST", "/api/tasks/27/approval/decide",
                                 self.decide_body(doc["approval_requests"][0]))
        self.assertEqual(status, 409, body)
        self.assertIn("required approval 'security' is not positively approved", body["error"])
        self.assertEqual(self.files(27), before)


class Identity(ApprovalFixture):
    def test_follow_ups_records_and_approvals_use_explicit_existing_ids(self):
        tickets = sorted(p.name for p in (self.workflow / "tickets").glob("*.json"))
        for argv, code, text in (
            (("approval", "request", "99", "--action", "task_close", "--summary", "close it"), 1, "does not exist"),
            (("approval", "request", "abc", "--action", "task_close", "--summary", "close it"), 2, "explicit"),
            (("record", "--task-id", "99", "--role", "reviewer", "--", "codex", "exec", "--json", "-"), 2,
             "Task #99 does not exist"),
            (("record", "--task-id", "../x", "--role", "reviewer", "--", "codex", "exec", "--json", "-"), 2, "unsafe"),
            (("task", "resolve"), 2, "nothing is guessed"),
            (("task", "relate", "3", "--follow-up-of", "99"), 1, "does not exist"),
        ):
            with self.subTest(argv=argv):
                got, _, err = self.nlh(*argv)
                self.assertEqual(got, code, err)
                self.assertIn(text, err)
        self.assertIn("#2 [IMPLEMENTING]", self.nlh("task", "resolve")[2])
        self.assertEqual(self.nlh("task", "resolve", "--task-id", "2")[0], 0)
        self.assertEqual(sorted(p.name for p in (self.workflow / "tickets").glob("*.json")), tickets)
        self.assertFalse((self.workflow / "graphs").exists())

        earlier = self.path(2).read_bytes()
        self.assertEqual(self.nlh("task", "relate", "3", "--follow-up-of", "2", "--note", "adds freshness")[0], 0)
        once = self.path(3).read_bytes()
        self.assertEqual(self.nlh("task", "relate", "3", "--follow-up-of", "2")[0], 0)
        self.assertEqual(self.path(3).read_bytes(), once)  # recorded once
        self.assertEqual(self.path(2).read_bytes(), earlier)  # the earlier task is never rewritten
        self.assertEqual(self.ticket(3)["relations"][0]["task_id"], 2)
        cards = self.board()
        self.assertEqual((cards[2]["followed_up_by"], cards[3]["relations"][0]["type"]), ([3], "follow_up_of"))


class CardMetadata(ApprovalFixture):
    def test_unknown_board_fields_fall_back_read_only(self):
        self.write({"id": 30, "title": "Blocked task", "status": "BLOCKED",
                    "blocker": {"required_resolution": "Human gate", "human_action": "Authorize the commit"}})
        self.write({"id": 31, "title": "Graph task", "status": "IMPLEMENTING", "authorization": {"granted": True}})
        self.write(graph_ticket(31, authorization={"granted": True}))
        self.run_graph(31, finish_release=False)
        self.write({"id": 32, "title": "Handed off", "status": "READY"})
        (self.workflow / "checkpoints").mkdir()
        (self.workflow / "checkpoints" / "32.json").write_text(json.dumps({"handoff": {"plan": ["Run the smoke"]}}))
        self.write({"id": 34, "title": "Closed", "status": "DONE"})
        (self.workflow / "checkpoints" / "34.json").write_text(json.dumps({"handoff": {"plan": ["Old step"]}}))
        before = {n: self.path(n).read_bytes() for n in (30, 31, 32, 34)}
        cards = self.board()
        meta = {n: cards[n]["meta"] for n in (30, 31, 32, 34)}
        self.assertEqual(meta[34]["next_action"]["source"], "status")  # an old handoff is not a DONE task's next step
        self.assertEqual(meta[30]["next_action"], {"value": "Authorize the commit", "source": "blocker"})
        self.assertEqual(meta[30]["gate"], {"value": "Human gate", "source": "blocker"})
        self.assertEqual(meta[30]["owner"], {"value": "Unassigned", "source": "missing"})
        self.assertEqual(cards[30]["board"]["owner"], store.UNKNOWN)  # the stored field is not invented
        self.assertEqual(meta[31]["next_action"]["source"], "graph")
        self.assertIn("Start release only after", meta[31]["next_action"]["value"])
        self.assertIsNone(meta[31]["approval"])  # a finished review is not an approval
        self.assertEqual(meta[32]["next_action"], {"value": "Run the smoke", "source": "checkpoint"})
        self.assertEqual({n: self.path(n).read_bytes() for n in (30, 31, 32, 34)}, before)  # presentation wrote nothing
        card = self.call("PATCH", "/api/tasks/30", {"owner": "Norman", "next_action": "Wait", "expected_revision": 0})[1]
        self.assertEqual((card["card"]["meta"]["owner"]["source"], card["card"]["meta"]["next_action"]["value"]),
                         ("board", "Wait"))


class WorkflowScope(ApprovalFixture):
    """Review corrections: a workflow approval authorizes only its own content and graph cycle."""

    def approve_workflow(self, task_id: int) -> dict:
        request = self.ask(task_id)
        status, body = self.call("POST", f"/api/tasks/{task_id}/approval/decide", self.decide_body(request))
        self.assertEqual(status, 200, body)
        return request

    def new_cycle(self, task_id: int, *, finish_release: bool) -> None:
        path = self.workflow / "graphs" / f"{task_id}.json"
        path.unlink()
        self.run_graph(task_id, finish_release=finish_release)
        # Graph times have one-second resolution; give the rebuilt graph its own cycle identity.
        graph = json.loads(path.read_text())
        self.cycles = getattr(self, "cycles", 0) + 1
        graph["created_at"] = f"2030-01-0{self.cycles}T00:00:00+00:00"
        path.write_text(json.dumps(graph))

    def test_approve_done_reopen_edit_new_cycle_is_not_done_until_a_fresh_approval(self):
        self.gated(40)
        first = self.approve_workflow(40)
        self.assertEqual(self.ticket(40)["status"], "DONE")
        revision = self.board()[40]["board"]["revision"]
        self.assertEqual(self.call("POST", "/api/tasks/40/reopen", {"expected_revision": revision})[0], 200)
        revision = self.board()[40]["board"]["revision"]
        self.assertEqual(self.call("PATCH", "/api/tasks/40", {"details": "A different deliverable",
                                                               "expected_revision": revision})[0], 200)
        self.new_cycle(40, finish_release=True)  # a completed new cycle; release_granted is still on the ticket
        card = self.board()[40]
        self.assertNotEqual(card["status"], "DONE")
        self.assertIn(f"workflow approval ({first['id']}) was granted for other task content",
                      " ".join(card["auto_done"]["reasons"]))
        self.assertEqual(card["approvals"][0]["state"], "approved")  # history is kept as it was
        self.assertIn("approval_decision", [e["action"] for e in self.ticket(40)["history"]])

        self.new_cycle(40, finish_release=False)
        fresh = self.approve_workflow(40)
        doc = self.ticket(40)
        self.assertEqual((doc["status"], doc["lifecycle"]["closure"]["kind"]), ("DONE", "automatic"))
        self.assertEqual(doc["approvals"]["workflow_complete"]["request_id"], fresh["id"])

    def test_bookkeeping_events_keep_the_grant_and_changes_stale_it(self):
        self.gated(41)
        self.approve_workflow(41)
        self.assertEqual(self.ticket(41)["status"], "DONE")
        commit = self.ask(41, "commit")
        self.assertEqual(self.call("POST", "/api/tasks/41/approval/decide", self.decide_body(commit, "defer"))[0], 200)
        again = self.ask(41, "commit")
        self.assertEqual(self.call("POST", "/api/tasks/41/approval/decide", self.decide_body(again))[0], 200)
        graph = json.loads((self.workflow / "graphs" / "41.json").read_text())
        # An approved commit is permission only: without recorded execution it keeps a task open.
        unexecuted = {**self.ticket(41), "status": "REVIEW", "lifecycle": {}}
        self.assertIn(f"approved commit ({again['id']}) has no verified successful execution",
                      " ".join(store.completion_check(unexecuted, graph)["reasons"]))
        code, _, err = self.nlh("approval", "execute", "41", "--request", again["id"], "--action", "commit",
                                "--binding", again["binding_token"], "--succeeded", "--result-ref", "08ed79a",
                                "--evidence", "ev/commit.md")
        self.assertEqual(code, 0, err)
        doc = self.ticket(41)
        open_doc = {**doc, "status": "REVIEW", "lifecycle": {}}  # as if the derived closure were not there
        self.assertEqual(store.completion_check(open_doc, graph)["reasons"], [])
        changed = {**open_doc, "scope": {"summary": "changed"}}
        self.assertFalse(store.completion_check(changed, graph)["eligible"])
        other_cycle = {**graph, "created_at": "2030-01-01T00:00:00+00:00"}
        self.assertFalse(store.completion_check(open_doc, other_cycle)["eligible"])
        legacy = {**open_doc, "approvals": {}}  # no managed approval: the explicit release grant still counts
        self.assertEqual(store.completion_check(legacy, other_cycle)["reasons"], [])

    def test_required_workflow_approval_is_not_circular(self):
        self.gated(42, required_approvals=["workflow_complete"])
        self.approve_workflow(42)
        self.assertEqual(self.ticket(42)["status"], "DONE")
        self.gated(43, required_approvals=["workflow_complete", "security"], approvals={"security": "pending"})
        code, _, err = self.nlh("approval", "request", "43", "--action", "workflow_complete", "--summary", "ship",
                                "--resolves-blocker")
        self.assertEqual(code, 2)
        self.assertIn("'security' is not positively approved", err)
        self.assertNotIn("'workflow_complete' is not", err)


class ExecutionEvidence(ApprovalFixture):
    """Approve -> execute -> verify -> record -> complete; an approval is never the result."""

    SHA = "08ed79af1a9c37f1cd4d6bddd4bc057e30fa03a6"

    def approve(self, task_id: int, action: str) -> dict:
        request = self.ask(task_id, action)
        code, _, err = self.nlh("approval", "decide", str(task_id), "--request", request["id"], "--action", action,
                                "--gate", request["gate_id"], "--binding", request["binding_token"], "--approve")
        self.assertEqual(code, 0, err)
        return request

    def execute(self, task_id: int, request: dict, *extra: str, action: str | None = None,
                binding: str | None = None) -> tuple[int, str, str]:
        return self.nlh("approval", "execute", str(task_id), "--request", request["id"], "--action",
                        action or request["action"], "--binding", binding or request["binding_token"], *extra)

    def reasons(self, task_id: int) -> str:
        return " ".join(self.board()[task_id]["auto_done"]["reasons"])

    def test_missing_then_failed_keep_it_open_and_success_closes_without_another_approval(self):
        self.gated(50, required_approvals=["commit"])
        commit = self.approve(50, "commit")
        card = self.board()[50]
        self.assertEqual(card["meta"]["next_action"]["source"], "approval_request")
        self.assertIn("Execute approved commit now",card["meta"]["next_action"]["value"])
        self.assertIn(f"nlh approval execute 50 --request {commit['id']}", card["meta"]["next_action"]["value"])

        # The workflow may be approved before the commit runs; completion still waits for execution.
        self.approve(50, "workflow_complete")
        doc = self.ticket(50)
        self.assertIs(doc["authorization"]["release_granted"], True)
        self.assertNotEqual(doc["status"], "DONE")
        self.assertIn(f"approved commit ({commit['id']}) has no verified successful execution", self.reasons(50))

        code, out, err = self.execute(50, commit, "--failed", "--note", "pre-commit hook rejected it")
        self.assertEqual(code, 0, err)
        self.assertIn("do not ask again", out)
        self.assertNotEqual(self.ticket(50)["status"], "DONE")
        self.assertIn("failed when executed", self.reasons(50))
        self.assertIn("failed when executed", self.board()[50]["meta"]["next_action"]["value"])

        requests_before = len(self.ticket(50)["approval_requests"])
        code, out, err = self.execute(50, commit, "--succeeded", "--result-ref", self.SHA,
                                      "--evidence", "ev/git-log.txt", "--json")
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        self.assertTrue(result["closed_automatically"])
        doc = self.ticket(50)
        self.assertEqual((doc["status"], doc["lifecycle"]["closure"]["kind"]), ("DONE", "automatic"))
        self.assertEqual([e["action"] for e in doc["history"][-3:]],
                         ["approval_execution", "approval_execution", "auto_close"])
        execution = doc["approval_requests"][0]["execution"]
        self.assertEqual((execution["outcome"], execution["result_ref"], execution["request_id"]),
                         ("succeeded", self.SHA, commit["id"]))
        self.assertEqual(len(doc["approval_requests"]), requests_before)  # nobody was asked twice
        self.assertNotIn("merge_granted", doc["authorization"])
        self.assertNotIn("publish_granted", doc["authorization"])

        before = self.files(50)
        code, _, err = self.execute(50, commit, "--succeeded", "--result-ref", self.SHA, "--evidence", "ev/x.md")
        self.assertEqual((code, "already recorded as succeeded" in err), (2, True))
        self.assertEqual(self.files(50), before)

    def test_mismatched_pending_superseded_and_stale_evidence_is_refused(self):
        self.gated(51)
        self.gated(52)
        commit = self.approve(51, "commit")
        merge = self.ask(51, "merge")  # pending, never approved
        ok = ("--succeeded", "--result-ref", self.SHA, "--evidence", "ev/git-log.txt")
        before = {51: self.files(51), 52: self.files(52)}
        cases = [
            (self.execute(51, commit, *ok, binding="t1.x-g1.y"), "does not match"),
            (self.execute(51, commit, *ok, action="merge"), "is for commit, not merge"),
            (self.execute(52, commit, *ok), "belongs to Task #51"),
            (self.execute(51, merge, *ok), "is pending, not approved"),
            (self.execute(51, commit, "--succeeded", "--evidence", "ev/git-log.txt"), "names its result"),
            (self.execute(51, commit, "--succeeded", "--result-ref", "HEAD", "--evidence", "ev/g.txt"), "commit SHA"),
            (self.execute(51, commit, "--succeeded", "--result-ref", self.SHA), "verification evidence"),
            (self.execute(51, commit, "--failed"), "evidence or a note"),
        ]
        for (code, _, err), text in cases:
            with self.subTest(text=text):
                self.assertEqual(code, 2, err)
                self.assertIn(text, err)
        self.assertEqual({51: self.files(51), 52: self.files(52)}, before)

        # A newer approval supersedes the old one: its execution is not recorded, and the new
        # approval never inherits evidence recorded on an earlier request.
        newer = self.approve(51, "commit")
        code, _, err = self.execute(51, commit, *ok)
        self.assertEqual((code, "not the current commit approval" in err), (2, True))
        self.assertEqual(self.execute(51, newer, *ok)[0], 0)
        newest = self.approve(51, "commit")
        self.assertIn(f"approved commit ({newest['id']}) has no verified successful execution", self.reasons(51))

        # Content changed since the approval: stale, nothing recorded.
        revision = self.board()[51]["board"]["revision"]
        self.assertEqual(self.call("PATCH", "/api/tasks/51", {"details": "Different content",
                                                               "expected_revision": revision})[0], 200)
        snapshot = self.files(51)
        code, _, err = self.execute(51, newest, *ok)
        self.assertEqual((code, "is stale" in err), (2, True))
        self.assertEqual(self.files(51), snapshot)

    def test_legacy_or_unknown_execution_is_never_satisfied(self):
        self.write(graph_ticket(53))
        self.run_graph(53)
        doc, graph = self.ticket(53), json.loads((self.workflow / "graphs" / "53.json").read_text())
        self.assertEqual(store.completion_check(doc, graph)["reasons"], [])
        for approvals, extra in (({"commit": "approved"}, {}),
                                 ({"publish": {"decision": "approved"}}, {"required_approvals": ["publish"]}),
                                 ({"merge": {"decision": "approved", "request_id": "53-merge-1"}}, {})):
            with self.subTest(approvals=approvals):
                check = store.completion_check({**doc, "approvals": approvals, **extra}, graph)
                self.assertFalse(check["eligible"])
                self.assertIn("no approved request to carry execution evidence", " ".join(check["reasons"]))
        optional = {**doc, "approvals": {"commit": {"decision": "approved", "required": False}}}
        self.assertEqual(store.completion_check(optional, graph)["reasons"], [])

    def rebuild_graph(self, task_id: int, created_at: str) -> None:
        """Same ticket content, a new task graph: a new work cycle (release waiting)."""
        path = self.workflow / "graphs" / f"{task_id}.json"
        path.unlink()
        ticket = self.ticket(task_id)
        self.write({**ticket, "blocker": None})
        self.run_graph(task_id, finish_release=False)
        self.write(ticket)
        graph = json.loads(path.read_text())
        graph["created_at"] = created_at
        path.write_text(json.dumps(graph))

    def test_a_new_graph_cycle_with_the_same_content_needs_fresh_approval_and_execution(self):
        ok = ("--succeeded", "--result-ref", self.SHA, "--evidence", "ev/git-log.txt")
        # Recording against an approval from the previous cycle is refused.
        self.gated(54)
        old = self.approve(54, "commit")
        self.rebuild_graph(54, "2030-02-01T00:00:00+00:00")
        before = self.files(54)
        code, _, err = self.execute(54, old, *ok)
        self.assertEqual((code, "another work cycle" in err, "fresh commit approval" in err), (2, True, True))
        self.assertEqual(self.files(54), before)

        # A success from the previous cycle does not satisfy a workflow approved in the new one.
        self.gated(55)
        earlier = self.approve(55, "commit")
        self.assertEqual(self.execute(55, earlier, *ok)[0], 0)
        self.rebuild_graph(55, "2030-02-02T00:00:00+00:00")
        self.approve(55, "workflow_complete")  # the expected release transition stays in the new cycle
        self.assertNotEqual(self.ticket(55)["status"], "DONE")
        self.assertIn(f"approved commit ({earlier['id']}) was granted for other task content or another work cycle",
                      self.reasons(55))
        fresh = self.approve(55, "commit")
        code, out, err = self.execute(55, fresh, *ok, "--json")
        self.assertEqual(code, 0, err)
        self.assertTrue(json.loads(out)["closed_automatically"])
        self.assertEqual(self.ticket(55)["status"], "DONE")

    def test_completion_validates_the_whole_approval_request_execution_chain(self):
        self.gated(56)
        commit = self.approve(56, "commit")
        self.approve(56, "workflow_complete")
        doc = self.ticket(56)
        self.assertNotEqual(doc["status"], "DONE")
        graph = json.loads((self.workflow / "graphs" / "56.json").read_text())
        approval = doc["approvals"]["commit"]
        valid = {"outcome": "succeeded", "request_id": commit["id"], "action": "commit",
                 "binding_token": commit["binding_token"], "result_ref": self.SHA, "evidence": ["ev/git-log.txt"],
                 "scope_sha": approval["scope_sha"], "cycle": approval["cycle"]}

        def variant(execution: dict, request: dict | None = None, approval_changes: dict | None = None) -> dict:
            changed = copy.deepcopy(doc)
            target = next(r for r in changed["approval_requests"] if r["id"] == commit["id"])
            target.update(request or {}, execution=execution)
            changed["approvals"]["commit"].update(approval_changes or {})
            return changed

        self.assertTrue(store.completion_check(variant(valid), graph)["eligible"])
        incomplete = "is incomplete or does not match its approval"
        forged = {"outcome": "succeeded", "request_id": commit["id"], "scope_sha": approval["scope_sha"]}
        cases = [
            (variant(forged), incomplete),
            (variant({**valid, "result_ref": None}), incomplete),
            (variant({**valid, "result_ref": "HEAD"}), incomplete),
            (variant({**valid, "evidence": []}), incomplete),
            (variant({**valid, "evidence": [""]}), incomplete),
            (variant({**valid, "binding_token": "t1.x-g1.y"}), incomplete),
            (variant({**valid, "action": "merge"}), incomplete),
            (variant({**valid, "request_id": "56-commit-9"}), incomplete),
            (variant({**valid, "cycle": {"graph_created_at": "2001-01-01T00:00:00+00:00"}}), incomplete),
            (variant(valid, {"task_id": 999}), "does not match its request"),
            (variant(valid, {"binding_token": "t1.x-g1.y"}), "does not match its request"),
            (variant(valid, {"decision": {"decision": "defer", "binding_token": commit["binding_token"]}}),
             "does not match its request"),
            (variant(valid, approval_changes={"scope_sha": None}), "predates content and work-cycle binding"),
        ]
        for ticket, text in cases:
            with self.subTest(text=text, execution=ticket["approval_requests"][0].get("execution")):
                check = store.completion_check(ticket, graph)
                self.assertFalse(check["eligible"])
                self.assertIn(text, " ".join(check["reasons"]))

        # A forged record stored on the ticket never closes it on the board's read path.
        self.write(variant(forged))
        self.assertNotEqual(self.board()[56]["status"], "DONE")
        self.write(doc)
        code, _, err = self.execute(56, commit, "--succeeded", "--result-ref", self.SHA, "--evidence", "ev/g.txt")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ticket(56)["status"], "DONE")

    def test_a_pre_change_managed_approval_needs_a_fresh_approval(self):
        self.gated(57)
        old = self.approve(57, "commit")
        doc = self.ticket(57)
        for key in ("scope_sha", "cycle"):  # as an approval recorded before content/cycle binding existed
            del doc["approvals"]["commit"][key]
        self.write(doc)
        revision = self.board()[57]["board"]["revision"]
        self.assertEqual(self.call("PATCH", "/api/tasks/57", {"details": "Changed after the approval",
                                                               "expected_revision": revision})[0], 200)
        before = self.files(57)
        code, _, err = self.execute(57, old, "--succeeded", "--result-ref", self.SHA, "--evidence", "ev/g.txt")
        self.assertEqual(code, 2, err)
        self.assertIn("predates content and work-cycle binding; request a fresh commit approval", err)
        self.assertEqual(self.files(57), before)
        self.assertIn(f"approved commit ({old['id']}) predates content and work-cycle binding", self.reasons(57))
        fresh = self.approve(57, "commit")
        self.assertEqual(self.execute(57, fresh, "--succeeded", "--result-ref", self.SHA,
                                      "--evidence", "ev/g.txt")[0], 0)


FEEDBACK = "Add the browser proof"


class Feedback(ApprovalFixture):
    """Request Changes carries required feedback, enforced in the shared Board.decide."""

    def changes(self, task_id: int) -> dict:
        """A pending release request decided as Request Changes with FEEDBACK."""
        self.gated(task_id)
        request = self.ask(task_id)
        status, body = self.call("POST", f"/api/tasks/{task_id}/approval/decide",
                                 self.decide_body(request, "request_changes", note=f"  {FEEDBACK}  "))
        self.assertEqual(status, 200, body)
        return request

    def test_empty_whitespace_and_overlong_feedback_are_refused_everywhere_and_write_nothing(self):
        self.gated(40)
        request = self.ask(40)
        before = self.files(40)
        for note in (None, "", "   \n\t  ", "x" * 1001):
            with self.subTest(note=note):
                body = self.decide_body(request, "request_changes", **({} if note is None else {"note": note}))
                status, reply = self.call("POST", "/api/tasks/40/approval/decide", body)
                self.assertEqual(status, 400, reply)
                self.assertEqual(self.files(40), before)
        code, _, err = self.nlh("approval", "decide", "40", "--request", request["id"], "--action", "workflow_complete",
                                "--gate", "release", "--binding", request["binding_token"], "--request-changes")
        self.assertEqual((code != 0, "needs feedback" in err), (True, True), err)
        code, _, _ = self.nlh("approval", "decide", "40", "--request", request["id"], "--action", "workflow_complete",
                              "--gate", "release", "--binding", request["binding_token"], "--request-changes",
                              "--note", "   ")
        self.assertNotEqual(code, 0)
        with self.assertRaises(store.StoreError):
            store.Board(self.workflow).decide(40, request_id=request["id"], action="workflow_complete", gate="release",
                                              binding=request["binding_token"], decision="request_changes",
                                              actor="t", now="2026-10-13T00:00:00Z", via="test", note=" ")
        self.assertEqual(self.files(40), before)
        self.assertEqual(self.board()[40]["approvals"][0]["effective_state"], "pending")

    def test_valid_feedback_is_stored_on_the_exact_binding_and_served_after_reload(self):
        request = self.changes(41)
        doc = self.ticket(41)
        decided = doc["approval_requests"][0]
        self.assertEqual((decided["id"], decided["task_id"], decided["action"], decided["gate_id"], decided["state"]),
                         (request["id"], 41, "workflow_complete", "release", "changes_requested"))
        self.assertEqual((decided["binding_token"], decided["binding"]), (request["binding_token"], request["binding"]))
        self.assertEqual((decided["decision"]["note"], decided["decision"]["binding_token"]),
                         (FEEDBACK, request["binding_token"]))
        self.assertEqual((doc["history"][-1]["action"], doc["history"][-1]["note"]), ("approval_decision", FEEDBACK))
        # What a reloaded page (the board bootstrap and Inspect) receives.
        view = self.board()[41]["approvals"][0]
        self.assertEqual(view["decision"]["note"], FEEDBACK)
        self.assertIn(f"Changes requested on workflow_complete for Task #41: {FEEDBACK}", view["next_step"])
        code, output, error = self.nlh("approval", "show", "41", "--json")
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)[0]["decision"]["note"], FEEDBACK)

    def test_decided_requests_keep_their_next_step_and_an_old_empty_one_invents_nothing(self):
        self.changes(42)
        # An earlier Request Changes recorded before feedback was required (note null), as on Task 8.
        doc = self.ticket(42)
        doc["approval_requests"][0]["decision"]["note"] = None
        doc["history"][-1]["note"] = None
        self.write(doc)
        view = self.board()[42]["approvals"][0]
        self.assertIsNone(view["decision"]["note"])
        self.assertTrue(view["next_step"].startswith("Changes requested on workflow_complete for Task #42. Nothing"),
                        view["next_step"])
        again = self.ask(42)
        self.assertEqual(self.call("POST", "/api/tasks/42/approval/decide", self.decide_body(again, "defer"))[0], 200)
        deferred = {a["id"]: a for a in self.board()[42]["approvals"]}[again["id"]]
        self.assertIn("deferred. Nothing was granted", deferred["next_step"])
        # Commit approval: strictly one commit; merge and publish are not authorized by it.
        self.write(graph_ticket(43, authorization={"granted": True}))
        self.run_graph(43, finish_release=False)
        commit = self.ask(43, "commit")
        status, body = self.call("POST", "/api/tasks/43/approval/decide", self.decide_body(commit))
        self.assertEqual(status, 200, body)
        self.assertIn("permits this one commit only; merge and publish each need their own approval", body["next_step"])
        self.assertEqual(self.board()[43]["approvals"][0]["next_step"], body["next_step"])
        self.assertEqual(self.ticket(43)["authorization"], {"granted": True, "commit_granted": True})

        # Returning to Inspect must not turn a historical approval into permission for changed content.
        self.call("PATCH", "/api/tasks/43", {"details": "Changed scope", "expected_revision":
                  self.board()[43]["board"]["revision"]})
        old = self.board()[43]["approvals"][0]
        self.assertEqual(old["state"], "approved")  # history is preserved
        self.assertIn("does not authorize current content", old["next_step"])
        self.assertNotIn("may now commit", old["next_step"])


@unittest.skipUnless(JSDOM, "node with jsdom not available (set HARMONY_JSDOM_PATH)")
class ApprovalPage(ApprovalFixture):
    page = tl.LifecyclePage.page
    real = tl.LifecyclePage.real

    def test_inspect_offers_the_pending_request_and_cards_show_fallbacks(self):
        self.gated(33)
        request = self.ask(33)
        responses = self.real()
        responses["/api/tasks/33/approval/decide"] = {
            "body": self.call("POST", "/api/tasks/33/approval/decide", self.decide_body(request))[1]}
        shots = self.page("#kanban", responses, [{"snapshot": "board"}])
        fields = shots["board"]["kanban"]["fields"]["33"]
        self.assertEqual(fields["owner"], "Unassigned")
        self.assertIn("Human decision needed", fields["next"])
        self.assertEqual(fields["nextSource"], "from the approval request")
        self.assertIn("approval pending: workflow_complete", shots["board"]["kanban"]["flags"]["33"])

        button = '.approval[data-request="33-workflow_complete-1"] button[data-decision="approve"]'
        shots = self.page("#inspect/33", responses, [{"snapshot": "open"}, {"click": button}, {"snapshot": "decided"}])
        self.assertEqual(shots["open"]["approvals"], [{"id": "33-workflow_complete-1", "state": "pending",
                                                       "buttons": ["Approve", "Request Changes", "Defer"]}])
        self.assertIn(request["binding_token"], shots["open"]["inspect"])
        # Approve sends exactly the offered request and binding; no note field is offered or sent.
        self.assertEqual(shots["decided"]["requests"], [{"path": "/api/tasks/33/approval/decide", "method": "POST",
                                                         "body": self.decide_body(request)}])
        self.assertIsNone(shots["open"]["approvalPanels"][0]["feedback"])
        self.assertNotIn("Note (optional)", shots["open"]["inspect"])
        result = shots["decided"]["approvalResult"]
        self.assertEqual(result["role"], "status")
        self.assertTrue(result["text"].startswith("Approved"), result)
        self.assertIn("closed automatically", result["text"])

    def test_inspect_of_a_task_with_runs_also_shows_its_canonical_record_once(self):
        # Fixture task 2 has a completed run record, so Inspect takes the run-report branch.
        self.assertEqual(self.nlh("task", "relate", "2", "--follow-up-of", "1")[0], 0)
        revision = self.board()[2]["board"]["revision"]
        self.assertEqual(self.call("PATCH", "/api/tasks/2", {"details": "Canonical follow-up details",
                                                              "expected_revision": revision})[0], 200)
        request = self.ask(2, "task_close")
        shot = self.page("#inspect/2", self.real(), [{"snapshot": "open"}])["open"]
        self.assertIn("Completed runs", shot["inspect"])  # the run report is still there
        for text in ("Follow-up of", "Task #1", "Canonical follow-up details", "History", "relate",
                     "approval request"):
            self.assertIn(text, shot["inspect"])
        self.assertEqual(shot["approvals"], [{"id": request["id"], "state": "pending",
                                              "buttons": ["Approve", "Request Changes", "Defer"]}])

    def test_request_changes_opens_a_required_field_and_sends_only_valid_feedback_on_submit(self):
        self.gated(44)
        release = self.ask(44)
        commit = self.ask(44, "commit")
        responses = self.real()
        responses["/api/tasks/44/approval/decide"] = {"body": self.call(
            "POST", "/api/tasks/44/approval/decide", self.decide_body(release, "request_changes", note=FEEDBACK))[1]}
        item = f'.approval[data-request="{release["id"]}"]'
        area = item + " .approval-feedback textarea"
        shots = self.page("#inspect/44", responses, [
            {"click": item + ' button[data-decision="request_changes"]'}, {"snapshot": "opened"},
            {"click": item + " .submit-changes"}, {"snapshot": "empty"},
            {"type": area, "text": "  \n  "}, {"click": item + " .submit-changes"}, {"snapshot": "blank"},
            {"type": area, "text": FEEDBACK},
            # The 3 s refresh redraws only once focus leaves the field; the draft survives it.
            {"eval": "document.activeElement.blur(); window.__app.loadInspect()"}, {"snapshot": "polled"},
            {"click": item + " .submit-changes"}, {"snapshot": "sent"}])
        panels = lambda shot: {p["id"]: p for p in shot["approvalPanels"]}  # noqa: E731
        opened = panels(shots["opened"])
        self.assertEqual(shots["opened"]["requests"], [])  # the first click decides nothing
        self.assertEqual((opened[release["id"]]["feedback"]["value"], opened[release["id"]]["feedback"]["required"],
                          opened[release["id"]]["feedback"]["maxLength"]), ("", True, 1000))
        self.assertIn("required", opened[release["id"]]["feedback"]["label"])
        self.assertIsNone(opened[commit["id"]]["feedback"])  # no feedback UI or draft on the other request
        for name in ("empty", "blank"):
            self.assertEqual(shots[name]["requests"], [])
            self.assertIn("Feedback is required", panels(shots[name])[release["id"]]["error"])
        polled = panels(shots["polled"])
        self.assertEqual(polled[release["id"]]["feedback"]["value"], FEEDBACK)
        self.assertIsNone(polled[commit["id"]]["feedback"])
        self.assertEqual(shots["sent"]["requests"], [{"path": "/api/tasks/44/approval/decide", "method": "POST",
                                                      "body": self.decide_body(release, "request_changes", note=FEEDBACK)}])
        result = shots["sent"]["approvalResult"]
        self.assertEqual(result["role"], "status")
        self.assertTrue(result["text"].startswith("Changes requested"), result)
        self.assertIn(f"Changes requested on workflow_complete for Task #44: {FEEDBACK}", result["text"])

        shots = self.page("#inspect/44", responses, [
            {"click": item + ' button[data-decision="request_changes"]'}, {"type": area, "text": "draft"},
            {"click": item + " .cancel-changes"}, {"snapshot": "cancelled"},
            {"click": item + ' button[data-decision="request_changes"]'}, {"snapshot": "reopened"}])
        self.assertEqual(shots["cancelled"]["requests"], [])
        self.assertIsNone(panels(shots["cancelled"])[release["id"]]["feedback"])
        self.assertEqual(panels(shots["reopened"])[release["id"]]["feedback"]["value"], "")  # cancel drops the draft

    def test_decided_requests_explain_themselves_on_reload_without_inventing_feedback(self):
        self.gated(45)
        changed = self.ask(45)
        self.call("POST", "/api/tasks/45/approval/decide", self.decide_body(changed, "request_changes", note=FEEDBACK))
        deferred = self.ask(45)
        self.call("POST", "/api/tasks/45/approval/decide", self.decide_body(deferred, "defer"))
        self.gated(46)
        legacy = self.ask(46)
        self.call("POST", "/api/tasks/46/approval/decide", self.decide_body(legacy, "request_changes", note="x"))
        doc = self.ticket(46)
        doc["approval_requests"][0]["decision"]["note"] = None  # an earlier decision made without feedback
        self.write(doc)
        self.write(graph_ticket(47, authorization={"granted": True}))
        self.run_graph(47, finish_release=False)
        commit = self.ask(47, "commit")
        self.call("POST", "/api/tasks/47/approval/decide", self.decide_body(commit))
        responses = self.real()
        shots = {n: self.page(f"#inspect/{n}", responses, [{"snapshot": "s"}])["s"] for n in (45, 46, 47)}
        for shot in shots.values():
            for panel in shot["approvalPanels"]:
                self.assertIsNone(panel["feedback"])  # no lingering feedback UI on decided requests
            self.assertTrue(all(a["buttons"] == [] for a in shot["approvals"]))
        five = {p["id"]: p["decided"] for p in shots[45]["approvalPanels"]}
        self.assertTrue(five[changed["id"]].startswith("Changes requested by"), five)
        self.assertIn("This request is decided, so it offers no buttons.", five[changed["id"]])
        self.assertIn(f"Feedback: {FEEDBACK}", five[changed["id"]])
        self.assertTrue(five[deferred["id"]].startswith("Deferred by"))
        old = shots[46]["approvalPanels"][0]["decided"]
        self.assertIn("No feedback was supplied with this earlier decision.", old)
        self.assertNotIn("Feedback:", old)
        approved = shots[47]["approvalPanels"][0]["decided"]
        self.assertTrue(approved.startswith("Approved by"))
        self.assertIn("Next: Authorized next action: the existing agent may now commit", approved)
        self.assertIn("merge and publish each need their own approval", approved)


if __name__ == "__main__":
    unittest.main()
