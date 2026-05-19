"""Protocol constants, event type names, and error codes.

V1 is an OpenAI-inspired transcription-only subset. Deviations from the
public OpenAI Realtime transcription shape (snapshot: 2026-04-20):

- no conversation graph, no output-audio events, no tools/assistant events
- ``item_id`` and server-emitted ``event_id`` are server-minted
- ``previous_item_id`` omitted
- transcript deltas may be a single final-sized delta plus ``completed``
  because the MLX V1 backend is commit-oriented, not truly streaming
- ``speech_started`` / ``speech_stopped`` emitted only when server VAD is on;
  with ``turn_detection: null`` they are never emitted
- custom events: ``server.hello``, ``server.status``, ``session.close``,
  ``session.cancel``, ``session.closed``
- ``server.hello`` and ``server.status`` carry a ``backend`` object
  (``{name, model}``) naming the ASR behind the socket. Additive V1 field;
  ``PROTOCOL_VERSION`` is unchanged — readers that do not know it ignore it.

OpenAI compatibility notes:

- ``transcription_session.update`` is accepted as an alias for the
  V1 ``session.update`` event; likewise the server emits
  ``transcription_session.created`` / ``.updated`` alongside ``session.*``
  so a vanilla OpenAI transcription-mode client can talk to this server.
- ``input_audio_format`` is accepted either as an OpenAI-style string
  (``"pcm16"``) at the session root or as the legacy nested
  ``session.audio.input.format = {encoding, rate, channels}`` object.
  Only 16 kHz / mono / pcm16 is supported on the wire either way.
- ``turn_detection`` is accepted at the session root (OpenAI) or nested
  under ``session.audio.input.turn_detection`` (legacy). Must be null.
- ``input_audio_buffer.clear`` drops uncommitted audio without closing
  the session (paired with an ``input_audio_buffer.cleared`` ack);
  ``session.cancel`` remains the coarser extension that also closes.
- Backend decode failures emit
  ``conversation.item.input_audio_transcription.failed`` with an
  ``item_id``; session-level errors use the generic ``error`` event.
"""

from __future__ import annotations

from enum import Enum

PROTOCOL_VERSION = "0.1"

# Wire format is pinned in V1.
AUDIO_SAMPLE_RATE_HZ = 16000
AUDIO_CHANNELS = 1
AUDIO_SAMPLE_WIDTH_BYTES = 2  # PCM16LE
AUDIO_FORMAT = "pcm16"

# Resource limits.
MAX_APPEND_BYTES = 1 * 1024 * 1024  # 1 MiB per append event
MAX_UNCOMMITTED_SECONDS = 300
MAX_UNCOMMITTED_BYTES = (
    MAX_UNCOMMITTED_SECONDS * AUDIO_SAMPLE_RATE_HZ * AUDIO_CHANNELS * AUDIO_SAMPLE_WIDTH_BYTES
)
# Per-session outbound-write high-water mark (bytes). When the socket's
# pending write buffer exceeds this, the server closes the session with
# ``send_queue_overflow`` rather than blocking the decode loop on a slow
# consumer. 1 MiB is well above a typical burst of small JSON events.
SEND_QUEUE_HIGH_WATER_BYTES = 1 * 1024 * 1024
SHUTDOWN_DRAIN_TIMEOUT_SECONDS = 10.0


# --- Client -> server event types ---
EVT_SESSION_UPDATE = "session.update"
# OpenAI transcription-mode alias. Handled identically to session.update.
EVT_TRANSCRIPTION_SESSION_UPDATE = "transcription_session.update"
EVT_AUDIO_APPEND = "input_audio_buffer.append"
EVT_AUDIO_COMMIT = "input_audio_buffer.commit"
EVT_AUDIO_CLEAR = "input_audio_buffer.clear"
EVT_SERVER_STATUS_REQ = "server.status"
EVT_SESSION_CLOSE = "session.close"
EVT_SESSION_CANCEL = "session.cancel"

CLIENT_EVENT_TYPES = frozenset(
    {
        EVT_SESSION_UPDATE,
        EVT_TRANSCRIPTION_SESSION_UPDATE,
        EVT_AUDIO_APPEND,
        EVT_AUDIO_COMMIT,
        EVT_AUDIO_CLEAR,
        EVT_SERVER_STATUS_REQ,
        EVT_SESSION_CLOSE,
        EVT_SESSION_CANCEL,
    }
)

# --- Server -> client event types ---
EVT_SERVER_HELLO = "server.hello"
EVT_SESSION_CREATED = "session.created"
EVT_SESSION_UPDATED = "session.updated"
# OpenAI transcription-mode counterparts, emitted alongside session.* so
# strict OpenAI clients see the event names they expect.
EVT_TRANSCRIPTION_SESSION_CREATED = "transcription_session.created"
EVT_TRANSCRIPTION_SESSION_UPDATED = "transcription_session.updated"
EVT_SESSION_CLOSED = "session.closed"
EVT_AUDIO_COMMITTED = "input_audio_buffer.committed"
EVT_AUDIO_CLEARED = "input_audio_buffer.cleared"
EVT_TRANSCRIPT_DELTA = "conversation.item.input_audio_transcription.delta"
EVT_TRANSCRIPT_COMPLETED = "conversation.item.input_audio_transcription.completed"
EVT_TRANSCRIPT_FAILED = "conversation.item.input_audio_transcription.failed"
EVT_SERVER_STATUS = "server.status"
EVT_ERROR = "error"


class ErrorCode(str, Enum):
    INVALID_JSON = "invalid_json"
    INVALID_EVENT = "invalid_event"
    UNSUPPORTED_EVENT = "unsupported_event"
    INVALID_CONFIG = "invalid_config"
    BUFFER_EMPTY = "buffer_empty"
    BUFFER_OVERFLOW = "buffer_overflow"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    BACKEND_ERROR = "backend_error"
    UNAUTHORIZED = "unauthorized"
    INTERNAL_ERROR = "internal_error"
