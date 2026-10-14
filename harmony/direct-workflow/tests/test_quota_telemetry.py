#!/usr/bin/env python3
"""Offline tests for quota_telemetry.py and the statusline quota snapshot.

Fake transports and clocks only: nothing here reads a login, starts the
installed codex or claude, or touches the network. The real-subprocess tests
run tiny Python stand-ins for `codex app-server` and for the CLI's PTY.

    python3 -B -m unittest discover -s direct-workflow/tests -p 'test_quota_telemetry.py'
"""

from __future__ import annotations

import io
import json
import math
import os
import queue
import stat
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

PACKAGE_DIR = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(PACKAGE_DIR))

import quota_telemetry as qt  # noqa: E402
import statusline as sl  # noqa: E402

NOW = 1_791_519_163.0
RESET = NOW + 3 * 86400


def window(used=29, minutes=10080, resets=RESET):
    return {"usedPercent": used, "windowDurationMins": minutes, "resetsAt": resets}


def bucket(primary=None, secondary=None, limit_id="codex"):
    return {"limitId": limit_id, "limitName": None, "primary": primary, "secondary": secondary}


def result(legacy=None, by_id=None):
    out = {}
    if legacy is not None:
        out["rateLimits"] = legacy
    if by_id is not None:
        out["rateLimitsByLimitId"] = by_id
    return out


class CodexParsing(unittest.TestCase):
    def weekly(self, res):
        return qt.codex_weekly(res, NOW)

    def test_explicit_week_length_selects_the_window_not_its_position(self):
        snap, why = self.weekly(result(bucket(window()), {"codex": bucket(window())}))
        self.assertIsNone(why)
        self.assertEqual((snap["used_percent"], snap["remaining_percent"], snap["resets_at"], snap["window_minutes"]),
                         (29.0, 71.0, RESET, 10080))
        snap, _ = self.weekly(result(bucket(window(90, 300), window(12))))
        self.assertEqual(snap["remaining_percent"], 88.0)  # secondary is the weekly one here
        snap, why = self.weekly(result(bucket(window(29, 300), window(40, 1440))))
        self.assertIsNone(snap)
        self.assertEqual(why, "no weekly (10080-minute) window reported")
        for minutes in ("10080", 10080.0, True, None):
            self.assertIsNone(self.weekly(result(bucket(window(minutes=minutes))))[0], minutes)

    def test_conflicting_and_model_specific_buckets_are_not_guessed(self):
        both = result(bucket(window(10), window(20)))
        self.assertEqual(self.weekly(both), (None, "conflicting weekly windows"))
        legacy_vs_map = result(bucket(window(10)), {"codex": bucket(window(30))})
        self.assertEqual(self.weekly(legacy_vs_map), (None, "conflicting weekly windows"))
        other = result(bucket(window(5), limit_id="codex_model_x"), {"codex_model_x": bucket(window(5))})
        self.assertEqual(self.weekly(other), (None, "no general Codex limit reported"))
        # A model-specific bucket next to the general one is ignored.
        snap, _ = self.weekly(result(bucket(window(29)), {"codex": bucket(window(29)),
                                                           "codex_model_x": bucket(window(80))}))
        self.assertEqual(snap["used_percent"], 29.0)
        self.assertEqual(self.weekly(None), (None, "unreadable response"))
        self.assertEqual(self.weekly({}), (None, "no general Codex limit reported"))

    def test_used_percent_must_be_a_real_number_in_range(self):
        for bad in (None, "29", True, False, math.nan, math.inf, -0.5, 100.01, [29]):
            snap, why = self.weekly(result(bucket(window(bad))))
            self.assertIsNone(snap, bad)
            self.assertEqual(why, "weekly usage not a valid percentage")
        self.assertEqual(self.weekly(result(bucket(window(0))))[0]["remaining_percent"], 100.0)
        self.assertEqual(self.weekly(result(bucket(window(100))))[0]["remaining_percent"], 0.0)
        # Missing or null reset is supported (reset unknown); a present but malformed one is not.
        self.assertIsNone(self.weekly(result(bucket(window(resets=None))))[0]["resets_at"])
        missing = window()
        del missing["resetsAt"]
        self.assertIsNone(self.weekly(result(bucket(missing)))[0]["resets_at"])
        for bad_reset in ("soon", True, False, -1, 0, math.nan, math.inf, {}, [1], 10**400):
            self.assertEqual(self.weekly(result(bucket(window(resets=bad_reset)))),
                             (None, "weekly reset time not valid"), bad_reset)

    def test_huge_numbers_are_unknown_not_an_overflow(self):
        self.assertEqual(self.weekly(result(bucket(window(10**400)))), (None, "weekly usage not a valid percentage"))
        self.assertEqual(self.weekly(result(bucket(window(resets=10**400)))), (None, "weekly reset time not valid"))
        self.assertEqual(self.weekly(result(bucket(window(resets=1.5e9))))[0]["resets_at"], 1.5e9)
        self.assertIsNone(self.weekly(result(bucket(window(minutes=10**400))))[0])


class Presenting(unittest.TestCase):
    def test_current_expired_and_stale_semantics(self):
        snap = qt.snapshot("codex", qt.CODEX_SOURCE, 29.0, RESET, NOW)
        shown = qt.present(snap, NOW + 60)
        self.assertEqual((shown["available"], shown["remaining_percent"], shown["measured_at"], shown["refresh_error"]),
                         (True, 71.0, NOW, None))
        self.assertEqual((shown["state"], shown["stale"]), ("fresh", False))
        expired = qt.present(snap, RESET)
        self.assertEqual(expired, {"available": False, "state": "expired",
                                   "note": "Weekly window has reset; a new reading is needed", "last_measured_at": NOW})
        no_reset = qt.snapshot("claude", qt.CLAUDE_SOURCE, 10.0, None, NOW)
        self.assertTrue(qt.present(no_reset, NOW + 6 * 86400)["available"])
        self.assertFalse(qt.present(no_reset, NOW + qt.WEEK_SECONDS)["available"])
        stale = qt.present(snap, NOW + 60, error="Codex app-server timed out")
        self.assertEqual((stale["available"], stale["state"], stale["refresh_error"], stale["measured_at"]),
                         (True, "stale", "Codex app-server timed out", NOW))
        self.assertEqual(qt.present(None, NOW, error="Codex CLI not found"),
                         {"available": False, "state": "unavailable", "note": "Codex CLI not found"})
        # Old without an error (about two refresh intervals): last value kept, marked stale.
        old = qt.present(snap, NOW + qt.STALE_AFTER + 1)
        self.assertEqual((old["state"], old["remaining_percent"], old["measured_at"]), ("stale", 71.0, NOW))
        self.assertEqual(qt.present(snap, NOW + qt.STALE_AFTER)["state"], "fresh")
        # The first check has not finished: pending, never "unavailable".
        self.assertEqual(qt.present(None, NOW, pending=True),
                         {"available": False, "state": "pending", "note": "Checking weekly allowance"})


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def line(message) -> bytes:
    return (json.dumps(message) + "\n").encode()


class FakeTransport:
    """Replies scripted per request method; an empty queue times out at once."""

    def __init__(self, replies, clock=None):
        self.replies, self.clock = replies, clock
        self.sent, self.pending, self.closed = [], [], False

    def send(self, message):
        self.sent.append(message)
        self.pending.extend(self.replies.get(message.get("method"), []))

    def receive(self, timeout):
        if self.pending:
            return self.pending.pop(0)
        if self.clock is not None:
            self.clock.t += timeout
        raise queue.Empty

    def close(self):
        self.closed = True


def ok_replies(used=29, before=()):
    return {"initialize": [line({"id": 0, "result": {"userAgent": "x"}})],
            "account/rateLimits/read": [*before, line({"id": 1, "result": result(bucket(window(used)))})]}


