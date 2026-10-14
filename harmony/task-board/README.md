# Task board

The companion page: **Observability** and **Tasks**, a small local Kanban-style
board of the Tasks in `<workflow-dir>/tickets/<id>.json`.
Python standard library only — no npm, no framework, no build step. It shows
work and records planning metadata; it never executes, authorizes or routes
anything.


## Run it

```bash
python3 task-board/server.py [--repo <repo>] [--workflow-dir <dir>] [--runs-dir <dir>] [--port 8765]
```

Then open **http://127.0.0.1:8765**. Loopback only; any other bind address is
refused. Defaults: `--repo` is the directory above `task-board/`,
`--workflow-dir` is `<repo>/.workflow`, `--runs-dir` is
`$DIRECT_RUN_RECORDS_DIR`, else `<repo>/.direct-runs`. An empty or missing
tickets directory gives an empty board; nothing is seeded.

## Where the data lives

The ticket files are the single source of truth; there is no second task list
or tracking file. Under one lock (`tickets/.task-board.lock`) and one revision
counter (`board.revision`), the board writes back only:

- `board` — stage, owner, next action, gate, display title, verification,
  board notes, links, linked defects, priority, revision;
- the canonical **title** (the first title is kept as
  `lifecycle.original_title`), **details** (`scope.summary`) and **status**;
- `lifecycle` — origin, the current closure record and completion-cycle
  markers — and an appended `history` audit list (time, actor, action,
  revision, canonical before/after values, names of changed board fields).

It never writes `authorization`, `blocker`, `acceptance_criteria`, `routing`,
`review`, `approvals`, `required_approvals`, `gates`, `evidence_refs` or `id`,
and keeps unknown fields as they are. A payload naming a protected field is
refused with HTTP 403, any other unknown field with 400, and every check runs
before the single atomic write, so a refused request changes nothing. Every
write carries the revision the editor loaded; a stale write is refused with
HTTP 409. A ticket whose `history`, `lifecycle` or `scope` has a shape the
board does not understand keeps it: canonical edits to it are refused with 409
(board-only edits still work).

**Details vs board notes.** *Details* is the ticket's canonical
`scope.summary`; *Board notes* are board-only planning text in `board.notes`.

A stage is a visibility label, not permission to act, and moving a card is
never a review or an approval. The Done lane always matches the canonical
status: a DONE ticket shows in Done (whatever lane it was last stored in), a
non-DONE ticket never does, choosing Done in *Move* is *Mark Done*, and a DONE
card leaves Done only through *Reopen*.

## Task lifecycle

- **Add task** creates a task without any external intake. When this repo
  has `agent-orchestrator/intake.py`, its durable allocator is used as before
  (unassessed ⇒ BLOCKED at intake, `create_mode: "intake"`). Otherwise
  (`create_mode: "canonical"`) the store allocates the next numeric id above
  every id seen in `tickets/` (archives such as `12.closeout.json` included),
  `graphs/` and `checkpoints/`, under the lock, and creates the file
  exclusively (write a temporary file, then hard-link it into place), so an
  existing ticket is never replaced and an id is never reused. The ticket is
  `BACKLOG`, `authorization.granted: false`, no blocker, no acceptance
  criteria and no review. There is no delete.
- **Edit** (`PATCH /api/tasks/<id>`) changes title, details and status along
  with board fields; the editor sends a canonical field only when it changed.
  Editing the title clears an intake display title so the card shows the
  canonical title.
- **Mark Done** (`POST /api/tasks/<id>/done`, or status DONE in the editor)
  is an administrative closure only. It is labelled *Manual closure* on the
  card, in Inspect and in `lifecycle.closure`; it records no review, approval,
  authorization or release, and leaves the blocker, any human release gate,
  review and the task graph untouched — the graph's release stays blocked or
  pending.
- **Reopen** (`POST /api/tasks/<id>/reopen`) returns a DONE task to `READY`
  when it is explicitly authorized, unblocked and has acceptance criteria,
  else to `BACKLOG` (or the BACKLOG/READY status asked for). It grants
  nothing and does not touch the graph; the closure moves into `history`.
