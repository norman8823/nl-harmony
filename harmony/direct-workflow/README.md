# direct-workflow

The agent-run half of Harmony. Usage, setup and examples are
in the repository's root `README.md`; each script's docstring has its full
contract and `--help` lists its flags.

| File | Role |
|---|---|
| `direct_run_recorder.py` | Runs a `claude` or `codex exec --json` call unchanged, keeps a live `running` snapshot, and writes one JSON record per run |
| `run_observability.py` | Read-only `inspect-run` / `summarize-task` / `compare` over run records; the companion's per-task view and attribution |
| `task_checkpoint.py` | One compact checkpoint (live context) per task, and a fresh-session bootstrap |
| `task_graph.py` | One fixed-shape workflow graph per task and its next valid action |
| `statusline.py` | One-line Claude Code `statusLine` renderer; with `--quota-file` (inside `.workflow`) also keeps the official `seven_day` reading for the companion |
| `quota_telemetry.py` | Weekly allowance remaining from official sources only: Codex app-server rate limits (10080-minute window) and the Claude status-line snapshot |
| `direct-workflow-pr-guard.py` | PreToolUse Bash hook that blocks `gh pr create` and `gh api` PR POSTs |
| `release-settings.json` | Loads only that hook into a session |
| `worker_pr_publisher.py` | Optional PR publisher using your own GitHub App; fails closed |
| `worker-publisher.example.json` | Fake config; copy to the git-ignored `worker-publisher.json` and edit |
| `output_summary.py` | Runs a noisy command, keeps full output in an evidence file, prints a bounded verdict |
| `tests/` | Offline standard-library tests |

## Release-capable sessions

Writer and correction sessions must be started with
`--settings direct-workflow/release-settings.json`; the recorder refuses them
otherwise. That flag is the only thing that loads the guard, which is
deliberately not registered in a shared `.claude/settings.json`. A session
started with `--setting-sources ''` and no `--settings` is **not
release-capable**: do not publish from it.

The hook command runs `${HARMONY_PR_GUARD:-${CLAUDE_PROJECT_DIR:-.}/direct-workflow/direct-workflow-pr-guard.py}`.
The recorder sets `HARMONY_PR_GUARD` to this package's guard for every Claude
run, so an installed package loads the real hook from any project; if the
path resolves to nothing, `python3` exits 2 and every Bash call is blocked.

The guard is a string-level check of the command Claude Code is about to run,
not a sandbox: obfuscation, scripts that call `gh`, `curl`, GraphQL mutations
or anything outside a session that loaded the hook are not seen. The
publisher's post-creation author check is what catches a PR opened under the
wrong identity.

## Parallel investigators

New task graphs are `shape_version` 2. Implement has no dependencies; review-1
depends on implement and every investigator; `max_concurrency` (default 2)
caps all running nodes, the writer included, and at most one non-investigator
runs at a time. `startable` puts a ready writer ahead of investigators, so the
default lets the writer and one investigator run together. `status`/`next` and
the checkpoint's `review_handoff` list the investigators' deduplicated evidence
refs and run ids apart from the review's own evidence; a review must link its
own evidence, and an inherited finding ref never counts as proof that the
review happened. A graph with no `shape_version` (or 1) keeps the legacy
serial rules exactly and is never rewritten; any other version is refused.

An investigator run reads one explicit commit in its own temporary worktree:

    python3 direct-workflow/direct_run_recorder.py run --task-id 42 --role investigator \
        --base-commit <sha> -- codex exec --json -m <model> -s read-only - < investigate.md

`--base-commit` (also on `nlh record`) is required for investigators and
refused for every other role. It must be a SHA in the repository containing
`--project-root` (default: the current directory; `nlh record` passes the
selected `--project`, whatever the current directory) — no `HEAD` or branch
names, nothing guessed; a `base_commit` declared on the task's ticket or graph
must be the same commit. Any repository-configured git filter command
(`filter.<name>.smudge`, `.clean` or `.process`, from repository config) is
refused before the checkout and again before the status check, naming only the
driver: git would run it outside the investigator's sandbox. The recorder
resolves the records directory to an absolute path, adds a detached worktree of
that commit in a fresh temporary directory outside the checkout (hooks and
fsmonitor disabled for its own git calls), checks that HEAD is the base and the
status is clean, starts the run with that worktree as its working directory,
and removes only that worktree afterwards, on success, failure, Ctrl-C, SIGTERM
or SIGHUP — including a signal during preparation. On a signal the child is
terminated (killed after 5 s) and waited for before the worktree is removed,
the previous signal handlers are restored, and the record becomes
`interrupted` (`interrupted_by`, the child's `exit_code`,
`isolated_worktree_removed`); the recorder exits 128 + signal. `running`,
`launch_failed`, `interrupted` and final records carry `repository`
(`base_commit`, `read_commit`, `isolated_worktree`, `source`); a record
imported with `parse` keeps them unknown.

The argv is checked fail-closed (every option known, each at most once):
Codex needs `codex exec` with `-s/--sandbox read-only` and refuses `-c/--config`,
profiles, `--full-auto`, bypass, `--cd` and `--add-dir`. Claude needs
`--restricted`, `--tools` limited to `Read,Grep,Glob` (or `""`),
`--disallowedTools` naming at least
`Write,Edit,MultiEdit,NotebookEdit,Bash,PowerShell,REPL,Agent,Task`,
`--strict-mcp-config --mcp-config '{"mcpServers":{}}'`,
`--settings '{"disableAllHooks":true}'` and `--setting-sources ''`, and refuses
`--allowedTools`, permission bypass/widening modes, `--add-dir`, plugins,
agents, resume/continue and its own `--worktree`. `--restricted` confines
Claude's built-in file tools to the working directory (the worktree); it is
required alongside the deny list, not instead of it. Put the prompt before
`--tools`/`--disallowedTools`/`--mcp-config`, which consume every following
non-option argument.

Known limits: the pinned worktree is the run's default repository input, not
a proof that every read came from that commit, and it isolates files, not the
machine — the argv checks are what keep a run read-only. Codex's read-only
sandbox prevents writes but is not a universal filesystem read boundary;
Claude's `--restricted` confinement is the CLI's documented behaviour, and
no native outside-read denial has been observed by Harmony (the single real
probe ran with `--restricted` and exposed only Read/Grep/Glob). The recorder’s
Git preparation ignores global/system config without changing
user settings. Repository-configured filter commands are still refused; this
includes local Git LFS filters. Global LFS helpers do not run, so committed
LFS pointers remain pointers in the investigator worktree.
SIGKILL (and a crash of the recorder itself) cannot be handled: the child, the
worktree and a `running` record can then be left behind. A removal that git
refuses is reported on stderr and its temporary directory is still deleted,
leaving a stale entry that `git worktree list` shows as prunable (the recorder
never prunes).
