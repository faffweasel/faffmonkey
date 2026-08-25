from __future__ import annotations

from faffmonkey.types import CompletionRequest, CompletionResponse, ToolCall


class FauxProvider:
    def __init__(self, responses: list[CompletionResponse]) -> None:
        self._responses = list(responses)
        self._index = 0
        self.calls: list[CompletionRequest] = []

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.calls.append(request)
        if self._index >= len(self._responses):
            raise StopIteration
        response = self._responses[self._index]
        self._index += 1
        return response

    @property
    def remaining(self) -> int:
        return len(self._responses) - self._index

    def assert_exhausted(self) -> None:
        """Fail when the code under test made fewer provider calls than scripted.

        A test that queues three responses and consumes one is not testing
        what its name says: the control flow it assumes did not happen. The
        fake stayed silent about that, which is what allowed integration
        tests to pass with the feature under test removed entirely.
        """
        if self.remaining:
            raise AssertionError(
                f"{self.remaining} of {len(self._responses)} scripted responses "
                f"were never consumed (only {self._index} provider calls made)"
            )


def faux_response(
    text: str = "",
    tool_calls: list[dict] | None = None,
) -> CompletionResponse:
    formatted: list[ToolCall] | None = None
    if tool_calls is not None:
        formatted = [
            ToolCall(
                id=tc.get("id", f"call_{i}"),
                name=tc["name"],
                arguments=tc.get("arguments", {}),
            )
            for i, tc in enumerate(tool_calls)
        ]
    return CompletionResponse(
        text=text,
        model="faux",
        tool_calls=formatted,
    )
