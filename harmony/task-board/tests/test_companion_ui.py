#!/usr/bin/env python3
"""Companion page behaviour: the served index.html + app.js run in jsdom.

The overview (two weekly allowance cards, the current task, its confirmed
active runs and compact usage rows; no chart) and the
secondary Inspect Task view are checked as rendered DOM, against canned
payloads for selection edge cases and the real server's payloads for Inspect.
Skipped when node or jsdom is unavailable; point HARMONY_JSDOM_PATH (or
NODE_PATH) at a node_modules directory that contains jsdom.

    HARMONY_JSDOM_PATH=/path/to/node_modules python3 -B -m unittest discover -s tests -p 'test_*.py'
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

from test_board_observability import BoardFixture, add_running_review, rec, server

STATUSLINE = Path(__file__).resolve().parents[2] / "direct-workflow" / "statusline.py"

HARNESS = Path(__file__).with_name("dom_harness.js")
NODE_PATH = os.environ.get("HARMONY_JSDOM_PATH") or os.environ.get("NODE_PATH", "")


def jsdom_available() -> bool:
    if not shutil.which("node"):
        return False
    probe = subprocess.run(["node", "-e", "require('jsdom')"], capture_output=True,
                           env={**os.environ, "NODE_PATH": NODE_PATH})
    return probe.returncode == 0


JSDOM = jsdom_available()
T = "2026-10-08T20:{:02d}:{:02d}Z".format


def telemetry(*, ctx=None, window=None, percent=None, model=None, cum=None, provisional=None, scope=None,
              series=(), measured=None, phase=None, seen=()) -> dict:
    cumulative = {"processed_input": None, "output": None, "provisional": provisional, "basis": "fixture"}
    cumulative.update(cum or {})
    if scope:
        cumulative["scope_unverified"] = scope
    return {"context": {"tokens": ctx, "window": window, "percent": percent, "model": model, "basis": "fixture"},
            "cumulative": cumulative, "series": list(series), "last_measured_at": measured,
            "measured_at_basis": "provider event timestamp", "last_event_at": measured, "phase": phase,
            "events": 3, "models_seen": list(seen)}


def live(run_id, provider, role, started, *, state="running", model=None, tele=None, elapsed=None,
         active=False) -> dict:
    labels = {"writer": "Implementer", "correction": "Correction", "planner": "Planner", "reviewer": "Reviewer"}
    return {"run_id": run_id, "task_id": run_id.split("-")[0], "role": role, "role_label": labels[role],
            "provider": provider, "model_requested": model, "models_seen": (tele or {}).get("models_seen", []),
            "started_at": started, "state": state, "state_basis": "fixture", "elapsed_ms": elapsed,
            "active": {"confirmed": active, "basis": "fixture"}, "telemetry": tele or telemetry()}


def group(provider, model, role, tokens, *, name=None, source="reported", share=None, total=None) -> dict:
    labels = {"writer": "Implementer", "correction": "Correction", "planner": "Planner", "reviewer": "Reviewer"}
    return {"provider": provider, "model": model, "model_name": name, "model_source": source, "role": role,
            "role_label": labels[role], "runs": ["r"], "reasoning_output": None, "input_total": None,
            "tokens": dict(zip(("uncached_input", "cache_creation_input", "cached_input", "output"), tokens)),
            "cached_share": share, "total": total}


def done(run_id, provider, role, started, *, model=None, reported=None, tokens=(None, None, None, None),
         outcome="success") -> dict:
    labels = {"writer": "Implementer", "correction": "Correction", "planner": "Planner", "reviewer": "Reviewer"}
    return {"run_id": run_id, "role": role, "role_label": labels[role], "provider": provider, "model": model,
            "model_reported": reported, "started_at": started, "duration_ms": {"value": 1000},
            "tokens": dict(zip(("uncached_input", "cache_creation_input", "cached_input", "output"), tokens)),
            "outcome_cli": outcome, "outcome_review": None}


def view(task_id, *, live_runs=(), runs=(), tele=None, phases=None, title=None, status=None, groups=()) -> dict:
    return {"task_id": task_id, "title": title, "status": status, "live": list(live_runs), "phases": phases,
            "telemetry": tele or {},
            "summary": {"run_count": len(runs), "runs": list(runs), "unknowns": [],
                        "tokens": {"processed_input": None, "output": None}, "duration_ms": {"value": None},
                        "attribution": {"by_provider_model": [], "by_role": [], "basis": "",
                                        "by_provider_model_role": list(groups)}}}


def card(task_id, stage, status="IMPLEMENTING", title=None) -> dict:
    return {"id": task_id, "display": f"#{task_id}", "title": title or f"Card {task_id}", "ticket_title": "t",
            "status": status, "observability": None,
            "board": {"stage": stage, "verification": "unknown", "next_action": "unknown", "owner": "unknown",
                      "gate": "unknown", "links": [], "linked_defects": [], "revision": 0}}


def payloads(views, cards, quota=None) -> dict:
    return {"/api/quota": {"body": {"ok": True, "providers": quota or {}}},"/api/observability": {"body": {"ok": True, "available": True, "generated_at": T(59, 0),
                                            "runs_dir": "/tmp/runs", "tasks": views}},
            "/api/board": {"body": {"cards": cards, "intake_available": False,
                                    "counts": {s: 0 for s in ("Next", "In Progress", "Blocked", "Done")}}}}


@unittest.skipUnless(JSDOM, "node with jsdom not available (set HARMONY_JSDOM_PATH)")
class CompanionPage(BoardFixture):
    def setUp(self) -> None:
        super().setUp()
        add_running_review(self)

    def real_payloads(self) -> dict:
        return {"/api/observability": {"body": json.loads(self.request("GET", "/api/observability")[1])},
                "/api/board": {"body": json.loads(self.request("GET", "/api/board")[1])}}

    def page(self, hash_: str, responses: dict, steps: list, fake_timers: bool = False) -> dict:
        source = {"html": self.request("GET", "/")[1].decode(), "script": self.request("GET", "/app.js")[1].decode(),
                  "hash": hash_, "responses": responses, "steps": steps, "fakeTimers": fake_timers}
        done_ = subprocess.run(["node", str(HARNESS)], input=json.dumps(source), capture_output=True, text=True,
                               timeout=60, env={**os.environ, "NODE_PATH": NODE_PATH}, check=True)
        out = json.loads(done_.stdout)
        self.assertEqual(out["errors"], [])
        return out["snapshots"]

    def test_first_run_wizard_shows_checks_and_can_reopen(self):
        checks = [{"id": "codex", "label": "Codex CLI", "state": "ready", "detail": "Found on PATH"},
                  {"id": "claude", "label": "Claude Code CLI", "state": "missing", "detail": "Install Claude"}]
        responses = payloads([], [])
        responses["/api/setup"] = {"body": {"ok": True, "checks": checks,
                                             "ready_for_assisted_setup": False}}
        result = self.page("", responses, [{"snapshot": "initial"},
                                           {"click": "#setup-close"}, {"snapshot": "skipped"},
                                           {"click": "#setup-open"}, {"snapshot": "reopened"}])
        self.assertTrue(result["initial"]["setup"]["open"])
        self.assertEqual([c["state"] for c in result["initial"]["setup"]["checks"]],
                         ["ready", "missing"])
        self.assertFalse(result["skipped"]["setup"]["open"])
        self.assertTrue(result["reopened"]["setup"]["open"])

    # -- overview: weekly allowance, the task, its active runs, its usage rows --

    def test_overview_weekly_cards_task_line_active_run_and_usage_rows_without_chart(self):
        series = [{"t": T(30, 0), "context_tokens": 20000, "processed_input": 20000, "output": 400},
                  {"t": T(30, 10), "context_tokens": 23506, "processed_input": 48522, "output": 1700}]
        writer = live("7-impl", "claude", "writer", T(30, 0), model="claude-opus-5-5", elapsed=600000, active=True,
                      tele=telemetry(ctx=23506, model="claude-opus-5-5", cum={"processed_input": 48522, "output": 1700},
                                     provisional=True, series=series, measured=T(30, 10), phase="responding",
                                     seen=("claude-opus-5-5",)))
        # Alive by pid but not proven to be this task's recorder: no active line for it.
        planner = live("7-plan", "codex", "planner", T(31, 0), model="gpt-5.5", elapsed=5000)
        seven = view("7", live_runs=[writer, planner], runs=[
            done("7-review", "codex", "reviewer", T(20, 0), model="gpt-5.5", tokens=(2954, None, 12288, 5))],
            phases=[{"id": "implement", "type": "implement", "state": "running"},
                    {"id": "review-1", "type": "review", "state": "pending"}],
            # Completion-only Codex (cache writes not reported) and an exact Claude id.
            groups=[group("codex", "gpt-5.5", "reviewer", (2954, None, 12288, 5), name="GPT-5.5",
                          source="requested", share=0.8062, total=15247),
                    group("claude", "claude-opus-5-5", "writer", (18, 10079, 38085, 77), name="Opus 5.5",
                          share=0.7904, total=48259)])
        now = time.time()
        quota = {"codex": {"available": True, "remaining_percent": 71, "used_percent": 29,
                           "resets_at": now + 3 * 86400 + 8 * 3600 + 120, "measured_at": now - 120,
                           "source": "codex app-server account/rateLimits", "window_minutes": 10080,
                           "refresh_error": None},
                 "claude": {"available": False, "note": "No Claude Code status-line reading captured"}}
        shot = self.page("", payloads([seven], [card(7, "In Progress", title="Seven")], quota),
                         [{"snapshot": "s"}])["s"]

        self.assertEqual(shot["order"], ["quota", "current", "active-runs", "usage"])
        self.assertEqual(shot["quotaOrder"], ["codex", "claude"])
        codex, claude = shot["quota"]["codex"], shot["quota"]["claude"]
        self.assertEqual((codex["state"], codex["big"], codex["meter"], codex["reset"]),
                         ("fresh", "71%", "71", "Resets in 3d 8h"))
        self.assertIn("CodexPlanner / Reviewer71%Weekly allowance left", codex["text"])
        self.assertTrue(codex["measured"].startswith("Measured ") and "2m" in codex["measured"])
        self.assertEqual((claude["state"], claude["meter"], claude["big"]), ("unavailable", None, None))
        self.assertEqual(claude["note"], "Weekly allowance unavailable · No Claude Code status-line reading captured")
        self.assertTrue(claude["text"].startswith("ClaudeImplementer / Correction"))

        self.assertEqual((shot["current"], shot["phase"]), ("Task #7 · Seven", "Phase implement · running"))
        self.assertEqual((shot["overviewInspectHref"], shot["overviewInspectText"]), ("#inspect/7", "Inspect ›"))
        # No cumulative-token chart, no tables, no context or run ids; unknown parts are left out calmly.
        self.assertIsNone(shot["chart"])
        self.assertEqual((shot["overviewCharts"], shot["overviewTables"], shot["tabs"]), (0, 0, 2))
        for absent in ("Context", "23.5k", "48.5k", "7-", "unknown", "provisional", "$"):
            self.assertNotIn(absent, shot["overview"])
        self.assertEqual(shot["runState"], {"state": "unconfirmed", "text": "Running · some unconfirmed"})
        self.assertEqual(shot["active"], ["Running · Claude Implementer · 10m 00s"])
        self.assertEqual(shot["usage"], [
            {"provider": "codex", "who": "Codex · Reviewer", "model": "GPT-5.5 · gpt-5.5 (requested; not reported)",
             "total": "15.2k", "parts": "fresh in 3.0k · cache read 12.3k · out 5 · 80.6% cached input"},
            {"provider": "claude", "who": "Claude · Implementer", "model": "Opus 5.5 · claude-opus-5-5",
             "total": "48.3k", "parts": "fresh in 18 · cache read 38.1k · cache write 10.1k · out 77 · 79% cached input"}])
        # Bars: each group's share of the recorded processed tokens (15,247 + 48,259 = 63,506).
        self.assertEqual([(b["share"], b["label"]) for b in shot["usageBars"]],
                         [("24%", "24% share of recorded processed tokens"),
                          ("76%", "76% share of recorded processed tokens")])
        for bar, total in zip(shot["usageBars"], (15247, 48259)):
            self.assertAlmostEqual(float(bar["width"].rstrip("%")), 100 * total / 63506)
        self.assertTrue(shot["usageBars"][1]["title"].startswith(
            "48,259 recorded processed tokens (input 48,182 + output 77) · 1 completed run"), shot["usageBars"][1])
        self.assertIn("Shares of recorded processed tokens", shot["usageCaption"])
        self.assertIn("input + output · completed runs", shot["usageCaption"])
        # Weighted: (12,288 + 38,085) / (15,242 + 48,182) = 79.4%, not the 79.8% mean of 80.6% and 79%;
        # Codex did not report cache writes, so no cache-write amount is claimed.
        self.assertEqual(shot["usageCache"], "Cache reuse: 79.4% of measured input")

        # Without a known cached share or any counter: the share and the total are omitted, never 0.
        seven["summary"]["attribution"]["by_provider_model_role"] = [
            group("codex", "gpt-5.5", "reviewer", (None, None, None, None), source="requested"),
            group("claude", "claude-fable-5-1 + claude-haiku-4-5-20251001", "correction", (5, None, 500, 9),
                  name="Fable 5.1 + Haiku 4.5", source="mixed")]
        seven["live"] = []
        shot = self.page("", payloads([seven], [card(7, "In Progress", title="Seven")]), [{"snapshot": "s"}])["s"]
        self.assertEqual(shot["order"], ["quota", "current", "usage"])  # nothing proven active: no line
        self.assertEqual(shot["usage"], [
            {"provider": "codex", "who": "Codex · Reviewer", "model": "gpt-5.5 (requested; not reported)",
             "total": "", "parts": "no usage reported"},
            {"provider": "claude", "who": "Claude · Correction",
             "model": "Fable 5.1 + Haiku 4.5 · claude-fable-5-1 + claude-haiku-4-5-20251001 (not split)",
             "total": "", "parts": "fresh in 5 · cache read 500 · out 9"}])
        self.assertNotIn("unknown", shot["overview"])
        # No known total: no denominator, so no shares and no bars (never zero-width ones), and no cache line.
        self.assertEqual([(b["share"], b["width"]) for b in shot["usageBars"]], [(None, None), (None, None)])
        self.assertIn("2 groups without a recorded total are excluded", shot["usageCaption"])
        self.assertIsNone(shot["usageCache"])

    def test_usage_bars_zero_and_missing_totals_backend_input_and_weighted_cache(self):
        def with_input(g, input_total):
            return {**g, "input_total": input_total}
        groups = [
            with_input(group("claude", "claude-opus-5-5", "writer", (100, 0, 100, 100), name="Opus 5.5",
                             share=0.5, total=300), 200),
            with_input(group("codex", "gpt-5.5", "reviewer", (0, None, 0, 0), source="requested", total=0), 0),
            group("claude", "claude-fable-5-1 + claude-haiku-4-5-20251001", "correction", (None, None, None, None),
                  source="mixed"),
            with_input(group("codex", "gpt-5.5", "planner", (40, None, 50, 10), name="GPT-5.5", share=0.5556,
                             total=100), 90)]
        seven = view("7", runs=[done("7-x", "claude", "writer", T(20, 0))], groups=groups)
        shot = self.page("", payloads([seven], [card(7, "In Progress", title="Seven")]), [{"snapshot": "s"}])["s"]
        bars = shot["usageBars"]
        # Denominator 300 + 0 + 100 = 400 (the missing total is not counted as zero).
        self.assertEqual([(b["share"], b["width"]) for b in bars],
                         [("75%", "75%"), ("0%", "0%"), (None, None), ("25%", "25%")])
        self.assertTrue(bars[0]["title"].startswith("300 recorded processed tokens (input 200 + output 100)"))
        self.assertTrue(bars[2]["title"].startswith("No recorded total; excluded from the shares"))
        # Provenance stays: exact ids, readable names and the requested / not split qualifiers.
        self.assertEqual([u["model"] for u in shot["usage"]],
                         ["Opus 5.5 · claude-opus-5-5", "gpt-5.5 (requested; not reported)",
                          "claude-fable-5-1 + claude-haiku-4-5-20251001 (not split)", "GPT-5.5 · gpt-5.5"])
        self.assertIn("1 group without a recorded total is excluded", shot["usageCaption"])
        # (100 + 0 + 50) / (200 + 0 + 90) = 51.7%, not the 52.8% mean of 50% and 55.6%; the mixed group has
        # no measured input and is named as excluded; cache writes are not all measured, so none are claimed.
        self.assertEqual(shot["usageCache"],
                         "Cache reuse: 51.7% of measured input · 1 group without measured input or cache reads excluded")
        for absent in ("saved", "$", "allowance used", "unknown"):
            self.assertNotIn(absent, shot["overview"])

    def test_usage_rows_from_real_records_completion_only_codex_multi_model_and_resumed(self):
        fable, haiku = "claude-fable-5-1", "claude-haiku-4-5-20251001"
        usage = lambda *n: dict(zip(("input_tokens", "cache_creation_input_tokens",  # noqa: E731
                                     "cache_read_input_tokens", "output_tokens"), n))
        per_model = lambda *n: dict(zip(("inputTokens", "cacheCreationInputTokens",  # noqa: E731
                                         "cacheReadInputTokens", "outputTokens"), n))

        def claude(run_id, role, started, used, model_usage, argv):
            result = {"type": "result", "subtype": "success", "is_error": False, "session_id": run_id,
                      "usage": used, "modelUsage": model_usage}
            return rec.build_record(task_id="2", run_id=run_id, role=role, model_requested="opus", events=[result],
                                    exit_code=0, wall_duration_ms=1000, started_at=started, ended_at=None,
                                    argv=argv, stream_path=None, api_key_env_present=False)

        # Codex reports usage once at turn end, here without a cache-write field.
        codex = rec.build_record(
            task_id="2", run_id="2-plan", role="reviewer", model_requested="gpt-5.5", provider="codex",
            events=[{"type": "thread.started", "thread_id": "t"},
                    {"type": "turn.completed", "usage": {"input_tokens": 15242, "cached_input_tokens": 12288,
                                                         "output_tokens": 5}}],
            exit_code=0, wall_duration_ms=1000, started_at="2026-10-01T01:00:00Z", ended_at=None,
            argv=["codex", "exec", "--json", "-m", "gpt-5.5", "-"], stream_path=None, api_key_env_present=False)
        records = [
            codex,
            # A fresh run with a Haiku subagent: modelUsage sums exactly to usage, so it splits by model.
            claude("2-correction", "correction", "2026-10-01T02:00:00Z", usage(18, 10079, 38085, 77),
                   {fable: per_model(15, 10079, 37085, 70), haiku: per_model(3, 0, 1000, 7)}, ["claude", "-p", "x"]),
            # A resumed run: modelUsage is session-cumulative and never summed.
            claude("2-resumed", "investigator", "2026-10-01T03:00:00Z", usage(5, 0, 500, 9),
                   {fable: per_model(900, 50000, 400000, 8000)}, ["claude", "-p", "x", "--resume", "s"])]
        for record in records:
            (self.runs / f"{record['run_id']}.json").write_text(json.dumps(record))
        real = self.real_payloads()
        task = next(t for t in real["/api/observability"]["body"]["tasks"] if t["task_id"] == "2")
        plan = next(r for r in task["summary"]["attribution"]["rows"] if r["run_id"] == "2-plan")
        self.assertIsNone(plan["tokens"]["cache_creation_input"])  # unknown kept in the data, not zero

        shot = self.page("", real, [{"snapshot": "s"}])["s"]
        self.assertEqual(shot["current"], "Task #2 · Fixture 2")
        self.assertEqual(shot["active"], [])  # the running record's pid is alive but is not its recorder
        self.assertEqual(shot["usage"], [
            {"provider": "claude", "who": "Claude · Implementer",
             "model": "Opus 5.5 · claude-opus-5-5 (requested; not reported)", "total": "1.1k",
             "parts": "fresh in 10 · cache read 1.0k · cache write 100 · out 7 · 90.1% cached input"},
            {"provider": "codex", "who": "Codex · Reviewer", "model": "GPT-5.5 · gpt-5.5 (requested; not reported)",
             "total": "15.2k", "parts": "fresh in 3.0k · cache read 12.3k · out 5 · 80.6% cached input"},
            {"provider": "claude", "who": "Claude · Correction", "model": "Fable 5.1 · claude-fable-5-1",
             "total": "47.2k", "parts": "fresh in 15 · cache read 37.1k · cache write 10.1k · out 70 · 78.6% cached input"},
            {"provider": "claude", "who": "Claude · Correction", "model": "Haiku 4.5 · claude-haiku-4-5-20251001",
             "total": "1.0k", "parts": "fresh in 3 · cache read 1.0k · cache write 0 · out 7 · 99.7% cached input"},
            {"provider": "claude", "who": "Claude · Investigator", "model": "Fable 5.1 · claude-fable-5-1",
             "total": "514", "parts": "fresh in 5 · cache read 500 · cache write 0 · out 9 · 99% cached input"}])
        for cumulative in ("400.0k", "8.0k", "50.0k"):
            self.assertNotIn(cumulative, shot["overview"])

    def test_weekly_cards_unavailable_expired_zero_full_and_failed_refresh(self):
        now = time.time()
        reading = lambda remaining, **extra: {"available": True, "remaining_percent": remaining,  # noqa: E731
                                              "used_percent": 100 - remaining, "resets_at": None,
                                              "measured_at": now - 30, "source": "s", "window_minutes": 10080,
                                              "refresh_error": None, **extra}
        quota = {"codex": reading(0), "claude": reading(100, refresh_error="Codex app-server timed out")}
        shot = self.page("", payloads([], [], quota), [{"snapshot": "s"}])["s"]
        codex, claude = shot["quota"]["codex"], shot["quota"]["claude"]
        self.assertEqual((codex["big"], codex["meter"], codex["reset"]), ("0%", "0", None))
        self.assertEqual((claude["big"], claude["meter"]), ("100%", "100"))
        self.assertEqual((codex["state"], claude["state"]), ("fresh", "stale"))
        self.assertIn("· not refreshed: Codex app-server timed out", claude["measured"])
        self.assertTrue(claude["measured"].startswith("Last measured "))
        # A reset time already passed in the browser: no meter, a calm line instead.
        quota = {"codex": reading(40, resets_at=now - 5),
                 "claude": {"available": False, "state": "expired",
                            "note": "Weekly window has reset; a new reading is needed",
                            "last_measured_at": now - 9 * 86400}}
        shot = self.page("", payloads([], [], quota), [{"snapshot": "s"}])["s"]
        for provider in ("codex", "claude"):
            card_ = shot["quota"][provider]
            self.assertEqual((card_["state"], card_["meter"], card_["big"]), ("expired", None, None))
            self.assertEqual(card_["note"], "Weekly window has reset · a new reading is needed")
            self.assertTrue(card_["measured"].startswith("Last measured "))
        self.assertNotIn("40%", shot["quota"]["codex"]["text"])
        # No /api/quota at all (older server or failure): unavailable, never a 0% meter.
        responses = payloads([], [])
        del responses["/api/quota"]
        shot = self.page("", responses, [{"snapshot": "s"}])["s"]
        self.assertEqual(shot["quota"]["codex"]["note"],
                         "Weekly allowance unavailable · the companion server could not be reached")
        self.assertNotIn("0%", shot["quota"]["claude"]["text"])
        self.assertEqual(shot["overview"], "Nothing is running or in progress, and no runs are recorded yet.")

    def test_weekly_pending_fetch_failure_stale_recovery_and_old_reading(self):
        now = time.time()
        fresh = {"available": True, "state": "fresh", "stale": False, "remaining_percent": 58, "used_percent": 42,
                 "resets_at": now + 86400 + 3600 + 120, "measured_at": now - 20, "source": "s",
                 "window_minutes": 10080, "refresh_error": None}
        pending = {"available": False, "state": "pending", "note": "Checking weekly allowance"}
        responses = payloads([], [], {"codex": pending, "claude": {"available": False, "state": "unavailable",
                                                                   "note": "No Claude Code status-line reading captured"}})
        good = {"body": {"ok": True, "providers": {"codex": fresh, "claude": dict(fresh, remaining_percent=12,
                                                                                  used_percent=88)}}}
        shots = self.page("", responses, [
            {"snapshot": "pending"},
            {"respond": "/api/quota", "with": good}, {"eval": "__app.loadQuota()"}, {"snapshot": "fresh"},
            {"respond": "/api/quota", "with": {"fail": True}}, {"eval": "__app.loadQuota()"}, {"snapshot": "down"},
            {"respond": "/api/quota", "with": good}, {"eval": "__app.loadQuota()"}, {"snapshot": "back"}])
        self.assertEqual(shots["pending"]["quota"]["codex"]["note"], "Checking weekly allowance…")
        self.assertIsNone(shots["pending"]["quota"]["codex"]["meter"])
        self.assertEqual(shots["pending"]["quota"]["claude"]["note"],
                         "Weekly allowance unavailable · No Claude Code status-line reading captured")
        codex = shots["fresh"]["quota"]["codex"]
        self.assertEqual((codex["state"], codex["big"], codex["reset"]), ("fresh", "58%", "Resets in 1d 1h"))
        self.assertEqual(shots["fresh"]["quota"]["claude"]["big"], "12%")
        # A failed fetch keeps the last values with their own time, clearly not fresh.
        down = shots["down"]["quota"]["codex"]
        self.assertEqual((down["state"], down["big"], down["meter"]), ("stale", "58%", "58"))
        self.assertIn("not refreshed: the companion server could not be reached", down["measured"])
        self.assertTrue(down["measured"].startswith("Last measured "))
        self.assertIn("Weekly allowance left · last measured", down["text"])
        self.assertEqual(shots["back"]["quota"]["codex"]["state"], "fresh")
        # The original failure: an older running server without the endpoint (404), no earlier payload.
        old = dict(payloads([], []), **{"/api/quota": {"status": 404, "body": {"ok": False, "error": "no such path"}}})
        shot = self.page("", old, [{"snapshot": "s"}])["s"]
        for provider in ("codex", "claude"):
            self.assertEqual(shot["quota"][provider]["note"], "Weekly allowance unavailable · this companion server "
                             "predates weekly allowance; restart the companion")
        # A reading the server still calls fresh but that is old by the browser's clock: stale.
        aged = dict(fresh, measured_at=now - 600)
        shot = self.page("", payloads([], [], {"codex": aged, "claude": aged}), [{"snapshot": "s"}])["s"]
        self.assertEqual(shot["quota"]["codex"]["state"], "stale")
        self.assertIn("not refreshed: no newer reading", shot["quota"]["codex"]["measured"])
        # A source polled every 5 minutes (Claude /usage) says how long it stays current; the layout is the same.
        polled = dict(fresh, measured_at=now - 300, stale_after_seconds=600)
        late = dict(polled, measured_at=now - 700)
        shot = self.page("", payloads([], [], {"codex": late, "claude": polled}), [{"snapshot": "s"}])["s"]
        self.assertEqual((shot["quota"]["claude"]["state"], shot["quota"]["codex"]["state"]), ("fresh", "stale"))

    def test_weekly_allowance_is_asked_on_every_live_3s_tick(self):
        # The page opened before the server's background read finished: first answer pending.
        now = time.time()
        pending = {"available": False, "state": "pending", "note": "Checking weekly allowance"}
        fresh = {"available": True, "state": "fresh", "remaining_percent": 70, "used_percent": 30, "resets_at": None,
                 "measured_at": now - 5, "source": "s", "window_minutes": 10080, "refresh_error": None}
        claude = {"available": False, "state": "unavailable", "note": "No Claude Code status-line reading captured"}
        good = {"body": {"ok": True, "providers": {"codex": fresh, "claude": claude}}}
        live = "document.getElementById('obs-live').checked = "
        shots = self.page("#kanban", payloads([], [], {"codex": pending, "claude": claude}), [
            {"snapshot": "first"},
            {"eval": live + "false"}, {"eval": "__tick(3000)"}, {"snapshot": "paused"},
            {"eval": live + "true"}, {"respond": "/api/quota", "with": good},
            {"eval": "__tick(3000)"}, {"snapshot": "next"},
            {"eval": "__tick(3000)"}, {"snapshot": "again"},
            {"eval": "__clock(57000); __tick(3000)"}, {"snapshot": "minute"}], fake_timers=True)
        self.assertEqual(shots["first"]["quota"]["codex"]["state"], "pending")
        self.assertEqual(shots["first"]["fetches"]["/api/quota"], 1)
        self.assertEqual(shots["paused"]["fetches"]["/api/quota"], 1)  # paused Live never fetches
        codex = shots["next"]["quota"]["codex"]
        self.assertEqual((codex["state"], codex["big"], shots["next"]["fetches"]["/api/quota"]), ("fresh", "70%", 2))
        # A fresh reading is still asked for on the next tick: a new status-line snapshot shows within 3 s.
        self.assertEqual(shots["again"]["fetches"]["/api/quota"], 3)
        self.assertEqual(shots["minute"]["fetches"]["/api/quota"], 4)
        self.assertEqual(shots["minute"]["quota"]["codex"]["state"], "fresh")

    def test_weekly_allowance_loads_at_startup_on_kanban_too(self):
        now = time.time()
        reading = {"available": True, "state": "fresh", "remaining_percent": 33, "used_percent": 67, "resets_at": None,
                   "measured_at": now - 5, "source": "s", "window_minutes": 10080, "refresh_error": None}
        shot = self.page("#kanban", payloads([], [], {"codex": reading, "claude": reading}), [{"snapshot": "s"}])["s"]
        self.assertTrue(shot["visible"]["kanban"])
        self.assertEqual((shot["quota"]["codex"]["state"], shot["quota"]["codex"]["big"]), ("fresh", "33%"))

    def test_supported_feed_fixture_through_capture_server_api_and_page(self):
        # SUPPORTED-FEED FIXTURE, not an account reading: the documented status-line payload shape.
        fixture = {"model": {"id": "claude-opus-5-5"}, "context_window": {"used_percentage": 12},
                   "rate_limits": {"seven_day": {"used_percentage": 27, "resets_at": time.time() + 2 * 86400}}}
        quota_file = self.workflow / "telemetry" / "claude-quota.json"
        done = subprocess.run([sys.executable, str(STATUSLINE), "--capture-only", "--quota-file", str(quota_file)],
                              input=json.dumps(fixture).encode(), capture_output=True, timeout=30)
        self.assertEqual((done.stdout, done.returncode), (b"", 0))
        with mock.patch.dict(os.environ, {"HARMONY_CODEX_QUOTA": "0", "HARMONY_CLAUDE_QUOTA_FILE": ""}):
            self.httpd.quota = server.default_quota(self.workflow)
        api = json.loads(self.request("GET", "/api/quota")[1])
        claude = api["providers"]["claude"]
        self.assertEqual((claude["state"], claude["remaining_percent"]), ("fresh", 73.0))
        self.assertEqual(api["providers"]["codex"]["note"], "Codex reading is turned off")
        responses = dict(payloads([], []), **{"/api/quota": {"body": api}})
        shot = self.page("", responses, [{"snapshot": "s"}])["s"]
        self.assertEqual((shot["quota"]["claude"]["state"], shot["quota"]["claude"]["big"],
                          shot["quota"]["claude"]["reset"]), ("fresh", "73%", "Resets in 1d 23h"))
        self.assertEqual(shot["quota"]["codex"]["note"], "Weekly allowance unavailable · Codex reading is turned off")

    def test_header_state_never_idle_when_uncheckable_and_paused_says_paused(self):
        maybe = view("8", live_runs=[live("8-review", "codex", "reviewer", T(40, 0), state="running (process not checkable)")])
        five = view("5", runs=[done("5-impl", "claude", "writer", T(50, 0))])
        shots = self.page("", payloads([maybe, five], [card(5, "Done", "DONE"), card(9, "In Progress")]),
                          [{"snapshot": "s"}, {"click": "#obs-live"}, {"snapshot": "paused"}])
        shot = shots["s"]
        self.assertEqual(shot["runState"], {"state": "unconfirmed", "text": "Cannot confirm"})
        self.assertEqual((shot["current"], shot["phase"]), ("Task #9 · Card 9", "Status IMPLEMENTING"))
        self.assertIsNone(shot["chart"])
        self.assertEqual(shots["paused"]["runState"], {"state": "paused", "text": "Paused"})
        # Alive by pid but not proven its recorder (reused pid, other process, `ps` denied): amber, and the
        # newer run never takes the current task from the In Progress card.
        pid_only = view("8", live_runs=[live("8-review", "codex", "reviewer", T(58, 0))])
        shot = self.page("", payloads([pid_only, five], [card(9, "In Progress")]), [{"snapshot": "s"}])["s"]
        self.assertEqual(shot["runState"], {"state": "unconfirmed", "text": "Cannot confirm"})
        self.assertEqual((shot["current"], shot["active"]), ("Task #9 · Card 9", []))
        proven = view("8", live_runs=[live("8-review", "codex", "reviewer", T(58, 0), active=True)])
        shot = self.page("", payloads([proven, five], [card(9, "In Progress")]), [{"snapshot": "s"}])["s"]
        self.assertEqual(shot["runState"], {"state": "running", "text": "Running"})
        self.assertEqual(shot["current"], "Task #8")
        mixed = view("8", live_runs=[live("8-review", "codex", "reviewer", T(58, 0), active=True),
                                     live("8-plan", "codex", "planner", T(57, 0))])
        shot = self.page("", payloads([mixed], []), [{"snapshot": "s"}])["s"]
        self.assertEqual(shot["runState"], {"state": "unconfirmed", "text": "Running · some unconfirmed"})
        idle = self.page("", payloads([five], []), [{"snapshot": "s"}])["s"]
        self.assertEqual(idle["runState"], {"state": "idle", "text": "Idle"})
        interrupted = self.page("", payloads([view("7", live_runs=[live("7-impl", "claude", "writer", T(30, 0),
                                                                            state="interrupted")])], []),
                                [{"snapshot": "s"}])["s"]
        self.assertEqual(interrupted["runState"], {"state": "interrupted", "text": "Interrupted"})
        broken = dict(payloads([five], []), **{"/api/observability": {"status": 500, "body": {"error": "boom"}}})
        self.assertEqual(self.page("", broken, [{"snapshot": "s"}])["s"]["runState"],
                         {"state": "unknown", "text": "Status unknown"})

    def test_recent_task_then_calm_empty_state(self):
        older = view("7", live_runs=[live("7-impl", "claude", "writer", T(30, 0), state="interrupted")])
        newer = view("5", runs=[done("5-impl", "claude", "writer", T(50, 0), model="claude-opus-5-5")])
        shot = self.page("", payloads([older, newer], [card(5, "Done", "DONE"), card(7, "Next")]),
                         [{"snapshot": "s"}])["s"]
        self.assertEqual(shot["current"], "Task #5 · Card 5")
        empty = self.page("", payloads([], [card(3, "Next")]), [{"snapshot": "s"}])["s"]
        self.assertEqual(empty["overview"], "Nothing is running or in progress, and no runs are recorded yet.")

    def test_phase_line_is_truthful_about_review_and_release(self):
        phases = [{"id": "implement", "type": "implement", "state": "completed"},
                  {"id": "review-1", "type": "review", "state": "completed", "outcome": "fail"},
                  {"id": "correct-1", "type": "correct", "state": "completed"},
                  {"id": "review-2", "type": "review", "state": "completed", "outcome": "pass"},
                  {"id": "release", "type": "release", "state": "blocked"}]
        seven = view("7", runs=[done("7-review", "codex", "reviewer", T(20, 0))], phases=phases)
        phase = lambda: self.page("", payloads([seven], [card(7, "In Progress")]),  # noqa: E731
                                  [{"snapshot": "s"}])["s"]["phase"]
        self.assertEqual(phase(), "Review complete · Awaiting approval")
        phases[3]["outcome"] = "fail"
        self.assertEqual(phase(), "Phase release · blocked")
        phases[3]["outcome"], phases[2]["state"] = "pass", "running"
        self.assertEqual(phase(), "Phase correct-1 · running")
        phases[2]["state"], phases[4]["state"] = "skipped", "skipped"
        self.assertEqual(phase(), "All phases completed")
        phases[4]["state"] = "pending"
        self.assertEqual(phase(), "Review complete · Awaiting approval")

    # -- Inspect Task ---------------------------------------------------------

    def test_inspect_from_overview_keeps_the_full_report_and_escape_returns(self):
        shots = self.page("", self.real_payloads(), [
            {"snapshot": "overview"}, {"click": "#overview a[data-inspect]"}, {"snapshot": "inspect"},
            {"key": "Escape"}, {"snapshot": "back"}])
        overview, inspect, back = shots["overview"], shots["inspect"], shots["back"]
        self.assertEqual(overview["current"], "Task #2 · Fixture 2")
        # The live record's pid (this test runner) is alive but is not its recorder: never green, no active line.
        self.assertEqual(overview["runState"], {"state": "unconfirmed", "text": "Cannot confirm"})
        self.assertIsNone(overview["chart"])
        self.assertEqual(overview["active"], [])
        self.assertEqual(overview["phase"], "Phase review-1 · running")

        self.assertEqual(inspect["hash"], "#inspect/2")
        self.assertEqual(inspect["visible"], {"observability": False, "kanban": False, "inspect": True})
        self.assertEqual(inspect["focus"]["id"], "inspect-title")
        self.assertEqual(inspect["back"], "← Back to Observability")
        for text in ("Task #2 — Fixture 2", "review-1", "Completed runs", "Telemetry · Implementer · 2-writer",
                     "Usage by provider and model", "Usage by role", "requested gpt-5.5", "Task total",
                     "Cached share", "Cache write", "Est. list cost (not a charge)",
                     # Only requested (no reported id in the record): the Inspect model cell says so.
                     "Opus 5.5 (claude-opus-5-5) (requested; not reported)", "running (unconfirmed)"):
            self.assertIn(text, inspect["inspect"])
        self.assertEqual(inspect["inspectTables"], 3)

        self.assertEqual(back["hash"], "#observability")
        self.assertEqual(back["visible"], {"observability": True, "kanban": False, "inspect": False})
        self.assertEqual((back["focus"]["inspect"], back["focus"]["inPanel"]), ("2", "panel-observability"))

    def test_inspect_from_kanban_back_button_and_details_stay_open_across_polls(self):
        responses = self.real_payloads()
        shots = self.page("#kanban", responses, [
            {"snapshot": "kanban"}, {"click": '#lanes a[data-inspect="1"]'}, {"snapshot": "inspect"},
            {"open": 'details.run-telemetry[data-run-id="1-writer"]'}, {"eval": "__app.loadInspect()"},
            {"snapshot": "polled"},
            {"respond": "/api/observability", "with": {"status": 500, "body": {"error": "boom"}}},
            {"eval": "__app.loadInspect()"}, {"snapshot": "failed"},
            {"click": "#inspect-back"}, {"snapshot": "back"},
            {"click": "#tab-observability"}, {"snapshot": "observability"}])
        kanban = shots["kanban"]["kanban"]
        self.assertEqual((kanban["cards"], kanban["inspectLinks"], kanban["edit"], kanban["move"]), (4, 4, 4, 4))
        self.assertFalse(kanban["filters"])
        self.assertEqual(shots["kanban"]["tabLabels"], ["Observability", "Tasks"])
        self.assertEqual(shots["inspect"]["back"], "← Back to Tasks")
        self.assertEqual({d["id"]: d["open"] for d in shots["polled"]["inspectDetails"]},
                         {"1-writer": True, "1-correction": False})
        failed = shots["failed"]
        self.assertIn("showing the last loaded report", failed["inspectMeta"])
        self.assertIn("Completed runs", failed["inspect"])
        back = shots["back"]
        self.assertEqual((back["hash"], back["visible"]["kanban"], back["visible"]["inspect"]), ("#kanban", True, False))
        self.assertEqual((back["focus"]["inspect"], back["focus"]["inPanel"]), ("1", "panel-kanban"))
        # The one status banner (lane counts, board feedback) lives in Tasks only.
        self.assertEqual(shots["kanban"]["status"]["inPanel"], "panel-kanban")
        self.assertIn("tasks — To Do", shots["kanban"]["status"]["text"])
        self.assertFalse(shots["kanban"]["status"]["hidden"])
        obs = shots["observability"]
        self.assertEqual(obs["status"]["count"], 1)
        self.assertEqual(obs["visible"], {"observability": True, "kanban": False, "inspect": False})
        self.assertTrue(obs["status"]["hidden"])  # effectively hidden with its panel
        self.assertTrue(shots["inspect"]["status"]["hidden"])


    def test_inspect_without_runs_missing_task_and_api_failure_are_calm(self):
        real = self.real_payloads()
        no_runs = self.page("#inspect/3", real, [{"snapshot": "s"}, {"key": "Escape"}, {"snapshot": "back"}])
        self.assertIn("Task #3 — Fixture 3", no_runs["s"]["inspect"])
        self.assertIn("No runs recorded for this task yet.", no_runs["s"]["inspect"])
        self.assertEqual(no_runs["back"]["hash"], "#observability")
        missing = self.page("#inspect/99", real, [{"snapshot": "s"}])["s"]
        self.assertEqual(missing["inspect"], "Task #99 was not found on the board or in run records.")
        broken = dict(real, **{"/api/observability": {"status": 500, "body": {"error": "boom"}}})
        failed = self.page("#inspect/3", broken, [{"snapshot": "s"}])["s"]
        self.assertIn("Run records could not be loaded: boom", failed["inspect"])
        down = {"/api/observability": {"fail": True}, "/api/board": {"fail": True}}
        offline = self.page("#inspect/2", down, [{"snapshot": "s"}])["s"]
        self.assertEqual(offline["inspect"], "Could not load this task: Failed to fetch")
        overview = self.page("", broken, [{"snapshot": "s"}])["s"]
        self.assertIn("Run records could not be loaded: boom", overview["overview"])
        self.assertEqual(overview["current"], "Task #2 · Fixture 2")  # still chosen from the board

    def test_scope_unverified_usage_stays_labelled_in_inspect(self):
        usage = {"input_tokens": 100, "cached_input_tokens": 20, "cache_write_input_tokens": 0, "output_tokens": 7}
        record = rec.build_record(
            task_id="4", run_id="4-resumed", role="reviewer", model_requested="gpt-5.5", provider="codex",
            events=[{"type": "turn.completed", "usage": usage}], exit_code=0, wall_duration_ms=1000,
            started_at="2026-10-01T00:00:00Z", ended_at=None,
            argv=["codex", "exec", "--json", "-m", "m", "resume", "old"], stream_path=None, api_key_env_present=False)
        (self.runs / "4-resumed.json").write_text(json.dumps(record))
        shot = self.page("#inspect/4", self.real_payloads(), [{"open": "details.run-telemetry"}, {"snapshot": "s"}])["s"]
        self.assertIn("Reported, scope unverified", shot["inspect"])
        self.assertIn("Cumulativeunknown", shot["inspect"])  # label and value cells, joined by textContent


if __name__ == "__main__":
    unittest.main()
