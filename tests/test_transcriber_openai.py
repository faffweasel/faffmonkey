import io
import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from contrib.transcriber_openai import (
    OpenAITranscriber,
    _MAX_AUDIO_BYTES,
    _MAX_RESPONSE_BYTES,
    _multipart_body,
)


def _mock_response(payload: object) -> MagicMock:
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    mock_resp = MagicMock()
    mock_resp.read.return_value = body
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


class TestMultipartBody:
    def test_contains_fields_and_file(self):
        body, content_type = _multipart_body(
            {"model": "whisper-1"}, "voice.ogg", b"AUDIO", "audio/ogg",
        )
        assert content_type.startswith("multipart/form-data; boundary=")
        boundary = content_type.split("boundary=")[1]
        assert boundary.encode() in body
        assert b'name="model"' in body
        assert b"whisper-1" in body
        assert b'filename="voice.ogg"' in body
        assert b"Content-Type: audio/ogg" in body
        assert b"AUDIO" in body
        assert body.endswith(f"--{boundary}--\r\n".encode())


class TestOpenAITranscriber:
    def test_requires_api_key(self):
        t = OpenAITranscriber(api_key="")
        with pytest.raises(RuntimeError, match="API key"):
            t.transcribe(b"audio", "audio/ogg")

    def test_rejects_oversized_audio(self):
        t = OpenAITranscriber(api_key="key")
        with pytest.raises(RuntimeError, match="25MB"):
            t.transcribe(b"x" * (_MAX_AUDIO_BYTES + 1), "audio/ogg")

    def test_strips_trailing_slash_from_base_url(self):
        t = OpenAITranscriber(api_key="key", base_url="https://api.example.com/v1/")
        assert t.base_url == "https://api.example.com/v1"

    @patch("contrib.transcriber_openai._no_redirect_opener.open")
    def test_transcribes_audio(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response({"text": "  hello world  "})
        t = OpenAITranscriber(api_key="key", model="whisper-1")

        result = t.transcribe(b"AUDIO", "audio/ogg")

        assert result == "hello world"
        req = mock_urlopen.call_args[0][0]
        assert req.full_url == "https://api.openai.com/v1/audio/transcriptions"
        assert req.get_header("Authorization") == "Bearer key"
        assert b"whisper-1" in req.data
        assert b'filename="voice.ogg"' in req.data

    @patch("contrib.transcriber_openai._no_redirect_opener.open")
    def test_maps_mime_to_filename_extension(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response({"text": "ok"})
        t = OpenAITranscriber(api_key="key")

        t.transcribe(b"AUDIO", "audio/mpeg")

        req = mock_urlopen.call_args[0][0]
        assert b'filename="voice.mp3"' in req.data

    @patch("contrib.transcriber_openai._no_redirect_opener.open")
    def test_unknown_mime_defaults_to_ogg(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response({"text": "ok"})
        t = OpenAITranscriber(api_key="key")

        t.transcribe(b"AUDIO", "audio/weird")

        req = mock_urlopen.call_args[0][0]
        assert b'filename="voice.ogg"' in req.data

    @patch("contrib.transcriber_openai._no_redirect_opener.open")
    def test_http_error_raises_runtime_error(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.HTTPError(
            "url", 401, "Unauthorized", {}, io.BytesIO(b""),
        )
        t = OpenAITranscriber(api_key="bad-key")
        with pytest.raises(RuntimeError, match="HTTP 401"):
            t.transcribe(b"AUDIO", "audio/ogg")

    @patch("contrib.transcriber_openai._no_redirect_opener.open")
    def test_url_error_raises_runtime_error(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.URLError("connection refused")
        t = OpenAITranscriber(api_key="key")
        with pytest.raises(RuntimeError, match="transcription API error"):
            t.transcribe(b"AUDIO", "audio/ogg")

    @patch("contrib.transcriber_openai._no_redirect_opener.open")
    def test_invalid_json_raises_runtime_error(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response(b"not json")
        t = OpenAITranscriber(api_key="key")
        with pytest.raises(RuntimeError, match="invalid JSON"):
            t.transcribe(b"AUDIO", "audio/ogg")

    @patch("contrib.transcriber_openai._no_redirect_opener.open")
    def test_missing_text_field_raises_runtime_error(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response({"error": "nope"})
        t = OpenAITranscriber(api_key="key")
        with pytest.raises(RuntimeError, match="unexpected response shape"):
            t.transcribe(b"AUDIO", "audio/ogg")

    @patch("contrib.transcriber_openai._no_redirect_opener.open")
    def test_oversized_response_raises_runtime_error(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response(
            b"x" * (_MAX_RESPONSE_BYTES + 1),
        )
        t = OpenAITranscriber(api_key="key")
        with pytest.raises(RuntimeError, match="size limit"):
            t.transcribe(b"AUDIO", "audio/ogg")
