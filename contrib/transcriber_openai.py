"""Speech-to-text via the OpenAI-compatible /audio/transcriptions API."""

import json
import logging
import urllib.error
import urllib.request
import uuid

logger = logging.getLogger(__name__)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            req.full_url, code,
            f"redirect to {newurl!r} blocked: Bearer token must not follow redirects",
            headers, fp,
        )


_no_redirect_opener = urllib.request.build_opener(_NoRedirectHandler)

_MAX_AUDIO_BYTES = 25 * 1024 * 1024
_MAX_RESPONSE_BYTES = 1024 * 1024
_TIMEOUT = 60

_MIME_EXTENSIONS = {
    "audio/ogg": "ogg",
    "audio/mpeg": "mp3",
    "audio/mp4": "m4a",
    "audio/wav": "wav",
    "audio/webm": "webm",
    "audio/flac": "flac",
}


def _multipart_body(
    fields: dict[str, str], filename: str, file_bytes: bytes, file_mime: str,
) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n".encode()
        )
    parts.append(
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: {file_mime}\r\n\r\n".encode()
    )
    parts.append(file_bytes)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


class OpenAITranscriber:
    def __init__(
        self,
        api_key: str = "",
        base_url: str = "https://api.openai.com/v1",
        model: str = "whisper-1",
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model

    def transcribe(self, audio: bytes, mime_type: str) -> str:
        if not self.api_key:
            raise RuntimeError("transcriber API key is not set")
        if len(audio) > _MAX_AUDIO_BYTES:
            raise RuntimeError("audio exceeds 25MB transcription limit")

        mime = mime_type or "audio/ogg"
        ext = _MIME_EXTENSIONS.get(mime, "ogg")
        body, content_type = _multipart_body(
            {"model": self.model}, f"voice.{ext}", audio, mime,
        )
        req = urllib.request.Request(
            f"{self.base_url}/audio/transcriptions",
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": content_type,
            },
            method="POST",
        )

        try:
            with _no_redirect_opener.open(req, timeout=_TIMEOUT) as resp:
                raw = resp.read(_MAX_RESPONSE_BYTES + 1)
                if len(raw) > _MAX_RESPONSE_BYTES:
                    raise RuntimeError("transcription response exceeded size limit")
                data = json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as e:
            logger.error("transcription HTTP %d: %s", e.code, e.reason)
            raise RuntimeError(f"transcription API error: HTTP {e.code}") from e
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            logger.error("transcription failed: %s", e)
            raise RuntimeError(f"transcription API error: {e}") from e
        except json.JSONDecodeError as e:
            raise RuntimeError("transcription API returned invalid JSON") from e

        if not isinstance(data, dict) or not isinstance(data.get("text"), str):
            raise RuntimeError("transcription API returned unexpected response shape")
        return data["text"].strip()
