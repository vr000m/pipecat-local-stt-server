"""Trip-wire pin for the frozen V1 wire protocol version.

``PROTOCOL_VERSION`` is part of the frozen V1 surface. Bumping it requires
editing this test explicitly — that is the intended friction. A protocol bump
needs its own dev plan and a coordinated bot upgrade; it must never happen as a
silent side effect of an unrelated change.
"""

from __future__ import annotations

import stt_server
from stt_server import protocol


def test_protocol_version_is_pinned():
    assert protocol.PROTOCOL_VERSION == "0.1"


def test_protocol_version_reexport_matches():
    # The package root re-exports the constant; it must agree with the module.
    assert stt_server.PROTOCOL_VERSION == protocol.PROTOCOL_VERSION == "0.1"
