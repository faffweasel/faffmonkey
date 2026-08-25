"""Brave Search API provider for the SearchProvider seam."""

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request

from faffmonkey.types import SearchResult

logger = logging.getLogger(__name__)

BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
MAX_RESULTS_CAP = 10
_MAX_BRAVE_RESPONSE_BYTES = 512 * 1024
_MAX_TITLE_LEN = 256
_MAX_SNIPPET_LEN = 2048
_MAX_URL_LEN = 2048


class BraveSearchProvider:
    def __init__(self, api_key: str = "") -> None:
        self.api_key = api_key or os.environ.get("BRAVE_API_KEY", "")

    def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        if not self.api_key:
            raise RuntimeError("BRAVE_API_KEY is not set")

        count = max(1, min(max_results, MAX_RESULTS_CAP))
        params = urllib.request.quote(query, safe="")
        url = f"{BRAVE_SEARCH_URL}?q={params}&count={count}"

        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "X-Subscription-Token": self.api_key,
            },
        )

        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read(_MAX_BRAVE_RESPONSE_BYTES + 1)
                if len(raw) > _MAX_BRAVE_RESPONSE_BYTES:
                    raise RuntimeError("search response exceeded size limit")
                data = json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as e:
            logger.error("brave search HTTP %d: %s", e.code, e.reason)
            raise RuntimeError(f"Brave Search API error: HTTP {e.code}") from e
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            logger.error("brave search failed: %s", e)
            raise RuntimeError(f"Brave Search API error: {e}") from e

        if not isinstance(data, dict):
            return []
        web = data.get("web")
        if not isinstance(web, dict):
            return []
        raw_results = web.get("results")
        if not isinstance(raw_results, list):
            return []

        results: list[SearchResult] = []
        for item in raw_results:
            if not isinstance(item, dict):
                continue
            raw_url = item.get("url")
            if not raw_url or not isinstance(raw_url, str):
                continue
            try:
                scheme = urllib.parse.urlparse(raw_url).scheme.lower()
            except ValueError:
                continue
            if scheme not in ("http", "https"):
                continue
            results.append(SearchResult(
                title=str(item.get("title", ""))[:_MAX_TITLE_LEN],
                url=raw_url[:_MAX_URL_LEN],
                snippet=str(item.get("description", ""))[:_MAX_SNIPPET_LEN],
            ))
            if len(results) >= count:
                break

        return results
