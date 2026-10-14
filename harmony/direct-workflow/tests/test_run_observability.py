#!/usr/bin/env python3
"""Offline tests for run_observability.py on synthetic records and streams.

Records are built with the recorder's own `build_record`, so the fixtures
have exactly the shape a real run produces. No real stream is used.

Run:  python3 direct-workflow/tests/test_run_observability.py
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(PACKAGE_DIR))

import direct_run_recorder as rec  # noqa: E402
import run_observability as obs  # noqa: E402

SESSION = "sess-a"


def usage(i, cc, cr, o):
    return {"input_tokens": i, "cache_creation_input_tokens": cc,
            "cache_read_input_tokens": cr, "output_tokens": o}


def model_usage(i, cc, cr, o, cost):
    return {"claude-opus-5-5": {"inputTokens": i, "cacheCreationInputTokens": cc,
                                "cacheReadInputTokens": cr, "outputTokens": o, "costUSD": cost}}


def result(u, mu, cost, session=SESSION, subtype="success", duration=60000):
    return {"type": "result", "subtype": subtype, "is_error": subtype != "success",
            "session_id": session, "duration_ms": duration, "duration_api_ms": duration - 1000,
            "num_turns": 4, "usage": u, "modelUsage": mu, "total_cost_usd": cost}


def assistant(blocks, session=SESSION, parent=None, ts="2026-10-01T00:00:00Z"):
    return {"type": "assistant", "session_id": session, "parent_tool_use_id": parent, "timestamp": ts,
            "message": {"id": "m", "content": blocks, "usage": usage(1, 1, 1, 1)}}


def tool_use(tid, name, **inputs):
    return {"type": "tool_use", "id": tid, "name": name, "input": inputs}


def tool_result(tid, error=False, session=SESSION, parent=None):
    return {"type": "user", "session_id": session, "parent_tool_use_id": parent,
            "message": {"content": [{"type": "tool_result", "tool_use_id": tid, "is_error": error}]}}


INIT = {"type": "system", "subtype": "init", "model": "claude-opus-5-5", "session_id": SESSION,
        "apiKeySource": "none"}


def tool_stream(final):
    return [
        INIT,
        assistant([{"type": "text", "text": "start"}, tool_use("t1", "Read", file_path="a.py")]),
        tool_result("t1"),
        assistant([tool_use("t2", "Read", file_path="a.py", offset=10, limit=5)]),
        tool_result("t2"),
        assistant([tool_use("t3", "Read", file_path="a.py")]),
        tool_result("t3"),
        assistant([tool_use("t4", "Edit", file_path="a.py", old_string="x", new_string="y")]),
        tool_result("t4", error=True),
        assistant([tool_use("t5", "Bash", command="pytest -q", description="Run tests")]),
        tool_result("t5"),
        assistant([tool_use("s1", "Read", file_path="sub.py")], parent="t9"),
        tool_result("s1", parent="t9"),
        assistant([tool_use("t6", "Write", file_path="b.py", content="")]),
        tool_result("t6"),
        final,
    ]


def make_record(events, *, task="40", run_id, role="writer", started=None, argv=None,
                prior=None, wall=None):
    record = rec.build_record(
        task_id=task, run_id=run_id, role=role, model_requested="claude-opus-5-5", events=events,
        exit_code=0, wall_duration_ms=wall, started_at=started, ended_at=None, argv=argv,
        stream_path=None, api_key_env_present=False, prior_result=prior,
    )
    return record


FRESH_ARGV = ["claude", "-p", "x", "--model", "claude-opus-5-5"]
RESUME_ARGV = FRESH_ARGV + ["--resume", SESSION]


def session_runs():
    """Three invocations of one session; modelUsage and cost grow each time."""
    r1 = result(usage(10, 100, 1000, 50), model_usage(10, 100, 1000, 50, 1.0), 1.0)
    r2 = result(usage(5, 20, 500, 30), model_usage(15, 120, 1500, 80, 1.5), 1.5)
    r3 = result(usage(1, 2, 3, 4), model_usage(16, 122, 1503, 84, 1.6), 1.6)
    a = make_record([INIT, r1], run_id="40-writer", argv=FRESH_ARGV,
                    started="2026-10-01T00:00:00Z", wall=1000)
    b = make_record([INIT, r2], run_id="40-correction-1", role="correction", argv=RESUME_ARGV,
                    prior=r1, started="2026-10-01T01:00:00Z", wall=2000)
    c = make_record([INIT, r3], run_id="40-correction-2", role="correction", argv=RESUME_ARGV,
                    prior=r2, started="2026-10-01T02:00:00Z", wall=3000)
    return a, b, c


class InspectRun(unittest.TestCase):
    def setUp(self):
        final = result(usage(7, 70, 700, 77), model_usage(7, 70, 700, 77, 0.5), 0.5)
        self.record = make_record(tool_stream(final), run_id="r1", argv=FRESH_ARGV, wall=4321)
        text = "\n".join(json.dumps(e) for e in tool_stream(final))
        self.report = obs.inspect_run(self.record, stream_text=text, stream_path="r1.stream.jsonl")

    def test_raw_tokens_and_derived_are_separate(self):
        raw, derived = self.report["raw"], self.report["derived"]
        self.assertEqual(raw["tokens"]["uncached_input"], 7)
        self.assertEqual(raw["tokens"]["cache_creation_input"], 70)
        self.assertEqual(raw["tokens"]["cached_input"], 700)
        self.assertEqual(raw["tokens"]["output"], 77)
        self.assertEqual(derived["processed_input_tokens"]["value"], 777)
        self.assertEqual(derived["duration_ms"], {"value": 4321, "basis": "wall", "source": rec.SRC_WALL})
        self.assertEqual(raw["estimated_list_cost_usd"]["value"], 0.5)
        self.assertEqual(raw["estimated_list_cost_usd"]["method"], "fresh_session_total_matches_usage")
        self.assertIsNone(raw["actual_provider_charge_usd"]["value"])

    def test_tools_files_and_repeated_reads(self):
        stream = self.report["derived"]["stream"]
        self.assertTrue(stream["available"])
        self.assertEqual(stream["tool_calls"], {"Bash": 1, "Edit": 1, "Read": 3, "Write": 1})
        self.assertEqual(stream["tool_calls_total"], 6)
        self.assertEqual(stream["subagent_tool_calls"], 1)
        self.assertEqual(stream["tool_errors"], 1)
        self.assertEqual(stream["files"]["a.py"], {"read": 3, "edit": 1, "write": 0})
        self.assertNotIn("sub.py", stream["files"])  # subagent files are not main-thread facts
        self.assertEqual(stream["repeated_reads"],
                         [{"path": "a.py", "reads": 3, "same_range_repeats": 1}])

    def test_activity_is_ordered(self):
        activity = self.report["derived"]["stream"]["activity"]
        self.assertEqual([a["seq"] for a in activity], list(range(1, len(activity) + 1)))
        kinds = [(a["kind"], a.get("tool")) for a in activity]
        self.assertEqual(kinds[0], ("session_start", None))
        self.assertEqual(kinds[1:4], [("text", None), ("tool_use", "Read"), ("tool_result", "Read")])
        self.assertEqual(kinds[-1], ("result", None))
        edit_result = [a for a in activity if a["kind"] == "tool_result" and a["tool"] == "Edit"][0]
        self.assertTrue(edit_result["is_error"])
        self.assertIn("subagent", {a["agent"] for a in activity})

    def test_result_only_output_gives_unknown_tools_not_zero(self):
        final = result(usage(1, 1, 1, 1), model_usage(1, 1, 1, 1, 0.1), 0.1)
        report = obs.inspect_run(make_record([final], run_id="x", argv=FRESH_ARGV),
                                 stream_text=json.dumps(final))
        stream = report["derived"]["stream"]
        self.assertFalse(stream["available"])
        self.assertNotIn("tool_calls_total", stream)
        self.assertIn("derived.stream (tools, files, activity)", report["unknowns"])

    def test_stream_from_another_session_is_not_attributed(self):
        other = [dict(e, session_id="other") for e in tool_stream(result(usage(1, 1, 1, 1), None, None))]
        report = obs.inspect_run(self.record, stream_text="\n".join(json.dumps(e) for e in other))
        self.assertFalse(report["derived"]["stream"]["available"])
        self.assertIn("do not match", report["derived"]["stream"]["reason"])

    def test_missing_stream_is_explicit(self):
        report = obs.inspect_record(self.record)
        self.assertFalse(report["derived"]["stream"]["available"])
        self.assertEqual(report["derived"]["stream"]["reason"], "the record links no stream")


class SummarizeTask(unittest.TestCase):
    def test_session_cumulative_is_never_added(self):
        a, b, c = session_runs()
        summary = obs.summarize_task([c, a, b], "40")
        tokens = summary["tokens"]
        # per-invocation usage sum: 10+5+1, 100+20+2, 1000+500+3, 50+30+4
        self.assertEqual(
            [tokens[f] for f in obs.TOKEN_FIELDS], [16, 122, 1503, 84])
        # adding modelUsage would have given 41/342/4003/214
        self.assertEqual(tokens["processed_input"], 16 + 122 + 1503)
        self.assertEqual(summary["shared_sessions"],
                         {SESSION: ["40-writer", "40-correction-1", "40-correction-2"]})
        # estimates are the checked per-invocation deltas: 1.0 + 0.5 + 0.1
        self.assertAlmostEqual(summary["estimated_list_cost_usd"]["value"], 1.6)
        self.assertEqual(summary["duration_ms"]["value"], 6000)
        self.assertEqual((summary["writer_runs"], summary["correction_runs"]), (1, 2))

    def test_order_and_final_outcome_from_final_run_annotation(self):
        a, b, c = session_runs()
        rec.annotate(c, review_rounds=2, corrections=2, review_outcome="pass")
        rec.annotate(a, review_rounds=1, review_outcome="findings")
        summary = obs.summarize_task([c, b, a], "40")
        self.assertTrue(summary["order_known"])
        self.assertEqual([r["run_id"] for r in summary["runs"]],
                         ["40-writer", "40-correction-1", "40-correction-2"])
        self.assertEqual(summary["final_outcome"]["run_id"], "40-correction-2")
        self.assertEqual(summary["final_outcome"]["review"], "pass")
        self.assertEqual(summary["review"]["rounds"], 2)  # not 1 + 2

    def test_unknown_order_means_unknown_final_outcome(self):
        a, b, c = session_runs()
        b["started_at"] = None
        summary = obs.summarize_task([a, b, c], "40")
        self.assertFalse(summary["order_known"])
        self.assertIsNone(summary["final_outcome"]["run_id"])
        self.assertIn("final_outcome", summary["unknowns"])

    def test_partial_values_are_not_summed(self):
        a, b, c = session_runs()
        b["tokens"]["output"] = None
        b["cost"]["estimated_list_cost_usd"]["value"] = None
        b["duration_ms"]["wall"]["value"] = None
        b["duration_ms"]["reported"]["value"] = None
        summary = obs.summarize_task([a, b, c], "40")
        self.assertIsNone(summary["tokens"]["output"])
        self.assertEqual(summary["tokens"]["uncached_input"], 16)
        self.assertIsNone(summary["estimated_list_cost_usd"]["value"])
        self.assertIsNone(summary["duration_ms"]["value"])
        for name in ("tokens.output", "estimated_list_cost_usd", "duration_ms"):
            self.assertIn(name, summary["unknowns"])

    def test_provider_charge_only_when_explicit(self):
        a, b, c = session_runs()
        self.assertIsNone(obs.summarize_task([a, b, c], "40")["actual_provider_charge_usd"]["value"])
        for record, usd in ((a, 1.0), (b, 2.0), (c, 3.0)):
            rec.annotate(record, actual_charge_usd=usd, actual_charge_source="invoice 7")
        self.assertEqual(obs.summarize_task([a, b, c], "40")["actual_provider_charge_usd"]["value"], 6.0)
        task_level = obs.summarize_task([a], "40", actual_charge_usd=9.5, actual_charge_source="console")
        self.assertEqual(task_level["actual_provider_charge_usd"]["value"], 9.5)
        with self.assertRaises(obs.ObservabilityError):
            obs.summarize_task([a], "40", actual_charge_usd=9.5)

    def test_directory_loading_skips_incomplete_and_other_tasks(self):
        a, b, c = session_runs()
        other = make_record([INIT, result(usage(1, 1, 1, 1), None, None)], task="41", run_id="41-writer")
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            for record in (a, b, c, other):
                (d / f"{record['run_id']}.json").write_text(json.dumps(record))
            (d / "40-writer-zzz.json").write_text(json.dumps(
                {"schema": rec.SCHEMA, "run_id": "40-writer-zzz", "status": "reserved"}))
            (d / "40-writer-zzz.stream.jsonl").write_text("")
            summary = obs.summarize_task_dir(d, "40")
        self.assertEqual(summary["run_count"], 3)
        self.assertEqual(len(summary["skipped_records"]), 1)
        self.assertIn("reserved", summary["skipped_records"][0]["why"])


class Compare(unittest.TestCase):
    def test_runs_absolute_and_percent(self):
        a, b, _ = session_runs()
        result_ = obs.compare(obs.inspect_run(a), obs.inspect_run(b))
        by = {m["metric"]: m for m in result_["metrics"]}
        out = by["tokens.output"]
        self.assertEqual((out["a"], out["b"], out["abs_diff"], out["pct_diff"]), (50, 30, -20, -40.0))
        self.assertEqual(by["duration_ms"]["abs_diff"], 1000)
        self.assertFalse(by["tool_calls_total"]["comparable"])
        self.assertIn("unknown on a and b", by["tool_calls_total"]["reason"])
        self.assertTrue(result_["context"]["same_session"])

    def test_different_duration_basis_is_incomparable(self):
        a, b, _ = session_runs()
        b["duration_ms"]["wall"]["value"] = None
        by = {m["metric"]: m for m in obs.compare(obs.inspect_run(a), obs.inspect_run(b))["metrics"]}
        self.assertFalse(by["duration_ms"]["comparable"])
        self.assertIn("different basis", by["duration_ms"]["reason"])

    def test_zero_base_has_no_percent(self):
        a, b, c = session_runs()
        one = obs.summarize_task([a], "x")
        three = obs.summarize_task([a, b, c], "y")
        by = {m["metric"]: m for m in obs.compare(one, three)["metrics"]}
        self.assertEqual(by["correction_runs"]["abs_diff"], 2)
        self.assertIsNone(by["correction_runs"]["pct_diff"])

    def test_mixed_kinds_refused(self):
        a, _, _ = session_runs()
        with self.assertRaises(obs.ObservabilityError):
            obs.compare(obs.inspect_run(a), obs.summarize_task([a], "40"))


class CardMetrics(unittest.TestCase):
    def test_line_from_safe_totals_only(self):
        summary = obs.summarize_task(list(session_runs()), "40")
        card = obs.card_metrics(summary)
        # one token total (processed input 1641 + output 84) and one duration
        self.assertEqual(card["line"], "1.7k tok · 6s")
        self.assertEqual(card["total_tokens"], 1725)
        self.assertNotIn("$", card["line"])
        self.assertNotIn("run", card["line"])

    def test_unknown_metric_is_omitted(self):
        a, _, _ = session_runs()
        a["tokens"]["output"] = None  # total no longer attributable
        self.assertEqual(obs.card_metrics(obs.summarize_task([a], "40"))["line"], "1s")
        b, _, _ = session_runs()
        b["duration_ms"]["wall"]["value"] = None
        b["duration_ms"]["reported"]["value"] = None
        self.assertEqual(obs.card_metrics(obs.summarize_task([b], "40"))["line"], "1.2k tok")

    def test_nothing_safe_gives_none(self):
        a, _, _ = session_runs()
        a["tokens"]["output"] = None
        a["duration_ms"]["wall"]["value"] = None
        a["duration_ms"]["reported"]["value"] = None
        self.assertIsNone(obs.card_metrics(obs.summarize_task([a], "40")))
        self.assertIsNone(obs.card_metrics(obs.summarize_task([], "40")))


class Cli(unittest.TestCase):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = obs.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_commands_end_to_end(self):
        a, b, c = session_runs()
        final = result(usage(7, 70, 700, 77), model_usage(7, 70, 700, 77, 0.5), 0.5)
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            for record in (a, b, c):
                (d / f"{record['run_id']}.json").write_text(json.dumps(record))
            tooled = make_record(tool_stream(final), run_id="40-tooled", argv=FRESH_ARGV)
            # relative path as the recorder writes it; found next to the record
            tooled["stream_path"] = ".direct-runs/40-tooled.stream.jsonl"
            (d / "40-tooled.stream.jsonl").write_text("\n".join(json.dumps(e) for e in tool_stream(final)))
            (d / "40-tooled.json").write_text(json.dumps(tooled))

            code, out, _ = self.run_cli("inspect-run", "--records-dir", tmp, "--run-id", "40-tooled")
            self.assertEqual(code, 0)
            self.assertIn("tool calls     6", out)
            self.assertIn("repeated reads", out)
            code, out, _ = self.run_cli("summarize-task", "--records-dir", tmp, "--task-id", "40", "--json")
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["run_count"], 4)
            code, out, _ = self.run_cli("compare", "--records-dir", tmp, "--run", "40-writer",
                                        "--run", str(d / "40-correction-1.json"))
            self.assertEqual(code, 0)
            self.assertIn("-40.00%", out)
            code, _, err = self.run_cli("inspect-run", "--records-dir", tmp, "--run-id", "nope")
            self.assertEqual(code, 2)
            self.assertIn("no run record", err)


if __name__ == "__main__":
    unittest.main()
