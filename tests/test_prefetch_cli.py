"""``python -m stt_server prefetch`` (lean).

Nothing touches the network or the real Hugging Face cache: the download seam
``stt_server.prefetch._snapshot_download`` builds fake snapshot folders under
``tmp_path``, and the progress seams (``_remote_files``, ``_repo_cache_dir``)
are stubbed.
"""

from __future__ import annotations

import fnmatch
import json
import sys
from pathlib import Path

import pytest

from stt_server import __main__ as M
from stt_server import prefetch as PF

# The files each fake snapshot gets (enough to satisfy every backend's ``required``).
_FILES = {
    # Kyutai's loader reads these names from config.json (moshi_name defaults to
    # model.safetensors); the other backends ignore the content.
    "config.json": b'{"mimi_name": "mimi-x@1.safetensors", "tokenizer_name": "tokenizer.model"}',
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
    monkeypatch.setattr(
        PF, "_remote_files", lambda spec: [(n, len(d), f"blob-{n}") for n, d in _FILES.items()]
    )
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


def _quantized_kyutai_hub(tmp_path, monkeypatch):
    """A fake hub whose Kyutai repo names a q4 LM and also holds other weights."""
    config = (
        b'{"mimi_name": "mimi-x.safetensors", "moshi_name": "model.q4.safetensors",'
        b' "tokenizer_name": "tok.model"}'
    )
    remote = {
        "config.json": config,
        "mimi-x.safetensors": b"i" * 50,
        "model.q4.safetensors": b"q" * 40,
        "model.q8.safetensors": b"8" * 80,
        "model.safetensors": b"m" * 200,
        "tok.model": b"t" * 10,
    }
    snap = tmp_path / "hub" / "kyutai-q4"
    downloaded: list[tuple[str, ...]] = []

    def fake(spec, *, local_files_only):
        if local_files_only:
            if not snap.exists():
                raise FileNotFoundError("not cached")
            return str(snap)
        downloaded.append(spec.allow_patterns)
        snap.mkdir(parents=True, exist_ok=True)
        for name, data in remote.items():
            if any(fnmatch.fnmatch(name, p) for p in spec.allow_patterns or ("*",)):
                (snap / name).write_bytes(data)
        return str(snap)

    monkeypatch.setattr(PF, "_snapshot_download", fake)
    monkeypatch.setattr(
        PF,
        "_remote_files",
        lambda spec: [
            (n, len(d), f"blob-{n}")
            for n, d in remote.items()
            if any(fnmatch.fnmatch(n, p) for p in spec.allow_patterns or ("*",))
        ],
    )
    monkeypatch.setattr(PF, "_repo_cache_dir", lambda repo: tmp_path / "cache" / "kyutai-q4")
    return snap, downloaded


def test_kyutai_prefetch_follows_quantized_moshi_name(tmp_path, monkeypatch, capsys):
    snap, downloaded = _quantized_kyutai_hub(tmp_path, monkeypatch)
    code, events, _ = _run(
        monkeypatch, capsys, "--backend", "kyutai", "--model", "kyutai/q4", "--json"
    )
    assert code == 0, events
    # config first, then exactly the files config.json names -- not every *.safetensors.
    assert downloaded[0] == ("config.json",)
    assert set(downloaded[1]) == {
        "config.json",
        "mimi-x.safetensors",
        "model.q4.safetensors",
        "tok.model",
    }
    assert sorted(p.name for p in snap.iterdir()) == sorted(downloaded[1])
    assert not (snap / "model.safetensors").exists()
    assert not (snap / "model.q8.safetensors").exists()
    progress = [e for e in events if e["event"] == "progress"]
    assert progress[-1]["files_total"] == 4  # config + mimi + q4 + tokenizer, not all six

    code, events, _ = _run(
        monkeypatch, capsys, "--backend", "kyutai", "--model", "kyutai/q4", "--check", "--json"
    )
    assert code == 0 and events[-1]["cached"] is True


def test_kyutai_check_uses_named_weights_not_model_safetensors(tmp_path, monkeypatch, capsys):
    snap, _ = _quantized_kyutai_hub(tmp_path, monkeypatch)
    snap.mkdir(parents=True)
    (snap / "config.json").write_bytes(
        b'{"mimi_name": "mimi-x.safetensors", "moshi_name": "model.q4.safetensors",'
        b' "tokenizer_name": "tok.model"}'
    )
    for name in ("mimi-x.safetensors", "tok.model", "model.safetensors"):
        (snap / name).write_bytes(b"x")
    # model.safetensors is present but the config names the q4 file, which is not.
    code, events, _ = _run(
        monkeypatch, capsys, "--backend", "kyutai", "--model", "kyutai/q4", "--check", "--json"
    )
    assert code == 0 and events[-1]["cached"] is False
    (snap / "model.q4.safetensors").write_bytes(b"x")
    code, events, _ = _run(
        monkeypatch, capsys, "--backend", "kyutai", "--model", "kyutai/q4", "--check", "--json"
    )
    assert events[-1]["cached"] is True


def test_check_echo_is_always_cached(monkeypatch, capsys):
    code, events, _ = _run(monkeypatch, capsys, "--backend", "echo", "--check", "--json")
    assert code == 0
    assert events == [
        {"event": "summary", "backend": "echo", "model": None, "cached": True, "size_bytes": 0}
    ]


@pytest.mark.parametrize("check", [True, False])
@pytest.mark.parametrize("model", ["/definitely/missing/model", "some/repo"])
def test_echo_refuses_an_explicit_model(monkeypatch, capsys, check, model):
    """Echo takes no model: an explicit ``--model`` (local path or repo id) is
    a ConfigError in check and fetch, never silently dropped and reported
    cached / complete."""
    argv = ["--backend", "echo", "--model", model, "--json"]
    if check:
        argv.append("--check")
    code, events, _ = _run(monkeypatch, capsys, *argv)
    assert code == 1
    assert events[-1]["event"] == "error" and events[-1]["code"] == "ConfigError"
    assert not any(e["event"] in ("summary", "complete") for e in events)


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


def test_fetch_progress_counts_only_uncached_and_reaches_total(tmp_path, monkeypatch, capsys):
    """Already-cached blobs are excluded from total/files_total (TTS semantics);
    a partial .incomplete blob counts toward downloaded; the last progress event
    before ``done`` is complete."""
    monkeypatch.setattr(PF, "_PROGRESS_INTERVAL_S", 0.01)
    cache = tmp_path / "cache" / "mlx-community--parakeet-tdt-0.6b-v3"
    blobs = cache / "blobs"
    blobs.mkdir(parents=True)
    (blobs / "blob-config.json").write_bytes(b"{}")  # already cached
    (blobs / "blob-model.safetensors.incomplete").write_bytes(b"m" * 80)  # interrupted
    monkeypatch.setattr(PF, "_repo_cache_dir", lambda repo: cache)
    monkeypatch.setattr(
        PF,
        "_remote_files",
        lambda spec: [
            ("config.json", 2, "blob-config.json"),
            ("model.safetensors", 200, "blob-model.safetensors"),
        ],
    )

    def fake(spec, *, local_files_only):
        # Let the reporter observe the partial blob, then finish the download.
        import time

        time.sleep(0.1)
        (blobs / "blob-model.safetensors.incomplete").rename(blobs / "blob-model.safetensors")
        (blobs / "blob-model.safetensors").write_bytes(b"m" * 200)
        snap = tmp_path / "hub" / "p"
        snap.mkdir(parents=True, exist_ok=True)
        for name in ("config.json", "model.safetensors"):
            (snap / name).write_bytes(b"x")
        return str(snap)

    monkeypatch.setattr(PF, "_snapshot_download", fake)
    code, events, _ = _run(monkeypatch, capsys, "--backend", "parakeet", "--json")
    assert code == 0
    progress = [e for e in events if e["event"] == "progress"]
    assert progress, events
    assert all(e["total_bytes"] == 200 and e["files_total"] == 1 for e in progress)
    assert progress[0]["downloaded_bytes"] == 80 and progress[0]["files_done"] == 0
    last = progress[-1]
    assert last["downloaded_bytes"] == last["total_bytes"] == 200
    assert last["files_done"] == last["files_total"] == 1
    assert [e["event"] for e in events[-3:]] == ["progress", "done", "complete"]


def test_fetch_progress_unknown_blob_ids_still_reach_total(fake_hub, monkeypatch, capsys):
    monkeypatch.setattr(PF, "_remote_files", lambda spec: [("model.safetensors", 200)])
    code, events, _ = _run(monkeypatch, capsys, "--backend", "parakeet", "--json")
    assert code == 0
    last = [e for e in events if e["event"] == "progress"][-1]
    assert last["downloaded_bytes"] == last["total_bytes"] == 200
    assert last["files_done"] == last["files_total"] == 1


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


def test_fetch_builtin_connection_error_is_download_error(monkeypatch, capsys):
    """A bare ConnectionError during a fetch is a download failure (phase-tagged)."""

    def reset(spec, *, local_files_only):
        raise ConnectionResetError("peer reset")

    monkeypatch.setattr(PF, "_snapshot_download", reset)
    monkeypatch.setattr(PF, "_remote_files", lambda spec: [])
    monkeypatch.setattr(PF, "_repo_cache_dir", lambda repo: Path("/nonexistent"))
    code, events, _ = _run(monkeypatch, capsys, "--backend", "mlx", "--json")
    assert code == 1 and events[-1]["code"] == "ModelDownloadError"


def test_missing_huggingface_hub_is_clean_error(monkeypatch, capsys):
    def no_hub(spec, *, local_files_only):
        raise ModuleNotFoundError("No module named 'huggingface_hub'", name="huggingface_hub")

    monkeypatch.setattr(PF, "_snapshot_download", no_hub)
    code, events, _ = _run(monkeypatch, capsys, "--backend", "kyutai", "--check", "--json")
    assert code == 1
    assert events[-1]["event"] == "error" and events[-1]["code"] == "ModuleNotFoundError"


@pytest.mark.parametrize("check", [True, False])
def test_malformed_repo_id_is_config_error_in_check_and_fetch(monkeypatch, capsys, check):
    """``--check`` must not turn a malformed Hub repo id into ``cached: false``
    (exit 0): both prefetch paths report ``ConfigError``."""

    class HFValidationError(ValueError):
        pass

    HFValidationError.__module__ = "huggingface_hub.errors"

    def invalid(spec, *, local_files_only):
        raise HFValidationError("Repo id must be in the form 'repo_name'")

    monkeypatch.setattr(PF, "_snapshot_download", invalid)
    monkeypatch.setattr(PF, "_remote_files", lambda spec: [])
    monkeypatch.setattr(PF, "_repo_cache_dir", lambda repo: Path("/nonexistent"))
    args = ["--backend", "mlx", "--model", "bad repo", "--json"] + (["--check"] if check else [])
    code, events, _ = _run(monkeypatch, capsys, *args)
    assert code == 1
    assert [e["event"] for e in events if e["event"] in ("check", "summary")] == []
    assert events[-1]["event"] == "error" and events[-1]["code"] == "ConfigError"
    assert events[-1]["repo"] == "bad repo"


def _complete_local_model(path: Path) -> Path:
    path.mkdir()
    for name, data in _FILES.items():
        (path / name).write_bytes(data)
    return path


@pytest.mark.parametrize("backend", ["mlx", "parakeet", "nemotron"])
def test_complete_local_model_path_needs_no_download(
    fake_hub, tmp_path, monkeypatch, capsys, backend
):
    _cached, calls = fake_hub
    model = _complete_local_model(tmp_path / backend)
    code, events, _ = _run(
        monkeypatch, capsys, "--backend", backend, "--model", str(model), "--check", "--json"
    )
    assert code == 0 and events[-1]["cached"] is True and calls == []


@pytest.mark.parametrize("state", ["missing", "incomplete"])
@pytest.mark.parametrize("check", [True, False])
def test_invalid_local_model_path_is_uncached_or_fetch_error(
    fake_hub, tmp_path, monkeypatch, capsys, state, check
):
    _cached, calls = fake_hub
    model = tmp_path / state
    if state == "incomplete":
        model.mkdir()
        (model / "config.json").write_text("{}")
    argv = ["--backend", "mlx", "--model", str(model), "--json"]
    if check:
        argv.append("--check")

    code, events, _ = _run(monkeypatch, capsys, *argv)

    if check:
        assert code == 0
        assert events[-1]["event"] == "summary"
        assert events[-1]["cached"] is False
    else:
        assert code == 1
        assert events[-1]["event"] == "error"
        assert not any(event["event"] == "complete" for event in events)
    assert calls == []


@pytest.mark.parametrize("check", [True, False])
def test_kyutai_rejects_local_model_paths(tmp_path, monkeypatch, capsys, check):
    model = _complete_local_model(tmp_path / "kyutai")
    argv = ["--backend", "kyutai", "--model", str(model), "--json"]
    if check:
        argv.append("--check")

    code, events, _ = _run(monkeypatch, capsys, *argv)

    assert code == 1
    assert events[-1]["event"] == "error"
    assert "local" in events[-1]["message"].lower()


def test_local_model_uses_backend_required_patterns(tmp_path, monkeypatch, capsys):
    model = tmp_path / "custom"
    model.mkdir()
    (model / "sentinel.bin").write_bytes(b"ok")
    monkeypatch.setattr(
        PF,
        "_module_specs",
        lambda loaded, selected: [PF.SnapshotSpec(selected, required=("sentinel.bin",))],
    )

    code, events, _ = _run(
        monkeypatch, capsys, "--backend", "mlx", "--model", str(model), "--check", "--json"
    )

    assert code == 0
    assert events[-1]["cached"] is True


@pytest.mark.parametrize(
    "weights,cached",
    [
        ("weights.safetensors", True),
        ("weights.npz", True),
        ("weights.txt", False),  # matches ``weights.*``; mlx_whisper never reads it
        ("sub/weights.safetensors", False),  # the loader opens model_path / name only
    ],
)
def test_mlx_complete_means_the_files_its_loader_opens(
    tmp_path, monkeypatch, capsys, weights, cached
):
    """An mlx snapshot is complete only with ``config.json`` and
    ``weights.safetensors`` or ``weights.npz`` (the only two weight files
    ``mlx_whisper.load_models.load_model`` opens); no ``weights.*`` glob."""
    model = tmp_path / "custom"
    (model / weights).parent.mkdir(parents=True, exist_ok=True)
    (model / "config.json").write_text("{}")
    (model / weights).write_bytes(b"w")
    _code, events, _ = _run(
        monkeypatch, capsys, "--backend", "mlx", "--model", str(model), "--check", "--json"
    )
    assert events[-1]["cached"] is cached


def test_human_check_output(fake_hub, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["stt_server", "prefetch", "--backend", "parakeet", "--check"])
    M.main()
    out = capsys.readouterr().out
    assert "mlx-community/parakeet-tdt-0.6b-v3@main: missing" in out
    assert "parakeet: not cached" in out


def test_prefetch_requires_backend(monkeypatch, capsys):
    code, _events, err = _run(monkeypatch, capsys)
    assert code == 2 and "--backend" in err


def _fake_hub_with_config(tmp_path, monkeypatch, config: bytes | None, drop: str | None = None):
    files = dict(_FILES)
    if config is None:
        files.pop("config.json")
    else:
        files["config.json"] = config
    if drop:
        files.pop(drop)

    def fake(spec, *, local_files_only):
        snap = tmp_path / "hub" / spec.repo_id.replace("/", "--")
        snap.mkdir(parents=True, exist_ok=True)
        for name, data in files.items():
            (snap / name).write_bytes(data)
        return str(snap)

    monkeypatch.setattr(PF, "_snapshot_download", fake)
    monkeypatch.setattr(PF, "_remote_files", lambda spec: [])
    monkeypatch.setattr(PF, "_repo_cache_dir", lambda repo: tmp_path / "cache")


@pytest.mark.parametrize(
    ("backend", "config", "drop"),
    [
        ("parakeet", _FILES["config.json"], "model.safetensors"),  # incomplete snapshot
        ("kyutai", b"{not json", None),  # corrupt config.json
        ("kyutai", b'{"tokenizer_name": "tokenizer.model"}', None),  # missing mimi_name
        ("kyutai", b'{"mimi_name": "*.safetensors", "tokenizer_name": "t.model"}', None),
        ("kyutai", b"null", None),  # valid JSON, not an object
        ("kyutai", b"[1, 2]", None),
    ],
)
def test_unusable_snapshot_is_model_invalid_not_download_error(
    tmp_path, monkeypatch, capsys, backend, config, drop
):
    """Nothing network-related failed: neither ModelDownloadError nor the
    generic BackendStartError/ConfigError (prefetch never starts a backend)."""
    _fake_hub_with_config(tmp_path, monkeypatch, config, drop)
    code, events, _ = _run(monkeypatch, capsys, "--backend", backend, "--json")
    assert code == 1
    assert events[-1]["event"] == "error" and events[-1]["code"] == "ModelInvalidError"


@pytest.mark.parametrize("check", [True, False])
def test_invalid_local_model_is_model_invalid(tmp_path, monkeypatch, capsys, check):
    model = tmp_path / "incomplete"
    model.mkdir()
    (model / "config.json").write_text("{}")
    argv = ["--backend", "mlx", "--model", str(model), "--json"]
    if check:
        argv.append("--check")
    code, events, _ = _run(monkeypatch, capsys, *argv)
    if check:
        assert code == 0 and events[-1]["cached"] is False
    else:
        assert code == 1 and events[-1]["code"] == "ModelInvalidError"


@pytest.mark.parametrize(
    "name",
    [
        "*.safetensors",
        "../evil.safetensors",
        "sub/model.safetensors",
        "m[1].bin",
        "x|config.json",
        "",
        7,
    ],
)
def test_kyutai_config_names_must_be_plain_filenames(name):
    from stt_server.backends import kyutai as K

    config = {"mimi_name": name, "tokenizer_name": "tokenizer.model"}
    with pytest.raises(ValueError):
        K.weight_files(config)
    config = {"mimi_name": "mimi@1.safetensors", "tokenizer_name": "t.model", "moshi_name": name}
    with pytest.raises(ValueError):
        K.weight_files(config)


def test_kyutai_glob_config_never_reads_as_cached(tmp_path, monkeypatch, capsys):
    """A config naming ``*.safetensors`` must not match whatever ``.safetensors``
    file exists while the literal file the loader requests is absent."""
    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "config.json").write_text(
        json.dumps({"mimi_name": "*.safetensors", "tokenizer_name": "tokenizer.model"})
    )
    (snap / "model.safetensors").write_bytes(b"m")
    (snap / "tokenizer.model").write_bytes(b"t")
    monkeypatch.setattr(PF, "_snapshot_download", lambda spec, *, local_files_only: str(snap))
    code, events, _ = _run(monkeypatch, capsys, "--backend", "kyutai", "--check", "--json")
    assert code == 0 and events[-1]["cached"] is False


