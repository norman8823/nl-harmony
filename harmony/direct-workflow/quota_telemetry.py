#!/usr/bin/env python3
"""Weekly allowance remaining, from official provider sources only.

Codex   `codex app-server` (stdio JSON-RPC, https://learn.chatgpt.com/docs/app-server):
        initialize -> initialized -> `account/rateLimits/read`; while connected,
        `account/rateLimits/updated` notifications are applied too. Only the
        general `codex` bucket is used, and only a window whose
        `windowDurationMins` is exactly 10080 (one week) counts as weekly;
        primary/secondary position is never taken to mean weekly.
Claude  primary: the unmodified `claude` CLI's built-in `/usage` panel
        (https://code.claude.com/docs/en/commands), read in a bounded
        screen-reader PTY every 5 minutes by one process per installation.
        Only the literal `/usage` command is typed: no prompt, no model call.
        The "Current week (all models)" block is accepted only from a complete
        pane rendered after the panel's "Refreshing…"/"Loading…" marker, so
        the cached pane shown first is never taken as a new reading.
        Fallback: the Claude Code status line payload
        (https://code.claude.com/docs/en/statusline):
        `rate_limits.seven_day.used_percentage` and `resets_at`, captured by
        statusline.py when it is given a quota file and kept as a minimal
        snapshot. Claude Code only sends it for Pro/Max after the first
        response, and drops the window after it resets.

Remaining = 100 - used percent, only when used is a finite, non-boolean number
in 0..100; anything else is unknown, never clamped. No credential, config or
log file is read, no thread/turn/login call is made, and no account, auth or
credit field is kept; terminal output stays in bounded memory and is never
written anywhere. Standard library only.
"""

from __future__ import annotations

import bisect
import codecs
import datetime as _dt
import fcntl
import hashlib
import json
import math
import os
import queue
import re
import select
import signal
import stat
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

WEEK_MINUTES = 10080
WEEK_SECONDS = WEEK_MINUTES * 60
CODEX_SOURCE = "codex app-server account/rateLimits"
CLAUDE_SOURCE = "Claude Code status line rate_limits.seven_day"
SNAPSHOT_KIND = "harmony-quota/v1"
CLAUDE_QUOTA_ENV = "HARMONY_CLAUDE_QUOTA_FILE"
CODEX_QUOTA_ENV = "HARMONY_CODEX_QUOTA"  # "0" turns the Codex reader off
MAX_LINE = 1 << 20
MAX_MESSAGES = 200
QUEUE_LINES = 8  # lines buffered between the reader thread and the consumer
SNAPSHOT_KEYS = ("kind", "provider", "source", "window_minutes", "used_percent", "remaining_percent",
                 "resets_at", "measured_at")


def _finite(value: Any) -> float | None:
    """A finite float from a real (non-boolean) number; huge ints are unknown, not an error."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        value = float(value)
    except OverflowError:
        return None
    return value if math.isfinite(value) else None


def _percent(value: Any) -> float | None:
    value = _finite(value)
    return value if value is not None and 0.0 <= value <= 100.0 else None


def _epoch(value: Any) -> float | None:
    value = _finite(value)
    return value if value is not None and value > 0 else None


INVALID = object()


def _reset(container: dict[str, Any], key: str) -> Any:
    """A reset time: None when absent or null (supported: reset unknown), the
    epoch seconds when valid, INVALID when present but malformed."""
    if container.get(key) is None:
        return None
    value = _epoch(container[key])
    return INVALID if value is None else value


def snapshot(provider: str, source: str, used: float, resets_at: float | None, measured_at: float) -> dict[str, Any]:
    return {"kind": SNAPSHOT_KIND, "provider": provider, "source": source, "window_minutes": WEEK_MINUTES,
            "used_percent": used, "remaining_percent": round(100.0 - used, 4), "resets_at": resets_at,
            "measured_at": measured_at}


# --- Codex -------------------------------------------------------------------


def _same(window: dict[str, Any]) -> str:
    """A comparable key for a window's reading, whatever types it holds."""
    return json.dumps([window.get("usedPercent"), window.get("resetsAt")], default=repr)


def _weekly_window(bucket: Any) -> tuple[str, dict[str, Any] | None]:
    """('ok', window) | ('none', None) | ('ambiguous', None) for one bucket."""
    if not isinstance(bucket, dict):
        return "none", None
    weekly = []
    for key in ("primary", "secondary"):
        window = bucket.get(key)
        if not isinstance(window, dict):
            continue
        minutes = window.get("windowDurationMins")
        if isinstance(minutes, int) and not isinstance(minutes, bool) and minutes == WEEK_MINUTES:
            weekly.append(window)
    if not weekly:
        return "none", None
    if len({_same(window) for window in weekly}) > 1:
        return "ambiguous", None
    return "ok", weekly[0]


