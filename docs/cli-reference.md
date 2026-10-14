# Harmony CLI Command Reference

A quick reference for the `nlh` command. Run `nlh --help` to see top-level commands and `nlh <command> --help` for accepted flags.

> **Before you begin:** Run commands from your target project's directory, or use `nlh --project /path/to/project ...`. Harmony uses the nearest `harmony.json` (or `--config /path/to/harmony.json`) and keeps task state in that project's `.workflow/` directory. Project selection options go **before** the command.

## Command overview

| Command | Purpose |
|---|---|
| `nlh companion` | Launch the local Observability and Tasks interface. |
| `nlh inspect <id>` | Print task checkpoints, workflow graph, recorded runs, and evidence summary. |
| `nlh record --task-id <id> --role <role> -- COMMAND...` | Run a Claude or Codex invocation with telemetry associated with an existing task. |
| `nlh task resolve [--task-id <id>]` | Resolve a known task ID, or show the open tasks when an ID is missing. |
| `nlh task relate <id> --follow-up-of <other-id>` | Link an existing task to an earlier task without deleting either record. |
| `nlh task archive <id>` | Hide a completed task from the active board, preserving its history. |
| `nlh task restore <id>` | Return an archived task to the board. |
| `nlh approval request <id> ...` | Create a revision-bound request for human approval of a particular action. |
| `nlh approval show <id>` | View a task's approval requests, bindings, and decisions. |
| `nlh approval decide <id> ...` | Record an approve, request-changes, or defer decision. |
| `nlh approval execute <id> ...` | Record the result **after** an approved action has been executed and verified. |
| `nlh --version` | Print the installed Harmony version. |
| `nlh --help` | Show general CLI help. |

**Creating tasks:** Use **Companion → Tasks → Add task**. There is no `nlh task create` subcommand in v0.1.

## Start and inspect

```bash
nlh companion
nlh companion --no-browser --port 0   # pick a free port and print its URL
nlh inspect 3                          # inspect task #3
nlh task resolve --task-id 3
nlh task archive 3                     # task must be Done
nlh task restore 3
nlh task relate 5 --follow-up-of 3
```

The Companion defaults to your configured port, otherwise port `8765`. A task's board position never grants permission to commit, merge, or publish.

## Record a model run

Use an existing, authorized task ID. The final `--` separates Harmony options from the CLI command being recorded.

```bash
nlh record --task-id 3 --role planner -- \
  codex exec --json --sandbox read-only -m YOUR_CODEX_MODEL - < plan.md
```

For a Claude implementation run, use a real approved task and load the packaged PR guard settings:

```bash
D="$(python3 -c 'from pathlib import Path; import harmony; print(Path(harmony.__file__).parent / "direct-workflow")')"
export HARMONY_PR_GUARD="$D/direct-workflow-pr-guard.py"

nlh record --task-id 3 --role writer -- \
  claude -p "Implement the approved task" --model opus \
  --output-format stream-json --verbose \
  --settings "$D/release-settings.json"
```

Supported roles are validated by the recorder; `writer` means implementation, and `correction` means addressing review findings. **Investigators** require an explicit `--base-commit SHA` and read-only isolation; see the [workflow guide](../harmony/direct-workflow/README.md) for the checked command and restrictions. `nlh record` records an invocation, but does not autonomously plan or dispatch an entire workflow.

For additional run options:

```bash
nlh record --help
```

## Human approvals

Approvals are specific to a **task, gate, action, and revision**. They do not execute Git operations.

**Request approval** once the required task review and verification steps have passed:

```bash
nlh approval request 3 \
  --action commit \
  --summary "Reviewed local changes ready to commit" \
  --evidence evidence/verification.md
```

**Inspect the pending request:**

```bash
nlh approval show 3
nlh approval show 3 --json
```

**Record a decision.** Use the exact `--request`, `--gate`, and `--binding` from the current approval offer. Choose **one** of `--approve`, `--request-changes`, or `--defer`. Requesting changes also requires `--note`.

```bash
nlh approval decide 3 \
  --request REQUEST_ID --action commit \
  --gate GATE_ID --binding BINDING_TOKEN \
  --request-changes --note "Please address the failing test"
```

**After** a commit, merge, or publication has been explicitly approved, actually performed, and verified, record the result:

```bash
nlh approval execute 3 \
  --request REQUEST_ID --action commit \
  --binding BINDING_TOKEN \
  --succeeded --result-ref COMMIT_SHA \
  --evidence evidence/commit-verification.md
```

This last command only **records evidence**. It never performs the Git operation. Commit, merge, and publication require separate authorization.

## Global options and additional help

```bash
nlh --project /path/to/project companion
nlh --config /path/to/project/harmony.json inspect 3
nlh approval request --help
nlh approval decide --help
nlh task archive --help
```

For detailed workflow scripts, checkpoints, graphs, and approval semantics, see the [workflow guide](../harmony/direct-workflow/README.md) and [Companion guide](../harmony/task-board/README.md). Those are in the same GitHub repository.

[← Back to Harmony](../README.md)
