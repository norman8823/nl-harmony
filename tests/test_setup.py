"""Basic readiness and skill-install regression tests. No model CLI is launched."""
from pathlib import Path
import tempfile
import unittest
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

    def test_unreadable_hook_remains_missing(self):
        with tempfile.TemporaryDirectory() as temp:
            with mock.patch.object(setup.codex_bridge, "is_installed", side_effect=ValueError("bad TOML")):
                info = setup.report(Path(temp))
            self.assertEqual(info["checks"][1]["state"], "missing")


if __name__ == "__main__":
    unittest.main()
