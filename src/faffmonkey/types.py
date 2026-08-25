import base64
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_VALID_ROLES = frozenset({"system", "user", "assistant", "tool"})


class RetryableError(Exception):
    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class AuthError(Exception):
    pass


class ProviderUnavailableError(Exception):
    pass


class ContextLengthError(Exception):
    pass


class ProviderError(Exception):
    pass


@dataclass
class InboundMessage:
    sender_id: str
    text: str
    channel_id: str
    timestamp: datetime
    # Inbox paths, not bytes. sessions.db records where the image landed
    # so history stays small and the file remains addressable by tools.
    images: list[Path] = field(default_factory=list)
    audio: bytes | None = None
    audio_mime: str | None = None
    attachments: list[Path] = field(default_factory=list)
    # Set by a channel for a message from a group or guild channel, naming
    # that room. Those conversations are kept apart from the main session,
    # because a reply there is read by everyone in the room.
    group_id: str | None = None


@dataclass
class OutboundMessage:
    text: str
    attachments: list[Path] = field(default_factory=list)
    audio: bytes | None = None
    audio_mime: str | None = None
    # The room this message answers, copied from the inbound message. A
    # channel sends to it when set and to the owner's direct chat when
    # not, so a cron announcement never lands in a group.
    group_id: str | None = None


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
        )


@dataclass
class Message:
    role: str
    content: str = ""
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None
    name: str | None = None
    timestamp: str | None = None
    images: list[str] = field(default_factory=list)


@dataclass
class CompletionRequest:
    messages: list[Message]
    model: str
    tools: list[dict] | None = None
    tool_choice: str | dict | None = None
    max_tokens: int | None = None
    temperature: float | None = None


@dataclass
class CompletionResponse:
    text: str
    model: str
    tool_calls: list[ToolCall] | None = None
    usage: TokenUsage = field(default_factory=TokenUsage)


@dataclass
class ToolResult:
    id: str
    content: str
    is_error: bool = False
    # Files a skill produced, via its MEDIA: lines. Both ends of this
    # existed for months and the middle did not, so a generated image was
    # named in the tool result and never sent.
    attachments: list[Path] = field(default_factory=list)


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str


MAX_IMAGE_BYTES = 5 * 1024 * 1024

_IMAGE_MIME = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp",
}


def image_data_uri(path: str) -> str | None:
    """Read an image into a base64 data URI, or None if it cannot be sent.

    Images live on disk and are read at request time, so history holds a
    path rather than megabytes of base64 per photo.
    """
    p = Path(path)
    mime = _IMAGE_MIME.get(p.suffix.lower())
    if mime is None:
        logger.warning("not a supported image type, skipping: %s", path)
        return None
    try:
        if p.stat().st_size > MAX_IMAGE_BYTES:
            logger.warning("image too large to send (%d bytes): %s", p.stat().st_size, path)
            return None
        data = p.read_bytes()
    except OSError as e:
        logger.warning("could not read image %s: %s", path, e)
        return None
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


def message_to_dict(msg: Message) -> dict:
    d: dict = {"role": msg.role}
    if msg.images:
        parts: list[dict] = []
        if msg.content:
            parts.append({"type": "text", "text": msg.content})
        for path in msg.images:
            uri = image_data_uri(path)
            if uri is None:
                parts.append({"type": "text", "text": f"[image unavailable: {path}]"})
                continue
            parts.append({"type": "image_url", "image_url": {"url": uri}})
        d["content"] = parts
    elif msg.content:
        d["content"] = msg.content
    if msg.tool_calls is not None:
        tc_list = []
        for tc in msg.tool_calls:
            try:
                args_json = json.dumps(tc.arguments)
            except (TypeError, ValueError) as e:
                logger.warning("failed to serialize tool_call arguments for %s: %s", tc.name, e)
                args_json = "{}"
            tc_list.append({
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.name,
                    "arguments": args_json,
                },
            })
        d["tool_calls"] = tc_list
    if msg.tool_call_id is not None:
        d["tool_call_id"] = msg.tool_call_id
    if msg.name is not None:
        d["name"] = msg.name
    return d


def dict_to_message(d: dict) -> Message | None:
    tool_calls = None
    raw_tc = d.get("tool_calls")
    if isinstance(raw_tc, list):
        tool_calls = []
        for tc in raw_tc:
            if not isinstance(tc, dict):
                continue
            func = tc.get("function")
            if isinstance(func, dict):
                args_str = func.get("arguments", "{}")
                try:
                    if isinstance(args_str, str) and len(args_str) > 256_000:
                        logger.warning("tool_call %s: arguments too large (%d bytes), skipping", tc.get("id", "?"), len(args_str))
                        continue
                    elif isinstance(args_str, str):
                        arguments = json.loads(args_str)
                    else:
                        arguments = args_str
                    if not isinstance(arguments, dict):
                        logger.warning("tool_call %s: arguments is %s, expected dict; coercing to {}", tc.get("id", "?"), type(arguments).__name__)
                        arguments = {}
                except (ValueError, TypeError, RecursionError):
                    arguments = {}
                tool_calls.append(ToolCall(
                    id=tc.get("id", ""),
                    name=func.get("name", ""),
                    arguments=arguments,
                ))
            else:
                raw_args = tc.get("arguments", {})
                if not isinstance(raw_args, dict):
                    logger.warning("tool_call %s: arguments is %s, expected dict; coercing to {}", tc.get("id", "?"), type(raw_args).__name__)
                    raw_args = {}
                tool_calls.append(ToolCall(
                    id=tc.get("id", ""),
                    name=tc.get("name", ""),
                    arguments=raw_args,
                ))
    role = d.get("role", "")
    if not role:
        logger.warning("skipping malformed message dict: missing 'role'")
        return None
    if role not in _VALID_ROLES:
        logger.warning("skipping message with unrecognised role: %s", role)
        return None
    raw_images = d.get("images")
    images = [str(i) for i in raw_images] if isinstance(raw_images, list) else []
    return Message(
        role=role,
        content=d.get("content") or "",
        tool_calls=tool_calls,
        tool_call_id=d.get("tool_call_id"),
        name=d.get("name"),
        images=images,
    )


def usage_from_dict(d: object) -> TokenUsage:
    if not isinstance(d, dict):
        logger.debug("usage_from_dict received non-dict: %s", type(d).__name__)
        return TokenUsage()
    try:
        return TokenUsage(
            prompt_tokens=int(d.get("prompt_tokens", 0)),
            completion_tokens=int(d.get("completion_tokens", 0)),
            total_tokens=int(d.get("total_tokens", 0)),
        )
    except (ValueError, TypeError):
        return TokenUsage()
