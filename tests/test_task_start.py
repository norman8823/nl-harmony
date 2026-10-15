"""Work identity survives turns, processes, and independent Codex sessions."""
import concurrent.futures
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from harmony import cli


class TaskStartTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name)
        patch = mock.patch.dict(os.environ)
        patch.start()
        self.addCleanup(patch.stop)
        for name in ("HARMONY_TASK_ID", "CODEX_THREAD_ID"):
            os.environ.pop(name, None)
        self.board = cli._store().Board(self.project / ".workflow")

    def command(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--project", str(self.project), "task", *args])
        return code, out.getvalue(), err.getvalue()

    def start(self, *args):
        code, out, err = self.command("start", *args, "--json")
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def test_turns_and_runs_reuse_identity_without_overwriting_task(self):
        os.environ["CODEX_THREAD_ID"] = "thread-1"
        first = self.start("--title", "Fix parser", "--details", "Accept escaped quotes")
        original = self.board.read_ticket(first["task_id"])
        second = self.start("--title", "Try another approach")
        third = self.start()
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["task_id"], third["task_id"])
        self.assertEqual(original, self.board.read_ticket(first["task_id"]))
        self.assertEqual(original["status"], "BACKLOG")
        self.assertFalse(original["authorization"]["granted"])

    def test_new_thread_can_continue_same_problem_and_then_resume_by_thread(self):
        os.environ["CODEX_THREAD_ID"] = "first-thread"
        first = self.start("--title", "One problem")
        os.environ["CODEX_THREAD_ID"] = "second-thread"
        second = self.start("--task-id", str(first["task_id"]), "--external-id", "second-thread")
        ticket = self.board.read_ticket(first["task_id"])
        self.assertFalse(second["created"])
        self.assertEqual(len(ticket["lifecycle"]["work_sources"]), 2)
        self.assertEqual(ticket["history"][-1]["action"], "work_link")
        self.assertEqual(self.start()["task_id"], first["task_id"])
        self.assertEqual(ticket, self.board.read_ticket(first["task_id"]))

    def test_inherited_task_id_reuses_existing_card(self):
        first = self.start("--title", "Original")
        os.environ["HARMONY_TASK_ID"] = str(first["task_id"])
        os.environ["CODEX_THREAD_ID"] = "later-thread"
        self.assertEqual(self.start()["task_id"], first["task_id"])
        self.assertEqual(len(self.board.ticket_ids()), 1)

    def test_linking_continuation_preserves_approval_binding_but_scope_changes_do_not(self):
        first = self.start("--title", "Same problem")
        task_id = first["task_id"]
        store = cli._store()
        ticket = self.board.read_ticket(task_id)
        binding = store.current_binding(ticket, ticket["board"]["revision"], None)
        self.start("--task-id", str(task_id), "--source", "codex", "--external-id", "new-thread")
        updated = self.board.read_ticket(task_id)
        self.assertTrue(store.binding_holds(binding, updated, updated["board"]["revision"], None))
        updated["scope"] = {"summary": "A different scope"}
        self.assertFalse(store.binding_holds(binding, updated, updated["board"]["revision"], None))

    def test_conflicting_task_and_thread_fail_without_writes(self):
        a = self.start("--title", "Problem A", "--external-id", "a")
        b = self.start("--title", "Problem B", "--external-id", "b")
        before = [self.board.read_ticket(i) for i in self.board.ticket_ids()]
        code, _, err = self.command("start", "--task-id", str(b["task_id"]), "--external-id", "a")
        self.assertEqual(code, 2)
        self.assertIn(f"Task #{a['task_id']}", err)
        self.assertEqual(before, [self.board.read_ticket(i) for i in self.board.ticket_ids()])

    def test_new_problem_in_same_thread_is_explicit_and_not_title_deduplicated(self):
        os.environ["CODEX_THREAD_ID"] = "thread"
        first = self.start("--title", "Fix tests")
        os.environ["HARMONY_TASK_ID"] = str(first["task_id"])
        second = self.start("--new", "--title", "Fix tests")
        self.assertNotEqual(first["task_id"], second["task_id"])
        self.assertTrue(second["created"])
        self.assertEqual(self.start("--task-id", str(second["task_id"]))["task_id"], second["task_id"])
        os.environ["HARMONY_TASK_ID"] = str(second["task_id"])
        self.assertEqual(self.start()["task_id"], second["task_id"])

    def test_unknown_task_or_missing_title_creates_nothing(self):
        for args in [("--task-id", "42"), (), ("--title", "   "),
                     ("--title", "Valid", "--external-id", "bad\nidentity")]:
            code, _, _ = self.command("start", *args)
            self.assertNotEqual(code, 0)
            self.assertEqual(self.board.ticket_ids(), [])

    def test_completed_archived_task_is_reused_without_reopening(self):
        first = self.start("--title", "Completed", "--external-id", "key")
        task_id = first["task_id"]
        closed = self.board.close(task_id, expected_revision=1, actor="human", now=cli._now(), note="Done")
        self.board.archive(task_id, expected_revision=closed["board"]["revision"], actor="human", now=cli._now())
        before = self.board.read_ticket(task_id)
        self.assertFalse(self.start("--external-id", "key")["created"])
        self.assertEqual(before, self.board.read_ticket(task_id))
        code, out, _ = self.command("list", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["tasks"], [])
        _, out, _ = self.command("list", "--all", "--json")
        self.assertEqual(json.loads(out)["tasks"][0]["task_id"], task_id)

    def test_parallel_processes_allocate_only_one_card(self):
        argv = [sys.executable, "-m", "harmony.cli", "--project", str(self.project), "task", "start",
                "--source", "codex", "--external-id", "shared-thread", "--title", "Concurrent", "--json"]
        def launch(_):
            result = subprocess.run(argv, capture_output=True, text=True, check=True)
            return json.loads(result.stdout)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(launch, range(8)))
        self.assertEqual(len({r["task_id"] for r in results}), 1)
        self.assertEqual(sum(r["created"] for r in results), 1)
        self.assertEqual(len(self.board.ticket_ids()), 1)

    def test_launcher_passes_task_id_and_preserves_failure_status(self):
        script = "import os,pathlib,sys; pathlib.Path('id.txt').write_text(os.environ['HARMONY_TASK_ID']); sys.exit(7)"
        code, _, err = self.command("start", "--title", "Terminal work", "--", sys.executable, "-c", script)
        self.assertEqual(code, 7, err)
        self.assertEqual((self.project / "id.txt").read_text(), str(self.board.ticket_ids()[0]))

    def test_custom_intake_cannot_be_bypassed(self):
        path = self.project / "agent-orchestrator" / "intake.py"
        path.parent.mkdir()
        path.write_text("raise RuntimeError('not called')")
        code, _, err = self.command("start", "--title", "New")
        self.assertEqual(code, 2)
        self.assertIn("intake allocator", err)
        self.assertEqual(self.board.ticket_ids(), [])

    def test_conflicting_stored_identity_and_corrupt_ticket_fail_closed(self):
        first = self.start("--title", "Original", "--external-id", "shared")
        duplicate = self.start("--title", "Other problem", "--new")
        path = self.board.tickets / f"{duplicate['task_id']}.json"
        ticket = self.board.read_ticket(duplicate["task_id"])
        ticket["lifecycle"]["work_sources"] = [{"source": "terminal", "external_id": "shared"}]
        path.write_text(json.dumps(ticket))
        code, _, err = self.command("start", "--title", "Must not create", "--external-id", "shared")
        self.assertEqual(code, 2)
        self.assertIn("multiple tasks", err)
        path.write_text("not json")
        code, _, err = self.command("start", "--title", "Must not create", "--external-id", "unknown")
        self.assertNotEqual(code, 0)
        self.assertIn("not readable JSON", err)
        self.assertEqual(self.board.ticket_ids(), [first["task_id"], duplicate["task_id"]])


if __name__ == "__main__":
    unittest.main()
