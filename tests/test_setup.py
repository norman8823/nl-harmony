"""Basic readiness and skill-install regression tests. No model CLI is launched."""
from pathlib import Path
import json
import tempfile
import threading
import unittest
import urllib.request
from unittest import mock

from harmony import setup


class SetupTests(unittest.TestCase):
    def test_report_distinguishes_installed_from_verified(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            with mock.patch.object(setup.shutil, "which", side_effect=lambda x: "/bin/" + x):
                with mock.patch.object(setup.codex_bridge, "is_installed", return_value=True):
                    info = setup.report(home)
            self.assertFalse(info["multi_agent_verified"])
            self.assertFalse(info["ready_for_assisted_setup"])
            self.assertEqual(info["checks"][2]["state"], "missing")
            self.assertEqual(info["checks"][3]["state"], "ready")

    def test_skill_install_idempotent_and_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            state, path = setup.install_setup_skill(home)
            self.assertEqual(state, "installed")
            self.assertTrue(path.is_symlink())
            self.assertEqual(setup.install_setup_skill(home)[0], "installed")
            path.unlink()
            path.mkdir()
            (path / "SKILL.md").write_text("custom skill")
            self.assertEqual(setup.install_setup_skill(home)[0], "conflict")
            self.assertEqual((path / "SKILL.md").read_text(), "custom skill")

    def test_doctor_does_not_claim_full_workflow(self):
        from harmony import cli
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            setup.install_setup_skill(home)
            with mock.patch.object(setup.shutil, "which", return_value="/usr/bin/tool"):
                with mock.patch.object(setup.codex_bridge, "is_installed", return_value=True):
                    with mock.patch.object(setup, "codex_home", return_value=home):
                        result = setup.report()
            self.assertTrue(result["ready_for_assisted_setup"])
            self.assertFalse(result["multi_agent_verified"])

    def test_companion_exposes_read_only_setup_checks(self):
        from harmony import cli
        server_module = cli._load_board_server()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            class NoQuota:
                def close(self):
                    pass
            httpd = server_module.build_server(root, root / ".workflow", port=0,
                                               session="test", actor="test", quota=NoQuota())
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{httpd.server_address[1]}/api/setup", timeout=5
                ) as response:
                    result = json.load(response)
                self.assertTrue(result["ok"])
                self.assertEqual([c["id"] for c in result["checks"]],
                                 ["codex", "connection", "skill", "claude"])
                self.assertFalse(result["multi_agent_verified"])
            finally:
                httpd.shutdown()
                thread.join(timeout=5)
                httpd.server_close()

    def test_unreadable_hook_remains_missing(self):
        with tempfile.TemporaryDirectory() as temp:
            with mock.patch.object(setup.codex_bridge, "is_installed", side_effect=ValueError("bad TOML")):
                info = setup.report(Path(temp))
            self.assertEqual(info["checks"][1]["state"], "missing")


if __name__ == "__main__":
    unittest.main()