def codex_weekly(result: Any, measured_at: float) -> tuple[dict[str, Any] | None, str | None]:
    """(snapshot, None) or (None, reason) from an `account/rateLimits/read` result
    or an `account/rateLimits/updated` notification's params."""
    if not isinstance(result, dict):
        return None, "unreadable response"
    candidates = []
    by_id = result.get("rateLimitsByLimitId")
    if isinstance(by_id, dict) and "codex" in by_id:
        candidates.append(by_id["codex"])
    legacy = result.get("rateLimits")
    if isinstance(legacy, dict) and legacy.get("limitId") in ("codex", None):
        candidates.append(legacy)
    if not candidates:
        return None, "no general Codex limit reported"
    found = []
    for bucket in candidates:
        status, window = _weekly_window(bucket)
        if status == "ambiguous":
            return None, "conflicting weekly windows"
        if window is not None:
            found.append(window)
    if not found:
        return None, "no weekly (10080-minute) window reported"
    if len({_same(window) for window in found}) > 1:
        return None, "conflicting weekly windows"
    used = _percent(found[0].get("usedPercent"))
    if used is None:
        return None, "weekly usage not a valid percentage"
    resets = _reset(found[0], "resetsAt")
    if resets is INVALID:
        return None, "weekly reset time not valid"
    return snapshot("codex", CODEX_SOURCE, used, resets, measured_at), None


class Transport:
    """A started `codex app-server`: JSON lines in, JSON lines out, bounded."""

    def __init__(self, command: list[str]):
        self.proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, shell=False)
        self.lines: queue.Queue = queue.Queue(maxsize=QUEUE_LINES)
        self.cancel = threading.Event()
        self.produced = 0
        self.reader = threading.Thread(target=self._pump, daemon=True)
        self.reader.start()

    def _put(self, item: bytes | None) -> bool:
        """Hand one item over, waiting while the queue is full; False once cancelled."""
        while not self.cancel.is_set():
            try:
                self.lines.put(item, timeout=0.05)
                return True
            except queue.Full:
                continue
        return False

    def _pump(self) -> None:
        """Reads at most MAX_MESSAGES + 1 lines of at most MAX_LINE + 1 bytes
        (the consumer never uses more), so memory stays bounded; an oversized
        line is passed on once and reading stops."""
        try:
            while not self.cancel.is_set() and self.produced <= MAX_MESSAGES:
                line = self.proc.stdout.readline(MAX_LINE + 1)
                if not line:
                    break
                self.produced += 1
                if not self._put(line) or len(line) > MAX_LINE:
                    return
        except (OSError, ValueError):
            pass
        self._put(None)

    def send(self, message: dict[str, Any]) -> None:
        self.proc.stdin.write((json.dumps(message) + "\n").encode())
        self.proc.stdin.flush()

    def receive(self, timeout: float) -> bytes | None:
        """One line, None at end of stream; queue.Empty on timeout."""
        return self.lines.get(timeout=max(0.0, timeout))

    def close(self) -> None:
        # Stop the process first: closing stdout while the pump thread is
        # blocked reading it would wait for that read to finish.
        self.cancel.set()
        try:
            self.proc.stdin.close()
        except (OSError, ValueError):
            pass
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=2)
        self.reader.join(timeout=2)
        if not self.reader.is_alive():  # never close stdout under a blocked read
            try:
                self.proc.stdout.close()
            except (OSError, ValueError):
                pass


class CodexQuota:
    """Reads Codex's weekly window at most once per `ttl` seconds (monotonic).

    One reader at a time (a lock); a short-lived app-server per read, closed
    before returning. A failed read keeps the last valid snapshot and records
    why the refresh failed. `peek()` never waits for a read in progress;
    `abort()` stops one promptly (used when the server closes).
    """

    def __init__(self, command: list[str] | None = None, *, ttl: float = 60.0, timeout: float = 10.0,
                 spawn: Callable[[list[str]], Any] = Transport, monotonic: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time):
        self.command = command or ["codex", "app-server"]
        self.ttl, self.timeout = ttl, timeout
        self.spawn, self.monotonic, self.wall = spawn, monotonic, wall
        self.lock = threading.Lock()
        self.last: dict[str, Any] | None = None
        self.error: str | None = None
        self.checked: float | None = None
        self.attempted = False  # a read has finished (successfully or not)
        self.reads = 0
        self.aborted = threading.Event()
        self.transport: Any = None

    def state(self, max_age: float | None = None) -> dict[str, Any]:
        """Refreshes first when the last read is older than `max_age` (default `ttl`)."""
        with self.lock:
            now = self.monotonic()
            limit = self.ttl if max_age is None else max_age
            if not self.aborted.is_set() and (self.checked is None or now - self.checked >= limit):
                self._refresh()
                self.checked = self.monotonic()
                self.attempted = True
            return self.peek()

    def peek(self) -> dict[str, Any]:
        """The latest result without reading or waiting."""
        return {"snapshot": self.last, "error": self.error, "attempted": self.attempted}

    def abort(self) -> None:
        self.aborted.set()
        transport = self.transport
        if transport is not None:
            try:
                transport.close()
            except Exception:  # noqa: BLE001 - best effort; the reader's own cleanup follows
                pass

    def _refresh(self) -> None:
        self.reads += 1
        try:
            transport = self.spawn(self.command)
        except FileNotFoundError:
            self.error = "Codex CLI not found"
            return
        except OSError:
            self.error = "Codex app-server could not start"
            return
        self.transport = transport
        try:
            self.error = self._read(transport)
        except (OSError, ValueError):
            self.error = "Codex app-server connection failed"
        finally:
            self.transport = None
            try:
                transport.close()
            except Exception:  # noqa: BLE001 - cleanup never masks the reading
                pass

    def _read(self, transport: Any) -> str | None:
        deadline = self.monotonic() + self.timeout
        transport.send({"id": 0, "method": "initialize", "params": {"clientInfo": {
            "name": "harmony_companion", "title": "Harmony Companion", "version": "0.1"}}})
        sent_read = False
        count = 0
        while count < MAX_MESSAGES:
            if self.aborted.is_set():
                return "Codex reading stopped"
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                return "Codex app-server timed out"
            try:  # short waits, so abort() is noticed promptly
                line = transport.receive(min(remaining, 0.25))
            except queue.Empty:
                continue
            count += 1
            if line is None:
                return "Codex app-server closed"
            if len(line) > MAX_LINE:
                return "Codex app-server reply too large"
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if not isinstance(message, dict):
                continue
            if message.get("method") == "account/rateLimits/updated" and "id" not in message:
                params = message.get("params")
                found, _ = codex_weekly(params if isinstance(params, dict) else None, self.wall())
                if found:
                    self.last = found
                continue
            if message.get("id") == 0 and not sent_read:
                if "error" in message:
                    return "Codex app-server refused to initialize"
                transport.send({"method": "initialized", "params": {}})
                transport.send({"id": 1, "method": "account/rateLimits/read"})
                sent_read = True
                continue
            if message.get("id") == 1 and sent_read:
                if "error" in message:
                    return "Codex did not report rate limits (signed in?)"
                found, reason = codex_weekly(message.get("result"), self.wall())
                if found:
                    self.last = found
                    return None
                return reason
        return "Codex app-server sent too many messages"


