#!/usr/bin/env python3
"""One-command smoke runner: every local check in one table.

    .venv/bin/python scripts/smoke.py [--agents] [--launchd]

Run it as ``just smoke [--agents] [--launchd]``. Steps, in order:

* ``supervised``: ``smoke_supervised.py --backend echo``.
* ``peercred``: ``smoke_peercred.py`` (SKIPPED when there is no second uid, the
  usual dev case, where the same-uid leg still ran; or when the platform is not
  macOS or Linux, where nothing ran).
* ``token-file``: a spare-port echo server with a token file; a request with no
  token or a wrong one must get 401 and the right one 101 (``assert_token_gate``).
* ``agents`` (``--agents``): decode a short generated clip through every installed
  ``pipecat.stt-server*`` agent from its plist endpoint. The plists are read from
  ``_smoke_lib.agents_home()`` (the account home, the same one the launchd
  harness snapshots; ``$HOME`` does not move it). Strictly read-only: it never
  calls bootout, bootstrap, kickstart or the installer.
* ``launchd`` (``--launchd``): ``smoke_launchd.py`` on the spare label and port.
  macOS only, never in CI, and it needs both ``PIPECAT_STT_LABEL`` and
  ``KODA_STT_LABEL`` set to the spare label, which this runner does explicitly.

Each step is PASS, FAIL or SKIPPED with a reason. The exit status is 1 if any
step FAILED, else 0 (SKIPPED never fails). Stdlib only; every step runs in a
subprocess through ``run_command`` with ``sys.executable`` (never ``uv run``,
which would prune the backend extras).

Each step runs in its own session. On a step timeout the step gets SIGTERM, on
Ctrl-C (or a SIGINT or SIGTERM sent to this runner) SIGINT; either way it has
``TEARDOWN_GRACE`` to clean up before its whole process group is SIGKILLed, and
further signals to the runner are ignored until then.
"""

import argparse
import contextlib
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
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
# After a step's timeout (SIGTERM) or Ctrl-C (SIGINT) the child gets this long to
# run its own cleanup before its process group is SIGKILLed: smoke_launchd.py's
# teardown runs the installer's uninstall (up to 120 s) and waits on the spare
# .lock.
TEARDOWN_GRACE = 180.0

# smoke_peercred.py's own skip lines (it exits 0 on both).
PEERCRED_NO_UID_MARK = "skipped: needs a second local uid"
PEERCRED_PLATFORM_MARK = "skipped: peer-cred smoke needs"


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

    The child runs in its own session (process group), so a terminal Ctrl-C
    reaches only this runner, which forwards it. On timeout the child is sent
    SIGTERM, on Ctrl-C (``KeyboardInterrupt``, which a SIGTERM to this runner
    also raises under ``main``) SIGINT; then ``_stop`` gives it ``TEARDOWN_GRACE``
    to clean up and SIGKILLs its group, and the exception propagates.
    """
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=str(REPO),
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _stop(proc, signal.SIGTERM)
        raise
    except KeyboardInterrupt:
        _stop(proc, signal.SIGINT)
        raise
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


@contextlib.contextmanager
def _signals_held():
    """Ignore SIGINT and SIGTERM to this process for the block (main thread only),
    so a second Ctrl-C cannot cut a child's teardown short."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    old = {s: signal.signal(s, signal.SIG_IGN) for s in (signal.SIGINT, signal.SIGTERM)}
    try:
        yield
    finally:
        for s, h in old.items():
            if h is not None:
                signal.signal(s, h)


def _stop(proc: subprocess.Popen, sig: int) -> None:
    """Send SIG to PROC, wait up to TEARDOWN_GRACE for it to exit, then SIGKILL
    its process group so nothing it started (a server, a sleep) outlives it."""
    with _signals_held():
        with contextlib.suppress(ProcessLookupError):
            proc.send_signal(sig)
        try:
            proc.communicate(timeout=TEARDOWN_GRACE)
        except subprocess.TimeoutExpired:
            # The group first: a grandchild holding the pipes would block the read.
            _kill_group(proc)
            proc.kill()
            proc.communicate()
        _kill_group(proc)


def _kill_group(proc: subprocess.Popen) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)


@contextlib.contextmanager
def _term_as_interrupt():
    """Make a SIGTERM to this runner raise KeyboardInterrupt, like Ctrl-C, so the
    active step is stopped through ``_stop`` instead of being orphaned."""

    def handler(signum, _frame):
        raise KeyboardInterrupt(f"signal {signum}")

    old = signal.signal(signal.SIGTERM, handler)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, old)


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
    out = (proc.stdout or "") if proc is not None else ""
    if outcome == PASS and PEERCRED_PLATFORM_MARK in out:
        return StepResult(
            "peercred", SKIPPED, f"peer-cred smoke needs macOS or Linux, not {sys.platform}"
        )
    if outcome == PASS and PEERCRED_NO_UID_MARK in out:
        return StepResult(
            "peercred",
            SKIPPED,
            "cross-uid leg unavailable: no second uid via passwordless sudo (same-uid leg passed)",
        )
    return StepResult("peercred", outcome, detail)


def agent_plists() -> list[Path]:
    """Installed real-agent plists, from the home the launchd harness snapshots."""
    override = _smoke_lib.agents_home_override()
    if override is not None:
        print(
            f"warning: reading installed agents from {override} "
            f"({_smoke_lib.TEST_AGENTS_HOME_ENV} is a test-only override)",
            file=sys.stderr,
        )
    return _smoke_lib.agent_plists(_smoke_lib.agents_home())


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

    # The runner's timeout sends SIGTERM; its default action would skip the
    # finally below and leave the server and the token directory behind.
    def on_term(signum, _frame):
        raise SystemExit(128 + signum)

    old_term = signal.signal(signal.SIGTERM, on_term)
    tmp = None
    server = None
    try:
        # The token file must pass stt_server.auth's policy: 0600, owner-only,
        # under an owner-only directory below $HOME (/tmp fails the ancestor check).
        tmp = tempfile.mkdtemp(prefix="stt-smoke-token.", dir=str(Path.home()))
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
        with _signals_held():
            if server is not None and server.poll() is None:
                server.terminate()
                try:
                    server.wait(10)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait()
            if tmp is not None:
                shutil.rmtree(tmp, ignore_errors=True)
        signal.signal(signal.SIGTERM, old_term)


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
    with _term_as_interrupt():
        results = build_and_run(args.agents, args.launchd)
    print(render_table(results))
    return exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
