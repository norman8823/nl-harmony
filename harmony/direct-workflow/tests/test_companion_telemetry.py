#!/usr/bin/env python3
"""Companion telemetry: Codex recording, live snapshots, attribution, guard path.

Offline: fake `claude`/`codex` binaries on PATH, counters copied from real
probe output (.workflow/evidence/companion-v01/probes; numbers only).

Run:  python3 direct-workflow/tests/test_companion_telemetry.py
"""

from __future__ import annotations

import datetime as dt
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(PACKAGE_DIR))

import direct_run_recorder as rec  # noqa: E402
import run_observability as obs  # noqa: E402

RECORDER = PACKAGE_DIR / "direct_run_recorder.py"
RELEASE_SETTINGS = str(PACKAGE_DIR / "release-settings.json")

# Real `codex exec --json` 0.162.0 output shape (probe, 2026-10-08).
CODEX_USAGE = {"input_tokens": 15242, "cached_input_tokens": 12288, "cache_write_input_tokens": 0,
               "output_tokens": 5, "reasoning_output_tokens": 0}
CODEX_EVENTS = [
    {"type": "thread.started", "thread_id": "01a11d16-4bb0-7910-8884-0a785cb43a38"},
    {"type": "turn.started"},
    {"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": "OK"}},
    {"type": "turn.completed", "usage": CODEX_USAGE},
]
CODEX_ARGV = ["codex", "exec", "--json", "--sandbox", "read-only", "-m", "gpt-5.5", "-"]


def claude_assistant(mid, model, usage, *, ts, parent=None, content=None):
    return {"type": "assistant", "timestamp": ts, "parent_tool_use_id": parent,
            "message": {"id": mid, "model": model, "content": content or [{"type": "text", "text": "x"}],
                        "usage": dict(zip(("input_tokens", "cache_creation_input_tokens",
                                           "cache_read_input_tokens", "output_tokens"), usage))}}


