"""Documentation ownership, navigation, and README landing-page checks."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from urllib.parse import unquote, urlsplit

REPO_ROOT = Path(__file__).resolve().parent.parent
README = REPO_ROOT / "README.md"
DOCS = sorted((REPO_ROOT / "docs").glob("*.md"))
MARKDOWN = [README, *DOCS]

_LINK_RE = re.compile(r"(?<!!)\[[^\]]+\]\((<[^>]+>|[^\s)]+)")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _anchors(path: Path) -> set[str]:
    """Return GitHub-style heading slugs, excluding fenced code blocks."""
    anchors: set[str] = set()
    counts: Counter[str] = Counter()
    fenced = False
    for line in _text(path).splitlines():
        if line.startswith("```"):
            fenced = not fenced
            continue
        if fenced or (match := _HEADING_RE.match(line)) is None:
            continue
        heading = re.sub(r"<[^>]+>", "", match.group(2)).replace("`", "")
        slug = re.sub(r"[^\w\- ]", "", heading.lower(), flags=re.UNICODE)
        slug = re.sub(r"\s+", "-", slug.strip())
        suffix = counts[slug]
        counts[slug] += 1
        anchors.add(f"{slug}-{suffix}" if suffix else slug)
    return anchors


def test_relative_markdown_links_and_anchors_resolve() -> None:
    failures: list[str] = []
    for source in MARKDOWN:
        for line_number, line in enumerate(_text(source).splitlines(), 1):
            for raw_target in _LINK_RE.findall(line):
                target = raw_target.removeprefix("<").removesuffix(">")
                parsed = urlsplit(target)
                if parsed.scheme or parsed.netloc:
                    continue
                relative = unquote(parsed.path)
                destination = (source.parent / relative).resolve() if relative else source
                if not destination.exists():
                    failures.append(
                        f"{source.relative_to(REPO_ROOT)}:{line_number}: missing {target}"
                    )
                    continue
                if parsed.fragment and destination.is_file():
                    anchor = unquote(parsed.fragment)
                    if anchor not in _anchors(destination):
                        failures.append(
                            f"{source.relative_to(REPO_ROOT)}:{line_number}: "
                            f"missing anchor #{anchor} in "
                            f"{destination.relative_to(REPO_ROOT)}"
                        )
    assert failures == [], "\n".join(failures)


def test_readme_is_a_compact_safe_landing_page() -> None:
    text = _text(README)
    assert len(text.splitlines()) <= 180
    assert len(text.split()) <= 1_200
    assert "uv run --no-sync python -m stt_server serve" in text
    assert "--socket-path ~/Library/Caches/pipecat-stt/stt.sock" in text
    assert "--host 127.0.0.1 --port 9900" in text
    assert "Loopback TCP has no peer-uid check" in text
    assert "--auth-token-file /absolute/path/to/token" in text


def test_detailed_guides_do_not_depend_on_readme_anchors() -> None:
    offenders = [path.name for path in DOCS if re.search(r"(?:\.\./)?README\.md#", _text(path))]
    assert offenders == []


def test_detailed_topics_have_one_canonical_owner() -> None:
    expected = {
        "## Prefetching and cache checks": "backends.md",
        "## Whisper hallucination suppression": "backends.md",
        "### Stable process error codes": "architecture.md",
        "## Implementation checklist": "contributing.md",
    }
    for heading, owner in expected.items():
        actual = [path.name for path in DOCS if heading in _text(path)]
        assert actual == [owner], f"{heading!r} owners: {actual}"


def test_relocated_contract_tokens_are_preserved() -> None:
    backends = _text(REPO_ROOT / "docs" / "backends.md")
    architecture = _text(REPO_ROOT / "docs" / "architecture.md")
    contributing = _text(REPO_ROOT / "docs" / "contributing.md")

    for token in (
        "PINNED_REVISIONS",
        "mlx-community/whisper-large-v3-turbo",
        "mlx-community/parakeet-tdt-0.6b-v3",
        "mlx-community/nemotron-3.5-asr-streaming-0.6b",
        "kyutai/stt-1b-en_fr-mlx",
        "ModelInvalidError",
        "PIPECAT_STT_WHISPER_CONDITION_ON_PREVIOUS_TEXT",
        "PIPECAT_STT_WHISPER_DEGENERATE_TOKEN_RATIO",
    ):
        assert token in backends

    for code in (
        "EADDRINUSE",
        "EACCES",
        "ModuleNotFoundError",
        "ModelDownloadError",
        "ModelInvalidError",
        "ConfigError",
        "BackendStartError",
    ):
        assert code in architecture

    for token in (
        "TranscriptionBackend",
        "SnapshotSpec",
        "DecodeGuardMixin",
        "snapshot.download_phase()",
        "BackendEntry",
        "tests/test_backend_registry.py",
    ):
        assert token in contributing
