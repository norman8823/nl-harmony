#!/usr/bin/env python3
"""Read-only observability over direct-workflow run records.

One library, three commands, no daemon and no UI of its own. It reads the
per-run JSON records written by `direct_run_recorder.py` and, when a record
links one, the captured `stream-json` output of that run.

    # one run: identity, tokens, cost fields, tools, files, ordered activity
    python3 direct-workflow/run_observability.py inspect-run --run-id <run-id>
    python3 direct-workflow/run_observability.py inspect-run --record path/to/run.json

    # one task: writer/correction runs, review rounds, final outcome, totals
    python3 direct-workflow/run_observability.py summarize-task --task-id 39

    # two runs, or two tasks: absolute and percent difference where comparable
    python3 direct-workflow/run_observability.py compare --run <a> --run <b>
    python3 direct-workflow/run_observability.py compare --task 1 --task 2

Add `--json` for the machine-readable form. The Kanban board imports
`summarize_task` / `card_metrics` from this module, so the CLI and the board
share one calculation.

Rules this module keeps:

  * **Raw vs derived.** `raw` holds values copied from the record with the
    source the recorder gave them; `derived` holds what this module computed
    (sums, counts, the duration it chose) and how.
  * **Unknown stays unknown.** A missing value is `null` and listed under
    `unknowns`; a total is never summed from a partial set and never
    defaulted to zero. A stream without tool events gives unknown tool
    counts, not zero.
  * **No double counting.** Token totals add each run's per-invocation
    `tokens` (the result's `usage`). `session_cumulative` (`modelUsage`,
    `total_cost_usd`) grows across resumed invocations of one session and is
    never added to anything.
  * **Cost.** The only estimate used is the recorder's per-invocation
    `cost.estimated_list_cost_usd` (filled only when attributable). It is a
    client-side list-price estimate, not a charge. A provider charge appears
    only when explicitly supplied with a source.

Standard library only. Exit codes: 0 ok, 2 usage or unreadable input.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import direct_run_recorder as recorder  # noqa: E402

INSPECTION_KIND = "run-inspection/v1"
SUMMARY_KIND = "task-summary/v1"
COMPARISON_KIND = "comparison/v1"

TOKEN_FIELDS = tuple(recorder.USAGE_FIELDS)  # uncached_input, cache_creation_input, cached_input, output
INPUT_FIELDS = ("uncached_input", "cache_creation_input", "cached_input")
NO_DOUBLE_COUNT = (
    "sum of each run's per-invocation `tokens` (result.usage); "
    "session-cumulative modelUsage/total_cost_usd is never added"
)

# Tool inputs that name a file, and what the tool does to it.
FILE_TOOLS = {
    "Read": ("read", "file_path"),
    "Edit": ("edit", "file_path"),
    "MultiEdit": ("edit", "file_path"),
    "Write": ("write", "file_path"),
    "NotebookEdit": ("edit", "notebook_path"),
}
SEARCH_TOOLS = ("Grep", "Glob")
TARGET_CHARS = 120


class ObservabilityError(Exception):
    """Input that cannot be read as a run record or a task."""


# --- loading ----------------------------------------------------------------


def records_dir(value: str | os.PathLike | None = None) -> Path:
    """Same default as the recorder: flag, then env, then `.direct-runs`."""
    return Path(value or os.environ.get(recorder.RECORDS_DIR_ENV) or recorder.DEFAULT_RECORDS_DIR)


def load_record(path: str | os.PathLike) -> dict[str, Any]:
    path = Path(path)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ObservabilityError(f"cannot read run record {path}: {exc}") from exc
    if not isinstance(record, dict) or record.get("schema") != recorder.SCHEMA:
        raise ObservabilityError(f"{path} is not a {recorder.SCHEMA} run record")
    record.setdefault("_path", str(path))
    return record


def iter_record_paths(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(
        p for p in directory.glob("*.json")
        if not p.name.startswith(".") and not p.name.endswith(".stream.json")
    )


def load_task_records(directory: Path, task_id: str) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Completed records of one task, plus records skipped and why.

    A `reserved` (running or interrupted) or `launch_failed` record is not a
    completed run: it is reported as skipped, never counted. A reserved
    record carries no task id, so it is attributed only through the
    recorder's default `<task>-<role>-...` run id.
    """
    task_id = str(task_id)
    found, skipped = [], []
    for path in iter_record_paths(directory):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(raw, dict) or raw.get("schema") != recorder.SCHEMA:
            continue
        status = raw.get("status")
        if status is not None:
            owner = raw.get("task_id")
            ours = str(owner) == task_id if owner is not None else str(
                raw.get("run_id") or "").startswith(f"{task_id}-")
            if ours:
                skipped.append({"path": str(path), "why": f"record status {status!r}, not a completed run"})
            continue
        if str(raw.get("task_id")) == task_id:
            raw.setdefault("_path", str(path))
            found.append(raw)
    return found, skipped


def task_ids_with_records(directory: Path) -> list[str]:
    """Task ids named by completed or running records (reserved ones name none)."""
    ids = set()
    for path in iter_record_paths(directory):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(raw, dict) and raw.get("schema") == recorder.SCHEMA and raw.get("task_id") is not None \
                and raw.get("status") in (None, "running"):
            ids.add(str(raw["task_id"]))
    return sorted(ids)


def find_run(directory: Path, run_id: str) -> dict[str, Any]:
    path = directory / f"{run_id}.json"
    if path.exists():
        return load_record(path)
    for candidate in iter_record_paths(directory):
        try:
            record = load_record(candidate)
        except ObservabilityError:
            continue
        if record.get("run_id") == run_id:
            return record
    raise ObservabilityError(f"no run record {run_id!r} in {directory}")


def resolve_stream(record: dict[str, Any]) -> Path | None:
    """The record's stream file, if it can be found.

    The recorder stores the path as given at run time, which may be relative
    to the directory the run was launched from. Try it as given, then next to
    the record.
    """
    value = record.get("stream_path")
    if not value:
        return None
    candidate = Path(value).expanduser()
    if candidate.is_absolute():
        return candidate if candidate.is_file() else None
    options = [Path.cwd() / candidate]
    if record.get("_path"):
        options.append(Path(record["_path"]).parent / candidate.name)
    for option in options:
        if option.is_file():
            return option
    return None


# --- helpers ----------------------------------------------------------------


def _value(node: Any) -> Any:
    return node.get("value") if isinstance(node, dict) else None


def _raw(node: Any) -> dict[str, Any]:
    """A `{value, source}` field copied as-is (missing becomes unknown)."""
    if isinstance(node, dict):
        return {"value": node.get("value"), "source": node.get("source")}
    return {"value": None, "source": None}


def _num(value: Any) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _short(text: Any, limit: int = TARGET_CHARS) -> str | None:
    if not isinstance(text, str):
        return None
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --- stream analysis ----------------------------------------------------------


def _target(name: str, tool_input: dict[str, Any]) -> str | None:
    if name in FILE_TOOLS:
        return tool_input.get(FILE_TOOLS[name][1])
    if name in SEARCH_TOOLS:
        pattern = tool_input.get("pattern")
        where = tool_input.get("path")
        return _short(f"{pattern} in {where}" if where else pattern)
    if name == "Bash":
        return _short(tool_input.get("description") or tool_input.get("command"))
    for key in ("description", "url", "query", "prompt"):
        if isinstance(tool_input.get(key), str):
            return _short(tool_input[key])
    return None


