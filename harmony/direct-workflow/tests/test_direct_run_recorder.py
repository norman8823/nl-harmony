#!/usr/bin/env python3
"""Offline tests for the direct-run recorder.

No real `claude`, no network, no credits: parsing runs on fixtures, and the
`run` path executes a fake `claude` placed first on PATH.

Run:  python3 direct-workflow/tests/test_direct_run_recorder.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

PACKAGE_DIR = Path(__file__).resolve().parents[1]
RECORDER_PATH = PACKAGE_DIR / "direct_run_recorder.py"


def load_recorder():
    spec = importlib.util.spec_from_file_location("direct_run_recorder", RECORDER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    sys.dont_write_bytecode = True
    spec.loader.exec_module(module)
    return module


rec = load_recorder()

INIT = {
    "type": "system",
    "subtype": "init",
    "model": "claude-opus-5-5",
    "session_id": "sess-1",
    "apiKeySource": "none",
}
RESULT = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "duration_ms": 61000,
    "duration_api_ms": 55000,
    "num_turns": 12,
    "session_id": "sess-1",
    "total_cost_usd": 1.2345,
    "usage": {
        "input_tokens": 10,
        "cache_creation_input_tokens": 2000,
        "cache_read_input_tokens": 90000,
        "output_tokens": 3000,
    },
    # A fresh session: the cumulative counters equal this invocation's usage.
    "modelUsage": {
        "claude-opus-5-5": {
            "inputTokens": 10,
            "outputTokens": 3000,
            "cacheReadInputTokens": 90000,
            "cacheCreationInputTokens": 2000,
            "costUSD": 1.2345,
        },
    },
}
STREAM = "\n".join(
    json.dumps(e)
    for e in (INIT, {"type": "assistant", "message": {"content": []}}, RESULT)
) + "\n"


def real_result(subtype, usage, model_usage, total_cost):
    """Minimal Claude CLI result summary with the given counters."""
    return {
        "type": "result",
        "subtype": subtype,
        "is_error": subtype != "success",
        "session_id": REAL_SESSION,
        "total_cost_usd": total_cost,
        "usage": dict(zip(
            ("input_tokens", "cache_creation_input_tokens",
             "cache_read_input_tokens", "output_tokens"), usage)),
        "modelUsage": {"claude-opus-5-5": dict(zip(
            ("inputTokens", "cacheCreationInputTokens", "cacheReadInputTokens",
             "outputTokens", "costUSD"), (*model_usage, total_cost)))},
    }


# Three successive invocations of one resumed Opus session (synthetic counters
# with the shape the CLI reports: `usage` is per invocation, `modelUsage` and
# `total_cost_usd` are session-cumulative). CORRECTION's cumulative counters
# deliberately do not equal RESUME's plus its own usage; CORRECTION2's do.
# Order of counters: uncached input, cache creation, cache read, output.
REAL_SESSION = "00000000-0000-4000-8000-000000000001"
REAL_RESUME = real_result(
    "success", (10, 1000, 20000, 300), (10, 1000, 20000, 300),
    1.25)
REAL_CORRECTION = real_result(
    "error_max_turns", (20, 2000, 30000, 400), (40, 3500, 60000, 800),
    3.5)
REAL_CORRECTION2 = real_result(
    "success", (5, 500, 10000, 100), (45, 4000, 70000, 900),
    4.0)
RESUME_ARGV = ["claude", "-p", "x", "--resume", REAL_SESSION,
               "--model", "claude-opus-5-5", "--output-format", "json"]


def build(events, **kw):
    defaults = dict(
        task_id="39",
        run_id="r1",
        role="writer",
        model_requested="claude-opus-5-5",
        events=events,
        exit_code=0,
        wall_duration_ms=62000,
        started_at="2026-10-01T00:00:00.000+00:00",
        ended_at="2026-10-01T00:01:02.000+00:00",
        argv=["claude", "-p", "x"],
        stream_path=None,
        api_key_env_present=False,
    )
    defaults.update(kw)
    return rec.build_record(**defaults)


class TestParsing(unittest.TestCase):
    def test_stream_json_tokens_come_from_invocation_usage(self):
        record = build(list(rec.iter_events(STREAM)))
        tokens = record["tokens"]
        self.assertEqual(tokens["source"], rec.SRC_USAGE)
        self.assertEqual(tokens["scope"], rec.INVOCATION_SCOPE)
        self.assertEqual(tokens["uncached_input"], 10)
        self.assertEqual(tokens["cache_creation_input"], 2000)
        self.assertEqual(tokens["cached_input"], 90000)
        self.assertEqual(tokens["output"], 3000)
        self.assertEqual(tokens["cross_check"], "cumulative_equals_usage")
        cumulative = record["session_cumulative"]
        self.assertEqual(cumulative["source"], rec.SRC_MODEL_USAGE)
        self.assertEqual(cumulative["scope"], rec.CUMULATIVE_SCOPE)
        self.assertEqual(set(cumulative["by_model"]), set(RESULT["modelUsage"]))
        self.assertEqual(record["model"]["reported"]["value"], ["claude-opus-5-5"])
        self.assertEqual(record["model"]["reported"]["scope"], rec.CUMULATIVE_SCOPE)
        self.assertIs(record["resumed_session"]["value"], False)
        self.assertEqual(record["outcome"]["cli"]["value"], "success")
        self.assertEqual(record["num_turns"]["value"], 12)
        self.assertEqual(record["duration_ms"]["wall"]["value"], 62000)
        self.assertEqual(record["duration_ms"]["reported"]["value"], 61000)
        self.assertEqual(record["auth"]["api_key_source"]["value"], "none")

    def test_json_output_without_model_usage_keeps_usage_but_not_cost(self):
        single = {k: v for k, v in RESULT.items() if k != "modelUsage"}
        record = build(list(rec.iter_events(json.dumps(single, indent=2))))
        self.assertEqual(record["tokens"]["source"], rec.SRC_USAGE)
        self.assertEqual(record["tokens"]["cached_input"], 90000)
        self.assertEqual(record["tokens"]["uncached_input"], 10)
        self.assertIsNone(record["session_cumulative"]["source"])
        estimate = record["cost"]["estimated_list_cost_usd"]
        self.assertIsNone(estimate["value"])
        self.assertTrue(estimate["unknown_reason"])
        self.assertEqual(
            record["session_cumulative"]["estimated_list_cost_usd"]["value"], 1.2345
        )

    def test_noise_lines_are_skipped_not_guessed(self):
        events = list(rec.iter_events("garbage\n" + STREAM + "{broken\n"))
        self.assertEqual([e["type"] for e in events], ["system", "assistant", "result"])

    def test_last_result_event_wins(self):
        early = dict(RESULT, subtype="error_max_turns", is_error=True)
        record = build([INIT, early, RESULT])
        self.assertEqual(record["outcome"]["cli"]["value"], "success")


class TestUnknowns(unittest.TestCase):
    def test_no_result_event_is_all_unknown_not_zero(self):
        record = build([INIT], exit_code=1)
        for name in ("uncached_input", "cached_input", "cache_creation_input", "output"):
            self.assertIsNone(record["tokens"][name])
            self.assertIn(f"tokens.{name}", record["unknowns"])
        self.assertIsNone(record["tokens"]["source"])
        self.assertEqual(record["outcome"]["cli"]["value"], "unknown")
        self.assertIn("cost.estimated_list_cost_usd", record["unknowns"])
        self.assertEqual(record["model"]["reported"]["value"], ["claude-opus-5-5"])
        self.assertEqual(record["model"]["reported"]["source"], f"{rec.SRC_INIT}.model")

    def test_empty_stream(self):
        record = build([], exit_code=None, wall_duration_ms=None)
        self.assertIn("duration_ms.wall", record["unknowns"])
        self.assertIn("auth.api_key_source", record["unknowns"])
        self.assertIn("model.reported", record["unknowns"])

    def test_partial_model_usage_does_not_produce_a_partial_sum(self):
        partial = dict(RESULT)
        partial["modelUsage"] = {
            "claude-opus-5-5": {"inputTokens": 1, "outputTokens": 2},
            "claude-haiku-4-5-20251001": {"inputTokens": 3, "outputTokens": 4,
                                          "cacheReadInputTokens": 5},
        }
        record = build([partial])
        totals = record["session_cumulative"]["totals"]
        self.assertEqual(totals["uncached_input"], 4)
        self.assertIsNone(totals["cached_input"])
        self.assertEqual(record["tokens"]["cached_input"], 90000)
        self.assertIsNone(record["cost"]["estimated_list_cost_usd"]["value"])
        self.assertIn("cost.estimated_list_cost_usd", record["unknowns"])

    def test_bad_counter_types_are_unknown(self):
        bad = dict(RESULT, total_cost_usd="1.23", num_turns=True)
        bad["usage"] = {"input_tokens": "40", "output_tokens": -1}
        record = build([bad])
        self.assertIsNone(record["tokens"]["uncached_input"])
        self.assertIsNone(record["tokens"]["output"])
        self.assertIn("tokens.output", record["unknowns"])
        self.assertIsNone(record["num_turns"]["value"])
        self.assertIsNone(record["cost"]["estimated_list_cost_usd"]["value"])
        self.assertIsNone(
            record["session_cumulative"]["estimated_list_cost_usd"]["value"]
        )

    def test_review_rounds_and_corrections_default_unknown(self):
        record = build(list(rec.iter_events(STREAM)))
        self.assertIn("review_rounds", record["unknowns"])
        self.assertIn("corrections", record["unknowns"])
        self.assertIn("outcome.review", record["unknowns"])

    def test_error_exit_with_success_result_is_an_error(self):
        record = build([RESULT], exit_code=1)
        self.assertEqual(record["outcome"]["cli"]["value"], "error")


class TestCostSeparation(unittest.TestCase):
    def test_estimate_is_never_promoted_to_charge_or_credits(self):
        record = build(list(rec.iter_events(STREAM)))
        cost = record["cost"]
        self.assertEqual(cost["estimated_list_cost_usd"]["value"], 1.2345)
        self.assertEqual(cost["estimated_list_cost_usd"]["source"], rec.SRC_TOTAL_COST)
        self.assertIsNone(cost["actual_provider_charge_usd"]["value"])
        self.assertIsNone(cost["subscription_credits"]["value"])
        self.assertIn("cost.actual_provider_charge_usd", record["unknowns"])
        self.assertIn("cost.subscription_credits", record["unknowns"])

    def test_annotation_needs_a_source_for_money(self):
        record = build(list(rec.iter_events(STREAM)))
        with self.assertRaises(rec.RefusedError):
            rec.annotate(record, actual_charge_usd=3.0)
        with self.assertRaises(rec.RefusedError):
            rec.annotate(record, subscription_credits=1.0)
        self.assertIsNone(record["cost"]["actual_provider_charge_usd"]["value"])

    def test_annotation_records_operator_facts(self):
        record = build(list(rec.iter_events(STREAM)))
        rec.annotate(
            record,
            review_rounds=2,
            corrections=1,
            review_outcome="pass",
            actual_charge_usd=0.0,
            actual_charge_source="subscription; no metered charge per console",
        )
        self.assertEqual(record["review_rounds"], {"value": 2, "source": "operator"})
        self.assertEqual(record["corrections"]["value"], 1)
        self.assertEqual(record["outcome"]["review"]["value"], "pass")
        self.assertTrue(
            record["cost"]["actual_provider_charge_usd"]["source"].startswith("operator: ")
        )
        self.assertNotIn("review_rounds", record["unknowns"])
        self.assertIn("cost.subscription_credits", record["unknowns"])


class TestResumedSessionScope(unittest.TestCase):
    """Resumed-session summaries: cumulative fields never become per-run."""

    def test_resumed_run_takes_tokens_from_usage_and_leaves_cost_unknown(self):
        record = build([REAL_CORRECTION2], argv=RESUME_ARGV)
        tokens = record["tokens"]
        self.assertEqual(tokens["source"], rec.SRC_USAGE)
        self.assertEqual(
            [tokens[n] for n in ("uncached_input", "cache_creation_input",
                                 "cached_input", "output")],
            [5, 500, 10000, 100],
        )
        self.assertIs(record["resumed_session"]["value"], True)
        estimate = record["cost"]["estimated_list_cost_usd"]
        self.assertIsNone(estimate["value"])
        self.assertIn("resumes a session", estimate["unknown_reason"])
        self.assertIn("cost.estimated_list_cost_usd", record["unknowns"])
        cumulative = record["session_cumulative"]
        self.assertEqual(cumulative["scope"], rec.CUMULATIVE_SCOPE)
        self.assertEqual(cumulative["totals"]["output"], 900)
        self.assertEqual(
            cumulative["estimated_list_cost_usd"]["value"], 4.0
        )
        self.assertEqual(record["model"]["reported"]["value"], ["claude-opus-5-5"])

    def test_checked_prior_snapshot_delta_gives_the_invocation_cost(self):
        record = build([REAL_CORRECTION2], argv=RESUME_ARGV,
                       prior_result=REAL_CORRECTION, prior_result_path="c1.json")
        estimate = record["cost"]["estimated_list_cost_usd"]
        self.assertAlmostEqual(estimate["value"], 0.5, places=9)
        self.assertEqual(estimate["method"], "prior_snapshot_delta")
        self.assertEqual(estimate["source"], rec.SRC_PRIOR_DELTA)
        self.assertIsNone(estimate["unknown_reason"])
        self.assertEqual(record["tokens"]["cross_check"], "prior_snapshot_delta_match")
        self.assertEqual(record["prior_result"], "c1.json")
        self.assertNotIn("cost.estimated_list_cost_usd", record["unknowns"])
        # still an estimate, never a charge or credits
        self.assertIsNone(record["cost"]["actual_provider_charge_usd"]["value"])
        self.assertIsNone(record["cost"]["subscription_credits"]["value"])

    def test_prior_delta_that_does_not_match_usage_is_not_attributed(self):
        # correction's modelUsage minus resume's differs from correction's usage
        record = build([REAL_CORRECTION], argv=RESUME_ARGV, prior_result=REAL_RESUME)
        estimate = record["cost"]["estimated_list_cost_usd"]
        self.assertIsNone(estimate["value"])
        self.assertIn("cannot be attributed", estimate["unknown_reason"])
        self.assertEqual(record["tokens"]["cross_check"], "prior_snapshot_delta_mismatch")
        self.assertEqual(record["tokens"]["output"], 400)

    def test_prior_from_another_session_is_rejected(self):
        other = dict(REAL_CORRECTION, session_id="other-session")
        record = build([REAL_CORRECTION2], argv=RESUME_ARGV, prior_result=other)
        self.assertIsNone(record["cost"]["estimated_list_cost_usd"]["value"])
        self.assertIn("session", record["cost"]["estimated_list_cost_usd"]["unknown_reason"])

    def test_unknown_resume_state_leaves_cost_unknown(self):
        record = build([REAL_RESUME], argv=None)
        self.assertIsNone(record["resumed_session"]["value"])
        self.assertIn("resumed_session", record["unknowns"])
        self.assertIsNone(record["cost"]["estimated_list_cost_usd"]["value"])
        self.assertEqual(record["tokens"]["output"], 300)

    def test_fresh_run_with_cumulative_unlike_usage_leaves_cost_unknown(self):
        record = build([REAL_CORRECTION2], argv=["claude", "-p", "x"])
        estimate = record["cost"]["estimated_list_cost_usd"]
        self.assertIsNone(estimate["value"])
        self.assertEqual(record["tokens"]["cross_check"], "cumulative_differs_from_usage")

    def test_resume_flag_forms(self):
        for argv in (["claude", "-c"], ["claude", "--continue"], ["claude", "-r", "s"],
                     ["claude", "--resume=s"], ["claude", "--fork-session"]):
            with self.subTest(argv):
                self.assertIs(rec.is_resume(argv), True)
        self.assertIs(rec.is_resume(["claude", "-p", "x"]), False)
        self.assertIsNone(rec.is_resume(None))

    def test_parse_cli_accepts_a_prior_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            prior = Path(tmp) / "c1.json"
            prior.write_text(json.dumps(REAL_CORRECTION))
            stream = Path(tmp) / "c2.json"
            stream.write_text(json.dumps(REAL_CORRECTION2))
            out = Path(tmp) / "rec.json"
            code = rec.main(["parse", "--task-id", "39", "--role", "correction",
                             "--stream", str(stream), "--prior-result", str(prior),
                             "--out", str(out)])
            self.assertEqual(code, 0)
            record = json.loads(out.read_text())
            self.assertAlmostEqual(
                record["cost"]["estimated_list_cost_usd"]["value"], 0.5, places=9
            )
            missing = rec.main(["parse", "--task-id", "39", "--role", "correction",
                                "--stream", str(stream),
                                "--prior-result", str(Path(tmp) / "nope.json"),
                                "--out", str(out)])
            self.assertEqual(missing, 2)


RELEASE_SETTINGS = str(PACKAGE_DIR / "release-settings.json")


class TestInvocationChecks(unittest.TestCase):
    GOOD = ["claude", "-p", "do it", "--model", "claude-opus-5-5",
            "--output-format", "stream-json", "--verbose",
            "--settings", RELEASE_SETTINGS]

    def test_accepts_the_direct_opus_writer_call(self):
        self.assertEqual(rec.check_invocation(self.GOOD, "writer", {}), "claude-opus-5-5")
        self.assertEqual(
            rec.check_invocation(
                ["/usr/local/bin/claude", "--print", "x", "--model=claude-opus-5-5",
                 "--output-format=json", f"--settings={RELEASE_SETTINGS}"],
                "correction", {}),
            "claude-opus-5-5",
        )

    def test_writer_and_correction_accept_any_explicit_claude_model(self):
        for role in ("writer", "correction"):
            for model in ("sonnet", "haiku", "claude-fable-5-1", "custom-explicit-model"):
                with self.subTest(role=role, model=model):
                    argv = list(self.GOOD)
                    argv[argv.index("--model") + 1] = model
                    self.assertEqual(rec.check_invocation(argv, role, {}), model)

    def test_non_opus_models_still_require_model_output_and_guard(self):
        for role in ("writer", "correction"):
            argv = list(self.GOOD)
            argv[argv.index("--model") + 1] = "sonnet"
            missing_model = list(argv)
            at = missing_model.index("--model")
            del missing_model[at:at + 2]
            text_output = list(argv)
            text_output[text_output.index("--output-format") + 1] = "text"
            for invalid in (missing_model, text_output, argv[:-2]):
                with self.subTest(role=role, argv=invalid):
                    with self.assertRaises(rec.RefusedError):
                        rec.check_invocation(invalid, role, {})

    def test_relative_release_settings_resolve_against_the_run_directory(self):
        argv = [a for a in self.GOOD if a != RELEASE_SETTINGS] + [
            "direct-workflow/release-settings.json"]
        argv.remove("--settings")
        argv.insert(-1, "--settings")
        self.assertEqual(
            rec.check_invocation(argv, "writer", {}, PACKAGE_DIR.parent), "claude-opus-5-5"
        )
        with self.assertRaises(rec.RefusedError):
            rec.check_invocation(argv, "writer", {}, Path(tempfile.gettempdir()))

    def test_writer_and_correction_require_the_release_settings_guard(self):
        bare = self.GOOD[:-2]
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "release-settings.json"
            copy.write_text((PACKAGE_DIR / "release-settings.json").read_text())
            empty = Path(tmp) / "empty.json"
            empty.write_text("{}")
            cases = {
                "settings omitted": bare,
                "other settings file": bare + ["--settings", str(empty)],
                "copy outside the package": bare + ["--settings", str(copy)],
                "inline json": bare + ["--settings", '{"hooks": {}}'],
                "flag without value": bare + ["--settings"],
            }
            for role in ("writer", "correction"):
                for label, argv in cases.items():
                    with self.subTest(role=role, case=label):
                        with self.assertRaises(rec.RefusedError) as ctx:
                            rec.check_invocation(argv, role, {})
                        self.assertIn("release-settings.json", str(ctx.exception))

    def test_release_settings_file_registers_the_guard(self):
        self.assertTrue(rec._registers_guard(rec.RELEASE_SETTINGS))

    def test_refusals(self):
        cases = {
            "not claude": (["python3", "x.py"], "writer", {}),
            "interactive": ([a for a in self.GOOD if a != "-p"], "writer", {}),
            "text output": (["claude", "-p", "x", "--model", "claude-opus-5-5"], "writer", {}),
            "implicit model": (["claude", "-p", "x", "--output-format", "json"], "writer", {}),
            "unguarded sonnet writer": (
                ["claude", "-p", "x", "--model", "claude-sonnet-5-5",
                 "--output-format", "json"], "writer", {}),
            "api key env": (self.GOOD, "writer", {"ANTHROPIC_API_KEY": "sk-test"}),
            "auth token env": (self.GOOD, "writer", {"ANTHROPIC_AUTH_TOKEN": "t"}),
        }
        for label, (argv, role, env) in cases.items():
            with self.subTest(label):
                with self.assertRaises(rec.RefusedError):
                    rec.check_invocation(argv, role, env)

    def test_investigator_may_use_another_explicit_model(self):
        argv = ["claude", "-p", "x", "--model", "claude-sonnet-5-5", "--output-format", "json"]
        self.assertEqual(rec.check_invocation(argv, "investigator", {}), "claude-sonnet-5-5")

    def test_long_arguments_are_redacted(self):
        redacted = rec.redact_argv(["claude", "-p", "x" * 500])
        self.assertEqual(redacted[:2], ["claude", "-p"])
        self.assertTrue(redacted[2].startswith("<500 chars sha256:"))


class TestRunEndToEnd(unittest.TestCase):
    """`run` executes the given argv unchanged and records it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.bin = root / "bin"
        self.bin.mkdir()
        self.records = root / "records"
        self.argv_log = root / "argv.json"
        fake = self.bin / "claude"
        fake.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys, time\n"
            f"open({str(self.argv_log)!r}, 'w').write(json.dumps(sys.argv[1:]))\n"
            "tag = os.environ.get('FAKE_TAG')\n"
            "if tag:\n"
            "    sys.stdout.write(json.dumps({'type': 'marker', 'tag': tag}) + '\\n')\n"
            "    sys.stdout.flush()\n"
            "time.sleep(float(os.environ.get('FAKE_SLEEP', '0')))\n"
            "out = os.environ.get('FAKE_OUTPUT')\n"
            f"sys.stdout.write(open(out).read() if out else {STREAM!r})\n"
            "sys.exit(int(os.environ.get('FAKE_EXIT', '0')))\n"
        )
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)

    CLAUDE_ARGS = [
        "-p", "hello", "--model", "claude-opus-5-5",
        "--output-format", "stream-json", "--verbose",
        "--settings", RELEASE_SETTINGS,
    ]

    def recorder_cmd(self, run_id="t1", claude_args=None):
        cmd = [sys.executable, str(RECORDER_PATH), "run", "--task-id", "39",
               "--role", "writer", "--records-dir", str(self.records)]
        if run_id is not None:
            cmd += ["--run-id", run_id]
        return cmd + ["--", "claude", *(claude_args or self.CLAUDE_ARGS)]

    def recorder_env(self, extra_env=None):
        env = {k: v for k, v in os.environ.items() if k not in rec.API_KEY_ENV}
        env["PATH"] = f"{self.bin}{os.pathsep}{env.get('PATH', '')}"
        env.update(extra_env or {})
        return env

    def run_recorder(self, extra_env=None, claude_args=None, run_id="t1"):
        return subprocess.run(
            self.recorder_cmd(run_id, claude_args),
            capture_output=True, text=True, env=self.recorder_env(extra_env),
        )

    def start_recorder(self, tag, run_id, sleep="0.5"):
        return subprocess.Popen(
            self.recorder_cmd(run_id),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=self.recorder_env({"FAKE_TAG": tag, "FAKE_SLEEP": sleep}),
        )

    def records_by_id(self):
        out = {}
        for path in self.records.glob("*.json"):
            out[path.name[: -len(".json")]] = json.loads(path.read_text())
        return out

    def stream_tags(self, run_id):
        text = (self.records / f"{run_id}.stream.jsonl").read_text()
        return [e["tag"] for e in rec.iter_events(text) if e.get("type") == "marker"]

    def test_runs_unchanged_tees_output_and_writes_record(self):
        result = self.run_recorder()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, STREAM, "stdout must pass through unchanged")
        self.assertEqual(json.loads(self.argv_log.read_text()), self.CLAUDE_ARGS)
        record = json.loads((self.records / "t1.json").read_text())
        self.assertEqual(record["schema"], rec.SCHEMA)
        self.assertEqual(record["task_id"], "39")
        self.assertEqual(record["role"], "writer")
        self.assertEqual(record["model"]["requested"]["value"], "claude-opus-5-5")
        self.assertEqual(record["tokens"]["cached_input"], 90000)
        self.assertEqual(record["cost"]["estimated_list_cost_usd"]["value"], 1.2345)
        self.assertIsInstance(record["duration_ms"]["wall"]["value"], int)
        self.assertFalse(record["auth"]["api_key_env_present"])
        self.assertEqual((self.records / "t1.stream.jsonl").read_text(), STREAM)

    def test_exit_code_is_propagated(self):
        result = self.run_recorder({"FAKE_EXIT": "3"})
        self.assertEqual(result.returncode, 3)
        record = json.loads((self.records / "t1.json").read_text())
        self.assertEqual(record["outcome"]["cli"]["value"], "error")
        self.assertEqual(record["outcome"]["cli"]["exit_code"], 3)

    def test_api_key_env_refuses_before_running(self):
        result = self.run_recorder({"ANTHROPIC_API_KEY": "sk-test"})
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.argv_log.exists(), "claude must not have been started")
        self.assertFalse((self.records / "t1.json").exists())

    def test_existing_record_is_not_overwritten(self):
        self.assertEqual(self.run_recorder().returncode, 0)
        before = (self.records / "t1.stream.jsonl").read_text()
        self.argv_log.unlink()
        self.assertEqual(self.run_recorder().returncode, 2)
        self.assertFalse(self.argv_log.exists())
        self.assertEqual((self.records / "t1.stream.jsonl").read_text(), before)

    def test_writer_without_release_settings_refuses_before_running(self):
        args = self.CLAUDE_ARGS[:-2]
        result = self.run_recorder(claude_args=args)
        self.assertEqual(result.returncode, 2)
        self.assertIn("release-settings.json", result.stderr)
        self.assertFalse(self.argv_log.exists(), "claude must not have been started")
        self.assertFalse(self.records.exists() and any(self.records.iterdir()))

    def test_sequential_default_ids_never_collide(self):
        for _ in range(3):
            self.assertEqual(self.run_recorder(run_id=None).returncode, 0)
        records = self.records_by_id()
        self.assertEqual(len(records), 3)
        for run_id, record in records.items():
            with self.subTest(run_id):
                self.assertEqual(record["run_id"], run_id)
                self.assertTrue(run_id.startswith("39-writer-"))
                self.assertEqual(
                    (self.records / f"{run_id}.stream.jsonl").read_text(), STREAM
                )

    def test_concurrent_default_ids_get_their_own_record_and_stream(self):
        procs = {tag: self.start_recorder(tag, None) for tag in ("a", "b", "c")}
        for proc in procs.values():
            proc.communicate(timeout=30)
            self.assertEqual(proc.returncode, 0)
        records = self.records_by_id()
        self.assertEqual(len(records), 3)
        tags = sorted(tag for run_id in records for tag in self.stream_tags(run_id))
        self.assertEqual(tags, ["a", "b", "c"], "each stream holds exactly one run")

    def test_concurrent_same_explicit_id_admits_exactly_one(self):
        procs = {tag: self.start_recorder(tag, "same") for tag in ("a", "b", "c")}
        codes = {}
        for tag, proc in procs.items():
            proc.communicate(timeout=30)
            codes[tag] = proc.returncode
        winners = [tag for tag, code in codes.items() if code == 0]
        self.assertEqual(len(winners), 1, codes)
        self.assertEqual(sorted(codes.values()), [0, 2, 2])
        self.assertEqual(self.stream_tags("same"), winners, "stream not shared or truncated")
        self.assertTrue((self.records / "same.stream.jsonl").read_text().endswith(STREAM))
        self.assertEqual(self.records_by_id()["same"]["run_id"], "same")

    def test_resumed_run_with_prior_result_records_the_checked_delta(self):
        root = Path(self.tmp.name)
        prior = root / "prior.stream.jsonl"
        prior.write_text(json.dumps(REAL_CORRECTION) + "\n")
        output = root / "c2.json"
        output.write_text(json.dumps(REAL_CORRECTION2) + "\n")
        args = ["--resume", REAL_SESSION, *self.CLAUDE_ARGS]
        cmd = self.recorder_cmd(claude_args=args)
        cmd[cmd.index("--"):cmd.index("--")] = ["--prior-result", str(prior)]
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            env=self.recorder_env({"FAKE_OUTPUT": str(output)}),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        record = json.loads((self.records / "t1.json").read_text())
        self.assertIs(record["resumed_session"]["value"], True)
        self.assertEqual(record["tokens"]["output"], 100)
        self.assertAlmostEqual(
            record["cost"]["estimated_list_cost_usd"]["value"], 0.5, places=9
        )
        self.assertEqual(
            record["session_cumulative"]["estimated_list_cost_usd"]["value"],
            4.0,
        )

    def test_unreadable_prior_result_refuses_before_running(self):
        cmd = self.recorder_cmd()
        cmd[cmd.index("--"):cmd.index("--")] = [
            "--prior-result", str(Path(self.tmp.name) / "missing.json")]
        result = subprocess.run(cmd, capture_output=True, text=True,
                                env=self.recorder_env())
        self.assertEqual(result.returncode, 2)
        self.assertIn("--prior-result", result.stderr)
        self.assertFalse(self.argv_log.exists(), "claude must not have been started")

    def test_unsafe_run_id_is_refused(self):
        for run_id in ("../escape", "a/b", ".hidden", ""):
            with self.subTest(run_id):
                result = self.run_recorder(run_id=run_id)
                self.assertEqual(result.returncode, 2)
        self.assertFalse(self.argv_log.exists())