FABLE = "claude-fable-5-1"
CLAUDE_EVENTS = [
    {"type": "system", "subtype": "init", "model": FABLE, "session_id": "s-1"},
    # One API message arrives twice; output grows while it streams.
    claude_assistant("m1", FABLE, (10, 9479, 13803, 4), ts="2026-10-08T20:00:01Z",
                     content=[{"type": "tool_use", "name": "Read", "id": "t1", "input": {}}]),
    claude_assistant("m1", FABLE, (10, 9479, 13803, 40), ts="2026-10-08T20:00:02Z"),
    {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1"}]}},
    # A tool loop re-sends the whole prompt: context grows, cumulative grows faster.
    claude_assistant("m2", FABLE, (5, 600, 23282, 30), ts="2026-10-08T20:00:05Z"),
    # A subagent message counts toward cumulative but is not the main context.
    claude_assistant("m3", "claude-haiku-4-5-20251001", (3, 0, 1000, 7), ts="2026-10-08T20:00:06Z", parent="t9"),
    {"type": "result", "subtype": "success", "is_error": False, "session_id": "s-1", "timestamp": "2026-10-08T20:00:07Z",
     "usage": {"input_tokens": 18, "cache_creation_input_tokens": 10079, "cache_read_input_tokens": 38085,
               "output_tokens": 77},
     "modelUsage": {FABLE: {"inputTokens": 15, "cacheCreationInputTokens": 10079, "cacheReadInputTokens": 37085,
                            "outputTokens": 70, "contextWindow": 200000},
                    "claude-haiku-4-5-20251001": {"inputTokens": 3, "cacheCreationInputTokens": 0,
                                                  "cacheReadInputTokens": 1000, "outputTokens": 7,
                                                  "contextWindow": 200000}}},
]


class CodexInvocation(unittest.TestCase):
    def test_accepts_exec_json_with_explicit_model(self):
        for role in ("planner", "reviewer", "investigator", "other"):
            self.assertEqual(rec.check_invocation(CODEX_ARGV, role, {}), "gpt-5.5")
        self.assertEqual(rec.check_invocation(["codex", "exec", "--json", "-c", 'model="o9"', "x"], "reviewer", {}),
                         "o9")

    def test_refusals(self):
        cases = {
            "writer is an Opus Claude role": (CODEX_ARGV, "writer", {}),
            "correction is an Opus Claude role": (CODEX_ARGV, "correction", {}),
            "interactive": (["codex", "--json", "-m", "m"], "reviewer", {}),
            "text output": (["codex", "exec", "-m", "m", "x"], "reviewer", {}),
            "implicit model": (["codex", "exec", "--json", "x"], "reviewer", {}),
            "api key env": (CODEX_ARGV, "reviewer", {"CODEX_API_KEY": "k"}),
            "other binary": (["python3", "exec", "--json"], "reviewer", {}),
        }
        for label, (argv, role, env) in cases.items():
            with self.subTest(label), self.assertRaises(rec.RefusedError):
                rec.check_invocation(argv, role, env)

    def test_claude_model_choice_preserves_writer_guard(self):
        sonnet = ["claude", "-p", "x", "--model", "claude-sonnet-5-5", "--output-format", "json",
                  "--settings", RELEASE_SETTINGS]
        self.assertEqual(rec.check_invocation(sonnet, "writer", {}), "claude-sonnet-5-5")
        with self.assertRaises(rec.RefusedError):
            rec.check_invocation(sonnet[:-2] + ["--model", "claude-opus-5-5"], "writer", {})


class CodexRecord(unittest.TestCase):
    def build(self, events=CODEX_EVENTS, argv=CODEX_ARGV, role="reviewer"):
        return rec.build_record(task_id="1", run_id="r", role=role, model_requested="gpt-5.5", events=events,
                                exit_code=0, wall_duration_ms=1000, started_at="2026-10-08T20:00:00Z",
                                ended_at=None, argv=argv, stream_path=None, api_key_env_present=False,
                                provider="codex")

    def test_counters_map_without_double_counting_cached_or_reasoning(self):
        record = self.build()
        tokens = record["tokens"]
        self.assertEqual((tokens["uncached_input"], tokens["cache_creation_input"], tokens["cached_input"],
                          tokens["output"], tokens["reasoning_output"]), (2954, 0, 12288, 5, 0))
        self.assertEqual(obs.run_tokens(record)["complete"], True)
        self.assertEqual(obs.inspect_run(record)["derived"]["processed_input_tokens"]["value"], 15242)
        self.assertEqual(record["schema"], rec.SCHEMA)
        self.assertEqual(record["provider"], "codex")
        self.assertEqual(record["outcome"]["cli"]["value"], "success")
        self.assertEqual(record["session_id"]["value"], "01a11d16-4bb0-7910-8884-0a785cb43a38")

    def test_unreported_fields_stay_unknown(self):
        record = self.build()
        self.assertIsNone(record["model"]["reported"]["value"])
        self.assertIn("no model identity", record["model"]["reported"]["unknown_reason"])
        self.assertIn("model.reported", record["unknowns"])
        self.assertIsNone(record["cost"]["estimated_list_cost_usd"]["value"])
        self.assertIsNone(record["telemetry"]["context"]["tokens"])
        self.assertIn("does not report context", record["telemetry"]["context"]["basis"])

    def test_cache_writes_resume_and_multi_turn_leave_tokens_unknown(self):
        written = [*CODEX_EVENTS[:-1], {"type": "turn.completed", "usage": {**CODEX_USAGE, "cache_write_input_tokens": 9}}]
        self.assertIsNone(self.build(written)["tokens"]["uncached_input"])
        resumed = self.build(argv=["codex", "exec", "--json", "-m", "m", "resume", "abc"])
        self.assertIsNone(resumed["tokens"]["output"])
        self.assertIn("resumed", resumed["tokens"]["cross_check"])
        twice = self.build([*CODEX_EVENTS, CODEX_EVENTS[-1]])
        self.assertIsNone(twice["tokens"]["output"])
        self.assertIsNone(self.build(argv=None)["tokens"]["output"])  # parse without argv: resume unknown

    def test_telemetry_follows_the_invocation_scope(self):
        resumed_argv = ["codex", "exec", "--json", "-m", "m", "resume", "old"]
        fresh = self.build()["telemetry"]["cumulative"]
        self.assertEqual((fresh["processed_input"], fresh["output"], fresh["provisional"]), (15242, 5, False))
        self.assertNotIn("scope_unverified", fresh)
        cases = {
            "resumed": (CODEX_EVENTS, resumed_argv, "resumed thread", 1),
            "argv unknown, single turn": (CODEX_EVENTS, None, "unknown (no argv)", 1),
            "multi-turn": ([*CODEX_EVENTS, CODEX_EVENTS[-1]], CODEX_ARGV, "2 turn.completed", 2),
        }
        for label, (events, argv, reason, turns) in cases.items():
            with self.subTest(label):
                record = self.build(events, argv)
                cum = record["telemetry"]["cumulative"]
                # Unknown, never zero, and not marked authoritative or merely provisional.
                for name in ("uncached_input", "cached_input", "output", "reasoning_output", "processed_input"):
                    self.assertIsNone(cum[name], name)
                self.assertIsNone(cum["provisional"])
                self.assertIn(reason, cum["basis"])
                self.assertNotIn("as reported", cum["basis"])
                self.assertEqual((cum["scope_unverified"]["processed_input"], cum["scope_unverified"]["output"],
                                  cum["scope_unverified"]["turns"]), (15242, 5, turns))
                self.assertTrue(all(p["processed_input"] is None and p["output"] is None
                                    for p in record["telemetry"]["series"]))
                self.assertIsNone(record["tokens"]["output"])  # record and telemetry agree
                self.assertIsNone(obs.card_metrics(obs.summarize_task([record], "1"))["total_tokens"])

    def test_parse_built_codex_record_has_unknown_scope_telemetry(self):
        with tempfile.TemporaryDirectory() as tmp:
            stream = Path(tmp) / "x.stream.jsonl"
            stream.write_text("".join(json.dumps(e) + "\n" for e in CODEX_EVENTS))
            out = Path(tmp) / "x.json"
            self.assertEqual(rec.main(["parse", "--task-id", "1", "--role", "reviewer", "--model", "gpt-5.5",
                                       "--stream", str(stream), "--out", str(out)]), 0)
            cum = json.loads(out.read_text())["telemetry"]["cumulative"]
            self.assertIsNone(cum["processed_input"])
            self.assertIn("unknown (no argv)", cum["basis"])
            self.assertEqual(cum["scope_unverified"]["processed_input"], 15242)

    def test_parse_detects_codex_and_refuses_a_codex_writer(self):
        with tempfile.TemporaryDirectory() as tmp:
            stream = Path(tmp) / "x.stream.jsonl"
            stream.write_text("".join(json.dumps(e) + "\n" for e in CODEX_EVENTS))
            out = Path(tmp) / "x.json"
            self.assertEqual(rec.main(["parse", "--task-id", "1", "--role", "planner", "--model", "gpt-5.5",
                                       "--stream", str(stream), "--out", str(out)]), 0)
            self.assertEqual(json.loads(out.read_text())["provider"], "codex")
            self.assertEqual(rec.main(["parse", "--task-id", "1", "--role", "writer",
                                       "--stream", str(stream), "--out", str(out)]), 2)


class ClaudeTelemetry(unittest.TestCase):
    def test_context_is_latest_main_prompt_and_cumulative_dedupes_messages(self):
        t = rec.Telemetry("claude")
        for event in CLAUDE_EVENTS[:-1]:
            t.feed(event, "2026-10-08T20:10:00Z")
        snap = t.snapshot()
        self.assertEqual(snap["context"]["tokens"], 5 + 600 + 23282)  # m2, not the subagent's m3
        self.assertIsNone(snap["context"]["window"])  # unknown until the result reports it
        self.assertIsNone(snap["context"]["percent"])
        cum = snap["cumulative"]
        self.assertTrue(cum["provisional"])
        self.assertEqual(cum["processed_input"], (10 + 9479 + 13803) + (5 + 600 + 23282) + (3 + 0 + 1000))
        self.assertEqual(cum["output"], 40 + 30 + 7)  # m1 counted once, at its largest
        self.assertEqual(snap["last_measured_at"], "2026-10-08T20:00:06Z")
        self.assertEqual(snap["measured_at_basis"], "provider event timestamp")
        self.assertEqual(snap["models_seen"], [FABLE, "claude-haiku-4-5-20251001"])
        self.assertEqual(snap["activity"], {"tool:Read": 1})

        t.feed(CLAUDE_EVENTS[-1], "2026-10-08T20:10:01Z")
        final = t.snapshot()
        self.assertFalse(final["cumulative"]["provisional"])
        self.assertEqual(final["cumulative"]["output"], 77)  # the result's usage, as reported
        self.assertEqual(final["context"]["window"], 200000)
        self.assertEqual(final["context"]["percent"], round(100 * 23887 / 200000, 1))
        self.assertEqual(final["phase"], "done")
        self.assertEqual([p["context_tokens"] for p in final["series"]][:2], [23292, 23292])

    def test_series_is_capped_and_keeps_the_latest(self):
        t = rec.Telemetry("claude")
        for i in range(rec.SERIES_CAP * 3):
            t.feed(claude_assistant(f"m{i}", FABLE, (1, 0, i, 1), ts=f"t{i}"))
        self.assertLessEqual(len(t.series), rec.SERIES_CAP)
        self.assertEqual(t.series[-1]["t"], f"t{rec.SERIES_CAP * 3 - 1}")

    def test_codex_shows_nothing_measured_until_turn_end(self):
        t = rec.Telemetry("codex")
        for event in CODEX_EVENTS[:3]:
            t.feed(event, "2026-10-08T20:00:00Z")
        snap = t.snapshot()
        self.assertIsNone(snap["last_measured_at"])
        self.assertIsNone(snap["cumulative"]["processed_input"])
        self.assertIn("only at turn end", snap["cumulative"]["basis"])
        self.assertEqual(snap["phase"], "responding")
        t.feed(CODEX_EVENTS[3], "2026-10-08T20:00:09Z")
        snap = t.snapshot()
        self.assertEqual(snap["last_measured_at"], "2026-10-08T20:00:09Z")
        self.assertEqual(snap["measured_at_basis"], "recorder clock when the line was read")
        self.assertEqual((snap["cumulative"]["processed_input"], snap["cumulative"]["reasoning_output"]), (15242, 0))


def claude_record(run_id, role, events, *, argv=("claude", "-p", "x"), task="7"):
    return rec.build_record(task_id=task, run_id=run_id, role=role, model_requested="claude-opus-5-5",
                            events=list(events), exit_code=0, wall_duration_ms=5000,
                            started_at="2026-10-08T20:00:00Z", ended_at=None, argv=list(argv),
                            stream_path=None, api_key_env_present=False)


class Attribution(unittest.TestCase):
    def test_exact_reported_identity_is_preserved(self):
        result = {"type": "result", "subtype": "success", "session_id": "s", "usage": CLAUDE_EVENTS[-1]["usage"],
                  "modelUsage": {FABLE: {"inputTokens": 18, "cacheCreationInputTokens": 10079,
                                         "cacheReadInputTokens": 38085, "outputTokens": 77}}}
        rows = obs.attribute_usage(claude_record("a", "reviewer", [result]))
        self.assertEqual([(r["model"], r["model_basis"], r["provider"], r["role_label"]) for r in rows],
                         [(FABLE, "reported", "claude", "Reviewer")])

    def test_mixed_models_split_only_when_provably_equal_else_one_row(self):
        fresh = claude_record("b", "writer", CLAUDE_EVENTS)
        rows = obs.attribute_usage(fresh)
        self.assertEqual(sorted(r["model"] for r in rows), sorted([FABLE, "claude-haiku-4-5-20251001"]))
        self.assertEqual(sum(r["total"] for r in rows), obs._total(fresh["tokens"]))

        # A resumed session: per-model counters include an earlier invocation.
        result = json.loads(json.dumps(CLAUDE_EVENTS[-1]))
        result["modelUsage"][FABLE]["outputTokens"] += 100
        resumed = claude_record("c", "correction", [*CLAUDE_EVENTS[:-1], result],
                                argv=("claude", "-p", "x", "--resume", "s"))
        rows = obs.attribute_usage(resumed)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["model"], f"{FABLE} + claude-haiku-4-5-20251001")
        self.assertTrue(rows[0]["model_basis"].startswith("mixed"))
        self.assertEqual(rows[0]["total"], obs._total(resumed["tokens"]))

    def test_task_attribution_by_provider_model_and_role(self):
        codex = CodexRecord().build()
        codex.update(task_id="7", run_id="plan")
        records = [codex, claude_record("impl", "writer", CLAUDE_EVENTS)]
        summary = obs.summarize_task(records, "7")
        groups = {(g["provider"], g["model"]): g for g in summary["attribution"]["by_provider_model"]}
        self.assertEqual(groups[("codex", "gpt-5.5")]["total"], 15242 + 5)
        self.assertEqual(groups[("codex", "gpt-5.5")]["reasoning_output"], 0)
        roles = {g["role"]: g["total"] for g in summary["attribution"]["by_role"]}
        self.assertEqual(sum(roles.values()), summary["tokens"]["processed_input"] + summary["tokens"]["output"])
        self.assertEqual({r["provider"] for r in summary["runs"]}, {"codex", "claude"})
        self.assertIn("codex", obs.render_summary(summary))

    def test_legacy_record_without_provider_is_claude(self):
        record = claude_record("d", "writer", CLAUDE_EVENTS)
        del record["provider"], record["telemetry"]
        self.assertEqual(obs.run_provider(record), "claude")
        self.assertEqual(obs.summarize_task([record], "7")["runs"][0]["provider"], "claude")


