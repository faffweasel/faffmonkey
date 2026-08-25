from typing import Protocol, runtime_checkable


@runtime_checkable
class Synthesiser(Protocol):
    def synthesise(self, text: str) -> tuple[bytes, str] | None: ...


class NoopSynthesiser:
    def synthesise(self, text: str) -> tuple[bytes, str] | None:
        return None