- **Automatic Done.** Reading the board (`/api/board`, `/api/observability`)
  makes one explicit check per task — no background service. A task is closed
  automatically only when its graph (validated by
  `direct-workflow/task_graph.py`) has every node completed or skipped, the
  last completed review passed, the release node completed where the graph
  has one, `authorization.granted` and (where release is required)
  `authorization.release_granted` are `true`, there is no blocker, and every
  declared `required_approvals` / `approvals` / `gates` entry and any
  ticket-level `review` is positively approved, and every declared commit,
  merge or publish approval has verified successful execution recorded on its
  approved request (see Approvals). No graph, an invalid graph,
  no passed review, missing release approval or an unreadable gate keep it
  open; each card's `auto_done.reasons` says why. The closure is recorded with
  the graph's completion cycle (creation time + latest node completion; a
  `link` does not count as a new cycle). Sync only ever moves a task to DONE,
  so a manual closure is never reopened by it, and a task reopened after a
  completion is not closed again until the graph completes a new cycle.

- **Declared testing.** A ticket with `testing_required: true` also needs a
  positively passed `tests` gate; a missing or failed one keeps it open. An
  agent's own text (a summary, a "done" claim) and the board's record check
  never close anything.
- **Activity.** On a board read, a run whose recorder proof holds
  (`run_observability` `active.confirmed`: the live recorder process for this
  task and role) moves its task to In Progress through
  `Board.sync_activity`: BACKLOG/READY become IMPLEMENTING (INVESTIGATING for
  planner/investigator, REVIEW for reviewer), an active status is kept, and
  `history` gets an `activity_start` entry naming the run. Only once per run,
  and never for a DONE task, an unauthorized one, one with a blocker, status
  BLOCKED, a card held in Blocked or a pending approval request. An archived
  task that was explicitly reopened moves like any other (it stays archived).
  An unconfirmed, interrupted or other task's run moves nothing.
- **Archive** (`POST /api/tasks/<id>/archive`, `.../restore`,
  `nlh task archive|restore <id>`) hides a DONE task in *Archived* (Tasks →
  *Archived (n)*) and shows it again. *Archive completed*
  (`POST /api/tasks/archive` with `{tasks: [{id, expected_revision}]}`)
  archives every completed card on the board, all or none: one open, stale,
  unknown or repeated task refuses the whole request before anything is
  written. Visibility only — `board.archived` on the same ticket, same path
  and id; history, evidence, approvals, graph and Inspect stay; nothing is
  archived automatically, deleted or renumbered. Archive never freezes Reopen,
  Edit or activity; the flag stays across status changes until a human
  Restores the card, and Restore is always offered on an archived card. An
  Archive/Restore alone does not make a pending approval stale.

No lifecycle action starts a worker, runs a command, merges or publishes.
**Inspect** on a task without run records shows its stored status, details,
closure, completion check, original title, follow-up relations and history.

## Card display

Lanes: *To Do* (the stored stage stays `Next`), *In Progress* (blue),
*Blocked* (amber), *Done* (green). Badges are compact; the full text is in the
tooltip and Inspect:

- **Completion** (DONE only), judged again from the workflow records with the
  automatic-completion rules: *Verified complete* for an automatic closure the
  records still support; *Closed by graph · records changed*; *Closed
  manually · Unverified* (or *· Verified* only when the workflow records fully
  hold, never from the record checkbox); *Done as recorded · Unverified*. Every
  DONE card hides its old next action, gate and settled approval badges (not
  deleted; Inspect keeps them). A compact *release / commit / merge / publish
  not authorized* badge shows while the current canonical records still lack
  that authorization. Mark Done never grants release, commit, merge, deploy or
  publication.
- **Record checked / Record unchecked** is the editable `board.verification`
  (`verified` / `unknown`): a person checked this card against its ticket
  record and evidence. It is not testing or software verification; the
  *Record checked first* / *Only record checked* filters use it as before.
