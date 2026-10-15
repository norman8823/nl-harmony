# Harmony v0.1.0

This update adds local Codex CLI task tracking to the existing public release.
Published history is retained; development history and local runtime state are excluded.

## Local Codex CLI task tracking

- One running Companion receives tasks from Codex terminals in any repository on
  the same computer. No hosted service, login, or per-task wrapper is needed.
- Follow-up prompts, turns, and runs reuse the same task. Codex updates its stage
  to In Progress, Blocked when waiting, and Done when the requested work is complete.
- Completed cards show **Completed by Codex** with the summary, including earlier
  Codex completions. Companion does not require an additional review.
- **Archive completed** lives in the Done column; record-check filter toggles are removed.
- The local installer preserves existing Codex settings and saved hook approval.
  The README explains the first-launch hook review and upgrade restarts.
- Stale edits and declared workflow gates remain enforced. Completion errors now
  report Companion's actual refusal instead of inferring missing requirements.
- Occupied Companion ports show restart instructions instead of a cleanup traceback.

## Included

- Explicit Claude implementation/correction and Codex planning/independent review.
- Durable canonical task identity, bounded investigation graphs, compact checkpoints
  and resumable handoffs; selective context and command-output summaries.
- Two-tab Companion: measured Observability and Tasks, with secondary Inspect.
- Provider/model/role contribution bars, supported weekly allowance, cache reuse,
  precise unknown/stale semantics and diagnostics separate from actual charges.
- Manual task creation/editing/closure/reopening, archive/restore, verified workflow
  completion and durable revision-bound human approval feedback.
- Recorded execution required after approved final actions; separate commit,
  workflow completion, merge and publication scopes, with guarded release tools.

Python 3.10+ on macOS/Linux; no runtime Python dependencies or frontend build.
MIT license retained. Model runs require separately installed authenticated CLIs.
Local Codex hook configuration requires Python 3.11+ and a Codex CLI with hooks.

## Known limits

Agents invoke workflow commands explicitly; there is no automatic model routing
or agent scheduler. Claude quota parses its documented native CLI terminal output,
which is not a versioned JSON API. Unknown readings stay unknown. Requested Codex
model identifiers are qualified when not provider-reported. Subagent counters are
not invented. API-equivalent estimates are not actual charges or quota usage.

Approved release-image availability and clean-install/browser verification are
recorded in the local release-preparation report before publication approval.

New task graphs let read-only investigators run beside implementation (two
running nodes by default). Review consumes their evidence after all have
finished or been skipped. Existing graphs preserve their previous shape.
Investigator invocations require an explicit base commit and a temporary
detached worktree; records retain the actual commit read. Claude additionally
requires restricted file access and explicit write/execution-tool denial.
