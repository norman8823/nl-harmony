#!/usr/bin/env python3
"""Run one noisy verification command; keep its full output, print a short summary.

    python3 direct-workflow/output_summary.py --evidence PATH [--max-lines N] -- CMD [ARG ...]

CMD runs exactly as given: the argv is passed to the OS unchanged (no shell,
no pipeline), with the caller's cwd, environment and stdin. Its stdout and
stderr are written, combined and unmodified, to the evidence file PATH (which
must not already exist; parent directories are created). Nothing goes to the
terminal except the summary:

  command    the argv, shell-quoted for reading only (cut at 300 characters)
  exit       the child's real exit status (a signal is named)
  result     summary lines recognized verbatim (pytest, unittest, ruff),
             at most --max-lines, with a count of any not shown and the
             first failing one among those; omitted when none are recognized
  relevant   failure/error lines, at most --max-lines (default 40), each cut
             at 300 characters, with a count of any not shown; when the
             command failed and nothing matched, the output's last lines
  note       a DISCREPANCY line when the exit status and the output disagree
  evidence   PATH, the complete raw output

The helper exits with the child's exit status (128+N for signal N), so a
failure is never hidden. It exits 2 before running anything on a usage error
or an existing evidence file, and 127/126 when CMD cannot be found/executed.
It is for explicit use around noisy commands; nothing wraps commands
automatically.
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import signal
import subprocess
import sys
from pathlib import Path

LINE_CHARS = 300
TAIL_LINES = 20

# Verbatim result lines that are safe to recognize.
RESULT_PATTERNS = [
    re.compile(r"^=+ .*\b(passed|failed|error|errors|skipped|deselected|no tests ran)\b.* in [\d.]+s.* =+$"),
    re.compile(r"^Ran \d+ tests? in [\d.]+s$"),
    re.compile(r"^(OK|FAILED)( \(.*\))?$"),
    re.compile(r"^Found \d+ errors?\b"),
    re.compile(r"^All checks passed!$"),
]

# Lines that indicate a failure or error.
FAILURE_PATTERNS = [
    re.compile(r"^(FAILED|ERROR) "),                # pytest short summary
    re.compile(r"^(FAIL|ERROR): "),                 # unittest
    re.compile(r"^E\s{2,}"),                        # pytest assertion detail
    re.compile(r"^Traceback \(most recent call last\):"),
    re.compile(r"^[\w.]*(Error|Exception): "),      # raised exception
    re.compile(r"\b(error|fatal):\s", re.IGNORECASE),
    re.compile(r"^\S+:\d+:\d+: [A-Z]+\d+ "),        # ruff/flake8 diagnostics
    re.compile(r": (trailing whitespace|space before tab in indent|new blank line at EOF)\.$"),
    re.compile(r"leftover conflict marker"),
]

FAILED_COUNT = re.compile(r"\b(\d+) (failed|errors?)\b|\((failures|errors)=(\d+)")


def _cut(line: str) -> str:
    return line if len(line) <= LINE_CHARS else line[:LINE_CHARS] + " …[cut]"


class Scan:
    """Bounded view of the evidence file; counts and flags cover every line."""

    def __init__(self) -> None:
        self.results: list[str] = []       # first max_lines result lines
        self.result_total = 0
        self.result_failed = False         # any result line, shown or not, reports a failure
        self.omitted_failure: str | None = None  # first failing result line not shown
        self.relevant: list[str] = []      # first max_lines failure lines
        self.relevant_total = 0
        self.tail: list[str] = []


def scan(path: Path, max_lines: int) -> Scan:
    out = Scan()
    tail: list[str] = []
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.rstrip("\r\n")
            stripped = line.strip()
            tail.append(line)
            if len(tail) > TAIL_LINES:
                tail.pop(0)
            if any(p.search(stripped) for p in RESULT_PATTERNS):
                out.result_total += 1
                failed = reports_failure(stripped)
                out.result_failed = out.result_failed or failed
                if len(out.results) < max_lines:
                    out.results.append(_cut(stripped))
                elif failed and out.omitted_failure is None:
                    out.omitted_failure = _cut(stripped)
            elif any(p.search(line) for p in FAILURE_PATTERNS):
                out.relevant_total += 1
                if len(out.relevant) < max_lines:
                    out.relevant.append(_cut(line))
    out.tail = [_cut(t) for t in tail if t.strip()]
    return out


def reports_failure(line: str) -> bool:
    if line.startswith("FAILED") or line.startswith("Found "):
        return True
    return any(int(m.group(1) or m.group(4) or 0) > 0 for m in FAILED_COUNT.finditer(line))


def describe(code: int) -> tuple[str, int]:
    if code < 0:
        try:
            name = signal.Signals(-code).name
        except ValueError:
            name = f"signal {-code}"
        return f"killed by {name} (status {code})", 128 - code
    return str(code), code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run CMD, keep its complete output in an evidence file, print a bounded summary.")
    parser.add_argument("--evidence", required=True, type=Path,
                        help="file for the complete combined stdout/stderr (must not exist)")
    parser.add_argument("--max-lines", type=int, default=40,
                        help="most result lines, and most failure lines, to print (default 40)")
    parser.add_argument("cmd", nargs=argparse.REMAINDER, help="-- CMD [ARG ...]")
    args = parser.parse_args(argv)
    cmd = args.cmd[1:] if args.cmd[:1] == ["--"] else args.cmd
    if not cmd:
        parser.error("no command given; use: --evidence PATH -- CMD [ARG ...]")
    if args.max_lines < 1:
        parser.error("--max-lines must be at least 1")
    evidence: Path = args.evidence
    if evidence.exists():
        print(f"output_summary: refusing to overwrite existing evidence {evidence}", file=sys.stderr)
        return 2
    evidence.parent.mkdir(parents=True, exist_ok=True)

    shown = shlex.join(cmd)
    launch_error = None
    with evidence.open("xb") as out:
        try:
            code = subprocess.run(cmd, stdout=out, stderr=subprocess.STDOUT, check=False).returncode
        except FileNotFoundError as exc:
            launch_error, code = exc, 127
        except PermissionError as exc:
            launch_error, code = exc, 126
        except OSError as exc:
            launch_error, code = exc, 126
        if launch_error is not None:
            out.write(f"output_summary: could not execute: {launch_error}\n".encode())

    exit_text, exit_code = describe(code)
    found = scan(evidence, args.max_lines)

    print(f"command:  {_cut(shown)}")
    print(f"exit:     {exit_text}" + ("" if code == 0 else "  — FAILED"))
    if launch_error is not None:
        print(f"error:    could not execute: {launch_error}")
    for line in found.results:
        print(f"result:   {line}")
    if found.result_total > len(found.results):
        print(f"result:   {found.result_total - len(found.results)} more result line(s) not shown"
              f" ({found.result_total} total)")
        if found.omitted_failure is not None:
            print(f"result:   first failing one not shown: {found.omitted_failure}")
    if found.relevant:
        print(f"relevant: {found.relevant_total} line(s)"
              + (f", first {len(found.relevant)} shown" if found.relevant_total > len(found.relevant) else ""))
        for line in found.relevant:
            print(f"  {line}")
    elif code != 0 and found.tail and launch_error is None:
        print(f"relevant: no failure lines recognized; last {len(found.tail)} line(s):")
        for line in found.tail:
            print(f"  {line}")
    if code == 0 and (found.relevant_total or found.result_failed):
        print("note:     DISCREPANCY — exit status 0 but the output has failure-like lines; read the evidence")
    elif code != 0 and found.result_total and not found.result_failed:
        print("note:     DISCREPANCY — non-zero exit but the recognized result lines report no failures; read the evidence")
    print(f"evidence: {os.path.abspath(evidence)}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