SAFE_CLAUDE_INVESTIGATOR = [
    "claude", "-p", "find where the parser lives", "--model", "claude-sonnet-5-5",
    "--output-format", "stream-json", "--verbose", "--restricted", "--tools", "Read,Grep,Glob",
    "--disallowedTools", ",".join(rec.CLAUDE_DENIED_TOOLS),
    "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
    "--settings", '{"disableAllHooks":true}', "--setting-sources", "",
]
SAFE_CODEX_INVESTIGATOR = ["codex", "exec", "--json", "-m", "gpt-5.5", "-s", "read-only", "-"]


def _without(argv, flag, values=1):
    index = argv.index(flag)
    return argv[:index] + argv[index + 1 + values:]


def _replace(argv, flag, value):
    index = argv.index(flag)
    return argv[:index + 1] + [value] + argv[index + 2:]


class InvestigatorArgvGuard(unittest.TestCase):
    """Read-only investigator argv: an allowlist AND a deny list, parsed fail-closed."""

    def test_safe_claude_forms_are_accepted(self):
        safe = SAFE_CLAUDE_INVESTIGATOR
        cases = {
            "as documented": safe,
            "no tools at all": _replace(safe, "--tools", ""),
            "deny-list alias, space separated, plus extra denials": _without(safe, "--disallowedTools") + [
                "--disallowed-tools", " ".join(rec.CLAUDE_DENIED_TOOLS) + " WebFetch"],
            "equals forms": [a for a in safe if a not in ("--settings", '{"disableAllHooks":true}')]
            + ['--settings={"disableAllHooks":true}'],
            "plan mode added": safe + ["--permission-mode", "plan"],
        }
        for label, argv in cases.items():
            with self.subTest(label):
                rec.check_investigator(argv)
                self.assertEqual(rec.check_invocation(argv, "investigator", {}), "claude-sonnet-5-5")

    def test_unsafe_claude_forms_are_refused(self):
        safe = SAFE_CLAUDE_INVESTIGATOR
        deny = ",".join(rec.CLAUDE_DENIED_TOOLS)
        cases = {
            "allowlist only, no deny list": _without(safe, "--disallowedTools"),
            "deny list only, no allowlist": _without(safe, "--tools"),
            "allowlist widened with Write": _replace(safe, "--tools", "Read,Write"),
            "allowlist widened with Bash": _replace(safe, "--tools", "Read Bash"),
            "default tool set": _replace(safe, "--tools", "default"),
            "deny list misses Task": _replace(safe, "--disallowedTools", deny.replace(",Task", "")),
            "deny list misses Bash": _replace(safe, "--disallowedTools", deny.replace("Bash,", "")),
            "deny list misses PowerShell": _replace(safe, "--disallowedTools", deny.replace("PowerShell,", "")),
            "deny pattern instead of tool": _replace(safe, "--disallowedTools", deny.replace("Bash", "Bash(git *)")),
            "no strict MCP": [a for a in safe if a != "--strict-mcp-config"],
            "no MCP config": _without(safe, "--mcp-config"),
            "MCP config from a file": _replace(safe, "--mcp-config", "mcp.json"),
            "MCP config with a server": _replace(safe, "--mcp-config", '{"mcpServers":{"x":{"command":"sh"}}}'),
            "second MCP config value": safe + ["--mcp-config", '{"mcpServers":{}}'],
            "no hook guard": _without(safe, "--settings"),
            "hooks enabled": _replace(safe, "--settings", '{"disableAllHooks":false}'),
            "settings with more keys": _replace(safe, "--settings",
                                                '{"disableAllHooks":true,"permissions":{"allow":["Bash"]}}'),
            "settings from a file": _replace(safe, "--settings", RELEASE_SETTINGS),
            "setting sources missing": _without(safe, "--setting-sources"),
            "user and project settings load": _replace(safe, "--setting-sources", "user,project"),
            "allowedTools pre-approval": safe + ["--allowedTools", "Read"],
            "bypass permissions": safe + ["--dangerously-skip-permissions"],
            "allow bypass": safe + ["--allow-dangerously-skip-permissions"],
            "acceptEdits mode": safe + ["--permission-mode", "acceptEdits"],
            "bypass mode": safe + ["--permission-mode=bypassPermissions"],
            "no --restricted file confinement": [a for a in safe if a != "--restricted"],
            "--restricted given twice": safe + ["--restricted"],
            "--restricted with a value": [a if a != "--restricted" else "--restricted=false" for a in safe],
            "extra directory": safe + ["--add-dir", "/"],
            "extra directory, equals form": safe + ["--add-dir=/Users"],
            "extra directory without --restricted": [a for a in safe if a != "--restricted"] + ["--add-dir", "."],
            "plugin": safe + ["--plugin-dir", "plugins"],
            "agents": safe + ["--agents", '{"w":{"description":"d","prompt":"p"}}'],
            "resume": safe + ["--resume", "abc"],
            "continue": safe + ["-c"],
            "own worktree": safe + ["--worktree"],
            "unknown option": safe + ["--future-flag"],
            "duplicate tools": safe + ["--tools", "Read"],
            "duplicate deny list through the alias": safe + ["--disallowed-tools", deny],
            "prompt swallowed by a variadic option": ["claude", "-p"] + safe[3:safe.index("Read,Grep,Glob") + 1]
            + ["the prompt"] + safe[safe.index("Read,Grep,Glob") + 1:],
            "argument separator": safe + ["--", "prompt"],
            "subcommand": ["claude", "mcp"] + safe[1:],
        }
        for label, argv in cases.items():
            with self.subTest(label), self.assertRaises(rec.RefusedError):
                rec.check_investigator(argv)

    def test_safe_codex_forms_are_accepted(self):
        for argv in (SAFE_CODEX_INVESTIGATOR,
                     ["codex", "exec", "--json", "--model", "gpt-5.5", "--sandbox=read-only", "prompt"]):
            with self.subTest(argv):
                rec.check_investigator(argv)
                self.assertEqual(rec.check_invocation(argv, "investigator", {}), "gpt-5.5")

    def test_unsafe_codex_forms_are_refused(self):
        safe = SAFE_CODEX_INVESTIGATOR
        cases = {
            "no sandbox flag": _without(safe, "-s"),
            "workspace write": _replace(safe, "-s", "workspace-write"),
            "full access": _replace(safe, "-s", "danger-full-access"),
            "conflicting sandbox flags": safe + ["--sandbox", "workspace-write"],
            "repeated sandbox flag": safe + ["-s", "read-only"],
            "full auto": safe + ["--full-auto"],
            "bypass": safe + ["--dangerously-bypass-approvals-and-sandbox"],
            "sandbox config override": safe + ["-c", 'sandbox_mode="danger-full-access"'],
            "model by config override only": ["codex", "exec", "--json", "-c", "model=gpt-5.5", "-s", "read-only"],
            "profile": safe + ["--profile", "wide"],
            "other working root": safe + ["-C", "/"],
            "extra writable dir": safe + ["--add-dir", "/tmp"],
            "writes a file": safe + ["-o", "last.txt"],
            "resumed thread": ["codex", "exec", "resume", "--json", "-m", "gpt-5.5", "-s", "read-only"],
            "not exec first": ["codex", "--json", "exec", "-m", "gpt-5.5", "-s", "read-only"],
            "unknown option": safe + ["--future-flag"],
        }
        for label, argv in cases.items():
            with self.subTest(label), self.assertRaises(rec.RefusedError):
                rec.check_investigator(argv)

    def test_other_roles_are_not_held_to_the_investigator_argv(self):
        argv = ["claude", "-p", "x", "--model", "claude-sonnet-5-5", "--output-format", "json"]
        self.assertEqual(rec.check_invocation(argv, "reviewer", {}), "claude-sonnet-5-5")
        with self.assertRaises(rec.RefusedError):
            rec.check_investigator(argv)


