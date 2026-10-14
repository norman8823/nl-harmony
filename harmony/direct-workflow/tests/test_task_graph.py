#!/usr/bin/env python3
"""Offline tests for task_graph.py on synthetic graphs, tickets and run records.

Run:  python3 direct-workflow/tests/test_task_graph.py
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(PACKAGE_DIR))

import task_checkpoint as tc  # noqa: E402
import task_graph as tg  # noqa: E402

TICKET = {
    "id": 7,
    "title": "Synthetic task",
    "status": "IMPLEMENTING",
    "updated_at": "2026-01-01T00:00:00Z",
    "scope": {"summary": "Do the synthetic thing"},
    "acceptance_criteria": ["It works"],
    "authorization": {"granted": False},
    "blocker": None,
    "evidence_refs": [],
}


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "repo"
        self.wf = self.root / ".workflow"
        (self.wf / "tickets").mkdir(parents=True)
        self.write_ticket(TICKET)
        self.cstore = tc.Store(self.root, None, None)
        self.gs = tg.GraphStore(self.cstore)

    def tearDown(self):
        self._tmp.cleanup()

    def write_ticket(self, ticket):
        (self.wf / "tickets" / "7.json").write_text(json.dumps(ticket))

    def init(self, **kw):
        kw.setdefault("correction_ceiling", 2)
        return self.gs.create("7", **kw)

    def go(self, action, node_id, **kw):
        return self.gs.transition("7", action, node_id, **kw)

    def state(self, node_id):
        return tg.node(self.gs.load("7"), node_id)["state"]

    def ready(self):
        return [n["id"] for n in tg.ready_nodes(self.gs.load("7"))]

    def run_node(self, node_id, run_id, **complete):
        self.go("start", node_id, run_ids=[run_id])
        return self.go("complete", node_id, **complete)

    def through_implement(self, **kw):
        self.init(**kw)
        for n in [n["id"] for n in self.gs.load("7")["nodes"] if n["type"] == "investigate"]:
            self.go("skip", n, reason="scope already known")
        self.run_node("implement", "7-writer-1")

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = tg.main([argv[0], "--task-id", "7", "--project-root", str(self.root), *argv[1:]])
        return code, out.getvalue(), err.getvalue()


class ShapeTests(Base):
    def test_fixed_shape_and_stable_ids(self):
        g = self.init(investigators=2, correction_ceiling=2)
        self.assertEqual([n["id"] for n in g["nodes"]],
                         ["investigate-1", "investigate-2", "implement", "review-1", "correct-1",
                          "review-2", "correct-2", "review-3", "release"])
        on_disk = json.loads(self.gs.path("7").read_text())
        self.assertEqual(on_disk, g)
        self.assertEqual(set(on_disk["nodes"][0]), set(tg.NODE_KEYS))
        self.assertEqual((on_disk["shape_version"], on_disk["config"]["max_concurrency"]), (2, 2))
        self.assertEqual(tg.node(g, "implement")["deps"], [])
        self.assertEqual(tg.node(g, "review-1")["deps"], ["implement", "investigate-1", "investigate-2"])
        self.assertEqual(tg.node(g, "correct-1")["role"], "correction")
        self.assertEqual(self.ready(), ["investigate-1", "investigate-2", "implement"])

    def test_no_investigators_and_no_release(self):
        g = self.init(investigators=0, correction_ceiling=0, release=False)
        self.assertEqual([n["id"] for n in g["nodes"]], ["implement", "review-1"])
        self.assertEqual(self.ready(), ["implement"])

    def test_init_refuses_relayout_and_bad_config(self):
        self.init()
        with self.assertRaises(tc.Refused):
            self.init()
        for bad in ({"correction_ceiling": -1}, {"correction_ceiling": 9}, {"correction_ceiling": 1, "max_concurrency": 0},
                    {"correction_ceiling": 1, "investigators": 99}):
            with self.assertRaises(tc.Refused):
                tg.new_graph("8", **bad)

    def test_ceiling_is_required_on_cli(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            tg.main(["init", "--task-id", "7", "--project-root", str(self.root)])

    def test_tampered_graph_refused(self):
        self.init()
        g = json.loads(self.gs.path("7").read_text())
        for mutate in (lambda d: d["nodes"][1].update(state="ready"),
                       lambda d: d["nodes"][1].update(deps=["release"]),
                       lambda d: d["nodes"].append(dict(d["nodes"][1])),
                       lambda d: d["nodes"][1].update(extra=1),
                       lambda d: d.update(task_id="8")):
            bad = json.loads(json.dumps(g))
            mutate(bad)
            self.gs.path("7").write_text(json.dumps(bad))
            with self.assertRaises(tc.Refused):
                self.gs.load("7")

    def tamper(self, mutate):
        """Each case edits a fresh copy of the valid graph on disk, then restores it."""
        original = self.gs.path("7").read_text()
        g = json.loads(original)
        mutate({n["id"]: n for n in g["nodes"]}, g)
        self.gs.path("7").write_text(json.dumps(g))
        with self.assertRaises(tc.Refused):
            self.gs.load("7")
        self.gs.path("7").write_text(original)
        self.gs.load("7")

    def test_topology_tampering_refused(self):  # F1
        self.init(investigators=1, correction_ceiling=1)
        self.tamper(lambda n, g: n["review-1"].update(deps=[]))  # review before implement
        self.tamper(lambda n, g: n["release"].update(deps=["review-1"]))  # release before the rounds
        self.tamper(lambda n, g: n["review-1"].update(deps=["implement"]))  # findings bypassed before review
        self.tamper(lambda n, g: n["implement"].update(deps=["investigate-1"]))  # a legacy edge on a v2 graph
        self.tamper(lambda n, g: g["nodes"].reverse())  # order
        self.tamper(lambda n, g: n["correct-1"].update(type="investigate", role="investigator"))
        self.tamper(lambda n, g: g["config"].update(correction_ceiling=2))  # ceiling raised by hand

    def test_shape_version_is_strict(self):
        self.init(investigators=1, correction_ceiling=1)
        for bad in (3, 0, True, "2", 2.0, None):
            self.tamper(lambda n, g, bad=bad: g.update(shape_version=bad))
        self.tamper(lambda n, g: g.update(shape_version=1))  # a v2 layout relabelled legacy
        self.tamper(lambda n, g: g.pop("shape_version"))  # ... or with the key dropped
        for bad in (3, True, "1"):
            with self.subTest(bad=bad), self.assertRaises(tc.Refused):
                tg.new_graph("7", correction_ceiling=1, shape_version=bad)

    def test_impossible_state_combinations_refused(self):  # F1
        self.init(investigators=0, correction_ceiling=1)
        self.tamper(lambda n, g: n["review-1"].update(state="running"))  # implement not done
        self.tamper(lambda n, g: (n["implement"].update(state="completed"),
                                  n["review-1"].update(state="completed", outcome="pass"),
                                  n["correct-1"].update(state="running")))  # correction after a pass
        self.tamper(lambda n, g: (n["implement"].update(state="completed"),
                                  n["review-1"].update(state="completed", outcome="fail", evidence_refs=["r.md"]),
                                  n["correct-1"].update(state="skipped"), n["review-2"].update(state="skipped"),
                                  n["release"].update(state="running")))  # release without a pass
        self.tamper(lambda n, g: (n["implement"].update(state="completed"),
                                  n["review-1"].update(state="completed", outcome="pass")))  # no evidence


class FlowTests(Base):
    def test_skipped_investigation_then_implement(self):  # legacy shape: implement waits for investigation
        self.init(investigators=1, shape_version=1)
        with self.assertRaises(tc.Refused):
            self.go("start", "implement")
        self.go("skip", "investigate-1", reason="scope fully specified in ticket")
        self.assertEqual(self.state("investigate-1"), "skipped")
        self.assertEqual(self.ready(), ["implement"])

    def test_only_investigate_and_release_skip_by_hand(self):
        self.init()
        for node_id in ("implement", "review-1", "correct-1"):
            with self.assertRaises(tc.Refused):
                self.go("skip", node_id, reason="no")

    def test_review_pass_skips_rounds_and_readies_release(self):
        self.through_implement()
        self.assertEqual(self.ready(), ["review-1"])
        self.run_node("review-1", "7-review-1", outcome="pass", evidence=["evidence/review-1.md"])
        g = self.gs.load("7")
        self.assertEqual([tg.node(g, i)["state"] for i in ("correct-1", "review-2", "correct-2", "review-3")],
                         ["skipped"] * 4)
        self.assertEqual(self.ready(), ["release"])
        self.assertIn("grants no authorization", tg.next_action(g))

    def test_review_fail_correction_rereview(self):
        self.through_implement()
        self.run_node("review-1", "7-review-1", outcome="fail", evidence=["evidence/review-1.md"])
        self.assertEqual(self.ready(), ["correct-1"])
        self.run_node("correct-1", "7-correction-1")
        self.assertEqual(self.ready(), ["review-2"])
        self.run_node("review-2", "7-review-2", outcome="pass", evidence=["evidence/review-2.md"])
        g = self.gs.load("7")
        self.assertEqual(tg.node(g, "correct-2")["state"], "skipped")
        self.assertEqual(self.ready(), ["release"])

    def test_bounded_correction_stops_and_blocks_release(self):
        self.through_implement(correction_ceiling=1)
        self.run_node("review-1", "r1", outcome="fail", evidence=["review-1.md"])
        self.run_node("correct-1", "c1")
        self.run_node("review-2", "r2", outcome="fail", evidence=["review-2.md"])
        g = self.gs.load("7")
        self.assertEqual(tg.node(g, "release")["state"], "blocked")
        self.assertIn("correction ceiling 1 reached", tg.node(g, "release")["reason"])
        self.assertEqual(self.ready(), [])
        self.assertTrue(tg.next_action(g).startswith("Correction ceiling (1) reached"))
        # Unblocking cannot release without a passing review.
        self.go("unblock", "release")
        self.assertEqual(self.ready(), [])
        with self.assertRaises(tc.Refused):
            self.go("start", "release")
        self.assertTrue(tg.next_action(self.gs.load("7")).startswith("Correction ceiling"))

    def test_review_needs_verdict_and_evidence(self):
        self.through_implement()
        self.go("start", "review-1", run_ids=["r1"])
        with self.assertRaises(tc.Refused):
            self.go("complete", "review-1")
        with self.assertRaises(tc.Refused):
            self.go("complete", "review-1", outcome="approved")
        self.assertEqual(self.state("review-1"), "running")  # refused changes are never written

    def test_review_verdict_needs_evidence_not_just_a_run(self):  # F2
        self.through_implement()
        self.go("start", "review-1", run_ids=["7-review-1"])
        for outcome in ("pass", "fail"):
            with self.assertRaises(tc.Refused):
                self.go("complete", "review-1", outcome=outcome, run_ids=["7-review-1b"])
        self.assertEqual(self.state("review-1"), "running")
        self.go("complete", "review-1", outcome="pass", evidence=["evidence/review-1.md"])

    def test_pass_skips_pre_blocked_later_rounds(self):  # F3
        self.through_implement(correction_ceiling=2)
        self.go("block", "correct-1", reason="pre-blocked")
        self.go("block", "review-2", reason="pre-blocked")
        self.run_node("review-1", "r1", outcome="pass", evidence=["review-1.md"])
        g = self.gs.load("7")
        self.assertEqual([tg.node(g, i)["state"] for i in ("correct-1", "review-2", "correct-2", "review-3")],
                         ["skipped"] * 4)
        self.assertEqual(tg.node(g, "correct-1")["reason"], "not needed: review-1 passed")
        self.assertEqual(self.ready(), ["release"])
        with self.assertRaises(tc.Refused):
            self.go("unblock", "correct-1")

    def test_writer_completion_needs_a_link(self):
        self.init(investigators=0)
        self.go("start", "implement")
        with self.assertRaises(tc.Refused):
            self.go("complete", "implement")
        self.go("complete", "implement", run_ids=["w1"])

    def test_release_block_and_resume(self):
        self.through_implement()
        self.run_node("review-1", "r1", outcome="pass", evidence=["review-1.md"])
        self.go("block", "release", reason="publisher preflight Blocked")
        g = self.gs.load("7")
        self.assertEqual(tg.summary(g)["blocked"], ["release: publisher preflight Blocked"])
        self.assertIn("Resolve block on release", tg.next_action(g))
        with self.assertRaises(tc.Refused):
            self.go("start", "release")
        self.go("unblock", "release")
        self.assertEqual(self.ready(), ["release"])
        self.run_node("release", "release-1", evidence=["https://example.invalid/pr/1"])
        self.assertEqual(tg.next_action(self.gs.load("7")), "Graph complete: release recorded; nothing further")

    def test_fail_and_restart(self):
        self.init(investigators=0)
        self.go("start", "implement", run_ids=["w1"])
        with self.assertRaises(tc.Refused):
            self.go("fail", "implement")
        self.go("fail", "implement", reason="CLI error")
        self.assertIn("Inspect failed implement", tg.next_action(self.gs.load("7")))
        self.go("start", "implement", run_ids=["w2"])
        n = tg.node(self.gs.load("7"), "implement")
        self.assertEqual((n["state"], n["run_ids"]), ("running", ["w1", "w2"]))

    def test_invalid_transitions(self):
        self.init(investigators=1)
        with self.assertRaises(tc.Refused):
            self.go("complete", "investigate-1")  # not running
        with self.assertRaises(tc.Refused):
            self.go("unblock", "investigate-1")  # not blocked
        with self.assertRaises(tc.Refused):
            self.go("start", "nope")
        with self.assertRaises(tc.Refused):
            self.go("link", "implement")
        self.assertEqual(self.gs.load("7")["revision"], 1)

    def test_ticket_blocker_wins(self):
        self.init(investigators=0)
        self.write_ticket(dict(TICKET, blocker="waiting on owner"))
        with self.assertRaises(tc.Refused):
            self.go("start", "implement")
        self.assertIn("Ticket blocker wins", self.gs.summary("7")["next_action"])


class ConcurrencyTests(Base):
    def test_legacy_default_is_one_at_a_time(self):
        self.init(investigators=2, shape_version=1)
        self.go("start", "investigate-1")
        with self.assertRaises(tc.Refused):
            self.go("start", "investigate-2")

    def test_legacy_investigators_overlap_within_cap(self):
        self.init(investigators=3, max_concurrency=2, shape_version=1)
        self.assertEqual([n["id"] for n in tg.startable(self.gs.load("7"))], ["investigate-1", "investigate-2"])
        self.go("start", "investigate-1")
        self.go("start", "investigate-2")
        with self.assertRaises(tc.Refused) as cm:
            self.go("start", "investigate-3")
        self.assertIn("max_concurrency 2", str(cm.exception))
        self.go("complete", "investigate-1")
        self.go("start", "investigate-3")
        self.assertEqual(self.ready(), [])

    def test_legacy_writer_and_reviewer_run_alone(self):
        g = tg.new_graph("7", correction_ceiling=1, investigators=1, max_concurrency=4, shape_version=1)
        tg.node(g, "investigate-1")["state"] = "running"
        self.assertIsNotNone(tg.concurrency_refusal(g, tg.node(g, "implement")))
        tg.node(g, "investigate-1")["state"] = "completed"
        tg.node(g, "implement")["state"] = "running"
        self.assertIsNotNone(tg.concurrency_refusal(g, tg.node(g, "review-1")))
        tg.node(g, "implement")["state"] = "completed"
        tg.node(g, "review-1")["state"] = "running"
        self.assertIsNotNone(tg.concurrency_refusal(g, tg.node(g, "correct-1")))
        tg.node(g, "implement")["state"] = "running"
        with self.assertRaises(tc.Refused):
            tg.validate(g, "7")

    def test_reviewer_run_must_differ_from_writer_run(self):
        self.through_implement()
        with self.assertRaises(tc.Refused):
            self.go("start", "review-1", run_ids=["7-writer-1"])
        self.assertEqual(self.state("review-1"), "pending")

    def test_parallel_writes_serialize(self):
        self.init(investigators=0)
        errors = []

        def link(i):
            try:
                self.go("link", "implement", evidence=[f"note-{i}.md"])
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=link, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        g = self.gs.load("7")
        self.assertEqual(errors, [])
        self.assertEqual(g["revision"], 9)
        self.assertEqual(len(tg.node(g, "implement")["evidence_refs"]), 8)


def _start(g, *node_ids):
    for node_id in node_ids:
        tg.apply(g, "start", node_id)
    return tg.validate(g, "7")


class ParallelInvestigatorTests(Base):
    """Shape version 2: the writer starts at once; review-1 waits for every investigator."""

    def ids(self, nodes):
        return [n["id"] for n in nodes]

    def test_implement_is_startable_immediately_and_first(self):
        self.init(investigators=2)
        g = self.gs.load("7")
        self.assertEqual(self.ids(tg.startable(g)), ["implement", "investigate-1"])  # default 2: writer + one
        self.go("start", "implement", run_ids=["7-writer-1"])
        self.assertEqual(self.ids(tg.startable(self.gs.load("7"))), ["investigate-1"])

    def test_writer_and_investigator_overlap_in_either_start_order(self):
        for order in (("implement", "investigate-1"), ("investigate-1", "implement")):
            with self.subTest(order=order):
                g = _start(tg.new_graph("7", correction_ceiling=1, investigators=2), *order)
                self.assertEqual(sorted(n["id"] for n in g["nodes"] if n["state"] == "running"),
                                 ["implement", "investigate-1"])
                refusal = tg.concurrency_refusal(g, tg.node(g, "investigate-2"))
                self.assertIn("max_concurrency 2 reached", refusal)
                with self.assertRaises(tc.Refused):
                    tg.apply(g, "start", "investigate-2")

    def test_writer_priority_does_not_depend_on_graph_order(self):
        self.init(investigators=1, max_concurrency=1)
        g = self.gs.load("7")
        self.assertEqual(self.ids(tg.startable(g)), ["implement"])
        self.assertIn("ready but held by the cap or the one-writer rule: investigate-1", tg.next_action(g))
        self.go("start", "investigate-1")  # an operator may still choose investigation first
        with self.assertRaises(tc.Refused) as cm:
            self.go("start", "implement")
        self.assertIn("max_concurrency 1", str(cm.exception))

    def test_several_investigators_share_the_total_cap_with_the_writer(self):
        self.init(investigators=3, max_concurrency=3)
        self.assertEqual(self.ids(tg.startable(self.gs.load("7"))), ["implement", "investigate-1", "investigate-2"])
        for node_id in ("investigate-1", "investigate-2", "implement"):
            self.go("start", node_id)
        with self.assertRaises(tc.Refused) as cm:
            self.go("start", "investigate-3")
        self.assertIn("max_concurrency 3", str(cm.exception))
        self.go("complete", "investigate-1", evidence=["inv/1.md"])
        self.go("start", "investigate-3")
        self.assertEqual(tg.summary(self.gs.load("7"))["running"], ["investigate-2", "investigate-3", "implement"])

    def test_non_investigators_stay_exclusive_whatever_the_cap(self):
        g = tg.new_graph("7", correction_ceiling=1, investigators=2, max_concurrency=8)
        others = [n for n in g["nodes"] if n["type"] != "investigate"]
        for running in others:
            for candidate in others:
                if candidate is running:
                    continue
                with self.subTest(running=running["id"], candidate=candidate["id"]):
                    running["state"] = "running"
                    self.assertIn("one non-investigator at a time", tg.concurrency_refusal(g, candidate))
                    self.assertIsNone(tg.concurrency_refusal(g, tg.node(g, "investigate-1")))
                    running["state"] = "pending"
        # Stored states are held to the same rules.
        bad = tg.new_graph("7", correction_ceiling=0, investigators=3, release=False)
        for node_id in ("investigate-1", "investigate-2", "investigate-3"):
            tg.node(bad, node_id)["state"] = "running"
        with self.assertRaises(tc.Refused) as cm:
            tg.validate(bad, "7")
        self.assertIn("exceeds max_concurrency 2", str(cm.exception))
        tg.node(bad, "investigate-3")["state"] = "pending"
        tg.validate(bad, "7")

    def test_review_waits_for_every_investigator_to_complete_or_skip(self):
        self.init(investigators=2)
        self.go("start", "investigate-1")
        self.run_node("implement", "7-writer-1")
        g = self.gs.load("7")
        self.assertNotIn("review-1", self.ready())
        self.assertIn("Review-1 waits for investigator findings from investigate-1 (running), "
                      "investigate-2 (pending)", tg.next_action(g))
        self.assertIn("1 of max_concurrency 2 running", tg.next_action(g))
        with self.assertRaises(tc.Refused) as cm:
            self.go("start", "review-1")
        self.assertIn("waiting on investigate-1, investigate-2", str(cm.exception))
        self.go("complete", "investigate-1", run_ids=["7-inv-1"], evidence=["inv/1.md"])
        self.assertNotIn("review-1", self.ready())
        self.go("skip", "investigate-2", reason="covered by investigate-1")
        self.assertEqual(self.ready(), ["review-1"])
        self.assertNotIn("waits for investigator", tg.next_action(self.gs.load("7")))

    def test_review_handoff_deduplicates_findings_and_keeps_them_apart_from_review_proof(self):
        self.init(investigators=2)
        self.go("start", "investigate-1", run_ids=["7-inv-1"])
        self.go("start", "investigate-2", run_ids=["7-inv-2"])
        self.go("complete", "investigate-1", evidence=["inv/1.md", "inv/shared.md"])
        self.go("complete", "investigate-2", run_ids=["7-inv-1"], evidence=["inv/shared.md"])
        self.run_node("implement", "7-writer-1")
        handoff = self.gs.summary("7")["review_handoff"]
        self.assertEqual(handoff["investigator_evidence_refs"], ["inv/1.md", "inv/shared.md"])
        self.assertEqual(handoff["investigator_run_ids"], ["7-inv-1", "7-inv-2"])
        self.assertEqual(handoff["investigators"], {"investigate-1": "completed", "investigate-2": "completed"})
        self.go("start", "review-1", run_ids=["7-review-1"])
        with self.assertRaises(tc.Refused) as cm:  # inherited findings are not proof the review happened
            self.go("complete", "review-1", outcome="pass", evidence=["inv/1.md"])
        self.assertIn("not proof of a review", str(cm.exception))
        self.assertEqual(self.state("review-1"), "running")
        self.go("complete", "review-1", outcome="pass", evidence=["inv/shared.md", "review/1.md"])
        g = self.gs.load("7")
        self.assertEqual(tg.own_review_refs(g, tg.node(g, "review-1")), ["review/1.md"])
        self.assertEqual(tg.summary(g)["review_handoff"]["investigator_evidence_refs"],
                         ["inv/1.md", "inv/shared.md"])  # the review's own ref is not a finding
        with self.assertRaises(tc.Refused):  # nor can it be relabelled as one afterwards
            self.go("link", "investigate-1", evidence=["review/1.md"])
        code, out, _ = self.cli("status")
        self.assertEqual(code, 0)
        self.assertIn("- review handoff (investigator findings, not the review's own evidence): "
                      "inv/1.md, inv/shared.md; runs 7-inv-1, 7-inv-2", out)
        text = tc.render_bootstrap(tc.bootstrap(self.cstore, "7"))
        self.assertIn("- review handoff, investigator findings (not the review's own evidence): "
                      "inv/1.md, inv/shared.md; runs 7-inv-1, 7-inv-2", text)
        # A stored review whose only evidence is inherited is refused on load.
        self.tamper_review_to_inherited()

    def tamper_review_to_inherited(self):
        original = self.gs.path("7").read_text()
        g = json.loads(original)
        next(n for n in g["nodes"] if n["id"] == "review-1")["evidence_refs"] = ["inv/1.md"]
        self.gs.path("7").write_text(json.dumps(g))
        with self.assertRaises(tc.Refused):
            self.gs.load("7")
        self.gs.path("7").write_text(original)

    def test_next_action_describes_room_and_overlap(self):
        self.init(investigators=1)
        first = tg.next_action(self.gs.load("7"))
        self.assertEqual(first, "Start implement (writer) as the only running writer; start read-only "
                                "investigator(s) investigate-1 alongside implement (0 of max_concurrency 2 "
                                "running; investigators may overlap any node, one non-investigator at a time)")
        self.go("start", "implement")
        self.assertTrue(tg.next_action(self.gs.load("7")).startswith(
            "Start read-only investigator(s) investigate-1 alongside implement (1 of max_concurrency 2 running"))
        self.go("start", "investigate-1")
        self.assertTrue(tg.next_action(self.gs.load("7")).startswith("Wait for investigate-1, implement"))


LEGACY_NODE = {"model": None, "run_ids": [], "evidence_refs": [], "outcome": None, "reason": None,
               "started_at": None, "completed_at": None, "state": "pending"}
# A graph exactly as the pre-version code serialized it: no shape_version, serial deps, cap 1.
LEGACY_GRAPH = {
    "kind": "task-graph/v1", "task_id": "7", "revision": 1,
    "created_at": "2026-09-01T00:00:00+00:00", "updated_at": "2026-09-01T00:00:00+00:00",
    "config": {"max_concurrency": 1, "correction_ceiling": 1},
    "nodes": [dict(LEGACY_NODE, id=i, type=t, role=r, deps=d) for i, t, r, d in (
        ("investigate-1", "investigate", "investigator", []),
        ("investigate-2", "investigate", "investigator", []),
        ("implement", "implement", "writer", ["investigate-1", "investigate-2"]),
        ("review-1", "review", "reviewer", ["implement"]),
        ("correct-1", "correct", "correction", ["review-1"]),
        ("review-2", "review", "reviewer", ["correct-1"]),
        ("release", "release", "release", ["review-1", "correct-1", "review-2"]))],
}


class LegacyShapeTests(Base):
    """Version 1 graphs load and finish under their old rules; nothing rewrites them."""

    def write(self, graph):
        self.gs.graph_dir.mkdir(parents=True, exist_ok=True)
        self.gs.path("7").write_text(json.dumps(graph))

    def test_version_1_layout_is_the_old_serialization(self):
        g = tg.new_graph("7", correction_ceiling=1, investigators=2, shape_version=1)
        self.assertNotIn("shape_version", g)
        strip = lambda d: {k: v for k, v in d.items() if k not in ("created_at", "updated_at")}  # noqa: E731
        self.assertEqual(strip(g), strip(LEGACY_GRAPH))

    def test_serialized_legacy_graph_finishes_under_old_rules(self):
        self.write(LEGACY_GRAPH)
        self.assertEqual(self.ready(), ["investigate-1", "investigate-2"])
        with self.assertRaises(tc.Refused):
            self.go("start", "implement")  # implement still waits for investigation
        self.go("start", "investigate-1")
        with self.assertRaises(tc.Refused):
            self.go("start", "investigate-2")  # stored cap 1 is kept, not the new default
        self.go("complete", "investigate-1", evidence=["inv/1.md"])
        self.go("skip", "investigate-2", reason="enough")
        self.go("start", "implement", run_ids=["7-writer-1"])
        self.go("complete", "implement")
        g = self.gs.load("7")
        self.assertEqual(tg.next_action(g), "Start review-1: independent reviewer in a fresh context, not the "
                                            "writer's session")
        s = tg.summary(g)
        self.assertNotIn("review_handoff", s)
        self.assertNotIn("shape_version", s)
        self.run_node("review-1", "7-review-1", outcome="pass", evidence=["inv/1.md"])  # old rule: any ref
        self.run_node("release", "7-release-1", evidence=["https://example.invalid/pr/1"])
        on_disk = json.loads(self.gs.path("7").read_text())
        self.assertNotIn("shape_version", on_disk)  # never silently rewritten to version 2
        self.assertEqual([n["deps"] for n in on_disk["nodes"]], [n["deps"] for n in LEGACY_GRAPH["nodes"]])
        self.assertEqual(on_disk["config"], LEGACY_GRAPH["config"])
        self.assertEqual(tg.next_action(tg.validate(on_disk, "7")), "Graph complete: release recorded; nothing further")
        code, out, _ = self.cli("status")
        self.assertIn("max concurrency 1 (investigators only)", out)
        self.assertNotIn("review handoff", out)

    def test_explicit_version_1_is_accepted_and_kept(self):
        self.write(dict(LEGACY_GRAPH, shape_version=1))
        self.go("start", "investigate-1")
        self.assertEqual(json.loads(self.gs.path("7").read_text())["shape_version"], 1)

    def test_legacy_layout_under_another_version_is_refused(self):
        for version in (2, 3, True):
            with self.subTest(version=version):
                self.write(dict(LEGACY_GRAPH, shape_version=version))
                with self.assertRaises(tc.Refused):
                    self.gs.load("7")


class ResumeAndCheckpointTests(Base):
    def test_session_reload_resumes_from_disk(self):
        self.through_implement()
        self.run_node("review-1", "r1", outcome="fail", evidence=["review-1.md"])
        # a fresh process: new store objects, nothing in memory
        fresh = tg.GraphStore(tc.Store(self.root, None, None))
        s = fresh.summary("7")
        self.assertEqual(s["last_completed"], "review-1 (fail)")
        self.assertEqual(s["ready"], ["correct-1"])
        self.assertEqual(s["next_action"], "Start correct-1 (correction) as the only running writer")
        code, out, _ = self.cli("next")
        self.assertEqual((code, out.strip()), (0, s["next_action"]))

    def test_run_and_evidence_links_by_reference(self):
        runs = self.root / ".direct-runs"
        runs.mkdir()
        (runs / "7-writer-1.json").write_text(json.dumps({"schema": "direct-run-record/v2", "run_id": "7-writer-1"}))
        (runs / "7-writer-2.json").write_text(json.dumps({"schema": "direct-run-record/v2", "status": "reserved"}))
        (self.root / "evidence").mkdir()
        (self.root / "evidence" / "INDEX.md").write_text("x")
        self.init(investigators=0)
        self.go("start", "implement", run_ids=["7-writer-1"], model="claude-opus-5-5")
        self.go("link", "implement", run_ids=["7-writer-2", "7-writer-3"], evidence=["evidence/INDEX.md", "gone.md"])
        n = tg.node(self.gs.load("7"), "implement")
        self.assertEqual(n["run_ids"], ["7-writer-1", "7-writer-2", "7-writer-3"])
        self.assertEqual(n["model"], "claude-opus-5-5")
        code, out, _ = self.cli("status")
        self.assertEqual(code, 0)
        for line in ("run 7-writer-1 [recorded]", "run 7-writer-2 [reserved]", "run 7-writer-3 [missing]",
                     "evidence [present] evidence/INDEX.md", "evidence [missing] gone.md"):
            self.assertIn(line, out)
        with self.assertRaises(tc.Refused):
            self.go("link", "implement", run_ids=["../escape"])

    def test_checkpoint_carries_compact_reference_only(self):
        self.through_implement()
        cp = tc.update(self.cstore, "7", {"phase": "review"}, no_ticket=False, reset=False)
        ref = cp["graph"]
        self.assertEqual(set(ref), {"path", "revision", "updated_at", "last_completed", "running", "ready",
                                    "blocked", "failed", "next_action", "shape_version", "review_handoff"})
        self.assertEqual(ref["ready"], ["review-1"])
        self.assertNotIn("nodes", json.dumps(cp))
        self.assertEqual(json.loads(tc.Store(self.root, None, None).checkpoint_path("7").read_text()), cp)

    def test_bootstrap_reads_graph_live_and_reports_drift(self):
        self.through_implement()
        tc.update(self.cstore, "7", {"phase": "review", "next_action": "stale"}, no_ticket=False, reset=False)
        self.run_node("review-1", "r1", outcome="pass", evidence=["review-1.md"])
        b = tc.bootstrap(self.cstore, "7")
        self.assertEqual(b["graph"]["ready"], ["release"])
        self.assertEqual(len(b["graph_drift"]), 1)
        text = tc.render_bootstrap(b)
        self.assertIn("- graph next action: Start release only after", text)
        self.assertIn("DRIFT (graph wins): checkpoint saw graph rev", text)
        idx = [text.index(h) for h in ("## 1.", "## 2.", "- STALE:", "- phase: review", "- next action: stale",
                                       "## 3.", "graph next action", "DRIFT (graph wins)", "## 4.")]
        self.assertEqual(idx, sorted(idx))
        self.assertTrue(b["context_stale"])

    def test_rewrite_refreshes_graph_ref_and_clears_stale(self):
        self.through_implement()
        tc.update(self.cstore, "7", {"phase": "review", "evidence_refs": ["review-1.md"]},
                  no_ticket=False, reset=False)
        self.run_node("review-1", "r1", outcome="pass", evidence=["review-1.md"])
        self.assertTrue(tc.bootstrap(self.cstore, "7")["context_stale"])
        err = io.StringIO()
        with contextlib.redirect_stderr(err):  # the ref is held by the graph node, so no provenance warning
            cp = tc.update(self.cstore, "7", {"phase": "release"}, no_ticket=False, reset=True)
        self.assertEqual(err.getvalue(), "")
        self.assertEqual(cp["graph"]["ready"], ["release"])
        b = tc.bootstrap(self.cstore, "7")
        self.assertEqual((b["graph_drift"], b["context_stale"]), ([], False))
        self.assertNotIn("STALE", tc.render_bootstrap(b))

    def test_no_graph_leaves_checkpoint_unchanged(self):
        cp = tc.update(self.cstore, "7", {"phase": "x"}, no_ticket=False, reset=False)
        self.assertNotIn("graph", cp)
        b = tc.bootstrap(self.cstore, "7")
        self.assertIsNone(b["graph"])
        self.assertNotIn("graph next action", tc.render_bootstrap(b))

    def test_external_graph_dir(self):
        other = Path(self._tmp.name) / "graphs-elsewhere"
        gs = tg.GraphStore(self.cstore, other)
        gs.create("7", correction_ceiling=1)
        self.assertTrue((other / "7.json").is_file())
        store = tc.Store(self.root, None, None, other)
        self.assertEqual(tc.bootstrap(store, "7")["graph"]["path"], str((other / "7.json").resolve()))

    def test_cli_round_trip(self):
        self.assertEqual(self.cli("init", "--correction-ceiling", "1", "--investigators", "0")[0], 0)
        self.assertEqual(self.cli("start", "--node", "implement", "--run-id", "w1")[0], 0)
        code, _, err = self.cli("start", "--node", "review-1")
        self.assertEqual(code, 2)
        self.assertIn("not ready", err)
        code, out, _ = self.cli("status", "--json")
        self.assertEqual(json.loads(out)["running"], ["implement"])


if __name__ == "__main__":
    unittest.main()
