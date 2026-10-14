#!/usr/bin/env python3
"""Direct-workflow release PR publisher — the GitHub App worker identity.

The non-secret identity (app id, slug, repository, permissions, key location)
comes from a local JSON config file (copy `worker-publisher.example.json` to
`worker-publisher.json` and edit it; that file is git-ignored):

    RS256 app JWT (openssl, local key file)
      -> verify the app identity and this repository's installation
      -> mint a fresh, narrowly scoped installation token, in memory only
      -> create the PR
      -> verify the created PR's author is exactly `<app_slug>[bot]`
      -> revoke the token (always, including every failure path)

There is deliberately no fallback to `gh`, to environment tokens, or to the
operator's user credentials. A missing or invalid config, or a worker
identity that cannot be verified, Blocks the release; it is never published
under another identity.

The private key is read only by `openssl` in a subprocess: this process never
reads, stores or prints key material, and never prints a token.

Usage
-----
Preflight only (mints nothing; JWT-scoped reads only):

    python3 direct-workflow/worker_pr_publisher.py --preflight-only

Publish a release PR (requires --execute; without it nothing is created):

    python3 direct-workflow/worker_pr_publisher.py \
        --title "..." --head <branch> --base main \
        --body-file <path> --execute

Config: `--config <path>`, else $WORKER_PUBLISHER_CONFIG, else
`worker-publisher.json` beside this script. Key location: `--key-path`, else
$WORKER_APP_KEY_PATH, else the config's `key_path`.

Exit codes: 0 success or dry run · 1 fail-closed (config, auth, access,
permission, author mismatch, HTTP failure) · 2 usage.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

API_ROOT = "https://api.github.com"
USER_AGENT = "direct-workflow-release-publisher"
DEFAULT_CONFIG_PATH = Path(__file__).resolve().with_name("worker-publisher.json")
CONFIG_ENV = "WORKER_PUBLISHER_CONFIG"
KEY_PATH_ENV = "WORKER_APP_KEY_PATH"
PERMISSION_LEVELS = {"read", "write"}

# GitHub's top-level "message" is kept for diagnosis, truncated to this many
# characters. No response body is ever surfaced raw.
MAX_ERROR_CHARS = 200


class PublisherError(Exception):
    """Fail-closed error. Messages raised here are safe to print.

    `pr` carries the number/URL of a PR that was already created before the
    failure, so the operator handles that PR instead of publishing again.
    """

    pr: dict[str, Any] | None = None


class ConfigError(PublisherError):
    """The non-secret publisher config is missing, unreadable or invalid."""


class AuthError(PublisherError):
    """Local signing or app-JWT construction failed."""


class AccessError(PublisherError):
    """Expected app identity, installation, repo or permissions unavailable."""


class AuthorMismatchError(PublisherError):
    """A PR was created, but not under the expected worker identity."""


class TokenRevocationError(PublisherError):
    """The installation token could not be confirmed revoked.

    Never a success: even when the PR was created and verified, publication
    is reported Blocked so the live token is dealt with first.
    """


class GitHubError(PublisherError):
    """A GitHub request failed. Carries status and a sanitized message only."""

    def __init__(self, method: str, path: str, status: int, message: str = "") -> None:
        self.status = status
        detail = f": {message}" if message else ""
        super().__init__(f"GitHub request failed: {method} {path} HTTP {status}{detail}")


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _sanitize(body: Any) -> str:
    """Return only GitHub's top-level message, truncated. Never a raw body."""
    if not isinstance(body, dict):
        return ""
    message = body.get("message")
    if not isinstance(message, str):
        return ""
    message = " ".join(message.split())
    if len(message) > MAX_ERROR_CHARS:
        message = message[:MAX_ERROR_CHARS] + "..."
    return message


# --- Signing and transport (both injectable, so tests touch neither) --------


