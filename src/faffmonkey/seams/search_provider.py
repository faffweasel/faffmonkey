from typing import Protocol, runtime_checkable

from faffmonkey.types import SearchResult


@runtime_checkable
class SearchProvider(Protocol):
    def search(self, query: str, max_results: int = 5) -> list[SearchResult]: ...


class SearchNotConfigured(RuntimeError):
    pass


class NoopSearchProvider:
    def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        """Refuse rather than return nothing.

        An empty list is indistinguishable from "the web has no answer",
        so the model concluded the search had run and found nothing.
        """
        raise SearchNotConfigured(
            "web search is not configured; run: faff setup search"
        )
