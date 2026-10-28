"""Run a smoke script with ``_smoke_lib.agents_home()`` moved to a temp directory.

    python _with_agents_home.py HOME SCRIPT [ARGS...]

The stubbed suites run ``scripts/smoke_launchd.py`` (and ``scripts/smoke.py``)
as real processes; without this wrapper those processes would read the
developer's real ``~/Library/LaunchAgents`` (``agents_home()`` is the account
home from the password database, which ``$HOME`` does not move). The wrapper
imports ``scripts/_smoke_lib.py`` first, replaces ``agents_home`` with one that
returns HOME, then runs SCRIPT as ``__main__``; the script's own
``import _smoke_lib`` finds the patched module in ``sys.modules``. Production
code carries no override: this is the only way the agents home moves.

It chains with ``_audit_library_reads.py`` (``_audit_library_reads.py LOG
_with_agents_home.py HOME SCRIPT ...``).
"""

import runpy
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
HOME = Path(sys.argv[1])
sys.path.insert(0, str(SCRIPTS))

import _smoke_lib  # noqa: E402

_smoke_lib.agents_home = lambda: HOME
sys.argv = sys.argv[2:]
runpy.run_path(sys.argv[0], run_name="__main__")