class QuotaWorker:
    """The server's background reader for one collector (CodexQuota or
    ClaudeCliQuota: anything with `state(max_age)`, `abort()` and `timeout`):
    asks at start, then every `interval` seconds, whether or not a browser is
    asking. The collector decides whether a read is due. `close()` stops it
    (aborting a read in progress) and joins the thread."""

    def __init__(self, collector: Any, interval: float = 60.0):
        self.collector, self.interval = collector, interval
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None
        self.cycles = 0

    def start(self) -> None:
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, name="harmony-quota", daemon=True)
            self.thread.start()

    def _run(self) -> None:
        while not self.stop.is_set():
            self.collector.state(max_age=self.interval / 2)  # the collector's lock and throttle still apply
            self.cycles += 1
            self.stop.wait(self.interval)

    def close(self) -> None:
        self.stop.set()
        self.collector.abort()
        if self.thread is not None:
            self.thread.join(timeout=self.collector.timeout + 10)


# --- Claude ------------------------------------------------------------------


def claude_weekly(data: Any, measured_at: float) -> dict[str, Any] | None:
    limits = data.get("rate_limits") if isinstance(data, dict) else None
    week = limits.get("seven_day") if isinstance(limits, dict) else None
    if not isinstance(week, dict):
        return None
    used = _percent(week.get("used_percentage"))
    resets = _reset(week, "resets_at")
    if used is None or resets is INVALID:
        return None
    return snapshot("claude", CLAUDE_SOURCE, used, resets, measured_at)


def _in_workflow(path: Path) -> bool:
    return ".workflow" in path.parent.parts


