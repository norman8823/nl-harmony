#!/usr/bin/env python3
"""Board checks: the completed-card run line and the board basics.

Everything runs against temporary workflow and run-record directories on an
ephemeral loopback port; no live ticket or run record is read.

    python3 -B -m unittest discover -s task-board/tests -p 'test_*.py'
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / "task-board"))
sys.path.insert(0, str(ROOT / "direct-workflow"))

import direct_run_recorder as rec  # noqa: E402
import run_observability  # noqa: E402
import server  # noqa: E402


def ticket(task_id: int, status: str) -> dict:
    return {"schema_version": 2, "id": task_id, "title": f"Fixture {task_id}", "status": status,
            "scope": {"summary": "fixture"}, "acceptance_criteria": ["fixture"],
            "authorization": {"granted": False}, "blocker": None}


def run_record(task: str, run_id: str, output: int, *, wall: int | None = 60000) -> dict:
    usage = {"input_tokens": 10, "cache_creation_input_tokens": 100,
             "cache_read_input_tokens": 1000, "output_tokens": output}
    result = {"type": "result", "subtype": "success", "is_error": False, "session_id": run_id,
              "duration_ms": None, "usage": usage}
    return rec.build_record(
        task_id=task, run_id=run_id, role="writer", model_requested="claude-opus-5-5",
        events=[result], exit_code=0, wall_duration_ms=wall, started_at="2026-10-01T00:00:00Z",
        ended_at=None, argv=None, stream_path=None, api_key_env_present=False,
    )


class FakeQuota:
    """Stands in for the quota source, so no test starts codex or reads a login."""

    def __init__(self) -> None:
        self.calls = 0

    def payload(self) -> dict:
        self.calls += 1
        return {"providers": {"codex": {"available": False, "note": "fixture"},
                              "claude": {"available": False, "note": "fixture"}}}


class BoardFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="task-board-obs-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = self.tmp / "repo"
        self.workflow = self.repo / ".workflow"
        (self.workflow / "tickets").mkdir(parents=True)
        self.runs = self.tmp / "runs"
        self.runs.mkdir()
        for task_id, status in ((1, "DONE"), (2, "IMPLEMENTING"), (3, "DONE"), (4, "DONE")):
            (self.workflow / "tickets" / f"{task_id}.json").write_text(json.dumps(ticket(task_id, status)))
        records = [run_record("1", "1-writer", 500), run_record("1", "1-correction", 1500, wall=30000),
                   run_record("2", "2-writer", 7), run_record("4", "4-writer", 9, wall=None)]
        for record in records:
            (self.runs / f"{record['run_id']}.json").write_text(json.dumps(record))

        self.quota = FakeQuota()
        self.httpd = server.build_server(self.repo, self.workflow, port=0, session="test", actor="test",
                                         runs_dir=self.runs, quota=self.quota)
        self.port = self.httpd.server_address[1]
        thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def request(self, method: str, path: str, body: dict | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Host": f"127.0.0.1:{self.port}", "Connection": "close"}
        payload = None
        if body is not None:
            payload = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=payload, headers=headers)
        response = conn.getresponse()
        data = response.read()
        conn.close()
        return response.status, data

    def cards(self) -> dict:
        status, data = self.request("GET", "/api/board")
        self.assertEqual(status, 200)
        return {card["id"]: card for card in json.loads(data)["cards"]}


class CompletedCardLine(BoardFixture):
    def test_done_task_gets_the_shared_summary_line(self):
        card = self.cards()[1]["observability"]
        expected = run_observability.card_metrics(run_observability.summarize_task_dir(self.runs, "1"))
        self.assertEqual(card, expected)
        self.assertEqual(card["line"], "4.2k tok · 1m 30s")

    def test_only_tasks_with_records_get_a_line(self):
        cards = self.cards()
        # An active task with an attributed completed run gets the same shared line.
        self.assertEqual(cards[2]["observability"]["line"], "1.1k tok · 1m 00s")
        self.assertEqual(cards[2]["observability"]["running"], 0)
        self.assertIsNone(cards[3]["observability"])  # DONE without records

    def test_unsafe_duration_is_left_out(self):
        line = self.cards()[4]["observability"]["line"]
        self.assertEqual(line, "1.1k tok")
        self.assertIsNone(self.cards()[4]["observability"]["duration_ms"])

    def test_the_server_does_no_calculation_of_its_own(self):
        original = run_observability.card_metrics
        run_observability.card_metrics = lambda summary, live=None: {"line": f"shared:{summary['run_count']}"}
        try:
            self.assertEqual(self.cards()[1]["observability"], {"line": "shared:2"})
        finally:
            run_observability.card_metrics = original

    def test_page_has_the_metrics_slot(self):
        status, page = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b'class="run-metrics" hidden', page)


def add_running_review(fixture: BoardFixture) -> None:
    """Task 2: a graph with a running review phase and a live Codex reviewer run."""
    graph = {"kind": "task-graph/v1", "task_id": "2", "nodes": [
        {"id": "implement", "type": "implement", "role": "writer", "state": "completed",
         "run_ids": ["2-writer"], "started_at": "2026-10-01T00:00:00Z"},
        {"id": "review-1", "type": "review", "role": "reviewer", "state": "running", "run_ids": ["2-review"]}]}
    (fixture.workflow / "graphs").mkdir()
    (fixture.workflow / "graphs" / "2.json").write_text(json.dumps(graph))
    telemetry = rec.Telemetry("codex")
    telemetry.feed({"type": "item.started", "item": {"type": "command_execution"}}, "2026-10-01T00:05:00Z")
    (fixture.runs / "2-review.json").write_text(json.dumps({
        "schema": rec.SCHEMA, "run_id": "2-review", "task_id": "2", "role": "reviewer", "provider": "codex",
        "status": "running", "started_at": "2026-10-01T00:04:00Z",
        "model": {"requested": {"value": "gpt-5.5", "source": "recorder.argv"}},
        "recorder": {"pid": os.getpid(), "host": rec.socket.gethostname()},
        "telemetry": telemetry.snapshot()}))


class CompanionObservability(BoardFixture):
    """The Observability tab's data: the shared library's per-task view, read-only."""

    def setUp(self) -> None:
        super().setUp()
        add_running_review(self)

    def observability(self) -> dict:
        status, data = self.request("GET", "/api/observability")
        self.assertEqual(status, 200)
        return json.loads(data)

    def test_running_task_comes_first_with_live_phase_and_unknown_tokens(self):
        payload = self.observability()
        self.assertTrue(payload["available"])
        first = payload["tasks"][0]
        self.assertEqual((first["task_id"], first["title"], first["status"]), ("2", "Fixture 2", "IMPLEMENTING"))
        live = first["live"][0]
        self.assertEqual((live["state"], live["role_label"], live["provider"]), ("running", "Reviewer", "codex"))
        # The pid is alive but is this test runner, not the task's recorder: never shown as active.
        self.assertFalse(live["active"]["confirmed"])
        self.assertEqual(live["telemetry"]["phase"], "running a command")
        self.assertIsNone(live["telemetry"]["last_measured_at"])
        self.assertIsNone(live["telemetry"]["context"]["tokens"])
        phases = {p["id"]: p for p in first["phases"]}
        self.assertEqual(phases["implement"]["runs"][0]["status"], "recorded")
        # A live pid without the recorder proof never claims a running phase run.
        self.assertEqual(phases["review-1"]["runs"], [{"run_id": "2-review", "status": "running (unconfirmed)"}])
        proof = {"confirmed": True, "basis": "fixture proof"}
        with unittest.mock.patch.object(run_observability, "confirm_active", lambda *a, **k: proof):
            first = self.observability()["tasks"][0]
        phases = {p["id"]: p for p in first["phases"]}
        self.assertEqual(phases["review-1"]["runs"], [{"run_id": "2-review", "status": "running"}])
        self.assertEqual({t["task_id"] for t in payload["tasks"]}, {"1", "2", "4"})

    def test_resumed_codex_usage_is_served_unknown_with_scope_unverified_apart(self):
        usage = {"input_tokens": 100, "cached_input_tokens": 20, "cache_write_input_tokens": 0, "output_tokens": 7}
        record = rec.build_record(
            task_id="4", run_id="4-resumed", role="reviewer", model_requested="gpt-5.5", provider="codex",
            events=[{"type": "turn.completed", "usage": usage}], exit_code=0, wall_duration_ms=1000,
            started_at="2026-10-01T00:00:00Z", ended_at=None, argv=["codex", "exec", "--json", "-m", "m", "resume", "old"],
            stream_path=None, api_key_env_present=False)
        (self.runs / "4-resumed.json").write_text(json.dumps(record))
        view = next(t for t in self.observability()["tasks"] if t["task_id"] == "4")
        cum = view["telemetry"]["4-resumed"]["cumulative"]
        self.assertEqual((cum["processed_input"], cum["output"], cum["provisional"]), (None, None, None))
        self.assertEqual((cum["scope_unverified"]["processed_input"], cum["scope_unverified"]["output"]), (100, 7))
        self.assertIn("resumed thread", cum["basis"])
        script = self.request("GET", "/app.js")[1].decode()
        self.assertIn('"Reported, scope unverified"', script)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_sparkline_x_is_elapsed_time_not_sample_index(self):
        script = self.request("GET", "/app.js")[1].decode()
        start = script.index("function sparkTimes(")
        source = script[start:script.index("\n}\n", start) + 3]
        series = [{"t": "2026-10-01T00:00:00Z", "v": 1}, {"t": "2026-10-01T00:00:01Z", "v": 2},
                  {"t": "2026-10-01T00:00:10Z", "v": 3}, {"t": None, "v": 4}, {"t": "not a time", "v": 5},
                  {"t": "2026-10-01T00:00:02Z", "v": None}]
        probe = source + (f"\nconst a = sparkTimes({json.dumps(series)}, 'v');"
                          "\nconst same = sparkTimes([{t: '2026-10-01T00:00:00Z', v: 1}, {t: '2026-10-01T00:00:00Z', v: 2}], 'v');"
                          "\nconsole.log(JSON.stringify({pos: a.points.map((p) => p.pos), span: a.span,"
                          " untimed: a.untimed, same, one: sparkTimes([a.points[0]], 'v')}));")
        out = json.loads(subprocess.run(["node", "-e", probe], capture_output=True, text=True, check=True).stdout)
        self.assertEqual(out["pos"], [0, 0.1, 1])  # irregular gaps keep their proportions
        self.assertEqual((out["span"], out["untimed"]), (10000, 2))
        self.assertIsNone(out["same"])  # no elapsed time, no time axis
        self.assertIsNone(out["one"])

    def test_payload_is_the_shared_calculation(self):
        view = next(t for t in self.observability()["tasks"] if t["task_id"] == "1")
        expected = run_observability.summarize_task_dir(self.runs, "1")
        self.assertEqual(view["summary"]["tokens"], expected["tokens"])
        self.assertEqual(view["summary"]["attribution"]["by_role"], expected["attribution"]["by_role"])

    def test_active_card_shows_the_running_role_without_provisional_tokens(self):
        # Pid alive (this test runner) but not proven the task's recorder: no running role on the card.
        card = self.cards()[2]["observability"]
        self.assertEqual(card["line"], "1.1k tok · 1m 00s")
        self.assertEqual(card["running"], 0)
        proof = {"confirmed": True, "basis": "fixture proof"}
        with unittest.mock.patch.object(run_observability, "confirm_active", lambda *a, **k: proof):
            card = self.cards()[2]["observability"]
        self.assertTrue(card["line"].startswith("1.1k tok · 1m 00s · ▶ Reviewer"), card["line"])
        self.assertEqual(card["total_tokens"], 1117)  # the completed writer run only
        self.assertEqual(card["running"], 1)
        done = self.cards()[1]["observability"]
        self.assertNotIn("running", done)

    def test_quota_endpoint_is_read_only_and_guarded(self):
        status, data = self.request("GET", "/api/quota")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["providers"]["codex"], {"available": False, "note": "fixture"})
        for method in ("POST", "PATCH"):
            self.assertEqual(self.request(method, "/api/quota", {"x": 1})[0], 404)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", "/api/quota", headers={"Host": "evil.example", "Connection": "close"})
        response = conn.getresponse()
        response.read()
        self.assertEqual(response.status, 403)
        conn.close()
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", "/api/quota", headers={"Host": f"127.0.0.1:{self.port}", "Origin": "https://evil.example",
                                                    "Connection": "close"})
        response = conn.getresponse()
        response.read()
        self.assertEqual(response.status, 403)
        conn.close()
        self.assertEqual(self.quota.calls, 1)  # refused requests never reach the source

    def test_default_quota_source_paths_and_opt_out(self):
        with unittest.mock.patch.dict(os.environ, {"HARMONY_CODEX_QUOTA": "0", "HARMONY_CLAUDE_QUOTA_FILE": "",
                                                   "HARMONY_CLAUDE_CLI_QUOTA": "1"}):
            source = server.default_quota(self.workflow)
        self.assertIsNone(source.codex)
        self.assertEqual(source.claude_file, self.workflow / "telemetry" / "claude-quota.json")
        self.assertEqual(source.payload()["providers"]["codex"]["note"], "Codex reading is turned off")
        # The Claude /usage reader is installation-wide: Companions for different repositories share one
        # owner-only lock and cache under the temp dir. Built but never started here (no claude, nothing created).
        other = self.tmp / "other-repo" / ".workflow"
        with unittest.mock.patch.dict(os.environ, {"HARMONY_CODEX_QUOTA": "0", "HARMONY_CLAUDE_CLI_QUOTA": "1"}):
            elsewhere = server.default_quota(other)
        root = Path(tempfile.gettempdir()).absolute() / f"harmony-{os.geteuid()}"
        for cli in (source.claude_cli, elsewhere.claude_cli):
            self.assertEqual(cli.root, root)
            self.assertEqual(cli.cache.parent.parent, root / ".workflow" / "telemetry")
        self.assertEqual((source.claude_cli.cache, source.claude_cli.lock_path),
                         (elsewhere.claude_cli.cache, elsewhere.claude_cli.lock_path))
        self.assertNotEqual(source.claude_cli.cwd, elsewhere.claude_cli.cwd)  # each still runs in its own project
        self.assertFalse((self.workflow / "telemetry" / "claude-cli-quota.json").exists())
        self.assertEqual((source.claude_cli.reads, source.claude_worker.thread), (0, None))
        with unittest.mock.patch.dict(os.environ, {"HARMONY_CODEX_QUOTA": "0", "HARMONY_CLAUDE_CLI_QUOTA": "0"}):
            self.assertIsNone(server.default_quota(self.workflow).claude_cli)

    def test_observability_is_read_only(self):
        for method in ("POST", "PATCH"):
            status, _ = self.request(method, "/api/observability", {"x": 1})
            self.assertEqual(status, 404)

    def test_one_page_observability_default_then_tasks_with_two_tabs(self):
        page = self.request("GET", "/")[1].decode()
        self.assertLess(page.index('id="tab-observability"'), page.index('id="tab-kanban"'))
        self.assertIn('id="tab-observability" aria-controls="panel-observability" aria-selected="true"', page)
        self.assertIn('aria-selected="false" tabindex="-1">Tasks</button>', page)
        self.assertEqual(page.count('role="tab"'), 2)
        self.assertIn('id="panel-kanban" class="panel" role="tabpanel" aria-labelledby="tab-kanban" hidden', page)
        tasks = page[page.index('id="panel-kanban"'):page.index("<template")]
        self.assertIn('id="lanes"', tasks)
        self.assertNotIn("data-ns", page)
        script = self.request("GET", "/app.js")[1].decode()
        self.assertIn('const tab = location.hash === "#kanban" ? "kanban" : "observability";', script)
        self.assertIn('api("/api/observability")', script)

class QuotaLifecycle(unittest.TestCase):
    """Background collection belongs to the server: started explicitly, stopped on close."""

    def test_collected_at_start_before_any_request_and_stopped_on_close(self):
        import quota_telemetry as qt  # noqa: PLC0415
        tmp = Path(tempfile.mkdtemp(prefix="task-board-quota-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        workflow = tmp / ".workflow"
        (workflow / "tickets").mkdir(parents=True)
        spawned = []

        class Transport:  # a fake app-server: the real codex is never started
            def __init__(self):
                self.pending, self.closed = [], False

            def send(self, message):
                if message.get("id") == 0:
                    self.pending.append(b'{"id": 0, "result": {}}')
                if message.get("id") == 1:
                    self.pending.append(json.dumps({"id": 1, "result": {"rateLimits": {
                        "limitId": "codex", "primary": {"usedPercent": 25, "windowDurationMins": 10080}}}}).encode())

            def receive(self, timeout):
                if self.pending:
                    return self.pending.pop(0)
                raise __import__("queue").Empty

            def close(self):
                self.closed = True

        def spawn(command):
            spawned.append(Transport())
            return spawned[-1]

        codex = qt.CodexQuota(spawn=spawn, timeout=2)
        worker = qt.QuotaWorker(codex, interval=0.05)
        httpd = server.build_server(tmp, workflow, port=0, session="t", actor="t",
                                    quota=qt.QuotaSource(None, codex, worker=worker))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        httpd.start_quota()
        end = __import__("time").monotonic() + 5
        while codex.reads < 2:  # read at start and again periodically, with no request at all
            self.assertLess(__import__("time").monotonic(), end)
            __import__("time").sleep(0.01)
        conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=10)
        conn.request("GET", "/api/quota", headers={"Host": f"127.0.0.1:{httpd.server_address[1]}",
                                                    "Connection": "close"})
        body = json.loads(conn.getresponse().read())
        conn.close()
        self.assertEqual((body["providers"]["codex"]["state"], body["providers"]["codex"]["remaining_percent"]),
                         ("fresh", 75.0))
        httpd.shutdown()
        httpd.server_close()
        self.assertFalse(worker.thread.is_alive())
        self.assertTrue(all(t.closed for t in spawned))


class BoardBasics(BoardFixture):
    def test_edit_moves_a_card_and_refuses_protected_fields(self):
        status, data = self.request("PATCH", "/api/tasks/2", {"stage": "Blocked", "expected_revision": 0})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["card"]["board"]["stage"], "Blocked")
        stored = json.loads((self.workflow / "tickets" / "2.json").read_text())
        self.assertEqual(stored["status"], "IMPLEMENTING")
        # Status is a lifecycle field now; gates and grants stay protected.
        status, _ = self.request("PATCH", "/api/tasks/2", {"authorization": {"granted": True}, "expected_revision": 1})
        self.assertEqual(status, 403)
        status, _ = self.request("PATCH", "/api/tasks/2", {"status": "SHIPPED", "expected_revision": 1})
        self.assertEqual(status, 400)
        status, _ = self.request("PATCH", "/api/tasks/2", {"stage": "Next", "expected_revision": 0})
        self.assertEqual(status, 409)

    def test_add_task_without_the_intake_allocator_creates_a_canonical_ticket(self):
        status, data = self.request("POST", "/api/tasks", {"title": "A new card"})
        self.assertEqual(status, 201)
        self.assertEqual(json.loads(data)["created_by"], "canonical")
        self.assertEqual(sorted(p.name for p in (self.workflow / "tickets").glob("*.json")),
                         ["1.json", "2.json", "3.json", "4.json", "5.json"])

    def test_create_mode_follows_the_intake_file(self):
        board = json.loads(self.request("GET", "/api/board")[1])
        self.assertEqual((board["intake_available"], board["create_mode"]), (False, "canonical"))
        intake = self.repo / "agent-orchestrator" / "intake.py"
        intake.parent.mkdir(parents=True)
        intake.write_text("# stand-in; existence is the only capability checked\n")
        board = json.loads(self.request("GET", "/api/board")[1])
        self.assertEqual((board["intake_available"], board["create_mode"]), (True, "intake"))

    def test_page_enables_add_task_once_the_board_loads(self):
        page = self.request("GET", "/")[1]
        self.assertIn(b'id="new-card" class="primary" type="button" disabled>Add task</button>', page)
        script = self.request("GET", "/app.js")[1]
        self.assertIn(b"add.disabled = false;", script)
        self.assertNotIn(b"intake unavailable", script)

    def test_foreign_host_is_refused(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", "/api/board", headers={"Host": "evil.example"})
        self.assertEqual(conn.getresponse().status, 403)
        conn.close()


class BlockerShapes(BoardFixture):
    """A historical ticket whose blocker is plain text must not take the board down."""

    def setUp(self) -> None:
        super().setUp()
        shapes = {5: "Waiting for human approval of the release gate",
                  6: {"reason": "needs sign-off", "required_resolution": "Human signs off the plan"},
                  7: ["unsupported", "shape"]}
        for task_id, blocker in shapes.items():
            doc = {**ticket(task_id, "BLOCKED"), "blocker": blocker}
            (self.workflow / "tickets" / f"{task_id}.json").write_text(json.dumps(doc))

    def test_text_blocker_is_the_gate_and_both_endpoints_serve(self):
        cards = self.cards()
        self.assertEqual(cards[5]["board"]["gate"], "Waiting for human approval of the release gate")
        self.assertIsNone(cards[5]["blocker_reason"])
        self.assertEqual(cards[6]["board"]["gate"], "Human signs off the plan")
        self.assertEqual(cards[6]["blocker_reason"], "needs sign-off")
        self.assertEqual(cards[7]["board"]["gate"], "unknown")
        self.assertIsNone(cards[7]["blocker_reason"])
        status, _ = self.request("GET", "/api/observability")
        self.assertEqual(status, 200)

    def test_editing_a_text_blocker_card_keeps_the_canonical_ticket(self):
        before = json.loads((self.workflow / "tickets" / "5.json").read_text())
        status, data = self.request("PATCH", "/api/tasks/5", {"next_action": "Ask the owner", "expected_revision": 0})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["card"]["board"]["next_action"], "Ask the owner")
        after = json.loads((self.workflow / "tickets" / "5.json").read_text())
        # Only the board and the appended audit entry change; the canonical record does not.
        self.assertEqual({k: v for k, v in after.items() if k not in ("board", "history")}, before)
        self.assertEqual([(e["action"], e["board_fields"]) for e in after["history"]], [("edit", ["next_action"])])
        self.assertEqual(self.cards()[5]["board"]["gate"], before["blocker"])


if __name__ == "__main__":
    unittest.main()
