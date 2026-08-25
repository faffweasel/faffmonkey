import json
import logging
import math
import urllib.error
import urllib.request
from typing import ClassVar
from urllib.parse import urlsplit

from faffmonkey.config import ConfigError
from faffmonkey.types import (
    AuthError,
    CompletionRequest,
    CompletionResponse,
    ContextLengthError,
    ProviderError,
    ProviderUnavailableError,
    RetryableError,
    ToolCall,
    message_to_dict,
    usage_from_dict,
)

logger = logging.getLogger(__name__)

_MAX_RESPONSE_BYTES = 10 * 1024 * 1024

from faffmonkey.config import LOCAL_HOSTS as _CLEARTEXT_SAFE_HOSTS


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            req.full_url, code,
            f"redirect to {newurl!r} blocked: Bearer token must not follow redirects",
            headers, fp,
        )


_no_redirect_opener = urllib.request.build_opener(_NoRedirectHandler)

_MAX_ERROR_DETAIL = 500


def _error_detail(e: urllib.error.HTTPError) -> str:
    """The provider's own reason for rejecting the request: the 400 body
    names the offending field, and the status line alone cannot be
    diagnosed from the log.
    """
    try:
        raw = e.read(_MAX_RESPONSE_BYTES + 1)
    except (OSError, AttributeError, ValueError):
        return str(e.reason)
    if len(raw) > _MAX_RESPONSE_BYTES:
        raise ProviderError(f"response exceeded {_MAX_RESPONSE_BYTES} byte limit")
    text = raw.decode("utf-8", "replace").strip()
    try:
        body = json.loads(text)
    except ValueError:
        return text[:_MAX_ERROR_DETAIL] or str(e.reason)
    err = body.get("error", body) if isinstance(body, dict) else None
    if isinstance(err, str) and err:
        return err[:_MAX_ERROR_DETAIL]
    if isinstance(err, dict):
        parts = [str(err[k]) for k in ("code", "param", "message") if err.get(k)]
        if parts:
            return ": ".join(parts)[:_MAX_ERROR_DETAIL]
    return text[:_MAX_ERROR_DETAIL] or str(e.reason)