def _write_private(path: Path, doc: dict[str, Any]) -> bool:
    """Atomic owner-only (0600) write; the temporary file is created
    exclusively and never through a symlink, and os.replace swaps the name."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(doc, sort_keys=True))
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        return False


def save_snapshot(path: Path, snap: dict[str, Any]) -> bool:
    """Atomically write only the allowlisted fields, inside a `.workflow` tree."""
    path = Path(path).expanduser().absolute()
    if not _in_workflow(path):
        return False
    return _write_private(path, {key: snap.get(key) for key in SNAPSHOT_KEYS})


def keep_claude_snapshot(path: Path, snap: dict[str, Any]) -> bool:
    """Save a Claude reading unless the kept one has the same used percent and
    reset; then the file and its `measured_at` stay untouched.

    The status line feed carries no measurement time of its own, and Claude
    Code repeats the last value on timer refreshes and in other sessions, so
    `measured_at` is when this exact reading was first observed, not proof
    that the provider measured it again. An unchanged value therefore ages
    into stale on purpose. Codex readings are not deduplicated here."""
    path = Path(path).expanduser().absolute()
    if not _in_workflow(path):
        return False
    kept = load_snapshot(path, "claude")
    if kept is not None and (kept["source"], kept["used_percent"], kept["resets_at"]) == (
            snap.get("source"), snap.get("used_percent"), snap.get("resets_at")):
        return True
    return save_snapshot(path, snap)


def load_snapshot(path: Path | None, provider: str) -> dict[str, Any] | None:
    """A saved snapshot, revalidated; anything malformed is treated as absent."""
    if path is None:
        return None
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return _revalidate(doc, provider)


def _revalidate(doc: Any, provider: str) -> dict[str, Any] | None:
    if not isinstance(doc, dict) or doc.get("kind") != SNAPSHOT_KIND or doc.get("provider") != provider:
        return None
    used, measured = _percent(doc.get("used_percent")), _epoch(doc.get("measured_at"))
    resets = _reset(doc, "resets_at")
    if used is None or measured is None or resets is INVALID:
        return None
    return snapshot(provider, str(doc.get("source") or ""), used, resets, measured)


# --- Claude: the CLI's /usage panel ------------------------------------------
#
# Observed screen-reader output (phase1/structure-probe.txt, cli-probe-result.json):
# after `/usage`, a complete pane from the CLI's cache arrives first, its
# "Current week (all models)" block followed by a "Refreshing…" footer; when the
# refresh finishes, a later write re-renders the blocks and ends with "Usage
# credits" instead. So a block counts only when it was read in a chunk after the
# one holding the last "Refreshing…"/"Loading…" marker, is complete (followed by
# the next section), and nothing has arrived for `settle` seconds. Anything else
# (no marker ever seen, the marker still last, a block re-rendered in place, two
# different weekly values) is no reading: unknown, never a guess.

CLAUDE_CLI_SOURCE = "Claude Code /usage Current week (all models)"
CLAUDE_CLI_ENV = "HARMONY_CLAUDE_CLI_QUOTA"  # "0" turns the /usage reader off
CLAUDE_CLI_ARGS = ["--ax-screen-reader", "--tools", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                   # process-local only: no hooks (so no status line) and no remote control; no file is changed
                   "--settings", '{"disableAllHooks":true,"remoteControlAtStartup":false}',
                   "--permission-mode", "plan", "--no-chrome"]
USAGE_INPUT = b"/usage\r"  # the only bytes ever written to the CLI
CLAUDE_CLI_CACHE = "claude-cli-quota.json"
CLAUDE_CLI_LOCK = "claude-cli-quota.lock"
CLAUDE_CLI_KIND = "harmony-claude-cli-quota/v1"
CLAUDE_POLL_SECONDS = 300.0  # one /usage read per installation per 5 minutes (clamped to 5..10)
CLAUDE_STALE_AFTER = 600.0   # two poll intervals
CLAUDE_TICK = 30.0           # how often a Companion checks whether a read is due or ownership is free
CLAUDE_MAX_BACKOFF = 3600.0
MAX_PTY_BYTES = 512 * 1024
STARTUP_KEEP = 16 * 1024

CLI_NOT_FOUND = "Claude CLI not found"
CLI_NOT_STARTED = "Claude CLI could not start"
CLI_EXITED = "Claude CLI exited before /usage finished"
CLI_NEEDS_SETUP = "Claude CLI needs setup (folder trust or sign-in); nothing was answered automatically"
CLI_TIMEOUT = "Claude /usage timed out"
CLI_NOT_REFRESHED = "Claude /usage did not show a completed refresh"
CLI_NO_WEEKLY = "Claude /usage showed no complete Current week (all models) reading"
CLI_AMBIGUOUS = "Claude /usage weekly reading was ambiguous"
CLI_RATE_LIMITED = "Claude /usage was rate limited; backing off"
CLI_ERROR = "Claude /usage reported an error"
CLI_TOO_LARGE = "Claude /usage output too large"
CLI_STOPPED = "Claude reading stopped"
CLI_UNSAFE = "Claude quota lock or cache path is unsafe"
CLI_CACHE_FAILED = "Claude quota cache could not be written"
CLI_REASONS = frozenset((CLI_NOT_FOUND, CLI_NOT_STARTED, CLI_EXITED, CLI_NEEDS_SETUP, CLI_TIMEOUT, CLI_NOT_REFRESHED,
                         CLI_NO_WEEKLY, CLI_AMBIGUOUS, CLI_RATE_LIMITED, CLI_ERROR, CLI_TOO_LARGE, CLI_STOPPED,
                         CLI_UNSAFE, CLI_CACHE_FAILED))
STOP_REASONS = frozenset((CLI_NEEDS_SETUP, CLI_RATE_LIMITED, CLI_ERROR))  # settled: no point waiting longer

_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ESCAPE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-Z\\-_]")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
PROGRESS = re.compile(r"Refreshing|Loading")
WEEKLY = re.compile(r"Current week \(all models\)")
SECTION_END = re.compile(r"Current week \(|Current session|What['’]s contributing|Usage credits|Esc to cancel"
                         r"|Refreshing|Loading")
COMPLETED = re.compile(r"Usage credits[\s\S]*Esc to cancel")  # the refreshed pane's footer (structure probe)
USED_LINE = re.compile(r"(?:(\d{1,3}(?:\.\d+)?)%\s+)?(\d{1,3}(?:\.\d+)?)%\s+used")
SETUP_PROMPT = re.compile(r"do you trust|trust (?:this|the) (?:folder|files|directory|project)|yes, proceed"
                          r"|/login|log ?in to|sign in to|select login|api key|text style|press enter to continue"
                          r"|enter to confirm|❯\s*1\.", re.IGNORECASE)
RATE_LIMITED = re.compile(r"\b429\b|rate[- ]limited|too many requests", re.IGNORECASE)
REPORTED_ERROR = re.compile(r"\berror\b|failed to|unable to|could not|couldn['’]t", re.IGNORECASE)
RESET_LINE = re.compile(r"Resets (?P<mon>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) (?P<day>\d{1,2})"
                        r"(?:, (?P<year>\d{4}))? at (?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?(?P<ampm>am|pm)"
                        r" \((?P<tz>UTC|[A-Za-z][A-Za-z0-9_+\-]*(?:/[A-Za-z0-9_+\-]+)+)\)")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _cursor(match: re.Match) -> str:
    final = match.group(0)[-1]
    return "\n" if final in "ABEFHfd" else " " if final == "C" else ""


def terminal_text(raw: str) -> str:
    """Plain text from decoded terminal output: no escapes or control bytes;
    cursor jumps become line breaks, cursor-forward a space."""
    text = _CSI.sub(_cursor, _OSC.sub("", raw))
    text = re.sub(r"\x1b[@-Z\\-_]?", "", text).replace("\r\n", "\n").replace("\r", "\n")
    return _CONTROL.sub("", text)


class TerminalText:
    """Incremental bytes -> plain text; an escape split across reads is held
    back (at most a few bytes) until it is complete."""

    def __init__(self) -> None:
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.held = ""

    def feed(self, chunk: bytes) -> str:
        text = self.held + self.decoder.decode(chunk)
        self.held = ""
        cut = text.rfind("\x1b")
        if cut >= 0 and len(text) - cut < 64 and not _ESCAPE.match(text, cut):
            text, self.held = text[:cut], text[cut:]
        return terminal_text(text)


def reset_epoch(line: str, now: float) -> float | None:
    """Epoch seconds for a "Resets Oct 16 at 12:59am (America/New_York)" line,
    to the minute; None (unknown) unless the date, time and zone are exact and
    exactly one year puts the reset within the coming week."""
    found = RESET_LINE.fullmatch(" ".join(line.split()))
    if not found:
        return None
    try:
        from zoneinfo import ZoneInfo  # noqa: PLC0415
        zone = ZoneInfo(found["tz"])
    except Exception:  # noqa: BLE001 - unknown or malformed zone: reset unknown
        return None
    hour, minute = int(found["hour"]), int(found["minute"] or 0)
    if not 1 <= hour <= 12 or minute > 59:
        return None
    hour = hour % 12 + (12 if found["ampm"] == "pm" else 0)
    month, day = MONTHS.index(found["mon"]) + 1, int(found["day"])
    this_year = _dt.datetime.fromtimestamp(now, zone).year
    years = [int(found["year"])] if found["year"] else [this_year - 1, this_year, this_year + 1]
    candidates = []
    for year in years:
        try:
            wall = _dt.datetime(year, month, day, hour, minute)
        except ValueError:
            continue
        early, late = wall.replace(tzinfo=zone, fold=0), wall.replace(tzinfo=zone, fold=1)
        if not now - 86400 <= early.timestamp() <= now + WEEK_SECONDS + 86400:
            continue
        if early.utcoffset() != late.utcoffset():
            return None  # a repeated or skipped hour at a clock change: no single time
        candidates.append(early.timestamp())
    return candidates[0] if len(candidates) == 1 else None


def weekly_reading(text: str, now: float) -> tuple[tuple[float, float | None, int] | None, str | None]:
    """((used, resets_at, offset), None) from complete "Current week (all
    models)" blocks that all agree, else (None, reason). Session, other weekly
    scopes, credits and contributors are never read."""
    found, offset = set(), None
    for anchor in WEEKLY.finditer(text):
        end = SECTION_END.search(text, anchor.end())
        if end is None:
            return None, CLI_NO_WEEKLY  # not complete yet
        lines = [line.strip() for line in text[anchor.end():end.start()].split("\n") if line.strip()]
        used_line = USED_LINE.fullmatch(lines[0]) if lines else None
        if used_line is None or len(lines) > 2 or (len(lines) == 2 and not lines[1].startswith("Resets ")):
            return None, CLI_AMBIGUOUS
        bar, used = used_line.groups()
        if bar is not None and float(bar) != float(used):
            return None, CLI_AMBIGUOUS
        if _percent(float(used)) is None:
            return None, CLI_AMBIGUOUS
        found.add((float(used), lines[1] if len(lines) == 2 else None))
        offset = anchor.start() if offset is None else offset
    if not found:
        return None, CLI_NO_WEEKLY
    if len(found) > 1:
        return None, CLI_AMBIGUOUS
    used, reset_line = found.pop()
    return (used, reset_epoch(reset_line, now) if reset_line else None, offset), None


def judge_usage(text: str, starts: list[int], walls: list[float]) -> tuple[dict[str, Any] | None, str]:
    """The refreshed reading from everything read after `/usage` was sent
    (`starts[i]` is where read i begins in `text`, `walls[i]` when it arrived),
    or why there is none yet."""
    marks = [m.start() for m in PROGRESS.finditer(text)]
    if not marks:
        return None, CLI_NOT_REFRESHED  # no refresh observed: the pane may be the cached one
    later = bisect.bisect_right(starts, marks[-1])  # the first read after the one holding the marker
    if later >= len(starts):
        return None, CLI_NOT_REFRESHED
    tail = text[starts[later]:]
    # Prompts and errors first: a pane that also shows (cached) quota is never a reading.
    if SETUP_PROMPT.search(tail):
        return None, CLI_NEEDS_SETUP
    if RATE_LIMITED.search(tail):
        return None, CLI_RATE_LIMITED
    if REPORTED_ERROR.search(tail):
        return None, CLI_ERROR
    # Completion is the refreshed pane's own footer ("Usage credits", then "Esc to
    # cancel"), not just a section boundary: a partial redraw is still pending.
    done = COMPLETED.search(tail)
    if done is None:
        return None, CLI_NOT_REFRESHED
    found, why = weekly_reading(tail[:done.start()], walls[later])
    if found is None:
        return None, why or CLI_NO_WEEKLY
    used, resets, offset = found
    measured = walls[bisect.bisect_right(starts, starts[later] + offset) - 1]
    return snapshot("claude", CLAUDE_CLI_SOURCE, used, resets, measured), ""


class PtySession:
    """The CLI in its own pseudo-terminal and process group; `stop()` may be
    called from another thread, `close()` only by the reader."""

    def __init__(self, command: list[str], cwd: Path | str | None = None):
        import pty  # noqa: PLC0415
        import struct  # noqa: PLC0415
        import termios  # noqa: PLC0415
        master, slave = pty.openpty()
        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 160, 0, 0))
            env = {key: value for key, value in os.environ.items() if key != "CLAUDECODE"}
            env.setdefault("TERM", "xterm-256color")
            self.proc = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave, cwd=cwd, env=env,
                                         start_new_session=True, close_fds=True, shell=False)
        except BaseException:
            os.close(master)
            raise
        finally:
            os.close(slave)
        self.master = master
        self.closed = False

    def read(self, timeout: float) -> bytes | None:
        """Some output, b"" when nothing arrived in time, None at end of output."""
        try:
            if not select.select([self.master], [], [], max(0.0, timeout))[0]:
                return b""
            return os.read(self.master, 65536) or None
        except (OSError, ValueError):
            return None

    def write(self, data: bytes) -> None:
        os.write(self.master, data)

    def _group_alive(self) -> bool:
        self.proc.poll()  # reap the CLI itself so it no longer counts
        try:
            os.killpg(self.proc.pid, 0)
        except ProcessLookupError:
            return False
        except OSError:
            return True
        return True

    def stop(self) -> None:
        """SIGTERM to the whole process group, then SIGKILL to whatever is left
        after a bounded wait, even when the CLI itself has already exited."""
        for sig in (signal.SIGTERM, signal.SIGKILL):
            if not self._group_alive():
                break
            try:
                os.killpg(self.proc.pid, sig)
            except ProcessLookupError:
                break
            except OSError:
                pass
            end = time.monotonic() + 2
            while time.monotonic() < end and self._group_alive():
                time.sleep(0.02)
        try:
            self.proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.stop()
            try:
                os.close(self.master)
            except OSError:
                pass


def install_state() -> tuple[Path, Path]:
    """(root, state_dir) shared by every Companion of this installation, across
    repositories: <tempdir>/harmony-<uid> (owner-only) /.workflow/telemetry/
    claude-cli-<hash of this module's installed path>. Paths only; nothing is
    created until a read is attempted."""
    root = Path(tempfile.gettempdir()).absolute() / f"harmony-{os.geteuid()}"
    install = hashlib.sha256(str(Path(__file__).resolve()).encode()).hexdigest()[:16]
    return root, root / ".workflow" / "telemetry" / f"claude-cli-{install}"


def _private_dir(path: Path) -> bool:
    """A real directory (not a symlink), owned by this user, with no group or other access."""
    try:
        path.mkdir(mode=0o700, exist_ok=True)
        info = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o077


def _safe_dir(directory: Path, root: Path | None = None) -> bool:
    """A real directory (not a symlink) owned by this user, inside `.workflow`.
    With `root` (the shared temp-dir state), the root and every directory below
    it down to `directory` must also be owner-only, so nothing in a shared
    temp dir can redirect or read the lock and cache."""
    if not _in_workflow(directory / "x"):
        return False
    if root is not None:
        try:
            parts = directory.relative_to(root).parts
        except ValueError:
            return False
        path = root
        if not _private_dir(path):
            return False
        for part in parts:
            path = path / part
            if not _private_dir(path):
                return False
        return True
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = os.lstat(directory)
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid()


def _read_private(path: Path, limit: int = 8192) -> Any:
    """JSON from a regular file owned by this user, never through a symlink."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_size > limit:
            return None
        return json.loads(os.read(fd, limit + 1).decode("utf-8"))
    except (OSError, ValueError):
        return None
    finally:
        os.close(fd)


