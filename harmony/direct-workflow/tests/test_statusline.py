#!/usr/bin/env python3
"""Offline tests for statusline.py on synthetic statusLine JSON and transcripts.

Run:  python3 direct-workflow/tests/test_statusline.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parents[1]
SCRIPT = PACKAGE_DIR / "statusline.py"
sys.dont_write_bytecode = True
sys.path.insert(0, str(PACKAGE_DIR))

import statusline as sl  # noqa: E402

FULL = {
    "session_id": "s1",
    "model": {"id": "claude-opus-5-5", "display_name": "Opus 5.5"},
    "context_window": {"used_percentage": 42, "context_window_size": 200000,
                       "total_input_tokens": 84000, "total_output_tokens": 900,
                       "current_usage": {"input_tokens": 4000, "output_tokens": 900,
                                         "cache_creation_input_tokens": 0,
                                         "cache_read_input_tokens": 80000}},
    "rate_limits": {"seven_day": {"used_percentage": 41.2}, "five_hour": {"used_percentage": 90}},
    "cost": {"total_cost_usd": 12.34},
    "prompt_cache": {"warm": True, "hit_ratio": 0.9},
}


def entry(msg_id, i, cc, cr, o, block="text", sidechain=False, model="claude-opus-5-5"):
    return {"type": "assistant", "isSidechain": sidechain, "requestId": "req-" + msg_id,
            "message": {"id": msg_id, "model": model, "content": [{"type": block}],
                        "usage": {"input_tokens": i, "cache_creation_input_tokens": cc,
                                  "cache_read_input_tokens": cr, "output_tokens": o}}}


class Values(unittest.TestCase):
    def test_context_prefers_used_percentage_then_reliable_current_usage(self):
        self.assertEqual(sl.context_percent(FULL), 42)
        derived = {"context_window": {"used_percentage": None, "context_window_size": 200000,
                                      "current_usage": FULL["context_window"]["current_usage"]}}
        self.assertEqual(sl.context_percent(derived), 42.0)  # (4000 + 0 + 80000) / 200000
        null_usage = {"context_window": {"used_percentage": None, "context_window_size": 200000,
                                         "current_usage": None}}
        self.assertIsNone(sl.context_percent(null_usage))
        incomplete = {"context_window": {"context_window_size": 200000,
                                         "current_usage": {"input_tokens": 5}}}
        self.assertIsNone(sl.context_percent(incomplete))

    def test_weekly_only_from_seven_day(self):
        self.assertAlmostEqual(sl.weekly_remaining(FULL), 58.8)
        self.assertIsNone(sl.weekly_remaining({"rate_limits": {"five_hour": {"used_percentage": 3}}}))
        self.assertIsNone(sl.weekly_remaining({}))


class Render(unittest.TestCase):
    def test_full_line_has_no_cache_or_estimate(self):
        line = sl.render(FULL, 200, tokens=(1_500_000, 42_000))
        self.assertEqual(line, "Opus 5.5 │ ctx ████░░░░░░ 42% │ 7d 59% left │ in 1.5M out 42.0k")
        self.assertNotIn("cache", line)
        self.assertNotIn("$", line)
        self.assertNotIn("12.34", line)
        self.assertNotIn("\n", line)

    def test_unknowns_are_omitted_not_zero(self):
        self.assertEqual(sl.render({"model": {"display_name": "Opus"}}, 80), "Opus")
        self.assertEqual(sl.render({}, 80), "")

    def test_fields_drop_by_priority_as_width_shrinks(self):
        lines = {w: sl.render(FULL, w, tokens=(10, 20), charge=1.5) for w in (200, 60, 45, 30, 18, 5)}
        self.assertIn("billed $1.50", lines[200])
        self.assertEqual(lines[60], "Opus 5.5 │ ctx ████░░░░░░ 42% │ 7d 59% left │ in 10 out 20")
        self.assertEqual(lines[45], "Opus 5.5 │ ctx ████░░░░░░ 42% │ 7d 59% left")
        self.assertEqual(lines[30], "Opus 5.5 │ ctx ████░░░░░░ 42%")
        self.assertEqual(lines[18], "Opus 5.5 │ ctx 42%")
        self.assertEqual(lines[5], "Opus…")
        for width, line in lines.items():
            self.assertLessEqual(sl.cells(line), width)

    def test_width_from_columns_and_reserve(self):
        old = os.environ.get("COLUMNS")
        try:
            os.environ["COLUMNS"] = "50"
            self.assertEqual(sl.terminal_width(None, 2), 48)
            os.environ["COLUMNS"] = "junk"
            self.assertEqual(sl.terminal_width(None, 0), sl.DEFAULT_WIDTH)
        finally:
            if old is None:
                os.environ.pop("COLUMNS", None)
            else:
                os.environ["COLUMNS"] = old
        self.assertEqual(sl.terminal_width(30, 2), 28)


class Transcript(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.cache = self.dir / "cache"
        self.path = self.dir / "t.jsonl"

    def write(self, entries, mode="w"):
        with self.path.open(mode) as handle:
            for e in entries:
                handle.write(json.dumps(e) + "\n")

    def test_dedupes_per_message_and_skips_sidechain_and_synthetic(self):
        self.write([
            entry("m1", 2, 100, 1000, 50, "thinking"),
            entry("m1", 2, 100, 1000, 50, "tool_use"),  # same message, second block
            {"type": "user", "message": {"content": "x"}},
            entry("m2", 1, 0, 2000, 10),
            entry("m3", 9, 9, 9, 9, sidechain=True),
            entry("m4", 9, 9, 9, 9, model="<synthetic>"),
        ])
        self.assertEqual(sl.session_tokens(str(self.path), self.cache), (2 + 100 + 1000 + 1 + 2000, 60))

    def test_incremental_append_and_partial_line(self):
        self.write([entry("m1", 1, 1, 1, 1)])
        self.assertEqual(sl.session_tokens(str(self.path), self.cache), (3, 1))
        with self.path.open("a") as handle:
            handle.write(json.dumps(entry("m2", 1, 1, 1, 1)) + "\n")
            handle.write(json.dumps(entry("m3", 5, 5, 5, 5))[:20])  # still being written
        self.assertEqual(sl.session_tokens(str(self.path), self.cache), (6, 2))
        with self.path.open("a") as handle:
            handle.write(json.dumps(entry("m3", 5, 5, 5, 5))[20:] + "\n")
        self.assertEqual(sl.session_tokens(str(self.path), self.cache), (21, 7))

    def test_missing_or_empty_transcript_is_unknown(self):
        self.assertIsNone(sl.session_tokens(str(self.dir / "nope.jsonl"), self.cache))
        self.assertIsNone(sl.session_tokens(None, self.cache))
        self.write([{"type": "user"}])
        self.assertIsNone(sl.session_tokens(str(self.path), self.cache))


class Charge(unittest.TestCase):
    def test_only_explicit_same_session_with_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "charge.json"
            path.write_text(json.dumps({"session_id": "s1", "usd": 2.5, "source": "console"}))
            self.assertEqual(sl.metered_charge(FULL, str(path)), 2.5)
            self.assertIsNone(sl.metered_charge({**FULL, "session_id": "s2"}, str(path)))
            path.write_text(json.dumps({"session_id": "s1", "usd": 2.5}))
            self.assertIsNone(sl.metered_charge(FULL, str(path)))
        self.assertIsNone(sl.metered_charge(FULL, None))


class Script(unittest.TestCase):
    def test_one_line_from_stdin(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "COLUMNS": "48", "TMPDIR": tmp}
            env.pop("STATUSLINE_CHARGE_FILE", None)
            out = subprocess.run([sys.executable, "-B", str(SCRIPT)], input=json.dumps(FULL),
                                 capture_output=True, text=True, env=env, check=True).stdout
            self.assertEqual(out, "Opus 5.5 │ ctx ████░░░░░░ 42% │ 7d 59% left\n")
            bad = subprocess.run([sys.executable, "-B", str(SCRIPT)], input="not json",
                                 capture_output=True, text=True, env=env, check=True).stdout
            self.assertEqual(bad, "\n")


if __name__ == "__main__":
    unittest.main()