@pytest.mark.parametrize("check", [True, False])
def test_unexpandable_tilde_model_is_a_missing_local_model(monkeypatch, capsys, check):
    """``~nobody/x`` (``expanduser`` raises ``RuntimeError``) is a missing
    local model, not a BackendStartError."""
    argv = ["--backend", "mlx", "--model", "~no_such_user_7f3a_r8/model", "--json"]
    if check:
        argv.append("--check")
    code, events, _ = _run(monkeypatch, capsys, *argv)
    if check:
        assert code == 0 and events[-1]["event"] == "summary"
        assert events[-1]["cached"] is False
    else:
        assert code == 1 and events[-1]["code"] == "ModelInvalidError"


def test_kyutai_pipe_name_never_reads_as_cached(tmp_path, monkeypatch, capsys):
    """``|`` is not an alternatives delimiter: a config naming
    ``model.q8.safetensors|config.json`` never passes completeness on
    ``config.json`` alone (the loader requests the literal name)."""
    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "config.json").write_text(
        json.dumps(
            {
                "mimi_name": "mimi.safetensors",
                "moshi_name": "model.q8.safetensors|config.json",
                "tokenizer_name": "tokenizer.model",
            }
        )
    )
    (snap / "mimi.safetensors").write_bytes(b"i")
    (snap / "tokenizer.model").write_bytes(b"t")
    monkeypatch.setattr(PF, "_snapshot_download", lambda spec, *, local_files_only: str(snap))
    code, events, _ = _run(monkeypatch, capsys, "--backend", "kyutai", "--check", "--json")
    assert code == 0 and events[-1]["cached"] is False
    code, events, _ = _run(monkeypatch, capsys, "--backend", "kyutai", "--json")
    assert code == 1 and events[-1]["code"] == "ModelInvalidError"