def _git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                           "-c", "commit.gpgsign=false", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


class InvestigatorIsolatedRun(unittest.TestCase):
    """A real temporary Git repo: the investigator reads the base commit, never the writer's edits."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name).resolve()
        self.repo = root / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-q")
        (self.repo / "a.txt").write_text("base\n")
        _git(self.repo, "add", "a.txt")
        _git(self.repo, "commit", "-q", "-m", "base")
        self.base = _git(self.repo, "rev-parse", "HEAD")
        # The writer is mid-edit: a modified tracked file and an untracked one.
        (self.repo / "a.txt").write_text("writer edit, uncommitted\n")
        (self.repo / "new.txt").write_text("untracked writer file\n")
        self.bin = root / "bin"
        self.bin.mkdir()
        self.log = root / "seen.json"
        for name in ("claude", "codex"):
            fake = self.bin / name
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, sys, time\n"
                "record = os.environ.get('FAKE_RECORD')\n"
                "running = None\n"
                "for _ in range(200):\n"
                "    try:\n"
                "        running = json.load(open(record))\n"
                "        if running.get('status') == 'running':\n"
                "            break\n"
                "    except (OSError, ValueError, TypeError):\n"
                "        pass\n"
                "    time.sleep(0.02)\n"
                "seen = {'cwd': os.getcwd(), 'a': open('a.txt').read(), 'new': os.path.exists('new.txt'),\n"
                "        'running_repository': (running or {}).get('repository'), 'pid': os.getpid()}\n"
                f"open({str(self.log)!r} + '.tmp', 'w').write(json.dumps(seen))\n"
                f"os.replace({str(self.log)!r} + '.tmp', {str(self.log)!r})\n"
                "time.sleep(float(os.environ.get('FAKE_SLEEP', '0')))\n"
                f"sys.stdout.write({STREAM!r})\n"
                "sys.exit(int(os.environ.get('FAKE_EXIT', '0')))\n")
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items() if k not in rec.API_KEY_ENV + rec.CODEX_API_KEY_ENV}
        env.pop("TASK_WORKFLOW_DIR", None)
        env["PATH"] = f"{self.bin}{os.pathsep}{env.get('PATH', '')}"
        env["FAKE_RECORD"] = str(self.repo / "runs" / "inv.json")
        env.update(extra)
        return env

    def run_recorder(self, wrapped, *, role="investigator", base=None, extra=(), **env):
        cmd = [sys.executable, str(RECORDER_PATH), "run", "--task-id", "39", "--role", role,
               "--run-id", "inv", "--records-dir", "runs", *extra]
        if base is not None:
            cmd += ["--base-commit", base]
        return subprocess.run(cmd + ["--", *wrapped], cwd=self.repo, capture_output=True, text=True,
                              env=self.env(**env))

    def worktrees(self):
        return [line for line in _git(self.repo, "worktree", "list", "--porcelain").splitlines()
                if line.startswith("worktree ")]

    def assert_writer_untouched(self):
        self.assertEqual((self.repo / "a.txt").read_text(), "writer edit, uncommitted\n")
        self.assertTrue((self.repo / "new.txt").is_file())
        self.assertEqual(self.worktrees(), [f"worktree {self.repo}"])

    def test_claude_investigator_reads_the_clean_base_commit_and_the_worktree_is_removed(self):
        result = self.run_recorder(SAFE_CLAUDE_INVESTIGATOR, base=self.base[:12])
        self.assertEqual(result.returncode, 0, result.stderr)
        seen = json.loads(self.log.read_text())
        cwd = Path(seen["cwd"]).resolve()
        self.assertNotEqual(cwd, self.repo)
        self.assertNotIn(self.repo, cwd.parents)
        self.assertEqual((seen["a"], seen["new"]), ("base\n", False))  # never the writer's edits
        self.assertFalse(cwd.exists(), "the owned worktree is removed after the run")
        record = json.loads((self.repo / "runs" / "inv.json").read_text())
        repo_fields = record["repository"]
        self.assertEqual((repo_fields["base_commit"], repo_fields["read_commit"]), (self.base, self.base))
        self.assertEqual(Path(repo_fields["isolated_worktree"]).resolve(), cwd)
        self.assertIs(repo_fields["worktree_clean_at_launch"], True)
        self.assertEqual((repo_fields["source"], Path(repo_fields["repository"])), ("recorder.git", self.repo))
        self.assertEqual(seen["running_repository"]["read_commit"], self.base)  # the live record says so too
        # The record and stream stay in the original records directory, as an absolute path.
        self.assertEqual(Path(record["stream_path"]), self.repo / "runs" / "inv.stream.jsonl")
        self.assertEqual((self.repo / "runs" / "inv.stream.jsonl").read_text(), STREAM)
        self.assert_writer_untouched()

    def test_failed_codex_investigator_is_still_cleaned_up(self):
        result = self.run_recorder(SAFE_CODEX_INVESTIGATOR, base=self.base, FAKE_EXIT="3")
        self.assertEqual(result.returncode, 3)
        seen = json.loads(self.log.read_text())
        self.assertEqual(seen["a"], "base\n")
        self.assertFalse(Path(seen["cwd"]).exists())
        record = json.loads((self.repo / "runs" / "inv.json").read_text())
        self.assertEqual((record["provider"], record["repository"]["read_commit"]), ("codex", self.base))
        self.assert_writer_untouched()

    def test_launch_failure_records_the_repository_and_cleans_up(self):
        missing = str(Path(self.tmp.name) / "nowhere" / "claude")
        result = self.run_recorder([missing, *SAFE_CLAUDE_INVESTIGATOR[1:]], base=self.base)
        self.assertEqual(result.returncode, 127)
        record = json.loads((self.repo / "runs" / "inv.json").read_text())
        self.assertEqual(record["status"], "launch_failed")
        self.assertEqual(record["repository"]["base_commit"], self.base)
        self.assertFalse(Path(record["repository"]["isolated_worktree"]).exists())
        self.assert_writer_untouched()

    def test_base_commit_is_explicit_and_resolved_before_anything_starts(self):
        tree = _git(self.repo, "rev-parse", "HEAD^{tree}")
        other = Path(self.tmp.name) / "other"
        other.mkdir()
        _git(other, "init", "-q")
        _git(other, "commit", "-q", "--allow-empty", "-m", "elsewhere")
        foreign = _git(other, "rev-parse", "HEAD")
        for label, base in (("missing", None), ("a ref, not a SHA", "HEAD"), ("unknown", "deadbeefdeadbeef"),
                            ("not a commit", tree), ("another repository's commit", foreign)):
            with self.subTest(label):
                result = self.run_recorder(SAFE_CLAUDE_INVESTIGATOR, base=base)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("refused", result.stderr)
                self.assertFalse(self.log.exists(), "nothing was launched")
                self.assertFalse((self.repo / "runs").exists(), "nothing was reserved")
        self.assert_writer_untouched()

    def test_declared_base_commit_must_match(self):
        tickets = self.repo / ".workflow" / "tickets"
        tickets.mkdir(parents=True)
        _git(self.repo, "commit", "-q", "--allow-empty", "-m", "later")  # HEAD moves; the declared base does not
        later = _git(self.repo, "rev-parse", "HEAD")
        (tickets / "39.json").write_text(json.dumps({"id": 39, "base_commit": self.base}))
        result = self.run_recorder(SAFE_CLAUDE_INVESTIGATOR, base=later)
        self.assertEqual(result.returncode, 2)
        self.assertIn("declares base_commit", result.stderr)
        self.assertFalse(self.log.exists())
        result = self.run_recorder(SAFE_CLAUDE_INVESTIGATOR, base=self.base)
        self.assertEqual(result.returncode, 0, result.stderr)
        record = json.loads((self.repo / "runs" / "inv.json").read_text())
        self.assertIn("matches base_commit declared in", record["repository"]["base_commit_source"])
        self.assertEqual(json.loads((tickets / "39.json").read_text()), {"id": 39, "base_commit": self.base})

    def test_unsafe_investigator_argv_starts_nothing(self):
        for argv in (_without(SAFE_CLAUDE_INVESTIGATOR, "--disallowedTools"),
                     _replace(SAFE_CODEX_INVESTIGATOR, "-s", "workspace-write")):
            with self.subTest(argv[0]):
                result = self.run_recorder(argv, base=self.base)
                self.assertEqual(result.returncode, 2)
                self.assertFalse(self.log.exists())
                self.assertFalse((self.repo / "runs").exists())
        self.assert_writer_untouched()

    def test_non_investigator_runs_are_unchanged(self):
        reviewer = ["claude", "-p", "review", "--model", "claude-sonnet-5-5", "--output-format", "stream-json",
                    "--verbose"]
        for extra in (("--base-commit", self.base), ("--project-root", str(self.repo))):
            result = self.run_recorder(reviewer, role="reviewer", extra=extra)
            self.assertEqual(result.returncode, 2)
            self.assertIn("only to investigator", result.stderr)
        result = self.run_recorder(reviewer, role="reviewer")
        self.assertEqual(result.returncode, 0, result.stderr)
        seen = json.loads(self.log.read_text())
        self.assertEqual((Path(seen["cwd"]).resolve(), seen["a"]), (self.repo, "writer edit, uncommitted\n"))
        record = json.loads((self.repo / "runs" / "inv.json").read_text())
        self.assertNotIn("repository", record)
        self.assertEqual(record["stream_path"], str(Path("runs") / "inv.stream.jsonl"))  # relative, as before

    def test_worktree_context_removes_only_its_own_worktree_on_interrupt(self):
        sibling = Path(self.tmp.name) / "sibling"
        _git(self.repo, "worktree", "add", "-q", "--detach", str(sibling), self.base)
        with self.assertRaises(KeyboardInterrupt):
            with rec.investigator_worktree(self.repo, self.base) as path:
                owned = path
                self.assertEqual((path / "a.txt").read_text(), "base\n")
                raise KeyboardInterrupt
        self.assertFalse(owned.exists())
        self.assertFalse(owned.parent.exists())
        self.assertTrue(sibling.is_dir(), "another worktree is never removed or pruned")
        self.assertEqual(sorted(self.worktrees()), sorted([f"worktree {self.repo}", f"worktree {sibling.resolve()}"]))

    def git_env_config(self, **pairs):
        """Git configuration through the environment only: no repository or global config is written."""
        env = {"GIT_CONFIG_COUNT": str(len(pairs))}
        for index, (key, value) in enumerate(pairs.items()):
            env[f"GIT_CONFIG_KEY_{index}"], env[f"GIT_CONFIG_VALUE_{index}"] = key, value
        return env

    def test_configured_checkout_filter_is_refused_before_the_worktree_exists(self):
        (self.repo / ".gitattributes").write_text("*.txt filter=mark\n")
        _git(self.repo, "add", ".gitattributes")
        _git(self.repo, "commit", "-q", "-m", "attributes")
        base = _git(self.repo, "rev-parse", "HEAD")
        marker = Path(self.tmp.name) / "smudge-ran"
        config = self.git_env_config(**{"filter.mark.smudge": f"touch '{marker}'; cat"})
        # The regression is real: a plain checkout of this commit runs the smudge command.
        probe = Path(self.tmp.name) / "probe"
        subprocess.run(["git", "worktree", "add", "-q", "--detach", str(probe), base], cwd=self.repo,
                       check=True, capture_output=True, env={**os.environ, **config})
        self.assertTrue(marker.exists(), "the configured smudge command runs on checkout")
        _git(self.repo, "worktree", "remove", "--force", str(probe))
        marker.unlink()

        result = self.run_recorder(SAFE_CLAUDE_INVESTIGATOR, base=base, **config)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("filter driver(s) mark", result.stderr)
        self.assertNotIn(str(marker), result.stderr, "the command itself is never reported")
        self.assertFalse(marker.exists(), "no checkout ran the filter")
        self.assertFalse(self.log.exists(), "no provider was launched")
        self.assertFalse((self.repo / "runs").exists(), "refused before anything was reserved")
        self.assertEqual(self.worktrees(), [f"worktree {self.repo}"])

    def test_filter_commands_of_every_kind_are_refused_also_at_verification(self):
        cases = {"smudge": "filter.x.smudge", "clean": "filter.x.clean", "process": "filter.lfs.process"}
        for label, key in cases.items():
            with self.subTest(label), mock.patch.dict(os.environ, self.git_env_config(**{key: "cat"})):
                with self.assertRaises(rec.RefusedError):
                    rec.refuse_filter_drivers(self.repo)
                with self.assertRaises(rec.RefusedError):
                    rec.verify_investigator_worktree(self.repo, {"base_commit": self.base})
        # Driver settings without a command (or with an empty one) run nothing and are accepted.
        with mock.patch.dict(os.environ, self.git_env_config(**{"filter.x.required": "true",
                                                                "filter.y.smudge": ""})):
            rec.refuse_filter_drivers(self.repo)

    def test_executable_fsmonitor_never_runs_for_the_recorders_git_calls(self):
        marker = Path(self.tmp.name) / "fsmonitor-ran"
        hook = Path(self.tmp.name) / "fsmonitor.sh"
        hook.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 1\n")
        hook.chmod(0o755)
        config = self.git_env_config(**{"core.fsmonitor": str(hook)})
        subprocess.run(["git", "status", "--porcelain"], cwd=self.repo, capture_output=True,
                       env={**os.environ, **config})
        self.assertTrue(marker.exists(), "a plain git status runs the configured fsmonitor")
        marker.unlink()
        result = self.run_recorder(SAFE_CLAUDE_INVESTIGATOR, base=self.base, **config)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(marker.exists())
        self.assert_writer_untouched()

    def wait_for_running_child(self, recorder):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            self.assertIsNone(recorder.poll(), "the recorder ended before its child was running")
            if self.log.exists():
                seen = json.loads(self.log.read_text())
                if (seen.get("running_repository") or {}).get("read_commit") == self.base:
                    return seen
            time.sleep(0.05)
        self.fail("the fake investigator never saw a running record with its read commit")

    def test_sigterm_and_sighup_stop_the_child_remove_the_worktree_and_record_the_interruption(self):
        sibling = Path(self.tmp.name) / "sibling"
        _git(self.repo, "worktree", "add", "-q", "--detach", str(sibling), self.base)
        for signum in (signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal.Signals(signum).name):
                recorder = subprocess.Popen(
                    [sys.executable, str(RECORDER_PATH), "run", "--task-id", "39", "--role", "investigator",
                     "--run-id", "inv", "--records-dir", "runs", "--base-commit", self.base, "--",
                     *SAFE_CLAUDE_INVESTIGATOR], cwd=self.repo, stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE, text=True, env=self.env(FAKE_SLEEP="60"),
                    # an ignored SIGHUP (a test runner under nohup) would be inherited and stay ignored
                    preexec_fn=lambda: signal.signal(signal.SIGHUP, signal.SIG_DFL))
                try:
                    seen = self.wait_for_running_child(recorder)
                    recorder.send_signal(signum)
                    _, stderr = recorder.communicate(timeout=30)
                finally:
                    if recorder.poll() is None:
                        recorder.kill()
                        recorder.wait()
                self.assertEqual(recorder.returncode, 128 + signum, stderr)
                with self.assertRaises(ProcessLookupError, msg="the investigator child was stopped"):
                    os.kill(seen["pid"], 0)
                self.assertFalse(Path(seen["cwd"]).exists(), "the owned worktree is removed")
                self.assertFalse(Path(seen["cwd"]).parent.exists())
                record = json.loads((self.repo / "runs" / "inv.json").read_text())
                self.assertEqual((record["status"], record["interrupted_by"]),
                                 ("interrupted", signal.Signals(signum).name))
                self.assertEqual(record["exit_code"], -signal.SIGTERM)  # the recorder terminated its child
                self.assertIs(record["isolated_worktree_removed"], True)
                self.assertEqual(record["repository"]["read_commit"], self.base)
                self.assertEqual(sorted(self.worktrees()),
                                 sorted([f"worktree {self.repo}", f"worktree {sibling.resolve()}"]))
                self.assertTrue(sibling.is_dir(), "an unowned worktree is never touched")
                self.assertEqual((self.repo / "a.txt").read_text(), "writer edit, uncommitted\n")
                shutil.rmtree(self.repo / "runs")
                self.log.unlink()

    def test_signal_during_preparation_cleans_up_and_restores_the_previous_handlers(self):
        def previous_term(signum, frame):
            pass

        def previous_hup(signum, frame):
            pass

        old = signal.signal(signal.SIGTERM, previous_term), signal.signal(signal.SIGHUP, previous_hup)
        self.addCleanup(signal.signal, signal.SIGTERM, old[0])
        self.addCleanup(signal.signal, signal.SIGHUP, old[1])
        records = self.repo / "runs"

        def signalled_while_verifying(path, repository):
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(1)  # the handler raises here, before the provider could start

        with mock.patch.dict(os.environ, self.env(), clear=True), \
                mock.patch.object(rec, "verify_investigator_worktree", side_effect=signalled_while_verifying):
            code = rec.main(["run", "--task-id", "39", "--role", "investigator", "--run-id", "inv",
                             "--records-dir", str(records), "--project-root", str(self.repo),
                             "--base-commit", self.base, "--", *SAFE_CLAUDE_INVESTIGATOR])
        self.assertEqual(code, 128 + signal.SIGTERM)
        self.assertIs(signal.getsignal(signal.SIGTERM), previous_term)
        self.assertIs(signal.getsignal(signal.SIGHUP), previous_hup)
        record = json.loads((records / "inv.json").read_text())
        self.assertEqual((record["status"], record["interrupted_by"], record["exit_code"]),
                         ("interrupted", "SIGTERM", None))
        self.assertIs(record["isolated_worktree_removed"], True)
        self.assertFalse(self.log.exists(), "no provider was launched")
        self.assert_writer_untouched()

    def test_global_checkout_filter_is_not_loaded_or_executed(self):
        (self.repo / ".gitattributes").write_text("*.txt filter=mark\n")
        _git(self.repo, "add", ".gitattributes")
        _git(self.repo, "commit", "-q", "-m", "attributes")
        base = _git(self.repo, "rev-parse", "HEAD")
        marker = Path(self.tmp.name) / "global-filter-ran"
        global_config = Path(self.tmp.name) / "global.gitconfig"
        global_config.write_text(f'[filter "mark"]\n\tsmudge = touch {marker}; cat\n')
        result = self.run_recorder(SAFE_CLAUDE_INVESTIGATOR, base=base,
                                   GIT_CONFIG_GLOBAL=str(global_config))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(marker.exists())
        self.assertIn("smudge", global_config.read_text(), "user configuration is preserved")
        self.assertEqual(json.loads(self.log.read_text())["a"], "base\n")
        self.assert_writer_untouched()

    def test_signal_during_process_creation_stops_the_bound_child(self):
        spawned = []
        real_popen = subprocess.Popen

        def signal_after_creation(argv, *args, **kwargs):
            process = real_popen(argv, *args, **kwargs)
            if Path(argv[0]).name == "claude":
                spawned.append(process)
                os.kill(os.getpid(), signal.SIGTERM)
            return process

        records = self.repo / "runs"
        with mock.patch.dict(os.environ, self.env(FAKE_SLEEP="30"), clear=True), \
                mock.patch.object(rec.subprocess, "Popen", side_effect=signal_after_creation):
            code = rec.main(["run", "--task-id", "39", "--role", "investigator", "--run-id", "inv",
                             "--records-dir", str(records), "--project-root", str(self.repo),
                             "--base-commit", self.base, "--", *SAFE_CLAUDE_INVESTIGATOR])
        self.assertEqual(code, 128 + signal.SIGTERM)
        self.assertEqual(len(spawned), 1)
        self.assertIsNotNone(spawned[0].poll(), "the created child was stopped and waited for")
        record = json.loads((records / "inv.json").read_text())
        self.assertEqual(record["status"], "interrupted")
        self.assertTrue(record["isolated_worktree_removed"])
        self.assert_writer_untouched()

    def test_imported_investigator_record_keeps_the_commit_unknown(self):
        stream = Path(self.tmp.name) / "old.stream.jsonl"
        stream.write_text(STREAM)
        out = Path(self.tmp.name) / "old.json"
        self.assertEqual(rec.main(["parse", "--task-id", "39", "--role", "investigator", "--stream", str(stream),
                                   "--out", str(out)]), 0)
        repo_fields = json.loads(out.read_text())["repository"]
        self.assertEqual((repo_fields["base_commit"], repo_fields["read_commit"], repo_fields["source"]),
                         (None, None, None))
        self.assertIn("not observed", repo_fields["unknown_reason"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
