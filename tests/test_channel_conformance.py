"""Every shipped channel must satisfy the Channel Protocol.

`faff run` validates each configured channel through
`wiring._validate_protocol` before starting it, so a channel missing a
Protocol method is a startup crash, not a latent gap. TelegramChannel
shipped without `poll()` from 14 May to 7 August because nothing asserted
this: the per-channel tests construct their class directly and never check
it against the Protocol, and `_build_channels` has no test at all.
"""

import sys
from unittest.mock import MagicMock, patch

import pytest

for _mod in ("telegram", "telegram.ext", "discord"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

from faffmonkey.cli.__main__ import BUILTIN_CHANNELS, CONTRIB_CHANNEL_SOURCES
from faffmonkey.seams.channel import Channel
from faffmonkey.wiring import WiringError, _import_class, _validate_protocol

_ENV = {
    "TELEGRAM_BOT_TOKEN": "test-token",
    "DISCORD_BOT_TOKEN": "test-token",
}


@pytest.mark.parametrize("name,path", sorted(CONTRIB_CHANNEL_SOURCES.items()))
class TestContribChannelsSatisfyProtocol:
    """contrib/ is the source an extension install copies into extensions/."""

    def test_instance_passes_the_wiring_gate(self, name, path):
        cls = _import_class(path)
        with patch.dict("os.environ", _ENV):
            instance = cls(allowed_users=["1"], workspace=None)
        # The exact call _build_channels makes before starting a channel.
        _validate_protocol(instance, Channel, f"channel({name})")
        assert isinstance(instance, Channel)


class TestChannelMapsAgree:
    def test_builtin_and_contrib_cover_the_same_channels(self):
        assert set(BUILTIN_CHANNELS) == set(CONTRIB_CHANNEL_SOURCES)

    def test_doctor_uses_the_shared_map(self):
        """doctor kept a private copy that had drifted to telegram only."""
        import inspect

        from faffmonkey.cli import doctor

        source = inspect.getsource(doctor)
        assert "BUILTIN_CHANNELS" in source
        assert "extensions.channel_telegram" not in source


class TestGateActuallyRejects:
    def test_missing_method_is_caught(self):
        """Guards the guard: prove _validate_protocol fails on a gap."""
        class HalfChannel:
            def start(self) -> None: ...
            def stop(self) -> None: ...
            def receive(self): ...
            def send(self, message) -> None: ...
            def is_allowed(self, sender_id: str) -> bool: ...
            # no poll, no is_closed

        with pytest.raises(WiringError, match="missing methods"):
            _validate_protocol(HalfChannel(), Channel, "channel(half)")


@pytest.mark.parametrize("name,path", sorted(CONTRIB_CHANNEL_SOURCES.items()))
class TestContribChannelsCarryInboundImages:
    """D6b/D6c: a photo has to reach the loop as an inbox path.

    Every shipped channel saved images to the inbox and then built an
    InboundMessage that did not mention them, so vision had no input.
    """

    def test_channel_has_an_inbox(self, name, path, tmp_path):
        cls = _import_class(path)
        with patch.dict("os.environ", _ENV):
            instance = cls(allowed_users=["1"], workspace=tmp_path)
        assert instance._inbox == tmp_path / "shared" / "inbox"
