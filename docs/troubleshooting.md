# Troubleshooting

Find the issue that matches what you see. For first-time setup, start with the
[five-step guide](../README.md#install-and-start).

## The installer stops before finishing

Check that Python and Codex are available:

```bash
python3 --version
codex --version
```

The local connection needs Python **3.11 or newer** and a recent Codex CLI with
hook support. If either command is missing, follow the installation links in the
[setup guide](../README.md#install-and-start), then reopen your terminal.

Run `bash install-local.sh` again from the Harmony folder. If it still fails,
keep the error message; it identifies the step that needs attention.

## The terminal says “nlh: command not found”

Use the full command from the setup guide:

```bash
~/.local/share/harmony/venv/bin/nlh companion
```

This works without changing your terminal settings. If you chose a custom
installation location, use the executable path printed by the installer instead.

## Companion says “address already in use”

Companion may already be running. Find its terminal, press **Ctrl+C**, then start
it again from the same Harmony folder. Stop the existing instance before opening
another one.

If you cannot find that terminal, this command identifies the program listening
on the default port on macOS or Linux systems with `lsof` installed:

```bash
lsof -nP -iTCP:8765 -sTCP:LISTEN
```

Check which program it is before stopping it; another application may use that port.

## The browser does not open

Look in the Companion terminal for **Harmony companion on**, followed by a local
address. Copy that address into your browser and select **Tasks**. Keep the
Companion terminal running.

## Codex does not create a card

1. Check that Companion is running and the browser can load its board.
2. Close and reopen Codex if it was open when you installed Harmony.
3. If Codex shows **Hooks need review**, review and approve **Connecting to Harmony**.
   Choosing **Continue without trusting** leaves the connection inactive.
4. Give Codex an actual work request. Ordinary questions do not need new cards.

To check the installed connection and discover running boards, run:

```bash
~/.local/share/harmony/venv/bin/nlh codex status
```

The result reports `hook_installed`, `active_boards`, and `ready`. This checks
configuration and running boards; it does not prove a hook has been approved or
an already-open Codex session has reloaded its settings.

Companion and Codex CLI must run on the **same computer**. Codex cloud tasks on
another machine do not automatically sync to your local board. If several
Companions are running, tell Codex which board to use.

## Codex asks me to review the hook again

Review is expected on the first installation or when the hook settings change.
Ordinary restarts should remember approval, and updates preserve it when the
settings stay unchanged.

Update Harmony using the steps below. On the next Codex launch, review the
**Connecting to Harmony** hook and approve it. Then exit and reopen Codex without
reinstalling Harmony. If it asks again, note whether it says **new** or **changed**
and include your Codex version when reporting the issue. Repository trust is a
separate Codex prompt.

## A follow-up creates a duplicate card

Tell Codex which existing task to continue:

> Continue Harmony task #10 for this work. Reuse that card for follow-up messages.

Use your actual task number. A task represents one problem across prompts, turns,
and runs. Separate problems get separate cards; similar titles alone do not prove
two tasks are the same. Keep existing records while clarifying which card to use.

## Work is finished but the card is still open

Tell Codex:

> Refresh Harmony task #10 and record completion with a summary. If completion
> is refused, show me the exact error.

Use your actual task number. Successful completion moves the card to **Done**
with **Completed by Codex**. Simple CLI tasks do not need a second Companion
review; lack of a task graph alone does not prevent completion.

A task using a formal workflow may have explicitly declared checks or approval
gates. Codex should report the actual outstanding requirement. A stale-revision
error means it must refresh the card before trying again.

## My cards disappeared after restarting

Companion stores cards in the board folder's `.workflow/` directory. Starting it
from a different folder can open a different board.

Stop Companion and restart it from the same folder you used before. To select
that folder explicitly:

```bash
~/.local/share/harmony/venv/bin/nlh --project /path/to/your/board-folder companion
```

Replace `/path/to/your/board-folder` with your existing board folder. Also check
**Archived** in the Tasks view. Updating Harmony does not require deleting task data.

## Allowance or token numbers are unknown or stale

This can mean no supported run data is available or a provider reading could not
be refreshed. It does not mean usage was zero. See the
[measurement questions in the FAQ](faq.md#what-does-harmony-measure).

## Update or restart Harmony

To update, open a terminal in your existing Harmony folder:

```bash
git pull --ff-only origin main
bash install-local.sh
```

Stop Companion with **Ctrl+C**, then start it again from the same board folder:

```bash
~/.local/share/harmony/venv/bin/nlh companion
```

Close and reopen Codex too. To restart without updating, just stop and start
Companion; you do not need to reinstall it.

## Still stuck?

Include the error message, what you were trying to do, and these version outputs
when reporting the issue:

```bash
~/.local/share/harmony/venv/bin/nlh --version
codex --version
python3 --version
```

[← Back to Harmony](../README.md)
