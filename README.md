# Harmony
🤝

*by NL Intelligence*

**Better Together.**

Harmony is an open-source orchestration and observability toolkit for developers using both Claude Code and OpenAI Codex. It brings planning, implementation, review, task management, and resource visibility together, without adding another hosted platform to manage.

**Less managing. More building.**

![How Harmony works: Codex plans and reviews, Claude implements, and humans approve](docs/images/workflow.png)

## The best of both worlds.

A coding assistant can write a function. Building and maintaining software takes more: a clear plan, the right context, a second set of eyes, and a record of what happened when the session ends.

Harmony gives those steps a shared structure. **Codex plans and independently reviews. Claude implements and addresses the findings.** When extra investigation is useful, bounded read-only agents can work in parallel on a pinned commit, then return evidence to the workflow. One primary writer owns the changes.

The process remains deliberately controlled. Harmony records task state, dependencies, handoffs, and approvals; agents or operators explicitly launch the underlying coding CLIs. It is **not** an autonomous one-prompt development pipeline.

## Built for work that doesn't fit in one conversation

**Pick up where you left off.** Harmony's lightweight, CLM-inspired context system keeps a compact working picture of each task: decisions, constraints, evidence, and what to do next. Durable checkpoints and resumable task graphs help a new session continue without rediscovering the project.

**Spend tokens on progress, not repetition.** Selective context loading, concise agent handoffs, and a command-output summarizer keep noisy logs and unnecessary rereads out of the model's immediate context while preserving the original evidence. Focused checks and bounded correction cycles aim to avoid repeating work. Actual savings depend on the task; Harmony does not promise a particular reduction.

**Keep every task accountable.** Work has a canonical task ID, acceptance criteria, recorded runs, and a verification history. Automatic completion requires the applicable workflow checks and approvals. Manual closure is marked differently, and archived work keeps its evidence.

**See what your models actually did.** Harmony attributes measured tokens to providers, models, roles, and tasks; distinguishes fresh input, cached input, output, and estimated costs; and shows available Claude and Codex allowance readings with their measurement times. Unknown data stays unknown.

**Approve with confidence.** A human can approve, defer, or request specific changes at a bound workflow gate. Permission to commit is not permission to merge or publish, and an approval is not treated as proof that an action succeeded.

## Meet Harmony Companion

One local browser window brings the moving pieces together.

**Tasks** is a persistent Kanban board to create, edit, track, close, reopen, and archive development work. **Observability** shows measured allowance, model/role contributions, cache reuse, and the latest task. Open **Inspect** for the underlying workflow graph, run details, review evidence, and approval history.

![Harmony Companion with Observability and Tasks views](docs/images/companion-observability-tasks.png)

*Harmony Companion brings task management and model observability together: track work on the Kanban board, monitor allowances, and see model-level token usage and cache reuse.*

![Harmony Inspect view showing run-level diagnostics](docs/images/companion-inspect.png)

*Inspect a task to explore its workflow, agent runs, review evidence, approval history, and detailed resource usage.*

## Install and start