def codex_record(usage, *, run_id="plan", role="reviewer", argv=CODEX_ARGV):
    events = [*CODEX_EVENTS[:-1], {"type": "turn.completed", "usage": usage}]
    record = CodexRecord().build(events, argv, role)
    record.update(task_id="7", run_id=run_id)
    return record


def groups(summary, key="by_provider_model_role"):
    return {tuple(g[k] for k in ("provider", "model", "role") if k in g): g for g in summary["attribution"][key]}


class UsageBreakdown(unittest.TestCase):
    """The overview's rows: per-invocation usage by provider, exact model and role."""

    def test_completion_only_codex_counts_and_unreported_cache_writes_stay_unknown(self):
        # Codex reports usage once, at turn.completed, with no timestamp series; this one has no cache-write field.
        usage = {k: v for k, v in CODEX_USAGE.items() if k != "cache_write_input_tokens"}
        record = codex_record(usage)
        self.assertEqual([p for p in record["telemetry"]["series"] if p["t"]], [])  # nothing a time chart could use
        summary = obs.summarize_task([record], "7")
        row = groups(summary)[("codex", "gpt-5.5", "reviewer")]
        self.assertEqual(row["tokens"], {"uncached_input": 2954, "cache_creation_input": None,
                                         "cached_input": 12288, "output": 5})  # never guessed as zero
        self.assertEqual((row["model_source"], row["model_name"], row["role_label"]), ("requested", "GPT-5.5", "Reviewer"))
        # Codex input_tokens already holds cache reads: the denominator is 15242, not 15242 + cache writes.
        self.assertEqual((row["input_total"], row["total"]), (15242, 15247))
        self.assertEqual(row["cached_share"], round(12288 / 15242, 4))
        self.assertEqual(groups(summary, "by_role")[("reviewer",)]["total"], 15247)
        # The record keeps the unknown; the task-wide processed input (all three parts) stays unknown too.
        self.assertIsNone(record["tokens"]["cache_creation_input"])
        self.assertIsNone(summary["tokens"]["processed_input"])

        # A positive cache write has no verified relation to input_tokens: input, share and total stay unknown.
        written = groups(obs.summarize_task([codex_record({**CODEX_USAGE, "cache_write_input_tokens": 9})], "7"))
        row = written[("codex", "gpt-5.5", "reviewer")]
        self.assertEqual((row["tokens"]["cache_creation_input"], row["tokens"]["uncached_input"]), (9, None))
        self.assertEqual((row["input_total"], row["cached_share"], row["total"]), (None, None, None))
        self.assertEqual(row["tokens"]["output"], 5)

    def test_exact_reported_id_with_a_readable_name_never_the_alias(self):
        result = {"type": "result", "subtype": "success", "session_id": "s", "usage": CLAUDE_EVENTS[-1]["usage"],
                  "modelUsage": {FABLE: {"inputTokens": 18, "cacheCreationInputTokens": 10079,
                                         "cacheReadInputTokens": 38085, "outputTokens": 77}}}
        record = rec.build_record(task_id="7", run_id="a", role="writer", model_requested="opus", events=[result],
                                  exit_code=0, wall_duration_ms=1, started_at=None, ended_at=None,
                                  argv=["claude", "-p", "x"], stream_path=None, api_key_env_present=False)
        row = obs.attribute_usage(record)[0]
        self.assertEqual((row["model"], row["model_name"], row["model_source"]), (FABLE, "Fable 5.1", "reported"))
        names = {"claude-opus-5-5": "Opus 5.5", "claude-haiku-4-5-20251001": "Haiku 4.5",
                 "claude-sonnet-4-20250514": "Sonnet 4", "claude-3-5-sonnet-20241022": "Sonnet 3.5",
                 "claude-opus-5-5[1m]": "Opus 5.5 (1M context)", "gpt-5.5": "GPT-5.5",
                 "gpt-5.5-codex": "GPT-5.5 Codex", "gpt-5-codex-mini": "GPT-5 Codex Mini",
                 f"{FABLE} + claude-haiku-4-5-20251001": "Fable 5.1 + Haiku 4.5",
                 "opus": None, "my-local-model": None, "claude-instant": None, None: None}
        self.assertEqual({k: obs.model_name(k) for k in names}, names)

    def test_several_claude_models_and_roles_partition_each_run_once(self):
        records = [codex_record(CODEX_USAGE), claude_record("impl", "writer", CLAUDE_EVENTS)]
        summary = obs.summarize_task(records, "7")
        rows = groups(summary)
        self.assertEqual(set(rows), {("codex", "gpt-5.5", "reviewer"), ("claude", FABLE, "writer"),
                                     ("claude", "claude-haiku-4-5-20251001", "writer")})
        # The subagent's Haiku usage is inside the run's usage; split by modelUsage, never added on top.
        self.assertEqual(rows[("claude", "claude-haiku-4-5-20251001", "writer")]["tokens"],
                         {"uncached_input": 3, "cache_creation_input": 0, "cached_input": 1000, "output": 7})
        claude_total = sum(g["total"] for k, g in rows.items() if k[0] == "claude")
        self.assertEqual(claude_total, obs._total(records[1]["tokens"]))
        self.assertEqual(sum(g["total"] for g in rows.values()),
                         summary["tokens"]["processed_input"] + summary["tokens"]["output"])
        self.assertIn("not attributed separately", summary["attribution"]["subagents"])

    def test_cached_share_uses_each_providers_own_input(self):
        claude = {"uncached_input": 10, "cache_creation_input": 90, "cached_input": 900, "output": 1}
        self.assertEqual(obs.cached_share(claude, "claude"), 0.9)  # 900 / (10 + 90 + 900)
        self.assertIsNone(obs.cached_share(dict(claude, cache_creation_input=None), "claude"))
        codex = {"uncached_input": 100, "cache_creation_input": None, "cached_input": 300, "output": 1}
        self.assertEqual(obs.cached_share(codex, "codex"), 0.75)  # 300 / input_tokens 400
        self.assertIsNone(obs.cached_share(dict(codex, uncached_input=None), "codex"))
        self.assertIsNone(obs.cached_share(dict(codex, uncached_input=0, cached_input=0), "codex"))  # no input: undefined
        self.assertIsNone(obs.cached_share(dict(codex, cached_input=None), "codex"))

    def test_session_cumulative_and_resumed_model_usage_are_never_summed(self):
        usage = {"input_tokens": 5, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 500, "output_tokens": 9}
        result = {"type": "result", "subtype": "success", "session_id": "s", "usage": usage,
                  "modelUsage": {FABLE: {"inputTokens": 900, "cacheCreationInputTokens": 50000,
                                         "cacheReadInputTokens": 400000, "outputTokens": 8000}}}
        resumed = claude_record("r1", "correction", [result], argv=("claude", "-p", "x", "--resume", "s"))
        row = groups(obs.summarize_task([resumed], "7"))[("claude", FABLE, "correction")]
        self.assertEqual(row["tokens"], {"uncached_input": 5, "cache_creation_input": 0, "cached_input": 500,
                                         "output": 9})
        self.assertEqual(row["total"], 514)
        self.assertEqual(resumed["session_cumulative"]["totals"]["output"], 8000)  # kept, never added
        # Two models whose session counters do not equal this invocation's usage: one mixed row, not a split.
        result["modelUsage"]["claude-haiku-4-5-20251001"] = {"inputTokens": 1, "cacheCreationInputTokens": 0,
                                                            "cacheReadInputTokens": 1, "outputTokens": 1}
        mixed = claude_record("r2", "correction", [result], argv=("claude", "-p", "x", "--resume", "s"))
        rows = list(groups(obs.summarize_task([mixed], "7")).values())
        self.assertEqual([(g["model_source"], g["total"]) for g in rows], [("mixed", 514)])
        self.assertEqual(rows[0]["model_name"], "Fable 5.1 + Haiku 4.5")

    def test_requested_only_usage_never_merges_into_the_reported_model(self):
        opus = "claude-opus-5-5"
        usage = {"input_tokens": 10, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 1000,
                 "output_tokens": 7}
        reported = {"type": "result", "subtype": "success", "session_id": "a", "usage": usage,
                    "modelUsage": {opus: {"inputTokens": 10, "cacheCreationInputTokens": 100,
                                          "cacheReadInputTokens": 1000, "outputTokens": 7}}}
        # Same requested id, but this run's result names no model: only the request is known.
        requested = {"type": "result", "subtype": "success", "session_id": "b", "usage": usage}
        summary = obs.summarize_task([claude_record("a", "writer", [reported]),
                                      claude_record("b", "writer", [requested])], "7")
        by_model = {(g["provider"], g["model"], g["model_source"]): g
                    for g in summary["attribution"]["by_provider_model"]}
        self.assertEqual(set(by_model), {("claude", opus, "reported"), ("claude", opus, "requested")})
        self.assertEqual([by_model[("claude", opus, s)]["runs"] for s in ("reported", "requested")], [["a"], ["b"]])
        self.assertEqual({g["total"] for g in by_model.values()}, {1117})
        self.assertEqual(sum(g["total"] for g in by_model.values()),
                         sum(r["total"] for r in summary["attribution"]["rows"]))  # each run counted once
        self.assertIn("requested", obs.render_summary(summary))


