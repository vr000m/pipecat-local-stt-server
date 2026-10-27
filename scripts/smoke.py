#!/usr/bin/env python3
"""One-command smoke runner: every local check in one table.

    .venv/bin/python scripts/smoke.py [--agents] [--launchd]

Run it as ``just smoke [--agents] [--launchd]``. Steps, in order:

* ``supervised``: ``smoke_supervised.py --backend echo``.
* ``peercred``: ``smoke_peercred.py`` (SKIPPED when there is no second uid, the
  usual dev case; the same-uid leg still ran).
* ``token-file``: a spare-port echo server with a token file; a request with no
  token or a wrong one must get 401 and the right one 101 (``assert_token_gate``).
* ``agents`` (``--agents``): decode a short generated clip through every installed
  ``pipecat.stt-server*`` agent from its plist endpoint. Strictly read-only: it
  never calls bootout, bootstrap, kickstart or the installer.
* ``launchd`` (``--launchd``): ``smoke_launchd.py`` on the spare label and port.
  macOS only, never in CI, and it needs both ``PIPECAT_STT_LABEL`` and
  ``KODA_STT_LABEL`` set to the spare label, which this runner does explicitly.

Each step is PASS, FAIL or SKIPPED with a reason. The exit status is 1 if any
step FAILED, else 0 (SKIPPED never fails). Stdlib only; every step runs in a
subprocess through ``run_command`` with ``sys.executable`` (never ``uv run``,
which would prune the backend extras).
"""

import argparse
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

PASS = "PASS"
FAIL = "FAIL"
SKIPPED = "SKIPPED"

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))

import _smoke_lib  # noqa: E402

SPARE_LABEL = _smoke_lib.SPARE_LABEL
CLIP_TEXT = "Hello, this is a smoke test of the speech to text server."
CLIP_KEYWORD = "hello"

SUPERVISED_TIMEOUT = 600.0
PEERCRED_TIMEOUT = 180.0
TOKEN_TIMEOUT = 90.0
CLIP_TIMEOUT = 60.0
PLIST_READ_TIMEOUT = 30.0
DECODE_TIMEOUT = 180.0
LAUNCHD_TIMEOUT = 900.0
# After a step's timeout (or Ctrl-C) the child gets SIGTERM and this long to run
# its own cleanup before SIGKILL: smoke_launchd.py's teardown runs the installer's
# uninstall (up to 120 s) and waits on the spare .lock.
TEARDOWN_GRACE = 180.0

PEERCRED_NO_UID_MARK = "skipped: needs a second local uid"


@dataclass
class StepResult:
    name: str
    outcome: str
    detail: str = ""


def render_table(results: list[StepResult]) -> str:
    """One table: name, outcome, detail; every step present."""
    rows = [("STEP", "OUTCOME", "DETAIL")]
    for r in results:
        rows.append((r.name, r.outcome, " ".join(r.detail.split())))
    w0 = max(len(r[0]) for r in rows)
    w1 = max(len(r[1]) for r in rows)
    lines = [f"{a:<{w0}}  {b:<{w1}}  {c}".rstrip() for a, b, c in rows]
    lines.insert(1, f"{'-' * w0}  {'-' * w1}  {'-' * 6}")
    return "\n".join(lines)


def exit_code(results: list[StepResult]) -> int:
    return 1 if any(r.outcome == FAIL for r in results) else 0


def run_command(
    cmd: list[str], timeout: float, env: dict | None = None
) -> subprocess.CompletedProcess:
    """The only place this module spawns a subprocess for a step.

    On timeout the child is sent SIGTERM and given ``TEARDOWN_GRACE`` to clean up
    (SIGKILL only after that), then ``TimeoutExpired`` is raised. On Ctrl-C the
    child (same process group, so it got the SIGINT too) is waited for, not
    killed, so a harness's teardown finishes before the interrupt propagates.
    """
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=str(REPO),
        stdin=subprocess.DEVNULL,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.send_signal(signal.SIGTERM)
        _reap(proc)
        raise
    except KeyboardInterrupt:
        _reap(proc)
        raise
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def _reap(proc: subprocess.Popen) -> None:
    """Wait up to TEARDOWN_GRACE for PROC to exit on its own, then SIGKILL it."""
    try:
        proc.communicate(timeout=TEARDOWN_GRACE)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()


def _tail(proc: subprocess.CompletedProcess) -> str:
    text = ((proc.stderr or "") + "\n" + (proc.stdout or "")).strip()
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return lines[-1][:200] if lines else ""


def _execute(cmd, timeout, env=None):
    """Run one command; return (outcome, detail, proc-or-None)."""
    try:
        proc = run_command(cmd, timeout, env)
    except subprocess.TimeoutExpired:
        return FAIL, f"timed out after {timeout:g}s", None
    except Exception as exc:  # noqa: BLE001 - a crash is a FAIL, never a traceback
        return FAIL, f"crashed: {type(exc).__name__}: {exc}", None
    if proc.returncode != 0:
        detail = f"exit {proc.returncode}"
        tail = _tail(proc)
        return FAIL, f"{detail}: {tail}" if tail else detail, proc
    return PASS, "", proc


