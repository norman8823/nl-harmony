#!/usr/bin/env python3
"""PreToolUse guard: PR creation for direct releases goes through the worker app.

Direct release PRs are published only by `direct-workflow/worker_pr_publisher.py`,
under the worker app's `<app_slug>[bot]` identity configured in
`direct-workflow/worker-publisher.json`. This hook makes that an actual
preflight failure rather than only a written rule: a Claude Code Bash call
that tries to create a PR with `gh` is blocked before it runs.

What is blocked
---------------
`gh pr create` in its common honest forms, including:

  * a bare `gh pr create ...`
  * an absolute or relative path (`/opt/homebrew/bin/gh pr create`, `./gh ...`)
  * leading environment assignments and wrappers
    (`GH_TOKEN=... gh pr create`, `env GH_TOKEN=... gh pr create`,
    `sudo`/`command`/`nohup`/`time`/`exec`)
  * global flags before the subcommand (`gh --repo o/r pr create`)
  * compound commands and substitutions
    (`cd x && gh pr create`, `a; gh pr create`, `$(which gh) pr create`)
  * quoted single words (`gh "pr" 'create'`, `"gh" pr create`)
  * `gh pr new`, gh's built-in alias for `gh pr create`
  * `gh api` PR-creation calls to `[/]repos/:o/:r/pulls` (also as an absolute
    `https://…` URL, with or without a query string): an explicit
    `-X/--method POST` (`-XPOST`, `-X=POST`, `--method=post`, clustered
    `-iX POST`), or the implicit POST gh sends when `-f`, `-F`, `--field`,
    `--raw-field` or `--input` is given without another method, including
    attached and clustered shorthands (`-fhead=b`, `-Ftitle=t`, `-if a=b`)
  * `gh pr create --help`, so that appending a help flag is not a bypass

What stays allowed
------------------
Every read-only `gh` call: `gh pr list`, `gh pr view`, `gh pr diff`,
`gh pr checks`, `gh repo view`, `gh issue ...`, `gh api` GETs. Only the
create forms above are refused.

Enforcement boundary — read this before relying on it
-----------------------------------------------------
This is a string-level guard over the command Claude Code is about to run.
It stops the default habit and the honest mistake. It is NOT a sandbox and
does not claim to prevent a determined bypass. It cannot see through:

  * obfuscation (`eval`, base64, a command assembled from variables)
  * an indirection layer (a script or Makefile target that calls `gh`
    internally, an alias, a renamed `gh` binary)
  * a different transport (`curl`, `python3` against the REST API)
  * anything outside a Claude Code session that loaded this hook: another
    terminal, another agent, another checkout

**It only enforces where it is loaded.** A direct session launched with
`--setting-sources ''` loads no project settings. A release-capable session
must pass `--settings direct-workflow/release-settings.json`, which the CLI
loads as additional settings regardless of `--setting-sources`. A session
with empty setting sources and no `--settings` is **not release-capable**.

If the hook's own input envelope cannot be parsed it allows the call, so a
future change to the hook payload format cannot brick every Bash call in the
session.

The guard does **not** remove anyone's ability to open a PR — the operator's
own `gh` login still has that. What catches a PR opened under the wrong
identity is the publisher's post-creation check that the author is exactly
the configured worker bot.

Exit codes: 0 allow · 2 block (stderr is shown to the agent).
"""

from __future__ import annotations

import json
import re
import sys

GH_DOCS = "direct-workflow/README.md"
PUBLISHER = "direct-workflow/worker_pr_publisher.py"

# Short `gh` flags that consume the next token, so that token is not mistaken
# for a subcommand.
VALUE_FLAGS = {
    "-R", "-B", "-H", "-t", "-b", "-F", "-f", "-l", "-a", "-r", "-p", "-T", "-m", "-X",
}

# Hard command separators: a `gh` reference does not carry across these.
# Parentheses and braces are deliberately NOT separators. Keeping them inside
# words is what lets `$(which gh) pr create` and `` `which gh` pr create ``
# still resolve to a `gh` program reference.
_TOKEN_RE = re.compile(
    r"""
    (?P<sep>&&|\|\||;;|;|\|&|\||&|\n)      # separators
  | "(?P<dq>(?:[^"\\]|\\.)*)"              # "double quoted"
  | '(?P<sq>[^']*)'                        # 'single quoted'
  | (?P<word>[^\s&|;'"]+)                  # bare word
    """,
    re.VERBOSE,
)


