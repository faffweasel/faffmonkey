"""A real HTTP server speaking the OpenAI chat-completions API.

The e2e suite fakes exactly one thing: the model. Everything else is the
real code path, so a request only reaches here if config parsing, wiring,
model routing and the provider seam all worked.

Responses are scripted in order. Each request is recorded so a test can
assert what was actually sent, which is how "did the briefing reach the
history" style questions get answered.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any


class Script:
    """An ordered list of responses, plus the requests that consumed them."""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        self._index = 0
        self._lock = threading.Lock()

    def next_response(self, request_body: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self.requests.append(request_body)
            if self._index >= len(self.responses):
                # Better a loud, well-formed refusal than a hang: the run
                # made more calls than the test scripted.
                return message("[script exhausted]")
            response = self.responses[self._index]
            self._index += 1
            return response

    @property
    def remaining(self) -> int:
        return len(self.responses) - self._index

    @property
    def call_count(self) -> int:
        return self._index

    def assert_exhausted(self) -> None:
        if self.remaining:
            raise AssertionError(
                f"{self.remaining} of {len(self.responses)} scripted responses "
                f"were never consumed (only {self._index} calls made)"
            )

    def sent_texts(self) -> list[str]:
        """Every message content the runtime has sent, flattened."""
        out: list[str] = []
        for req in self.requests:
            for msg in req.get("messages", []):
                content = msg.get("content")
                if isinstance(content, str):
                    out.append(content)
                elif isinstance(content, list):
                    for part in content:
                        if part.get("type") == "text":
                            out.append(part.get("text", ""))
        return out


def message(text: str) -> dict[str, Any]:
    return {"role": "assistant", "content": text}


def tool_call(name: str, arguments: dict[str, Any], call_id: str = "call_1") -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [{
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)},
        }],
    }


class _Handler(BaseHTTPRequestHandler):
    script: Script

    def do_POST(self) -> None:  # noqa: N802 (http.server API)
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            body = {"_unparseable": raw.decode("utf-8", "replace")}

        assistant = self.script.next_response(body)
        payload = {
            "id": "chatcmpl-e2e",
            "object": "chat.completion",
            "model": body.get("model", "e2e-model"),
            "choices": [{"index": 0, "message": assistant, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
        encoded = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:  # noqa: N802
        """The preflight probe hits /models."""
        encoded = json.dumps({"data": [{"id": "e2e-model"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *args: object) -> None:
        pass


class ScriptedProvider:
    """Context manager yielding a live server and its Script."""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.script = Script(responses)
        handler = type("BoundHandler", (_Handler,), {"script": self.script})
        self._server = HTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://127.0.0.1:{port}/v1"

    def __enter__(self) -> "ScriptedProvider":
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
