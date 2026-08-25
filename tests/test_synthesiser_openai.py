import io
import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from contrib.synthesiser_openai import (
    OpenAISynthesiser,
    _MAX_INPUT_CHARS,
    _MAX_RESPONSE_BYTES,
)


def _mock_response(body: bytes) -> MagicMock:
    mock_resp = MagicMock()
    mock_resp.read.return_value = body
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


class TestOpenAISynthesiser:
    def test_empty_text_returns_none(self):
        s = OpenAISynthesiser(api_key="key")
        assert s.synthesise("") is None
        assert s.synthesise("   ") is None

    def test_requires_api_key(self):
        s = OpenAISynthesiser(api_key="")
        with pytest.raises(RuntimeError, match="API key"):
            s.synthesise("hello")

    def test_strips_trailing_slash_from_base_url(self):
        s = OpenAISynthesiser(api_key="key", base_url="https://api.example.com/v1/")
        assert s.base_url == "https://api.example.com/v1"

    @patch("contrib.synthesiser_openai._no_redirect_opener.open")
    def test_synthesises_audio(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response(b"OGGDATA")
        s = OpenAISynthesiser(api_key="key", model="tts-1", voice="alloy")

        result = s.synthesise("hello world")

        assert result == (b"OGGDATA", "audio/ogg")
        req = mock_urlopen.call_args[0][0]
        assert req.full_url == "https://api.openai.com/v1/audio/speech"
        assert req.get_header("Authorization") == "Bearer key"
        payload = json.loads(req.data.decode())
        assert payload == {
            "model": "tts-1",
            "voice": "alloy",
            "input": "hello world",
            "response_format": "opus",
        }

    @patch("contrib.synthesiser_openai._no_redirect_opener.open")
    def test_truncates_long_input(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response(b"OGGDATA")
        s = OpenAISynthesiser(api_key="key")

        s.synthesise("x" * (_MAX_INPUT_CHARS + 500))

        payload = json.loads(mock_urlopen.call_args[0][0].data.decode())
        assert len(payload["input"]) == _MAX_INPUT_CHARS

    @patch("contrib.synthesiser_openai._no_redirect_opener.open")
    def test_empty_audio_returns_none(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response(b"")
        s = OpenAISynthesiser(api_key="key")
        assert s.synthesise("hello") is None

    @patch("contrib.synthesiser_openai._no_redirect_opener.open")
    def test_http_error_raises_runtime_error(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.HTTPError(
            "url", 429, "Too Many Requests", {}, io.BytesIO(b""),
        )
        s = OpenAISynthesiser(api_key="key")
        with pytest.raises(RuntimeError, match="HTTP 429"):
            s.synthesise("hello")

    @patch("contrib.synthesiser_openai._no_redirect_opener.open")
    def test_url_error_raises_runtime_error(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.URLError("connection refused")
        s = OpenAISynthesiser(api_key="key")
        with pytest.raises(RuntimeError, match="synthesis API error"):
            s.synthesise("hello")

    @patch("contrib.synthesiser_openai._no_redirect_opener.open")
    def test_oversized_response_raises_runtime_error(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response(b"x" * (_MAX_RESPONSE_BYTES + 1))
        s = OpenAISynthesiser(api_key="key")
        with pytest.raises(RuntimeError, match="size limit"):
            s.synthesise("hello")