class CodexReader(unittest.TestCase):
    def reader(self, replies, **kw):
        clock = Clock()
        made = []

        def spawn(command):
            transport = FakeTransport(replies() if callable(replies) else replies, clock)
            made.append(transport)
            return transport

        reader = qt.CodexQuota(spawn=spawn, monotonic=clock, wall=lambda: NOW, ttl=60, timeout=5, **kw)
        return reader, made, clock

    def test_official_handshake_then_read_and_cache_by_monotonic_ttl(self):
        reader, made, clock = self.reader(ok_replies)
        state = reader.state()
        self.assertEqual((state["snapshot"]["remaining_percent"], state["error"]), (71.0, None))
        sent = made[0].sent
        self.assertEqual([m.get("method") for m in sent], ["initialize", "initialized", "account/rateLimits/read"])
        self.assertEqual(sent[0]["id"], 0)
        self.assertEqual(sent[0]["params"]["clientInfo"],
                         {"name": "harmony_companion", "title": "Harmony Companion", "version": "0.1"})
        self.assertNotIn("id", sent[1])
        self.assertEqual(sent[2]["id"], 1)
        self.assertTrue(made[0].closed)
        clock.t += 59
        reader.state()
        self.assertEqual(reader.reads, 1)  # a 3 s poll does not start a process each time
        clock.t += 2
        reader.state()
        self.assertEqual(reader.reads, 2)

    def test_updated_notifications_apply_and_a_failed_read_keeps_the_last_reading(self):
        note = line({"method": "account/rateLimits/updated", "params": {"rateLimits": bucket(window(50))}})
        reader, made, clock = self.reader({"initialize": [note, line({"id": 0, "result": {}})],
                                           "account/rateLimits/read": [line({"id": 1, "error": {"code": -1}})]})
        state = reader.state()
        self.assertEqual(state["snapshot"]["used_percent"], 50.0)
        self.assertEqual(state["error"], "Codex did not report rate limits (signed in?)")
        # The read result, when it comes, wins over an earlier notification.
        reader, _, _ = self.reader(ok_replies(29, before=[note]))
        self.assertEqual(reader.state()["snapshot"]["used_percent"], 29.0)
        # A notification for another bucket is ignored.
        other = line({"method": "account/rateLimits/updated",
                      "params": {"rateLimits": bucket(window(1), limit_id="codex_model_x")}})
        reader, _, _ = self.reader(ok_replies(29, before=[other]))
        self.assertEqual(reader.state()["snapshot"]["used_percent"], 29.0)

    def test_malformed_refresh_keeps_last_reading_and_its_measured_time(self):
        bad = {"initialize": [line({"id": 0, "result": {}})],
               "account/rateLimits/read": [line({"id": 1, "result": result(bucket(window(resets={})))})]}
        replies = iter([ok_replies(), bad])
        reader, _, clock = self.reader(lambda: next(replies))
        first = reader.state()["snapshot"]
        clock.t += 61
        state = reader.state()
        self.assertEqual(state["error"], "weekly reset time not valid")
        self.assertIs(state["snapshot"], first)
        self.assertEqual(state["snapshot"]["measured_at"], NOW)

    def test_timeout_missing_cli_oversize_and_closed_stream_are_unavailable_and_cleaned_up(self):
        replies = iter([ok_replies(), {}])
        reader, made, clock = self.reader(lambda: next(replies))
        self.assertIsNone(reader.state()["error"])
        clock.t += 61
        state = reader.state()
        self.assertEqual(state["error"], "Codex app-server timed out")
        self.assertEqual(state["snapshot"]["used_percent"], 29.0)  # last valid reading kept
        self.assertTrue(all(t.closed for t in made))

        def missing(command):
            raise FileNotFoundError(command[0])

        absent = qt.CodexQuota(spawn=missing, wall=lambda: NOW)
        self.assertEqual(absent.state(), {"snapshot": None, "error": "Codex CLI not found", "attempted": True})
        big, made, _ = self.reader({"initialize": [b"x" * (qt.MAX_LINE + 1)]})
        self.assertEqual(big.state()["error"], "Codex app-server reply too large")
        self.assertTrue(made[0].closed)
        closed, _, _ = self.reader({"initialize": [None]})
        self.assertEqual(closed.state()["error"], "Codex app-server closed")
        noisy, _, _ = self.reader({"initialize": [b"not json\n"] * (qt.MAX_MESSAGES + 5)})
        self.assertEqual(noisy.state()["error"], "Codex app-server sent too many messages")

    def test_concurrent_requests_share_one_read(self):
        started = threading.Event()

        class Slow(FakeTransport):
            def send(self, message):
                started.set()
                time.sleep(0.05)
                super().send(message)

        reader = qt.CodexQuota(spawn=lambda c: Slow(ok_replies()), wall=lambda: NOW, ttl=60, timeout=5)
        results = []
        threads = [threading.Thread(target=lambda: results.append(reader.state())) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(reader.reads, 1)
        self.assertEqual({r["snapshot"]["used_percent"] for r in results}, {29.0})

    def test_real_subprocess_transport_reads_and_is_stopped(self):
        stand_in = (
            "import json,sys\n"
            "for raw in sys.stdin:\n"
            "    m = json.loads(raw)\n"
            "    if m.get('id') == 0: print(json.dumps({'id': 0, 'result': {}}), flush=True)\n"
            "    if m.get('id') == 1: print(json.dumps({'id': 1, 'result': {'rateLimits': "
            "{'limitId': 'codex', 'primary': {'usedPercent': 12, 'windowDurationMins': 10080, 'resetsAt': 9e9}}}}), flush=True)\n"
        )
        made = []

        def spawn(command):
            made.append(qt.Transport([sys.executable, "-c", stand_in]))
            return made[0]

        state = qt.CodexQuota(spawn=spawn, timeout=10).state()
        self.assertEqual((state["snapshot"]["remaining_percent"], state["error"]), (88.0, None))
        self.assertIsNotNone(made[0].proc.poll())

        hung = []

        def spawn_hung(command):
            hung.append(qt.Transport([sys.executable, "-c", "import time; time.sleep(60)"]))
            return hung[0]

        state = qt.CodexQuota(spawn=spawn_hung, timeout=0.3).state()
        self.assertEqual(state["error"], "Codex app-server timed out")
        self.assertIsNotNone(hung[0].proc.poll())  # terminated, not left running


class BoundedReader(unittest.TestCase):
    """Real subprocess stand-ins (never codex): output is read and buffered within bounds."""

    def transport(self, script):
        return qt.Transport([sys.executable, "-c", script])

    def assert_cleaned(self, transport, started):
        transport.close()
        self.assertLess(time.monotonic() - started, 6)
        self.assertIsNotNone(transport.proc.poll())
        self.assertFalse(transport.reader.is_alive())

    def test_flood_is_neither_read_nor_buffered_without_bound(self):
        flood = "import sys\nwhile True:\n    sys.stdout.write('{}\\n' * 1000)\n"
        started = time.monotonic()
        transport = self.transport(flood)
        time.sleep(0.4)  # nothing consumes: the reader must stop at the queue bound
        self.assertLessEqual(transport.lines.qsize(), qt.QUEUE_LINES)
        self.assertLessEqual(transport.produced, qt.QUEUE_LINES + 1)
        self.assert_cleaned(transport, started)
        # Consumed through CodexQuota: stops at the message cap, then cleans up.
        made = []

        def spawn(command):
            made.append(self.transport(flood))
            return made[0]

        started = time.monotonic()
        state = qt.CodexQuota(spawn=spawn, timeout=10).state()
        self.assertEqual(state["error"], "Codex app-server sent too many messages")
        self.assertLessEqual(made[0].produced, qt.MAX_MESSAGES + 1)
        self.assertIsNotNone(made[0].proc.poll())
        self.assertFalse(made[0].reader.is_alive())
        self.assertLess(time.monotonic() - started, 8)

    def test_oversized_line_stops_reading(self):
        big = "import sys, time\nsys.stdout.write('x' * (3 << 20))\nsys.stdout.flush()\ntime.sleep(60)\n"
        made = []

        def spawn(command):
            made.append(self.transport(big))
            return made[0]

        started = time.monotonic()
        state = qt.CodexQuota(spawn=spawn, timeout=10).state()
        self.assertEqual(state["error"], "Codex app-server reply too large")
        self.assertEqual(made[0].produced, 1)
        self.assertIsNotNone(made[0].proc.poll())
        self.assertFalse(made[0].reader.is_alive())
        self.assertLess(time.monotonic() - started, 6)


class BackgroundCollection(unittest.TestCase):
    """The server's worker: reads at start and periodically; requests only look."""

    def source(self, spawn, interval=0.05):
        # A fixed wall clock: readings carry resetsAt = NOW + 3 days, which real time has passed.
        codex = qt.CodexQuota(spawn=spawn, wall=lambda: NOW, ttl=60, timeout=2)
        worker = qt.QuotaWorker(codex, interval=interval)
        source = qt.QuotaSource(None, codex, worker=worker, wall=lambda: NOW)
        self.addCleanup(source.close)
        return source, codex, worker

    def wait_for(self, condition, limit=5.0):
        end = time.monotonic() + limit
        while not condition():
            self.assertLess(time.monotonic(), end, "condition not reached")
            time.sleep(0.01)

    def test_reads_at_start_then_periodically_and_stops_on_close(self):
        made = []

        def spawn(command):
            made.append(FakeTransport(ok_replies()))
            return made[-1]

        source, codex, worker = self.source(spawn)
        self.assertEqual(source.payload()["providers"]["codex"]["state"], "pending")  # not started: no read
        self.assertEqual(codex.reads, 0)
        source.start()
        self.wait_for(lambda: codex.reads >= 3)
        shown = source.payload()["providers"]["codex"]
        self.assertEqual((shown["state"], shown["remaining_percent"]), ("fresh", 71.0))
        source.close()
        self.assertFalse(worker.thread.is_alive())
        reads = codex.reads
        time.sleep(0.2)
        self.assertEqual(codex.reads, reads)  # stopped: no further reads
        self.assertTrue(all(t.closed for t in made))

    def test_requests_never_read_and_never_wait_for_a_read_in_progress(self):
        release = threading.Event()

        class Slow(FakeTransport):
            def receive(self, timeout):
                if not release.is_set():
                    time.sleep(min(timeout, 0.02))
                    raise queue.Empty
                return super().receive(timeout)

        source, codex, worker = self.source(lambda c: Slow(ok_replies()), interval=60)
        source.start()
        self.wait_for(lambda: codex.transport is not None)
        started = time.monotonic()
        shown = source.payload()["providers"]["codex"]
        self.assertLess(time.monotonic() - started, 0.2)
        self.assertEqual(shown, {"available": False, "state": "pending", "note": "Checking weekly allowance"})
        release.set()
        self.wait_for(lambda: source.payload()["providers"]["codex"]["state"] == "fresh")
        for _ in range(20):
            source.payload()
        self.assertEqual(codex.reads, 1)  # one collector; requests only peek

    def test_close_aborts_a_read_in_progress_promptly(self):
        made = []

        class Hanging(FakeTransport):
            def receive(self, timeout):
                time.sleep(min(timeout, 0.02))
                raise queue.Empty

        def spawn(command):
            made.append(Hanging({}))
            return made[-1]

        codex = qt.CodexQuota(spawn=spawn, wall=time.time, timeout=30)
        worker = qt.QuotaWorker(codex, interval=60)
        worker.start()
        self.wait_for(lambda: codex.transport is not None)
        started = time.monotonic()
        worker.close()
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertFalse(worker.thread.is_alive())
        self.assertEqual(codex.error, "Codex reading stopped")
        self.assertTrue(made[0].closed)

    def test_failed_refresh_is_stale_with_the_old_time_then_recovers(self):
        clock = Clock()
        wall = [NOW]
        failing = {"initialize": [line({"id": 0, "result": {}})],
                   "account/rateLimits/read": [line({"id": 1, "error": {"code": -1}})]}
        replies = iter([ok_replies(), failing, ok_replies(40)])
        codex = qt.CodexQuota(spawn=lambda c: FakeTransport(next(replies), clock), monotonic=clock,
                              wall=lambda: wall[0], ttl=60, timeout=5)
        source = qt.QuotaSource(None, codex, wall=lambda: wall[0])
        self.assertEqual(source.payload()["providers"]["codex"]["state"], "fresh")
        clock.t += 61
        wall[0] = NOW + 61
        stale = source.payload()["providers"]["codex"]
        self.assertEqual((stale["state"], stale["remaining_percent"], stale["measured_at"], stale["refresh_error"]),
                         ("stale", 71.0, NOW, "Codex did not report rate limits (signed in?)"))
        clock.t += 61
        wall[0] = NOW + 122
        fresh = source.payload()["providers"]["codex"]
        self.assertEqual((fresh["state"], fresh["remaining_percent"], fresh["measured_at"]), ("fresh", 60.0, NOW + 122))


class StatuslineCapture(unittest.TestCase):
    """`statusline.py --capture-only [-- command]`: keeps the reading, preserves any status line."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="capture-"))
        self.addCleanup(__import__("shutil").rmtree, self.tmp, ignore_errors=True)
        self.file = self.tmp / ".workflow" / "telemetry" / "claude-quota.json"
        self.home = self.tmp / "home"
        (self.home / ".claude").mkdir(parents=True)
        self.settings = self.home / ".claude" / "settings.json"
        self.settings.write_text('{"statusLine": {"type": "command", "command": "mine"}}')

    def run_capture(self, raw: bytes, *command, extra=()):
        argv = [sys.executable, str(PACKAGE_DIR / "statusline.py"), "--capture-only", "--quota-file", str(self.file),
                *extra]
        if command:
            argv += ["--", *command]
        env = {"PATH": __import__("os").environ.get("PATH", ""), "HOME": str(self.home)}
        return __import__("subprocess").run(argv, input=raw, capture_output=True, cwd=self.tmp, env=env, timeout=30)

    def test_wrapped_command_gets_exact_stdin_and_keeps_its_output_and_exit_status(self):
        raw = b'{ "model": {"display_name": "Opus"},\n  "rate_limits": {"seven_day": {"used_percentage": 36, "resets_at": 9e9}} }'
        echo = ("import sys; d = sys.stdin.buffer.read(); sys.stdout.buffer.write(b'LINE:' + d); "
                "sys.stderr.write('note'); sys.exit(7)")
        done = self.run_capture(raw, sys.executable, "-c", echo)
        self.assertEqual((done.stdout, done.stderr, done.returncode), (b"LINE:" + raw, b"note", 7))
        saved = json.loads(self.file.read_text())
        self.assertEqual(sorted(saved), sorted(qt.SNAPSHOT_KEYS))
        self.assertEqual((saved["remaining_percent"], saved["resets_at"]), (64.0, 9e9))
        self.assertNotIn("Opus", self.file.read_text())  # nothing but the allowlisted reading
        self.assertEqual(self.settings.read_text(), '{"statusLine": {"type": "command", "command": "mine"}}')
        self.assertEqual(sorted(p.name for p in self.home.rglob("*")), [".claude", "settings.json"])
        # A shell command is the operator's explicit choice.
        done = self.run_capture(raw, "sh", "-c", "wc -c")
        self.assertEqual((done.stdout.strip(), done.returncode), (str(len(raw)).encode(), 0))

    def test_capture_only_without_command_prints_nothing(self):
        done = self.run_capture(b'{"rate_limits": {"seven_day": {"used_percentage": 0}}}')
        self.assertEqual((done.stdout, done.stderr, done.returncode), (b"", b"", 0))
        self.assertEqual(json.loads(self.file.read_text())["remaining_percent"], 100.0)
        self.run_capture(b'{"rate_limits": {"seven_day": {"used_percentage": 100}}}')
        self.assertEqual(json.loads(self.file.read_text())["remaining_percent"], 0.0)

    def test_absent_malformed_or_unrelated_payloads_keep_the_last_reading(self):
        self.run_capture(b'{"rate_limits": {"seven_day": {"used_percentage": 20, "resets_at": 9e9}}}')
        first = self.file.read_text()
        for raw in (b'{"model": {"id": "claude-opus-5-5"}, "context_window": {"used_percentage": 50}}',
                    b'{"rate_limits": {"five_hour": {"used_percentage": 5}}}',
                    b'{"rate_limits": {"seven_day": {"used_percentage": 20, "resets_at": {}}}}',
                    b'{"rate_limits": {"seven_day": {"used_percentage": 101}}}',
                    b"not json at all", b""):
            done = self.run_capture(raw, sys.executable, "-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())")
            self.assertEqual((done.stdout, done.returncode), (raw, 0))
            self.assertEqual(self.file.read_text(), first)

    def test_identical_reading_from_another_session_leaves_the_file_and_the_wrapper_alone(self):
        week = '"rate_limits": {"seven_day": {"used_percentage": 3, "resets_at": 1792126800}}'
        echo = "import sys; sys.stdout.buffer.write(b'LINE:' + sys.stdin.buffer.read()); sys.exit(3)"
        first = ('{"session_id": "a", "cost": {"total_api_duration_ms": 2235}, ' + week + '}').encode()
        self.assertEqual(self.run_capture(first, sys.executable, "-c", echo).returncode, 3)
        before, stat = self.file.read_bytes(), self.file.stat()
        time.sleep(0.05)
        # A timer refresh of the same session, then another session replaying the same reading.
        for raw in (first, ('{"session_id": "b", "transcript_path": "/elsewhere", ' + week + '}').encode()):
            done = self.run_capture(raw, sys.executable, "-c", echo)
            self.assertEqual((done.stdout, done.stderr, done.returncode), (b"LINE:" + raw, b"", 3))
            self.assertEqual(self.file.read_bytes(), before)
            self.assertEqual((self.file.stat().st_ino, self.file.stat().st_mtime_ns), (stat.st_ino, stat.st_mtime_ns))
        # A changed percentage is a new reading.
        self.run_capture(first.replace(b'"used_percentage": 3', b'"used_percentage": 4'))
        saved = json.loads(self.file.read_text())
        self.assertEqual(saved["used_percent"], 4.0)
        self.assertGreater(saved["measured_at"], json.loads(before)["measured_at"])

    def test_missing_command_timeout_and_misuse(self):
        raw = b"{}"
        self.assertEqual(self.run_capture(raw, str(self.tmp / "no-such-command")).returncode, 127)
        slow = self.run_capture(raw, sys.executable, "-c", "import time; time.sleep(10)",
                                extra=("--command-timeout", "0.3"))
        self.assertEqual(slow.returncode, 124)
        misuse = __import__("subprocess").run([sys.executable, str(PACKAGE_DIR / "statusline.py"), "--", "true"],
                                              input=raw, capture_output=True, timeout=30)
        self.assertEqual(misuse.returncode, 2)


class ClaudeSnapshot(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="quota-"))
        self.file = self.tmp / ".workflow" / "telemetry" / "claude-quota.json"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_seven_day_reading_validation(self):
        data = {"rate_limits": {"seven_day": {"used_percentage": 36, "resets_at": RESET},
                                "five_hour": {"used_percentage": 99}}}
        snap = qt.claude_weekly(data, NOW)
        self.assertEqual((snap["provider"], snap["remaining_percent"], snap["resets_at"], snap["source"]),
                         ("claude", 64.0, RESET, qt.CLAUDE_SOURCE))
        for bad in ({}, {"rate_limits": None}, {"rate_limits": {"five_hour": {"used_percentage": 5}}},
                    {"rate_limits": {"seven_day": {"used_percentage": "36"}}},
                    {"rate_limits": {"seven_day": {"used_percentage": 140}}},
                    {"rate_limits": {"seven_day": {"used_percentage": True}}}, None,
                    {"rate_limits": {"seven_day": {"used_percentage": 10**400}}},
                    {"rate_limits": {"seven_day": {"used_percentage": 36, "resets_at": 10**400}}},
                    {"rate_limits": {"seven_day": {"used_percentage": 36, "resets_at": {}}}},
                    {"rate_limits": {"seven_day": {"used_percentage": 36, "resets_at": "tomorrow"}}},
                    {"rate_limits": {"seven_day": {"used_percentage": 36, "resets_at": -5}}}):
            self.assertIsNone(qt.claude_weekly(bad, NOW), bad)
        unknown_reset = qt.claude_weekly({"rate_limits": {"seven_day": {"used_percentage": 36, "resets_at": None}}}, NOW)
        self.assertEqual((unknown_reset["remaining_percent"], unknown_reset["resets_at"]), (64.0, None))

    def test_sanitized_atomic_write_inside_workflow_only(self):
        snap = dict(qt.snapshot("claude", qt.CLAUDE_SOURCE, 36.0, RESET, NOW), session_id="secret",
                    transcript_path="/x")
        self.assertTrue(qt.save_snapshot(self.file, snap))
        saved = json.loads(self.file.read_text())
        self.assertEqual(sorted(saved), sorted(qt.SNAPSHOT_KEYS))
        self.assertEqual(qt.load_snapshot(self.file, "claude")["remaining_percent"], 64.0)
        self.assertIsNone(qt.load_snapshot(self.file, "codex"))
        self.assertEqual(list(self.file.parent.glob(".*.tmp")), [])
        outside = self.tmp / "elsewhere" / "q.json"
        self.assertFalse(qt.save_snapshot(outside, snap))
        self.assertFalse(outside.exists())
        for junk in ("{", "[]", json.dumps(dict(saved, used_percent=101)), json.dumps(dict(saved, kind="x")),
                     json.dumps(dict(saved, resets_at={})), json.dumps(dict(saved, resets_at="soon")),
                     json.dumps(dict(saved, resets_at=True)), json.dumps(dict(saved, measured_at=-1)),
                     json.dumps(saved).replace(str(RESET), "1" + "0" * 400)):
            self.file.write_text(junk)
            self.assertIsNone(qt.load_snapshot(self.file, "claude"))

    def run_statusline(self, data, *extra):
        out = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(data))), redirect_stdout(out):
            sl.main(["--width", "120", "--no-transcript", *extra])
        return out.getvalue()

    def test_statusline_output_unchanged_and_last_reading_never_wiped(self):
        data = {"model": {"display_name": "Opus"},
                "rate_limits": {"seven_day": {"used_percentage": 36, "resets_at": RESET}}}
        plain = self.run_statusline(data)
        self.assertEqual(self.run_statusline(data, "--quota-file", str(self.file)), plain)
        self.assertEqual(plain, "Opus │ 7d 64% left\n")
        first = json.loads(self.file.read_text())
        self.assertEqual(first["used_percent"], 36.0)
        # Claude Code drops the window after a reset, or never sends it (API, print mode).
        missing = {"model": {"display_name": "Opus"}}
        self.assertEqual(self.run_statusline(missing, "--quota-file", str(self.file)), "Opus\n")
        self.assertEqual(json.loads(self.file.read_text()), first)
        # Malformed numbers (huge, non-finite, bad reset) print what is known, crash nothing,
        # and leave the last good reading in place.
        for bad, printed in (({"used_percentage": 10**400}, "Opus\n"),
                             ({"used_percentage": 36, "resets_at": {}}, "Opus │ 7d 64% left\n"),
                             ({"used_percentage": 36, "resets_at": 10**400}, "Opus │ 7d 64% left\n")):
            weird = {"model": {"display_name": "Opus"}, "rate_limits": {"seven_day": bad},
                     "context_window": {"used_percentage": 10**400}}
            self.assertEqual(self.run_statusline(weird, "--quota-file", str(self.file)), printed)
            self.assertEqual(json.loads(self.file.read_text()), first)
        # A destination outside .workflow, or an unwritable one, still prints the line.
        self.assertEqual(self.run_statusline(data, "--quota-file", str(self.tmp / "x.json")), plain)
        self.assertFalse((self.tmp / "x.json").exists())
        with mock.patch.dict("os.environ", {sl.QUOTA_ENV: str(self.file)}):
            self.assertEqual(self.run_statusline(dict(data, rate_limits={"seven_day": {"used_percentage": 50}})),
                             "Opus │ 7d 50% left\n")
        self.assertEqual(json.loads(self.file.read_text())["used_percent"], 50.0)

    def test_repeated_identical_reading_keeps_its_first_observed_time_and_goes_stale(self):
        data = {"model": {"display_name": "Opus"},
                "rate_limits": {"seven_day": {"used_percentage": 3, "resets_at": RESET}}}

        def at(wall, payload):
            with mock.patch("time.time", return_value=wall):
                printed = self.run_statusline(payload, "--quota-file", str(self.file))
            return printed, self.file.read_bytes()

        printed, first = at(NOW, data)
        self.assertEqual((printed, json.loads(first)["measured_at"]), ("Opus │ 7d 97% left\n", NOW))
        # The same values later (timer refresh): no new measurement is claimed, so it ages into stale.
        self.assertEqual(at(NOW + 300, data), (printed, first))
        claude = qt.QuotaSource(self.file, None, wall=lambda: NOW + 300).payload()["providers"]["claude"]
        self.assertEqual((claude["state"], claude["measured_at"], claude["remaining_percent"]), ("stale", NOW, 97.0))
        # Missing fields keep the snapshot.
        self.assertEqual(at(NOW + 400, {"model": {"display_name": "Opus"}})[1], first)
        # A changed percentage, reset, or a reset becoming unknown is a new reading at the new time.
        for wall, week in ((NOW + 500, {"used_percentage": 4, "resets_at": RESET}),
                           (NOW + 600, {"used_percentage": 4, "resets_at": RESET + 7 * 86400}),
                           (NOW + 700, {"used_percentage": 4})):
            saved = json.loads(at(wall, dict(data, rate_limits={"seven_day": week}))[1])
            self.assertEqual((saved["used_percent"], saved["resets_at"], saved["measured_at"]),
                             (4.0, week.get("resets_at"), wall))
            claude = qt.QuotaSource(self.file, None, wall=lambda: wall + 10).payload()["providers"]["claude"]
            self.assertEqual(claude["state"], "fresh")
        # An invalid kept file is replaced, never trusted for the comparison.
        self.file.write_text("{")
        self.assertEqual(json.loads(at(NOW + 800, data)[1])["measured_at"], NOW + 800)

    def test_quota_source_payload(self):
        qt.save_snapshot(self.file, qt.snapshot("claude", qt.CLAUDE_SOURCE, 36.0, RESET, NOW))
        payload = qt.QuotaSource(self.file, None, wall=lambda: NOW + 10).payload()["providers"]
        self.assertEqual(payload["codex"], {"available": False, "state": "unavailable", "note": "Codex reading is turned off"})
        self.assertEqual((payload["claude"]["available"], payload["claude"]["remaining_percent"]), (True, 64.0))
        expired = qt.QuotaSource(self.file, None, wall=lambda: RESET + 1).payload()["providers"]["claude"]
        self.assertFalse(expired["available"])
        none = qt.QuotaSource(self.tmp / "absent.json", None, wall=lambda: NOW).payload()["providers"]["claude"]
        self.assertEqual(none, {"available": False, "state": "unavailable",
                                "note": "No Claude Code status-line reading captured"})


# --- Claude CLI /usage -----------------------------------------------------------
#
# Panes modelled on the sanitized structure probe (phase1/structure-probe.txt):
# a cached pane ending in "Refreshing…", a partial re-render, then the refreshed
# pane ending in "Usage credits". These are fixtures, not account readings.

NY = __import__("zoneinfo").ZoneInfo("America/New_York")
WALL0 = __import__("datetime").datetime(2026, 10, 13, 12, 0, tzinfo=NY).timestamp()
RESET_1259 = __import__("datetime").datetime(2026, 10, 16, 0, 59, tzinfo=NY).timestamp()
STARTUP = b"\x1b[?25l\x1b[2K\x1b[G> \r\n\x1b[2mplan mode on\x1b[22m\r\n"


def pane(week="16% 16% used\r\nResets Oct 16 at 1am (America/New_York)", footer="Refreshing…", extra=()):
    lines = ["\x1b[2K Settings  Status   Config   Usage", "Current session", "3% 3% used",
             "Resets 4pm (America/New_York)", "Current week (all models)", week,
             "Current week (Fable)", "51% 51% used", "Resets Oct 16 at 1am (America/New_York)", *extra,
             "What's contributing to your limits", "Mostly long sessions this week", footer, "Esc to cancel"]
    return ("\r\n".join(lines) + "\r\n").encode()


CACHED = pane()
PARTIAL = b"\x1b[3A\x1b[2KMostly long sessions this week\r\nRefreshing\xe2\x80\xa6\r\nEsc to cancel\r\n"
FINAL = pane("17% 17% used\r\nResets Oct 16 at 12:59am (America/New_York)", "Usage credits\r\nUsage credits off")
REFRESHED = [(0.05, CACHED), (0.15, PARTIAL), (0.2, FINAL)]


class FakeSession:
    """A scripted PTY on a fake clock: startup output, then `after` (delay, chunk)
    once something is written; an empty schedule times out at once."""

    def __init__(self, clock, startup=(STARTUP,), after=(), eof=False):
        self.clock = clock
        self.events = [(clock.t + 0.05 * (i + 1), chunk) for i, chunk in enumerate(startup)]
        self.after, self.eof = list(after), eof
        self.writes, self.written_at, self.closed, self.stopped = [], None, False, False

    def write(self, data):
        self.writes.append(data)
        self.written_at = self.clock.t
        self.events += [(self.clock.t + delay, chunk) for delay, chunk in self.after]

    def read(self, timeout):
        if self.stopped:
            return None
        if self.events:
            due, chunk = self.events[0]
            if due <= self.clock.t + timeout:
                self.clock.t = max(self.clock.t, due)
                self.events.pop(0)
                return chunk
        elif self.eof and self.writes:
            return None
        self.clock.t += timeout
        return b""

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True


class ClaudeUsageFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="claude-cli-"))
        self.addCleanup(__import__("shutil").rmtree, self.tmp, ignore_errors=True)
        self.dir = self.tmp / ".workflow" / "telemetry"
        self.clock = Clock()

    def wall(self):
        return WALL0 + self.clock.t - 100.0

    def reader(self, *scripts, state_dir=None, **kw):
        scripts = iter(scripts)
        self.made = []

        def spawn(command, cwd):
            self.made.append(FakeSession(self.clock, **next(scripts)))
            return self.made[-1]

        return qt.ClaudeCliQuota(state_dir or self.dir, spawn=spawn, monotonic=self.clock, wall=self.wall, **kw)


class ClaudeUsagePanes(ClaudeUsageFixture):
    def read(self, **script):
        reader = self.reader(script)
        return reader._read(), self.made[0]

    def test_cached_pane_then_refreshed_pane_is_the_reading(self):
        (snap, why), session = self.read(after=REFRESHED)
        self.assertIsNone(why)
        self.assertEqual((snap["used_percent"], snap["remaining_percent"], snap["resets_at"], snap["source"]),
                         (17.0, 83.0, RESET_1259, qt.CLAUDE_CLI_SOURCE))
        # Measured when the refreshed pane arrived, not when the cached one did.
        self.assertAlmostEqual(snap["measured_at"], WALL0 + session.written_at + 0.2 - 100.0, places=3)
        self.assertEqual(session.writes, [b"/usage\r"])  # the literal built-in command, nothing else
        self.assertTrue(session.closed)

    def test_cold_start_loading_then_pane(self):
        loading = b"\x1b[2KLoading usage\xe2\x80\xa6\r\n"
        (snap, why), _ = self.read(after=[(0.05, loading), (1.5, FINAL)])
        self.assertEqual((snap["used_percent"], why), (17.0, None))

    def test_refreshed_pane_split_inside_an_escape_still_reads(self):
        cut = FINAL.index(b"\x1b[2K") + 2
        (snap, why), _ = self.read(after=[(0.05, CACHED), (0.2, FINAL[:cut]), (0.21, FINAL[cut:])])
        self.assertEqual((snap["used_percent"], why), (17.0, None))

    def test_cached_or_still_refreshing_pane_is_never_a_reading(self):
        for after in ([(0.05, CACHED)], [(0.05, CACHED), (0.15, PARTIAL)],
                      [(0.05, pane(footer="Usage credits"))],          # no refresh ever observed
                      [(0.05, b"Refreshing\xe2\x80\xa6\r\n" + FINAL)]):  # marker and pane in one write
            (snap, why), session = self.read(after=after)
            self.assertEqual((snap, why), (None, qt.CLI_NOT_REFRESHED), after)
            self.assertTrue(session.closed)
        self.assertLessEqual(self.clock.t, 100 + 4 * 46)  # each bounded by the timeout

    def test_only_the_all_models_week_is_read(self):
        no_week = FINAL.replace(b"Current week (all models)\r\n17% 17% used\r\n"
                                b"Resets Oct 16 at 12:59am (America/New_York)\r\n", b"")
        (snap, why), _ = self.read(after=[(0.05, CACHED), (0.2, no_week)])
        self.assertEqual((snap, why), (None, qt.CLI_NO_WEEKLY))  # never Fable, session or contributors

    def test_ambiguous_weekly_blocks_are_unknown(self):
        twice = pane("17% 17% used", "Usage credits", extra=("Current week (all models)", "18% 18% used"))
        mismatched = pane("17% 18% used", "Usage credits")
        noisy = pane("17% 17% used\r\nResets Oct 16 at 1am (America/New_York)\r\nextra line", "Usage credits")
        for final in (twice, mismatched, noisy, pane("171% 171% used", "Usage credits")):
            (snap, why), _ = self.read(after=[(0.05, CACHED), (0.2, final)])
            self.assertEqual((snap, why), (None, qt.CLI_AMBIGUOUS), final)

    def test_error_and_rate_limit_panels(self):
        (snap, why), _ = self.read(after=[(0.05, CACHED), (0.2, b"Error: 429 Too Many Requests\r\n")])
        self.assertEqual((snap, why), (None, qt.CLI_RATE_LIMITED))
        (snap, why), _ = self.read(after=[(0.05, CACHED), (0.2, b"Failed to load usage data\r\n")])
        self.assertEqual((snap, why), (None, qt.CLI_ERROR))

    def test_cached_quota_shown_with_an_error_is_never_a_reading(self):
        # A complete pane (even with its footer) next to an error: the error wins, the quota is not read.
        for error, reason in ((b"Error: 429 Too Many Requests\r\n", qt.CLI_RATE_LIMITED),
                              (b"Could not refresh usage\r\n", qt.CLI_ERROR)):
            (snap, why), _ = self.read(after=[(0.05, CACHED), (0.2, error + FINAL)])
            self.assertEqual((snap, why), (None, reason))
            (snap, why), _ = self.read(after=[(0.05, CACHED), (0.2, FINAL + error)])
            self.assertEqual((snap, why), (None, reason))

    def test_a_partial_redraw_then_silence_is_still_pending(self):
        # After the marker, a weekly block ending at the next section but without the
        # completed footer ("Usage credits" then "Esc to cancel") proves nothing.
        partial = FINAL[:FINAL.index(b"What's contributing")]
        no_esc = FINAL[:FINAL.index(b"Esc to cancel")]
        for after in ([(0.05, CACHED), (0.2, partial)], [(0.05, CACHED), (0.2, no_esc)],
                      [(0.05, CACHED), (0.2, pane("17% 17% used", "Refreshing done"))]):
            (snap, why), session = self.read(after=after)
            self.assertEqual((snap, why), (None, qt.CLI_NOT_REFRESHED), after)
            self.assertTrue(session.closed)
        # The same partial redraw, then the completed footer later: a reading.
        (snap, why), _ = self.read(after=[(0.05, CACHED), (0.2, partial), (0.5, FINAL[len(partial):])])
        self.assertEqual((snap["used_percent"], why), (17.0, None))

    def test_the_weekly_block_must_precede_the_completed_footer(self):
        footer_first = b"Usage credits\r\nEsc to cancel\r\nCurrent week (all models)\r\n17% 17% used\r\nUsage credits\r\n"
        (snap, why), _ = self.read(after=[(0.05, CACHED), (0.2, footer_first)])
        self.assertEqual((snap, why), (None, qt.CLI_NO_WEEKLY))

    def test_trust_or_login_prompt_stops_without_any_input(self):
        for screen in (b"Do you trust the files in this folder?\r\n\xe2\x9d\xaf 1. Yes, proceed\r\n2. No, exit\r\n",
                       b"Select login method:\r\n", b"Invalid API key \xc2\xb7 Please run /login\r\n"):
            (snap, why), session = self.read(startup=(screen,), after=REFRESHED)
            self.assertEqual((snap, why, session.writes), (None, qt.CLI_NEEDS_SETUP, []), screen)
            self.assertTrue(session.closed)
        (snap, why), session = self.read(after=[(0.05, CACHED), (0.2, b"Please run /login\r\n")])
        self.assertEqual((why, session.writes), (qt.CLI_NEEDS_SETUP, [b"/usage\r"]))

    def test_silent_exit_timeout_and_oversize_are_bounded(self):
        (_, why), _ = self.read(startup=())
        self.assertEqual(why, qt.CLI_TIMEOUT)
        (_, why), _ = self.read(after=[(0.05, CACHED)], eof=True)
        self.assertEqual(why, qt.CLI_EXITED)
        (_, why), session = self.read(after=[(0.01 * i, b"x" * 65536) for i in range(1, 12)])
        self.assertEqual(why, qt.CLI_TOO_LARGE)
        self.assertTrue(session.closed)

    def test_missing_cli(self):
        def missing(command, cwd):
            raise FileNotFoundError(command[0])

        reader = qt.ClaudeCliQuota(self.dir, spawn=missing, wall=self.wall)
        self.assertEqual(reader._read(), (None, qt.CLI_NOT_FOUND))

    def test_reset_times_are_exact_or_unknown(self):
        now = WALL0
        self.assertEqual(qt.reset_epoch("Resets Oct 16 at 12:59am (America/New_York)", now), RESET_1259)
        self.assertEqual(qt.reset_epoch("Resets Oct 16, 2026 at 12:59am (America/New_York)", now), RESET_1259)
        self.assertEqual(qt.reset_epoch("Resets Oct 16 at 1pm (UTC)", now),
                         __import__("datetime").datetime(2026, 10, 16, 13, 0,
                                                         tzinfo=__import__("datetime").timezone.utc).timestamp())
        late_dec = __import__("datetime").datetime(2026, 12, 30, 12, 0, tzinfo=NY).timestamp()
        self.assertEqual(qt.reset_epoch("Resets Jan 2 at 9am (America/New_York)", late_dec),
                         __import__("datetime").datetime(2027, 1, 2, 9, 0, tzinfo=NY).timestamp())
        nov = __import__("datetime").datetime(2026, 10, 29, 12, 0, tzinfo=NY).timestamp()
        mar = __import__("datetime").datetime(2026, 3, 4, 12, 0, tzinfo=NY).timestamp()
        for line, at in (("Resets 4pm (America/New_York)", now),             # no date: not guessed
                         ("Resets Oct 16 at 1am", now),                        # no zone
                         ("Resets Oct 16 at 1am (Mars/Olympus)", now),         # unknown zone
                         ("Resets Oct 16 at 13am (America/New_York)", now),
                         ("Resets Feb 30 at 1am (America/New_York)", now),
                         ("Resets Dec 1 at 1am (America/New_York)", now),      # not within the coming week
                         ("Resets Nov 1 at 1:30am (America/New_York)", nov),   # repeated hour
                         ("Resets Mar 8 at 2:30am (America/New_York)", mar),   # skipped hour
                         ("Resets soon", now)):
            self.assertIsNone(qt.reset_epoch(line, at), line)
        (snap, _), _ = self.read(after=[(0.05, CACHED), (0.2, pane("17% 17% used\r\nResets whenever", "Usage credits"))])
        self.assertEqual((snap["used_percent"], snap["resets_at"]), (17.0, None))  # reading kept, reset unknown


class ClaudeUsageSharing(ClaudeUsageFixture):
    def test_one_read_per_interval_across_companions_and_a_sanitized_private_cache(self):
        first = self.reader({"after": REFRESHED}, {"after": REFRESHED})
        second = self.reader({"after": REFRESHED})
        state = first.state()
        self.assertEqual((state["snapshot"]["used_percent"], state["error"], first.reads), (17.0, None, 1))
        self.assertEqual(second.state(), state)  # another Companion: the shared reading, no CLI started
        self.assertEqual(second.reads, 0)
        cache = self.dir / qt.CLAUDE_CLI_CACHE
        for path in (cache, self.dir / qt.CLAUDE_CLI_LOCK):
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        doc = json.loads(cache.read_text())
        self.assertEqual(sorted(doc), ["backoff", "checked_at", "error", "kind", "retry_at", "snapshot"])
        self.assertEqual(sorted(doc["snapshot"]), sorted(qt.SNAPSHOT_KEYS))
        for raw in ("Esc to cancel", "Fable", "contributing", "session", "Usage credits"):
            self.assertNotIn(raw, cache.read_text())
        self.clock.t += 290
        first.state()
        self.assertEqual(first.reads, 1)  # within 5 minutes: not again
        self.clock.t += 20
        measured = first.state()["snapshot"]["measured_at"]
        # The same 17% again is a new verified reading at its own time, not deduplicated.
        self.assertEqual((first.reads, first.peek()["snapshot"]["used_percent"]), (2, 17.0))
        self.assertGreater(measured, state["snapshot"]["measured_at"])

    def test_installation_state_is_shared_owner_only_and_symlink_safe(self):
        root = self.tmp / "harmony-test"
        state = root / ".workflow" / "telemetry" / "claude-cli-abc"
        # Companions for two different repositories (different cwd), one shared state: one read.
        first = self.reader({"after": REFRESHED}, state_dir=state, root=root, cwd=self.tmp / "repo-a")
        second = self.reader({"after": REFRESHED}, state_dir=state, root=root, cwd=self.tmp / "repo-b")
        self.assertEqual(first.state()["snapshot"]["used_percent"], 17.0)
        self.assertEqual((second.state()["snapshot"]["used_percent"], first.reads, second.reads), (17.0, 1, 0))
        for path in (root, root / ".workflow", root / ".workflow" / "telemetry", state):
            self.assertEqual(stat.S_IMODE(os.lstat(path).st_mode), 0o700, path)
        # A root others can enter, or a root that is a symlink, is never used.
        os.chmod(root, 0o755)
        self.clock.t += 400
        loose = self.reader({"after": REFRESHED}, state_dir=state, root=root)
        self.assertEqual((loose.state()["error"], loose.reads), (qt.CLI_UNSAFE, 0))
        target = self.tmp / "planted"
        target.mkdir(mode=0o700)
        link = self.tmp / "harmony-link"
        os.symlink(target, link)
        linked = self.reader({"after": REFRESHED}, root=link,
                             state_dir=link / ".workflow" / "telemetry" / "claude-cli-abc")
        self.assertEqual((linked.state()["error"], linked.reads), (qt.CLI_UNSAFE, 0))
        self.assertEqual(list(target.iterdir()), [])
        # The default is per user and per installed module, never under a repository.
        default_root, default_state = qt.install_state()
        self.assertEqual(default_root, Path(tempfile.gettempdir()).absolute() / f"harmony-{os.geteuid()}")
        self.assertEqual(default_state.parent, default_root / ".workflow" / "telemetry")
        self.assertEqual(qt.install_state(), (default_root, default_state))

    def test_a_held_lock_means_another_companion_is_reading(self):
        holder = qt.ClaudeCliQuota(self.dir)
        fd, why = holder._lock()
        self.assertIsNone(why)
        reader = self.reader({"after": REFRESHED})
        self.assertEqual(reader.state(), {"snapshot": None, "error": None, "attempted": False})  # pending
        self.assertEqual(reader.reads, 0)
        os.close(fd)
        self.assertEqual(reader.state()["snapshot"]["used_percent"], 17.0)

    def test_unsafe_lock_cache_or_directory_never_reads(self):
        self.dir.mkdir(parents=True)
        os.symlink(self.tmp / "elsewhere", self.dir / qt.CLAUDE_CLI_LOCK)
        reader = self.reader({"after": REFRESHED})
        self.assertEqual(reader.state(), {"snapshot": None, "error": qt.CLI_UNSAFE, "attempted": True})
        self.assertEqual((reader.reads, self.made), (0, []))
        self.assertFalse((self.tmp / "elsewhere").exists())
        outside = self.reader({"after": REFRESHED}, state_dir=self.tmp / "not-workflow")
        self.assertEqual((outside.state()["error"], outside.reads), (qt.CLI_UNSAFE, 0))
        # A cache reached through a symlink is never trusted.
        os.unlink(self.dir / qt.CLAUDE_CLI_LOCK)
        good = self.reader({"after": REFRESHED})
        good.state()
        real = self.dir / qt.CLAUDE_CLI_CACHE
        moved = self.tmp / "moved.json"
        os.replace(real, moved)
        os.symlink(moved, real)
        self.assertIsNone(qt.load_cli_cache(real))

    def test_failures_keep_the_last_real_reading_and_rate_limits_back_off(self):
        limited = {"after": [(0.05, CACHED), (0.2, b"Error: 429 Too Many Requests\r\n")]}
        reader = self.reader({"after": REFRESHED}, {"after": [(0.05, CACHED)]}, limited, limited, {"after": REFRESHED})
        first = reader.state()["snapshot"]
        self.clock.t += 301
        state = reader.state()
        self.assertEqual((state["snapshot"], state["error"]), (first, qt.CLI_NOT_REFRESHED))
        shown = qt.QuotaSource(None, None, wall=self.wall, claude_cli=reader).payload()["providers"]["claude"]
        self.assertEqual((shown["state"], shown["measured_at"], shown["refresh_error"], shown["remaining_percent"]),
                         ("stale", first["measured_at"], qt.CLI_NOT_REFRESHED, 83.0))
        self.clock.t += 301
        self.assertEqual(reader.state()["error"], qt.CLI_RATE_LIMITED)
        doc = qt.load_cli_cache(self.dir / qt.CLAUDE_CLI_CACHE)
        self.assertEqual((doc["backoff"], doc["snapshot"]), (600.0, first))
        self.clock.t += 400
        reader.state()
        self.assertEqual(reader.reads, 3)  # still backing off
        self.clock.t += 250
        reader.state()
        self.assertEqual(qt.load_cli_cache(self.dir / qt.CLAUDE_CLI_CACHE)["backoff"], 1200.0)
        self.clock.t += 1201
        self.assertEqual((reader.state()["error"], reader.reads), (None, 5))
        self.assertEqual(qt.load_cli_cache(self.dir / qt.CLAUDE_CLI_CACHE)["backoff"], 0.0)
        self.assertTrue(all(session.closed for session in self.made))

    def test_corrupt_cache_or_clock_jump_reads_again(self):
        reader = self.reader({"after": REFRESHED}, {"after": REFRESHED})
        self.dir.mkdir(parents=True)
        (self.dir / qt.CLAUDE_CLI_CACHE).write_text('{"kind": "harmony-claude-cli-quota/v1", "error": "secret"}')
        reader.state()
        self.assertEqual(reader.reads, 1)
        cache = self.dir / qt.CLAUDE_CLI_CACHE
        doc = json.loads(cache.read_text())
        cache.write_text(json.dumps(dict(doc, retry_at=doc["retry_at"] + 10 * 86400)))
        reader.state()
        self.assertEqual(reader.reads, 2)

    def test_close_stops_a_read_in_progress_and_leaves_the_shared_cache(self):
        made = []

        class Hanging:
            def __init__(self):
                self.stopped = self.closed = False

            def read(self, timeout):
                time.sleep(min(timeout, 0.02))
                return None if self.stopped else b""

            def write(self, data):
                raise AssertionError("nothing is typed before the prompt settles")

            def stop(self):
                self.stopped = True

            def close(self):
                self.closed = True

        def spawn(command, cwd):
            made.append(Hanging())
            return made[-1]

        cli = qt.ClaudeCliQuota(self.dir, spawn=spawn, timeout=30)
        worker = qt.QuotaWorker(cli, interval=60)
        worker.start()
        end = time.monotonic() + 5
        while cli.session is None:
            self.assertLess(time.monotonic(), end)
            time.sleep(0.01)
        started = time.monotonic()
        worker.close()
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertFalse(worker.thread.is_alive())
        self.assertEqual((cli.error, made[0].closed), (qt.CLI_STOPPED, True))
        self.assertFalse((self.dir / qt.CLAUDE_CLI_CACHE).exists())


class FakeCli:
    def __init__(self, snapshot=None, error=None, attempted=True):
        self.state = {"snapshot": snapshot, "error": error, "attempted": attempted}

    def peek(self):
        return self.state


class ClaudeSourceChoice(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="claude-choice-"))
        self.addCleanup(__import__("shutil").rmtree, self.tmp, ignore_errors=True)
        self.line = self.tmp / ".workflow" / "telemetry" / "claude-quota.json"

    def shown(self, cli, now):
        return qt.QuotaSource(self.line, None, wall=lambda: now, claude_cli=cli).payload()["providers"]["claude"]

    def test_fresh_poll_wins_and_status_line_is_the_fallback(self):
        polled = qt.snapshot("claude", qt.CLAUDE_CLI_SOURCE, 17.0, RESET, NOW)
        qt.save_snapshot(self.line, qt.snapshot("claude", qt.CLAUDE_SOURCE, 16.0, RESET, NOW + 60))
        # A later-observed but older status-line value never replaces a fresh /usage reading.
        fresh = self.shown(FakeCli(polled), NOW + 300)
        self.assertEqual((fresh["state"], fresh["used_percent"], fresh["source"], fresh["stale_after_seconds"]),
                         ("fresh", 17.0, qt.CLAUDE_CLI_SOURCE, qt.CLAUDE_STALE_AFTER))
        self.assertEqual(self.shown(FakeCli(polled), NOW + 601)["state"], "stale")
        # A failed poll: the newer status-line reading is shown with its own threshold.
        failed = self.shown(FakeCli(polled, qt.CLI_TIMEOUT), NOW + 100)
        self.assertEqual((failed["used_percent"], failed["source"], failed["stale_after_seconds"]),
                         (16.0, qt.CLAUDE_SOURCE, qt.STALE_AFTER))
        # First poll pending: the status line meanwhile; nothing at all: pending, then the reason.
        self.assertEqual(self.shown(FakeCli(attempted=False), NOW + 70)["used_percent"], 16.0)
        self.line.unlink()
        self.assertEqual(self.shown(FakeCli(attempted=False), NOW)["state"], "pending")
        self.assertEqual(self.shown(FakeCli(error=qt.CLI_NEEDS_SETUP), NOW),
                         {"available": False, "state": "unavailable", "note": qt.CLI_NEEDS_SETUP})
        stale = self.shown(FakeCli(polled, qt.CLI_RATE_LIMITED), NOW + 100)
        self.assertEqual((stale["state"], stale["used_percent"], stale["measured_at"]), ("stale", 17.0, NOW))


class RealPty(unittest.TestCase):
    """A Python stand-in for the CLI in a real pseudo-terminal (never claude)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="claude-pty-"))
        self.addCleanup(__import__("shutil").rmtree, self.tmp, ignore_errors=True)
        self.dir = self.tmp / ".workflow" / "telemetry"

    def reader(self, script, **kw):
        made = []

        def spawn(command, cwd):
            made.append(qt.PtySession([sys.executable, "-c", script], cwd))
            return made[-1]

        options = dict(spawn=spawn, settle=0.4, startup_quiet=0.3, startup_min=0.3, timeout=10)
        options.update(kw)
        return qt.ClaudeCliQuota(self.dir, **options), made

    def test_reads_the_refreshed_pane_and_stops_the_process(self):
        script = ("import sys, time\nout = sys.stdout.buffer\nout.write(%r); out.flush()\n"
                  "sys.stdin.readline()\nout.write(%r); out.flush(); time.sleep(0.2)\n"
                  "out.write(%r); out.flush(); time.sleep(60)\n") % (STARTUP, CACHED, FINAL)
        cli, made = self.reader(script)
        started = time.monotonic()
        state = cli.state()
        self.assertEqual((state["snapshot"]["used_percent"], state["error"]), (17.0, None))
        self.assertLess(time.monotonic() - started, 8)
        self.assertIsNotNone(made[0].proc.poll())  # terminated, not left running
        self.assertTrue(made[0].closed)

    def test_hung_cli_times_out_and_is_reaped(self):
        cli, made = self.reader("import time\ntime.sleep(60)\n", timeout=1)
        self.assertEqual(cli.state()["error"], qt.CLI_TIMEOUT)
        self.assertIsNotNone(made[0].proc.poll())

    @staticmethod
    def kill_quietly(pid):
        try:
            os.kill(pid, __import__("signal").SIGKILL)
        except OSError:
            pass

    def wait_gone(self, pid, limit=8.0):
        end = time.monotonic() + limit
        while time.monotonic() < end:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            time.sleep(0.05)
        return False

    def test_a_descendant_ignoring_sigterm_is_killed_even_after_the_cli_exited(self):
        # A synthetic CLI (never claude) starts a grandchild in its process group that ignores
        # SIGTERM, records its pid, and then exits itself (or keeps running).
        pid_file = self.tmp / "descendant.pid"
        child = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(120)"
        for parent_exits in (True, False):
            if pid_file.exists():
                pid_file.unlink()
            self.dir = self.tmp / ".workflow" / f"telemetry-{parent_exits}"  # each case due at once
            script = ("import subprocess, sys, time\n"
                      "p = subprocess.Popen([sys.executable, '-c', %r])\n"
                      "open(%r, 'w').write(str(p.pid))\n"
                      "time.sleep(0.3)\n"  # let the grandchild install its handler
                      "sys.stdout.write('> '); sys.stdout.flush()\n"
                      "%s\n") % (child, str(pid_file), "sys.exit(0)" if parent_exits else "time.sleep(120)")
            cli, made = self.reader(script, timeout=3)
            started = time.monotonic()
            cli.state()
            pid = int(pid_file.read_text())
            self.addCleanup(self.kill_quietly, pid)  # never leave it behind, even if the assertion fails
            self.assertTrue(self.wait_gone(pid), "descendant survived (parent exited: %s)" % parent_exits)
            self.assertIsNotNone(made[0].proc.poll())
            self.assertLess(time.monotonic() - started, 15)


if __name__ == "__main__":
    unittest.main()