def load_cli_cache(path: Path) -> dict[str, Any] | None:
    """The shared /usage state, revalidated field by field; else None."""
    doc = _read_private(path)
    if not isinstance(doc, dict) or doc.get("kind") != CLAUDE_CLI_KIND:
        return None
    checked, retry = _epoch(doc.get("checked_at")), _epoch(doc.get("retry_at"))
    backoff = _finite(doc.get("backoff"))
    if checked is None or retry is None or backoff is None or backoff < 0:
        return None
    snap = None
    if doc.get("snapshot") is not None:
        snap = _revalidate(doc["snapshot"], "claude")
        if snap is None or snap["source"] != CLAUDE_CLI_SOURCE:
            return None
    error = doc.get("error")
    if error is not None and error not in CLI_REASONS:
        return None
    return {"snapshot": snap, "error": error, "checked_at": checked, "retry_at": retry, "backoff": backoff}


def save_cli_cache(path: Path, snap: dict[str, Any] | None, error: str | None, checked_at: float,
                   retry_at: float, backoff: float, root: Path | None = None) -> bool:
    if not _safe_dir(path.parent, root):
        return False
    return _write_private(path, {
        "kind": CLAUDE_CLI_KIND, "snapshot": None if snap is None else {key: snap.get(key) for key in SNAPSHOT_KEYS},
        "error": error, "checked_at": checked_at, "retry_at": retry_at, "backoff": backoff})