def test_required_string_entry_is_one_literal_glob(tmp_path):
    """A plain string entry is never split: alternatives are a tuple."""
    (tmp_path / "config.json").write_text("{}")
    assert PF._complete(tmp_path, ("x|config.json",)) is False
    assert PF._complete(tmp_path, (("x", "config.json"),)) is True


def _mlx_hub(tmp_path, monkeypatch, weights: str):
    """A fake hub whose mlx repo holds ``config.json`` + ``weights`` only."""
    files = {"config.json": b"{}", weights: b"w" * 7}

    def fake(spec, *, local_files_only):
        snap = tmp_path / "hub" / spec.repo_id.replace("/", "--")
        if not local_files_only:
            snap.mkdir(parents=True, exist_ok=True)
            for name, data in files.items():
                (snap / name).write_bytes(data)
        elif not snap.is_dir():
            raise FileNotFoundError("not cached")
        return str(snap)

    monkeypatch.setattr(PF, "_snapshot_download", fake)
    monkeypatch.setattr(
        PF, "_remote_files", lambda spec: [(n, len(d), f"blob-{n}") for n, d in files.items()]
    )
    monkeypatch.setattr(PF, "_repo_cache_dir", lambda repo: tmp_path / "cache")


@pytest.mark.parametrize(("weights", "ok"), [("weights.npz", True), ("weights.txt", False)])
def test_mlx_weight_alternatives_through_hub_check_and_fetch(
    tmp_path, monkeypatch, capsys, weights, ok
):
    """The ``weights.safetensors``/``weights.npz`` alternative holds for every
    consumer (Hub fetch validation, Hub check), not only a local
    ``--check``."""
    _mlx_hub(tmp_path, monkeypatch, weights)
    code, events, _ = _run(monkeypatch, capsys, "--backend", "mlx", "--json")
    if ok:
        assert code == 0 and events[-1]["event"] == "complete"
    else:
        assert code == 1 and events[-1]["code"] == "ModelInvalidError"
    code, events, _ = _run(monkeypatch, capsys, "--backend", "mlx", "--check", "--json")
    assert code == 0 and events[-1]["cached"] is ok