def run_step(
    name: str,
    cmd: list[str],
    timeout: float,
    *,
    requested: bool = True,
    skip_reason: str | None = None,
    env: dict | None = None,
) -> StepResult:
    if not requested:
        return StepResult(name, SKIPPED, "not requested")
    if skip_reason:
        return StepResult(name, SKIPPED, skip_reason)
    outcome, detail, _ = _execute(cmd, timeout, env)
    return StepResult(name, outcome, detail)


# --- step builders ----------------------------------------------------------
def supervised_cmd() -> list[str]:
    return [sys.executable, str(SCRIPTS / "smoke_supervised.py"), "--backend", "echo"]


def peercred_cmd() -> list[str]:
    return [sys.executable, str(SCRIPTS / "smoke_peercred.py")]


def token_file_cmd() -> list[str]:
    return [sys.executable, str(Path(__file__).resolve()), "--_token-check"]


def launchd_cmd() -> list[str]:
    return [sys.executable, str(SCRIPTS / "smoke_launchd.py")]


def launchd_env(base: dict | None = None) -> dict:
    env = dict(os.environ if base is None else base)
    env["PIPECAT_STT_LABEL"] = SPARE_LABEL
    env["KODA_STT_LABEL"] = SPARE_LABEL
    return env


def launchd_skip_reason() -> str | None:
    return _smoke_lib.launchd_skip_reason(os.environ, sys.platform)


def peercred_step() -> StepResult:
    outcome, detail, proc = _execute(peercred_cmd(), PEERCRED_TIMEOUT)
    if outcome == PASS and proc is not None and PEERCRED_NO_UID_MARK in (proc.stdout or ""):
        return StepResult(
            "peercred",
            SKIPPED,
            "cross-uid leg unavailable: no second uid via passwordless sudo (same-uid leg passed)",
        )
    return StepResult("peercred", outcome, detail)


def agent_plists() -> list[Path]:
    return _smoke_lib.agent_plists(_smoke_lib.env_home())


def read_plist_cmd(field: str, plist: Path) -> list[str]:
    return [
        sys.executable,
        str(SCRIPTS / "read_stt_plist.py"),
        "--strict",
        "--field",
        field,
        str(plist),
    ]


def decode_cmd(endpoint: dict, wav: str) -> list[str]:
    cmd = [sys.executable, str(Path(__file__).resolve()), "--_decode", "--wav", wav]
    cmd += ["--keyword", CLIP_KEYWORD]
    if endpoint.get("socket-path"):
        cmd += ["--socket-path", endpoint["socket-path"]]
    else:
        cmd += ["--host", endpoint["host"], "--port", endpoint["port"]]
    if endpoint.get("auth-token-file"):
        cmd += ["--auth-token-file", endpoint["auth-token-file"]]
    return cmd


def make_clip(directory: str) -> tuple[str | None, str]:
    """Generate the clip with say + afconvert. Returns (wav path, error)."""
    aiff = os.path.join(directory, "clip.aiff")
    wav = os.path.join(directory, "clip.wav")
    steps = [
        ["say", "-o", aiff, CLIP_TEXT],
        ["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", aiff, wav],
    ]
    for cmd in steps:
        outcome, detail, _ = _execute(cmd, CLIP_TIMEOUT)
        if outcome != PASS:
            return None, f"{cmd[0]} {detail}"
    return wav, ""


def agents_step(requested: bool) -> StepResult:
    if not requested:
        return StepResult("agents", SKIPPED, "not requested")
    plists = agent_plists()
    if not plists:
        return StepResult("agents", SKIPPED, "no installed pipecat.stt-server* agents")
    if shutil.which("say") is None:
        return StepResult("agents", SKIPPED, "macOS `say` is not available to generate the clip")
    parts: list[str] = []
    failed = False
    with tempfile.TemporaryDirectory(prefix="stt-smoke-agents.") as tmp:
        wav, err = make_clip(tmp)
        if wav is None:
            return StepResult("agents", FAIL, err)
        for plist in plists:
            label = plist.name[: -len(".plist")]
            endpoint: dict[str, str] = {}
            bad = ""
            for field in ("socket-path", "host", "port", "auth-token-file"):
                outcome, detail, proc = _execute(read_plist_cmd(field, plist), PLIST_READ_TIMEOUT)
                if outcome != PASS or proc is None:
                    bad = f"cannot read {field}: {detail}"
                    break
                endpoint[field] = (proc.stdout or "").strip()
            if (
                not bad
                and not endpoint["socket-path"]
                and not (endpoint["host"] and endpoint["port"])
            ):
                bad = "plist has no endpoint"
            if bad:
                failed = True
                parts.append(f"{label}: FAIL {bad}")
                continue
            outcome, detail, _ = _execute(decode_cmd(endpoint, wav), DECODE_TIMEOUT)
            failed = failed or outcome != PASS
            parts.append(f"{label}: {outcome}" + (f" {detail}" if detail else ""))
    return StepResult("agents", FAIL if failed else PASS, "; ".join(parts))


