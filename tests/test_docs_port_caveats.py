"""Phase 4 (docs and release) acceptance checks for the loopback-TCP plan.

Greps the docs for the agreed literal phrases, checks that ``pyproject.toml``
and ``uv.lock`` agree on the version (0.5.0 or later) with a matching
``CHANGELOG.md`` section, and checks the 0.5.0 upgrade notes.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OPERATIONS = REPO_ROOT / "docs" / "operations.md"
MIGRATION = REPO_ROOT / "docs" / "migration.md"
CHANGELOG = REPO_ROOT / "CHANGELOG.md"
PLANS_README = REPO_ROOT / "docs" / "dev_plans" / "README.md"
# The release that shipped the loopback-TCP agents; a floor, not a pin.
VERSION = "0.5.0"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_operations_states_no_peer_uid_check() -> None:
    assert "no peer-uid check" in _read(OPERATIONS).lower()


def test_operations_has_restart_loop_note() -> None:
    text = _read(OPERATIONS).lower()
    assert "throttleinterval" in text
    assert "every 10 seconds" in text


def test_operations_headings_renamed() -> None:
    text = _read(OPERATIONS)
    lower = text.lower()
    assert "is not wired into the launchagent tooling" not in lower
    assert not re.search(r"^#+\s*per-asr socket convention", lower, re.MULTILINE)
    assert re.search(r"^#+\s*per-backend port convention", lower, re.MULTILINE)


def _project_version() -> str:
    return tomllib.loads(_read(REPO_ROOT / "pyproject.toml"))["project"]["version"]


def test_version_consistent_across_files() -> None:
    # Not pinned to one release: pyproject and uv.lock must agree, and the
    # current version must be at least the release that shipped this plan.
    version = _project_version()
    lock = tomllib.loads(_read(REPO_ROOT / "uv.lock"))
    pkgs = [p for p in lock["package"] if p["name"] == "pipecat-local-stt-server"]
    assert len(pkgs) == 1
    assert pkgs[0]["version"] == version
    as_tuple = tuple(int(x) for x in re.findall(r"\d+", version)[:3])
    assert as_tuple >= tuple(int(x) for x in VERSION.split(".")), version


def test_changelog_has_current_version_section() -> None:
    version = re.escape(_project_version())
    assert re.search(rf"^## \[{version}\]", _read(CHANGELOG), re.MULTILINE), (
        f"CHANGELOG.md has no ## [{_project_version()}] section"
    )


def test_changelog_0_5_0_section() -> None:
    # The 0.5.0 upgrade notes, wherever the section sits once later releases land.
    text = _read(CHANGELOG)
    m = re.search(r"^## \[0\.5\.0\].*?(?=^## \[|\Z)", text, re.MULTILINE | re.DOTALL)
    assert m is not None
    section = m.group(0)
    assert "PIPECAT_STT_SOCKET" in section
    lower = section.lower()
    assert "pipecat_stt_auth_token_file" in lower or "token file" in lower


def test_migration_has_rollback_line() -> None:
    assert "PIPECAT_STT_SOCKET=<path> just stt-install <backend>" in _read(MIGRATION)


def test_dev_plans_readme_lists_plan() -> None:
    assert "20261025-feature-stt-agents-loopback-tcp" in _read(PLANS_README)


def test_no_doc_claims_not_wired() -> None:
    files = [REPO_ROOT / "README.md", *sorted((REPO_ROOT / "docs").glob("*.md"))]
    offenders = [f.name for f in files if "not wired" in _read(f).lower()]
    assert offenders == []
