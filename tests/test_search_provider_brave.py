import json
from unittest.mock import MagicMock, patch

import pytest

from contrib.search_provider_brave import (
    BraveSearchProvider,
    _MAX_BRAVE_RESPONSE_BYTES,
    _MAX_TITLE_LEN,
    _MAX_SNIPPET_LEN,
)
from faffmonkey.types import SearchResult


class TestBraveSearchProvider:
    def test_requires_api_key(self):
        provider = BraveSearchProvider(api_key="")
        with pytest.raises(RuntimeError, match="BRAVE_API_KEY"):
            provider.search("test query")

    def test_reads_api_key_from_env(self, monkeypatch):
        monkeypatch.setenv("BRAVE_API_KEY", "test-key")
        provider = BraveSearchProvider()
        assert provider.api_key == "test-key"

    def test_constructor_api_key_takes_precedence(self, monkeypatch):
        monkeypatch.setenv("BRAVE_API_KEY", "env-key")
        provider = BraveSearchProvider(api_key="explicit-key")
        assert provider.api_key == "explicit-key"

    def _mock_response(self, results_data: list[dict]) -> MagicMock:
        body = json.dumps({"web": {"results": results_data}}).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = body
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        return mock_resp

    @patch("contrib.search_provider_brave.urllib.request.urlopen")
    def test_search_returns_results(self, mock_urlopen):
        mock_urlopen.return_value = self._mock_response([
            {"title": "Result 1", "url": "https://example.com/1", "description": "First result"},
            {"title": "Result 2", "url": "https://example.com/2", "description": "Second result"},
        ])

        provider = BraveSearchProvider(api_key="test-key")
        results = provider.search("test query", max_results=5)

        assert len(results) == 2
        assert isinstance(results[0], SearchResult)
        assert results[0].title == "Result 1"
        assert results[0].url == "https://example.com/1"
        assert results[0].snippet == "First result"
        assert results[1].title == "Result 2"

    @patch("contrib.search_provider_brave.urllib.request.urlopen")
    def test_search_respects_max_results(self, mock_urlopen):
        many_results = [
            {"title": f"R{i}", "url": f"https://example.com/{i}", "description": f"Desc {i}"}
            for i in range(10)
        ]
        mock_urlopen.return_value = self._mock_response(many_results)

        provider = BraveSearchProvider(api_key="test-key")
        results = provider.search("test", max_results=3)

        assert len(results) == 3

    @patch("contrib.search_provider_brave.urllib.request.urlopen")
    def test_search_caps_at_10(self, mock_urlopen):
        mock_urlopen.return_value = self._mock_response([])
        provider = BraveSearchProvider(api_key="test-key")
        provider.search("test", max_results=50)

        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        assert "count=10" in req.full_url

    @patch("contrib.search_provider_brave.urllib.request.urlopen")
    def test_search_empty_results(self, mock_urlopen):
        mock_urlopen.return_value = self._mock_response([])
        provider = BraveSearchProvider(api_key="test-key")
        results = provider.search("obscure query")
        assert results == []

    @patch("contrib.search_provider_brave.urllib.request.urlopen")
    def test_search_sends_correct_headers(self, mock_urlopen):
        mock_urlopen.return_value = self._mock_response([])
        provider = BraveSearchProvider(api_key="my-brave-key")
        provider.search("hello world")

        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        assert req.get_header("X-subscription-token") == "my-brave-key"
        assert req.get_header("Accept") == "application/json"

    @patch("contrib.search_provider_brave.urllib.request.urlopen")
    def test_search_handles_missing_fields(self, mock_urlopen):
        mock_urlopen.return_value = self._mock_response([
            {"url": "https://example.com/1"},
        ])
        provider = BraveSearchProvider(api_key="test-key")
        results = provider.search("test")
        assert len(results) == 1
        assert results[0].title == ""
        assert results[0].snippet == ""

    @patch("contrib.search_provider_brave.urllib.request.urlopen")
    def test_non_string_title_coerced(self, mock_urlopen):
        mock_urlopen.return_value = self._mock_response([
            {"title": ["nested", "list"], "url": "https://example.com/1", "description": {"key": "val"}},
        ])
        provider = BraveSearchProvider(api_key="test-key")
        results = provider.search("test")
        assert len(results) == 1
        assert isinstance(results[0].title, str)
        assert isinstance(results[0].snippet, str)

    @patch("contrib.search_provider_brave.urllib.request.urlopen")
    def test_missing_url_skipped(self, mock_urlopen):
        mock_urlopen.return_value = self._mock_response([
            {"title": "No URL", "description": "Missing"},
            {"title": "Has URL", "url": "https://example.com/2", "description": "Good"},
        ])
        provider = BraveSearchProvider(api_key="test-key")
        results = provider.search("test")
        assert len(results) == 1
        assert results[0].url == "https://example.com/2"

    @patch("contrib.search_provider_brave.urllib.request.urlopen")
    def test_none_url_skipped(self, mock_urlopen):
        mock_urlopen.return_value = self._mock_response([
            {"title": "Null URL", "url": None, "description": "Bad"},
            {"title": "OK", "url": "https://example.com/3", "description": "Fine"},
        ])
        provider = BraveSearchProvider(api_key="test-key")
        results = provider.search("test")
        assert len(results) == 1
        assert results[0].url == "https://example.com/3"

    @patch("contrib.search_provider_brave.urllib.request.urlopen")
    def test_search_handles_no_web_key(self, mock_urlopen):
        body = json.dumps({"query": {"original": "test"}}).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = body
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        provider = BraveSearchProvider(api_key="test-key")
        results = provider.search("test")
        assert results == []

    @patch("contrib.search_provider_brave.urllib.request.urlopen")
    def test_http_error_raises_runtime_error(self, mock_urlopen):
        import urllib.error
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="", code=401, msg="Unauthorized", hdrs=None, fp=None,
        )
        provider = BraveSearchProvider(api_key="bad-key")
        with pytest.raises(RuntimeError, match="HTTP 401"):
            provider.search("test")

    @patch("contrib.search_provider_brave.urllib.request.urlopen")
    def test_timeout_raises_runtime_error(self, mock_urlopen):
        mock_urlopen.side_effect = TimeoutError("timed out")
        provider = BraveSearchProvider(api_key="test-key")
        with pytest.raises(RuntimeError, match="timed out"):
            provider.search("test")

    @patch("contrib.search_provider_brave.urllib.request.urlopen")
    def test_javascript_url_filtered(self, mock_urlopen):
        mock_urlopen.return_value = self._mock_response([
            {"title": "Evil", "url": "javascript:alert(1)", "description": "XSS"},
            {"title": "Good", "url": "https://example.com/safe", "description": "Safe"},
        ])
        provider = BraveSearchProvider(api_key="test-key")
        results = provider.search("test")
        assert len(results) == 1
        assert results[0].url == "https://example.com/safe"

    @patch("contrib.search_provider_brave.urllib.request.urlopen")
    def test_data_url_filtered(self, mock_urlopen):
        mock_urlopen.return_value = self._mock_response([
            {"title": "Data", "url": "data:text/html,<h1>hi</h1>", "description": "Data URI"},
        ])
        provider = BraveSearchProvider(api_key="test-key")
        results = provider.search("test")
        assert results == []

    @patch("contrib.search_provider_brave.urllib.request.urlopen")
    def test_ftp_url_filtered(self, mock_urlopen):
        mock_urlopen.return_value = self._mock_response([
            {"title": "FTP", "url": "ftp://files.example.com/doc.pdf", "description": "FTP"},
            {"title": "OK", "url": "http://example.com/page", "description": "HTTP"},
        ])
        provider = BraveSearchProvider(api_key="test-key")
        results = provider.search("test")
        assert len(results) == 1
        assert results[0].url == "http://example.com/page"

    @patch("contrib.search_provider_brave.urllib.request.urlopen")
    def test_oversized_response_raises(self, mock_urlopen):
        body = b"x" * (_MAX_BRAVE_RESPONSE_BYTES + 1)
        mock_resp = MagicMock()
        mock_resp.read.return_value = body
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        provider = BraveSearchProvider(api_key="test-key")
        with pytest.raises(RuntimeError, match="size limit"):
            provider.search("test")

    @patch("contrib.search_provider_brave.urllib.request.urlopen")
    def test_title_and_snippet_truncated(self, mock_urlopen):
        long_title = "T" * (_MAX_TITLE_LEN + 500)
        long_snippet = "S" * (_MAX_SNIPPET_LEN + 500)
        mock_urlopen.return_value = self._mock_response([
            {"title": long_title, "url": "https://example.com/1", "description": long_snippet},
        ])
        provider = BraveSearchProvider(api_key="test-key")
        results = provider.search("test")
        assert len(results) == 1
        assert len(results[0].title) == _MAX_TITLE_LEN
        assert len(results[0].snippet) == _MAX_SNIPPET_LEN

    @patch("contrib.search_provider_brave.urllib.request.urlopen")
    def test_malformed_web_is_list_returns_empty(self, mock_urlopen):
        body = json.dumps({"web": ["not", "a", "dict"]}).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = body
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        provider = BraveSearchProvider(api_key="test-key")
        assert provider.search("test") == []

    @patch("contrib.search_provider_brave.urllib.request.urlopen")
    def test_malformed_results_is_string_returns_empty(self, mock_urlopen):
        body = json.dumps({"web": {"results": "not-a-list"}}).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = body
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        provider = BraveSearchProvider(api_key="test-key")
        assert provider.search("test") == []

    @patch("contrib.search_provider_brave.urllib.request.urlopen")
    def test_max_results_zero_clamped_to_one(self, mock_urlopen):
        mock_urlopen.return_value = self._mock_response([])
        provider = BraveSearchProvider(api_key="test-key")
        provider.search("test", max_results=0)
        req = mock_urlopen.call_args[0][0]
        assert "count=1" in req.full_url

    @patch("contrib.search_provider_brave.urllib.request.urlopen")
    def test_top_level_json_array_returns_empty(self, mock_urlopen):
        body = json.dumps([{"url": "https://example.com"}]).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = body
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        provider = BraveSearchProvider(api_key="test-key")
        assert provider.search("test") == []

    @patch("contrib.search_provider_brave.urllib.request.urlopen")
    def test_top_level_json_scalar_returns_empty(self, mock_urlopen):
        body = json.dumps("just a string").encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = body
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        provider = BraveSearchProvider(api_key="test-key")
        assert provider.search("test") == []

    @patch("contrib.search_provider_brave.urllib.request.urlopen")
    def test_max_results_negative_clamped_to_one(self, mock_urlopen):
        mock_urlopen.return_value = self._mock_response([])
        provider = BraveSearchProvider(api_key="test-key")
        provider.search("test", max_results=-5)
        req = mock_urlopen.call_args[0][0]
        assert "count=1" in req.full_url
