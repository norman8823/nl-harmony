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

[← Back to Harmony](../README.md)
