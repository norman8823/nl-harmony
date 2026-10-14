#!/usr/bin/env python3
"""Offline tests for output_summary.py on synthetic child commands.

Run:  python3 direct-workflow/tests/test_output_summary.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parents[1]
SCRIPT = PACKAGE_DIR / "output_summary.py"
sys.dont_write_bytecode = True


def run(evidence: Path, *cmd: str, extra: tuple[str, ...] = ()) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-B", str(SCRIPT), "--evidence", str(evidence), *extra, "--", *cmd],
                          capture_output=True, text=True, check=False)


def py(code: str) -> tuple[str, ...]:
    return (sys.executable, "-c", code)


class OutputSummaryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_success_keeps_raw_output_and_reports_counts(self):
        ev = self.dir / "nested" / "ok.log"
        body = "".join(f"noise line {i}\n" for i in range(500))
        code = ("import sys\n"
                "for i in range(500): sys.stdout.write(f'noise line {i}\\n')\n"
                "sys.stdout.flush(); sys.stderr.write('stderr noise\\n'); sys.stderr.flush()\n"
                "sys.stdout.write('Ran 3 tests in 0.010s\\n\\nOK\\n')")
        p = run(ev, *py(code))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        raw = ev.read_text()
        self.assertIn(body, raw)
        self.assertIn("stderr noise\n", raw)
        self.assertIn("result:   Ran 3 tests in 0.010s", p.stdout)
        self.assertIn("result:   OK", p.stdout)
        self.assertIn(f"evidence: {os.path.abspath(ev)}", p.stdout)
        self.assertNotIn("noise line 42", p.stdout)
        self.assertNotIn("DISCREPANCY", p.stdout)

    def test_failure_returns_true_exit_and_bounded_relevant_lines(self):
        ev = self.dir / "fail.log"
        code = ("import sys\n"
                "for i in range(60): print(f'FAILED tests/t.py::test_{i} - assert 1 == 2')\n"
                "print('E   assert 1 == 2', file=sys.stderr)\n"
                "print('=== 60 failed, 4 passed in 0.12s ===')\n"
                "sys.exit(3)")
        p = run(ev, *py(code), extra=("--max-lines", "5"))
        self.assertEqual(p.returncode, 3)
        self.assertIn("exit:     3  — FAILED", p.stdout)
        self.assertIn("result:   === 60 failed, 4 passed in 0.12s ===", p.stdout)
        self.assertIn("relevant: 61 line(s), first 5 shown", p.stdout)
        lines = p.stdout.splitlines()
        relevant_start = next(i for i, line in enumerate(lines) if line.startswith("relevant:"))
        shown = [line for line in lines[relevant_start + 1:] if line.startswith("  ")]
        self.assertEqual(len(shown), 5)  # stderr may be one of the five; exclude the echoed command.
        self.assertTrue(any(line.startswith("  FAILED tests/t.py") for line in shown))
        raw = ev.read_text()
        self.assertEqual(raw.count("FAILED tests/t.py"), 60)
        self.assertIn("E   assert 1 == 2", raw)

    def test_many_result_lines_are_bounded_and_late_failure_not_hidden(self):
        ev = self.dir / "many.log"
        code = ("for i in range(200): print(f'=== {i} passed in 0.01s ===')\n"
                "print('=== 1 failed, 9 passed in 0.02s ===')")
        p = run(ev, *py(code), extra=("--max-lines", "3"))
        self.assertEqual(p.returncode, 0)  # the child itself exited 0
        result_lines = [l for l in p.stdout.splitlines() if l.startswith("result:")]
        self.assertEqual(len(result_lines), 5, p.stdout)  # 3 shown + count + omitted failure
        self.assertIn("result:   198 more result line(s) not shown (201 total)", p.stdout)
        self.assertIn("first failing one not shown: === 1 failed, 9 passed in 0.02s ===", p.stdout)
        self.assertIn("DISCREPANCY — exit status 0", p.stdout)
        self.assertLess(len(p.stdout), 2000)
        raw = ev.read_text().splitlines()
        self.assertEqual(len(raw), 201)
        self.assertEqual(raw[-1], "=== 1 failed, 9 passed in 0.02s ===")

    def test_failure_without_recognized_lines_shows_tail(self):
        ev = self.dir / "quiet.log"
        p = run(ev, *py("import sys; print('something went sideways'); sys.exit(7)"))
        self.assertEqual(p.returncode, 7)
        self.assertIn("no failure lines recognized", p.stdout)
        self.assertIn("something went sideways", p.stdout)

    def test_discrepancy_when_exit_zero_but_output_reports_failures(self):
        ev = self.dir / "lie.log"
        p = run(ev, *py("print('=== 1 failed, 2 passed in 0.1s ===')"))
        self.assertEqual(p.returncode, 0)
        self.assertIn("DISCREPANCY", p.stdout)

    def test_argv_is_not_shell_interpreted(self):
        ev = self.dir / "argv.log"
        tricky = "$(echo hi); exit 5 | cat > nope && *"
        p = run(ev, *py("import sys; print(repr(sys.argv[1:]))"), tricky, "two words")
        self.assertEqual(p.returncode, 0)
        self.assertEqual(ev.read_text().strip(), repr([tricky, "two words"]))
        self.assertFalse((Path.cwd() / "nope").exists())

    def test_signal_exit_is_reported_and_mapped(self):
        ev = self.dir / "sig.log"
        p = run(ev, *py("import os, signal; os.kill(os.getpid(), signal.SIGTERM)"))
        self.assertEqual(p.returncode, 128 + 15)
        self.assertIn("killed by SIGTERM (status -15)", p.stdout)

    def test_missing_command_is_127_and_recorded(self):
        ev = self.dir / "missing.log"
        p = run(ev, "/nonexistent/definitely-not-a-command")
        self.assertEqual(p.returncode, 127)
        self.assertIn("could not execute", p.stdout)
        self.assertIn("could not execute", ev.read_text())

    def test_refuses_existing_evidence_without_running(self):
        ev = self.dir / "exists.log"
        ev.write_text("keep me\n")
        marker = self.dir / "ran"
        p = run(ev, *py(f"open({str(marker)!r}, 'w').close()"))
        self.assertEqual(p.returncode, 2)
        self.assertEqual(ev.read_text(), "keep me\n")
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