@pytest.mark.parametrize(("weights", "ok"), [("weights.npz", True), ("weights.txt", False)])
def test_mlx_weight_alternatives_through_local_fetch(tmp_path, monkeypatch, capsys, weights, ok):
    model = tmp_path / "custom"
    model.mkdir()
    (model / "config.json").write_text("{}")
    (model / weights).write_bytes(b"w")
    code, events, _ = _run(monkeypatch, capsys, "--backend", "mlx", "--model", str(model), "--json")
    if ok:
        assert code == 0 and events[-1]["event"] == "complete"
    else:
        assert code == 1 and events[-1]["code"] == "ModelInvalidError"
        assert "weights.safetensors or weights.npz" in events[-1]["message"]


@pytest.mark.parametrize("config", [b"null", b"[]", b'"x"', b"3"])
def test_kyutai_check_non_object_config_is_not_cached(tmp_path, monkeypatch, capsys, config):
    """``--check`` reports a cached config.json that is valid JSON but not an
    object as ``cached: false`` (an incomplete snapshot), not a crash
    classified as BackendStartError."""
    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "config.json").write_bytes(config)
    monkeypatch.setattr(PF, "_snapshot_download", lambda spec, *, local_files_only: str(snap))
    code, events, _ = _run(monkeypatch, capsys, "--backend", "kyutai", "--check", "--json")
    assert code == 0 and events[-1]["event"] == "summary" and events[-1]["cached"] is False


@pytest.mark.parametrize("check", [True, False])
def test_prefetch_unknown_or_unsupported_backend_input_is_config_error(
    tmp_path, monkeypatch, capsys, check
):
    """ConfigError is now raised explicitly (a bare ValueError no longer maps to
    it): a local path for a backend that cannot load one stays a ConfigError."""
    model = tmp_path / "local"
    model.mkdir()
    argv = ["--backend", "kyutai", "--model", str(model), "--json"]
    if check:
        argv.append("--check")
    code, events, _ = _run(monkeypatch, capsys, *argv)
    assert code == 1 and events[-1]["code"] == "ConfigError"
