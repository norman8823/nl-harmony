# First principles for the direct agent workflow

1. **Start from the task’s acceptance criteria and current state.** Load the checkpoint, graph, and pointed evidence before broad repository history. Read more only when a specific question requires it.

2. **Give each model a distinct job.** Use one implementation writer and one independent, targeted reviewer by default. Send material findings back to the same writer. Re-review only when the correction materially changes code, architecture, security boundaries, or acceptance criteria.

3. **Move only the context you need.** Prefer named files, bounded ranges, focused diffs, and stable evidence references. Do not repeatedly reload large outputs, evidence bundles, or repository history that has already been established.

4. **Run the smallest check that can falsify the change.** Expand to relevant regressions once the package is stable. Reuse passing checks when their inputs have not changed.

5. **Build the smallest thing that satisfies the task.** Reuse existing behavior before adding code, files, abstractions, dependencies, workflow steps, or UI. Defer attractive extras that are not required by the acceptance criteria.

6. **Do not optimize away safety.** Minimalism never overrides correctness, security, provenance, data-loss protection, independent review where warranted, or explicit human release gates.

7. **Treat usage as evidence, not a quota conversion.** Report provider counters, cache use, run scope, and unknowns separately. Processed-token counts, list-price estimates, and cache metrics do not establish subscription quota consumed or actual charges.

## Efficient coordination without weaker gates

These rules lower coordination and context overhead. They never stop authorized work: unfinished authorized work continues, and there are no token ceilings, usage quotas or usage-triggered stops. Subscription resets and API credits are lifelines an operator may choose; nothing here changes billing or authentication automatically.

- **Let software handle routine progress.** Waiting, status, and the next step of a known sequence belong to deterministic tools (checkpoint, graph, recorder, status line). Cheap tool polling is fine; repeated model turns that reinterpret unchanged progress are not.
- **Assign substantial, bounded work.** A writer assignment names the acceptance criteria, the files in scope, the invariants to preserve, and the checks that prove it. One well-specified assignment beats many small prompts that each reload context.
- **Return compact completion packets.** A finished run reports conclusions, check commands and results, open questions, and evidence references (`file:line`, record IDs, commit) — not raw logs or transcripts.
- **Do not repeat investigation; do verify what matters.** Reuse established findings with their provenance. The reviewer still independently checks consequential claims rather than accepting the writer’s account.
- **Keep review stable and targeted.** Use one independent review with consolidated finding IDs. Corrections address only those IDs; the follow-up review covers the correction delta and the invariants it could affect, not the whole package again.
- **Bound context deliberately.** Read named ranges and focused diffs. Reuse passing checks when their inputs have not changed. Continue a session when its context is still relevant and correct; start a fresh one when the task or role changes or the context is stale or noisy — as a choice, not by default.
- **Use ordinary model and reasoning settings.** Escalate model or reasoning effort only for concrete complexity (an unresolved failure, a security boundary, a subtle invariant), and say why.

None of this relaxes correctness, independent review where warranted, or human release gates. A human still approves and merges.

**Comparing future tasks.** Compare only comparable tasks: similar scope, model/provider, role boundaries, and session continuation. For each, record acceptance-criteria completion, verification commands and results, review and correction rounds, and provider counters separated by role (coordinator, writer, reviewer, correction; a mixed bucket when boundaries cannot be split), keeping fresh, cache-write, cached, output and reasoning-output counters distinct (reasoning output is a subset of output where the provider defines it that way; report it separately, never add it twice). A neutral result is valid. Make no savings claim without comparable evidence, and never convert token totals into quota or dollar savings (principle 7).