class ClaudeCliQuota:
    """Claude's weekly window from the CLI's `/usage`, at most once per
    `interval` per installation.

    Companions sharing a state directory (by default `install_state()`: one per
    installation and user, whatever repository each Companion serves)
    coordinate through a lock file (flock, held only while deciding and
    reading, so a closed Companion never blocks the others) and a sanitized
    cache recording the last real reading, the last attempt and when the next
    one is due. A Companion that cannot take the lock, or finds the cache not
    yet due, only reads the cache. A failed or rate-limited read keeps the last
    real reading; 429s back off exponentially up to an hour. `root`, when
    given, must be owner-only along with every directory below it.
    """

    def __init__(self, state_dir: Path, cwd: Path | str | None = None, command: list[str] | None = None, *,
                 root: Path | None = None,
                 interval: float = CLAUDE_POLL_SECONDS, timeout: float = 45.0, settle: float = 1.0,
                 startup_quiet: float = 1.0, startup_min: float = 2.0,
                 spawn: Callable[..., Any] = PtySession, monotonic: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time):
        state_dir = Path(state_dir).expanduser().absolute()
        self.root = None if root is None else Path(root).expanduser().absolute()
        self.cache, self.lock_path = state_dir / CLAUDE_CLI_CACHE, state_dir / CLAUDE_CLI_LOCK
        self.cwd, self.command = cwd, command or ["claude", *CLAUDE_CLI_ARGS]
        self.interval = min(max(interval, CLAUDE_POLL_SECONDS), 2 * CLAUDE_POLL_SECONDS)
        self.timeout, self.settle = timeout, settle
        self.startup_quiet, self.startup_min = startup_quiet, startup_min
        self.spawn, self.monotonic, self.wall = spawn, monotonic, wall
        self.lock = threading.Lock()
        self.aborted = threading.Event()
        self.session: Any = None
        self.last: dict[str, Any] | None = None
        self.error: str | None = None
        self.attempted = False
        self.reads = 0

    def state(self, max_age: float | None = None) -> dict[str, Any]:  # noqa: ARG002 - the shared cache decides
        with self.lock:
            if not self.aborted.is_set():
                self._maybe_read()
            return self.peek()

    def peek(self) -> dict[str, Any]:
        """The shared cache (whichever Companion read it), else this process's own result."""
        doc = load_cli_cache(self.cache)
        if doc is not None and self.error != CLI_UNSAFE:
            return {"snapshot": doc["snapshot"], "error": doc["error"], "attempted": True}
        return {"snapshot": self.last, "error": self.error, "attempted": self.attempted}

    def abort(self) -> None:
        self.aborted.set()
        session = self.session
        if session is not None:
            try:
                session.stop()
            except Exception:  # noqa: BLE001 - best effort; the reader's own cleanup follows
                pass

    def _lock(self) -> tuple[int | None, str | None]:
        """(fd, None) when held, (None, None) when another Companion holds it, (None, reason) when unsafe."""
        if not _safe_dir(self.lock_path.parent, self.root):
            return None, CLI_UNSAFE
        try:
            fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
                         | getattr(os, "O_CLOEXEC", 0), 0o600)
        except OSError:
            return None, CLI_UNSAFE  # e.g. a symlink planted in its place
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            os.close(fd)
            return None, CLI_UNSAFE
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return None, None
        return fd, None

    def _maybe_read(self) -> None:
        fd, unsafe = self._lock()
        if fd is None:
            if unsafe:
                self.error, self.attempted = unsafe, True
            return
        try:
            doc = load_cli_cache(self.cache)
            now = self.wall()
            if doc is not None and now < doc["retry_at"] <= now + CLAUDE_MAX_BACKOFF + self.interval:
                return  # read recently, here or by another Companion (a far-future time is a clock jump)
            self._read_and_keep(doc)
        finally:
            os.close(fd)  # releases the lock

    def _read_and_keep(self, doc: dict[str, Any] | None) -> None:
        self.reads += 1
        snap, reason = self._read()
        if reason == CLI_STOPPED:  # this Companion is closing: leave the shared state for the others
            self.error, self.attempted = reason, True
            return
        now = self.wall()
        kept = snap or (doc and doc["snapshot"]) or self.last  # the last real reading is never dropped
        backoff = 0.0
        if reason == CLI_RATE_LIMITED:
            backoff = min(CLAUDE_MAX_BACKOFF, max(2 * self.interval, 2 * (doc["backoff"] if doc else 0.0)))
        self.last, self.error, self.attempted = kept, reason, True
        if not save_cli_cache(self.cache, kept, reason, now, now + (backoff or self.interval), backoff, self.root):
            self.error = CLI_CACHE_FAILED if reason is None else reason

    def _read(self) -> tuple[dict[str, Any] | None, str | None]:
        try:
            session = self.spawn(self.command, self.cwd)
        except FileNotFoundError:
            return None, CLI_NOT_FOUND
        except OSError:
            return None, CLI_NOT_STARTED
        self.session = session
        try:
            snap, reason = (None, CLI_STOPPED) if self.aborted.is_set() else self._converse(session)
        except OSError:
            snap, reason = None, CLI_EXITED
        finally:
            self.session = None
            try:
                session.close()
            except Exception:  # noqa: BLE001 - cleanup never masks the reading
                pass
        if snap is None and self.aborted.is_set():
            return None, CLI_STOPPED  # stop() ends the output, so a stop otherwise reads as an early exit
        return snap, reason

    def _converse(self, session: Any) -> tuple[dict[str, Any] | None, str | None]:
        start = self.monotonic()
        deadline = start + self.timeout
        stream, total, startup, last_out = TerminalText(), 0, "", None
        # 1. Wait for the CLI to settle at its prompt; stop at any dialog (trust, login, onboarding).
        while True:
            if self.aborted.is_set():
                return None, CLI_STOPPED
            if self.monotonic() >= deadline:
                return None, CLI_TIMEOUT
            chunk = session.read(min(0.1, max(0.0, deadline - self.monotonic())))
            now = self.monotonic()
            if chunk is None:
                return None, CLI_EXITED
            if chunk:
                total += len(chunk)
                if total > MAX_PTY_BYTES:
                    return None, CLI_TOO_LARGE
                startup = (startup + stream.feed(chunk))[-STARTUP_KEEP:]
                last_out = now
                if SETUP_PROMPT.search(startup):
                    return None, CLI_NEEDS_SETUP
            elif last_out is not None and now - last_out >= self.startup_quiet and now - start >= self.startup_min:
                break
        startup = ""
        # 2. The literal built-in command, nothing else.
        session.write(USAGE_INPUT)
        # 3. Read until a refreshed pane has settled.
        text, starts, walls, why = "", [], [], CLI_TIMEOUT
        last_out, judged = self.monotonic(), True
        while True:
            if self.aborted.is_set():
                return None, CLI_STOPPED
            if self.monotonic() >= deadline:
                return None, why
            chunk = session.read(min(0.1, max(0.0, deadline - self.monotonic())))
            if chunk is None:
                return None, CLI_EXITED if why == CLI_TIMEOUT else why
            if chunk:
                total += len(chunk)
                if total > MAX_PTY_BYTES:
                    return None, CLI_TOO_LARGE
                piece = stream.feed(chunk)
                if piece:
                    starts.append(len(text))
                    walls.append(self.wall())
                    text += piece
                last_out, judged = self.monotonic(), False
                continue
            if judged or self.monotonic() - last_out < self.settle:
                continue
            judged = True
            snap, why = judge_usage(text, starts, walls)
            if snap is not None:
                return snap, None
            if why in STOP_REASONS:
                return None, why


