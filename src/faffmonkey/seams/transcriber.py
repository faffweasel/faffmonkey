from typing import Protocol, runtime_checkable


@runtime_checkable
class Transcriber(Protocol):
    def transcribe(self, audio: bytes, mime_type: str) -> str: ...


class TranscriptionNotConfigured(RuntimeError):
    pass


class NoopTranscriber:
    def transcribe(self, audio: bytes, mime_type: str) -> str:
        """Refuse rather than return a placeholder.

        A placeholder string would be persisted as the user's own words
        and answered by the model.
        """
        raise TranscriptionNotConfigured(
            "voice transcription is not configured; run: faff setup voice"
        )
