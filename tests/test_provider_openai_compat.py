import json
import urllib.error
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest

from faffmonkey.config import ConfigError
from faffmonkey.seams.provider_openai_compat import (
    OpenAICompatProvider,
    _MAX_RESPONSE_BYTES,
)
from faffmonkey.types import (
    AuthError,
    CompletionRequest,
    CompletionResponse,
    ContextLengthError,
    Message,
    ProviderError,
    ProviderUnavailableError,
    RetryableError,
)


def _make_response(body: dict, status: int = 200) -> MagicMock:
    raw = json.dumps(body).encode()
    mock = MagicMock()
    mock.read.return_value = raw
    mock.status = status
    mock.__enter__ = lambda s: s
    mock.__exit__ = MagicMock(return_value=False)
    return mock


def _make_completion_body(
    text: str = "hello",
    model: str = "test-model",
    tool_calls: list | None = None,
) -> dict:
    message: dict = {"content": text, "role": "assistant"}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "model": model,
        "choices": [{"message": message, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def _make_request(**kwargs) -> CompletionRequest:
    defaults = {
        "messages": [Message(role="user", content="hi")],
        "model": "test-model",
    }
    defaults.update(kwargs)
    return CompletionRequest(**defaults)


class TestComplete:
    def test_success(self):
        provider = OpenAICompatProvider("http://localhost:8080/v1", "test-key")
        body = _make_completion_body(text="hello world")
        mock_resp = _make_response(body)

        with patch("faffmonkey.seams.provider_openai_compat._no_redirect_opener.open", return_value=mock_resp) as mock_open:
            result = provider.complete(_make_request())

        assert isinstance(result, CompletionResponse)
        assert result.text == "hello world"
        assert result.model == "test-model"
        assert result.tool_calls is None
        assert result.usage.total_tokens == 15

        call_args = mock_open.call_args
        req = call_args[0][0]
        assert req.full_url == "http://localhost:8080/v1/chat/completions"
        assert req.get_header("Authorization") == "Bearer test-key"
        assert req.get_header("Content-type") == "application/json"

    def test_tool_calls_parsed(self):
        provider = OpenAICompatProvider("http://localhost:8080/v1")
        tool_calls = [
            {
                "id": "call_123",
                "type": "function",
                "function": {
                    "name": "file_read",
                    "arguments": '{"path": "workspace/test.md"}',
                },
            }
        ]
        body = _make_completion_body(text="", tool_calls=tool_calls)
        mock_resp = _make_response(body)

        with patch("faffmonkey.seams.provider_openai_compat._no_redirect_opener.open", return_value=mock_resp):
            result = provider.complete(_make_request())

        assert result.tool_calls is not None
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].id == "call_123"
        assert result.tool_calls[0].name == "file_read"
        assert result.tool_calls[0].arguments == {"path": "workspace/test.md"}

    def test_sends_tools_and_tool_choice(self):
        provider = OpenAICompatProvider("http://localhost:8080/v1")
        tools = [{"type": "function", "function": {"name": "test"}}]
        req = _make_request(tools=tools, tool_choice="auto")
        body = _make_completion_body()
        mock_resp = _make_response(body)

        with patch("faffmonkey.seams.provider_openai_compat._no_redirect_opener.open", return_value=mock_resp) as mock_open:
            provider.complete(req)

        sent = json.loads(mock_open.call_args[0][0].data)
        assert sent["tools"] == tools
        assert sent["tool_choice"] == "auto"

    def test_sends_temperature_and_max_tokens(self):
        provider = OpenAICompatProvider("http://localhost:8080/v1")
        req = _make_request(temperature=0.7, max_tokens=100)
        body = _make_completion_body()
        mock_resp = _make_response(body)

        with patch("faffmonkey.seams.provider_openai_compat._no_redirect_opener.open", return_value=mock_resp) as mock_open:
            provider.complete(req)

        sent = json.loads(mock_open.call_args[0][0].data)
        assert sent["temperature"] == 0.7
        assert sent["max_tokens"] == 100

    def test_omits_none_optional_fields(self):
        provider = OpenAICompatProvider("http://localhost:8080/v1")
        req = _make_request()
        body = _make_completion_body()
        mock_resp = _make_response(body)

        with patch("faffmonkey.seams.provider_openai_compat._no_redirect_opener.open", return_value=mock_resp) as mock_open:
            provider.complete(req)

        sent = json.loads(mock_open.call_args[0][0].data)
        assert "tools" not in sent
        assert "tool_choice" not in sent
        assert "temperature" not in sent
        assert "max_tokens" not in sent

    def test_no_api_key_skips_auth_header(self):
        provider = OpenAICompatProvider("http://localhost:8080/v1")
        body = _make_completion_body()
        mock_resp = _make_response(body)

        with patch("faffmonkey.seams.provider_openai_compat._no_redirect_opener.open", return_value=mock_resp) as mock_open:
            provider.complete(_make_request())

        req = mock_open.call_args[0][0]
        assert req.get_header("Authorization") is None

    def test_empty_choices(self):
        provider = OpenAICompatProvider("http://localhost:8080/v1")
        body = {"model": "test", "choices": []}
        mock_resp = _make_response(body)

        with patch("faffmonkey.seams.provider_openai_compat._no_redirect_opener.open", return_value=mock_resp):
            result = provider.complete(_make_request())

        assert result.text == ""


class TestErrorHandling:
    def _make_http_error(self, code: int, headers: dict | None = None) -> urllib.error.HTTPError:
        mock_headers = MagicMock()
        mock_headers.get = lambda key, default=None: (headers or {}).get(key, default)
        err = urllib.error.HTTPError(
            url="http://test/v1/chat/completions",
            code=code,
            msg=f"HTTP {code}",
            hdrs=mock_headers,
            fp=BytesIO(b"error"),
        )
        return err

    def test_401_raises_auth_error(self):
        provider = OpenAICompatProvider("http://localhost:8080/v1", "bad-key")
        with patch("faffmonkey.seams.provider_openai_compat._no_redirect_opener.open", side_effect=self._make_http_error(401)):
            with pytest.raises(AuthError, match="401"):
                provider.complete(_make_request())

    def test_403_raises_auth_error(self):
        provider = OpenAICompatProvider("http://localhost:8080/v1", "bad-key")
        with patch("faffmonkey.seams.provider_openai_compat._no_redirect_opener.open", side_effect=self._make_http_error(403)):
            with pytest.raises(AuthError, match="403"):
                provider.complete(_make_request())

    def test_429_raises_retryable_with_retry_after(self):
        provider = OpenAICompatProvider("http://localhost:8080/v1")
        err = self._make_http_error(429, headers={"retry-after": "5"})
        with patch("faffmonkey.seams.provider_openai_compat._no_redirect_opener.open", side_effect=err):
            with pytest.raises(RetryableError) as exc_info:
                provider.complete(_make_request())
        assert exc_info.value.retry_after == 5.0

    def test_429_without_retry_after(self):
        provider = OpenAICompatProvider("http://localhost:8080/v1")
        err = self._make_http_error(429)
        with patch("faffmonkey.seams.provider_openai_compat._no_redirect_opener.open", side_effect=err):
            with pytest.raises(RetryableError) as exc_info:
                provider.complete(_make_request())
        assert exc_info.value.retry_after is None

    def test_429_non_numeric_retry_after_falls_back(self):
        provider = OpenAICompatProvider("http://localhost:8080/v1")
        err = self._make_http_error(429, headers={"retry-after": "not-a-number"})
        with patch("faffmonkey.seams.provider_openai_compat._no_redirect_opener.open", side_effect=err):
            with pytest.raises(RetryableError) as exc_info:
                provider.complete(_make_request())
        assert exc_info.value.retry_after is None

    def test_429_negative_retry_after_clamped_to_zero(self):
        provider = OpenAICompatProvider("http://localhost:8080/v1")
        err = self._make_http_error(429, headers={"retry-after": "-10"})
        with patch("faffmonkey.seams.provider_openai_compat._no_redirect_opener.open", side_effect=err):
            with pytest.raises(RetryableError) as exc_info:
                provider.complete(_make_request())
        assert exc_info.value.retry_after == 0.0

    def test_429_large_retry_after_clamped_to_300(self):
        provider = OpenAICompatProvider("http://localhost:8080/v1")
        err = self._make_http_error(429, headers={"retry-after": "9999"})
        with patch("faffmonkey.seams.provider_openai_compat._no_redirect_opener.open", side_effect=err):
            with pytest.raises(RetryableError) as exc_info:
                provider.complete(_make_request())
        assert exc_info.value.retry_after == 300.0

    def test_500_raises_retryable(self):
        provider = OpenAICompatProvider("http://localhost:8080/v1")
        with patch("faffmonkey.seams.provider_openai_compat._no_redirect_opener.open", side_effect=self._make_http_error(500)):
            with pytest.raises(RetryableError, match="500"):
                provider.complete(_make_request())

    def test_502_raises_retryable(self):
        provider = OpenAICompatProvider("http://localhost:8080/v1")
        with patch("faffmonkey.seams.provider_openai_compat._no_redirect_opener.open", side_effect=self._make_http_error(502)):
            with pytest.raises(RetryableError, match="502"):
                provider.complete(_make_request())

    def test_503_raises_retryable(self):
        provider = OpenAICompatProvider("http://localhost:8080/v1")
        with patch("faffmonkey.seams.provider_openai_compat._no_redirect_opener.open", side_effect=self._make_http_error(503)):
            with pytest.raises(RetryableError, match="503"):
                provider.complete(_make_request())

    def test_timeout_raises_retryable(self):
        provider = OpenAICompatProvider("http://localhost:8080/v1")
        with patch("faffmonkey.seams.provider_openai_compat._no_redirect_opener.open", side_effect=TimeoutError("timed out")):
            with pytest.raises(RetryableError, match="timed out"):
                provider.complete(_make_request())

    def test_socket_timeout_raises_retryable(self):
        provider = OpenAICompatProvider("http://localhost:8080/v1")
        with patch("faffmonkey.seams.provider_openai_compat._no_redirect_opener.open", side_effect=OSError("timed out")):
            with pytest.raises(RetryableError, match="timed out"):
                provider.complete(_make_request())

    def test_connection_refused_raises_unavailable(self):
        provider = OpenAICompatProvider("http://localhost:8080/v1")
        err = urllib.error.URLError("Connection refused")
        with patch("faffmonkey.seams.provider_openai_compat._no_redirect_opener.open", side_effect=err):
            with pytest.raises(ProviderUnavailableError, match="connection refused"):
                provider.complete(_make_request())

    def test_other_url_error_raises_retryable(self):
        provider = OpenAICompatProvider("http://localhost:8080/v1")
        err = urllib.error.URLError("Name resolution failed")
        with patch("faffmonkey.seams.provider_openai_compat._no_redirect_opener.open", side_effect=err):
            with pytest.raises(RetryableError, match="Name resolution"):
                provider.complete(_make_request())


class TestCleartextBearerRejection:
    def test_http_remote_with_bearer_raises(self):
        with pytest.raises(ConfigError, match="refusing to send Bearer token over http://"):
            OpenAICompatProvider("http://example.com/v1", api_key="sk-test")

    def test_http_localhost_with_bearer_allowed(self):
        p = OpenAICompatProvider("http://localhost:11434/v1", api_key="sk-test")
        assert p.api_key == "sk-test"

    def test_http_127_with_bearer_allowed(self):
        p = OpenAICompatProvider("http://127.0.0.1:11434/v1", api_key="sk-test")
        assert p.api_key == "sk-test"

    def test_http_ipv6_loopback_with_bearer_allowed(self):
        p = OpenAICompatProvider("http://[::1]:11434/v1", api_key="sk-test")
        assert p.api_key == "sk-test"

    def test_https_remote_with_bearer_allowed(self):
        p = OpenAICompatProvider("https://api.openai.com/v1", api_key="sk-test")
        assert p.api_key == "sk-test"

    def test_http_remote_without_bearer_allowed(self):
        p = OpenAICompatProvider("http://example.com/v1")
        assert p.api_key == ""


class TestResponseSizeLimit:
    def test_success_path_oversized_raises_provider_error(self):
        provider = OpenAICompatProvider("http://localhost:8080/v1")
        mock = MagicMock()
        mock.read.return_value = b"x" * (_MAX_RESPONSE_BYTES + 2)
        mock.status = 200
        mock.__enter__ = lambda s: s
        mock.__exit__ = MagicMock(return_value=False)

        with patch("faffmonkey.seams.provider_openai_compat._no_redirect_opener.open", return_value=mock):
            with pytest.raises(ProviderError, match="byte limit"):
                provider.complete(_make_request())

    def test_error_path_oversized_raises_provider_error(self):
        provider = OpenAICompatProvider("http://localhost:8080/v1")
        mock_headers = MagicMock()
        mock_headers.get = lambda key, default=None: None
        err = urllib.error.HTTPError(
            url="http://test/v1/chat/completions",
            code=400,
            msg="HTTP 400",
            hdrs=mock_headers,
            fp=BytesIO(b"x" * (_MAX_RESPONSE_BYTES + 2)),
        )

        with patch("faffmonkey.seams.provider_openai_compat._no_redirect_opener.open", side_effect=err):
            with pytest.raises(ProviderError, match="byte limit"):
                provider.complete(_make_request())


class TestRetryAfterNonFinite:
    def _make_http_error(self, code: int, headers: dict | None = None) -> urllib.error.HTTPError:
        mock_headers = MagicMock()
        mock_headers.get = lambda key, default=None: (headers or {}).get(key, default)
        return urllib.error.HTTPError(
            url="http://test/v1/chat/completions",
            code=code,
            msg=f"HTTP {code}",
            hdrs=mock_headers,
            fp=BytesIO(b"error"),
        )

    def test_429_nan_retry_after_becomes_zero(self):
        provider = OpenAICompatProvider("http://localhost:8080/v1")
        err = self._make_http_error(429, headers={"retry-after": "nan"})
        with patch("faffmonkey.seams.provider_openai_compat._no_redirect_opener.open", side_effect=err):
            with pytest.raises(RetryableError) as exc_info:
                provider.complete(_make_request())
        assert exc_info.value.retry_after == 0.0

    def test_429_inf_retry_after_becomes_zero(self):
        provider = OpenAICompatProvider("http://localhost:8080/v1")
        err = self._make_http_error(429, headers={"retry-after": "inf"})
        with patch("faffmonkey.seams.provider_openai_compat._no_redirect_opener.open", side_effect=err):
            with pytest.raises(RetryableError) as exc_info:
                provider.complete(_make_request())
        assert exc_info.value.retry_after == 0.0


class TestContentPartsArray:
    def test_content_parts_array_joined(self):
        provider = OpenAICompatProvider("http://localhost:8080/v1")
        body = {
            "model": "test-model",
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Hello "},
                        {"type": "text", "text": "world"},
                    ],
                },
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
        mock_resp = _make_response(body)
        with patch("faffmonkey.seams.provider_openai_compat._no_redirect_opener.open", return_value=mock_resp):
            result = provider.complete(_make_request())
        assert result.text == "Hello world"

    def test_non_dict_choice_returns_empty(self):
        provider = OpenAICompatProvider("http://localhost:8080/v1")
        body = {
            "model": "test-model",
            "choices": ["not a dict"],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
        mock_resp = _make_response(body)
        with patch("faffmonkey.seams.provider_openai_compat._no_redirect_opener.open", return_value=mock_resp):
            result = provider.complete(_make_request())
        assert result.text == ""
        assert result.model == "test-model"

    def test_non_dict_message_returns_empty(self):
        provider = OpenAICompatProvider("http://localhost:8080/v1")
        body = {
            "model": "test-model",
            "choices": [{"message": "not a dict", "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
        mock_resp = _make_response(body)
        with patch("faffmonkey.seams.provider_openai_compat._no_redirect_opener.open", return_value=mock_resp):
            result = provider.complete(_make_request())
        assert result.text == ""

    def test_content_none_returns_empty(self):
        provider = OpenAICompatProvider("http://localhost:8080/v1")
        body = {
            "model": "test-model",
            "choices": [{"message": {"role": "assistant", "content": None}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
        mock_resp = _make_response(body)
        with patch("faffmonkey.seams.provider_openai_compat._no_redirect_opener.open", return_value=mock_resp):
            result = provider.complete(_make_request())
        assert result.text == ""


class TestSchemeAllowlist:
    def test_file_scheme_rejected(self):
        with pytest.raises(ConfigError, match="unsupported URL scheme"):
            OpenAICompatProvider("file:///etc/passwd")

    def test_ftp_scheme_rejected(self):
        with pytest.raises(ConfigError, match="unsupported URL scheme"):
            OpenAICompatProvider("ftp://evil.example.com/v1")

    def test_http_scheme_allowed(self):
        p = OpenAICompatProvider("http://localhost:8080/v1")
        assert p.base_url == "http://localhost:8080/v1"

    def test_https_scheme_allowed(self):
        p = OpenAICompatProvider("https://api.example.com/v1")
        assert p.base_url == "https://api.example.com/v1"


class TestMalformedToolCalls:
    """C3: `call.arguments | {...}` raised TypeError on anything but a dict."""

    def _parse(self, tool_calls):
        provider = OpenAICompatProvider("http://localhost:11434/v1")
        return provider._parse_response({
            "model": "test",
            "choices": [{"message": {"content": "", "tool_calls": tool_calls}}],
        })

    def test_null_arguments_coerce_to_empty_dict(self):
        resp = self._parse([{"id": "a", "function": {"name": "file_read", "arguments": "null"}}])
        assert resp.tool_calls[0].arguments == {}

    def test_array_arguments_coerce_to_empty_dict(self):
        resp = self._parse([{"id": "a", "function": {"name": "f", "arguments": "[1, 2]"}}])
        assert resp.tool_calls[0].arguments == {}

    def test_bare_string_arguments_coerce_to_empty_dict(self):
        resp = self._parse([{"id": "a", "function": {"name": "f", "arguments": '"hello"'}}])
        assert resp.tool_calls[0].arguments == {}

    def test_unparseable_arguments_coerce_to_empty_dict(self):
        resp = self._parse([{"id": "a", "function": {"name": "f", "arguments": "{not json"}}])
        assert resp.tool_calls[0].arguments == {}

    def test_non_object_tool_call_is_dropped(self):
        resp = self._parse(["nonsense", {"id": "a", "function": {"name": "f", "arguments": "{}"}}])
        assert [tc.id for tc in resp.tool_calls] == ["a"]

    def test_tool_call_without_a_function_object_is_dropped(self):
        assert self._parse([{"id": "a"}]).tool_calls is None


class TestReasoningFieldFallback:
    """2026-08-24: Kimi K2.6 via Ollama Cloud answered entirely in the
    reasoning field with empty content; the parser read only content, so
    the morning cron job logged "empty response after retries" and
    delivered nothing."""

    def _parse(self, message, finish_reason=None):
        provider = OpenAICompatProvider("http://localhost:11434/v1")
        choice = {"message": message}
        if finish_reason is not None:
            choice["finish_reason"] = finish_reason
        return provider._parse_response({"model": "test", "choices": [choice]})

    def test_empty_content_falls_back_to_reasoning(self):
        resp = self._parse({"content": "", "reasoning": "Good morning, AQI is 42."})
        assert resp.text == "Good morning, AQI is 42."

    def test_empty_content_falls_back_to_reasoning_content(self):
        resp = self._parse({"content": None, "reasoning_content": "the answer"})
        assert resp.text == "the answer"

    def test_tool_calls_keep_reasoning_out_of_text(self):
        resp = self._parse({
            "content": "",
            "reasoning": "thinking about which tool to call",
            "tool_calls": [{"id": "a", "function": {"name": "f", "arguments": "{}"}}],
        })
        assert resp.text == ""
        assert resp.tool_calls[0].name == "f"

    def test_normal_content_ignores_reasoning(self):
        resp = self._parse({"content": "hello", "reasoning": "hidden thoughts"})
        assert resp.text == "hello"

    def test_empty_everything_stays_empty(self):
        resp = self._parse({"content": ""}, finish_reason="length")
        assert resp.text == ""


class TestErrorBodySurfaced:
    """A rejected request must say why. The 400 handler read the body
    and re-raised the bare HTTPError, so the operator's log said
    "HTTP Error 400: Bad Request" and nothing else (2026-08-25)."""

    def _http_error(self, code: int, body: bytes) -> urllib.error.HTTPError:
        return urllib.error.HTTPError(
            url="http://test/v1/chat/completions",
            code=code,
            msg="Bad Request",
            hdrs=MagicMock(),
            fp=BytesIO(body),
        )

    def _complete_with(self, err: urllib.error.HTTPError):
        provider = OpenAICompatProvider("http://localhost:8080/v1")
        with patch("faffmonkey.seams.provider_openai_compat._no_redirect_opener.open", side_effect=err):
            provider.complete(_make_request())

    def test_400_openai_error_object_names_field_and_reason(self):
        body = json.dumps({"error": {
            "message": "Invalid content type. image_url is only supported by certain models.",
            "type": "invalid_request_error",
            "param": "messages.[1].content.[1].type",
            "code": None,
        }}).encode()
        with pytest.raises(ProviderError) as exc:
            self._complete_with(self._http_error(400, body))
        assert "HTTP 400" in str(exc.value)
        assert "image_url is only supported" in str(exc.value)
        assert "messages.[1].content.[1].type" in str(exc.value)

    def test_400_plain_text_body_is_kept(self):
        with pytest.raises(ProviderError, match="model 'nope' not found"):
            self._complete_with(self._http_error(400, b"model 'nope' not found"))

    def test_400_context_length_still_maps(self):
        body = json.dumps({"error": {
            "message": "This model's maximum context length is 8192 tokens.",
            "code": "context_length_exceeded",
        }}).encode()
        with pytest.raises(ContextLengthError, match="8192"):
            self._complete_with(self._http_error(400, body))

    def test_unmapped_status_is_a_provider_error_not_a_bare_httperror(self):
        body = json.dumps({"error": {"message": "The model does not exist"}}).encode()
        with pytest.raises(ProviderError, match="HTTP 404: The model does not exist"):
            self._complete_with(self._http_error(404, body))
