#!/usr/bin/env python3
"""Offline tests for the worker-app release PR publisher.

Fully offline by construction: the signer and the HTTP transport are both
injected, so no private key is read, no JWT is signed, no token is minted and
no GitHub request is made. No API credits, no network, no `gh`.

Run:  python3 direct-workflow/tests/test_worker_pr_publisher.py
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE = REPO_ROOT / "direct-workflow"
PUBLISHER_PATH = PACKAGE / "worker_pr_publisher.py"
EXAMPLE_CONFIG_PATH = PACKAGE / "worker-publisher.example.json"


def load_publisher():
    spec = importlib.util.spec_from_file_location("worker_pr_publisher", PUBLISHER_PATH)
    module = importlib.util.module_from_spec(spec)
    # Registered before exec so dataclass field resolution can find the module.
    sys.modules[spec.name] = module
    sys.dont_write_bytecode = True  # no __pycache__ beside the script
    spec.loader.exec_module(module)
    return module


wp = load_publisher()

APP_ID = 1000001
APP_SLUG = "example-worker"
EXPECTED_AUTHOR = "example-worker[bot]"
FAKE_TOKEN = "ghs_FAKE_TEST_TOKEN_not_a_real_credential"
APP = {"id": APP_ID, "slug": APP_SLUG}
INSTALLATION = {
    "id": 77777,
    "account": {"login": "example-org"},
    "repository_selection": "selected",
    "permissions": {"pull_requests": "write", "contents": "read", "metadata": "read"},
}
TOKEN_RESPONSE = {
    "token": FAKE_TOKEN,
    "expires_at": "2026-09-30T13:00:00Z",
    "permissions": {"pull_requests": "write", "contents": "read"},
    "repositories": [{"full_name": "example-org/example-repo"}],
}
REPO_RESPONSE = {"full_name": "example-org/example-repo"}
CREATED_PR = {"number": 150}
VERIFIED_PR = {
    "number": 150,
    "html_url": "https://github.com/example-org/example-repo/pull/150",
    "user": {"login": EXPECTED_AUTHOR, "type": "Bot"},
    "head": {"ref": "feat/task34", "sha": "a" * 40},
    "base": {"ref": "main"},
    "draft": False,
}
VALID_RAW = {
    "app_id": APP_ID,
    "app_slug": APP_SLUG,
    "repo_owner": "example-org",
    "repo_name": "example-repo",
    "permissions": {"pull_requests": "write", "contents": "read"},
    "key_path": "/nonexistent/test-key.pem",
}


def test_config(**overrides):
    return wp.config_from_dict(dict(VALID_RAW, **overrides))


def signer_ok(key_path, payload):
    """Stand-in for openssl. Never touches a key file."""
    return b"fake-signature-bytes"


def signer_fails(key_path, payload):
    raise wp.AuthError("Local RS256 signing failed (details suppressed)")


class FakeGitHub:
    """Scripted transport. Records every call, including the token used."""

    def __init__(self, overrides=None):
        self.calls = []
        self.tokens_seen = []
        self.responses = {
            ("GET", "/app"): (200, APP),
            ("GET", "/repos/example-org/example-repo/installation"): (200, INSTALLATION),
            ("POST", "/app/installations/77777/access_tokens"): (201, TOKEN_RESPONSE),
            ("GET", "/repos/example-org/example-repo"): (200, REPO_RESPONSE),
            ("POST", "/repos/example-org/example-repo/pulls"): (201, CREATED_PR),
            ("GET", "/repos/example-org/example-repo/pulls/150"): (200, VERIFIED_PR),
            ("DELETE", "/installation/token"): (204, None),
        }
        if overrides:
            self.responses.update(overrides)

    def __call__(self, method, path, token, data=None):
        self.calls.append((method, path, data))
        self.tokens_seen.append(token)
        try:
            return self.responses[(method, path)]
        except KeyError:  # pragma: no cover - a test asked for an unscripted call
            raise AssertionError(f"unscripted call: {method} {path}")

    @property
    def paths(self):
        return [(method, path) for method, path, _ in self.calls]

    def count(self, method, path):
        return self.paths.count((method, path))


def make_publisher(http=None, signer=signer_ok, config=None):
    return wp.Publisher(
        config=config or test_config(),
        signer=signer,
        http=http or FakeGitHub(),
        clock=lambda: 1_759_000_000,
    )


PUBLISH_KWARGS = dict(
    title="Task 34", head="feat/task34", base="main", body="body", draft=False
)


class TestConfig(unittest.TestCase):
    """Reusable config: explicit, validated, and failing closed."""

    def test_example_config_is_valid_and_clearly_fake(self):
        config = wp.load_config(EXAMPLE_CONFIG_PATH)
        self.assertEqual(config.app_slug, "your-worker-app")
        self.assertEqual(config.expected_author, "your-worker-app[bot]")
        self.assertEqual(config.repo, "your-org/your-repo")
        self.assertEqual(
            config.permissions, {"pull_requests": "write", "contents": "read"}
        )
        self.assertTrue(config.key_path.startswith("/path/to/"))

    def test_example_config_holds_no_secret_material(self):
        text = EXAMPLE_CONFIG_PATH.read_text()
        for marker in ("PRIVATE KEY", "ghs_", "ghp_", "token", "secret"):
            with self.subTest(marker):
                self.assertNotIn(marker, text)

    def test_expected_author_is_derived_not_configurable(self):
        config = wp.config_from_dict(dict(VALID_RAW, expected_author="example-org"))
        self.assertEqual(config.expected_author, EXPECTED_AUTHOR)

    def test_missing_config_file(self):
        with self.assertRaises(wp.ConfigError) as ctx:
            wp.load_config("/nonexistent/worker-publisher.json")
        self.assertIn("not found", str(ctx.exception))

    def test_unparseable_config_file(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            handle.write("{not json")
        self.addCleanup(os.unlink, handle.name)
        with self.assertRaises(wp.ConfigError):
            wp.load_config(handle.name)

    def test_invalid_fields_are_rejected(self):
        cases = {
            "not an object": [],
            "app_id missing": {k: v for k, v in VALID_RAW.items() if k != "app_id"},
            "app_id string": dict(VALID_RAW, app_id="1000001"),
            "app_id bool": dict(VALID_RAW, app_id=True),
            "app_id zero": dict(VALID_RAW, app_id=0),
            "slug empty": dict(VALID_RAW, app_slug=" "),
            "owner missing": {k: v for k, v in VALID_RAW.items() if k != "repo_owner"},
            "repo with slash": dict(VALID_RAW, repo_name="a/b"),
            "permissions empty": dict(VALID_RAW, permissions={}),
            "permission admin": dict(
                VALID_RAW, permissions={"pull_requests": "write", "contents": "admin"}
            ),
            "cannot create PRs": dict(VALID_RAW, permissions={"contents": "read"}),
            "no key path": {k: v for k, v in VALID_RAW.items() if k != "key_path"},
        }
        for label, raw in cases.items():
            with self.subTest(label):
                with self.assertRaises(wp.ConfigError):
                    wp.config_from_dict(raw)

    def test_key_path_override_wins(self):
        raw = {k: v for k, v in VALID_RAW.items() if k != "key_path"}
        self.assertEqual(
            wp.config_from_dict(raw, "/elsewhere/key.pem").key_path, "/elsewhere/key.pem"
        )

    def test_publisher_requires_an_explicit_config(self):
        with self.assertRaises(wp.ConfigError):
            wp.Publisher(config=None)

    def test_another_checkout_uses_its_own_identity(self):
        other = wp.config_from_dict(
            dict(VALID_RAW, app_id=42, app_slug="acme-worker", repo_owner="acme",
                 repo_name="widgets")
        )
        http = FakeGitHub(
            overrides={
                ("GET", "/app"): (200, {"id": 42, "slug": "acme-worker"}),
                ("GET", "/repos/acme/widgets/installation"): (
                    200,
                    dict(INSTALLATION, account={"login": "acme"}),
                ),
            }
        )
        result = make_publisher(http, config=other).preflight()
        self.assertEqual(result["expected_author"], "acme-worker[bot]")
        self.assertEqual(result["repo"], "acme/widgets")


class TestPreflight(unittest.TestCase):
    def test_reports_access_without_minting_a_token(self):
        http = FakeGitHub()
        result = make_publisher(http).preflight()
        self.assertEqual(result["worker_access"], "ok")
        self.assertEqual(result["installation_id"], 77777)
        self.assertEqual(result["expected_author"], EXPECTED_AUTHOR)
        self.assertEqual(result["app_id"], APP_ID)
        self.assertEqual(
            http.count("POST", "/app/installations/77777/access_tokens"),
            0,
            "preflight must not mint an installation token",
        )


class TestFailClosed(unittest.TestCase):
    """Every unavailable-access path raises; none degrades to another identity."""

    def test_signing_failure(self):
        with self.assertRaises(wp.AuthError):
            make_publisher(signer=signer_fails).preflight()

    def test_missing_key_file_is_reported_as_auth_error(self):
        with self.assertRaises(wp.AuthError) as ctx:
            wp.openssl_signer("/nonexistent/definitely-not-a-key.pem", b"payload")
        self.assertIn("not found", str(ctx.exception))

    def test_app_jwt_rejected(self):
        http = FakeGitHub(overrides={("GET", "/app"): (401, {"message": "Bad credentials"})})
        with self.assertRaises(wp.GitHubError) as ctx:
            make_publisher(http).preflight()
        self.assertEqual(ctx.exception.status, 401)

    def test_app_identity_mismatch(self):
        http = FakeGitHub(overrides={("GET", "/app"): (200, {"id": 999, "slug": "other-app"})})
        with self.assertRaises(wp.AccessError) as ctx:
            make_publisher(http).preflight()
        self.assertIn("App identity mismatch", str(ctx.exception))

    def test_app_slug_mismatch(self):
        http = FakeGitHub(
            overrides={("GET", "/app"): (200, {"id": APP_ID, "slug": "someone-else"})}
        )
        with self.assertRaises(wp.AccessError) as ctx:
            make_publisher(http).preflight()
        self.assertIn("App slug mismatch", str(ctx.exception))

    def test_installation_not_found(self):
        http = FakeGitHub(
            overrides={
                ("GET", "/repos/example-org/example-repo/installation"): (
                    404,
                    {"message": "Not Found"},
                )
            }
        )
        with self.assertRaises(wp.GitHubError):
            make_publisher(http).preflight()

    def test_installation_without_id(self):
        http = FakeGitHub(
            overrides={("GET", "/repos/example-org/example-repo/installation"): (200, {})}
        )
        with self.assertRaises(wp.AccessError) as ctx:
            make_publisher(http).preflight()
        self.assertIn("No app installation", str(ctx.exception))

    def test_installation_account_mismatch(self):
        wrong = dict(INSTALLATION, account={"login": "someone-else"})
        http = FakeGitHub(
            overrides={("GET", "/repos/example-org/example-repo/installation"): (200, wrong)}
        )
        with self.assertRaises(wp.AccessError) as ctx:
            make_publisher(http).preflight()
        self.assertIn("Installation account mismatch", str(ctx.exception))

    def test_insufficient_installation_permission(self):
        weak = dict(INSTALLATION, permissions={"pull_requests": "read", "contents": "read"})
        http = FakeGitHub(
            overrides={("GET", "/repos/example-org/example-repo/installation"): (200, weak)}
        )
        with self.assertRaises(wp.AccessError) as ctx:
            make_publisher(http).preflight()
        self.assertIn("pull_requests", str(ctx.exception))

    def test_write_satisfies_a_read_requirement(self):
        generous = dict(
            INSTALLATION, permissions={"pull_requests": "write", "contents": "write"}
        )
        http = FakeGitHub(
            overrides={("GET", "/repos/example-org/example-repo/installation"): (200, generous)}
        )
        self.assertEqual(make_publisher(http).preflight()["worker_access"], "ok")

    def test_token_request_refused(self):
        http = FakeGitHub(
            overrides={
                ("POST", "/app/installations/77777/access_tokens"): (
                    403,
                    {"message": "Resource not accessible by integration"},
                )
            }
        )
        with self.assertRaises(wp.GitHubError):
            make_publisher(http).create_pull_request(**PUBLISH_KWARGS)

    def test_token_response_without_token(self):
        http = FakeGitHub(
            overrides={("POST", "/app/installations/77777/access_tokens"): (201, {})}
        )
        with self.assertRaises(wp.AccessError) as ctx:
            make_publisher(http).create_pull_request(**PUBLISH_KWARGS)
        self.assertIn("no token", str(ctx.exception))

    def test_token_permission_downgrade_stops_before_creation(self):
        weak = dict(TOKEN_RESPONSE, permissions={"pull_requests": "read"})
        http = FakeGitHub(
            overrides={("POST", "/app/installations/77777/access_tokens"): (201, weak)}
        )
        with self.assertRaises(wp.AccessError), redirect_stderr(io.StringIO()):
            make_publisher(http).create_pull_request(**PUBLISH_KWARGS)
        self.assertEqual(http.count("POST", "/repos/example-org/example-repo/pulls"), 0)

    def test_token_scoped_to_the_wrong_repository(self):
        elsewhere = dict(TOKEN_RESPONSE, repositories=[{"full_name": "example-org/other"}])
        http = FakeGitHub(
            overrides={("POST", "/app/installations/77777/access_tokens"): (201, elsewhere)}
        )
        with self.assertRaises(wp.AccessError) as ctx, redirect_stderr(io.StringIO()):
            make_publisher(http).create_pull_request(**PUBLISH_KWARGS)
        self.assertIn("not scoped", str(ctx.exception))
        self.assertEqual(http.count("POST", "/repos/example-org/example-repo/pulls"), 0)

    def test_token_scope_must_be_exclusive_and_listed(self):
        target = {"full_name": "example-org/example-repo"}
        for label, response in (
            ("repositories omitted", {k: v for k, v in TOKEN_RESPONSE.items() if k != "repositories"}),
            ("repositories null", dict(TOKEN_RESPONSE, repositories=None)),
            ("repositories empty", dict(TOKEN_RESPONSE, repositories=[])),
            ("repositories not a list", dict(TOKEN_RESPONSE, repositories=target)),
            ("target plus an extra", dict(TOKEN_RESPONSE, repositories=[target, {"full_name": "example-org/other"}])),
            ("target listed twice", dict(TOKEN_RESPONSE, repositories=[target, target])),
            ("non-dict entry", dict(TOKEN_RESPONSE, repositories=["example-org/example-repo"])),
            ("entry without a name", dict(TOKEN_RESPONSE, repositories=[{"id": 1}])),
        ):
            with self.subTest(label):
                http = FakeGitHub(
                    overrides={("POST", "/app/installations/77777/access_tokens"): (201, response)}
                )
                with self.assertRaises(wp.AccessError), redirect_stderr(io.StringIO()):
                    make_publisher(http).create_pull_request(**PUBLISH_KWARGS)
                self.assertEqual(http.count("POST", "/repos/example-org/example-repo/pulls"), 0)
                self.assertEqual(http.count("DELETE", "/installation/token"), 1)

    def test_token_repo_check_mismatch(self):
        http = FakeGitHub(
            overrides={
                ("GET", "/repos/example-org/example-repo"): (200, {"full_name": "someone/else"})
            }
        )
        with self.assertRaises(wp.AccessError) as ctx, redirect_stderr(io.StringIO()):
            make_publisher(http).create_pull_request(**PUBLISH_KWARGS)
        self.assertIn("repository check failed", str(ctx.exception))


class TestAuthorVerification(unittest.TestCase):
    """The created PR must be authored by the worker bot, exactly."""

    def test_success_path_verifies_the_bot_author(self):
        http = FakeGitHub()
        with redirect_stderr(io.StringIO()):
            result = make_publisher(http).create_pull_request(**PUBLISH_KWARGS)
        self.assertEqual(result["author"], EXPECTED_AUTHOR)
        self.assertEqual(result["author_type"], "Bot")
        self.assertTrue(result["author_verified"])
        self.assertEqual(
            http.count("GET", "/repos/example-org/example-repo/pulls/150"),
            1,
            "the author must be re-read, not taken from the creation response",
        )

    def test_author_mismatches_are_hard_stops(self):
        for label, user in (
            ("human user", {"login": "example-org", "type": "User"}),
            ("different bot", {"login": "other-app[bot]", "type": "Bot"}),
            ("right login, not a bot", {"login": EXPECTED_AUTHOR, "type": "User"}),
        ):
            with self.subTest(label):
                http = FakeGitHub(
                    overrides={
                        ("GET", "/repos/example-org/example-repo/pulls/150"): (
                            200,
                            dict(VERIFIED_PR, user=user),
                        )
                    }
                )
                with self.assertRaises(wp.AuthorMismatchError) as ctx, redirect_stderr(
                    io.StringIO()
                ):
                    make_publisher(http).create_pull_request(**PUBLISH_KWARGS)
                self.assertIn("NOT ready", str(ctx.exception))
                self.assertEqual(
                    ctx.exception.pr,
                    {"number": 150, "url": VERIFIED_PR["html_url"]},
                    "the created PR is reported so the operator can handle it",
                )
                self.assertEqual(
                    http.count("POST", "/repos/example-org/example-repo/pulls"),
                    1,
                    "a mismatch is a hard stop, not a reason to publish again",
                )
                self.assertEqual(http.count("DELETE", "/installation/token"), 1)


class TestTokenLifecycle(unittest.TestCase):
    """Fresh token per operation, revoked on every path, never leaked."""

    def test_token_is_minted_after_identity_and_before_creation(self):
        http = FakeGitHub()
        with redirect_stderr(io.StringIO()):
            make_publisher(http).create_pull_request(**PUBLISH_KWARGS)
        paths = http.paths
        identity = paths.index(("GET", "/app"))
        mint = paths.index(("POST", "/app/installations/77777/access_tokens"))
        create = paths.index(("POST", "/repos/example-org/example-repo/pulls"))
        self.assertLess(identity, mint)
        self.assertLess(mint, create)

    def test_each_operation_mints_and_revokes_its_own_token(self):
        publisher = make_publisher(FakeGitHub())
        with redirect_stderr(io.StringIO()):
            publisher.create_pull_request(**PUBLISH_KWARGS)
            publisher.create_pull_request(**PUBLISH_KWARGS)
        self.assertEqual(publisher.tokens_minted, 2)
        self.assertEqual(publisher.tokens_revoked, 2)

    def test_token_revoked_on_success_and_failure(self):
        for label, overrides in (
            ("success", None),
            (
                "creation fails",
                {("POST", "/repos/example-org/example-repo/pulls"): (422, {"message": "x"})},
            ),
        ):
            with self.subTest(label):
                http = FakeGitHub(overrides=overrides)
                stderr = io.StringIO()
                with redirect_stderr(stderr):
                    try:
                        make_publisher(http).create_pull_request(**PUBLISH_KWARGS)
                    except wp.GitHubError:
                        pass
                self.assertEqual(http.count("DELETE", "/installation/token"), 1)

    def test_failing_revocation_does_not_mask_the_real_error(self):
        http = FakeGitHub(
            overrides={
                ("POST", "/repos/example-org/example-repo/pulls"): (500, {"message": "oops"}),
                ("DELETE", "/installation/token"): (500, None),
            }
        )
        publisher = make_publisher(http)
        with self.assertRaises(wp.TokenRevocationError) as ctx, redirect_stderr(io.StringIO()):
            publisher.create_pull_request(**PUBLISH_KWARGS)
        self.assertEqual(publisher.tokens_revoked, 0)
        message = str(ctx.exception)
        self.assertIn("oops", message, "the earlier failure stays visible")
        self.assertIn("no PR was created", message)
        self.assertIsNone(ctx.exception.pr)
        self.assertIsInstance(ctx.exception.__cause__, wp.GitHubError)

    def test_failed_revocation_after_creation_is_never_success(self):
        http = FakeGitHub(overrides={("DELETE", "/installation/token"): (500, None)})
        publisher = make_publisher(http)
        with self.assertRaises(wp.TokenRevocationError) as ctx, redirect_stderr(io.StringIO()):
            publisher.create_pull_request(**PUBLISH_KWARGS)
        self.assertEqual(ctx.exception.pr, {"number": 150, "url": VERIFIED_PR["html_url"]})
        self.assertIn("PR #150", str(ctx.exception))
        self.assertIn("do not publish again", str(ctx.exception))
        self.assertEqual(http.count("POST", "/repos/example-org/example-repo/pulls"), 1)

    def test_failed_revocation_after_author_mismatch_keeps_both(self):
        http = FakeGitHub(
            overrides={
                ("GET", "/repos/example-org/example-repo/pulls/150"): (
                    200,
                    dict(VERIFIED_PR, user={"login": "example-org", "type": "User"}),
                ),
                ("DELETE", "/installation/token"): (500, None),
            }
        )
        with self.assertRaises(wp.TokenRevocationError) as ctx, redirect_stderr(io.StringIO()):
            make_publisher(http).create_pull_request(**PUBLISH_KWARGS)
        self.assertEqual(ctx.exception.pr["number"], 150)
        self.assertIn("NOT ready", str(ctx.exception))
        self.assertIsInstance(ctx.exception.__cause__, wp.AuthorMismatchError)

    def test_the_token_is_used_only_for_repo_scoped_calls(self):
        http = FakeGitHub()
        with redirect_stderr(io.StringIO()):
            make_publisher(http).create_pull_request(**PUBLISH_KWARGS)
        for (method, path, _), token in zip(http.calls, http.tokens_seen):
            with self.subTest(f"{method} {path}"):
                if path == "/app" or path.endswith("/installation") or "access_tokens" in path:
                    self.assertNotEqual(token, FAKE_TOKEN)
                else:
                    self.assertEqual(token, FAKE_TOKEN)


class TestNoLeakAndNoFallback(unittest.TestCase):
    def test_http_error_carries_status_and_message_only(self):
        body = {
            "message": "Validation Failed",
            "errors": [{"resource": "PullRequest", "field": "head"}],
            "documentation_url": "https://docs.github.com/...",
        }
        http = FakeGitHub(overrides={("POST", "/repos/example-org/example-repo/pulls"): (422, body)})
        with self.assertRaises(wp.GitHubError) as ctx, redirect_stderr(io.StringIO()):
            make_publisher(http).create_pull_request(**PUBLISH_KWARGS)
        message = str(ctx.exception)
        self.assertIn("422", message)
        self.assertIn("Validation Failed", message)
        self.assertNotIn("documentation_url", message)
        self.assertNotIn("PullRequest", message)

    def test_sanitize_truncates_and_ignores_odd_bodies(self):
        self.assertLessEqual(len(wp._sanitize({"message": "x" * 5000})), wp.MAX_ERROR_CHARS + 3)
        for body in (None, "raw text", ["a"], {"message": {"nested": 1}}, {}):
            self.assertEqual(wp._sanitize(body), "")

    def test_token_never_reaches_output(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = make_publisher(FakeGitHub()).create_pull_request(**PUBLISH_KWARGS)
        self.assertNotIn(FAKE_TOKEN, stdout.getvalue() + stderr.getvalue() + json.dumps(result))

    def test_source_has_no_gh_or_user_credential_fallback(self):
        code = PUBLISHER_PATH.read_text().split('"""', 2)[2]
        for forbidden in ("GH_TOKEN", "GITHUB_TOKEN", "gh pr create", "gh auth"):
            with self.subTest(forbidden):
                self.assertNotIn(forbidden, code)

    def test_openssl_is_the_only_subprocess(self):
        source = PUBLISHER_PATH.read_text()
        self.assertEqual(source.count("subprocess.run"), 1)
        self.assertIn('"openssl", "dgst", "-sha256", "-sign"', source)