class Token:
    __slots__ = ("text", "is_sep", "quoted")

    def __init__(self, text: str, is_sep: bool = False, quoted: bool = False) -> None:
        self.text = text
        self.is_sep = is_sep
        self.quoted = quoted

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Token({self.text!r}, sep={self.is_sep}, quoted={self.quoted})"


def tokenize(command: str) -> list[Token]:
    """Split a command into words and separators, keeping quoted spans whole.

    Quoted spans are marked: a literal `"pr create"` inside a search string is
    one quoted token and never matches the `pr` + `create` sequence.
    """
    tokens: list[Token] = []
    for match in _TOKEN_RE.finditer(command):
        if match.group("sep") is not None:
            tokens.append(Token(match.group("sep"), is_sep=True))
        elif match.group("dq") is not None:
            tokens.append(Token(match.group("dq"), quoted=True))
        elif match.group("sq") is not None:
            tokens.append(Token(match.group("sq"), quoted=True))
        else:
            tokens.append(Token(match.group("word")))
    return tokens


def _program_name(word: str) -> str:
    """Basename of a possible program token, ignoring shell substitution noise."""
    cleaned = word.strip().strip("\"'")
    # `$(which gh)` / `` `which gh` `` leave trailing punctuation behind.
    cleaned = cleaned.rstrip(")`")
    cleaned = cleaned.lstrip("$(`")
    if "/" in cleaned:
        cleaned = cleaned.rsplit("/", 1)[-1]
    return cleaned


def _bare(word: str) -> str:
    """A bare word with shell grouping punctuation stripped.

    `(cd x && gh pr create)` tokenizes its last word as `create)`; comparing
    subcommands against the stripped form keeps subshell forms covered.
    """
    return word.strip("(){}`")


def _word(token: Token) -> str | None:
    """The shell word a token stands for, or None for literal text.

    Quotes around a single word do not change what the shell runs:
    `gh "pr" 'create'` is `gh pr create`. A quoted span containing whitespace
    (`--search 'pr create'`, `echo 'gh pr create'`) is one literal argument and
    never a program or subcommand.
    """
    if token.is_sep:
        return None
    if token.quoted and (not token.text or any(c.isspace() for c in token.text)):
        return None
    return token.text


def _is_gh_reference(token: Token) -> bool:
    word = _word(token)
    if word is None:
        return False
    # An assignment like GH_TOKEN=gh is not a program reference.
    if "=" in word and not word.startswith("/") and not word.startswith("."):
        return False
    return _program_name(word) == "gh"


def _segment_tokens(tokens: list[Token], start: int) -> list[Token]:
    """Tokens from `start` up to the next hard separator."""
    segment: list[Token] = []
    for token in tokens[start:]:
        if token.is_sep:
            break
        segment.append(token)
    return segment


def _next_word_after(segment: list[Token], index: int) -> str | None:
    """The next bare word after `index`, skipping flags and their values.

    Long flags are the reason this skips rather than indexes: `gh --repo o/r pr
    create` puts an arbitrary value between the program and the subcommand, and
    the guard cannot know every value-taking `gh` flag. Skipping flag runs and
    then asking "is the word right after `pr` equal to `create`?" is what keeps
    `gh pr list --search create` allowed while `gh --repo o/r pr create` blocks.
    """
    position = index + 1
    skip_next = False
    while position < len(segment):
        token = segment[position]
        position += 1
        if skip_next:
            skip_next = False
            continue
        word = _word(token)
        if word is None:
            # Multi-word literal text is a flag value, never a subcommand.
            continue
        if word.startswith("-"):
            if word in VALUE_FLAGS:
                skip_next = True
            continue
        return _bare(word)
    return None


# `gh pr new` is gh's built-in alias for `gh pr create`.
_CREATE_SUBCOMMANDS = ("create", "new")


def _creates_pr(segment: list[Token]) -> bool:
    """True when this `gh` segment is a `pr create` (or its alias `pr new`)."""
    for index, token in enumerate(segment):
        word = _word(token)
        if word is None or _bare(word) != "pr":
            continue
        if _next_word_after(segment, index) in _CREATE_SUBCOMMANDS:
            return True
    return False


# `gh api` sends a POST, without any -X, as soon as a request parameter or body
# is added; `-X GET` turns those parameters into a query string instead.
_API_PARAM_LONG = ("--field", "--raw-field", "--input")
# `gh api` shorthands that take a value: the rest of a cluster is that value
# (`-XPOST`, `-fhead=b`), so scanning a cluster stops at the first of these.
_API_PARAM_SHORT = "fF"
_API_VALUE_SHORT = "fFXHpqtR"
# Path, absolute URL (github.com or GHES `/api/v3`) and an optional query string.
_PULLS_ENDPOINT = re.compile(
    r"^(?:https?://[^/\s]+(?:/api/v3)?)?/?repos/[^/\s]+/[^/\s]+/pulls/?(?:\?.*)?$",
    re.IGNORECASE,
)


