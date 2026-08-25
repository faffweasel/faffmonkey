from typing import Protocol, runtime_checkable


@runtime_checkable
class Transcriber(Protocol):
    def transcribe(self, audio: bytes, mime_type: str) -> str: ...


class TranscriptionNotConfigured(RuntimeError):
    pass


class NoopTranscriber:
    def transcribe(self, audio: bytes, mime_type: str) -> str:
        """Refuse rather than return a placeholder.

        The placeholder was persisted as the user's own words, so the
        conversation recorded the user saying "[transcription not
        configured]" and the model answered it.
        """
        raise TranscriptionNotConfigured(
            "voice transcription is not configured; run: faff setup voice"
        )