class TestCli(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile("w", suffix=".md", delete=False)
        handle.write("test body\n")
        handle.close()
        self.body_file = handle.name
        self.addCleanup(os.unlink, self.body_file)
        # A synthetic local config, as an operator's own worker-publisher.json would be.
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(VALID_RAW, handle)
        handle.close()
        self.config_file = handle.name
        self.addCleanup(os.unlink, self.config_file)

    def run_cli(self, argv, publisher=None):
        if "--config" not in argv:
            argv = [*argv, "--config", self.config_file]
        if publisher is not None:
            original = wp.Publisher
            wp.Publisher = lambda *a, **k: publisher
            self.addCleanup(lambda: setattr(wp, "Publisher", original))
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = wp.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_preflight_only_reports_ok_and_mints_nothing(self):
        http = FakeGitHub()
        code, out, _ = self.run_cli(["--preflight-only"], make_publisher(http))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["worker_access"], "ok")
        self.assertEqual(http.count("POST", "/app/installations/77777/access_tokens"), 0)

    def test_preflight_failure_marks_the_release_blocked(self):
        http = FakeGitHub(overrides={("GET", "/app"): (401, {"message": "Bad credentials"})})
        code, out, _ = self.run_cli(["--preflight-only"], make_publisher(http))
        self.assertEqual(code, 1)
        payload = json.loads(out)
        self.assertEqual(payload["release_gate"], "Blocked")
        self.assertIn("do not publish", payload["fallback"])

    def test_missing_config_blocks_before_any_request(self):
        code, out, _ = self.run_cli(
            ["--preflight-only", "--config", "/nonexistent/worker-publisher.json"]
        )
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["release_gate"], "Blocked")

    def test_publication_requires_execute(self):
        http = FakeGitHub()
        code, out, _ = self.run_cli(
            ["--title", "t", "--head", "b", "--body-file", self.body_file],
            make_publisher(http),
        )
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["dry_run"])
        self.assertEqual(http.calls, [], "a dry run makes no GitHub call at all")

    def test_execute_publishes_and_prints_the_verified_author(self):
        code, out, _ = self.run_cli(
            ["--title", "t", "--head", "b", "--body-file", self.body_file, "--execute"],
            make_publisher(FakeGitHub()),
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["author"], EXPECTED_AUTHOR)

    def test_execute_failure_exits_1_and_reports_blocked(self):
        http = FakeGitHub(
            overrides={
                ("GET", "/repos/example-org/example-repo/pulls/150"): (
                    200,
                    dict(VERIFIED_PR, user={"login": "example-org", "type": "User"}),
                )
            }
        )
        code, out, _ = self.run_cli(
            ["--title", "t", "--head", "b", "--body-file", self.body_file, "--execute"],
            make_publisher(http),
        )
        self.assertEqual(code, 1)
        payload = json.loads(out)
        self.assertEqual(payload["release_gate"], "Blocked")
        self.assertEqual(payload["pr_number"], 150)

    def test_failed_revocation_is_blocked_with_the_created_pr(self):
        http = FakeGitHub(overrides={("DELETE", "/installation/token"): (500, None)})
        code, out, _ = self.run_cli(
            ["--title", "t", "--head", "b", "--body-file", self.body_file, "--execute"],
            make_publisher(http),
        )
        self.assertEqual(code, 1, "a PR created without confirmed revocation is not success")
        payload = json.loads(out)
        self.assertEqual(payload["release_gate"], "Blocked")
        self.assertTrue(payload["pr_created"])
        self.assertEqual(payload["pr_number"], 150)
        self.assertEqual(payload["pr_url"], VERIFIED_PR["html_url"])
        self.assertIn("do not re-run", payload["retry"])
        self.assertNotIn("author_verified", payload)

    def test_blocked_without_a_pr_reports_no_pr_fields(self):
        http = FakeGitHub(
            overrides={("POST", "/repos/example-org/example-repo/pulls"): (422, {"message": "x"})}
        )
        code, out, _ = self.run_cli(
            ["--title", "t", "--head", "b", "--body-file", self.body_file, "--execute"],
            make_publisher(http),
        )
        self.assertEqual(code, 1)
        self.assertNotIn("pr_number", json.loads(out))

    def test_missing_publication_arguments_is_a_usage_error(self):
        with self.assertRaises(SystemExit) as ctx:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                wp.main(["--title", "only a title"])
        self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
