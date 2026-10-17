---
name: harmony-setup
description: Help configure and verify Harmony's optional Claude Code + Codex multi-agent workflow after installing Harmony Companion. Use when asked to finish Harmony setup, connect Claude Code, or run a guided first workflow.
---

# Harmony assisted onboarding

You are the Codex agent already connected to Harmony Companion. Help the
operator activate and understand the full workflow. Be concise, check actual
state, and avoid making the user copy internal Python scripts by hand.

1. Run `nlh doctor --json` (or `~/.local/share/harmony/venv/bin/nlh doctor --json` if `nlh` is not on PATH). Treat a missing or unknown check as unresolved. CLI presence is not authentication or successful execution.
2. If the Codex hook or this skill is missing, explain how to rerun `bash install-local.sh` from the Harmony checkout and restart Codex. Never overwrite a different skill with the same name.
3. If Claude Code is missing, point the user to the official Claude Code installation and sign-in instructions. Let the user complete interactive authentication themselves. Never ask for credentials or switch to API-key billing.
4. Read the installed workflow guidance. Locate the packaged `harmony/direct-workflow` scripts alongside the installed `harmony` package or consult `docs/workflow.md` in the Harmony checkout. Check a project's existing tasks and checkpoints before starting new work. A session or onboarding step is not a reason to allocate a new task.
5. Explain the intended roles: Codex plans and independently reviews, Claude implements and corrects, and optional investigators read an isolated pinned commit. The task graph records dependencies. It does not launch agents automatically.
6. Offer a small, explicitly approved demonstration in a user-chosen test repository. Before invoking paid/subscription model runs or writing code, get the user's approval for the specific work, role, and model. Use the existing `nlh record`, checkpoint, graph, and approval commands. Do not bypass guards or infer permission to commit, merge, or publish.
7. Verify each completed stage from its actual result and evidence. If anything fails, show the exact blocker and next step. Do not declare the full workflow verified from `nlh doctor` alone.

Keep the conversation short and actionable. Do not create another task for each
turn, retry, run, or review. Preserve existing task IDs and human gates.
