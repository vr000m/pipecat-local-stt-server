"""Opt-in smoke test: drive ``serve --supervised`` the way a menubar app does.

Spawns real server processes (stdout JSON events, stdin EOF / SIGTERM stops,
parent death, second instance, stale socket, real client session) and prints
PASS/FAIL per scenario. Not part of CI or pytest: it needs the backend's extra
installed and, for a real model, the weights in the Hugging Face cache.

    .venv/bin/python scripts/smoke_supervised.py --backend echo
    .venv/bin/python scripts/smoke_supervised.py --backend mlx \
        --model mlx-community/whisper-large-v3-turbo-q4

Use the interpreter whose environment has the backend's extra (``--python``
defaults to the one running this script). Exit status is 0 only if every
scenario passed; 2 if the model is not cached (nothing was run).
Scenarios that need a warm-up window (S9, S13) are not meaningful for the
echo backend, which warms instantly and ignores ``--model``; they are skipped.
Speech for the transcription check is generated with macOS ``say`` unless
``--wav`` (16 kHz mono PCM16) is given.
"""

import argparse
import asyncio
import json
import os
import select
import signal
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
_ap.add_argument("--backend", default="echo")
_ap.add_argument("--model")
_ap.add_argument("--python", default=sys.executable)
_ap.add_argument("--wav")
_ap.add_argument("--cwd", default=str(REPO), help="checkout to run `-m stt_server` from")
_args = _ap.parse_args()
PY, BACKEND, MODEL, CWD = _args.python, _args.backend, _args.model, _args.cwd
sys.path.insert(0, CWD)


