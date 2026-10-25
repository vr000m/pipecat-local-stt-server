"""``python -m stt_server prefetch`` (lean).

Nothing touches the network or the real Hugging Face cache: the download seam
``stt_server.prefetch._snapshot_download`` builds fake snapshot folders under
``tmp_path``, and the progress seams (``_remote_files``, ``_repo_cache_dir``)
are stubbed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from stt_server import __main__ as M
from stt_server import prefetch as PF

# The files each fake snapshot gets (enough to satisfy every backend's ``required``).
_FILES = {
    "config.json": b"{}",
    "weights.safetensors": b"w" * 100,
    "model.safetensors": b"m" * 200,
    "mimi-x@1.safetensors": b"i" * 50,
    "tokenizer.model": b"t" * 10,
}


def _run(monkeypatch, capsys, *argv: str) -> tuple[int, list[dict], str]:
    monkeypatch.setattr(sys, "argv", ["stt_server", "prefetch", *argv])
    code = 0
    try:
        M.main()
    except SystemExit as exc:
        code = int(exc.code or 0)
    out, err = capsys.readouterr()
    return code, [json.loads(line) for line in out.splitlines() if line.strip()], err


@pytest.fixture
def fake_hub(tmp_path, monkeypatch):
    """Stub the hub. ``cached`` holds repo ids already "on disk"."""
    cached: set[str] = set()
    calls: list[tuple[str, bool]] = []

    def fake(spec, *, local_files_only):
        calls.append((spec.repo_id, local_files_only))
        snap = tmp_path / "hub" / spec.repo_id.replace("/", "--")
        if local_files_only and spec.repo_id not in cached:
            raise FileNotFoundError(f"{spec.repo_id} not cached")
        if not local_files_only:
            snap.mkdir(parents=True, exist_ok=True)
            for name, data in _FILES.items():
                (snap / name).write_bytes(data)
            cached.add(spec.repo_id)
        return str(snap)

    monkeypatch.setattr(PF, "_snapshot_download", fake)
    monkeypatch.setattr(PF, "_remote_files", lambda spec: [(n, len(d)) for n, d in _FILES.items()])
    monkeypatch.setattr(
        PF, "_repo_cache_dir", lambda repo: tmp_path / "cache" / repo.replace("/", "--")
    )
    return cached, calls


def test_specs_echo_needs_nothing():
    assert PF.specs_for("echo", None) == []


@pytest.mark.parametrize(
    ("backend", "repo"),
    [
        ("mlx", "mlx-community/whisper-large-v3-turbo"),
        ("parakeet", "mlx-community/parakeet-tdt-0.6b-v3"),
        ("nemotron", "mlx-community/nemotron-3.5-asr-streaming-0.6b"),
        ("kyutai", "kyutai/stt-1b-en_fr-mlx"),
    ],
)
def test_specs_default_repos(backend, repo):
    (spec,) = PF.specs_for(backend, None)
    assert spec.repo_id == repo
    assert spec.required, "every model spec names the files that make it complete"


def test_specs_parakeet_fetches_only_its_two_files():
    (spec,) = PF.specs_for("parakeet", None)
    assert spec.allow_patterns == ("config.json", "model.safetensors")


def test_specs_explicit_model_wins():
    (spec,) = PF.specs_for("mlx", "mlx-community/whisper-small")
    assert spec.repo_id == "mlx-community/whisper-small"


def test_mlx_default_matches_cli_default():
    from stt_server.backends.mlx_whisper import DEFAULT_MLX_MODEL

    assert DEFAULT_MLX_MODEL == M._DEFAULT_MLX_MODEL


def test_check_json_uncached_then_cached(fake_hub, monkeypatch, capsys):
    _cached, calls = fake_hub
    code, events, _ = _run(monkeypatch, capsys, "--backend", "kyutai", "--check", "--json")
    assert code == 0
    assert [e["event"] for e in events] == ["check", "summary"]
    assert events[-1]["cached"] is False
    assert all(local for _repo, local in calls)  # --check never goes online

    code, events, _ = _run(monkeypatch, capsys, "--backend", "kyutai", "--json")
    assert code == 0
    code, events, _ = _run(monkeypatch, capsys, "--backend", "kyutai", "--check", "--json")
    summary = events[-1]
    assert summary["cached"] is True
    assert summary["size_bytes"] == sum(len(d) for d in _FILES.values())


def test_check_incomplete_snapshot_is_not_cached(fake_hub, tmp_path, monkeypatch, capsys):
    cached, _ = fake_hub
    snap = tmp_path / "hub" / "kyutai--stt-1b-en_fr-mlx"
    snap.mkdir(parents=True)
    (snap / "config.json").write_text("{}")  # an interrupted download: no weights
    cached.add("kyutai/stt-1b-en_fr-mlx")
    code, events, _ = _run(monkeypatch, capsys, "--backend", "kyutai", "--check", "--json")
    assert code == 0 and events[-1]["cached"] is False


def test_check_echo_is_always_cached(monkeypatch, capsys):
    code, events, _ = _run(monkeypatch, capsys, "--backend", "echo", "--check", "--json")
    assert code == 0
    assert events == [
        {"event": "summary", "backend": "echo", "model": None, "cached": True, "size_bytes": 0}
    ]


def test_fetch_json_event_order(fake_hub, monkeypatch, capsys):
    monkeypatch.setattr(PF, "_PROGRESS_INTERVAL_S", 0.01)
    code, events, _ = _run(monkeypatch, capsys, "--backend", "parakeet", "--json")
    assert code == 0
    kinds = [e["event"] for e in events]
    assert kinds[0] == "start" and kinds[-2:] == ["done", "complete"]
    assert set(kinds) <= {"start", "progress", "done", "complete"}
    for e in events:
        if e["event"] == "progress":
            assert 0 <= e["downloaded_bytes"] <= e["total_bytes"]
    assert events[-1]["backend"] == "parakeet" and events[-1]["cached"] is True


def test_fetch_failure_is_error_event(monkeypatch, capsys):
    class _HubError(Exception):
        pass

    _HubError.__module__ = "huggingface_hub.errors"

    def broken(spec, *, local_files_only):
        raise _HubError("503 Service Unavailable")

    monkeypatch.setattr(PF, "_snapshot_download", broken)
    monkeypatch.setattr(PF, "_remote_files", lambda spec: [])
    monkeypatch.setattr(PF, "_repo_cache_dir", lambda repo: Path("/nonexistent"))
    code, events, _ = _run(monkeypatch, capsys, "--backend", "mlx", "--json")
    assert code == 1
    err = events[-1]
    assert err["event"] == "error" and err["code"] == "ModelDownloadError"
    assert err["repo"] == "mlx-community/whisper-large-v3-turbo"


def test_missing_huggingface_hub_is_clean_error(monkeypatch, capsys):
    def no_hub(spec, *, local_files_only):
        raise ModuleNotFoundError("No module named 'huggingface_hub'", name="huggingface_hub")

    monkeypatch.setattr(PF, "_snapshot_download", no_hub)
    code, events, _ = _run(monkeypatch, capsys, "--backend", "kyutai", "--check", "--json")
    assert code == 1
    assert events[-1]["event"] == "error" and events[-1]["code"] == "ModuleNotFoundError"


def test_local_model_path_needs_no_download(fake_hub, tmp_path, monkeypatch, capsys):
    _cached, calls = fake_hub
    code, events, _ = _run(
        monkeypatch, capsys, "--backend", "mlx", "--model", str(tmp_path), "--check", "--json"
    )
    assert code == 0 and events[-1]["cached"] is True and calls == []


def test_human_check_output(fake_hub, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["stt_server", "prefetch", "--backend", "parakeet", "--check"])
    M.main()
    out = capsys.readouterr().out
    assert "mlx-community/parakeet-tdt-0.6b-v3@main: missing" in out
    assert "parakeet: not cached" in out


def test_prefetch_requires_backend(monkeypatch, capsys):
    code, _events, err = _run(monkeypatch, capsys)
    assert code == 2 and "--backend" in err