RECORDER_ARGS = ["python3", "/x/harmony/direct-workflow/direct_run_recorder.py", "run", "--task-id", "7",
                 "--role", "writer", "--run-id", "7-impl", "--records-dir", "/r", "--", "claude", "-p", "x"]
TASK_ARGS = ["--task-id", "7", "--role", "writer", "--run-id", "7-impl"]
WRAPPED = ["--", "claude", "-p", "x"]
# Whether `ps` may inspect a process here (a sandbox can deny it); the proof then fails closed.
PS_AVAILABLE = obs.process_probe(os.getpid()) is not None


class ActiveProof(unittest.TestCase):
    """The overview's active indicator: the task's own recorder process, nothing less."""

    def setUp(self):
        self.records = Path(tempfile.mkdtemp(prefix="companion-active-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.records, ignore_errors=True))

    def running(self, pid, started, *, task="7", role="writer", run_id="7-impl", host=None):
        (self.records / f"{run_id}.json").write_text(json.dumps({
            "schema": rec.SCHEMA, "run_id": run_id, "task_id": task, "role": role, "provider": "claude",
            "status": "running", "started_at": started.isoformat(),
            "recorder": {"pid": pid, "host": host or rec.socket.gethostname()}, "telemetry": {}}))

    def active(self, probe=None):
        return obs.live_runs(self.records, "7", probe=probe)[0]["active"]

    def test_only_this_tasks_recorder_started_before_the_record_is_confirmed(self):
        started = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=10)
        self.running(os.getpid(), started)
        cases = {
            "recorder script": (20, RECORDER_ARGS, True),
            "nlh record": (20, ["/venv/bin/nlh", "record", "--task-id=7", "--role", "writer", "--", "claude"], True),
            "bare provider CLI": (20, ["claude", "-p", "x", "--task-id", "7", "--role", "writer"], False),
            "quota reader session": (20, ["claude", "--ax-screen-reader", "--tools", "", "--permission-mode", "plan"],
                                     False),
            "recorder words only inside the wrapped command": (
                20, ["python3", "x.py", "--", "python3", "direct_run_recorder.py", "run", "--task-id", "7",
                     "--role", "writer"], False),
            "another task": (20, [*RECORDER_ARGS[:4], "8", *RECORDER_ARGS[5:]], False),
            "another role": (20, [*RECORDER_ARGS[:6], "reviewer", *RECORDER_ARGS[7:]], False),
            "another run id": (20, [*RECORDER_ARGS[:8], "7-other", *RECORDER_ARGS[9:]], False),
            "pid reused after the record started": (3, RECORDER_ARGS, False),
        }
        for label, (elapsed, args, confirmed) in cases.items():
            with self.subTest(label):
                result = self.active(lambda pid, e=elapsed, a=args: (e, a))
                self.assertEqual(result["confirmed"], confirmed, result["basis"])
        self.assertFalse(self.active(lambda pid: None)["confirmed"])  # process could not be inspected

    def test_only_an_executed_recorder_entrypoint_counts(self):
        """Controlled probe metadata: the argv `ps` would show for each supported launch, and fakes."""
        self.running(os.getpid(), dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=10))
        recorder = "/x/harmony/direct-workflow/direct_run_recorder.py"
        supported = {
            "script by interpreter path": ["/opt/homebrew/bin/python3.14", recorder, "run", *TASK_ARGS],
            "script with interpreter options": ["python3", "-B", "-u", "-X", "utf8", recorder, "run", *TASK_ARGS],
            "macOS framework interpreter": [
                "/Library/Frameworks/Python.framework/Versions/3.14/Resources/Python.app/Contents/MacOS/Python",
                recorder, "run", *TASK_ARGS],
            "importable module": ["python3", "-m", "direct_run_recorder", "run", *TASK_ARGS],
            "module, attached -m": ["python3", "-mdirect_run_recorder", "run", *TASK_ARGS],
            "nlh console script": ["/venv/bin/nlh", "record", *TASK_ARGS],
            "nlh with global options": ["nlh", "--project", "/p", "--config=/p/h.json", "record", *TASK_ARGS],
            "nlh console script run by its interpreter": ["/venv/bin/python3", "/venv/bin/nlh", "record", *TASK_ARGS],
            "harmony.cli module": ["python3", "-m", "harmony.cli", "record", *TASK_ARGS],
            "nlh record without --run-id (recorder picks one)": ["nlh", "record", "--task-id", "7", "--role", "writer"],
        }
        fakes = {
            "python -c with recorder words": ["python3", "-c", "import time; time.sleep(60)", recorder, "run",
                                              *TASK_ARGS],
            "python -c inside a flag cluster": ["python3", "-Bc", "pass", recorder, "run", *TASK_ARGS],
            "python reading stdin": ["python3", "-", recorder, "run", *TASK_ARGS],
            "an unrelated script": ["python3", "/x/other.py", "run", *TASK_ARGS],
            "a script merely named like the recorder": ["python3", "/x/not_direct_run_recorder.py.bak", "run",
                                                        *TASK_ARGS],
            "an unrelated module": ["python3", "-m", "other", "direct_run_recorder", "run", *TASK_ARGS],
            "recorder without the run subcommand": ["python3", recorder, "summary", *TASK_ARGS],
            "nlh, another subcommand": ["nlh", "inspect", "record", *TASK_ARGS],
            "nlh with an unknown option first": ["nlh", "--weird", "record", *TASK_ARGS],
            "an unrelated program": ["/usr/bin/env", "python3", recorder, "run", *TASK_ARGS],
            "a shell wrapping the recorder": ["sh", "-c", f"python3 {recorder} run", *TASK_ARGS],
            "a program named like a path to nlh": ["/x/nlh-helper", "record", *TASK_ARGS],
            "flags only after the wrapped command": ["python3", recorder, "run", *WRAPPED, *TASK_ARGS],
        }
        for label, args in supported.items():
            with self.subTest(label):
                result = self.active(lambda pid, a=args: (20, [*a, *WRAPPED]))
                self.assertEqual(result, {"confirmed": True, "basis":
                                          "this task's recorder process is alive and started before the record"})
        for label, args in fakes.items():
            with self.subTest(label):
                result = self.active(lambda pid, a=args: (20, a))
                self.assertFalse(result["confirmed"], label)
        for label, args in {"another task": ["nlh", "record", "--task-id", "8", "--role", "writer"],
                            "another run": ["python3", "-m", "direct_run_recorder", "run", "--task-id", "7",
                                            "--role", "writer", "--run-id", "7-old"]}.items():
            with self.subTest(label):
                result = self.active(lambda pid, a=args: (20, a))
                self.assertEqual(result["basis"], "the recorder process is for another task, role or run")
        reused = self.active(lambda pid: (3, ["nlh", "record", *TASK_ARGS]))
        self.assertEqual(reused["basis"], "the process started after the record (reused pid)")

    def test_dead_or_foreign_host_recorder_is_never_active(self):
        started = dt.datetime.now(dt.timezone.utc)
        probe = lambda pid: (20, RECORDER_ARGS)  # noqa: E731 - would confirm, but must not be asked
        self.running(2 ** 22 + 12345, started)
        self.assertEqual(self.active(probe), {"confirmed": False, "basis": "recorder process gone"})
        self.running(os.getpid(), started, host="another-host")
        self.assertEqual(self.active(probe)["confirmed"], False)

    def test_etime_parsing(self):
        self.assertEqual((obs._etime_seconds("1-02:03:04"), obs._etime_seconds("03:04"), obs._etime_seconds("x")),
                         (93784, 184, None))

    @unittest.skipUnless(PS_AVAILABLE, "`ps` cannot inspect processes here (sandbox); needs an unsandboxed run")
    def test_a_real_sleeping_fake_with_recorder_words_is_rejected(self):
        # The reviewed false positive: Python `-c` code that only sleeps, its argv full of recorder words.
        # Never `claude` or `codex`. (The genuine recorder is proven in LiveRun.)
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)",
                                 "/x/direct_run_recorder.py", "run", *TASK_ARGS])
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        self.running(proc.pid, dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=1))
        _, words = self.wait_for_probe(proc.pid)
        self.assertIn("-c", words)
        self.assertEqual(self.active(), {"confirmed": False, "basis": "the pid is not a recorder run (reused pid?)"})
        proc.kill()
        proc.wait()
        self.assertEqual(self.active()["basis"], "recorder process gone")

    def wait_for_probe(self, pid):
        deadline = time.time() + 10
        while time.time() < deadline:
            seen = obs.process_probe(pid)
            if seen and "time.sleep(60)" in " ".join(seen[1]):
                return seen
            time.sleep(0.05)
        self.fail("process never visible to ps")