def openssl_signer(key_path: str, payload: bytes) -> bytes:
    """Sign via openssl. Key material never enters this process's memory."""
    if not os.path.isfile(key_path):
        raise AuthError(f"Worker app key not found at {key_path}")
    result = subprocess.run(
        ["openssl", "dgst", "-sha256", "-sign", key_path],
        input=payload,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0 or not result.stdout:
        # openssl's stderr can quote key-parsing detail; it is not surfaced.
        raise AuthError("Local RS256 signing failed (details suppressed)")
    return result.stdout


def urllib_http(
    method: str, path: str, token: str, data: dict[str, Any] | None
) -> tuple[int, Any]:
    request = urllib.request.Request(
        API_ROOT + path,
        data=None if data is None else json.dumps(data).encode(),
        method=method,
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
            return response.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            body = json.loads(raw)
        except Exception:
            body = None
        return exc.code, body
    except urllib.error.URLError as exc:
        raise GitHubError(
            method, path, 0, f"network unavailable ({exc.reason})"
        ) from None


@dataclass
class Config:
    """Non-secret worker identity. Every field is required; nothing defaults."""

    app_id: int
    app_slug: str
    repo_owner: str
    repo_name: str
    key_path: str
    permissions: dict[str, str] = field(default_factory=dict)

    @property
    def repo(self) -> str:
        return f"{self.repo_owner}/{self.repo_name}"

    @property
    def expected_author(self) -> str:
        # Derived, never configured: a GitHub App's PRs are authored by
        # `<slug>[bot]`, so allowing an override would only allow a mismatch.
        return f"{self.app_slug}[bot]"


def config_from_dict(raw: Any, key_path_override: str | None = None) -> Config:
    """Validate a config document. Raises ConfigError on anything doubtful."""
    if not isinstance(raw, dict):
        raise ConfigError("Publisher config must be a JSON object")

    app_id = raw.get("app_id")
    if isinstance(app_id, bool) or not isinstance(app_id, int) or app_id <= 0:
        raise ConfigError("Publisher config 'app_id' must be a positive integer")

    strings: dict[str, str] = {}
    for name in ("app_slug", "repo_owner", "repo_name"):
        value = raw.get(name)
        if not isinstance(value, str) or not value.strip() or "/" in value:
            raise ConfigError(f"Publisher config {name!r} must be a non-empty name")
        strings[name] = value.strip()

    permissions = raw.get("permissions")
    if not isinstance(permissions, dict) or not permissions:
        raise ConfigError("Publisher config 'permissions' must be a non-empty object")
    for name, level in permissions.items():
        if not isinstance(name, str) or level not in PERMISSION_LEVELS:
            raise ConfigError(
                f"Publisher config permission {name!r} must be 'read' or 'write'"
            )
    if permissions.get("pull_requests") != "write":
        raise ConfigError(
            "Publisher config must request 'pull_requests': 'write' to create a PR"
        )

    key_path = key_path_override or raw.get("key_path")
    if not isinstance(key_path, str) or not key_path.strip():
        raise ConfigError(
            f"No worker key location: pass --key-path, set {KEY_PATH_ENV}, "
            "or set 'key_path' in the config"
        )

    return Config(
        app_id=app_id,
        app_slug=strings["app_slug"],
        repo_owner=strings["repo_owner"],
        repo_name=strings["repo_name"],
        key_path=key_path,
        permissions=dict(permissions),
    )


def load_config(path: str | Path, key_path_override: str | None = None) -> Config:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"Publisher config not found at {path}") from None
    except (OSError, ValueError) as exc:
        raise ConfigError(
            f"Publisher config at {path} is unreadable ({type(exc).__name__})"
        ) from None
    return config_from_dict(raw, key_path_override)


class Publisher:
    """Worker-app publisher. `signer` and `http` are injected for offline tests."""

    def __init__(
        self,
        config: Config,
        signer: Callable[[str, bytes], bytes] = openssl_signer,
        http: Callable[..., tuple[int, Any]] = urllib_http,
        clock: Callable[[], int] = lambda: int(time.time()),
    ) -> None:
        if not isinstance(config, Config):
            raise ConfigError("Publisher requires an explicit, validated Config")
        self.config = config
        self._signer = signer
        self._http = http
        self._clock = clock
        self.tokens_minted = 0
        self.tokens_revoked = 0

    # -- transport helpers ---------------------------------------------------

    def _request(
        self, method: str, path: str, token: str, data: dict[str, Any] | None = None
    ) -> Any:
        status, body = self._http(method, path, token, data)
        if status >= 300:
            raise GitHubError(method, path, status, _sanitize(body))
        return body

    def _jwt(self) -> str:
        now = self._clock()
        header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
        claims = _b64url(
            json.dumps(
                {"iat": now - 60, "exp": now + 540, "iss": str(self.config.app_id)}
            ).encode()
        )
        unsigned = f"{header}.{claims}"
        signature = self._signer(self.config.key_path, unsigned.encode())
        return f"{unsigned}.{_b64url(signature)}"

    # -- preflight -----------------------------------------------------------

    def preflight(self) -> dict[str, Any]:
        """Verify the expected app, this repo's installation and its grants.

        Mints no installation token. Raises rather than returning a degraded
        result: an unverifiable worker identity Blocks the release, it never
        downgrades to another identity.
        """
        jwt = self._jwt()

        app = self._request("GET", "/app", jwt) or {}
        if app.get("id") != self.config.app_id:
            raise AccessError(
                f"App identity mismatch: expected app id {self.config.app_id}, "
                f"got {app.get('id')!r}"
            )
        if app.get("slug") != self.config.app_slug:
            raise AccessError(
                f"App slug mismatch: expected {self.config.app_slug!r}, "
                f"got {app.get('slug')!r}"
            )

        installation = (
            self._request("GET", f"/repos/{self.config.repo}/installation", jwt) or {}
        )
        installation_id = installation.get("id")
        if not installation_id:
            raise AccessError(f"No app installation found for {self.config.repo}")

        account = (installation.get("account") or {}).get("login", "")
        if account.lower() != self.config.repo_owner.lower():
            raise AccessError(
                f"Installation account mismatch: expected "
                f"{self.config.repo_owner!r}, got {account!r}"
            )

        self._assert_permissions(installation.get("permissions") or {}, "installation")

        granted = installation.get("permissions") or {}
        return {
            "app_id": app.get("id"),
            "app_slug": app.get("slug"),
            "expected_author": self.config.expected_author,
            "repo": self.config.repo,
            "installation_id": installation_id,
            "repository_selection": installation.get("repository_selection"),
            "granted_permissions": {
                key: granted.get(key) for key in self.config.permissions
            },
            "worker_access": "ok",
        }

    def _assert_permissions(self, granted: dict[str, Any], source: str) -> None:
        for name, needed in self.config.permissions.items():
            actual = granted.get(name)
            if actual != needed and not (needed == "read" and actual == "write"):
                raise AccessError(
                    f"Insufficient {source} permission {name!r}: need "
                    f"{needed!r}, have {actual!r}"
                )

    # -- token lifecycle -----------------------------------------------------

    def _mint_token(self, installation_id: int) -> tuple[str, dict[str, Any]]:
        """Mint a fresh repo-scoped installation token. Never cached, never written."""
        response = (
            self._request(
                "POST",
                f"/app/installations/{installation_id}/access_tokens",
                self._jwt(),
                {
                    "repositories": [self.config.repo_name],
                    "permissions": dict(self.config.permissions),
                },
            )
            or {}
        )
        token = response.get("token")
        if not token:
            raise AccessError("Installation token request returned no token")
        self.tokens_minted += 1
        return token, response

    def _revoke_token(self, token: str) -> bool:
        """Best-effort revocation, called on every path, success or failure."""
        try:
            status, _ = self._http("DELETE", "/installation/token", token, None)
            revoked = status == 204
        except Exception:
            revoked = False
        if revoked:
            self.tokens_revoked += 1
        return revoked

    def _verify_token_scope(self, token: str, response: dict[str, Any]) -> None:
        """Confirm the fresh token really carries the expected repo and grants."""
        self._assert_permissions(response.get("permissions") or {}, "token")
        # The token must be scoped to exactly the target repository. A response
        # that omits the list proves nothing, and any extra repository widens
        # the blast radius: both fail closed.
        repositories = response.get("repositories")
        if not isinstance(repositories, list) or not repositories:
            raise AccessError(
                "Token response does not list its repositories; cannot verify "
                f"exclusive scope to {self.config.repo}"
            )
        names = [
            repo.get("full_name") if isinstance(repo, dict) else None
            for repo in repositories
        ]
        if names != [self.config.repo]:
            raise AccessError(
                f"Token is not scoped exclusively to {self.config.repo} "
                f"({len(names)} repositories in scope)"
            )
        # Independent confirmation that the token actually reaches the repo.
        repo = self._request("GET", f"/repos/{self.config.repo}", token) or {}
        if repo.get("full_name") != self.config.repo:
            raise AccessError(
                f"Token repository check failed: expected {self.config.repo}, "
                f"got {repo.get('full_name')!r}"
            )

    # -- publication ---------------------------------------------------------

    def create_pull_request(
        self,
        title: str,
        head: str,
        base: str,
        body: str,
        draft: bool = False,
    ) -> dict[str, Any]:
        """Publish a release PR as the worker bot, or fail closed.

        A fresh token is minted immediately before creation and revoked on
        every path, success or failure. There is no retry under a different
        identity: an author mismatch is a hard stop and the release is not
        declared ready. A revocation that cannot be confirmed is never a
        success either: it raises TokenRevocationError carrying the created
        PR (if any), so the operator handles that PR rather than publishing
        again.
        """
        access = self.preflight()
        token, token_response = self._mint_token(access["installation_id"])
        created_pr: dict[str, Any] = {}
        try:
            result = self._publish(token, token_response, created_pr,
                                   title, head, base, body, draft)
        except BaseException as exc:
            revoked = self._finish_token(token)
            token = ""  # drop the last reference held by this frame
            if isinstance(exc, PublisherError) and created_pr:
                exc.pr = dict(created_pr)
            if not revoked:
                raise self._revocation_error(created_pr, exc) from exc
            raise
        revoked = self._finish_token(token)
        token = ""
        if not revoked:
            raise self._revocation_error(created_pr, None)
        return result

    def _finish_token(self, token: str) -> bool:
        revoked = self._revoke_token(token)
        print(json.dumps({"installation_token_revoked": revoked}), file=sys.stderr)
        return revoked

    @staticmethod
    def _revocation_error(
        created_pr: dict[str, Any], prior: BaseException | None
    ) -> TokenRevocationError:
        if created_pr:
            where = (
                f"PR #{created_pr.get('number')} ({created_pr.get('url') or 'url unknown'}) "
                "WAS created; handle that PR, do not publish again"
            )
        else:
            where = "no PR was created"
        detail = f"; earlier failure: {prior}" if isinstance(prior, PublisherError) else ""
        error = TokenRevocationError(
            "Installation token revocation could not be confirmed; publication is "
            f"Blocked ({where}){detail}. Revoke the token or let it expire before "
            "any further release action."
        )
        error.pr = dict(created_pr) if created_pr else None
        return error

    def _publish(
        self,
        token: str,
        token_response: dict[str, Any],
        created_pr: dict[str, Any],
        title: str,
        head: str,
        base: str,
        body: str,
        draft: bool,
    ) -> dict[str, Any]:
        """Verify scope, create, re-read. Records the PR in `created_pr` as soon
        as it exists, so every later failure can report it."""
        self._verify_token_scope(token, token_response)
        created = (
            self._request(
                "POST",
                f"/repos/{self.config.repo}/pulls",
                token,
                {"title": title, "head": head, "base": base, "body": body, "draft": draft},
            )
            or {}
        )
        number = created.get("number")
        if not number:
            raise GitHubError(
                "POST", f"/repos/{self.config.repo}/pulls", 0, "response carried no PR number"
            )
        created_pr.update(number=number, url=created.get("html_url"))

        # Re-read rather than trusting the creation response.
        verified = (
            self._request("GET", f"/repos/{self.config.repo}/pulls/{number}", token) or {}
        )
        if verified.get("html_url"):
            created_pr["url"] = verified["html_url"]
        author = (verified.get("user") or {}).get("login")
        author_type = (verified.get("user") or {}).get("type")
        if author != self.config.expected_author or author_type != "Bot":
            raise AuthorMismatchError(
                f"PR #{number} author is {author!r} ({author_type!r}), expected "
                f"{self.config.expected_author!r} (Bot). Release is NOT ready; "
                f"do not re-publish under gh or user credentials."
            )
        return {
            "number": number,
            "url": verified.get("html_url"),
            "author": author,
            "author_type": author_type,
            "expected_author": self.config.expected_author,
            "author_verified": True,
            "head": (verified.get("head") or {}).get("ref"),
            "head_sha": (verified.get("head") or {}).get("sha"),
            "base": (verified.get("base") or {}).get("ref"),
            "draft": verified.get("draft"),
        }


def _blocked(message: str, pr: dict[str, Any] | None = None) -> int:
    report: dict[str, Any] = {
        "worker_access": "unavailable",
        "release_gate": "Blocked",
        "reason": message,
        "fallback": "none - do not publish under gh or user credentials",
    }
    if pr:
        # A PR exists even though the release is Blocked: handle it, never re-publish.
        report["pr_created"] = True
        report["pr_number"] = pr.get("number")
        report["pr_url"] = pr.get("url")
        report["retry"] = "do not re-run the publisher; handle the existing PR"
    print(json.dumps(report, indent=2))
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="worker_pr_publisher.py",
        description=(
            "Create direct-release PRs as the worker GitHub App. This is the "
            "only permitted PR creation path for direct releases."
        ),
    )
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="verify worker access and exit; mints no token, creates nothing",
    )
    parser.add_argument("--title")
    parser.add_argument("--head", help="source branch")
    parser.add_argument("--base", default="main")
    parser.add_argument("--body-file", help="path to the PR body markdown")
    parser.add_argument("--draft", action="store_true")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="required to actually create the PR; omit for a no-op dry run",
    )
    parser.add_argument(
        "--config",
        default=os.environ.get(CONFIG_ENV) or str(DEFAULT_CONFIG_PATH),
        help="non-secret publisher config (app id, slug, repo, permissions)",
    )
    parser.add_argument(
        "--key-path",
        default=os.environ.get(KEY_PATH_ENV) or None,
        help="worker app private key path (a location, never key material)",
    )
    args = parser.parse_args(argv)

    if not args.preflight_only:
        missing = [
            name
            for name, value in (
                ("--title", args.title),
                ("--head", args.head),
                ("--body-file", args.body_file),
            )
            if not value
        ]
        if missing:
            parser.error(
                "missing required argument(s) for publication: " + ", ".join(missing)
            )

    try:
        config = load_config(args.config, args.key_path)
    except PublisherError as exc:
        return _blocked(str(exc))
    publisher = Publisher(config=config)

    if args.preflight_only:
        try:
            print(json.dumps(publisher.preflight(), indent=2))
        except PublisherError as exc:
            return _blocked(str(exc))
        return 0

    try:
        with open(args.body_file, encoding="utf-8") as handle:
            body = handle.read()
    except OSError as exc:
        parser.error(f"cannot read --body-file: {exc.strerror}")

    if not args.execute:
        print(
            json.dumps(
                {
                    "dry_run": True,
                    "would_create": {
                        "repo": publisher.config.repo,
                        "title": args.title,
                        "head": args.head,
                        "base": args.base,
                        "draft": args.draft,
                        "body_chars": len(body),
                    },
                    "note": "re-run with --execute to publish as "
                    + publisher.config.expected_author,
                },
                indent=2,
            )
        )
        return 0

    try:
        result = publisher.create_pull_request(
            title=args.title,
            head=args.head,
            base=args.base,
            body=body,
            draft=args.draft,
        )
    except PublisherError as exc:
        return _blocked(str(exc), exc.pr)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