- **Approval** badges are separate from completion and the record check.
  An intake warning shows as *unassessed at intake*.

## Approvals (conversation bridge)

An agent that reaches a human gate asks for a decision on one explicit,
existing task:

```bash
nlh approval request 9 --action workflow_complete --summary "..." \
    --evidence .workflow/evidence/x/report.md [--resolves-blocker]
```

The command records a request in the ticket's `approval_requests` (with a
`history` entry, under the board lock and revision) and prints an offer —
task, gate, typed action, request id and binding — with the exact
**Approve / Request Changes / Defer** commands. The same request shows in
**Inspect** with the same three buttons. Both call one store implementation
(`Board.request_approval` / `Board.decide`); the companion endpoints are
`POST /api/tasks/<id>/approval/request` and `.../approval/decide`, behind the
same Host/Origin guard as every write.

- **Typed scopes.** `task_close` approves the existing manual closure (no
  release). `workflow_complete` completes the graph's *ready* release gate (all
  other nodes finished, a passing review, no invalid skip, execution granted,
  every other declared approval positive; a required `workflow_complete`
  approval is judged as the one being supplied, not as missing) through the shared `task_graph`
  transitions, records `authorization.release_granted` and
  `approvals.workflow_complete`, clears the ticket blocker only when the request
  named that exact blocker (`--resolves-blocker`), and lets the existing
  completion sync close the task. The grant is bound to the approved task's
  substantive content (without bookkeeping, status/closure or the approval
  records themselves) and to the graph cycle it completed: after a reopen with
  changed content or a new graph cycle, automatic Done needs a new approval
  (history keeps the old one). Without a managed workflow approval, an
  explicit legacy `release_granted` still counts. `commit`, `merge` and `publish` record only
  `authorization.commit_granted` / `merge_granted` / `publish_granted` (merge
  and publish need an approved commit) and return the authorized next action
  for the existing agent to perform with its ordinary tools; the record runs
  no git command and never sets `release_granted`. Commit, merge and publish
  are separate scopes: one never implies another.
- **Execute → verify → record → complete.** An approval is permission, never
  evidence that anything happened. Right after an approved commit, merge or
  publish, the existing agent performs exactly the bound action with its
  ordinary tools (the web backend runs nothing), verifies it, and records the
  result on that exact approved request:

  ```bash
  nlh approval execute 9 --request 9-commit-3 --action commit --binding <token> \
      --succeeded --result-ref <commit-sha> --evidence .workflow/evidence/x/git-log.txt
  # or --failed --note "what failed"
  ```

  `Board.record_execution` writes `approval_requests[i].execution` and a
  `history` entry under the board lock. It refuses a request that is not this
  task's, not approved (pending, superseded, deferred, changes requested), not
  the current `approvals.<action>`, a mismatched action or binding, task
  content changed since approval, or an action already recorded as succeeded;
  a success needs a result reference (a hex SHA for commit) and evidence. A
  failure keeps the approval for the same content — retry and record again;
  nobody is asked twice. An approved commit/merge/publish is bound to the task
  content and the work cycle (the task graph's creation time, so the expected
  release-gate transition keeps it; a rebuilt graph starts a new cycle).
  Every declared commit/merge/publish approval keeps the task open until the
  whole chain holds: approval, its approved request and the execution name the
  same task, action and binding, the decision approved that binding, content
  and work cycle are current, and the success names its result and evidence.
  Missing, failed, incomplete, unknown or earlier-cycle evidence is never
  success, and a new approval never inherits old evidence. A workflow approval
  may be recorded first; the success record then runs the existing completion
  sync, so the task closes without another human approval when every other
  condition holds. While execution is pending, the card's next action is the
  `nlh approval execute` command.
- **Legacy approvals are not grandfathered.** A commit/merge/publish approval
  with no approved request (a plain `"approved"` or a dict without
  `request_id`) never counts as executed, and a managed approval recorded
  before content/work-cycle binding (no `scope_sha`/`cycle`) cannot be shown
  current, so it needs a fresh approval. Migrate it with a current
  `nlh approval request` + decision for that action, then perform, verify and
  `nlh approval execute`. Explicit administrative **Mark Done** stays available.
