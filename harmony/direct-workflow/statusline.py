#!/usr/bin/env python3
"""One-line Claude Code status line, sized to the terminal, unknowns omitted.

Claude Code runs the configured `statusLine` command with the session's JSON
on stdin and shows what it prints. This renderer prints exactly one line, in
priority order:

  1. model                 model.display_name (else model.id)
  2. context occupancy     bar + percent: context_window.used_percentage, else
                           (input + cache creation + cache read of
                           context_window.current_usage) / context_window_size
                           when current_usage is present and complete
  3. weekly remaining      100 - rate_limits.seven_day.used_percentage, only
                           when Claude Code sends it (Pro/Max, after the first
                           response)
  4. session in/out        cumulative tokens of this session, derived from
                           transcript_path (one count per API message, main
                           thread only; input = uncached + cache creation +
                           cache read). The JSON's total_*_tokens fields are
                           the *current context*, not a session total, so
                           they are not used here.
  5. metered charge        only from an operator-supplied charge file for this
                           exact session (`--charge-file` /
                           STATUSLINE_CHARGE_FILE). cost.total_cost_usd is a
                           client-side list-price estimate and is never shown.

With `--quota-file PATH` (or $HARMONY_CLAUDE_QUOTA_FILE), a valid
rate_limits.seven_day reading is also kept for the Harmony companion as a
minimal snapshot (provider, source, used/remaining percent, reset, measured
time), written atomically and only inside a `.workflow` directory. A payload
without it leaves the last snapshot alone; the printed line is unchanged. The
feed has no measurement time, so the same used percent and reset as the kept
snapshot leave the file (and its first-observed time) untouched.

`--capture-only [--quota-file PATH] [-- COMMAND ARGS...]` prints nothing
itself: it keeps the seven_day snapshot and, when a command follows `--`,
runs it (no shell; pass `sh -c '...'` explicitly for a shell command) with
the exact stdin bytes, its own stdout/stderr, and returns its exit status.
This lets any existing status line keep working unchanged while Harmony
captures the reading. Settings are never read or written.

Unknown values are omitted, never shown as 0. No cache field and no list-price
estimate. The line never wraps: its width is `--width`, else $COLUMNS (Claude
Code sets it), else 80, minus `--reserve` cells; lower-priority fields are
dropped first, then the context bar collapses to its percent, and finally
the model name is truncated.

Standard library only. Setup: README.md, "Status line".
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile
import time
import unicodedata
from pathlib import Path
from typing import Any

BAR_CELLS = 10
SEP = " │ "
DEFAULT_WIDTH = 80
CHARGE_ENV = "STATUSLINE_CHARGE_FILE"
QUOTA_ENV = "HARMONY_CLAUDE_QUOTA_FILE"
MAX_PARSE = 4 << 20  # larger stdin is passed through but not parsed
COMMAND_TIMEOUT = 30.0
CACHE_VERSION = 1


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        value = float(value)
    except OverflowError:  # an int too large for a float is unknown, not a crash
        return None
    return value if math.isfinite(value) else None


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def cells(text: str) -> int:
    """Terminal cells: wide/fullwidth characters take two."""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def truncate(text: str, width: int) -> str:
    if cells(text) <= width:
        return text
    if width <= 0:
        return ""
    out = ""
    for ch in text:
        if cells(out + ch) > width - 1:
            break
        out += ch
    return out + "…"


def fmt_tokens(n: int) -> str:
    for unit, size in (("B", 10**9), ("M", 10**6), ("k", 10**3)):
        if n >= size:
            return f"{n / size:.1f}{unit}"
    return str(n)


# --- values ----------------------------------------------------------------


def model_name(data: dict[str, Any]) -> str | None:
    model = data.get("model")
    if isinstance(model, dict):
        for key in ("display_name", "id"):
            if isinstance(model.get(key), str) and model[key].strip():
                return model[key].strip()
    return None


def context_percent(data: dict[str, Any]) -> float | None:
    window = data.get("context_window")
    if not isinstance(window, dict):
        return None
    used = _num(window.get("used_percentage"))
    if used is not None:
        return max(0.0, min(100.0, used))
    usage, size = window.get("current_usage"), _int(window.get("context_window_size"))
    if not isinstance(usage, dict) or not size:
        return None
    parts = [_int(usage.get(k)) for k in
             ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")]
    if any(p is None for p in parts):
        return None
    try:
        return max(0.0, min(100.0, sum(parts) / size * 100))
    except OverflowError:
        return None


def weekly_remaining(data: dict[str, Any]) -> float | None:
    limits = data.get("rate_limits")
    week = limits.get("seven_day") if isinstance(limits, dict) else None
    used = _num(week.get("used_percentage")) if isinstance(week, dict) else None
    if used is None:
        return None
    return max(0.0, 100.0 - used)


def _usage_of(entry: dict[str, Any]) -> tuple[str, int, int] | None:
    """(message id, processed input, output) of one main-thread API message."""
    if entry.get("type") != "assistant" or entry.get("isSidechain"):
        return None
    message = entry.get("message")
    if not isinstance(message, dict) or message.get("model") == "<synthetic>":
        return None
    usage = message.get("usage")
    msg_id = message.get("id") or entry.get("requestId")
    if not isinstance(usage, dict) or not msg_id:
        return None
    fresh, out = _int(usage.get("input_tokens")), _int(usage.get("output_tokens"))
    if fresh is None or out is None:
        return None
    created = _int(usage.get("cache_creation_input_tokens") or 0)
    read = _int(usage.get("cache_read_input_tokens") or 0)
    if created is None or read is None:
        return None
    return str(msg_id), fresh + created + read, out


def _cache_path(transcript: Path, cache_dir: Path) -> Path:
    digest = hashlib.sha256(str(transcript).encode()).hexdigest()[:24]
    return cache_dir / f"{digest}.json"


def session_tokens(transcript_path: Any, cache_dir: Path | None = None) -> tuple[int, int] | None:
    """Cumulative (input, output) of the session's main thread, or None.

    A message is written once per content block with the same usage, so
    counts are kept per message id (last write wins) and never summed per
    line. Parsing is incremental: the byte offset and per-message counts are
    cached in a private temp directory keyed by transcript path.
    """
    if not isinstance(transcript_path, str) or not transcript_path:
        return None
    path = Path(transcript_path).expanduser()
    try:
        stat = path.stat()
    except OSError:
        return None
    cache_dir = cache_dir or Path(tempfile.gettempdir()) / f"direct-statusline-{os.getuid()}"
    cache_file = _cache_path(path, cache_dir)
    state: dict[str, Any] = {"version": CACHE_VERSION, "inode": stat.st_ino, "offset": 0, "messages": {}}
    try:
        cached = json.loads(cache_file.read_text(encoding="utf-8"))
        if (cached.get("version") == CACHE_VERSION and cached.get("inode") == stat.st_ino
                and cached.get("offset", 0) <= stat.st_size and isinstance(cached.get("messages"), dict)):
            state = cached
    except (OSError, ValueError, AttributeError):
        pass
    if state["offset"] < stat.st_size:
        try:
            with path.open("rb") as handle:
                handle.seek(state["offset"])
                chunk = handle.read(stat.st_size - state["offset"])
        except OSError:
            return None
        end = chunk.rfind(b"\n") + 1  # only whole lines; a partial last line waits
        for line in chunk[:end].splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            found = _usage_of(entry) if isinstance(entry, dict) else None
            if found:
                state["messages"][found[0]] = [found[1], found[2]]
        state["offset"] += end
        try:
            cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            tmp = cache_file.with_name(f".{cache_file.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(state), encoding="utf-8")
            os.replace(tmp, cache_file)
        except OSError:
            pass  # a cache we cannot write only costs a re-parse next time
    if not state["messages"]:
        return None
    values = list(state["messages"].values())
    return sum(v[0] for v in values), sum(v[1] for v in values)


def metered_charge(data: dict[str, Any], charge_file: str | None) -> float | None:
    """An explicitly supplied charge, only for this exact session.

    File: {"session_id": "...", "usd": 1.23, "source": "console export ..."}.
    A missing source or another session's file shows nothing.
    """
    if not charge_file:
        return None
    try:
        doc = json.loads(Path(charge_file).expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or not doc.get("source"):
        return None
    if not data.get("session_id") or doc.get("session_id") != data.get("session_id"):
        return None
    usd = _num(doc.get("usd"))
    return usd if usd is not None and usd >= 0 else None


def keep_weekly_quota(data: dict[str, Any], quota_file: str | None) -> bool:
    """Save the official seven_day reading for the companion; never raises."""
    if not quota_file:
        return False
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import quota_telemetry  # noqa: PLC0415

        snap = quota_telemetry.claude_weekly(data, time.time())
        return bool(snap) and quota_telemetry.keep_claude_snapshot(Path(quota_file), snap)
    except Exception:  # noqa: BLE001 - the status line must always print
        return False


# --- layout ----------------------------------------------------------------


def bar(percent: float) -> str:
    filled = min(BAR_CELLS, int(percent * BAR_CELLS / 100))
    return "█" * filled + "░" * (BAR_CELLS - filled)


def render(
    data: dict[str, Any],
    width: int,
    *,
    tokens: tuple[int, int] | None = None,
    charge: float | None = None,
) -> str:
    """One line no wider than `width` cells."""
    model = model_name(data)
    ctx = context_percent(data)
    week = weekly_remaining(data)
    # (priority, text, compact text) — lower priority number = kept longer
    fields: list[list[Any]] = []
    if model:
        fields.append([1, model, None])
    if ctx is not None:
        fields.append([2, f"ctx {bar(ctx)} {ctx:.0f}%", f"ctx {ctx:.0f}%"])
    if week is not None:
        fields.append([3, f"7d {week:.0f}% left", None])
    if tokens is not None:
        fields.append([4, f"in {fmt_tokens(tokens[0])} out {fmt_tokens(tokens[1])}", None])
    if charge is not None:
        fields.append([5, f"billed ${charge:.2f}", None])

    def joined() -> str:
        return SEP.join(f[1] for f in fields)

    width = max(0, width)
    while cells(joined()) > width and fields:
        compactable = [f for f in fields if f[2]]
        lowest = max(fields, key=lambda f: f[0])
        if lowest[0] > 2:
            fields.remove(lowest)
        elif compactable:  # only model and context remain: drop the bar first
            compactable[0][1], compactable[0][2] = compactable[0][2], None
        elif len(fields) > 1:
            fields.remove(lowest)
        else:
            fields[0][1] = truncate(fields[0][1], width)
            break
    return joined()


def terminal_width(explicit: int | None, reserve: int) -> int:
    if explicit is not None:
        return max(0, explicit - reserve)
    try:
        columns = int(os.environ.get("COLUMNS", ""))
    except ValueError:
        columns = 0
    return max(0, (columns if columns > 0 else DEFAULT_WIDTH) - reserve)


def capture_and_run(raw: bytes, quota_file: str | None, command: list[str], timeout: float) -> int:
    """Keep the seven_day reading from `raw`, then run `command` on the same
    bytes with inherited stdout/stderr. Never prints anything of its own on
    success; the payload is not logged."""
    data: Any = None
    if len(raw) <= MAX_PARSE:
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            data = None
    if isinstance(data, dict):
        keep_weekly_quota(data, quota_file)
    if not command:
        return 0
    try:
        done = subprocess.run(command, input=raw, timeout=timeout, check=False, shell=False)
    except FileNotFoundError:
        sys.stderr.write(f"statusline.py: command not found: {command[0]}\n")
        return 127
    except subprocess.TimeoutExpired:
        return 124
    return done.returncode if done.returncode >= 0 else 128 - done.returncode


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    command: list[str] = []
    if "--" in argv:
        cut = argv.index("--")
        argv, command = argv[:cut], argv[cut + 1:]
    parser = argparse.ArgumentParser(prog="statusline.py", description=__doc__.splitlines()[0])
    parser.add_argument("--width", type=int, help="override $COLUMNS")
    parser.add_argument("--reserve", type=int, default=2, help="cells kept free at the right edge")
    parser.add_argument("--charge-file", default=os.environ.get(CHARGE_ENV),
                        help=f"operator-supplied metered charge for this session (or ${CHARGE_ENV})")
    parser.add_argument("--no-transcript", action="store_true", help="do not derive session in/out")
    parser.add_argument("--quota-file", default=os.environ.get(QUOTA_ENV),
                        help=f"keep the seven_day reading for the companion (or ${QUOTA_ENV}); "
                             "must be inside a .workflow directory")
    parser.add_argument("--capture-only", action="store_true",
                        help="print nothing; keep the reading and run the command after -- (if any) unchanged")
    parser.add_argument("--command-timeout", type=float, default=COMMAND_TIMEOUT,
                        help="seconds the wrapped command may run (capture-only)")
    args = parser.parse_args(argv)
    if command and not args.capture_only:
        parser.error("a command after -- needs --capture-only")
    if args.capture_only:
        stream = getattr(sys.stdin, "buffer", None)
        raw = stream.read() if stream is not None else sys.stdin.read().encode("utf-8")
        return capture_and_run(raw, args.quota_file, command, args.command_timeout)
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    keep_weekly_quota(data, args.quota_file)  # first: a render problem never skips the capture
    tokens = None if args.no_transcript else session_tokens(data.get("transcript_path"))
    line = render(data, terminal_width(args.width, args.reserve), tokens=tokens,
                  charge=metered_charge(data, args.charge_file))
    sys.stdout.write(line + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
