"""Every seam implementation must match its Protocol, signature included.

`runtime_checkable` isinstance only asks whether the method NAMES exist, so
`isinstance(x, Channel)` passes for a class whose `send()` takes no
arguments at all. That blindness is why TelegramChannel shipped without
`poll()`: nothing compared an implementation to the Protocol.

(A bare MagicMock does NOT pass isinstance on 3.12+, which resolves
Protocol members with `getattr_static` and so ignores auto-created
attributes. `MagicMock(spec=...)` does pass, at name level only.)

This module compares parameters, not just names, for:
  - the noop and built-in implementations
  - the contrib implementations, which are what an install copies
  - the test fakes, so a test built on one is testing the real contract
"""

from __future__ import annotations

import inspect
import sys
from unittest.mock import MagicMock

import pytest

for _mod in ("telegram", "telegram.ext", "discord"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

from faffmonkey.seams.channel import Channel
from faffmonkey.seams.channel_cli import CLIChannel
from faffmonkey.seams.channel_noop import NoopChannel
from faffmonkey.seams.provider import Provider
from faffmonkey.seams.provider_openai_compat import OpenAICompatProvider
from faffmonkey.seams.search_provider import NoopSearchProvider, SearchProvider
from faffmonkey.seams.synthesiser import NoopSynthesiser, Synthesiser
from faffmonkey.seams.transcriber import NoopTranscriber, Transcriber
from faffmonkey.wiring import _import_class

from tests.fakes import (
    FakeChannel,
    FakeSearchProvider,
    FakeSynthesiser,
    FakeTranscriber,
)
from tests.faux_provider import FauxProvider

_VAR_KINDS = (
    inspect.Parameter.VAR_POSITIONAL,
    inspect.Parameter.VAR_KEYWORD,
)


def protocol_methods(protocol: type) -> list[str]:
    """The method names a Protocol requires."""
    return sorted(
        name for name in dir(protocol)
        if not name.startswith("_")
        and callable(getattr(protocol, name, None))
    )


def _params(func: object) -> list[inspect.Parameter]:
    params = list(inspect.signature(func).parameters.values())
    if params and params[0].name in ("self", "cls"):
        params = params[1:]
    return params


def signature_problems(protocol: type, impl: type) -> list[str]:
    """Every way impl fails to be substitutable for protocol."""
    problems: list[str] = []

    for name in protocol_methods(protocol):
        impl_attr = getattr(impl, name, None)
        if impl_attr is None or not callable(impl_attr):
            problems.append(f"{name}: missing")
            continue

        want = _params(getattr(protocol, name))
        got = _params(impl_attr)

        # *args/**kwargs accept anything, so there is nothing to compare.
        if any(p.kind in _VAR_KINDS for p in got):
            continue

        want_names = [p.name for p in want]
        got_names = [p.name for p in got]
        if got_names[:len(want_names)] != want_names:
            problems.append(
                f"{name}: expected parameters {want_names}, got {got_names}"
            )
            continue

        for extra in got[len(want):]:
            if extra.default is inspect.Parameter.empty:
                problems.append(
                    f"{name}: extra required parameter {extra.name!r}"
                )

        # A Protocol default is part of the contract: callers omit it.
        for want_p, got_p in zip(want, got):
            if (
                want_p.default is not inspect.Parameter.empty
                and got_p.default is inspect.Parameter.empty
            ):
                problems.append(
                    f"{name}: {got_p.name!r} must keep its default"
                )

    return problems


# (protocol, implementation, label)
CASES: list[tuple[type, type, str]] = [
    (Channel, CLIChannel, "CLIChannel"),
    (Channel, NoopChannel, "NoopChannel"),
    (Channel, FakeChannel, "FakeChannel (test fake)"),
    (Provider, OpenAICompatProvider, "OpenAICompatProvider"),
    (Provider, FauxProvider, "FauxProvider (test fake)"),
    (Transcriber, NoopTranscriber, "NoopTranscriber"),
    (Transcriber, FakeTranscriber, "FakeTranscriber (test fake)"),
    (Synthesiser, NoopSynthesiser, "NoopSynthesiser"),
    (Synthesiser, FakeSynthesiser, "FakeSynthesiser (test fake)"),
    (SearchProvider, NoopSearchProvider, "NoopSearchProvider"),
    (SearchProvider, FakeSearchProvider, "FakeSearchProvider (test fake)"),
]

CONTRIB_CASES: list[tuple[type, str, str]] = [
    (Channel, "contrib.channel_telegram.TelegramChannel", "TelegramChannel"),
    (Channel, "contrib.channel_discord.DiscordChannel", "DiscordChannel"),
    (Transcriber, "contrib.transcriber_openai.OpenAITranscriber", "OpenAITranscriber"),
    (Synthesiser, "contrib.synthesiser_openai.OpenAISynthesiser", "OpenAISynthesiser"),
    (SearchProvider, "contrib.search_provider_brave.BraveSearchProvider", "BraveSearchProvider"),
]


@pytest.mark.parametrize(
    "protocol,impl,label", CASES, ids=[c[2] for c in CASES],
)
def test_implementation_matches_its_protocol(protocol, impl, label):
    problems = signature_problems(protocol, impl)
    assert not problems, f"{label} does not satisfy {protocol.__name__}: {problems}"


@pytest.mark.parametrize(
    "protocol,path,label", CONTRIB_CASES, ids=[c[2] for c in CONTRIB_CASES],
)
def test_contrib_implementation_matches_its_protocol(protocol, path, label):
    """contrib/ is the source an extension install copies into extensions/."""
    impl = _import_class(path)
    problems = signature_problems(protocol, impl)
    assert not problems, f"{label} does not satisfy {protocol.__name__}: {problems}"


class TestTheCheckActuallyCatchesThings:
    """Guards the guard. Each of these passes isinstance and must not pass here."""

    def test_missing_method_is_caught(self):
        class NoPoll:
            def start(self) -> None: ...
            def stop(self) -> None: ...
            def receive(self): ...
            def send(self, message) -> None: ...
            def is_allowed(self, sender_id: str) -> bool: ...
            def is_closed(self) -> bool: ...

        assert any("poll" in p for p in signature_problems(Channel, NoPoll))

    def test_wrong_arity_is_caught(self):
        class SendTakesNothing:
            def start(self) -> None: ...
            def stop(self) -> None: ...
            def receive(self): ...
            def send(self) -> None: ...
            def is_allowed(self, sender_id: str) -> bool: ...
            def poll(self): ...
            def is_closed(self) -> bool: ...

        assert isinstance(SendTakesNothing(), Channel), (
            "precondition: runtime_checkable is blind to this"
        )
        assert any("send" in p for p in signature_problems(Channel, SendTakesNothing))

    def test_dropped_default_is_caught(self):
        class SearchNeedsMaxResults:
            def search(self, query: str, max_results: int) -> list: ...

        assert isinstance(SearchNeedsMaxResults(), SearchProvider)
        problems = signature_problems(SearchProvider, SearchNeedsMaxResults)
        assert any("default" in p for p in problems)

    def test_a_specced_mock_satisfies_isinstance_but_not_this_check(self):
        """The reason these fakes exist rather than more MagicMocks.

        A bare mock fails isinstance on 3.12+, but spec= passes it, and
        spec= only copies names.
        """
        assert not isinstance(MagicMock(), Channel)
        assert isinstance(MagicMock(spec=Channel), Channel)
        assert signature_problems(Channel, MagicMock)