class OpenAICompatProvider:
    RETRYABLE_CODES: ClassVar[set[int]] = {429, 500, 502, 503}

    def __init__(
        self, base_url: str, api_key: str = "", timeout: int = 120,
        allow_insecure: bool = False,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        # validate_base_url honoured allow_insecure and this did not, so the
        # documented escape hatch let a config load and then failed on every
        # turn instead.
        self.allow_insecure = allow_insecure
        self._reject_unsafe_scheme()
        self._reject_cleartext_bearer()

    def _reject_unsafe_scheme(self) -> None:
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in ("http", "https"):
            raise ConfigError(
                f"unsupported URL scheme {parsed.scheme!r}; "
                f"only http:// and https:// are allowed"
            )

    def _reject_cleartext_bearer(self) -> None:
        if not self.api_key or self.allow_insecure:
            return
        parsed = urlsplit(self.base_url)
        if parsed.scheme != "http":
            return
        host = parsed.hostname or ""
        if host not in _CLEARTEXT_SAFE_HOSTS:
            raise ConfigError(
                f"refusing to send Bearer token over http:// to {host}; "
                f"use https:// or a localhost address"
            )

    def _build_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        url = f"{self.base_url}/chat/completions"
        body: dict = {
            "model": request.model,
            "messages": [message_to_dict(m) for m in request.messages],
        }
        if request.tools is not None:
            body["tools"] = request.tools
        if request.tool_choice is not None:
            body["tool_choice"] = request.tool_choice
        if request.max_tokens is not None:
            body["max_tokens"] = request.max_tokens
        if request.temperature is not None:
            body["temperature"] = request.temperature

        data = json.dumps(body).encode()
        req = urllib.request.Request(
            url, data=data, headers=self._build_headers(), method="POST"
        )

        try:
            with _no_redirect_opener.open(req, timeout=self.timeout) as resp:
                raw_bytes = resp.read(_MAX_RESPONSE_BYTES + 1)
                if len(raw_bytes) > _MAX_RESPONSE_BYTES:
                    raise ProviderError(f"response exceeded {_MAX_RESPONSE_BYTES} byte limit")
                raw = json.loads(raw_bytes)
        except urllib.error.HTTPError as e:
            status = e.code
            if status in (401, 403):
                raise AuthError(f"HTTP {status}: authentication failed") from e
            if status == 429:
                retry_after = e.headers.get("retry-after") if e.headers else None
                delay = None
                if retry_after is not None:
                    try:
                        parsed = float(retry_after)
                        if not math.isfinite(parsed):
                            parsed = 0.0
                        delay = max(0.0, min(parsed, 300.0))
                    except (ValueError, OverflowError):
                        pass
                raise RetryableError(
                    f"HTTP 429: rate limited", retry_after=delay
                ) from e
            if status in (500, 502, 503):
                raise RetryableError(f"HTTP {status}: server error") from e
            detail = _error_detail(e)
            lowered = detail.lower()
            if status == 400 and ("context_length" in lowered or "context length" in lowered):
                raise ContextLengthError(detail) from e
            raise ProviderError(f"HTTP {status}: {detail}") from e
        except urllib.error.URLError as e:
            if "Connection refused" in str(e.reason):
                raise ProviderUnavailableError(
                    f"connection refused: {self.base_url}"
                ) from e
            raise RetryableError(f"connection error: {e.reason}") from e
        except TimeoutError as e:
            raise RetryableError(f"request timed out ({self.timeout}s)") from e
        except OSError as e:
            if "timed out" in str(e):
                raise RetryableError(f"request timed out ({self.timeout}s)") from e
            raise RetryableError(f"connection error: {e}") from e

        return self._parse_response(raw)

    @staticmethod
    def _coerce_content(content: object) -> str:
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, dict):
                    text = part.get("text")
                    if isinstance(text, str):
                        parts.append(text)
                elif isinstance(part, str):
                    parts.append(part)
            return "".join(parts)
        return str(content)

    def _parse_response(self, raw: dict) -> CompletionResponse:
        usage = usage_from_dict(raw.get("usage") or {})
        choices = raw.get("choices", [])
        if not choices:
            return CompletionResponse(text="", model=raw.get("model", ""), usage=usage)

        choice = choices[0]
        if not isinstance(choice, dict):
            return CompletionResponse(text="", model=raw.get("model", ""), usage=usage)
        message = choice.get("message")
        if not isinstance(message, dict):
            return CompletionResponse(text="", model=raw.get("model", ""), usage=usage)
        text = self._coerce_content(message.get("content"))

        if not text.strip() and not message.get("tool_calls"):
            # Kimi K2.6 via Ollama Cloud put its whole answer in the
            # reasoning field and left content empty; this parser read only
            # content, so the loop retried into the same shape and the cron
            # runner reported "empty response after retries" with no message
            # delivered. Reasoning text is the answer's only carrier in that
            # shape, so it is used, and the response shape is logged either
            # way so an empty completion is diagnosable from the log.
            reasoning = message.get("reasoning_content") or message.get("reasoning")
            finish_reason = choice.get("finish_reason")
            if isinstance(reasoning, str) and reasoning.strip():
                logger.warning(
                    "empty content with %d chars in the reasoning field "
                    "(finish_reason=%r); using the reasoning text",
                    len(reasoning), finish_reason,
                )
                text = reasoning
            else:
                logger.warning(
                    "empty completion: finish_reason=%r, message keys=%s",
                    finish_reason, sorted(message.keys()),
                )

        tool_calls = None
        raw_tool_calls = message.get("tool_calls")
        if raw_tool_calls:
            tool_calls = []
            for tc in raw_tool_calls:
                # Nothing here may raise. dispatch does `call.arguments |
                # {...}`, and the assistant message carrying the tool call is
                # persisted before dispatch, so a malformed call that raised
                # would leave an orphaned call that breaks every later request.
                if not isinstance(tc, dict):
                    logger.warning("dropping non-object tool call: %r", tc)
                    continue
                func = tc.get("function")
                if not isinstance(func, dict):
                    logger.warning("dropping tool call with no function object: %r", tc)
                    continue
                args_str = func.get("arguments", "{}")
                try:
                    arguments = json.loads(args_str) if isinstance(args_str, str) else args_str
                except (ValueError, TypeError):
                    arguments = None
                if not isinstance(arguments, dict):
                    logger.warning(
                        "tool call %r had non-object arguments %r, coercing to {}",
                        func.get("name", ""), args_str,
                    )
                    arguments = {}
                tool_calls.append(ToolCall(
                    id=tc.get("id", ""),
                    name=func.get("name", ""),
                    arguments=arguments,
                ))

        return CompletionResponse(
            text=text,
            model=raw.get("model", ""),
            tool_calls=tool_calls if tool_calls else None,
            usage=usage,
        )
