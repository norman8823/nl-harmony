"""Read-only readiness checks for Harmony's guided local onboarding.

A successful check means that a prerequisite is present, not that a model run
has succeeded or an authenticated multi-agent workflow has been verified.
No CLI processes, model calls, or installation commands are run here.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from . import codex_bridge


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()


def install_setup_skill(home: Path | None = None) -> tuple[str, Path]:
    """Link the bundled skill into Codex, without replacing user-owned content."""
    home = home or codex_home()
    source = Path(__file__).resolve().parent / "skills" / "harmony-setup"
    target = home / "skills" / "harmony-setup"
    if not (source / "SKILL.md").is_file():
        raise ValueError("bundled harmony-setup skill is missing")
    if target.is_symlink():
        if target.resolve() == source.resolve():
            return "installed", target
        return "conflict", target
    if target.exists():
        return "conflict", target
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(source, target_is_directory=True)
    return "installed", target


def report(home: Path | None = None) -> dict:
    """Return stable statuses. Unknown authentication is never called ready."""
    home = home or codex_home()
    hook = False
    try:
        hook = codex_bridge.is_installed(home)
    except (OSError, ValueError):
        pass
    skill = (home / "skills" / "harmony-setup" / "SKILL.md").is_file()
    checks = [
        {
            "id": "codex",
            "label": "Codex CLI",
            "state": "ready" if shutil.which("codex") else "missing",
            "detail": "Found on PATH" if shutil.which("codex") else "Install and sign in to Codex CLI",
        },
        {
            "id": "connection",
            "label": "Codex connection",
            "state": "ready" if hook else "missing",
            "detail": "Hook configured; trust approval is checked in Codex" if hook
                      else "Run bash install-local.sh, then restart Codex",
        },
        {
            "id": "skill",
            "label": "Setup skill",
            "state": "ready" if skill else "missing",
            "detail": "Use $harmony-setup in a new Codex session" if skill
                      else "Re-run bash install-local.sh to install the skill",
        },
        {
            "id": "claude",
            "label": "Claude Code CLI",
            "state": "ready" if shutil.which("claude") else "missing",
            "detail": "Found on PATH; authentication still needs verification" if shutil.which("claude")
                      else "Install Claude Code and sign in",
        },
    ]
    return {
        "checks": checks,
        "ready_for_assisted_setup": all(c["state"] == "ready" for c in checks),
        "multi_agent_verified": False,
        "note": "CLI discovery is not proof of authentication or a completed multi-agent run.",
        "skill_command": "$harmony-setup",
    }
