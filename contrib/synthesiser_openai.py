"""Text-to-speech via the OpenAI-compatible /audio/speech API."""

import json
import logging
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            req.full_url, code,
            f"redirect to {newurl!r} blocked: Bearer token must not follow redirects",
            headers, fp,
        )


_no_redirect_opener = urllib.request.build_opener(_NoRedirectHandler)

_MAX_INPUT_CHARS = 4096
_MAX_RESPONSE_BYTES = 10 * 1024 * 1024
_TIMEOUT = 60


class OpenAISynthesiser:
    def __init__(
        self,
        api_key: str = "",
        base_url: str = "https://api.openai.com/v1",
        model: str = "tts-1",
        voice: str = "alloy",
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.voice = voice

    def synthesise(self, text: str) -> tuple[bytes, str] | None:
        if not text.strip():
            return None
        if not self.api_key:
            raise RuntimeError("synthesiser API key is not set")

        if len(text) > _MAX_INPUT_CHARS:
            logger.warning(
                "synthesis input truncated from %d to %d chars",
                len(text), _MAX_INPUT_CHARS,
            )
            text = text[:_MAX_INPUT_CHARS]

        body = json.dumps({
            "model": self.model,
            "voice": self.voice,
            "input": text,
            "response_format": "opus",
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/audio/speech",
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            with _no_redirect_opener.open(req, timeout=_TIMEOUT) as resp:
                audio = resp.read(_MAX_RESPONSE_BYTES + 1)
                if len(audio) > _MAX_RESPONSE_BYTES:
                    raise RuntimeError("synthesis response exceeded size limit")
        except urllib.error.HTTPError as e:
            logger.error("synthesis HTTP %d: %s", e.code, e.reason)
            raise RuntimeError(f"synthesis API error: HTTP {e.code}") from e
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            logger.error("synthesis failed: %s", e)
            raise RuntimeError(f"synthesis API error: {e}") from e

        if not audio:
            return None
        return audio, "audio/ogg"
