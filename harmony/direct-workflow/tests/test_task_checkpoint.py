#!/usr/bin/env python3
"""Offline tests for task_checkpoint.py on synthetic tickets and checkpoints.

Run:  python3 direct-workflow/tests/test_task_checkpoint.py
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PACKAGE_DIR = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(PACKAGE_DIR))

import task_checkpoint as tc  # noqa: E402

TICKET = {
    "id": 7,
    "title": "Synthetic task",
    "status": "READY",
    "updated_at": "2026-01-01T00:00:00Z",
    "scope": {"summary": "Do the synthetic thing"},
    "acceptance_criteria": ["It works", "Tests pass"],
    "authorization": {"granted": False},
    "blocker": None,
    "evidence_refs": ["evidence/task7/INDEX.md", "Merged as abc123"],
}


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "repo"
        self.root.mkdir()
        self.wf = self.root / ".workflow"
        (self.wf / "tickets").mkdir(parents=True)
        self.write_ticket(TICKET)
        self.store = tc.Store(self.root, None, None)

    def tearDown(self):
        self._tmp.cleanup()

    def write_ticket(self, ticket, task_id="7"):
        (self.wf / "tickets" / f"{task_id}.json").write_text(json.dumps(ticket))

    def cli(self, *argv, stdin=""):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(sys, "stdin", io.StringIO(stdin)):
            code = tc.main([argv[0], "--project-root", str(self.root), *argv[1:]])
        return code, out.getvalue(), err.getvalue()


class SchemaTests(Base):
    def test_checkpoint_shape(self):
        cp = tc.update(self.store, "7", {"phase": "implementation", "next_action": "write tests",
                                         "review_findings": [{"id": "F1", "summary": "s", "resolution": "fixed"}]},
                       no_ticket=False, reset=False)
        on_disk = json.loads((self.wf / "checkpoints" / "7.json").read_text())
        self.assertEqual(on_disk, cp)
        self.assertEqual(cp["kind"], tc.KIND)
        self.assertEqual(set(cp), {"kind", "task_id", "updated_at", "ticket", "handoff"})
        self.assertEqual(set(cp["handoff"]), set(tc.HANDOFF_FIELDS))
        self.assertIsNone(cp["handoff"]["risks"])  # never given: unknown, not []

    def test_unknown_field_refused(self):
        with self.assertRaises(tc.Refused):
            tc.update(self.store, "7", {"transcript": "..."}, no_ticket=False, reset=False)

    def test_bad_finding_shape_refused(self):
        for bad in ([{"summary": "no id"}], [{"id": "F1", "extra": "x"}], ["F1"]):
            with self.assertRaises(tc.Refused):
                tc.update(self.store, "7", {"review_findings": bad}, no_ticket=False, reset=False)

    def test_template_validates(self):
        tc.validate_handoff(tc.template(), ticket_owned=True)

    def test_invalid_task_id(self):
        code, _, err = self.cli("show", "--task-id", "../x")
        self.assertEqual(code, 2)
        self.assertIn("invalid task id", err)


class BoundednessTests(Base):
    def refused(self, data):
        with self.assertRaises(tc.Refused):
            tc.update(self.store, "7", data, no_ticket=False, reset=False)
        self.assertFalse((self.wf / "checkpoints" / "7.json").exists())

    def test_long_string(self):
        self.refused({"next_action": "x" * (tc.MAX_STRING + 1)})

    def test_too_many_lines(self):
        self.refused({"decisions": ["a\nb\nc\nd\ne"]})

    def test_too_many_items(self):
        self.refused({"changes": ["c"] * (tc.MAX_ITEMS + 1)})

    def test_diff_and_log_dumps(self):
        self.refused({"changes": ["diff --git a/x b/x"]})
        self.refused({"risks": ["see\n@@ -1 +1 @@"]})
        self.refused({"risks": ["Traceback (most recent call last)"]})

    def test_total_size_cap(self):
        many = [("y" * 500) for _ in range(tc.MAX_ITEMS)]
        self.refused({k: many for k in ("invariants", "decisions", "changes")})


class TicketAuthorityTests(Base):
    def test_ticket_fields_copied(self):
        cp = tc.update(self.store, "7", {}, no_ticket=False, reset=False)
        t = cp["ticket"]
        self.assertTrue(t["authoritative"])
        self.assertEqual((t["title"], t["status"], t["goal"]), ("Synthetic task", "READY", "Do the synthetic thing"))
        self.assertEqual(t["acceptance_criteria"], ["It works", "Tests pass"])
        self.assertIs(t["authorization_granted"], False)

    def test_handoff_cannot_set_ticket_fields(self):
        for key, value in (("status", "ACCEPTED"), ("acceptance_criteria", []), ("goal", "other"),
                           ("authorization", {"granted": True}), ("title", "x"), ("blocker", None)):
            with self.subTest(key=key), self.assertRaises(tc.Refused):
                tc.update(self.store, "7", {key: value}, no_ticket=False, reset=False)

    def test_task_id_mismatch_refused(self):
        with self.assertRaises(tc.Refused):
            tc.update(self.store, "7", {"task_id": "8"}, no_ticket=False, reset=False)
        self.write_ticket(dict(TICKET, id=8), task_id="9")
        with self.assertRaises(tc.Refused):
            tc.update(self.store, "9", {}, no_ticket=False, reset=False)

    def test_missing_ticket_needs_explicit_opt_out(self):
        with self.assertRaises(tc.Refused):
            tc.update(self.store, "99", {}, no_ticket=False, reset=False)
        with self.assertRaises(tc.Refused):
            tc.update(self.store, "7", {}, no_ticket=True, reset=False)
        cp = tc.update(self.store, "99", {"goal": "g", "status": "doing", "acceptance_criteria": ["a"]},
                       no_ticket=True, reset=False)
        self.assertFalse(cp["ticket"]["authoritative"])
        self.assertEqual(cp["ticket"]["status"], "doing")
        with self.assertRaises(tc.Refused):
            tc.update(self.store, "99", {"authorization": {"granted": True}}, no_ticket=True, reset=False)

    def test_bootstrap_reports_drift_and_ticket_wins(self):
        tc.update(self.store, "7", {"phase": "review"}, no_ticket=False, reset=False)
        self.write_ticket(dict(TICKET, status="BLOCKED", updated_at="2026-02-01T00:00:00Z"))
        b = tc.bootstrap(self.store, "7")
        self.assertEqual(b["ticket"]["status"], "BLOCKED")
        self.assertTrue(any(d.startswith("status:") for d in b["ticket_drift"]))
        self.assertIn("DRIFT (ticket wins): status", tc.render_bootstrap(b))

    def test_bootstrap_flags_vanished_ticket(self):
        tc.update(self.store, "7", {}, no_ticket=False, reset=False)
        (self.wf / "tickets" / "7.json").unlink()
        b = tc.bootstrap(self.store, "7")
        self.assertEqual(b["ticket"]["status"], "READY")
        self.assertIn("stale snapshot", b["ticket_drift"][0])


class ReplacementTests(Base):
    def test_replace_not_append(self):
        tc.update(self.store, "7", {"decisions": ["a", "b"], "phase": "p1"}, no_ticket=False, reset=False)
        cp = tc.update(self.store, "7", {"decisions": ["c"]}, no_ticket=False, reset=False)
        self.assertEqual(cp["handoff"]["decisions"], ["c"])
        self.assertEqual(cp["handoff"]["phase"], "p1")  # omitted field carries over
        cp = tc.update(self.store, "7", {"next_action": "n"}, no_ticket=False, reset=True)
        self.assertIsNone(cp["handoff"]["phase"])
        self.assertEqual(sorted(p.name for p in (self.wf / "checkpoints").iterdir()), ["7.json"])

    def test_failed_write_keeps_old_file(self):
        tc.update(self.store, "7", {"phase": "old"}, no_ticket=False, reset=False)
        path = self.wf / "checkpoints" / "7.json"
        before = path.read_text()
        with mock.patch.object(tc.os, "replace", side_effect=OSError("boom")), self.assertRaises(OSError):
            tc.update(self.store, "7", {"phase": "new"}, no_ticket=False, reset=False)
        self.assertEqual(path.read_text(), before)
        self.assertEqual([p.name for p in path.parent.iterdir()], ["7.json"])  # temp file cleaned up

    def test_refused_update_keeps_old_file(self):
        tc.update(self.store, "7", {"phase": "old"}, no_ticket=False, reset=False)
        path = self.wf / "checkpoints" / "7.json"
        before = path.read_text()
        with self.assertRaises(tc.Refused):
            tc.update(self.store, "7", {"status": "DONE"}, no_ticket=False, reset=False)
        self.assertEqual(path.read_text(), before)


class BootstrapTests(Base):
    def test_order_and_instruction_files(self):
        (self.root / "AGENTS.md").write_text("a")
        (self.root / "CLAUDE.md").write_text("c")
        tc.update(self.store, "7", {"phase": "implementation", "next_action": "run tests"},
                  no_ticket=False, reset=False)
        text = tc.render_bootstrap(tc.bootstrap(self.store, "7"))
        idx = [text.index(h) for h in ("## 1.", "AGENTS.md", "CLAUDE.md", "## 2.", "## 3.", "## 4.")]
        self.assertEqual(idx, sorted(idx))
        self.assertIn("- phase: implementation", text)
        self.assertIn("- next action: run tests", text)
        self.assertIn("does not limit further reading", text)

    def test_only_present_instruction_files(self):
        (self.root / "CLAUDE.md").write_text("c")
        b = tc.bootstrap(self.store, "7")
        self.assertEqual([Path(p).name for p in b["instructions"]], ["CLAUDE.md"])

    def test_no_checkpoint_still_bootstraps_from_ticket(self):
        b = tc.bootstrap(self.store, "7")
        self.assertIsNone(b["handoff"])
        self.assertIn("no checkpoint", tc.render_bootstrap(b))
        self.assertEqual(b["ticket"]["status"], "READY")

    def test_blocker_none_vs_unknown(self):
        self.assertIn("- blocker: none\n", tc.render_bootstrap(tc.bootstrap(self.store, "7")))
        self.write_ticket(dict(TICKET, blocker="waiting on review"))
        self.assertIn("- blocker: waiting on review\n", tc.render_bootstrap(tc.bootstrap(self.store, "7")))
        tc.update(self.store, "99", {"goal": "g"}, no_ticket=True, reset=False)
        self.assertIn("- blocker: unknown\n", tc.render_bootstrap(tc.bootstrap(self.store, "99")))

    def test_narrow_grant_is_not_blanket_authority(self):
        self.write_ticket(dict(TICKET, authorization={"granted": True, "granted_by": "owner",
                                                      "permissions": ["local tests only"],
                                                      "notes": "no deploy, no publish"}))
        text = tc.render_bootstrap(tc.bootstrap(self.store, "7"))
        self.assertIn("- authorization_granted: True\n", text)
        ticket_path = str(self.store.ticket_path("7"))
        self.assertIn(f"- authorization scope: not shown; read the full `authorization` in {ticket_path} "
                      "before any privileged action (a grant covers only what it names)", text)
        self.assertLess(text.index("authorization_granted"), text.index("authorization scope"))
        tc.update(self.store, "99", {"goal": "g"}, no_ticket=True, reset=False)
        text = tc.render_bootstrap(tc.bootstrap(self.store, "99"))
        self.assertIn("- authorization scope: none recorded; obtain explicit approval", text)

    def test_nothing_at_all(self):
        text = tc.render_bootstrap(tc.bootstrap(self.store, "404"))
        self.assertIn("no ticket and no checkpoint", text)

    def test_cli_roundtrip_and_external_store(self):
        fresh = Path(self._tmp.name) / "fresh-worktree"
        fresh.mkdir()
        (fresh / "AGENTS.md").write_text("a")
        with contextlib.redirect_stdout(io.StringIO()):
            code = tc.main(["update", "--task-id", "7", "--project-root", str(fresh),
                            "--workflow-dir", str(self.wf), "--phase", "review"])
        self.assertEqual(code, 0)
        self.assertTrue((self.wf / "checkpoints" / "7.json").is_file())
        with mock.patch.dict(os.environ, {"TASK_WORKFLOW_DIR": str(self.wf)}):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertEqual(tc.main(["bootstrap", "--task-id", "7", "--project-root", str(fresh), "--json"]), 0)
        b = json.loads(buf.getvalue())
        self.assertEqual(b["handoff"]["phase"], "review")
        self.assertEqual([Path(p).name for p in b["instructions"]], ["AGENTS.md"])

    def test_checkpoint_dir_override(self):
        other = Path(self._tmp.name) / "cps"
        with mock.patch.dict(os.environ, {"TASK_CHECKPOINT_DIR": str(other)}):
            code, _, _ = self.cli("update", "--task-id", "7", "--next-action", "x")
        self.assertEqual(code, 0)
        self.assertTrue((other / "7.json").is_file())

    def test_cli_from_stdin_and_refusal_exit(self):
        code, _, _ = self.cli("update", "--task-id", "7", "--from", "-", stdin=json.dumps({"risks": ["r"]}))
        self.assertEqual(code, 0)
        code, _, err = self.cli("update", "--task-id", "7", "--from", "-", stdin=json.dumps({"status": "DONE"}))
        self.assertEqual(code, 2)
        self.assertIn("owned by the ticket", err)
        code, out, _ = self.cli("show", "--task-id", "7")
        self.assertEqual(json.loads(out)["handoff"]["risks"], ["r"])


class LiveContextTests(Base):
    def rewrite(self, ctx, *extra):
        return self.cli("rewrite", "--task-id", "7", "--from", "-", *extra, stdin=json.dumps(ctx))

    def test_whole_context_rewrite_drops_stale_details(self):
        tc.update(self.store, "7", {"plan": ["old step"], "phase": "p1", "decisions": ["stale"],
                                    "open_questions": ["q"], "next_action": "old"}, no_ticket=False, reset=False)
        code, _, err = self.rewrite({"plan": ["new step"], "next_action": "new"})
        self.assertEqual((code, err), (0, ""))
        h = json.loads((self.wf / "checkpoints" / "7.json").read_text())["handoff"]
        self.assertEqual((h["plan"], h["next_action"]), (["new step"], "new"))
        self.assertEqual((h["phase"], h["decisions"], h["open_questions"]), (None, None, None))
        self.assertEqual(sorted(p.name for p in (self.wf / "checkpoints").iterdir()), ["7.json"])

    def test_rewrite_refuses_dropping_sole_evidence_pointer(self):
        tc.update(self.store, "7", {"evidence_refs": ["evidence/run-3.json", "evidence/task7/INDEX.md"]},
                  no_ticket=False, reset=False)
        path = self.wf / "checkpoints" / "7.json"
        before = path.read_text()
        code, _, err = self.rewrite({"evidence_refs": ["evidence/index.md"]})
        self.assertEqual(code, 2)
        self.assertIn("evidence/run-3.json", err)
        self.assertNotIn("INDEX.md", err)  # still held by the ticket
        self.assertEqual(path.read_text(), before)
        self.write_ticket(dict(TICKET, evidence_refs=TICKET["evidence_refs"] + ["evidence/run-3.json"]))
        code, _, err = self.rewrite({"evidence_refs": ["evidence/index.md"]})
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(path.read_text())["handoff"]["evidence_refs"], ["evidence/index.md"])

    def test_field_update_cannot_drop_sole_evidence_pointer(self):
        tc.update(self.store, "7", {"evidence_refs": ["evidence/only.json"]},
                  no_ticket=False, reset=False)
        path = self.wf / "checkpoints" / "7.json"
        before = path.read_text()
        code, _, err = self.cli("update", "--task-id", "7", "--from", "-",
                                stdin=json.dumps({"evidence_refs": []}))
        self.assertEqual(code, 2)
        self.assertIn("evidence/only.json", err)
        self.assertEqual(path.read_text(), before)

    def test_rewrite_is_bounded_and_atomic(self):
        tc.update(self.store, "7", {"phase": "old"}, no_ticket=False, reset=False)
        before = (self.wf / "checkpoints" / "7.json").read_text()
        for bad in ({"changes": ["diff --git a/x b/x"]}, {"status": "DONE"}, {"transcript": "..."},
                    {"plan": ["p"] * (tc.MAX_ITEMS + 1)}):
            code, _, _ = self.rewrite(bad)
            self.assertEqual(code, 2)
        self.assertEqual((self.wf / "checkpoints" / "7.json").read_text(), before)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            tc.main(["rewrite", "--task-id", "7"])  # --from is required: no accidental wipe

    def test_old_checkpoint_without_plan_still_works(self):
        tc.update(self.store, "7", {"phase": "impl", "decisions": ["d"]}, no_ticket=False, reset=False)
        path = self.wf / "checkpoints" / "7.json"
        old = json.loads(path.read_text())
        del old["handoff"]["plan"]
        path.write_text(json.dumps(old))
        self.assertIn("- plan: unknown\n", tc.render_bootstrap(tc.bootstrap(self.store, "7")))
        cp = tc.update(self.store, "7", {"next_action": "n"}, no_ticket=False, reset=False)
        self.assertEqual((cp["handoff"]["plan"], cp["handoff"]["decisions"]), (None, ["d"]))

    def test_bootstrap_order_context_before_metadata(self):
        (self.root / "AGENTS.md").write_text("a")
        tc.update(self.store, "7", {"plan": ["step A"], "open_questions": ["why X"], "next_action": "do A",
                                    "evidence_refs": ["evidence/e.json"]}, no_ticket=False, reset=False)
        text = tc.render_bootstrap(tc.bootstrap(self.store, "7"))
        order = ("## 1.", "AGENTS.md", "## 2.", "- goal (ticket): Do the synthetic thing", "  - It works",
                 "  - step A", "unresolved issues / open questions:", "- next action: do A",
                 "## 3.", "- status: READY", "- blocker: none", "## 4.", "evidence/e.json")
        idx = [text.index(h) for h in order]
        self.assertEqual(idx, sorted(idx))
        self.assertNotIn("STALE", text)

    def test_ticket_drift_marks_context_stale(self):
        tc.update(self.store, "7", {"phase": "review"}, no_ticket=False, reset=False)
        self.write_ticket(dict(TICKET, status="BLOCKED", acceptance_criteria=["New criterion"]))
        b = tc.bootstrap(self.store, "7")
        self.assertTrue(b["context_stale"])
        text = tc.render_bootstrap(b)
        self.assertLess(text.index("- STALE:"), text.index("  - New criterion"))  # live acceptance shown
        self.assertLess(text.index("## 3."), text.index("DRIFT (ticket wins): status"))

    def test_no_ticket_context_is_marked_not_authoritative(self):
        tc.update(self.store, "99", {"goal": "g", "acceptance_criteria": ["a"]}, no_ticket=True, reset=False)
        code, _, _ = self.cli("rewrite", "--task-id", "99", "--no-ticket", "--from", "-",
                              stdin=json.dumps({"goal": "g2", "plan": ["p"]}))
        self.assertEqual(code, 0)
        text = tc.render_bootstrap(tc.bootstrap(self.store, "99"))
        self.assertIn("- goal (checkpoint-only, not authoritative): g2", text)
        self.assertIn("- ticket: none (checkpoint-only, not authoritative)", text)
        self.assertNotIn("STALE", text)


class EvidenceTests(Base):
    def test_present_missing_not_a_path(self):
        (self.root / "evidence" / "task7").mkdir(parents=True)
        (self.root / "evidence" / "task7" / "INDEX.md").write_text("x")
        tc.update(self.store, "7", {"evidence_refs": ["evidence/gone.json", "reviewer round 1 PASS",
                                                      "evidence/task7/INDEX.md"]},
                  no_ticket=False, reset=False)
        b = tc.bootstrap(self.store, "7")
        status = {e["ref"]: e["status"] for e in b["evidence"]}
        self.assertEqual(status, {"evidence/task7/INDEX.md": "present", "Merged as abc123": "not-a-path",
                                  "evidence/gone.json": "missing", "reviewer round 1 PASS": "not-a-path"})
        self.assertEqual(len(b["evidence"]), 3 + 1)  # ticket and handoff refs deduplicated

    def test_resolves_against_external_store_root(self):
        orig = Path(self._tmp.name) / "orig"
        (orig / ".workflow" / "evidence").mkdir(parents=True)
        (orig / ".workflow" / "evidence" / "e.json").write_text("{}")
        store = tc.Store(self.root, orig / ".workflow", None)
        self.assertEqual(tc.evidence_status(store, ".workflow/evidence/e.json")["status"], "present")
        self.assertEqual(tc.evidence_status(store, str(orig / "nope.json"))["status"], "missing")

    def test_unreadable_checkpoint_refused(self):
        (self.wf / "checkpoints").mkdir()
        (self.wf / "checkpoints" / "7.json").write_text("{not json")
        code, _, err = self.cli("bootstrap", "--task-id", "7")
        self.assertEqual(code, 2)
        self.assertIn("unreadable JSON", err)


if __name__ == "__main__":
    unittest.main()