- **Binding.** A request is bound to the task revision, a digest of the
  ticket's gate-relevant content and the graph's revision and content
  (`t<rev>.<ticket>-g<rev>.<graph>`; an identifier, not a secret). A decision
  must name the pending request, task, gate, action and binding exactly.
  Stale (an intervening substantive or board edit; visibility-only Archive/Restore
  preserves the binding), ambiguous, decided, wrong-task, wrong-gate,
  wrong-action and forged-field decisions are refused; every check —
  including the gate's prerequisites for Approve — runs before anything is
  written. Lock order is board lock, then the task's graph lock; the ticket is
  written before the graph, so a failed graph write leaves the task open.
- **Request Changes / Defer** record the decision and an actionable next step
  and grant, close or run nothing. **Request Changes needs feedback**: 1–1000
  characters after trimming (`--note` on the CLI); an empty, whitespace-only
  or longer note is refused by `Board.decide` itself, so neither the CLI nor
  HTTP can bypass it, and nothing is written. The feedback is stored as the
  decision's `note` (request and `history`). In Inspect, Approve and Defer
  decide at once; Request Changes first opens a labelled, required feedback
  field with *Submit changes* / *Cancel* (no request is sent until a valid
  submit). Drafts are kept per request and binding across the 3 s refresh
  and dropped once the request is decided, stale or cancelled; buttons are
  disabled while a decision is in flight. The result shows *Approved*,
  *Changes requested* or *Deferred* with the exact next step
  (`role="status"`; errors `role="alert"`). A decided request keeps a durable
  explanation on every reload — who decided, why it has no buttons, the
  feedback, and its stored `next_step` (served on each card's decided
  requests); an earlier Request Changes recorded without feedback says *No
  feedback was supplied with this earlier decision* and nothing is invented.
  An approved commit's next step says it permits that one commit only (merge
  and publish need their own approval). Asking again for the same bound request is
  a no-op; a changed or stale pending request is marked `superseded` (kept) and
  reissued. Requests and decisions are never deleted. Nothing is ever approved
  automatically or inferred from Done, a move or another action.

## Task identity

New tasks are created only explicitly (**Add task** / `POST /api/tasks`).
Runs, reviews, corrections, handoffs and approvals continue an existing task
by its explicit id: `nlh record` refuses a numeric id with no ticket (a plain
non-numeric id stays a legacy standalone record) and unsafe ids;
`nlh approval` refuses missing, unsafe or unknown ids. `nlh task resolve
[--task-id ID]` (or `HARMONY_TASK_ID`) confirms an id and, with none, lists
open tasks and asks — nothing is guessed or text-matched. `nlh task relate 8
--follow-up-of 6` records the relation on the later ticket only; the earlier
card shows *Followed up by* read-only.

## Card metadata fallback

When a card's board *next action*, *gate* or *owner* is unknown, the card
shows (read only, source in the tooltip) the pending or decided approval
request, the ticket blocker, the checkpoint handoff plan or the graph's next
step; a missing owner shows *Unassigned*. Nothing is written back, the editor
still edits the stored board fields, and no status is called approved without a
recorded decision.

Links are either an `http(s)` URL (a real anchor) or a repository-relative path
shown as copyable text. The board is not a file server.

## Card run line

A card may show one compact line, e.g. `15.2M tok · 17m 10s`: the task's
total tokens (processed input + output) and its duration over **completed**
runs. An active (not `DONE`) card with run records also lists each running
Harmony-managed run whose recorder proof holds (`active.confirmed`) as
`▶ <role> <elapsed>`; a live pid alone claims nothing (task phases show such a
run as `running (unconfirmed)`). A running run's provisional
tokens are never added. It comes from `direct-workflow/run_observability.py`
(`summarize_task_dir` → `card_metrics`) — the same calculation as
`run_observability.py summarize-task`; the server computes nothing itself.
Run records are matched by `task_id` equal to the card's numeric id. Tokens
are the sum of each run's per-invocation usage (session-cumulative counters
are never added) and appear only when every completed run reported input and
output; duration only when every run has one. An unknown metric is omitted;
no run count, separate in/out or cost is shown. Cards with no matching
records show no line; an interrupted run (recorder process gone, no final
record) is not shown as running.

