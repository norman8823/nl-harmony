# Development workflow

Use this existing checkout for development. Do not create another worktree unless
the user requests one. Public releases are separate, explicit work; follow the
repository's release documentation and never push private history to a public repository.

## Connect work to Harmony

When the global Harmony prompt hook supplies a verified active Companion, use
its selected board_instance and the harmony_local MCP list_tasks/start_task tools
for intake, even if the user's work is in another repository. These tools call
the existing Companion outside the agent's filesystem sandbox; do not request
cross-repository file-write access or create a second local ticket store just because
the terminal's current directory differs. The instructions below are the fallback
when no global connection is configured (including isolated cloud tasks).

With the local tools, keep the same card synchronized using `update_task`:
`In Progress` when work begins, `Blocked` with an actionable `next_action` when
waiting on human input or a dependency, and `Next` only for queued work.
Resume the same card when the blocker clears. Call `complete_task` with a factual
summary when the requested outcome is complete and checks pass, before reporting
completion. Ending a turn or run is not completion. Use the latest returned
revision; refresh and reassess on conflicts. Declared approval and workflow gates
remain enforced; completion is labeled Completed by Codex with the summary.

At the start of a Codex cloud task **or terminal session**, and before beginning
a new problem in an existing conversation, connect the work to a Harmony card:

1. A task is the problem being solved, not a prompt, turn, session or recorded
   run. Continue using the task ID already established in this conversation or
   `HARMONY_TASK_ID`. Multiple turns and runs must keep that same card.
2. If no task ID is known, run
   `.venv/bin/nlh --project . task list --all --json` and inspect the tickets'
   scope and existing checkpoints/evidence. Reuse the task for the same problem,
   including when continuing in a different Codex thread. Do not equate a similar
   title with the same task. If several tasks plausibly match, ask which to
   continue instead of allocating another card.
3. Link an existing card with
   `.venv/bin/nlh --project . task start --task-id ID --source codex --json`.
   An explicit task ID takes precedence over inherited thread context. To link
   an otherwise unlinked Codex thread, also pass `--external-id "$CODEX_THREAD_ID"`
   when that variable is set. Do not rebind a thread that already belongs to a
   different problem; keep using the selected task ID instead.
   Repeat calls are safe. Preserve the returned `task_id` for later turns and
   pass it to `nlh record --task-id ID` and other task operations.
4. Only when this is a new problem with no existing card, run
   `.venv/bin/nlh --project . task start --title "Concise problem title" --details "User goal and scope" --source codex --json`.
   If the conversation is already linked to a different problem, use `--new` to
   ignore inherited task/thread IDs. Retain the returned task ID thereafter.

If `.venv/bin/nlh` is unavailable, use `python3 -m harmony.cli` with the same
arguments from this checkout. Do not invent thread IDs, silently overwrite a
conflicting identity, or create a card on every follow-up prompt. An unavailable
board service does not block this CLI: it writes the canonical local ticket
store directly. Report any store error before proceeding without a card.

New cards are BACKLOG and do not grant execution, release or approval permission.
Keep operational state in ignored `.workflow/` and `.direct-runs/` directories.
