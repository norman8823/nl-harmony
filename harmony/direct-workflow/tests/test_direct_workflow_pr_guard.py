#!/usr/bin/env python3
"""Offline tests for the direct-workflow PR-creation guard hook.

Fully offline: no network, no `gh`, no credentials. The hook is loaded from
its file path and exercised both through `decide()` and end to end through
the real stdin/exit-code contract Claude Code uses.

Run:  python3 direct-workflow/tests/test_direct_workflow_pr_guard.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = PACKAGE_DIR.parent
HOOK_PATH = PACKAGE_DIR / "direct-workflow-pr-guard.py"


def load_hook():
    spec = importlib.util.spec_from_file_location("pr_guard", HOOK_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    sys.dont_write_bytecode = True  # no __pycache__ beside the hook
    spec.loader.exec_module(module)
    return module


guard = load_hook()


class TestPlainGhPrCreateIsBlocked(unittest.TestCase):
    """Plain `gh pr create` fails preflight before it executes."""

    BLOCKED = [
        ("bare", "gh pr create"),
        ("with flags", 'gh pr create --title "x" --body "y" --base main'),
        ("fill", "gh pr create --fill"),
        ("web", "gh pr create --web"),
        ("absolute path", "/opt/homebrew/bin/gh pr create --fill"),
        ("other absolute path", "/usr/local/bin/gh pr create"),
        ("relative path", "./gh pr create"),
        ("env prefix", "GH_TOKEN=xxx gh pr create"),
        ("two env prefixes", "GH_TOKEN=xxx GH_HOST=github.com gh pr create"),
        ("env wrapper", "env GH_TOKEN=xxx gh pr create"),
        ("sudo wrapper", "sudo gh pr create"),
        ("global long flag", "gh --repo example-org/example-repo pr create --fill"),
        ("global short flag", "gh -R example-org/example-repo pr create"),
        ("and-chain", "cd example-repo && gh pr create --fill"),
        ("semicolon chain", "echo starting; gh pr create"),
        ("push then create", "git push -u origin feat && gh pr create --base main"),
        ("subshell", "(cd example-repo && gh pr create)"),
        ("command substitution", "$(which gh) pr create"),
        ("backtick substitution", "`which gh` pr create"),
        ("extra whitespace", "gh pr    create"),
        ("newline chain", "git push\ngh pr create --fill"),
        ("api post", "gh api -X POST /repos/example-org/example-repo/pulls -f title=x"),
        ("api post long", "gh api --method POST /repos/example-org/example-repo/pulls"),
        ("api post equals", "gh api --method=POST /repos/example-org/example-repo/pulls"),
        # F1: quoting a single word does not change the command the shell runs.
        ("quoted pr", 'gh "pr" create'),
        ("quoted create", "gh pr 'create' --fill"),
        ("quoted program", '"gh" pr create'),
        ("quoted subcommands with repo flag", "gh -R o/r 'pr' \"create\""),
        # F1: gh api sends POST implicitly once a field or body is given.
        ("api implicit -f", "gh api /repos/example-org/example-repo/pulls -f title=x -f head=b"),
        ("api implicit -F", "gh api repos/example-org/example-repo/pulls -F draft=true"),
        ("api implicit --field", "gh api /repos/o/r/pulls --field title=x"),
        ("api implicit --field=", "gh api /repos/o/r/pulls --field=title=x"),
        ("api implicit --raw-field", "gh api /repos/o/r/pulls --raw-field title=x"),
        ("api implicit --input", "gh api /repos/o/r/pulls --input body.json"),
        ("api placeholders", "gh api repos/{owner}/{repo}/pulls -f title=x"),
        ("api quoted endpoint", "gh api '/repos/o/r/pulls' -f title=x"),
        ("api -XPOST", "gh api -XPOST /repos/o/r/pulls"),
        ("api quoted POST", 'gh api -X "POST" /repos/o/r/pulls'),
        ("api lowercase post", "gh api --method post /repos/o/r/pulls"),
        # F1 (round 2): forms the first pass still let through.
        ("pr new alias", "gh pr new --fill"),
        ("quoted pr new alias", "gh -R o/r pr 'new' --title x"),
        ("api attached -f value", "gh api repos/o/r/pulls -fhead=b -fbase=main"),
        ("api attached -F value", "gh api repos/o/r/pulls -Ftitle=t"),
        ("api -f= value", "gh api repos/o/r/pulls -f=head=b"),
        ("api clustered -if", "gh api repos/o/r/pulls -if title=x"),
        ("api clustered -iXPOST", "gh api -iXPOST repos/o/r/pulls"),
        ("api clustered -iX POST", "gh api -iX POST repos/o/r/pulls"),
        ("api -X=POST", "gh api -X=POST repos/o/r/pulls"),
        ("api --method=POST", "gh api repos/o/r/pulls --method=POST"),
        ("api last method wins", "gh api repos/o/r/pulls -X GET -X POST -f a=b"),
        ("api absolute url", "gh api https://api.github.com/repos/o/r/pulls -f title=x"),
        ("api ghes url", "gh api https://ghe.example/api/v3/repos/o/r/pulls -f title=x"),
        ("api query string", "gh api 'repos/o/r/pulls?per_page=1' -f title=x"),
        ("api upper-case path", "gh api /REPOS/o/r/PULLS -f title=x"),
        ("api --input=", "gh api repos/o/r/pulls --input=body.json"),
    ]

    def test_blocked_forms(self):
        for label, command in self.BLOCKED:
            with self.subTest(label):
                blocked, reason = guard.decide(command)
                self.assertTrue(blocked, f"{label!r} should be blocked: {command!r}")
                self.assertTrue(reason, "a blocked call must carry a reason")


class TestHelpFlagIsNotABypass(unittest.TestCase):
    def test_help_forms_still_blocked(self):
        for command in (
            "gh pr create --help",
            "gh pr create -h",
            "gh pr create --help --title x",
            "GH_TOKEN=x gh pr create -h",
        ):
            with self.subTest(command):
                self.assertTrue(guard.decide(command)[0])


class TestReadOnlyGhRemainsAllowed(unittest.TestCase):
    ALLOWED = [
        "gh pr list",
        "gh pr list --state open --limit 20",
        "gh pr view 141",
        "gh pr view 141 --json author,headRefName",
        "gh pr diff 141",
        "gh pr checks 141",
        "gh pr status",
        "gh repo view",
        "gh issue list",
        "gh issue view 13",
        "gh issue create --title 'real bug'",
        "gh auth status",
        "gh api /repos/example-org/example-repo/pulls",
        "gh api -X GET /repos/example-org/example-repo/pulls",
        "gh api /repos/example-org/example-repo/pulls/141",
        # Fields with an explicit GET are a query string, not a creation.
        "gh api -X GET /repos/o/r/pulls -f state=open",
        "gh api --method GET repos/o/r/pulls -F per_page=100",
        # Fields against other endpoints are not PR creation.
        "gh api /repos/o/r/pulls/141/comments -f body=x",
        "gh api /repos/o/r/issues -f title=x",
        "gh api graphql -f query='{ viewer { login } }'",
        'gh pr view "141"',
        "gh 'pr' list",
        # Round 2: the stricter parsing must not swallow read-only calls.
        "gh api -XGET repos/o/r/pulls -fstate=open",
        "gh api repos/o/r/pulls -f state=open -X GET",
        "gh api -i repos/o/r/pulls",
        "gh api -H 'Accept: application/vnd.github+json' repos/o/r/pulls",
        "gh api https://api.github.com/repos/o/r/pulls",
        "gh api repos/o/r/pulls --jq '.[].number'",
        "gh api repos/o/r/pulls/141/reviews -fbody=x",
        "gh pr list --search new",
    ]

    def test_allowed_forms(self):
        for command in self.ALLOWED:
            with self.subTest(command):
                blocked, reason = guard.decide(command)
                self.assertFalse(blocked, f"read-only call blocked: {command!r} ({reason})")

    def test_create_as_quoted_text_is_not_a_command(self):
        for command in (
            "gh pr list --search 'pr create'",
            'gh pr list --search "create"',
            "git commit -m 'document gh pr create policy'",
            "echo 'gh pr create'",
            "grep -rn 'gh pr create' direct-workflow/",
        ):
            with self.subTest(command):
                self.assertFalse(guard.decide(command)[0])

    def test_unrelated_and_empty_commands_pass(self):
        for command in ("", "   ", "ls -la", "./scripts/test.sh", "git status"):
            with self.subTest(repr(command)):
                self.assertFalse(guard.decide(command)[0])

    def test_the_publisher_and_recorder_are_not_blocked(self):
        for command in (
            "python3 direct-workflow/worker_pr_publisher.py --preflight-only",
            "python3 direct-workflow/worker_pr_publisher.py --title x --head b "
            "--body-file /tmp/b.md --execute",
            "python3 direct-workflow/direct_run_recorder.py run --task-id 1 "
            "--role writer -- claude -p x --model claude-opus-5-5 "
            "--output-format stream-json --verbose",
        ):
            with self.subTest(command):
                self.assertFalse(guard.decide(command)[0])


class TestHookProcessContract(unittest.TestCase):
    def run_hook(self, payload: str):
        return subprocess.run(
            [sys.executable, str(HOOK_PATH)], input=payload, capture_output=True, text=True
        )

    def test_blocked_call_exits_2_and_explains(self):
        result = self.run_hook(json.dumps({"tool_input": {"command": "gh pr create --fill"}}))
        self.assertEqual(result.returncode, 2)
        self.assertIn("BLOCKED", result.stderr)
        self.assertIn("direct-workflow/worker_pr_publisher.py", result.stderr)
        self.assertIn("--preflight-only", result.stderr)

    def test_allowed_call_exits_0_silently(self):
        result = self.run_hook(json.dumps({"tool_input": {"command": "gh pr list"}}))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr.strip(), "")

    def test_malformed_envelope_allows_the_call(self):
        for payload in ("", "not json", "{}", json.dumps({"tool_input": {}})):
            with self.subTest(repr(payload)):
                self.assertEqual(self.run_hook(payload).returncode, 0)

    def test_self_check_mode(self):
        result = subprocess.run(
            [sys.executable, str(HOOK_PATH), "--check", "gh pr create"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertTrue(json.loads(result.stdout)["blocked"])


class TestReleaseCapableInvocation(unittest.TestCase):
    """The guard must be loaded by the session that can actually publish.

    A direct session launched with `--setting-sources ''` loads no project
    settings. A release-capable session additionally passes
    `--settings direct-workflow/release-settings.json`, which the CLI loads as
    additional settings regardless of `--setting-sources`.
    """

    SETTINGS = PACKAGE_DIR / "release-settings.json"
    LAUNCH_FLAG = "--settings direct-workflow/release-settings.json"
    DOCS = ("direct-workflow/README.md",)

    def setUp(self):
        self.settings = json.loads(self.SETTINGS.read_text())

    @staticmethod
    def bash_hook_commands(settings: dict) -> list[str]:
        return [
            hook["command"]
            for entry in settings.get("hooks", {}).get("PreToolUse", [])
            if entry.get("matcher") == "Bash"
            for hook in entry.get("hooks", [])
        ]

    @classmethod
    def guard_is_registered(cls, settings: dict) -> bool:
        return any(
            "direct-workflow-pr-guard.py" in command
            for command in cls.bash_hook_commands(settings)
        )

    def test_release_settings_register_the_guard(self):
        self.assertTrue(self.guard_is_registered(self.settings))

    def test_the_assertion_is_load_bearing(self):
        self.assertFalse(self.guard_is_registered({}))
        self.assertFalse(self.guard_is_registered({"hooks": {"PreToolUse": []}}))
        stripped = {
            "hooks": {
                "PreToolUse": [
                    {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo hi"}]}
                ]
            }
        }
        self.assertFalse(self.guard_is_registered(stripped))

    def test_release_settings_add_nothing_but_the_hook(self):
        self.assertEqual(set(self.settings), {"hooks"})
        self.assertEqual(set(self.settings["hooks"]), {"PreToolUse"})
        self.assertEqual(len(self.bash_hook_commands(self.settings)), 1)

    def _run_registered(self, command: str, env: dict) -> subprocess.CompletedProcess:
        return subprocess.run(
            self.bash_hook_commands(self.settings)[0],
            shell=True,
            input=json.dumps({"tool_input": {"command": command}}),
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            env={"PATH": os.environ.get("PATH", ""), **env},
        )

    def test_registered_command_resolves_and_blocks(self):
        for label, env in (
            ("CLAUDE_PROJECT_DIR set", {"CLAUDE_PROJECT_DIR": str(REPO_ROOT)}),
            ("CLAUDE_PROJECT_DIR unset", {}),
        ):
            with self.subTest(label):
                result = self._run_registered("gh pr create --fill", env)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("BLOCKED", result.stderr)

    def test_registered_command_still_allows_read_only_gh(self):
        result = self._run_registered(
            "gh pr list --state open", {"CLAUDE_PROJECT_DIR": str(REPO_ROOT)}
        )
        self.assertEqual(result.returncode, 0)

    @staticmethod
    def doc_text(relative: str) -> str:
        return " ".join((REPO_ROOT / relative).read_text().lower().split())

    def test_docs_require_the_release_launch_form(self):
        for relative in self.DOCS:
            with self.subTest(relative):
                self.assertIn(self.LAUNCH_FLAG, self.doc_text(relative))

    def test_docs_mark_the_unguarded_configuration_unsupported(self):
        for relative in self.DOCS:
            with self.subTest(relative):
                text = self.doc_text(relative)
                self.assertIn("--setting-sources", text)
                self.assertIn("not release-capable", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