FAKE = """#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path
open(os.environ["FAKE_ENV_LOG"], "w").write(json.dumps({"guard": os.environ.get("HARMONY_PR_GUARD")}))
events = [json.loads(line) for line in open(os.environ["FAKE_EVENTS"])]
gate = Path(os.environ["FAKE_GATE"])
for index, event in enumerate(events):
    if index == len(events) - 1:
        deadline = time.time() + 30
        while not gate.exists() and time.time() < deadline:
            time.sleep(0.05)
    print(json.dumps(event), flush=True)
"""


class LiveRun(unittest.TestCase):
    """A managed run is visible while it runs, then becomes a normal v2 record."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="companion-live-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        for name in ("claude", "codex"):
            path = self.bin / name
            path.write_text(FAKE)
            path.chmod(path.stat().st_mode | stat.S_IXUSR)
        self.records = self.tmp / "records"
        self.gate = self.tmp / "gate"
        self.env_log = self.tmp / "env.json"

    def start(self, argv, role, events, run_id):
        feed = self.tmp / f"{run_id}.events"
        feed.write_text("".join(json.dumps(e) + "\n" for e in events))
        env = {k: v for k, v in os.environ.items() if k not in rec.API_KEY_ENV + rec.CODEX_API_KEY_ENV}
        env.update(PATH=f"{self.bin}{os.pathsep}{env.get('PATH', '')}", FAKE_EVENTS=str(feed),
                   FAKE_GATE=str(self.gate), FAKE_ENV_LOG=str(self.env_log),
                   HARMONY_PR_GUARD="/somewhere/else.py")
        proc = subprocess.Popen([sys.executable, str(RECORDER), "run", "--task-id", "7", "--role", role,
                                 "--run-id", run_id, "--records-dir", str(self.records), "--", *argv],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        self.addCleanup(self.stop, proc)
        return proc

    @staticmethod
    def stop(proc):
        """A failed assertion must not leave the recorder (and its fake CLI) running."""
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        for stream in (proc.stdout, proc.stderr):
            stream.close()

    def wait_for(self, predicate, timeout=15.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.1)
        self.fail("condition not reached")

    def running_snapshot(self, run_id, phase):
        def check():
            try:
                raw = json.loads((self.records / f"{run_id}.json").read_text())
            except (OSError, ValueError):
                return None
            return raw if raw.get("status") == "running" and raw["telemetry"]["phase"] == phase else None
        return self.wait_for(check)

    def test_codex_reviewer_running_then_completed(self):
        proc = self.start(CODEX_ARGV, "reviewer", CODEX_EVENTS, "7-review")
        raw = self.running_snapshot("7-review", "responding")
        self.assertEqual((raw["task_id"], raw["role"], raw["provider"]), ("7", "reviewer", "codex"))
        self.assertIsNone(raw["telemetry"]["last_measured_at"])  # Codex: nothing measured before turn end

        live = obs.live_runs(self.records, "7")
        self.assertEqual([(r["run_id"], r["state"], r["role_label"]) for r in live],
                         [("7-review", "running", "Reviewer")])
        self.assertIsInstance(live[0]["elapsed_ms"], int)
        # The genuine recorder invocation (synthetic provider events) for this task and role: proven active
        # by its executed entrypoint. Where `ps` is denied (sandbox) the proof fails closed instead.
        if PS_AVAILABLE:
            self.assertEqual(live[0]["active"], {"confirmed": True, "basis":
                                                 "this task's recorder process is alive and started before the record"})
        else:
            self.assertEqual(live[0]["active"], {"confirmed": False,
                                                 "basis": "recorder process could not be inspected"})
        # The same live recorder pid under a record an hour older is a reused pid, never active.
        stale = dict(raw, started_at=(dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).isoformat())
        self.assertFalse(obs.confirm_active(stale, True)["confirmed"])
        records, skipped = obs.load_task_records(self.records, "7")
        self.assertEqual(records, [])  # never counted as a completed run
        self.assertIn("'running'", skipped[0]["why"])
        card = obs.card_metrics(obs.summarize_task(records, "7"), live)
        if PS_AVAILABLE:
            self.assertTrue(card["line"].startswith("▶ Reviewer"))
            self.assertIsNone(card["total_tokens"])
        else:
            self.assertIsNone(card)  # unproven: the card claims no running role

        self.gate.write_text("go")
        _, err = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 0, err)
        record = json.loads((self.records / "7-review.json").read_text())
        self.assertNotIn("status", record)
        self.assertEqual(record["provider"], "codex")
        self.assertEqual(record["tokens"]["output"], 5)
        self.assertEqual(record["telemetry"]["phase"], "turn completed")
        self.assertEqual(obs.live_runs(self.records, "7"), [])
        # The guard env is a Claude hook concern; a Codex run's environment is passed through as is.
        self.assertEqual(json.loads(self.env_log.read_text())["guard"], "/somewhere/else.py")

    def test_resumed_codex_run_streams_and_records_scope_unverified(self):
        # The second turn is held back, so the snapshot after the first turn is a running one.
        events = [*CODEX_EVENTS, {"type": "turn.started"}]
        argv = ["codex", "exec", "--json", "-m", "gpt-5.5", "resume", "01a11d16"]
        proc = self.start(argv, "reviewer", events, "7-resumed")
        raw = self.running_snapshot("7-resumed", "turn completed")
        cum = raw["telemetry"]["cumulative"]
        self.assertIsNone(cum["processed_input"])
        self.assertIn("resumed thread", cum["basis"])
        self.assertEqual(cum["scope_unverified"]["processed_input"], 15242)
        self.gate.write_text("go")
        _, err = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 0, err)
        record = json.loads((self.records / "7-resumed.json").read_text())
        self.assertIsNone(record["tokens"]["output"])
        self.assertTrue(record["resumed_session"]["value"])
        self.assertIsNone(record["telemetry"]["cumulative"]["output"])
        self.assertEqual(record["telemetry"]["cumulative"]["scope_unverified"]["output"], 5)

    def test_claude_writer_gets_this_packages_guard_and_live_context(self):
        argv = ["claude", "-p", "x", "--model", "claude-opus-5-5", "--output-format", "stream-json", "--verbose",
                "--settings", RELEASE_SETTINGS]
        proc = self.start(argv, "writer", CLAUDE_EVENTS, "7-impl")
        raw = self.running_snapshot("7-impl", "responding")
        self.wait_for(lambda: json.loads((self.records / "7-impl.json").read_text())
                      ["telemetry"]["context"]["tokens"] == 23887)
        self.gate.write_text("go")
        _, err = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 0, err)
        self.assertEqual(raw["model"]["requested"]["value"], "claude-opus-5-5")
        # The caller's HARMONY_PR_GUARD is replaced by this package's guard.
        self.assertEqual(json.loads(self.env_log.read_text())["guard"], str(rec.GUARD_PATH))
        record = json.loads((self.records / "7-impl.json").read_text())
        self.assertEqual(record["telemetry"]["context"]["window"], 200000)
        self.assertEqual(record["model"]["reported"]["value"], sorted([FABLE, "claude-haiku-4-5-20251001"]))

    def test_dead_recorder_reads_as_interrupted_and_leaves_the_card(self):
        self.records.mkdir()
        (self.records / "7-x.json").write_text(json.dumps({
            "schema": rec.SCHEMA, "run_id": "7-x", "task_id": "7", "role": "writer", "provider": "claude",
            "status": "running", "started_at": "2026-10-08T20:00:00Z",
            "recorder": {"pid": 2 ** 22 + 12345, "host": rec.socket.gethostname()}, "telemetry": {}}))
        live = obs.live_runs(self.records, "7", now=dt.datetime(2026, 10, 8, 21, tzinfo=dt.timezone.utc))
        self.assertEqual(live[0]["state"], "interrupted")
        self.assertFalse(live[0]["active"]["confirmed"])
        self.assertIsNone(live[0]["elapsed_ms"])
        self.assertIsNone(obs.card_metrics(obs.summarize_task([], "7"), live))


class GuardPath(unittest.TestCase):
    """release-settings.json runs the real guard wherever the package is installed."""

    def hook(self):
        settings = json.loads(Path(RELEASE_SETTINGS).read_text())
        return settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]

    def run_hook(self, command, env, cwd):
        return subprocess.run(self.hook(), shell=True, input=json.dumps({"tool_input": {"command": command}}),
                              capture_output=True, text=True, cwd=cwd,
                              env={"PATH": os.environ.get("PATH", ""), **env})

    def test_env_points_the_hook_at_the_package_guard_from_any_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {rec.GUARD_ENV: str(rec.GUARD_PATH), "CLAUDE_PROJECT_DIR": tmp}
            blocked = self.run_hook("gh pr create --fill", env, tmp)
            self.assertEqual(blocked.returncode, 2, blocked.stderr)
            self.assertIn("BLOCKED", blocked.stderr)
            self.assertEqual(self.run_hook("gh pr list", env, tmp).returncode, 0)

    def test_unresolvable_guard_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            for env in ({"CLAUDE_PROJECT_DIR": tmp}, {rec.GUARD_ENV: str(Path(tmp) / "missing.py")}):
                with self.subTest(env=sorted(env)):
                    # Exit 2 is Claude Code's blocking exit: every Bash call is refused.
                    self.assertEqual(self.run_hook("ls", env, tmp).returncode, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