# --- what the companion shows ------------------------------------------------


STALE_AFTER = 120.0  # about two background refresh intervals


def present(snap: dict[str, Any] | None, now: float, *, error: str | None = None,
            missing: str = "No reading yet", pending: bool = False,
            stale_after: float | None = STALE_AFTER) -> dict[str, Any]:
    """A weekly reading and its `state`: pending (first check not finished),
    unavailable (no reading), fresh, stale (the latest refresh failed or the
    reading is older than `stale_after`; the last value and its time are kept)
    or expired (its reset time passed, or without one older than a week)."""
    if snap is None:
        if pending:
            return {"available": False, "state": "pending", "note": "Checking weekly allowance"}
        return {"available": False, "state": "unavailable", "note": error or missing}
    resets, measured = snap.get("resets_at"), snap["measured_at"]
    expired = resets <= now if resets is not None else now - measured >= WEEK_SECONDS
    if expired:
        return {"available": False, "state": "expired", "note": "Weekly window has reset; a new reading is needed",
                "last_measured_at": measured}
    stale = bool(error) or (stale_after is not None and now - measured > stale_after)
    return {"available": True, "state": "stale" if stale else "fresh", "stale": stale,
            "remaining_percent": snap["remaining_percent"], "used_percent": snap["used_percent"],
            "resets_at": resets, "measured_at": measured, "source": snap["source"],
            "window_minutes": WEEK_MINUTES, "refresh_error": error, "stale_after_seconds": stale_after}


