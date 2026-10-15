"""A global prompt hook discovers the same live board from unrelated terminals."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
import urllib.request

from harmony import cli, codex_bridge


class CodexBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.registry = self.root / f"harmony-{os.getuid()}" / "companions"
        patch = mock.patch.object(codex_bridge, "registry_dir", return_value=self.registry)
        patch.start()
        self.addCleanup(patch.stop)
        self.payload = {"hook_event_name": "UserPromptSubmit", "session_id": "session-1",
                        "turn_id": "turn-1", "cwd": str(self.root / "other-project"),
                        "prompt": "Fix the parser and cover escaped quotes"}

    def companion(self, name="board"):
        root = self.root / name
        root.mkdir()
        project = cli.resolve_project(root, None)
        server = cli._load_board_server().build_server(root, project["workflow_dir"], port=0,
                                                      session="test", actor="test")
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
        return server, record

    def test_off_is_a_noop(self):
        self.assertEqual(codex_bridge.active_companions(), [])
        self.assertEqual(codex_bridge.prompt_context(self.payload, []), {})

    def test_different_terminal_receives_live_board_intake_and_creation_is_idempotent(self):
        server, record = self.companion()
        origin = Path(self.payload["cwd"])
        origin.mkdir()
        result = subprocess.run([sys.executable, "-m", "harmony.codex_bridge"], cwd=origin,
                                input=json.dumps(self.payload), capture_output=True, text=True, check=True,
                                env={**os.environ, "TMPDIR": str(self.root)})
        output = json.loads(result.stdout)
        context = output["hookSpecificOutput"]["additionalContext"]
        self.assertIn(record["project"], context)
        self.assertIn(str(origin), context)
        self.assertIn("session-1:turn-1", context)
        self.assertIn("not by itself a new task", context)
        self.assertFalse((origin / ".workflow").exists())
        # The agent performs intake after classifying new work, using the shared
        # board selected by the real hook, rather than a store in the terminal cwd.
        argv = [sys.executable, "-m", "harmony.cli", "--project", record["project"], "task", "start",
                "--new", "--source", "codex", "--external-id", "session-1:turn-1", "--title", "Fix parser",
                "--details", self.payload["prompt"], "--json"]
        results = [json.loads(subprocess.check_output(argv, cwd=origin, text=True)) for _ in range(2)]
        self.assertTrue(results[0]["created"])
        self.assertFalse(results[1]["created"])
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{record['port']}/api/board") as response:
            board = json.load(response)
        self.assertEqual(len(board["cards"]), 1)
        self.assertEqual(board["cards"][0]["id"], results[0]["task_id"])

    def test_registry_cleanup_and_stale_instance_are_not_active(self):
        _, record = self.companion()
        path = next(self.registry.glob("*.json"))
        stale = {**record, "instance": "old-instance"}
        path.write_text(json.dumps(stale))
        self.assertEqual(codex_bridge.active_companions(), [])

    def test_registration_is_removed_on_exit(self):
        root = self.root / "short-lived"
        root.mkdir()
        project = cli.resolve_project(root, None)
        server = cli._load_board_server().build_server(root, project["workflow_dir"], port=0,
                                                      session="test", actor="test")
        try:
            with codex_bridge.advertise(server, project):
                self.assertEqual(len(list(self.registry.glob("*.json"))), 1)
            self.assertEqual(list(self.registry.glob("*.json")), [])
        finally:
            server.server_close()

    def test_multiple_boards_require_selection_and_subagents_do_not_allocate(self):
        self.companion("first")
        self.companion("second")
        active = codex_bridge.active_companions()
        self.assertEqual(len(active), 2)
        context = codex_bridge.prompt_context(self.payload, active)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("ask which board", context)
        self.assertNotIn("task start", context)
        self.assertEqual(codex_bridge.prompt_context({**self.payload, "agent_id": "child"}, active), {})

    def test_missing_identity_does_not_invent_a_work_key(self):
        _, record = self.companion()
        self.payload.pop("turn_id")
        output = codex_bridge.prompt_context(self.payload, [record])
        self.assertIn("no card created", output["systemMessage"])

    def test_install_preserves_other_settings_and_hooks_and_is_repeatable(self):
        home = self.root / "codex"
        home.mkdir()
        path = home / "config.toml"
        original = 'model = "existing-model"\n[[hooks.UserPromptSubmit]]\nhooks = [{type="command", command="echo existing"}]\n'
        path.write_text(original)
        codex_bridge.install(home)
        first = path.read_text()
        self.assertTrue(codex_bridge.is_installed(home))
        self.assertTrue(first.startswith(original.rstrip()))
        self.assertIn("echo existing", first)
        self.assertEqual(len(list(home.glob("config.toml.harmony-backup-*"))), 1)
        codex_bridge.install(home)
        self.assertEqual(first, path.read_text())
        self.assertEqual(first.count(codex_bridge.BEGIN), 1)
        self.assertEqual(len(list(home.glob("config.toml.harmony-backup-*"))), 1)

    def test_disabled_hook_is_not_reported_connected(self):
        home = self.root / "codex"
        codex_bridge.install(home)
        path = home / "config.toml"
        path.write_text(path.read_text() + '\n[features]\nhooks = false\n')
        self.assertFalse(codex_bridge.is_installed(home))

    def test_reinstall_preserves_codex_saved_hook_trust_inside_managed_comments(self):
        home = self.root / "codex"
        path = codex_bridge.install(home)
        # Codex's TOML editor can insert its state beside the hook declaration,
        # inside the installer's comments. Those comments confer no ownership.
        saved = ('[hooks.state."/tmp/example-codex/config.toml:user_prompt_submit:0:0"]\n'
                 'trusted_hash = "user-approved-hook-hash"\nenabled = true\n\n')
        approved = path.read_text().replace("[mcp_servers.harmony_local]", saved + "[mcp_servers.harmony_local]")
        path.write_text(approved)
        before = path.stat().st_mtime_ns
        codex_bridge.install(home)
        self.assertEqual(path.read_text(), approved)
        self.assertEqual(path.stat().st_mtime_ns, before)

    def test_dead_registrations_are_cleaned(self):
        self.registry.mkdir(parents=True, mode=0o700)
        self.registry.parent.chmod(0o700)
        path = self.registry / 'dead.json'
        path.write_text(json.dumps({"pid": 12345, "port": 12345}))
        path.chmod(0o600)
        with mock.patch.object(codex_bridge.os, "kill", side_effect=ProcessLookupError):
            self.assertEqual(codex_bridge.active_companions(), [])
        self.assertFalse(path.exists())

    def test_invalid_or_disabled_config_remains_unchanged(self):
        home = self.root / "codex"
        home.mkdir()
        path = home / "config.toml"
        for original in ['broken = [', '[features]\nhooks = false\n', codex_bridge.BEGIN + '\n']:
            path.write_text(original)
            with self.assertRaises(ValueError):
                codex_bridge.install(home)
            self.assertEqual(path.read_text(), original)

    def test_shell_metacharacters_are_encoded_as_data_in_hook_context(self):
        _, record = self.companion()
        record["project"] = "/tmp/a 'quoted' $(untrusted) path"
        output = codex_bridge.prompt_context(self.payload, [record])
        context = output["hookSpecificOutput"]["additionalContext"]
        self.assertIn(json.dumps(record["project"]), context)
        self.assertNotIn("-m harmony.cli", context)


if __name__ == "__main__":
    unittest.main()
