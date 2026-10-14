# Harmony
🤝

*by NL Intelligence*

**Better Together.**

An open-source orchestration and observability toolkit for AI-powered software development.

Harmony brings Claude Code and OpenAI Codex together in a coordinated workflow, combining model orchestration, persistent context, token efficiency, task management, and resource observability.

**Less managing. More building.**

![Harmony workflow: Codex plans and reviews, Claude implements and corrects](docs/images/workflow.png)

## A clear division of work

Codex plans and independently reviews. Claude implements and corrects. Optional
investigators run alongside implementation, read-only on a pinned commit. Their
findings feed independent review, and the task graph bounds concurrency. A human
approves the final action, with commit, merge and publication kept as separate
permissions.

Harmony records this workflow so the next session can resume with the right
context. Agents and operators invoke the tools explicitly; Harmony does not
choose models, schedule agents or run an autonomous development pipeline.

## Harmony Companion

One local interface. **Observability** shows supported weekly allowance,
provider/model/role token contributions and cache reuse. **Tasks** provides a
Kanban board with editing, closure, archive and restore. **Inspect** opens the
underlying task graph, approvals, runs, diagnostics and evidence.

![Harmony Companion: Observability and Tasks](docs/images/companion-observability-tasks.png)

*Observability shows recorded measurements. The Tasks panel is an illustrative
example using mock tasks; it is not the release's canonical task state. Quota
percentages are historical screenshot readings, not promised account values.*

![Harmony Companion: actual Inspect view](docs/images/companion-inspect.png)

*Actual v0.1 diagnostics from Harmony's own development task. Historical run
measurements include explicit unknowns; estimated list cost is not an actual charge.*

## Keep context small and decisions durable

**Coordinated models.** Distinct planning, implementation, independent review
and correction roles. Graph dependencies and concurrency bounds support parallel
investigation without unlimited review loops. Model selection and agent launches
remain explicit.

**Lightweight context.** CLM-inspired selective loading, compact checkpoints,
resumable sessions and concise handoffs keep working context focused. A command
output summarizer keeps full evidence while returning a bounded verdict. These
tools can reduce unnecessary context; Harmony does not guarantee token savings.

**Tasks with evidence.** Canonical task IDs survive follow-ups, review and
correction. Automatic Done requires the recorded workflow, required tests,
review, approvals and verified final-action execution. Manual closure is labelled
separately; archive changes visibility only.

**Measured resources.** Exact model identities and requested-model qualifiers,
role attribution, fresh input/output, cache reads/writes and cache reuse stay
separate from account allowance. Unsupported readings remain unknown. Detailed
API-equivalent estimates appear in Inspect, separate from actual charges.

**Human control.** Revision-bound approval works through chat-coordinated CLI
commands or Companion. Request Changes requires durable feedback. Approval
permits an action; the executing agent must perform, verify and record it before
completion. The web backend never commits or publishes. The bundled PR guard
and optional publisher provide additional release controls.

## Install

macOS or Linux, Python **3.10+**, and Git. No frontend build or runtime Python
dependencies. Authenticated Claude Code and OpenAI Codex CLIs are needed for
model runs and their supported allowance readings; the board works without them.

After the public repository is released:

```bash
git clone https://github.com/norman8823/NL-Harmony.git
cd NL-Harmony
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install .
nlh --version
nlh --help
```

For a local release snapshot, begin with `cd /path/to/NL-Harmony` and run the
same virtual-environment and installation commands. Version: **0.1.0**.

## Five-minute Quickstart

With the environment above active, create a disposable example project:

```bash
demo_dir="$(mktemp -d "${TMPDIR:-/tmp}/harmony-demo.XXXXXX")"
cd "$demo_dir"
git init -q
printf '.workflow/\n.direct-runs/\nharmony.json\n' > .gitignore
cat > harmony.json <<'JSON'
{
  "workflow_dir": ".workflow",
  "runs_dir": ".direct-runs",
  "board": {"port": 8765}
}
JSON
nlh companion --no-browser --port 0
```

Open the printed local URL. Choose **Tasks → Add task**, enter **“Write a greeting”**
with details **“Add a tiny greeting function and a focused test.”**, then Save.
The task is Backlog and execution is not authorized. Edit its details, open
Inspect, or try Mark Done and Reopen; neither operation authorizes a release.
The Observability tab stays calm when no measurements exist.

