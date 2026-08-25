"""The FauxProvider fake's own contract.

Compaction and seam-conformance tests are built on this fake, so a fault in
it reads as a fault in the product. Its shape against the Provider Protocol
is checked in test_seam_conformance.py; what is checked here is the
behaviour those tests rely on: responses come back in the order they were
scripted, and running out is loud.
"""

from __future__ import annotations

import pytest

from faffmonkey.types import CompletionRequest

from tests.faux_provider import FauxProvider, faux_response


class TestFauxProvider:
    def test_returns_responses_in_order(self):
        fp = FauxProvider([
            faux_response(text="first"),
            faux_response(text="second"),
        ])
        req = CompletionRequest(messages=[], model="faux")
        assert fp.complete(req).text == "first"
        assert fp.complete(req).text == "second"

    def test_exhausted_raises_stop_iteration(self):
        fp = FauxProvider([faux_response(text="only")])
        req = CompletionRequest(messages=[], model="faux")
        fp.complete(req)
        with pytest.raises(StopIteration):
            fp.complete(req)


class TestFauxResponse:
    def test_text_only(self):
        r = faux_response(text="hello")
        assert r.text == "hello"
        assert r.model == "faux"
        assert r.tool_calls is None

    def test_with_tool_calls(self):
        r = faux_response(tool_calls=[
            {"name": "file_write", "arguments": {"path": "test.txt", "content": "x"}},
        ])
        assert r.tool_calls is not None
        assert len(r.tool_calls) == 1
        assert r.tool_calls[0].name == "file_write"

    def test_tool_call_ids_auto_generated(self):
        r = faux_response(tool_calls=[
            {"name": "file_read", "arguments": {"path": "a.txt"}},
            {"name": "file_read", "arguments": {"path": "b.txt"}},
        ])
        ids = [tc.id for tc in r.tool_calls]
        assert ids == ["call_0", "call_1"]

    def test_tool_call_custom_id(self):
        r = faux_response(tool_calls=[
            {"id": "my_id", "name": "file_read", "arguments": {}},
        ])
        assert r.tool_calls[0].id == "my_id"