def _speech_wav():
    if _args.wav:
        return _args.wav
    out = Path(tempfile.mkdtemp(prefix="stt-smoke.")) / "speech.wav"
    say = out.with_suffix(".aiff")
    try:
        subprocess.run(
            ["say", "-o", str(say), "Hello, this is a smoke test of the speech to text server."],
            check=True,
        )
        subprocess.run(
            ["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", str(say), str(out)],
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        sys.exit("smoke: need --wav (macOS `say`/`afconvert` unavailable)")
    return str(out)


WAV = _speech_wav()
# The socket must sit under $HOME (UDS trust boundary), in an owner-only dir.
DIR = Path.home() / "Library/Caches/pipecat-stt-smoke"
DIR.mkdir(parents=True, exist_ok=True)
os.chmod(DIR, 0o700)
SOCK = str(DIR / f"{BACKEND}.sock")
RESULTS = []


def rec(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(("PASS" if ok else "FAIL"), name, detail, flush=True)


def cmd(extra=()):
    c = [
        PY,
        "-m",
        "stt_server",
        "serve",
        "--supervised",
        "--socket-path",
        SOCK,
        "--backend",
        BACKEND,
    ]
    if MODEL:
        c += ["--model", MODEL]
    return c + list(extra)


def spawn(extra=()):
    return subprocess.Popen(
        cmd(extra),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=CWD,
    )


def read_events(p, until, timeout):
    """Read JSON stdout lines until predicate; returns (events, nonjson_lines)."""
    evs, bad, t0 = [], [], time.time()
    while time.time() - t0 < timeout:
        r, _, _ = select.select([p.stdout], [], [], 0.2)
        if r:
            line = p.stdout.readline()
            if not line:
                break
            try:
                e = json.loads(line)
                evs.append(e)
            except Exception:
                bad.append(line)
                continue
            if until(e):
                break
        elif p.poll() is not None:
            break
    return evs, bad


def wait_exit(p, timeout):
    t0 = time.time()
    try:
        p.wait(timeout)
        return time.time() - t0
    except subprocess.TimeoutExpired:
        return None


def pcm():
    with wave.open(WAV) as w:
        return w.readframes(w.getnframes())


async def transcribe(label="c"):
    from stt_server.client import TranscriptionClient

    async with TranscriptionClient(socket_path=SOCK) as c:
        await c.update_session(turn_detection=None)
        data = pcm()
        for i in range(0, len(data), 3200):
            await c.send_audio(data[i : i + 3200])
        await c.commit()
        await c.status()
        out = ""
        saw_status = False

        async def go():
            nonlocal out, saw_status
            async for e in c.events():
                t = e.get("type")
                if t == "conversation.item.input_audio_transcription.completed":
                    out = e.get("transcript", "")
                if t == "server.status":
                    saw_status = True
                if out and saw_status:
                    return

        await asyncio.wait_for(go(), 60)
        return out, saw_status


def cleanup():
    for f in DIR.glob(f"{BACKEND}.sock*"):
        try:
            f.unlink()
        except OSError:
            pass


def s_happy_and_stdin_eof():
    cleanup()
    p = spawn()
    t0 = time.time()
    evs, bad = read_events(p, lambda e: e.get("event") in ("ready", "error"), 180)
    ready = [e for e in evs if e.get("event") == "ready"]
    rec(
        "S1 ready event, stdout is JSON-only",
        bool(ready) and not bad,
        f"warmup={time.time() - t0:.1f}s bad={bad[:1]} ready={ready[:1]}",
    )
    if not ready:
        p.kill()
        return None
    text, st = asyncio.run(transcribe())
    rec(
        "S2 client transcribes real speech + server.status",
        bool(text.strip()) and st,
        f"text={text!r}",
    )
    rec(
        "S3 socket 0600 in 0700 dir",
        oct(os.stat(SOCK).st_mode & 0o777) == "0o600",
        oct(os.stat(SOCK).st_mode & 0o777),
    )
    p.stdin.close()
    dt = wait_exit(p, 15)
    rec(
        "S4 stdin EOF -> clean exit 0 <15s",
        dt is not None and p.returncode == 0,
        f"dt={dt} rc={p.returncode}",
    )
    rec("S5 socket removed after exit", not os.path.exists(SOCK))
    return p


def s_second_instance_refused():
    cleanup()
    a = spawn()
    evs, _ = read_events(a, lambda e: e.get("event") in ("ready", "error"), 180)
    if not any(e.get("event") == "ready" for e in evs):
        rec("S6 setup", False, str(evs))
        a.kill()
        return
    ino = os.stat(SOCK).st_ino
    t0 = time.time()
    b = spawn()
    evs2, bad = read_events(b, lambda e: e.get("event") == "error", 30)
    dt = wait_exit(b, 10)
    err = [e for e in evs2 if e.get("event") == "error"]
    rec(
        "S6 second instance: EADDRINUSE error event, fast, nonzero exit",
        bool(err)
        and err[0].get("code") == "EADDRINUSE"
        and (dt or 99) < 10
        and b.returncode not in (0, None),
        f"code={err[0].get('code') if err else None} rc={b.returncode} t={time.time() - t0:.1f}s",
    )
    rec(
        "S7 live socket not stolen (same inode, still serves)",
        os.stat(SOCK).st_ino == ino and bool(asyncio.run(transcribe())[0].strip()),
    )
    rec("S8 3 concurrent clients on one server", all(t.strip() for t, _ in asyncio.run(_multi())))
    a.stdin.close()
    wait_exit(a, 15)


async def _multi():
    return await asyncio.gather(*[transcribe(str(i)) for i in range(3)])


def s_stop_during_warmup(kind):
    cleanup()
    p = spawn()
    time.sleep(0.4)
    if kind == "eof":
        p.stdin.close()
    elif kind == "both":
        p.send_signal(signal.SIGTERM)
        p.stdin.close()
    else:
        p.send_signal(signal.SIGTERM)
    dt = wait_exit(p, 60)
    out = p.stdout.read() if p.stdout else ""
    evs = [json.loads(ln) for ln in out.splitlines() if ln.startswith("{")]
    rec(
        f"S9-{kind} stop during warm-up: exits promptly, no ready event, no leftovers",
        dt is not None
        and not any(e.get("event") == "ready" for e in evs)
        and not os.path.exists(SOCK),
        f"dt={dt and round(dt, 1)} rc={p.returncode} events={[e.get('event') for e in evs]}",
    )


def s_prefetch():
    base = [PY, "-m", "stt_server", "prefetch", "--backend", BACKEND] + (
        ["--model", MODEL] if MODEL else []
    )
    r = subprocess.run(
        base + ["--check", "--json"], capture_output=True, text=True, timeout=60, cwd=CWD
    )
    lines = [json.loads(ln) for ln in r.stdout.splitlines() if ln.startswith("{")]
    rec(
        "S0a prefetch --check --json: JSON-only, cached true for a cached model",
        r.returncode == 0
        and bool(lines)
        and all(ev.get("cached") for ev in lines if "cached" in ev),
        f"rc={r.returncode} last={lines[-1] if lines else r.stdout[:100]}",
    )
    r = subprocess.run(base + ["--json"], capture_output=True, text=True, timeout=120, cwd=CWD)
    lines = [json.loads(ln) for ln in r.stdout.splitlines() if ln.startswith("{")]
    rec(
        "S0b prefetch --json on cached model: completes, rc 0, final complete event",
        r.returncode == 0 and bool(lines) and lines[-1].get("event") == "complete",
        f"rc={r.returncode} n={len(lines)} last={lines[-1] if lines else r.stdout[:100]}",
    )
    if BACKEND in ("echo", "kyutai"):
        # echo takes no model; kyutai takes no local path: a ConfigError, not a missing path
        return
    r = subprocess.run(
        [
            PY,
            "-m",
            "stt_server",
            "prefetch",
            "--backend",
            BACKEND,
            "--model",
            "/nonexistent/dir",
            "--check",
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=CWD,
    )
    rec(
        "S0c prefetch --check on a nonexistent local path: not cached (not a backend failure)",
        '"cached": false' in r.stdout.replace('":false', '": false')
        or '"cached":false' in r.stdout,
        f"rc={r.returncode} out={r.stdout[:140]!r}",
    )


def s_parent_sigkill():
    cleanup()
    parent = subprocess.Popen(
        [
            PY,
            "-c",
            f"""
import subprocess,sys,time
p=subprocess.Popen({cmd()!r},stdin=subprocess.PIPE,stdout=subprocess.PIPE,cwd='{CWD}')
print(p.pid,flush=True); time.sleep(600)""",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    cpid = int(parent.stdout.readline())
    t0 = time.time()
    while not os.path.exists(SOCK) and time.time() - t0 < 180:
        time.sleep(0.5)
    time.sleep(1)
    parent.kill()
    parent.wait()
    t0 = time.time()
    while time.time() - t0 < 20:
        try:
            os.kill(cpid, 0)
            time.sleep(0.3)
        except ProcessLookupError:
            break
    gone = False
    try:
        os.kill(cpid, 0)
    except ProcessLookupError:
        gone = True
    rec(
        "S10 parent SIGKILLed -> server exits via stdin EOF <20s",
        gone,
        f"dt={time.time() - t0:.1f}s",
    )
    if not gone:
        os.kill(cpid, 9)


def s_kill9_then_restart():
    cleanup()
    a = spawn()
    evs, _ = read_events(a, lambda e: e.get("event") in ("ready", "error"), 180)
    a.kill()
    a.wait()
    stale = os.path.exists(SOCK)
    b = spawn()
    evs2, _ = read_events(b, lambda e: e.get("event") in ("ready", "error"), 180)
    rec(
        "S11 kill -9 then restart recovers stale socket/lock",
        any(e.get("event") == "ready" for e in evs2),
        f"stale_left={stale} ev={[e.get('event') for e in evs2]}",
    )
    b.stdin.close()
    wait_exit(b, 15)


def s_sigterm_midsession():
    cleanup()
    p = spawn()
    evs, _ = read_events(p, lambda e: e.get("event") in ("ready", "error"), 180)

    async def go():
        from stt_server.client import TranscriptionClient

        c = TranscriptionClient(socket_path=SOCK)
        await c.connect()
        await c.update_session(turn_detection=None)
        await c.send_audio(pcm()[:32000])
        await asyncio.sleep(0.3)
        t0 = time.time()
        p.send_signal(signal.SIGTERM)
        await asyncio.get_running_loop().run_in_executor(None, lambda: wait_exit(p, 20))
        await c.close()
        return time.time() - t0

    dt = asyncio.run(go())
    rec(
        "S12 SIGTERM with live client session: exits <15s rc 0",
        p.returncode == 0 and dt < 15,
        f"dt={dt:.1f}s rc={p.returncode}",
    )


def s_bad_model_then_sigterm():
    cleanup()
    c = [
        PY,
        "-m",
        "stt_server",
        "serve",
        "--supervised",
        "--socket-path",
        SOCK,
        "--backend",
        BACKEND,
        "--model",
        "/nonexistent/model",
    ]
    p = subprocess.Popen(
        c, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=CWD
    )
    evs, bad = read_events(p, lambda e: e.get("event") == "error", 60)
    dt = wait_exit(p, 20)
    rec(
        "S13 bad model: one error event, process exits nonzero, no leftovers",
        bool(evs) and dt is not None and p.returncode != 0 and not os.path.exists(SOCK),
        f"ev={evs[:1]} rc={p.returncode}",
    )


if __name__ == "__main__":
    if BACKEND != "echo":
        chk = subprocess.run(
            [PY, "-m", "stt_server", "prefetch", "--backend", BACKEND]
            + (["--model", MODEL] if MODEL else [])
            + ["--check", "--json"],
            capture_output=True,
            text=True,
            cwd=CWD,
        )
        if '"cached":true' not in chk.stdout.replace('": true', '":true'):
            print("smoke: model is not cached; run `prefetch` first. Nothing was run.")
            sys.exit(2)
    steps = (
        [
            s_prefetch,
            s_happy_and_stdin_eof,
            s_second_instance_refused,
        ]
        + (
            []
            if BACKEND == "echo"
            else [
                lambda: s_stop_during_warmup("eof"),
                lambda: s_stop_during_warmup("sigterm"),
                lambda: s_stop_during_warmup("both"),
            ]
        )
        + [s_parent_sigkill, s_kill9_then_restart, s_sigterm_midsession]
        + ([] if BACKEND == "echo" else [s_bad_model_then_sigterm])
    )
    for s in steps:
        try:
            s()
        except Exception as e:
            rec(getattr(s, "__name__", "step") + " CRASHED", False, repr(e)[:200])
    cleanup()
    print("SUMMARY", sum(ok for _, ok, _ in RESULTS), "/", len(RESULTS))
    sys.exit(0 if all(ok for _, ok, _ in RESULTS) else 1)