def _short_cluster(word: str) -> tuple[bool, str | None]:
    """Parse a single-dash shorthand cluster like `-if`, `-fhead=b`, `-iXPOST`.

    Returns (has_param_flag, method_or_None). Only value-taking shorthands end
    a cluster, matching how gh's flag parser reads it.
    """
    has_param, method = False, None
    body = word[1:]
    for position, char in enumerate(body):
        if char in _API_PARAM_SHORT:
            has_param = True
        if char in _API_VALUE_SHORT:
            if char == "X":
                method = body[position + 1:].lstrip("=") or None
            break
    return has_param, method


def _api_request_shape(words: list[str]) -> tuple[str | None, bool]:
    """(explicit method or None, whether any request parameter/body is given).

    The method is the last `-X`/`--method` value, like gh.
    """
    method, has_params = None, False
    for index, word in enumerate(words):
        following = words[index + 1] if index + 1 < len(words) else None
        if word in ("-X", "--method"):
            if following is not None:
                method = following
        elif word.startswith("--method="):
            method = word.split("=", 1)[1]
        elif word in _API_PARAM_LONG or word.startswith(
            tuple(f + "=" for f in _API_PARAM_LONG)
        ):
            has_params = True
        elif word.startswith("-") and not word.startswith("--") and len(word) > 1:
            cluster_param, cluster_method = _short_cluster(word)
            has_params = has_params or cluster_param
            if cluster_method is not None:
                method = cluster_method
            elif word.endswith("X") and following is not None:
                method = following  # `-iX POST`
    return (method.upper() if method else None), has_params


def _creates_pr_via_api(segment: list[Token]) -> bool:
    """True when this `gh api` segment POSTs to a repository's pulls endpoint."""
    words = [w for w in (_word(token) for token in segment) if w is not None]
    if "api" not in (_bare(w) for w in words):
        return False
    if not any(_PULLS_ENDPOINT.search(_bare(w)) for w in words):
        return False
    method, has_params = _api_request_shape(words)
    if method is not None:
        return method == "POST"
    return has_params


def decide(command: str) -> tuple[bool, str]:
    """Return (blocked, reason). `reason` is empty when the call is allowed."""
    if not command or not command.strip():
        return False, ""

    tokens = tokenize(command)
    for index, token in enumerate(tokens):
        if not _is_gh_reference(token):
            continue
        segment = _segment_tokens(tokens, index + 1)
        if _creates_pr(segment):
            return True, "gh pr create"
        if _creates_pr_via_api(segment):
            return True, "gh api POST .../pulls"

    return False, ""


MESSAGE = """
BLOCKED: direct-release PRs are not created with `gh`.

Attempted: {reason}

Direct release PRs are published only by the worker GitHub App, so the PR is
authored by the configured `<app_slug>[bot]` and the owner can review it.
`gh` here would author it as the logged-in user, which is not a permitted
publication path and is never used as a fallback.

Use the publisher instead:

  # at kickoff, when the task is expected to produce a PR
  python3 {publisher} --preflight-only

  # to publish (fresh installation token, author verified, token revoked)
  python3 {publisher} \\
      --title "<title>" --head <branch> --base main \\
      --body-file <path> --execute

If preflight fails, the release gate is Blocked: report that and stop. Do not
publish under `gh` or user credentials.

Read-only `gh` is unaffected: `gh pr list`, `gh pr view`, `gh pr diff`,
`gh issue ...` and `gh api` GETs all still work. Help for the create form is
blocked too, deliberately, so that adding `--help` is not a bypass.

Details and the enforcement boundary: {docs}
"""


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "--check":
        # Offline self-check: `--check '<command>'` prints the decision.
        blocked, reason = decide(argv[1] if len(argv) > 1 else "")
        print(json.dumps({"blocked": blocked, "reason": reason}))
        return 2 if blocked else 0

    try:
        payload = json.loads(sys.stdin.read() or "{}")
        command = (payload.get("tool_input") or {}).get("command") or ""
    except Exception:
        # See "Enforcement boundary": a malformed envelope allows the call
        # rather than blocking every Bash call in the session.
        return 0

    blocked, reason = decide(command)
    if not blocked:
        return 0
    print(
        MESSAGE.format(reason=reason, publisher=PUBLISHER, docs=GH_DOCS),
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