You'll need **macOS or Linux**, [Python 3.11+](https://www.python.org/downloads/),
[Git](https://git-scm.com/downloads), and [Codex CLI](https://developers.openai.com/codex/cli/).
Make sure you can open Codex and are signed in before continuing.

### Work from any repository

Set up Harmony once, then use Codex in any project on the same computer.
Follow these steps in order.

**1. Download Harmony**

Open a terminal and paste:

```bash
git clone https://github.com/norman8823/nl-harmony.git
cd nl-harmony
```

**2. Install it**

In that same terminal, run:

```bash
bash install-local.sh
```

Wait for the installer to finish.

**3. Open Harmony**

Still in the same terminal, paste:

```bash
~/.local/share/harmony/venv/bin/nlh companion
```

A browser window opens. Click **Tasks** to see your board. Leave this terminal running.

**4. Open Codex**

Open another terminal in the project you want to work on, then run:

```bash
codex
```

If Codex was already open during installation, close it and open it again.

On the first launch, Codex may show **Hooks need review**. Choose **Review hooks**,
check the **Connecting to Harmony** entry, and approve it. If Harmony is the only
hook listed, **Trust all and continue** also works. Review any other hooks before
trusting them: hooks can run commands outside Codex's sandbox.

This is normally a one-time approval. Ordinary restarts should remember it.

**5. Try your first task**

Type this into Codex:

> Create hello.txt containing "Hello from Harmony", then read it back to check it.

Watch the **Tasks** board. One card should appear, move to **In Progress**, then
**Done** with **Completed by Codex**. Open **Inspect** to see the completion summary.
For a small task, In Progress may only appear briefly.

Keep working normally in Codex. Follow-up messages about the same task reuse its
card. If Codex needs your input, the card moves to **Blocked** until work resumes.
Keep Companion running while you work.

<details>
<summary>Updating or restarting Harmony</summary>

To update, open a terminal in your existing Harmony folder and run:

```bash
git pull --ff-only origin main
bash install-local.sh
```

Stop Companion with **Ctrl+C**, then start it again using the command in step 3
from the same Harmony folder. This keeps your existing cards. Restart Codex too.
Unchanged hook settings keep their approval; changed settings may need review again.

</details>

Need help? See [Troubleshooting](#troubleshooting).

## How the workflow runs

The rest of this guide is optional. Use it when you want the full Claude and
Codex workflow, with recorded runs, independent review, and approval gates.

Harmony supplies durable records and checked commands; your agents or scripts invoke them. A typical task moves through **plan → implement → independent review → targeted correction (if needed) → human decision → verified action**.

**1. Establish the task and its context.** Define acceptance criteria and obtain execution authorization in the canonical ticket. The checkpoint keeps a compact handoff; the graph tracks allowed phases and bounded parallel investigation.

```bash
D="$(python3 -c 'from pathlib import Path; import harmony; print(Path(harmony.__file__).parent / "direct-workflow")')"

python3 "$D/task_checkpoint.py" update --task-id 1 \
  --phase planning --next-action "Define acceptance criteria"
python3 "$D/task_graph.py" init --task-id 1 \
  --investigators 0 --correction-ceiling 1
python3 "$D/task_graph.py" status --task-id 1
```

**2. Record work by role.** Once a task is authorized, `nlh record` captures machine-readable execution and attributes the result to its existing task ID. The commands below illustrate the explicit handoffs; prepare the referenced prompt files and use your supported model identifiers.

```bash
nlh record --task-id 1 --role planner -- \
  codex exec --json --sandbox read-only -m YOUR_CODEX_MODEL - < plan.md

export HARMONY_PR_GUARD="$D/direct-workflow-pr-guard.py"
nlh record --task-id 1 --role writer -- \
  claude -p "Implement the approved task" --model opus \
  --output-format stream-json --verbose \
  --settings "$D/release-settings.json"

nlh record --task-id 1 --role reviewer -- \
  codex exec --json --sandbox read-only -m YOUR_CODEX_MODEL - < review.md
```

Corrections return only the named review findings to the writer; the reviewer checks the resulting delta. Optional investigators use a pinned `--base-commit` and read-only isolation. Their findings must be recorded before review can start. The graph constrains execution; **it does not automatically spawn agents or choose models**.

**3. Request a decision, then prove the outcome.** When the required graph steps and review pass, the executing agent can request approval for a *specific* final action:

```bash
nlh approval request 1 --action commit \
  --summary "Verified local changes ready to commit" \
  --evidence evidence/verification.md
nlh approval show 1 --json
```

The human approves, requests changes with feedback, or defers. If approved, the agent must perform the action, verify it, and record evidence using `nlh approval execute`. Commit, merge, and publish are separate permissions; the Companion backend performs none of them itself.

For more on CLI options, use `nlh --help`, `nlh record --help`, and the in-repository [workflow](harmony/direct-workflow/README.md) and [Companion/approval](harmony/task-board/README.md) guides.

## What's measured (and what isn't)

Harmony keeps **per-invocation token totals** separate from session-cumulative data so runs aren't double-counted. It records model identities when reported, qualifies a Codex model when only the requested name is known, and treats unreported values as unknown rather than zero. Cache reuse describes measured input, **not money saved**. API-equivalent estimates are not subscription charges.

Weekly allowance comes from different provider interfaces: Codex's app-server seven-day rate limits and Claude Code's authenticated native `/usage` view, checked locally about every five minutes. Claude's terminal display is not a versioned API, so parsing may fail; stale readings keep their original timestamps. Disable collection with `HARMONY_CODEX_QUOTA=0` or `HARMONY_CLAUDE_CLI_QUOTA=0` if needed.

Task state, checkpoints, approval evidence, and run records stay in local `.workflow/` and `.direct-runs/` directories. Keep those paths, local configuration, and credentials out of Git. Harmony has no required cloud dashboard or billing service.

**Harmony is a toolkit for coordinating the agents you already use.**

**Better Together.**

## Troubleshooting

- **The board does not open:** use the local address printed in the Companion terminal.
- **The address is already in use:** Companion may already be running. Stop the existing
  Companion with Ctrl+C in its terminal before starting another.
- **No card appears:** check that Companion is running, restart Codex, and make sure
  you approved Connecting to Harmony. Skipping approval leaves the hook inactive.

Companion and Codex CLI must run on the same computer. Codex cloud tasks on a
separate machine do not automatically appear on your local board.

For more commands, see the [CLI reference](docs/cli-reference.md).

## Questions?

[Read the FAQ](docs/faq.md)

## License

[MIT](LICENSE) · © 2026 NL Intelligence

### About NL Intelligence

NL Intelligence is an independent applied AI lab building practical tools and solutions to solve real-world problems.

Harmony is an open-source project by NL Intelligence.
