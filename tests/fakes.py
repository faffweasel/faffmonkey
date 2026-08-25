"""Hand-written fakes for the seam Protocols.

A MagicMock stands in for a seam without ever being checked against it:
`runtime_checkable` isinstance compares method NAMES only, so a class
whose `send()` takes no arguments still passes. Tests built on a mock
prove nothing about the seam it replaces. These fakes are checked against
their Protocol in test_seam_conformance.py, signature by signature,
alongside the real implementations.

Each fake records what it was asked to do so a test can assert on
observable effects rather than on call counts.

FauxProvider (tests/faux_provider.py) is the Provider fake and stays
where it is; conformance covers it there.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from faffmonkey.types import InboundMessage, OutboundMessage, SearchResult


def inbound(
    text: str = "hello",
    sender_id: str = "user1",
    channel_id: str = "fake",
    images: list[Path] | None = None,
    audio: bytes | None = None,
    audio_mime: str | None = None,
    attachments: list[Path] | None = None,
    group_id: str | None = None,
) -> InboundMessage:
    """An InboundMessage with the boilerplate filled in."""
    return InboundMessage(
        sender_id=sender_id,
        text=text,
        channel_id=channel_id,
        timestamp=datetime.now(timezone.utc),
        images=list(images or []),
        audio=audio,
        audio_mime=audio_mime,
        attachments=list(attachments or []),
        group_id=group_id,
    )


class FakeChannel:
    """A Channel backed by a scripted inbound queue.

    Access control matches the real channels: an empty allowed_users denies
    everyone. Pass allow_all=True only when a test is not about access.

    A None in the queue is an idle poll, which is what Telegram and Discord
    return on most calls: nothing yet, not the end of the session. Only an
    exhausted queue closes the channel.
    """

    channel_id: str = "fake"

    def __init__(
        self,
        allowed_users: list[str] | None = None,
        workspace: Path | None = None,
        inbound_queue: list[InboundMessage | None] | None = None,
        allow_all: bool = False,
    ) -> None:
        self.allowed_users = list(allowed_users or [])
        self.workspace = workspace
        self._queue = list(inbound_queue or [])
        self._allow_all = allow_all
        self.sent: list[OutboundMessage] = []
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def receive(self) -> InboundMessage | None:
        if not self._queue:
            return None
        # A scripted None is an idle poll, and popping it is what advances
        # the script towards is_closed().
        return self._queue.pop(0)

    def send(self, message: OutboundMessage) -> None:
        self.sent.append(message)

    def is_allowed(self, sender_id: str) -> bool:
        if self._allow_all:
            return True
        return sender_id in self.allowed_users

    def poll(self) -> InboundMessage | None:
        return self.receive()

    def is_closed(self) -> bool:
        return not self._queue

    # -- test helpers, not part of the Protocol --

    def queue(self, message: InboundMessage | None) -> None:
        self._queue.append(message)

    @property
    def sent_text(self) -> list[str]:
        return [m.text for m in self.sent]


class FakeTranscriber:
    """Returns scripted text, or raises what the noop raises when empty."""

    def __init__(self, transcripts: list[str] | None = None) -> None:
        self._transcripts = list(transcripts or [])
        self.calls: list[tuple[int, str]] = []

    def transcribe(self, audio: bytes, mime_type: str) -> str:
        self.calls.append((len(audio), mime_type))
        if not self._transcripts:
            from faffmonkey.seams.transcriber import TranscriptionNotConfigured
            raise TranscriptionNotConfigured("no scripted transcript")
        return self._transcripts.pop(0)


class FakeSynthesiser:
    """Returns fixed audio bytes, or None to model 'synthesis unavailable'."""

    def __init__(self, audio: bytes | None = b"fake-audio", mime: str = "audio/ogg") -> None:
        self._audio = audio
        self._mime = mime
        self.calls: list[str] = []

    def synthesise(self, text: str) -> tuple[bytes, str] | None:
        self.calls.append(text)
        if self._audio is None:
            return None
        return (self._audio, self._mime)


class FakeSearchProvider:
    """Returns scripted results, or raises what the noop raises when empty.

    Never returns [] by default: an empty list reads to the model as "the
    web has no answer", which is the reason the real noop raises.
    """

    def __init__(self, results: list[SearchResult] | None = None) -> None:
        self._results = results
        self.queries: list[tuple[str, int]] = []

    def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        self.queries.append((query, max_results))
        if self._results is None:
            from faffmonkey.seams.search_provider import SearchNotConfigured
            raise SearchNotConfigured("no scripted results")
        return self._results[:max_results]
