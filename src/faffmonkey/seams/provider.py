from typing import Protocol, runtime_checkable

from faffmonkey.types import CompletionRequest, CompletionResponse


@runtime_checkable
class Provider(Protocol):
    def complete(self, request: CompletionRequest) -> CompletionResponse: ...