## Observability tab

The page opens on **Observability**; **Tasks** (the board, at `#kanban`) is
the second and only other tab. `GET /api/observability` (read-only) returns, per task
with run records, `run_observability.task_observability`: the task summary
with attribution by provider/model and role, running snapshots (context,
cumulative tokens, time series, last measured time, phase), and the task
graph's phases with their linked runs. The page polls it every 3 s while the
tab is visible.

The tab is one calm panel with three parts, in this order (the current task's
parts appear only when it has them):

1. **Weekly allowance left** — two cards, Codex (Planner / Reviewer) and
   Claude (Implementer / Correction), from `GET /api/quota` (read-only,
   account-wide, independent of any task's tokens; see *Weekly allowance*
   below), fetched at page load on any tab and then on every 3 s *Live* tick
   (a cheap local read, so a new Claude status-line snapshot shows promptly;
   Codex is read by the server once a minute and Claude about every five minutes). Each card
   is in one state: *Checking weekly allowance…* (first check not finished);
   *Weekly allowance unavailable · reason* (no reading: CLI missing, not
   signed in, no status-line capture, or the page could not reach the server —
   a 404 says the running companion predates this and should be restarted);
   **fresh** — `100 − used %`, a bar, *Resets in …* when known, *Measured*
   time; **stale** — the last measured value, dimmed, with *Last measured … ·
   not refreshed: reason* (a failed refresh or fetch, or a reading older than
   two minutes for Codex or ten minutes for the native Claude collector);
   **expired** — its reset time passed: no bar, *a new
   reading is needed*, and the last measured time.
2. **Current task** — chosen in order: the task of the newest run proven
   active (below; a live pid alone never selects); an In Progress board card (newest recorded
   activity, then higher id), even one without run records; the task with the
   newest recorded run. One phase line: *Review complete · Awaiting approval*
   only when the graph's last completed review passed and release is blocked
   or pending with nothing running; else the running, failed or blocked
   phase, else the next one neither completed nor skipped; the ticket status
   only without a graph. *Inspect ›* opens the full report.
   Below it, one *Running · <provider> <role> · <elapsed>* line per provider
   and role, only for a run proven active: its pid on this host executes the
   recorder itself — argv starts with a Python interpreter running
   `direct_run_recorder.py run`, `-m direct_run_recorder run` or
   `-m harmony.cli record`, or with `nlh record` — for this task and role
   (and run id when given), started no later than the record. A bare provider
   CLI, the quota reader, Python `-c` code or another script carrying recorder
   words, a dead or foreign-host recorder, or a reused pid never shows.
