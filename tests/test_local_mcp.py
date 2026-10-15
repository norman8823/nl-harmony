"""Codex's stdio tools use Companion without writing in the terminal project."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

from harmony import cli, codex_bridge, local_mcp


class LocalToolsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        patch = mock.patch.object(codex_bridge, "registry_dir", return_value=self.root / f"harmony-{os.getuid()}" / "companions")
        patch.start()
        self.addCleanup(patch.stop)

    def companion(self, name="board"):
        root = self.root / name
        root.mkdir()
        project = cli.resolve_project(root, None)
        server = cli._load_board_server().build_server(root, project["workflow_dir"], port=0, session="test", actor="test")
        self.server = server
        registration = codex_bridge.advertise(server, project)
        record = registration.__enter__()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def stop():
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()
            registration.__exit__(None, None, None)
        self.addCleanup(stop)
        return record

    def test_tools_create_continue_and_list_one_card_through_companion(self):
        record = self.companion()
        first = local_mcp.call_tool("start_task", {"title": "Fix parser", "details": "Support escaped quotes",
                                                   "external_id": "problem-1"})
        self.assertTrue(first["created"])
        again = local_mcp.call_tool("start_task", {"title": "Different turn", "external_id": "problem-1"})
        self.assertFalse(again["created"])
        continued = local_mcp.call_tool("start_task", {"task_id": first["task_id"], "board_instance": record["instance"]})
        self.assertFalse(continued["created"])
        tasks = local_mcp.call_tool("list_tasks", {})["tasks"]
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["title"], "Fix parser")
        self.assertEqual(tasks[0]["details"], "Support escaped quotes")

    def test_stdio_protocol_from_read_only_unrelated_directory(self):
        self.companion()
        origin = self.root / "unrelated"
        origin.mkdir()
        origin.chmod(0o555)
        self.addCleanup(origin.chmod, 0o700)
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                "name": "start_task", "arguments": {"title": "Work from another repository", "external_id": "s:t"}}},
        ]
        result = subprocess.run([sys.executable, "-m", "harmony.local_mcp"], cwd=origin,
                                input="\n".join(map(json.dumps, messages)) + "\n", text=True, capture_output=True,
                                env={**os.environ, "TMPDIR": str(self.root)}, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        replies = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([r["id"] for r in replies], [1, 2, 3])
        self.assertEqual(replies[0]["result"]["protocolVersion"], "2025-03-26")
        self.assertIn("start_task", [t["name"] for t in replies[1]["result"]["tools"]])
        self.assertFalse(replies[2]["result"]["isError"])
        self.assertEqual(list(origin.iterdir()), [])
        self.assertEqual(len(local_mcp.call_tool("list_tasks", {})["tasks"]), 1)

    def test_progress_block_resume_and_complete_same_card(self):
        self.companion()
        card = local_mcp.call_tool("start_task", {"title": "Fix lifecycle"})
        task_id = card["task_id"]
        self.assertEqual(card["stage"], "Next")
        for stage in ("In Progress", "Blocked", "In Progress"):
            card = local_mcp.call_tool("update_task", {"task_id": task_id,
                "expected_revision": card["revision"], "stage": stage,
                "next_action": "Need user input" if stage == "Blocked" else "Finish implementation"})
            self.assertEqual(card["stage"], stage)
        card = local_mcp.call_tool("complete_task", {"task_id": task_id,
            "expected_revision": card["revision"], "summary": "Implemented and checked requested behavior"})
        self.assertEqual((card["status"], card["stage"]), ("DONE", "Done"))
        self.assertFalse(card["completion"]["verified"])
        self.assertEqual(card["completion"]["label"], "Completed by Codex")
        self.assertEqual(card["completion"]["reasons"], [])
        self.assertEqual(card["completion"]["detail"], "Implemented and checked requested behavior")
        self.assertEqual(len(local_mcp.call_tool("list_tasks", {})["tasks"]), 1)
        ticket = json.loads((self.root / "board/.workflow/tickets" / f"{task_id}.json").read_text())
        self.assertFalse(ticket["authorization"]["granted"])
        self.assertEqual(ticket["lifecycle"]["closure"]["kind"], "codex")
        self.assertEqual(ticket["history"][-1]["action"], "codex_complete")
        self.assertEqual(ticket["lifecycle"]["closure"]["note"], "Implemented and checked requested behavior")

    def test_legacy_codex_closure_label_corrected_without_rewriting_history(self):
        self.companion()
        card = local_mcp.call_tool("start_task", {"title": "Previously completed by Codex"})
        path = self.root / "board/.workflow/tickets" / f"{card['task_id']}.json"
        ticket = json.loads(path.read_text())
        ticket["status"] = "DONE"
        ticket["board"]["stage"] = "Done"
        ticket["lifecycle"]["closure"] = {"kind": "manual", "by": "codex-lifecycle",
            "label": "Manual closure: administrative only", "note": "Verified clean working tree"}
        path.write_text(json.dumps(ticket))
        before = path.read_bytes()
        completion = local_mcp.call_tool("list_tasks", {})["tasks"][0]["completion"]
        self.assertEqual(completion["label"], "Completed by Codex")
        self.assertEqual(completion["detail"], "Verified clean working tree")
        self.assertEqual(completion["reasons"], [])
        self.assertEqual(path.read_bytes(), before)

    def test_stale_or_invalid_updates_cannot_complete_or_overwrite(self):
        self.companion()
        card = local_mcp.call_tool("start_task", {"title": "Preserve concurrent edits"})
        base = {"task_id": card["task_id"], "expected_revision": card["revision"]}
        updated = local_mcp.call_tool("update_task", {**base, "stage": "Blocked", "next_action": "Wait for input"})
        for name, arguments in [("update_task", {**base, "stage": "In Progress"}),
                                ("complete_task", {**base, "summary": "Stale completion"}),
                                ("update_task", {**base, "stage": "Done"}),
                                ("complete_task", {"task_id": card["task_id"]})]:
            result = local_mcp.dispatch({"id": 1, "method": "tools/call",
                "params": {"name": name, "arguments": arguments}})
            self.assertTrue(result["result"]["isError"])
        stored = local_mcp.call_tool("list_tasks", {})["tasks"][0]
        self.assertEqual(stored["revision"], updated["revision"])
        self.assertEqual(stored["stage"], "Blocked")

    def test_declared_blockers_gates_and_graphs_refuse_completion_without_mutation(self):
        self.companion()
        for patch in ({"blocker": "Need credentials"}, {"testing_required": True},
                      {"graph": "required"}, {"approval_requests": [{"action": "task_close", "gate_id": "close", "state": "pending"}]}):
            with self.subTest(patch=patch):
                card = local_mcp.call_tool("start_task", {"title": "Respect declared gates"})
                path = self.root / "board/.workflow/tickets" / f"{card['task_id']}.json"
                ticket = json.loads(path.read_text())
                ticket.update(patch)
                path.write_text(json.dumps(ticket))
                before = path.read_bytes()
                with self.assertRaises(ValueError):
                    local_mcp.call_tool("complete_task", {"task_id": card["task_id"],
                        "expected_revision": card["revision"], "summary": "Cannot bypass the gate"})
                self.assertEqual(path.read_bytes(), before)

    def test_multiple_boards_require_selection(self):
        first, second = self.companion("first"), self.companion("second")
        with self.assertRaises(ValueError):
            local_mcp.call_tool("start_task", {"title": "Do not guess"})
        local_mcp.call_tool("start_task", {"title": "Selected board", "board_instance": second["instance"]})
        self.assertEqual(local_mcp.call_tool("list_tasks", {"board_instance": first["instance"]})["tasks"], [])

    def test_off_unknown_tools_and_invalid_fields_do_not_create_cards(self):
        self.assertEqual(local_mcp.call_tool("list_boards", {}), {"boards": []})
        result = local_mcp.dispatch({"id": 1, "method": "tools/call", "params": {
            "name": "start_task", "arguments": {"title": "No running board"}}})
        self.assertTrue(result["result"]["isError"])
        self.companion()
        for name, args in [("delete_everything", {}), ("start_task", {"title": "Bad input", "authorization": True}),
                           ("start_task", {"title": "Bad id", "task_id": "../../other"})]:
            result = local_mcp.dispatch({"id": 2, "method": "tools/call", "params": {"name": name, "arguments": args}})
            self.assertTrue(result["result"]["isError"])
        self.assertEqual(local_mcp.call_tool("list_tasks", {})["tasks"], [])

    def test_custom_allocator_is_not_bypassed(self):
        self.companion()
        intake = self.root / "board" / "agent-orchestrator" / "intake.py"
        intake.parent.mkdir()
        intake.write_text("raise RuntimeError('must not be called')")
        result = local_mcp.dispatch({"id": 1, "method": "tools/call", "params": {
            "name": "start_task", "arguments": {"title": "Use existing intake"}}})
        self.assertTrue(result["result"]["isError"])
        self.assertEqual(local_mcp.call_tool("list_tasks", {})["tasks"], [])

    def test_completion_errors_preserve_actual_http_error_and_gate(self):
        self.companion()
        card = local_mcp.call_tool("start_task", {"title": "Diagnose actual completion refusal"})
        arguments = {"task_id": card["task_id"], "expected_revision": card["revision"], "summary": "Work checked"}
        message = {"id": 1, "method": "tools/call", "params": {"name": "complete_task", "arguments": arguments}}
        handler = self.server.RequestHandlerClass
        with mock.patch.object(handler, "_lifecycle_action", lambda server, *args: server._error(404, "not found")):
            result = local_mcp.dispatch(message)["result"]
        self.assertTrue(result["isError"])
        self.assertIn("HTTP 404: not found", result["content"][0]["text"])
        path = self.root / "board/.workflow/tickets" / f"{card['task_id']}.json"
        ticket = json.loads(path.read_text())
        ticket["blocker"] = "Waiting for credentials"
        path.write_text(json.dumps(ticket))
        result = local_mcp.dispatch(message)["result"]
        self.assertTrue(result["isError"])
        self.assertIn("HTTP 409", result["content"][0]["text"])
        self.assertIn("ticket has a blocker", result["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