def analyse_stream(text: str, expected_session: str | None) -> dict[str, Any]:
    """Tools, files, repeated reads and ordered activity from stream-json.

    Tool facts are reported only when the stream actually holds assistant
    events; a result-only `json` output gives unknown tool data, not zero.
    Events of a different session than the record's are not attributed.
    """
    events = list(recorder.iter_events(text))
    sessions = {e.get("session_id") for e in events if e.get("session_id")}
    if expected_session and sessions and sessions != {expected_session}:
        return {
            "available": False,
            "reason": f"stream session(s) {sorted(sessions)} do not match the record's session",
        }
    assistant = [e for e in events if e.get("type") == "assistant"]
    if not assistant:
        return {
            "available": False,
            "reason": "the linked output holds no assistant/tool events (result-only json output?)",
            "event_count": len(events),
        }

    tool_counts: dict[str, int] = {}
    subagent_tool_calls = 0
    files: dict[str, dict[str, int]] = {}
    reads: dict[str, list[dict[str, Any]]] = {}
    errors = 0
    activity: list[dict[str, Any]] = []
    names_by_id: dict[str, str] = {}

    def add(kind: str, event: dict[str, Any], **extra: Any) -> None:
        activity.append({
            "seq": len(activity) + 1,
            "timestamp": event.get("timestamp"),
            "kind": kind,
            "agent": "subagent" if event.get("parent_tool_use_id") else "main",
            **extra,
        })

    for event in events:
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            add("session_start", event, model=event.get("model"))
            continue
        if kind == "result":
            add("result", event, subtype=event.get("subtype"), is_error=event.get("is_error"))
            continue
        if kind not in ("assistant", "user"):
            continue
        content = (event.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        sub = bool(event.get("parent_tool_use_id"))
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if kind == "assistant" and btype == "text":
                add("text", event, chars=len(block.get("text") or ""))
            elif kind == "assistant" and btype == "tool_use":
                name = str(block.get("name") or "unknown")
                tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
                names_by_id[str(block.get("id"))] = name
                if sub:
                    subagent_tool_calls += 1
                else:
                    tool_counts[name] = tool_counts.get(name, 0) + 1
                target = _target(name, tool_input)
                add("tool_use", event, tool=name, target=target)
                if not sub and name in FILE_TOOLS and isinstance(target, str):
                    action = FILE_TOOLS[name][0]
                    entry = files.setdefault(target, {"read": 0, "edit": 0, "write": 0})
                    entry[action] += 1
                    if action == "read":
                        reads.setdefault(target, []).append(
                            {"offset": tool_input.get("offset"), "limit": tool_input.get("limit")}
                        )
            elif kind == "user" and btype == "tool_result":
                failed = block.get("is_error") is True
                if failed and not sub:
                    errors += 1
                add("tool_result", event,
                    tool=names_by_id.get(str(block.get("tool_use_id"))), is_error=failed)

    repeated = [
        {
            "path": path,
            "reads": len(ranges),
            "same_range_repeats": len(ranges) - len({json.dumps(r, sort_keys=True) for r in ranges}),
        }
        for path, ranges in sorted(reads.items())
        if len(ranges) > 1
    ]
    return {
        "available": True,
        "scope": "main-thread tool calls; subagent calls counted separately",
        "tool_calls_total": sum(tool_counts.values()),
        "tool_calls": dict(sorted(tool_counts.items())),
        "tool_errors": errors,
        "subagent_tool_calls": subagent_tool_calls,
        "files": dict(sorted(files.items())),
        "repeated_reads": repeated,
        "activity": activity,
        "limits": "file facts come from Read/Edit/MultiEdit/Write/NotebookEdit inputs only; "
                  "files read or written by shell commands are not attributed",
    }


# --- one run ------------------------------------------------------------------


def run_duration(record: dict[str, Any]) -> dict[str, Any]:
    """Recorder wall clock when present, else the CLI's reported duration."""
    durations = record.get("duration_ms") or {}
    for basis in ("wall", "reported"):
        value = _num(_value(durations.get(basis)))
        if value is not None:
            return {"value": int(value), "basis": basis,
                    "source": (durations.get(basis) or {}).get("source")}
    return {"value": None, "basis": None, "source": None}


def run_tokens(record: dict[str, Any]) -> dict[str, Any]:
    tokens = record.get("tokens") or {}
    values = {name: _num(tokens.get(name)) for name in TOKEN_FIELDS}
    complete = all(v is not None for v in values.values())
    return {
        **values,
        "source": tokens.get("source"),
        "scope": tokens.get("scope"),
        "cross_check": tokens.get("cross_check"),
        "complete": complete,
    }


def attributable_estimate(record: dict[str, Any]) -> dict[str, Any]:
    estimate = (record.get("cost") or {}).get("estimated_list_cost_usd") or {}
    return {
        "value": _num(estimate.get("value")),
        "method": estimate.get("method"),
        "source": estimate.get("source"),
        "basis": estimate.get("basis"),
        "unknown_reason": estimate.get("unknown_reason") if estimate.get("value") is None else None,
    }


def provider_charge(record: dict[str, Any]) -> dict[str, Any]:
    charge = (record.get("cost") or {}).get("actual_provider_charge_usd") or {}
    value = _num(charge.get("value"))
    # A charge without a stated source is not explicitly supplied.
    if value is None or not charge.get("source"):
        return {"value": None, "source": None}
    return {"value": value, "source": charge.get("source")}


def inspect_run(
    record: dict[str, Any],
    *,
    stream_text: str | None = None,
    stream_path: str | None = None,
    stream_error: str | None = None,
) -> dict[str, Any]:
    """Everything known about one run, raw and derived kept apart."""
    tokens = run_tokens(record)
    model = record.get("model") or {}
    outcome = record.get("outcome") or {}
    cli = outcome.get("cli") or {}
    cumulative = record.get("session_cumulative") or {}
    raw = {
        "task_id": record.get("task_id"),
        "run_id": record.get("run_id"),
        "role": record.get("role"),
        "session_id": _raw(record.get("session_id")),
        "resumed_session": _raw(record.get("resumed_session")),
        "model_requested": _raw(model.get("requested")),
        "model_reported": _raw(model.get("reported")),
        "started_at": record.get("started_at"),
        "ended_at": record.get("ended_at"),
        "duration_ms": {k: _raw((record.get("duration_ms") or {}).get(k))
                        for k in ("wall", "reported", "api_reported")},
        "num_turns": _raw(record.get("num_turns")),
        "tokens": {k: v for k, v in tokens.items() if k != "complete"},
        "outcome_cli": {"value": cli.get("value"), "source": cli.get("source"),
                        "exit_code": cli.get("exit_code"), "result_subtype": cli.get("result_subtype")},
        "outcome_review": _raw(outcome.get("review")),
        "review_rounds": _raw(record.get("review_rounds")),
        "corrections": _raw(record.get("corrections")),
        "estimated_list_cost_usd": attributable_estimate(record),
        "actual_provider_charge_usd": provider_charge(record),
        "session_cumulative": {
            "scope": cumulative.get("scope"),
            "totals": cumulative.get("totals"),
            "estimated_list_cost_usd": _value(cumulative.get("estimated_list_cost_usd")),
            "note": "session-cumulative; shown for reference, never added to per-run figures",
        },
        "record_unknowns": record.get("unknowns") or [],
        "record_path": record.get("_path"),
        "stream_path": record.get("stream_path"),
    }

    if stream_text is not None:
        stream = analyse_stream(stream_text, _value(record.get("session_id")))
        stream["path"] = stream_path
    else:
        stream = {"available": False,
                  "reason": stream_error or "the record links no readable stream"}

    processed_input = (
        sum(tokens[f] for f in INPUT_FIELDS) if all(tokens[f] is not None for f in INPUT_FIELDS) else None
    )
    derived = {
        "duration_ms": run_duration(record),
        "processed_input_tokens": {
            "value": processed_input,
            "basis": "uncached_input + cache_creation_input + cached_input of this invocation",
        },
        "stream": stream,
    }
    unknowns = sorted(set(raw["record_unknowns"]))
    if derived["duration_ms"]["value"] is None:
        unknowns.append("derived.duration_ms")
    if not stream["available"]:
        unknowns.append("derived.stream (tools, files, activity)")
    return {"kind": INSPECTION_KIND, "raw": raw, "derived": derived, "unknowns": unknowns}


def inspect_record(record: dict[str, Any], stream_override: str | None = None) -> dict[str, Any]:
    """`inspect_run` with the stream located and read from disk."""
    path = Path(stream_override) if stream_override else resolve_stream(record)
    if path is None:
        reason = ("the record links no stream" if not record.get("stream_path")
                  else f"linked stream not found: {record.get('stream_path')}")
        return inspect_run(record, stream_error=reason)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return inspect_run(record, stream_error=f"cannot read stream {path}: {exc}")
    return inspect_run(record, stream_text=text, stream_path=str(path))


# --- one task -------------------------------------------------------------------


def _parse_time(value: Any) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _order(records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    """Runs by recorded start time. If any start is unknown the order is too."""
    if len(records) == 1:
        return list(records), True
    times = [_parse_time(r.get("started_at")) for r in records]
    if records and all(t is not None for t in times):
        ordered = [r for _, r in sorted(zip(times, records), key=lambda pair: pair[0])]
        return ordered, True
    return sorted(records, key=lambda r: str(r.get("run_id"))), False


def _sum_all(values: list[Any]) -> int | float | None:
    if not values or any(v is None for v in values):
        return None
    return sum(values)


def summarize_task(
    records: list[dict[str, Any]],
    task_id: str,
    *,
    skipped: list[dict[str, str]] | None = None,
    actual_charge_usd: float | None = None,
    actual_charge_source: str | None = None,
) -> dict[str, Any]:
    """Task totals from per-run records. The one path the CLI and board share."""
    ordered, order_known = _order(records)
    unknowns: list[str] = []
    runs = []
    for record in ordered:
        tokens = run_tokens(record)
        runs.append({
            "run_id": record.get("run_id"),
            "role": record.get("role"),
            "role_label": role_label(record.get("role")),
            "provider": run_provider(record),
            "session_id": _value(record.get("session_id")),
            "model": _value((record.get("model") or {}).get("requested")),
            "model_reported": _reported_models(record),
            "started_at": record.get("started_at"),
            "duration_ms": run_duration(record),
            "tokens": {f: tokens[f] for f in TOKEN_FIELDS},
            "estimated_list_cost_usd": attributable_estimate(record)["value"],
            "actual_provider_charge_usd": provider_charge(record)["value"],
            "outcome_cli": ((record.get("outcome") or {}).get("cli") or {}).get("value"),
            "outcome_review": _value((record.get("outcome") or {}).get("review")),
            "review_rounds": _value(record.get("review_rounds")),
            "corrections": _value(record.get("corrections")),
        })

    roles: dict[str, int] = {}
    for run in runs:
        roles[str(run["role"])] = roles.get(str(run["role"]), 0) + 1

    sessions: dict[str, list[str]] = {}
    for run in runs:
        if run["session_id"]:
            sessions.setdefault(run["session_id"], []).append(run["run_id"])

    token_totals: dict[str, Any] = {}
    for field in TOKEN_FIELDS:
        token_totals[field] = _sum_all([run["tokens"][field] for run in runs])
        if token_totals[field] is None:
            unknowns.append(f"tokens.{field}")
    token_totals["processed_input"] = (
        sum(token_totals[f] for f in INPUT_FIELDS)
        if all(token_totals[f] is not None for f in INPUT_FIELDS) else None
    )
    token_totals["basis"] = NO_DOUBLE_COUNT

    durations = [run["duration_ms"]["value"] for run in runs]
    duration_total = _sum_all(durations)
    if duration_total is None:
        unknowns.append("duration_ms")
    estimate_total = _sum_all([run["estimated_list_cost_usd"] for run in runs])
    if estimate_total is None:
        unknowns.append("estimated_list_cost_usd")

    charge: dict[str, Any]
    if actual_charge_usd is not None:
        if not actual_charge_source:
            raise ObservabilityError("an actual provider charge needs an explicit source")
        charge = {"value": float(actual_charge_usd), "source": f"operator: {actual_charge_source}",
                  "basis": "explicitly supplied for the whole task"}
    else:
        per_run = _sum_all([run["actual_provider_charge_usd"] for run in runs])
        charge = {"value": per_run, "source": "sum of per-run operator-supplied charges" if per_run is not None else None,
                  "basis": "every run must carry an explicitly supplied charge"}
    if charge["value"] is None:
        unknowns.append("actual_provider_charge_usd")

    final = ordered[-1] if ordered and order_known else None
    if final is None:
        unknowns.append("final_outcome")
        final_outcome = {
            "run_id": None, "cli": None, "review": None,
            "unknown_reason": "no runs" if not runs else
            "run order is unknown (a record has no started_at), so no run can be called final",
        }
        review = {"rounds": None, "corrections_annotated": None,
                  "basis": "the final run's operator annotation; unknown without a final run"}
    else:
        final_run = runs[-1]
        final_outcome = {"run_id": final_run["run_id"], "cli": final_run["outcome_cli"],
                         "review": final_run["outcome_review"], "unknown_reason": None}
        review = {"rounds": final_run["review_rounds"], "corrections_annotated": final_run["corrections"],
                  "basis": "the final run's operator annotation (never summed across runs, "
                           "because annotations may be cumulative)"}
        if final_run["outcome_review"] is None:
            unknowns.append("final_outcome.review")
    if review["rounds"] is None:
        unknowns.append("review.rounds")

    return {
        "kind": SUMMARY_KIND,
        "task_id": str(task_id),
        "run_count": len(runs),
        "roles": dict(sorted(roles.items())),
        "correction_runs": roles.get("correction", 0),
        "writer_runs": roles.get("writer", 0),
        "order_known": order_known,
        "runs": runs,
        "shared_sessions": {sid: ids for sid, ids in sessions.items() if len(ids) > 1},
        "review": review,
        "final_outcome": final_outcome,
        "duration_ms": {"value": duration_total,
                        "bases": sorted({str(run["duration_ms"]["basis"]) for run in runs}),
                        "basis": "sum of per-run durations (recorder wall clock, else CLI duration_ms)"},
        "tokens": token_totals,
        "estimated_list_cost_usd": {"value": round(estimate_total, 10) if estimate_total is not None else None,
                                    "basis": "sum of per-run attributable CLI list-price estimates; "
                                             "not a charge; null unless every run has one"},
        "actual_provider_charge_usd": charge,
        "attribution": attribution(ordered),
        "skipped_records": skipped or [],
        "unknowns": sorted(set(unknowns)),
    }


def summarize_task_dir(directory: Path, task_id: str, **kwargs: Any) -> dict[str, Any]:
    records, skipped = load_task_records(directory, task_id)
    return summarize_task(records, task_id, skipped=skipped, **kwargs)


# --- attribution: task / provider / model / role / run ----------------------------


LEGACY_PROVIDER_BASIS = "record has no provider field; the recorder accepted only `claude` before it had one"


def run_provider(record: dict[str, Any]) -> str:
    return str(record.get("provider") or "claude")


def role_label(role: Any) -> str:
    return recorder.ROLE_LABELS.get(str(role), str(role))


def _reported_models(record: dict[str, Any]) -> list[str] | None:
    value = _value((record.get("model") or {}).get("reported"))
    return [str(m) for m in value] if isinstance(value, list) and value else None


_CLAUDE_ID = re.compile(r"^claude-(?:(\d+)-(\d+)-)?([a-z]+)(?:-(\d{1,2})(?:-(\d{1,2}))?)?(?:-(\d{8}))?(\[1m\])?$")
_GPT_ID = re.compile(r"^gpt-(\d+(?:\.\d+)?)((?:-[a-z0-9]+)*)$")


def model_name(model_id: Any) -> str | None:
    """A readable name derived only from an exact model id, or None.

    `claude-haiku-4-5-20251001` -> `Haiku 4.5`, `claude-opus-5-5[1m]` ->
    `Opus 5.5 (1M context)`, `gpt-5.5-codex` -> `GPT-5.5 Codex`. An id of
    any other shape gets no name (the id itself is still shown); a mixed
    `a + b` label is named part by part.
    """
    if not isinstance(model_id, str) or not model_id:
        return None
    if " + " in model_id:
        names = [model_name(part) for part in model_id.split(" + ")]
        return " + ".join(names) if all(names) else None
    match = _CLAUDE_ID.match(model_id)
    if match:
        old_major, old_minor, family, major, minor, _, wide = match.groups()
        version = (f"{old_major}.{old_minor}" if old_major else
                   f"{major}.{minor}" if minor else major)
        if version is None or (old_major and major):
            return None
        return f"{family.capitalize()} {version}" + (" (1M context)" if wide else "")
    match = _GPT_ID.match(model_id)
    if match:
        suffix = " ".join(word.capitalize() for word in match.group(2).split("-") if word)
        return f"GPT-{match.group(1)}" + (f" {suffix}" if suffix else "")
    return None


def input_total(values: dict[str, Any], provider: str | None = "claude") -> int | None:
    """This provider's own total input for the counters, or None.

    Claude reports uncached, cache-creation and cache-read input as disjoint
    parts, so its input is their sum. Codex's `input_tokens` already holds
    the cached (read) part, and cache writes are reported apart with no
    verified relation to it; so its input is uncached + cached, which the
    recorder derives only when no positive cache write was reported. A
    cache write Codex did not report is unknown, never zero, and is not
    part of this denominator.
    """
    names = ("uncached_input", "cached_input") if provider == "codex" else INPUT_FIELDS
    parts = [values.get(f) for f in names]
    return None if any(p is None for p in parts) else sum(parts)


def cached_share(values: dict[str, Any], provider: str | None = "claude") -> float | None:
    """Cache reads over the provider's own total input; None unless both are known."""
    denominator = input_total(values, provider)
    cached = values.get("cached_input")
    if cached is None or not denominator:
        return None
    return round(cached / denominator, 4)


CACHED_SHARE_BASIS = ("cache reads / the provider's total input (Claude: uncached + cache writes + cache "
                      "reads; Codex: input_tokens = uncached + cache reads); unknown when either is")


def attribute_usage(record: dict[str, Any]) -> list[dict[str, Any]]:
    """One run's per-invocation tokens as attribution rows that partition it.

    The rows of a run always add up to that run's tokens exactly once:

      * one reported model -> one row under that exact model id;
      * several reported models -> one row per model only when the result's
        per-model `modelUsage` sums exactly to this invocation's `usage` on
        every counter (then no earlier invocation contributed, so the split
        is this run's, subagent models included); otherwise one `mixed` row
        naming every model, never a copy per model;
      * nothing reported (Codex exec) -> the requested model, labelled so.

    No row is made per subagent: subagent usage is already inside the run's
    `usage`, and the record holds no per-subagent counters or identity.
    """
    tokens = run_tokens(record)
    counters = {f: tokens[f] for f in TOKEN_FIELDS}
    raw_tokens = record.get("tokens") or {}
    provider = run_provider(record)
    base = {"task_id": str(record.get("task_id")), "run_id": record.get("run_id"),
            "role": record.get("role"), "role_label": role_label(record.get("role")),
            "provider": provider}
    reported = _reported_models(record)
    requested = _value((record.get("model") or {}).get("requested"))

    def row(model: Any, source: str, basis: str, values: dict[str, Any], reasoning: Any = None) -> dict[str, Any]:
        return {**base, "model": model, "model_name": model_name(model), "model_source": source,
                "model_basis": basis, "tokens": values, "reasoning_output": reasoning,
                "input_total": input_total(values, provider), "cached_share": cached_share(values, provider),
                "total": _total(values, provider)}

    if reported and len(reported) > 1:
        by_model = (record.get("session_cumulative") or {}).get("by_model") or {}
        split_ok = (tokens["complete"] and set(by_model) == set(reported)
                    and all(_num((by_model[m] or {}).get(f)) is not None for m in reported for f in TOKEN_FIELDS)
                    and all(sum(by_model[m][f] for m in reported) == counters[f] for f in TOKEN_FIELDS))
        if split_ok:
            return [row(m, "reported", "reported; split by result.modelUsage, which sums exactly to this "
                        "invocation's usage", {f: by_model[m][f] for f in TOKEN_FIELDS}) for m in reported]
        return [row(" + ".join(reported), "mixed", "mixed: several reported models, not split (per-model "
                    "counters are session-cumulative or do not equal this invocation's usage)", counters)]
    if reported:
        return [row(reported[0], "reported", "reported", counters, raw_tokens.get("reasoning_output"))]
    if requested:
        return [row(requested, "requested", "requested only (the provider reported no model identity)", counters,
                    raw_tokens.get("reasoning_output"))]
    return [row(None, "unknown", "unknown", counters, raw_tokens.get("reasoning_output"))]


def _total(values: dict[str, Any], provider: str | None = "claude") -> int | None:
    """The provider's total input + output, or None when either is unknown."""
    total_in, output = input_total(values, provider), values.get("output")
    return None if total_in is None or output is None else total_in + output


def aggregate_attribution(rows: list[dict[str, Any]], keys: tuple[str, ...]) -> list[dict[str, Any]]:
    """Sum rows by `keys`; a counter is null when any contributing row lacks it.

    Input totals and totals are sums of the rows' own (each in its provider's
    terms), so a Codex row with unreported cache writes still counts.
    """
    groups: dict[tuple, dict[str, Any]] = {}
    for item in rows:
        key = tuple(item.get(k) for k in keys)
        group = groups.setdefault(key, {**{k: item.get(k) for k in keys}, "runs": [],
                                        "tokens": {f: [] for f in TOKEN_FIELDS}, "reasoning_output": [],
                                        "input_total": [], "total": []})
        if item["run_id"] not in group["runs"]:
            group["runs"].append(item["run_id"])
        for f in TOKEN_FIELDS:
            group["tokens"][f].append(item["tokens"][f])
        for name in ("reasoning_output", "input_total", "total"):
            group[name].append(item.get(name))
    out = []
    for group in groups.values():
        group["tokens"] = {f: _sum_all(v) for f, v in group["tokens"].items()}
        reasoning = [v for v in group["reasoning_output"] if v is not None]
        group["reasoning_output"] = sum(reasoning) if reasoning and len(reasoning) == len(group["reasoning_output"]) else None
        group["input_total"] = _sum_all(group["input_total"])
        group["total"] = _sum_all(group["total"])
        cached = group["tokens"]["cached_input"]
        group["cached_share"] = (round(cached / group["input_total"], 4)
                                 if cached is not None and group["input_total"] else None)
        if "model" in keys:
            group["model_name"] = model_name(group["model"])
        if "role" in keys:
            group["role_label"] = role_label(group["role"])
        out.append(group)
    return out


def attribution(records: list[dict[str, Any]]) -> dict[str, Any]:
    rows = [r for record in records for r in attribute_usage(record)]
    return {
        "rows": rows,
        # model_source stays a key: requested-only usage never merges into a reported model's totals.
        "by_provider_model": aggregate_attribution(rows, ("provider", "model", "model_source")),
        "by_role": aggregate_attribution(rows, ("role",)),
        "by_provider_model_role": aggregate_attribution(rows, ("provider", "model", "model_source", "role")),
        "basis": "per-invocation tokens (" + NO_DOUBLE_COUNT + "); each run's rows partition it once; "
                 "reasoning_output is a subset of output and never added; total = the provider's input + output",
        "cached_share_basis": CACHED_SHARE_BASIS,
        "subagents": "not attributed separately: subagent usage is already inside its run's usage, and the "
                     "record holds no per-subagent counters or identity",
    }


# --- live (running) runs -------------------------------------------------------------


def _alive(record: dict[str, Any]) -> bool | None:
    info = record.get("recorder") or {}
    pid = info.get("pid")
    if not isinstance(pid, int) or info.get("host") != recorder.socket.gethostname():
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


RECORDER_SCRIPT = "direct_run_recorder.py"
START_SLACK_S = 2  # `ps` elapsed time is whole seconds


def _etime_seconds(text: str) -> int | None:
    """`ps` elapsed time, `[[dd-]hh:]mm:ss`, in seconds."""
    days, _, clock = text.rpartition("-")
    parts = clock.split(":")
    if not 2 <= len(parts) <= 3 or not all(p.isdigit() for p in parts) or (days and not days.isdigit()):
        return None
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + int(part)
    return seconds + int(days or 0) * 86400


def process_probe(pid: int) -> tuple[int, list[str]] | None:
    """Seconds since a local process started and its argv words, from `ps`; None if unknown."""
    try:
        done = subprocess.run(["ps", "-ww", "-o", "etime=", "-o", "args=", "-p", str(pid)],
                              capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.SubprocessError):
        return None
    words = done.stdout.split()
    if done.returncode != 0 or len(words) < 2:
        return None
    seconds = _etime_seconds(words[0])
    return None if seconds is None else (seconds, words[1:])


def _flag(args: list[str], name: str) -> str | None:
    for i, arg in enumerate(args):
        if arg == name and i + 1 < len(args):
            return args[i + 1]
        if arg.startswith(name + "="):
            return arg.split("=", 1)[1]
    return None


_PYTHON = re.compile(r"^(?:python(?:\d+(?:\.\d+)*)?|Python)$")
_NLH_VALUE_OPTIONS = ("--project", "--config")  # `nlh` options before the subcommand


def _base(word: str) -> str:
    return word.rsplit("/", 1)[-1]


def _after_nlh_options(words: list[str]) -> list[str] | None:
    """The words after `nlh`'s global options when the subcommand is `record`."""
    i = 0
    while i < len(words) and words[i].startswith("-"):
        if words[i] in _NLH_VALUE_OPTIONS:
            i += 2
        elif words[i].split("=", 1)[0] in _NLH_VALUE_OPTIONS and "=" in words[i]:
            i += 1
        else:
            return None
    return words[i + 1:] if i < len(words) and words[i] == "record" else None


def _python_target(words: list[str]) -> tuple[str, str, list[str]] | None:
    """What a Python interpreter's argv runs: ("script", path, rest) or ("module", name, rest).

    `-c` code and stdin (`-`) are never a recorder, whatever words follow them.
    """
    i = 0
    while i < len(words):
        word = words[i]
        if word == "-" or not word.startswith("-"):
            return None if word == "-" else ("script", word, words[i + 1:])
        if word.startswith("--"):
            i += 2 if word == "--check-hash-based-pycs" else 1
            continue
        for at, letter in enumerate(word[1:], start=1):
            attached = word[at + 1:]
            if letter == "c":
                return None
            if letter == "m":
                if attached:
                    return ("module", attached, words[i + 1:])
                return ("module", words[i + 1], words[i + 2:]) if i + 1 < len(words) else None
            if letter in "WX":
                i += 0 if attached else 1
                break
        i += 1
    return None


def recorder_arguments(words: list[str]) -> list[str] | None:
    """The recorder's own arguments when argv executes the recorder, else None.

    Only the executed entrypoint counts, checked from argv[0]: the recorder
    script (`python … direct_run_recorder.py run`), its module
    (`python -m direct_run_recorder run`), or `nlh record` (the console script,
    run directly or by Python, or `python -m harmony.cli record`), which runs the
    recorder in-process. Recorder words anywhere else — `-c` code, another
    script's arguments, a wrapped command — do not.
    """
    if not words:
        return None
    if _base(words[0]) == "nlh":
        rest = _after_nlh_options(words[1:])
    elif _PYTHON.match(_base(words[0])):
        target = _python_target(words[1:])
        if target is None:
            return None
        kind, name, rest = target
        entry = _base(name) if kind == "script" else name
        if (kind, entry) in (("script", RECORDER_SCRIPT), ("module", "direct_run_recorder")):
            rest = rest[1:] if rest[:1] == ["run"] else None
        elif (kind, entry) in (("script", "nlh"), ("module", "harmony.cli")):
            rest = _after_nlh_options(rest)
        else:
            return None
    else:
        return None
    if rest is None:
        return None
    return rest[:rest.index("--")] if "--" in rest else rest


def confirm_active(raw: dict[str, Any], alive: bool | None, probe=None) -> dict[str, Any]:
    """Whether a running record is provably this task's run in progress.

    A live pid alone is not proof: the recorder may have died and its pid been
    reused. Confirmed only when the pid on this host executes the recorder
    (see `recorder_arguments`) for the record's task and role (and run id,
    when its argv names one) and that process started no later than the
    record. A bare provider CLI, a quota reader or any other process never
    qualifies.
    """
    def answer(confirmed: bool, basis: str) -> dict[str, Any]:
        return {"confirmed": confirmed, "basis": basis}

    if alive is not True:
        return answer(False, "recorder process gone" if alive is False else "recorder process not checkable")
    seen = (probe or process_probe)(raw["recorder"]["pid"])
    if seen is None:
        return answer(False, "recorder process could not be inspected")
    elapsed, words = seen
    args = recorder_arguments(words)
    if args is None:
        return answer(False, "the pid is not a recorder run (reused pid?)")
    run_id = _flag(args, "--run-id")
    if (_flag(args, "--task-id") != str(raw.get("task_id")) or _flag(args, "--role") != raw.get("role")
            or (run_id is not None and run_id != raw.get("run_id"))):
        return answer(False, "the recorder process is for another task, role or run")
    started = _parse_time(raw.get("started_at"))
    if started is None:
        return answer(False, "the record has no start time")
    process_start = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=elapsed)
    if process_start > started + dt.timedelta(seconds=START_SLACK_S):
        return answer(False, "the process started after the record (reused pid)")
    return answer(True, "this task's recorder process is alive and started before the record")


def live_runs(directory: Path, task_id: str | None = None, now: dt.datetime | None = None,
              probe=None) -> list[dict[str, Any]]:
    """Running snapshots (status `running`), newest first; never counted as completed runs.

    `active.confirmed` is the stricter proof the overview uses (see
    `confirm_active`); `state` keeps its earlier, pid-only meaning.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    out = []
    for path in iter_record_paths(directory):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(raw, dict) or raw.get("schema") != recorder.SCHEMA or raw.get("status") != "running":
            continue
        if task_id is not None and str(raw.get("task_id")) != str(task_id):
            continue
        alive = _alive(raw)
        started = _parse_time(raw.get("started_at"))
        telemetry = raw.get("telemetry") or {}
        out.append({
            "run_id": raw.get("run_id"), "task_id": str(raw.get("task_id")), "role": raw.get("role"),
            "role_label": role_label(raw.get("role")), "provider": raw.get("provider"),
            "model_requested": _value((raw.get("model") or {}).get("requested")),
            "models_seen": telemetry.get("models_seen") or [],
            "started_at": raw.get("started_at"), "snapshot_at": raw.get("snapshot_at"),
            "state": {True: "running", False: "interrupted", None: "running (process not checkable)"}[alive],
            "state_basis": ("recorder process alive" if alive else
                            "recorder process gone and no final record was written" if alive is False else
                            "recorded on another host or without a pid"),
            "elapsed_ms": int((now - started).total_seconds() * 1000) if started and alive is not False else None,
            "active": confirm_active(raw, alive, probe),
            "telemetry": telemetry,
        })
    return sorted(out, key=lambda r: str(r.get("started_at")), reverse=True)


# --- the companion's per-task view ----------------------------------------------------


def confirmed_running(run: dict[str, Any]) -> bool:
    """Whether a live entry may claim activity: only with the recorder proof (`active.confirmed`).

    A live pid alone (possibly reused) or an entry from a caller without the
    proof never claims a running role or phase.
    """
    active = run.get("active")
    return run.get("state") == "running" and isinstance(active, dict) and active.get("confirmed") is True


def _phases(graph: dict[str, Any] | None, summary: dict[str, Any], live: list[dict[str, Any]],
            skipped: list[dict[str, str]]) -> list[dict[str, Any]] | None:
    if not isinstance(graph, dict) or not isinstance(graph.get("nodes"), list):
        return None
    completed = {run["run_id"]: run for run in summary["runs"]}
    running = {run["run_id"]: run for run in live}
    other = {Path(item["path"]).stem: item["why"] for item in skipped}
    phases = []
    for node in graph["nodes"]:
        if not isinstance(node, dict):
            continue
        runs = []
        for rid in node.get("run_ids") or []:
            if rid in completed:
                run = completed[rid]
                runs.append({"run_id": rid, "status": "recorded", "provider": run["provider"],
                             "model": run["model"], "duration_ms": run["duration_ms"]["value"],
                             "total_tokens": _total(run["tokens"], run["provider"])})
            elif rid in running:
                run = running[rid]
                status = run["state"]
                if confirmed_running(run):
                    status = "running"
                elif status != "interrupted":
                    status = "running (unconfirmed)"
                runs.append({"run_id": rid, "status": status})
            else:
                runs.append({"run_id": rid, "status": "not a completed record" if rid in other else "missing"})
        phases.append({key: node.get(key) for key in ("id", "type", "role", "state", "model", "started_at",
                                                        "completed_at", "outcome")} | {"runs": runs})
    return phases


def task_observability(directory: Path, task_id: str, *, graph: dict[str, Any] | None = None,
                       now: dt.datetime | None = None) -> dict[str, Any]:
    """Everything the companion shows for one task, from the same calculations as the CLI."""
    records, skipped = load_task_records(directory, task_id)
    summary = summarize_task(records, task_id, skipped=skipped)
    live = live_runs(directory, task_id, now)
    return {
        "task_id": str(task_id),
        "summary": summary,
        "live": live,
        "phases": _phases(graph, summary, live, skipped),
        "telemetry": {run["run_id"]: (record.get("telemetry") or None) for run, record in
                      zip(summary["runs"], _order(records)[0])},
    }


# --- comparison -------------------------------------------------------------------


def _metric(name: str, a: Any, b: Any, basis_a: Any = None, basis_b: Any = None) -> dict[str, Any]:
    a, b = _num(a), _num(b)
    entry: dict[str, Any] = {"metric": name, "a": a, "b": b}
    if a is None or b is None:
        missing = " and ".join(side for side, v in (("a", a), ("b", b)) if v is None)
        return {**entry, "comparable": False, "reason": f"unknown on {missing}"}
    if basis_a != basis_b:
        return {**entry, "comparable": False,
                "reason": f"different basis ({basis_a} vs {basis_b})"}
    diff = b - a
    entry.update(comparable=True, abs_diff=round(diff, 10) if isinstance(diff, float) else diff)
    if a == 0:
        entry.update(pct_diff=None, pct_note="a is zero; percent change undefined")
    else:
        entry["pct_diff"] = round(diff / a * 100, 2)
    return entry


def compare(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """Two inspections or two summaries; comparable numbers only."""
    if a.get("kind") != b.get("kind") or a.get("kind") not in (INSPECTION_KIND, SUMMARY_KIND):
        raise ObservabilityError("compare needs two runs or two tasks, not a mix")
    metrics = []
    if a["kind"] == INSPECTION_KIND:
        ra, rb, da, db = a["raw"], b["raw"], a["derived"], b["derived"]
        label = {"a": ra["run_id"], "b": rb["run_id"]}
        for field in TOKEN_FIELDS:
            metrics.append(_metric(f"tokens.{field}", ra["tokens"][field], rb["tokens"][field]))
        metrics.append(_metric("processed_input_tokens", da["processed_input_tokens"]["value"],
                               db["processed_input_tokens"]["value"]))
        metrics.append(_metric("duration_ms", da["duration_ms"]["value"], db["duration_ms"]["value"],
                               da["duration_ms"]["basis"], db["duration_ms"]["basis"]))
        metrics.append(_metric("num_turns", ra["num_turns"]["value"], rb["num_turns"]["value"]))
        metrics.append(_metric("estimated_list_cost_usd", ra["estimated_list_cost_usd"]["value"],
                               rb["estimated_list_cost_usd"]["value"]))
        metrics.append(_metric("actual_provider_charge_usd", ra["actual_provider_charge_usd"]["value"],
                               rb["actual_provider_charge_usd"]["value"]))
        sa, sb = da["stream"], db["stream"]
        metrics.append(_metric("tool_calls_total", sa.get("tool_calls_total") if sa["available"] else None,
                               sb.get("tool_calls_total") if sb["available"] else None))
        context = {"role": {"a": ra["role"], "b": rb["role"]},
                   "model": {"a": ra["model_requested"]["value"], "b": rb["model_requested"]["value"]},
                   "same_session": (ra["session_id"]["value"] is not None
                                    and ra["session_id"]["value"] == rb["session_id"]["value"])}
    else:
        label = {"a": a["task_id"], "b": b["task_id"]}
        for field in TOKEN_FIELDS + ("processed_input",):
            metrics.append(_metric(f"tokens.{field}", a["tokens"][field], b["tokens"][field]))
        metrics.append(_metric("duration_ms", a["duration_ms"]["value"], b["duration_ms"]["value"],
                               a["duration_ms"]["bases"], b["duration_ms"]["bases"]))
        metrics.append(_metric("run_count", a["run_count"], b["run_count"]))
        metrics.append(_metric("correction_runs", a["correction_runs"], b["correction_runs"]))
        metrics.append(_metric("review.rounds", a["review"]["rounds"], b["review"]["rounds"]))
        metrics.append(_metric("estimated_list_cost_usd", a["estimated_list_cost_usd"]["value"],
                               b["estimated_list_cost_usd"]["value"]))
        metrics.append(_metric("actual_provider_charge_usd", a["actual_provider_charge_usd"]["value"],
                               b["actual_provider_charge_usd"]["value"]))
        context = {"final_outcome": {"a": a["final_outcome"], "b": b["final_outcome"]}}
    return {"kind": COMPARISON_KIND, "of": a["kind"], "a": label["a"], "b": label["b"],
            "diff_direction": "b minus a; percent relative to a",
            "context": context, "metrics": metrics}


# --- board card -----------------------------------------------------------------------


def fmt_tokens(n: int | float | None) -> str:
    if n is None:
        return "unknown"
    n = float(n)
    for unit, size in (("B", 1e9), ("M", 1e6), ("k", 1e3)):
        if n >= size:
            return f"{n / size:.1f}{unit}"
    return str(int(n))


def fmt_duration(ms: int | float | None) -> str:
    if ms is None:
        return "unknown"
    seconds = int(round(ms / 1000))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def card_metrics(summary: dict[str, Any], live: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    """The tiny card line: at most one token total and one duration, plus live runs.

    Tokens = the task's processed input + output (per-invocation usage summed
    over completed runs), shown only when both are known for every run;
    duration only when every run has one. Nothing else (no run count, no
    cost) goes on a card. For an active card, each confirmed running run
    (`confirmed_running`) adds its role and elapsed wall time (recorder
    clock); its provisional tokens are not added to the total. A pid-only or
    unproven running entry adds nothing. Returns None when nothing is safe to
    show.
    """
    running = [run for run in (live or []) if confirmed_running(run)]
    if not summary.get("run_count") and not running:
        return None
    total = duration = None
    parts = []
    if summary.get("run_count"):
        tokens = summary["tokens"]
        processed, output = tokens.get("processed_input"), tokens.get("output")
        total = processed + output if processed is not None and output is not None else None
        duration = summary["duration_ms"]["value"]
        if total is not None:
            parts.append(f"{fmt_tokens(total)} tok")
        if duration is not None:
            parts.append(fmt_duration(duration))
    for run in running:
        elapsed = run.get("elapsed_ms")
        parts.append(f"▶ {run.get('role_label') or run.get('role')}"
                     + (f" {fmt_duration(elapsed)}" if elapsed is not None else ""))
    if not parts:
        return None
    metrics = {
        "line": " · ".join(parts),
        "total_tokens": total,
        "duration_ms": duration,
        "basis": "tokens = processed input + output; " + NO_DOUBLE_COUNT,
    }
    if live is not None:
        metrics["basis"] += "; completed runs only — running runs show elapsed wall time, not tokens"
        metrics["running"] = len(running)
    return metrics


# --- text rendering -------------------------------------------------------------------


def _fmt(value: Any) -> str:
    if value is None:
        return "unknown"
    if isinstance(value, float):
        return f"{value:.6f}".rstrip("0").rstrip(".")
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def render_inspection(report: dict[str, Any], activity_limit: int = 0) -> str:
    raw, derived = report["raw"], report["derived"]
    tok = raw["tokens"]
    est, charge = raw["estimated_list_cost_usd"], raw["actual_provider_charge_usd"]
    lines = [
        f"run {raw['run_id']}  task {raw['task_id']}  role {raw['role']}",
        "raw (from the run record)",
        f"  session        {_fmt(raw['session_id']['value'])}  resumed={_fmt(raw['resumed_session']['value'])}",
        f"  model          requested {_fmt(raw['model_requested']['value'])}; "
        f"reported {_fmt(raw['model_reported']['value'])}",
        f"  outcome        cli {_fmt(raw['outcome_cli']['value'])} "
        f"({_fmt(raw['outcome_cli']['result_subtype'])}); review {_fmt(raw['outcome_review']['value'])}",
        f"  duration_ms    wall {_fmt(raw['duration_ms']['wall']['value'])}; reported "
        f"{_fmt(raw['duration_ms']['reported']['value'])}; api {_fmt(raw['duration_ms']['api_reported']['value'])}",
        f"  tokens         uncached {_fmt(tok['uncached_input'])} · cache create {_fmt(tok['cache_creation_input'])}"
        f" · cache read {_fmt(tok['cached_input'])} · output {_fmt(tok['output'])}  [{_fmt(tok['scope'])}]",
        f"  est. list cost {_fmt(est['value'])} USD"
        + (f"  ({est['method']})" if est["value"] is not None else f"  — {est['unknown_reason'] or 'unknown'}"),
        f"  provider charge {_fmt(charge['value'])}" + (f" USD ({charge['source']})" if charge["value"] is not None else ""),
        f"  session-cumulative (never added): {_fmt(raw['session_cumulative']['totals'])}",
        "derived (computed here)",
        f"  duration       {fmt_duration(derived['duration_ms']['value'])} (basis {_fmt(derived['duration_ms']['basis'])})",
        f"  processed input {_fmt(derived['processed_input_tokens']['value'])}",
    ]
    stream = derived["stream"]
    if not stream["available"]:
        lines.append(f"  tools/files/activity unknown: {stream['reason']}")
    else:
        tools = ", ".join(f"{k} {v}" for k, v in stream["tool_calls"].items()) or "none"
        lines += [
            f"  tool calls     {stream['tool_calls_total']} ({tools}); errors {stream['tool_errors']}; "
            f"subagent calls {stream['subagent_tool_calls']}",
            f"  files touched  {len(stream['files'])}",
        ]
        for path, counts in stream["files"].items():
            lines.append(f"    {path}  read {counts['read']} edit {counts['edit']} write {counts['write']}")
        if stream["repeated_reads"]:
            lines.append("  repeated reads")
            for item in stream["repeated_reads"]:
                lines.append(f"    {item['path']}  x{item['reads']} (identical range repeats {item['same_range_repeats']})")
        activity = stream["activity"]
        shown = activity if activity_limit <= 0 else activity[:activity_limit]
        lines.append(f"  activity ({len(activity)} events{'' if len(shown) == len(activity) else f', first {len(shown)}'})")
        for item in shown:
            detail = item.get("tool") or item.get("subtype") or item.get("model") or ""
            if item.get("target"):
                detail += f" {item['target']}"
            if item.get("kind") == "text":
                detail = f"{item['chars']} chars"
            flag = " ERROR" if item.get("is_error") else ""
            agent = " [sub]" if item["agent"] == "subagent" else ""
            lines.append(f"    {item['seq']:>4} {item['kind']:<13}{agent} {detail}{flag}".rstrip())
    lines.append("unknowns: " + (", ".join(report["unknowns"]) or "none"))
    return "\n".join(lines)


def render_summary(summary: dict[str, Any]) -> str:
    tok = summary["tokens"]
    final = summary["final_outcome"]
    lines = [
        f"task {summary['task_id']}: {summary['run_count']} runs "
        f"(writer {summary['writer_runs']}, correction {summary['correction_runs']}; roles {summary['roles']})",
        f"  run order      {'single run' if summary['run_count'] <= 1 else 'by started_at' if summary['order_known'] else 'unknown (listed by run id)'}",
    ]
    for run in summary["runs"]:
        reported = run.get("model_reported")
        lines.append(
            f"    {run['run_id']}  {run['role']}  {run.get('provider', 'claude')}  {_fmt(run['model'])}"
            + (f" (reported {', '.join(reported)})" if reported else "") + "  "
            f"{fmt_duration(run['duration_ms']['value'])}  out {_fmt(run['tokens']['output'])}  "
            f"cli {_fmt(run['outcome_cli'])}  review {_fmt(run['outcome_review'])}"
        )
    if summary["shared_sessions"]:
        for sid, ids in summary["shared_sessions"].items():
            lines.append(f"  shared session {sid}: {', '.join(ids)} (session-cumulative counters not added)")
    lines += [
        f"  review         rounds {_fmt(summary['review']['rounds'])}; corrections annotated "
        f"{_fmt(summary['review']['corrections_annotated'])}  [{summary['review']['basis']}]",
        "  final outcome  " + (f"{final['run_id']}: cli {_fmt(final['cli'])}, review {_fmt(final['review'])}"
                                if final["run_id"] else f"unknown — {final['unknown_reason']}"),
        f"  duration       {fmt_duration(summary['duration_ms']['value'])} (bases {summary['duration_ms']['bases']})",
        f"  tokens         uncached {_fmt(tok['uncached_input'])} · cache create {_fmt(tok['cache_creation_input'])}"
        f" · cache read {_fmt(tok['cached_input'])} · output {_fmt(tok['output'])}",
        f"                 [{tok['basis']}]",
        f"  est. list cost {_fmt(summary['estimated_list_cost_usd']['value'])} USD (not a charge)",
        f"  provider charge {_fmt(summary['actual_provider_charge_usd']['value'])}",
    ]
    groups = (summary.get("attribution") or {}).get("by_provider_model") or []
    if groups:
        lines.append("  by provider/model (processed input + output; reasoning output is a subset, not added)")
        for group in groups:
            lines.append(f"    {group['provider']:<7} {_fmt(group['model']):<32} {group['model_source']:<9} "
                         f"{_fmt(group['total'])}"
                         f"  runs {len(group['runs'])}"
                         + (f"  reasoning {_fmt(group['reasoning_output'])}" if group["reasoning_output"] is not None else ""))
    for group in (summary.get("attribution") or {}).get("by_role") or []:
        lines.append(f"    role {role_label(group['role']):<13} {_fmt(group['total'])}  runs {len(group['runs'])}")
    for item in summary["skipped_records"]:
        lines.append(f"  skipped {item['path']}: {item['why']}")
    lines.append("unknowns: " + (", ".join(summary["unknowns"]) or "none"))
    return "\n".join(lines)


def render_comparison(result: dict[str, Any]) -> str:
    lines = [f"compare {result['of']}: a={result['a']}  b={result['b']}  ({result['diff_direction']})"]
    for key, value in result["context"].items():
        lines.append(f"  {key}: {value}")
    for m in result["metrics"]:
        if m["comparable"]:
            pct = f"{m['pct_diff']:+.2f}%" if m.get("pct_diff") is not None else m.get("pct_note", "")
            lines.append(f"  {m['metric']:<28} {_fmt(m['a'])} -> {_fmt(m['b'])}  "
                         f"diff {m['abs_diff']:+,}  {pct}")
        else:
            lines.append(f"  {m['metric']:<28} incomparable: {m['reason']}")
    return "\n".join(lines)


# --- CLI ----------------------------------------------------------------------------------


def _load_run_arg(value: str, directory: Path) -> dict[str, Any]:
    path = Path(value)
    if path.suffix == ".json" and path.is_file():
        return load_record(path)
    return find_run(directory, value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="run_observability.py", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--records-dir", help=f"default: ${recorder.RECORDS_DIR_ENV} or {recorder.DEFAULT_RECORDS_DIR}")
        p.add_argument("--json", action="store_true", help="machine-readable output")

    ins = sub.add_parser("inspect-run", help="one run: tokens, cost fields, tools, files, activity")
    common(ins)
    target = ins.add_mutually_exclusive_group(required=True)
    target.add_argument("--record", help="path to a run record")
    target.add_argument("--run-id")
    ins.add_argument("--stream", help="override the record's linked stream file")
    ins.add_argument("--activity-limit", type=int, default=0, help="text mode: show the first N events (0 = all)")

    summ = sub.add_parser("summarize-task", help="one task across its runs")
    common(summ)
    summ.add_argument("--task-id", required=True)
    summ.add_argument("--actual-charge-usd", type=float, help="explicitly known provider charge for the task")
    summ.add_argument("--actual-charge-source", help="where that charge comes from (invoice, console...)")

    cmp_ = sub.add_parser("compare", help="two runs or two tasks")
    common(cmp_)
    cmp_.add_argument("--run", action="append", default=[], help="run id or record path (twice)")
    cmp_.add_argument("--task", action="append", default=[], help="task id (twice)")

    args = parser.parse_args(argv)
    directory = records_dir(args.records_dir)
    try:
        if args.command == "inspect-run":
            record = load_record(args.record) if args.record else find_run(directory, args.run_id)
            result = inspect_record(record, args.stream)
            text = render_inspection(result, args.activity_limit)
        elif args.command == "summarize-task":
            result = summarize_task_dir(directory, args.task_id, actual_charge_usd=args.actual_charge_usd,
                                        actual_charge_source=args.actual_charge_source)
            if not result["run_count"]:
                print(f"run_observability: no completed run records for task {args.task_id!r} in {directory}",
                      file=sys.stderr)
            text = render_summary(result)
        else:
            if (len(args.run), len(args.task)) not in ((2, 0), (0, 2)):
                parser.error("compare takes exactly two --run or exactly two --task")
            if args.run:
                a, b = (inspect_record(_load_run_arg(v, directory)) for v in args.run)
            else:
                a, b = (summarize_task_dir(directory, v) for v in args.task)
            result = compare(a, b)
            text = render_comparison(result)
    except ObservabilityError as exc:
        print(f"run_observability: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2) if args.json else text)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