Stop Companion with Ctrl-C. In the same terminal:

```bash
nlh task resolve --task-id 1
nlh inspect 1
D="$(python3 -c 'from pathlib import Path; import harmony; print(Path(harmony.__file__).parent / "direct-workflow")')"
python3 "$D/task_checkpoint.py" update --task-id 1 --phase planning --next-action "Define acceptance criteria and obtain execution authorization"
python3 "$D/task_graph.py" init --task-id 1 --investigators 0 --correction-ceiling 1
python3 "$D/task_graph.py" status --task-id 1
python3 "$D/run_observability.py" summarize-task --task-id 1 --records-dir .direct-runs
```

No model prompt is required for this demo. To run agents, first establish the
task's acceptance criteria and explicit authorization in its canonical ticket.
New graphs allow optional read-only investigators to run alongside implementation,
with a default limit of two running nodes. Review waits for implementation
and all investigator findings; editing, correction, review, and release
remain mutually exclusive. Existing graphs retain their original rules.
The graph records transitions; it does not launch agents. See the
[workflow guide](harmony/direct-workflow/README.md) and
[Companion/approval guide](harmony/task-board/README.md).

## Configure and record

Paths in `harmony.json` are relative to that file. The nearest configuration at
or above the project directory is used; `--project` and `--config` override it.
See [the portable example](examples/harmony.example.json). Role entries are
metadata, not an automatic model router.

When a task is authorized, recorded invocations name its existing ID:

```bash
nlh record --task-id 1 --role planner -- codex exec --json --sandbox read-only -m YOUR_CODEX_MODEL - < plan.md
export HARMONY_PR_GUARD="$D/direct-workflow-pr-guard.py"
nlh record --task-id 1 --role writer -- claude -p "Implement the approved task" --model opus --output-format stream-json --verbose --settings "$D/release-settings.json"
nlh record --task-id 1 --role reviewer -- codex exec --json --sandbox read-only -m YOUR_CODEX_MODEL - < review.md
```

Use your supported Codex model identifier. Opus is recommended for writers and
corrections, and is the default in the example configuration. Any explicit
Claude model is supported with machine-readable output and the packaged
PR guard settings.
Other reported Claude models retain their separate attribution. Resumed runs
use the recorder's `--prior-result` option; session-cumulative totals are not
summed as fresh invocation usage.

Investigators additionally require `--base-commit COMMIT_SHA`. The recorder
uses a temporary detached Git worktree at that exact commit and records the
SHA read. Codex requires an explicit read-only sandbox; Claude requires
explicit write/execution-tool denial, `--restricted`, and isolated settings. See the workflow
guide for the complete checked invocation. Historical records without this
proof remain unknown.

At a human gate, an agent presents the completion summary and verification:

```bash
nlh approval request 1 --action commit --summary "Verified local changes ready to commit" --evidence evidence/verification.md
nlh approval show 1 --json
```

These commands require a completed implementation and passing review graph;
they are not part of the unapproved demo. A human decision names the displayed
request, gate and binding. Commit approval grants no merge or publication. The
agent executes and verifies the approved action, records it with
`nlh approval execute`, then completes any separately authorized workflow gate.

## Measurement limits and privacy

Codex weekly allowance uses its official app-server seven-day rate-limit window.
Claude allowance uses its documented native `/usage` command, about every five
minutes, with one local owner/cache per installation and bounded retry/backoff.
Claude's terminal format is not a versioned JSON API; unsupported output fails
closed. Optional status-line capture preserves an existing command and output.
No direct credential reads, private HTTP polling or quota estimates from tokens.

Turn off background allowance collection with `HARMONY_CODEX_QUOTA=0` and
`HARMONY_CLAUDE_CLI_QUOTA=0`. Claude needs an authenticated trusted CLI and a
pseudo-terminal. Stale readings retain their original timestamp. Codex reports
completion-only token measurements and may supply no resolved model identity.
Subagent attribution appears only when existing counters support it. The running
indicator confirms a managed invocation, not remote model computation.

Keep `.workflow/`, `.direct-runs/`, local configuration, streams, credentials and
evidence out of Git. There is no hosted service or billing engine. For development,
optional DOM tests require Node and jsdom; production does not.

## License

[MIT](LICENSE) · © 2026 NL Intelligence
