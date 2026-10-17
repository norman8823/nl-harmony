# Claude and Codex workflow

This guide is optional. For assisted setup, open Codex and run
`$harmony-setup`. The skill checks the environment and guides your first
Claude + Codex workflow. This page is the manual reference for recorded runs,
independent review, and approval gates. Basic Codex task tracking only needs
the [README setup](../README.md#install-and-start).

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

For more on CLI options, use `nlh --help`, `nlh record --help`, and the in-repository [workflow](../harmony/direct-workflow/README.md) and [Companion/approval](../harmony/task-board/README.md) guides.

[← Back to Harmony](../README.md)
