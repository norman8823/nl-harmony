"""`nlh` end to end in a throwaway, unrelated project."""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harmony import cli  # noqa: E402

QUEUE = [{"id": "demo-1", "summary": "demo finding", "priority": 1, "result_state": "INVESTIGATED_NO_CHANGE",
          "usage": {"input_tokens": 3, "output_tokens": 1}, "allow_edit": True}]


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class CliTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.project = Path(self._tmp.name) / "other-project"
        (self.project / "sub").mkdir(parents=True)
        (self.project / ".workflow" / "tickets").mkdir(parents=True)
        (self.project / ".workflow" / "tickets" / "7.json").write_text(json.dumps({
            "id": 7, "title": "Unrelated task", "status": "READY", "scope": {"summary": "x"},
            "acceptance_criteria": ["y"], "authorization": {"granted": False}, "blocker": None,
            "evidence_refs": [],
        }), encoding="utf-8")
        self.write_config({"roles": {"writer": {"command": "any-cli", "model": "any-model"}}})

    def tearDown(self):
        self._tmp.cleanup()

    def write_config(self, value):
        (self.project / "harmony.json").write_text(json.dumps(value), encoding="utf-8")

    def test_record_runs_codex_into_the_project_runs_dir(self):
        bin_dir = Path(self._tmp.name) / "bin"
        bin_dir.mkdir()
        fake = bin_dir / "codex"
        fake.write_text("#!/usr/bin/env python3\nimport json\n"
                        "print(json.dumps({'type': 'thread.started', 'thread_id': 't'}))\n"
                        "print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 9, "
                        "'cached_input_tokens': 4, 'cache_write_input_tokens': 0, 'output_tokens': 2, "
                        "'reasoning_output_tokens': 1}}))\n")
        fake.chmod(0o755)
        env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
        env.pop("CODEX_API_KEY", None)
        result = subprocess.run(
            [sys.executable, "-m", "harmony.cli", "--project", str(self.project), "record", "--task-id", "7",
             "--role", "planner", "--run-id", "7-plan", "--", "codex", "exec", "--json", "-m", "gpt-5.5", "plan"],
            cwd=ROOT, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        record = json.loads((self.project / ".direct-runs" / "7-plan.json").read_text())
        self.assertEqual((record["provider"], record["role"], record["tokens"]["uncached_input"]),
                         ("codex", "planner", 5))
        code, out, _ = run_cli("--project", str(self.project), "inspect", "7")
        self.assertEqual(code, 0)
        self.assertIn("codex", out)
        self.assertIn("role Planner", out)

    def test_record_refuses_a_codex_writer(self):
        code, _, err = run_cli("--project", str(self.project), "record", "--task-id", "7", "--role", "writer",
                               "--", "codex", "exec", "--json", "-m", "m", "x")
        self.assertEqual(code, 2)
        self.assertIn("explicit-model Claude", err)
        self.assertFalse((self.project / ".direct-runs").exists() and any((self.project / ".direct-runs").iterdir()))

    def test_record_needs_an_existing_ticket_for_a_numeric_id_even_without_a_tickets_dir(self):
        import direct_run_recorder
        from unittest import mock

        bare = Path(self._tmp.name) / "bare-project"
        bare.mkdir()
        with mock.patch.object(direct_run_recorder, "main", return_value=0) as recorder:
            code, _, err = run_cli("--project", str(bare), "record", "--task-id", "99", "--role", "planner",
                                   "--", "codex", "exec", "--json", "-")
            self.assertEqual(code, 2)
            self.assertIn("Task #99 does not exist", err)
            recorder.assert_not_called()
            self.assertFalse((bare / ".workflow").exists())
            # An explicitly named legacy standalone record still goes through.
            code, _, _ = run_cli("--project", str(bare), "record", "--task-id", "legacy-run", "--role", "planner",
                                 "--", "codex", "exec", "--json", "-")
            self.assertEqual(code, 0)
            self.assertEqual(recorder.call_args.args[0][:3], ["run", "--task-id", "legacy-run"])

    def test_record_passes_an_investigator_base_commit_and_this_project_workflow(self):
        import direct_run_recorder
        from unittest import mock

        with mock.patch.object(direct_run_recorder, "main", return_value=0) as recorder:
            code, _, _ = run_cli("--project", str(self.project), "record", "--task-id", "7", "--role",
                                 "investigator", "--base-commit", "abc1234", "--", "codex", "exec", "--json", "-")
            self.assertEqual(code, 0)
            argv = recorder.call_args.args[0]
            self.assertEqual(argv[argv.index("--base-commit") + 1], "abc1234")
            self.assertEqual(Path(argv[argv.index("--project-root") + 1]), self.project.resolve())
            self.assertEqual(Path(argv[argv.index("--workflow-dir") + 1]), (self.project / ".workflow").resolve())
            self.assertEqual(argv[argv.index("--") + 1:], ["codex", "exec", "--json", "-"])
            # Other roles are passed on exactly as before.
            run_cli("--project", str(self.project), "record", "--task-id", "7", "--role", "planner",
                    "--", "codex", "exec", "--json", "-")
            argv = recorder.call_args.args[0]
            self.assertNotIn("--base-commit", argv)
            self.assertNotIn("--project-root", argv)
            self.assertNotIn("--workflow-dir", argv)

    def test_investigator_reads_the_selected_project_repository_not_the_current_directory(self):
        def git(cwd, *args):
            return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                                   "-c", "commit.gpgsign=false", *args], cwd=cwd, check=True,
                                  capture_output=True, text=True).stdout.strip()

        tmp = Path(self._tmp.name).resolve()
        repos = {"A": tmp / "repo-a", "B": self.project.resolve()}  # B is the selected project
        shas = {}
        for name, repo in repos.items():
            repo.mkdir(exist_ok=True)
            git(repo, "init", "-q")
            (repo / "f.txt").write_text(f"repository {name}\n")
            git(repo, "add", "f.txt")
            git(repo, "commit", "-q", "-m", name)
            shas[name] = git(repo, "rev-parse", "HEAD")
        bin_dir, log = tmp / "bin", tmp / "seen.json"
        bin_dir.mkdir()
        fake = bin_dir / "claude"
        fake.write_text("#!/usr/bin/env python3\nimport json, os\n"
                        f"open({str(log)!r}, 'w').write(json.dumps({{'cwd': os.getcwd(), "
                        "'f': open('f.txt').read()}))\n")
        fake.chmod(0o755)
        env = {k: v for k, v in os.environ.items()
               if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_API_KEY", "TASK_WORKFLOW_DIR")}
        env.update(PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}", PYTHONPATH=str(ROOT))
        claude = ["claude", "-p", "investigate", "--model", "claude-sonnet-5-5", "--output-format",
                  "stream-json", "--verbose", "--restricted", "--tools", "Read,Grep,Glob", "--disallowedTools",
                  "Write,Edit,MultiEdit,NotebookEdit,Bash,PowerShell,REPL,Agent,Task", "--strict-mcp-config",
                  "--mcp-config", '{"mcpServers":{}}', "--settings", '{"disableAllHooks":true}',
                  "--setting-sources", ""]

        def record(sha):  # run from repository A, select project B
            return subprocess.run([sys.executable, "-m", "harmony.cli", "--project", str(repos["B"]), "record",
                                   "--task-id", "7", "--role", "investigator", "--run-id", "inv",
                                   "--base-commit", sha, "--", *claude],
                                  cwd=repos["A"], env=env, capture_output=True, text=True)

        result = record(shas["A"])
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("is not a commit in", result.stderr)
        self.assertFalse(log.exists(), "nothing was launched for a foreign base commit")
        self.assertFalse((repos["B"] / ".direct-runs" / "inv.json").exists())

        result = record(shas["B"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(log.read_text())["f"], "repository B\n")
        record_file = json.loads((repos["B"] / ".direct-runs" / "inv.json").read_text())
        repository = record_file["repository"]
        self.assertEqual((Path(repository["repository"]), repository["base_commit"], repository["read_commit"]),
                         (repos["B"], shas["B"], shas["B"]))
        self.assertNotIn(repos["A"], Path(json.loads(log.read_text())["cwd"]).resolve().parents)
        for leaked in (".direct-runs", ".workflow"):
            self.assertFalse((repos["A"] / leaked).exists(), "nothing is written to the current directory")
        self.assertEqual(git(repos["A"], "worktree", "list", "--porcelain").count("worktree "), 1)
        self.assertEqual(git(repos["B"], "worktree", "list", "--porcelain").count("worktree "), 1)

    def test_record_refuses_an_investigator_without_a_base_commit(self):
        from unittest import mock

        env = {k: v for k, v in os.environ.items() if k != "CODEX_API_KEY"}
        with mock.patch.dict(os.environ, env, clear=True):
            code, _, err = run_cli("--project", str(self.project), "record", "--task-id", "7", "--role",
                                   "investigator", "--", "codex", "exec", "--json", "-m", "m", "-s", "read-only", "-")
        self.assertEqual(code, 2)
        self.assertIn("--base-commit", err)
        self.assertFalse((self.project / ".direct-runs").exists() and any((self.project / ".direct-runs").iterdir()))

    def test_config_found_from_subdirectory(self):
        project = cli.resolve_project(self.project / "sub", None)
        self.assertEqual(self.project.resolve(), project["root"])
        self.assertEqual(self.project.resolve() / ".workflow", project["workflow_dir"])



    def test_inspect_unknown_task_fails(self):
        code, _, _ = run_cli("--project", str(self.project), "inspect", "999")
        self.assertEqual(1, code)


if __name__ == "__main__":
    unittest.main()