class QuotaSource:
    """Both providers for the companion's `/api/quota`. With a worker, Codex is
    read in the background and a request only looks at the latest result;
    without one, a request reads (throttled by the collector). Claude: the
    `/usage` reading (`claude_cli`, shared cache) while it is fresh, else
    whichever of it and the status-line snapshot was measured later."""

    def __init__(self, claude_file: Path | None, codex: CodexQuota | None,
                 wall: Callable[[], float] = time.time, *, worker: QuotaWorker | None = None,
                 stale_after: float | None = STALE_AFTER, claude_cli: ClaudeCliQuota | None = None,
                 claude_worker: QuotaWorker | None = None):
        self.claude_file, self.codex, self.wall = claude_file, codex, wall
        self.worker, self.stale_after = worker, stale_after
        self.claude_cli, self.claude_worker = claude_cli, claude_worker

    def start(self) -> None:
        for worker in (self.worker, self.claude_worker):
            if worker is not None:
                worker.start()

    def close(self) -> None:
        for worker in (self.worker, self.claude_worker):
            if worker is not None:
                worker.close()

    def _claude(self, now: float) -> dict[str, Any]:
        line = load_snapshot(self.claude_file, "claude")
        if self.claude_cli is None:
            return present(line, now, missing="No Claude Code status-line reading captured",
                           stale_after=self.stale_after)
        state = self.claude_cli.peek()
        polled = state["snapshot"]
        shown = present(polled, now, error=state["error"], stale_after=CLAUDE_STALE_AFTER) if polled else None
        if shown is not None and shown["state"] == "fresh":
            return shown  # an older or replayed status-line value never replaces a fresh /usage reading
        if line is not None and (polled is None or line["measured_at"] > polled["measured_at"]):
            return present(line, now, stale_after=self.stale_after)
        if shown is not None:
            return shown
        return present(None, now, error=state["error"], pending=not state["attempted"],
                       missing="No Claude reading yet")

    def payload(self) -> dict[str, Any]:
        now = self.wall()
        if self.codex is None:
            codex = present(None, now, missing="Codex reading is turned off")
        else:
            state = self.codex.peek() if self.worker is not None else self.codex.state()
            codex = present(state["snapshot"], now, error=state["error"], pending=not state["attempted"],
                            stale_after=self.stale_after)
        return {"providers": {"codex": codex, "claude": self._claude(now)}}