def build_and_run(agents: bool, launchd: bool) -> list[StepResult]:
    results = [
        run_step("supervised", supervised_cmd(), SUPERVISED_TIMEOUT),
        peercred_step(),
        run_step("token-file", token_file_cmd(), TOKEN_TIMEOUT),
        agents_step(agents),
        run_step(
            "launchd",
            launchd_cmd(),
            LAUNCHD_TIMEOUT,
            requested=launchd,
            skip_reason=launchd_skip_reason() if launchd else None,
            env=launchd_env() if launchd else None,
        ),
    ]
    return results


# --- child roles (run as subprocesses of the steps above) --------------------
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _token_check() -> int:
    """Start an echo server with a token file on a spare port; run the gate."""
    sys.path.insert(0, str(REPO))
    from _smoke_lib import assert_token_gate

    port = _free_port()
    # The token file must pass stt_server.auth's policy: 0600, owner-only, under
    # an owner-only directory below $HOME (/tmp fails the ancestor check).
    tmp = tempfile.mkdtemp(prefix="stt-smoke-token.", dir=str(Path.home()))
    server = None
    try:
        os.chmod(tmp, 0o700)
        token_file = os.path.join(tmp, "token")
        fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write("smoke-token-" + os.urandom(8).hex() + "\n")
        server = subprocess.Popen(
            [
                sys.executable, "-m", "stt_server", "serve", "--backend", "echo",
                "--host", "127.0.0.1", "--port", str(port),
                "--auth-token-file", token_file,
            ],
            cwd=str(REPO),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )  # fmt: skip
        deadline = time.time() + 30
        while time.time() < deadline:
            if server.poll() is not None:
                err = (server.stderr.read() if server.stderr else "")[-300:]
                print(f"server exited early ({server.returncode}): {err}", file=sys.stderr)
                return 1
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                break
            except OSError:
                time.sleep(0.2)
        else:
            print(f"server did not listen on 127.0.0.1:{port} within 30s", file=sys.stderr)
            return 1
        try:
            assert_token_gate("127.0.0.1", port, token_file)
        except (AssertionError, OSError) as exc:
            print(f"token gate: {exc}", file=sys.stderr)
            return 1
        print(f"token gate OK on 127.0.0.1:{port}")
        return 0
    finally:
        if server is not None and server.poll() is None:
            server.terminate()
            try:
                server.wait(10)
            except subprocess.TimeoutExpired:
                server.kill()
        shutil.rmtree(tmp, ignore_errors=True)


def _decode(args: argparse.Namespace) -> int:
    """Decode a wav through one endpoint; require the key word in the transcript."""
    import asyncio
    import wave

    sys.path.insert(0, str(REPO))
    from stt_server.client import TranscriptionClient
    from stt_server.protocol import EVT_TRANSCRIPT_COMPLETED

    token = None
    if args.auth_token_file:
        with open(args.auth_token_file, encoding="utf-8") as fh:
            token = fh.read().strip()
    with wave.open(args.wav) as w:
        data = w.readframes(w.getnframes())

    async def go() -> str:
        kwargs = {"auth_token": token}
        if args.socket_path:
            kwargs["socket_path"] = args.socket_path
        else:
            kwargs["host"], kwargs["port"] = args.host, int(args.port)
        async with TranscriptionClient(**kwargs) as c:
            await c.update_session(turn_detection=None)
            for i in range(0, len(data), 3200):
                await c.send_audio(data[i : i + 3200])
            await c.commit()
            async for ev in c.events():
                if ev.get("type") == EVT_TRANSCRIPT_COMPLETED:
                    return ev.get("transcript", "")
        return ""

    try:
        text = asyncio.run(asyncio.wait_for(go(), 150))
    except Exception as exc:  # noqa: BLE001
        print(f"decode failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    if args.keyword.lower() not in text.lower():
        print(f"transcript lacks {args.keyword!r}: {text!r}", file=sys.stderr)
        return 1
    print(f"transcript: {text!r}")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["--_token-check"]:
        return _token_check()
    if argv[:1] == ["--_decode"]:
        ap = argparse.ArgumentParser()
        ap.add_argument("--_decode", action="store_true")
        ap.add_argument("--wav", required=True)
        ap.add_argument("--keyword", required=True)
        ap.add_argument("--socket-path")
        ap.add_argument("--host")
        ap.add_argument("--port")
        ap.add_argument("--auth-token-file")
        return _decode(ap.parse_args(argv))

    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument(
        "--agents",
        action="store_true",
        help="decode a clip through each installed agent (read-only)",
    )
    ap.add_argument(
        "--launchd", action="store_true", help="run the launchd harness on the spare label (macOS)"
    )
    args = ap.parse_args(argv)
    results = build_and_run(args.agents, args.launchd)
    print(render_table(results))
    return exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
