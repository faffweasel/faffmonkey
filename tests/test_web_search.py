from pathlib import Path


from faffmonkey.runtime.tools import ToolRegistry
from faffmonkey.seams.search_provider import NoopSearchProvider, SearchProvider
from faffmonkey.types import SearchResult, ToolCall


class FakeSearchProvider:
    def __init__(self, results: list[SearchResult] | None = None, error: str = "") -> None:
        self._results = results or []
        self._error = error
        self.last_query: str = ""
        self.last_max_results: int = 0

    def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        self.last_query = query
        self.last_max_results = max_results
        if self._error:
            raise RuntimeError(self._error)
        return self._results


def _make_registry(ws: Path, search_provider: SearchProvider | None = None) -> ToolRegistry:
    return ToolRegistry(
        workspace=ws,
        permissions={"web_search": "always"},
        shell_preapproved=[],
        search_provider=search_provider,
    )


class TestWebSearchWithProvider:
    def test_no_provider_returns_error(self, ws):
        registry = _make_registry(ws, search_provider=None)
        call = ToolCall(id="tc1", name="web_search", arguments={"query": "test"})
        result = registry.dispatch(call)
        assert result.is_error
        assert "No search provider is configured" in result.content

    def test_noop_provider_reports_it_is_not_configured(self, ws):
        # An empty list read as "the web has no answer", so the model
        # concluded the search had run and found nothing.
        registry = _make_registry(ws, search_provider=NoopSearchProvider())
        call = ToolCall(id="tc1", name="web_search", arguments={"query": "test"})
        result = registry.dispatch(call)
        assert result.is_error
        assert "not configured" in result.content

    def test_provider_returns_formatted_results(self, ws):
        provider = FakeSearchProvider(results=[
            SearchResult(title="Python Docs", url="https://docs.python.org", snippet="Official docs"),
            SearchResult(title="PyPI", url="https://pypi.org", snippet="Package index"),
        ])
        registry = _make_registry(ws, search_provider=provider)
        call = ToolCall(id="tc1", name="web_search", arguments={"query": "python"})
        result = registry.dispatch(call)
        assert not result.is_error
        assert "Python Docs" in result.content
        assert "https://docs.python.org" in result.content
        assert "Official docs" in result.content
        assert "PyPI" in result.content
        assert "https://pypi.org" in result.content

    def test_provider_passes_query_and_max_results(self, ws):
        provider = FakeSearchProvider(results=[])
        registry = _make_registry(ws, search_provider=provider)
        call = ToolCall(id="tc1", name="web_search", arguments={
            "query": "faffmonkey agent",
            "max_results": 3,
        })
        registry.dispatch(call)
        assert provider.last_query == "faffmonkey agent"
        assert provider.last_max_results == 3

    def test_provider_default_max_results(self, ws):
        provider = FakeSearchProvider(results=[])
        registry = _make_registry(ws, search_provider=provider)
        call = ToolCall(id="tc1", name="web_search", arguments={"query": "test"})
        registry.dispatch(call)
        assert provider.last_max_results == 5

    def test_provider_error_returns_error_result(self, ws):
        provider = FakeSearchProvider(error="API rate limited")
        registry = _make_registry(ws, search_provider=provider)
        call = ToolCall(id="tc1", name="web_search", arguments={"query": "test"})
        result = registry.dispatch(call)
        assert result.is_error
        assert "API rate limited" in result.content

    def test_missing_query_returns_error(self, ws):
        provider = FakeSearchProvider()
        registry = _make_registry(ws, search_provider=provider)
        call = ToolCall(id="tc1", name="web_search", arguments={})
        result = registry.dispatch(call)
        assert result.is_error
        assert "query" in result.content

    def test_empty_query_returns_error(self, ws):
        provider = FakeSearchProvider()
        registry = _make_registry(ws, search_provider=provider)
        call = ToolCall(id="tc1", name="web_search", arguments={"query": ""})
        result = registry.dispatch(call)
        assert result.is_error
        assert "query" in result.content

    def test_invalid_max_results_uses_default(self, ws):
        provider = FakeSearchProvider(results=[])
        registry = _make_registry(ws, search_provider=provider)
        call = ToolCall(id="tc1", name="web_search", arguments={
            "query": "test",
            "max_results": "not a number",
        })
        registry.dispatch(call)
        assert provider.last_max_results == 5

    def test_single_result_formatted(self, ws):
        provider = FakeSearchProvider(results=[
            SearchResult(title="Only Result", url="https://only.com", snippet="The one"),
        ])
        registry = _make_registry(ws, search_provider=provider)
        call = ToolCall(id="tc1", name="web_search", arguments={"query": "unique"})
        result = registry.dispatch(call)
        assert not result.is_error
        assert "**Only Result**" in result.content
        assert "https://only.com" in result.content
        assert "The one" in result.content
