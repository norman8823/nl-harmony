# Frequently Asked Questions

### I'm already building with AI. How will Harmony improve my workflow?

Harmony helps you coordinate Claude Code and Codex without constantly switching between agents, repeating instructions, or reconstructing context.

You get persistent tasks, compact working memory, structured reviews, and a dashboard showing what your models are doing.

Less coordination. More building.

### What makes Harmony different from other multi-agent tools?

Harmony isn't trying to launch the largest swarm of agents.

It combines complementary model roles, lightweight context management, persistent task tracking, independent verification, and resource observability in one toolkit.

The focus is on making AI-assisted development more organized, efficient, and accountable.

### Why can't I just use AGENTS.md and OpenRouter?

For simple projects, you absolutely can.

Markdown files describe how agents should behave. OpenRouter provides access to different models. Neither alone manages the full development process.

Harmony adds persistent execution state, context handoffs, task-level resource tracking, review evidence, and approval gates.

**Instructions define the workflow. Harmony helps manage and measure it.**

### Isn't this just another layer of abstraction over tools that already work?

For a quick bug fix, you probably don't need Harmony.

Its value becomes more apparent when projects span multiple agents, tasks, sessions, and review cycles. That's when context gets lost, work gets repeated, and coordination becomes increasingly time-consuming.

Harmony provides structure without requiring a separate cloud orchestration platform.

### Can I switch models or change their roles?

**Partially.** Harmony supports different Claude and Codex models through explicit configuration.

Planning, review, and investigation support model selection within the current execution rules. However, v0.1 reserves implementation and correction for Claude Code.

More flexible role assignments are a natural direction for future versions.

### Is Harmony compatible with other AI providers or open-source models?

**Not yet.**

Harmony currently supports Claude Code and OpenAI Codex. Its underlying task graphs, checkpoints, and context-management mechanisms are not inherently tied to those providers.

Support for other coding agents, OpenRouter, and locally hosted models is something we'd like to explore as Harmony evolves.

### Can I use Harmony with Codex in another repository?

Yes. Install the local Codex connection once and keep Companion running. Codex
terminals in any repository on the same computer can create or continue a card
and update its stage. Follow-up prompts for the same problem reuse that card.
See the [local setup and first-launch hook review](../README.md#work-from-any-repository).

### Does Companion check completed Codex work again?

No. Codex reports completion with a summary, and the card moves to Done with
**Completed by Codex**. Formal workflow tasks retain their explicitly declared
checks and approval gates; ordinary CLI cards do not require a second Companion review.

### Do I need hosting or another account for task tracking?

No. This integration uses your local Codex CLI and a running Companion on the
same computer. Codex cloud tasks on a separate machine do not automatically sync
to that local board.

### What does Harmony measure?

When supported run records are available, Harmony shows token usage, reported
model names, roles, and cache reuse. It keeps each invocation's totals separate
from session totals so runs are not counted twice. Creating a task card alone
does not guarantee that token measurements are available.

If only the requested model name is known, Harmony labels it accordingly.
Missing measurements stay **unknown**, rather than appearing as zero.

### Are the cost estimates my actual bill?

No. API-equivalent estimates use list pricing to describe recorded usage. They
are not subscription charges or a billing statement. Cache reuse describes
measured input; it is not a promise of money saved.

### Where do weekly allowance readings come from?

Codex readings come from its app-server seven-day rate limits. Claude Code
readings come from its authenticated native `/usage` view. Harmony checks
locally about every five minutes.

Claude's terminal display is not a versioned API, so a reading may fail to parse.
Stale readings keep their original timestamps; unknown readings stay unknown.
Both providers need their own authenticated CLIs for supported readings.

To disable collection, start Companion with `HARMONY_CODEX_QUOTA=0` or
`HARMONY_CLAUDE_CLI_QUOTA=0` in its environment. For setup and troubleshooting,
see the [Companion guide](../harmony/task-board/README.md#weekly-allowance).

### Where are my tasks and run records stored?

Task state, checkpoints, and approval evidence stay in the board folder's local
`.workflow/` directory. Run records stay in `.direct-runs/`. Keep those paths,
local configuration, and credentials out of Git. Harmony does not require a
cloud dashboard or billing service.

For common setup issues, see [Troubleshooting](troubleshooting.md). For the
optional recorded Claude and Codex workflow, see the [workflow guide](workflow.md).

[← Back to Harmony](../README.md)