3. **Token usage** — one compact row per provider, model and role over the
   task's completed runs (`attribution.by_provider_model_role`): the readable
   model name with the exact id the provider reported (*requested; not
   reported* when it reported none, as Codex exec; *not split* for a mixed
   set), the provider's input + output, and *fresh in · cache read · cache
   write · out · N% cached input*. Only per-invocation usage is summed
   (session-cumulative `modelUsage` never is); a Claude run with several
   models (e.g. a subagent's) splits only when `modelUsage` sums exactly to
   its usage, and there are no per-subagent rows. Codex completion-only
   usage counts: its input is `input_tokens` (uncached + cache reads), and a
   cache write it did not report stays unknown, never zero. Cached share is
   cache reads over that provider's own input, shown only when both are
   known. An unknown part is left out of the row (it stays `null` in the
   data). There is no token chart. Each row has a compact horizontal bar in
   its provider colour: its share of the **recorded processed tokens** (input
   + output) of the task's completed, measured runs — not subscription
   allowance. The denominator is the sum of the known, finite, non-negative
   group totals only; a group without a recorded total gets no bar (never a
   zero one) and the caption names it as excluded; a known total of 0 is a 0%
   bar. With no known total there are no shares. The exact totals are in the
   row tooltip. Below the rows, *Cache reuse: N% of measured input* is the
   summed cache reads over the summed measured input of the groups where both
   are known (weighted, never an average of the row percentages; input is the
   backend's `input_total`, else Claude fresh + cache write + cache read /
   Codex fresh + cache read from known counters), plus the summed cache writes
   only when every counted group measured them; excluded groups are counted.
   It says nothing about money saved or allowance consumed.

Context, runs, per-run tables, list-cost estimates and diagnostics are not on
the overview; they stay in Inspect. The header dot covers every Harmony-managed run: *Running* only for
runs proven active as above, *Cannot confirm* (a running record whose recorder is not proven — pid not
checkable, not inspectable, or not its recorder — never *Idle* and never green then),
*Interrupted*, *Idle*, *Status unknown* when records could not be read, and
*Paused* while *Live* refresh is off.

### Weekly allowance

`direct-workflow/quota_telemetry.py` reads official sources only and keeps
no credential, account or credit field:

- **Codex** — one background worker owned by the companion server reads at
  startup and then every minute, whether or not a page is open; requests
  only return its latest result and never wait for a read. Each read starts
  `codex app-server` (stdio JSON-RPC, no shell, stderr discarded), sends `initialize` /
  `initialized` / `account/rateLimits/read`, applies any
  `account/rateLimits/updated` notifications seen while connected, and stops
  the process (10 s limit, bounded line size and message count); closing the
  server stops the worker and any read in progress. Only the
  general `codex` bucket is used, and only a window with
  `windowDurationMins` exactly 10080; conflicting weekly windows are
  unknown. Missing CLI, not signed in or a timeout reads as unavailable and
  keeps the last valid reading. `HARMONY_CODEX_QUOTA=0` turns this off.
- **Claude (primary)** — every 5 minutes one Companion per installation,
  whichever repository it serves (an flock on `claude-cli-quota.lock`; others
  read the cache `claude-cli-quota.json`, both in an owner-only
  `<tempdir>/harmony-<uid>/.workflow/telemetry/claude-cli-<install hash>/`) starts the unmodified `claude` CLI in
  a screen-reader pseudo-terminal (`--ax-screen-reader`, no tools, no MCP
  servers, hooks and remote control off for that process only) and types the
  built-in `/usage`, nothing else. "Current week (all models)" counts only
  from a pane rendered after the panel's "Refreshing…" marker that ends in its
  completed footer ("Usage credits", then "Esc to cancel") and shows no error; a
  trust or sign-in prompt is never answered (the reading is unavailable). The
  reading is current for 10 minutes; failures keep the last real reading, and
  rate limiting backs off up to an hour. `HARMONY_CLAUDE_CLI_QUOTA=0` turns this off.
- **Claude (fallback)** — Claude Code passes `rate_limits.seven_day` only to an
  interactive session's status line (Pro/Max, after the first response; not
  in `claude -p` runs, so recorded runs alone never provide it). Configure
  your own status line with a quota file inside this project's `.workflow`:

  ```json
  "statusLine": {"type": "command",
    "command": "python3 /path/to/harmony/direct-workflow/statusline.py --quota-file /path/to/project/.workflow/telemetry/claude-quota.json"}
  ```

  To keep an existing status line exactly as it is, wrap it instead: with
  `--capture-only` the script prints nothing itself, keeps the reading, and
  runs the command after `--` with the same stdin, its own output and exit
  status (no shell unless you write `sh -c`; 30 s limit):

  ```json
  "statusLine": {"type": "command",
    "command": "python3 /path/to/harmony/direct-workflow/statusline.py --capture-only --quota-file /path/to/project/.workflow/telemetry/claude-quota.json -- sh -c 'your existing status line command'"}
  ```

  `examples/claude-statusline-capture.example.json` is a portable form of
  this wrapper, with paths relative to the project root and
  `~/.claude/statusline.sh` as the preserved command. Merge its `statusLine`
  key into the project's `.claude/settings.local.json` (ignored by git; keep
  every other key in that file), replace the command after `sh -c` with your
  own if it differs, and start interactive `claude` from the project root so
  the relative paths resolve. Never commit that local settings file.

  The example sets `refreshInterval: 60`, so Claude Code also re-runs the
  status line every 60 s while a session is idle, not only after a response.
  The documented timer re-runs the command; it does not guarantee an
  upstream quota refresh. In a prompt-free resumed session, startup omitted
  `seven_day`, the first timer supplied a real reading, and later timers
  repeated it without a new API response. Identical payloads cannot reveal
  whether the provider checked the value again. The feed carries no
  measurement time of its own, so `measured_at` is the time Harmony first
  observed that exact used percent and reset. A payload with the same used
  percent and reset as the kept snapshot leaves the file untouched; only a
  change writes a new snapshot with a new time. An unchanged reading
  therefore turns *stale* after about two minutes on purpose: identical
  values cannot prove the allowance was measured again, and nothing sends a
  prompt or probe to force one.

  *Optional, every repository.* To capture from interactive sessions in any
  repository into one shared file, use
  `examples/claude-statusline-capture.user.example.json` in your user-level
  `~/.claude/settings.json`. Replace `/path/to/Harmony` with the absolute path
  of your Harmony checkout or installed copy, and the command after `sh -c`
  with your current `statusLine.command` exactly as it is. Merge only the
  `statusLine` key: keep your existing `statusLine` properties (such as
  `padding`) and every other setting, and never replace the file wholesale.
  Get explicit user approval before modifying global settings. Back the
  file up and review the merge before saving; nothing here applies it for you. The snapshot goes to
  `$HOME/.harmony/.workflow/telemetry/claude-quota.json`; start the companion
  with the same file:

  ```sh
  HARMONY_CLAUDE_QUOTA_FILE="$HOME/.harmony/.workflow/telemetry/claude-quota.json" nlh companion
  ```

  A project-level `statusLine` (as above) takes precedence over the user-level
  one in that project, so use one or the other per repository.

  Nothing edits your settings; this is opt-in. A payload without
  `seven_day` keeps the last reading. The companion reads `$HARMONY_CLAUDE_QUOTA_FILE`, else
  `<workflow>/telemetry/claude-quota.json`. This fallback is needed only when
  the automatic native-CLI collector is disabled or unavailable. An authenticated
  interactive session verified a real `seven_day` measurement
  after its first response. The integration must be configured for interactive
  sessions; do not rely on `claude -p` writers to invoke a status line.
  A project-local `statusLine` wrapper can preserve the user-level command
  and output without changing global settings. If a session omits `seven_day`,
  the quiet fallback or timestamped last measurement remains; tests use
  labelled fixtures, never an account reading.

**Inspect task** (overview link, or *Inspect* on any task card) opens the
full per-task report as a secondary view at `#inspect/<id>`; *Back* or
Escape returns to the tab it came from and restores focus. In the report,
values the provider did not report render as `unknown`.
Sparklines place points by measurement time; points without a valid time
are counted in the caption but not drawn. Codex usage whose scope is not one
fresh single-turn invocation (resumed thread, unknown argv, several turns)
keeps cumulative `unknown` and shows the reported turn usage apart as
*Reported, scope unverified*. Opened run telemetry stays open across polls.

## Tests

```bash
python3 -B -m unittest discover -s task-board/tests -p 'test_*.py'
```

Isolated temporary workflow and run-record directories; no live data is read.
`test_companion_ui.py` runs the served page in jsdom (`tests/dom_harness.js`);
it is skipped unless `HARMONY_JSDOM_PATH` (or `NODE_PATH`) points at a
`node_modules` directory containing jsdom.


