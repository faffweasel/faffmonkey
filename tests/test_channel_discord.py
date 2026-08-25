"""Tests for contrib/channel_discord.py."""

import sys
from unittest.mock import MagicMock

if "discord" not in sys.modules:
    _mock_discord = MagicMock()
    _mock_discord.DMChannel = type("DMChannel", (), {})
    sys.modules["discord"] = _mock_discord

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from faffmonkey.types import InboundMessage, OutboundMessage
from contrib.channel_discord import DiscordChannel, _split_message, DISCORD_MAX_LENGTH


def _make_channel(**kwargs):
    with patch.dict("os.environ", {"DISCORD_BOT_TOKEN": "test-token"}):
        return DiscordChannel(**kwargs)


class TestIsAllowed:
    def test_allowed_user_accepted(self):
        ch = _make_channel(allowed_users=["123", "456"])
        assert ch.is_allowed("123") is True
        assert ch.is_allowed("456") is True

    def test_unknown_user_rejected(self):
        ch = _make_channel(allowed_users=["123"])
        assert ch.is_allowed("789") is False

    def test_empty_list_rejects_all(self):
        ch = _make_channel(allowed_users=[])
        assert ch.is_allowed("123") is False

    def test_none_list_rejects_all(self):
        ch = _make_channel()
        assert ch.is_allowed("123") is False


class TestGroupPolicyMention:
    def test_responds_when_mentioned(self):
        ch = _make_channel(allowed_users=["123"], group_policy="mention")
        assert ch._should_respond(is_dm=False, is_mentioned=True, sender_id="123") is True

    def test_ignores_without_mention(self):
        ch = _make_channel(allowed_users=["123"], group_policy="mention")
        assert ch._should_respond(is_dm=False, is_mentioned=False, sender_id="123") is False

    def test_rejects_disallowed_user_even_when_mentioned(self):
        ch = _make_channel(allowed_users=["123"], group_policy="mention")
        assert ch._should_respond(is_dm=False, is_mentioned=True, sender_id="999") is False


class TestGroupPolicyOpen:
    def test_responds_without_mention(self):
        ch = _make_channel(allowed_users=["123"], group_policy="open")
        assert ch._should_respond(is_dm=False, is_mentioned=False, sender_id="123") is True

    def test_rejects_disallowed_user(self):
        ch = _make_channel(allowed_users=["123"], group_policy="open")
        assert ch._should_respond(is_dm=False, is_mentioned=False, sender_id="999") is False


class TestDMAlwaysResponds:
    def test_dm_responds_for_allowed_user(self):
        ch = _make_channel(allowed_users=["123"], group_policy="mention")
        assert ch._should_respond(is_dm=True, is_mentioned=False, sender_id="123") is True

    def test_dm_responds_regardless_of_group_policy(self):
        for policy in ("mention", "open"):
            ch = _make_channel(allowed_users=["123"], group_policy=policy)
            assert ch._should_respond(is_dm=True, is_mentioned=False, sender_id="123") is True

    def test_dm_rejects_disallowed_user(self):
        ch = _make_channel(allowed_users=["123"])
        assert ch._should_respond(is_dm=True, is_mentioned=False, sender_id="999") is False


class TestSplitMessage:
    def test_short_message_unchanged(self):
        assert _split_message("hello") == ["hello"]

    def test_empty_returns_empty_list(self):
        assert _split_message("") == []

    def test_exact_limit_unchanged(self):
        text = "a" * DISCORD_MAX_LENGTH
        assert _split_message(text) == [text]

    def test_splits_at_newline(self):
        line1 = "a" * 1000
        line2 = "b" * 1500
        chunks = _split_message(f"{line1}\n{line2}")
        assert len(chunks) == 2
        assert chunks[0] == line1
        assert chunks[1] == line2

    def test_splits_at_space_when_no_newline(self):
        word1 = "a" * 1000
        word2 = "b" * 1500
        chunks = _split_message(f"{word1} {word2}")
        assert len(chunks) == 2
        assert chunks[0] == word1
        assert chunks[1] == word2

    def test_hard_cut_when_no_break(self):
        text = "a" * 3000
        chunks = _split_message(text)
        assert len(chunks) == 2
        assert chunks[0] == "a" * DISCORD_MAX_LENGTH
        assert chunks[1] == "a" * 1000

    def test_prefers_newline_over_space(self):
        part1 = "a" * 500 + " " + "b" * 498
        text = f"{part1}\n" + "c" * 1500
        chunks = _split_message(text)
        assert chunks[0] == part1
        assert chunks[1] == "c" * 1500

    def test_multiple_splits(self):
        text = "a" * 5000
        chunks = _split_message(text)
        assert len(chunks) == 3
        assert chunks[0] == "a" * 2000
        assert chunks[1] == "a" * 2000
        assert chunks[2] == "a" * 1000


class TestMissingToken:
    def test_raises_without_token(self):
        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(RuntimeError, match="DISCORD_BOT_TOKEN not set"):
                DiscordChannel()

    def test_raises_with_empty_token(self):
        with patch.dict("os.environ", {"DISCORD_BOT_TOKEN": ""}):
            with pytest.raises(RuntimeError, match="DISCORD_BOT_TOKEN not set"):
                DiscordChannel()


class TestReceive:
    def test_returns_queued_message(self):
        ch = _make_channel(allowed_users=["123"])
        msg = InboundMessage(
            sender_id="123",
            text="hello",
            channel_id="discord",
            timestamp=datetime.now(timezone.utc),
        )
        ch._queue.put(msg)
        result = ch.receive()
        assert result is not None
        assert result.text == "hello"
        assert result.sender_id == "123"
        assert result.channel_id == "discord"

    def test_returns_none_on_empty_queue(self):
        ch = _make_channel(allowed_users=["123"])
        result = ch.receive()
        assert result is None


class TestSend:
    def test_noop_without_setup(self):
        """send() before the client connects dispatches nothing and changes nothing.

        The test asserted only that the call did not raise, so anything the
        guard path did on its way out was invisible.
        """
        ch = _make_channel(allowed_users=["123"])
        with patch(
            "contrib.channel_discord.asyncio.run_coroutine_threadsafe",
        ) as mock_rcts:
            ch.send(OutboundMessage(text="hello"))
        mock_rcts.assert_not_called()
        assert ch._client is None
        assert ch._reply_channel is None
        assert ch._loop is None

    def test_splits_and_sends_long_messages(self):
        ch = _make_channel(allowed_users=["123"])
        mock_future = MagicMock()
        mock_future.result.return_value = None

        ch._client = MagicMock()
        ch._reply_channel = MagicMock()
        ch._loop = MagicMock()

        with patch(
            "contrib.channel_discord.asyncio.run_coroutine_threadsafe",
            return_value=mock_future,
        ) as mock_rcts:
            ch.send(OutboundMessage(text="a" * 3000))
            assert mock_rcts.call_count == 2

    def test_sends_attachments(self):
        ch = _make_channel(allowed_users=["123"])
        mock_future = MagicMock()
        mock_future.result.return_value = None

        ch._client = MagicMock()
        ch._reply_channel = MagicMock()
        ch._loop = MagicMock()

        with patch(
            "contrib.channel_discord.asyncio.run_coroutine_threadsafe",
            return_value=mock_future,
        ) as mock_rcts:
            ch.send(OutboundMessage(text="here", attachments=[Path("/tmp/f.txt")]))
            assert mock_rcts.call_count == 2

    def test_noop_when_loop_missing(self):
        ch = _make_channel(allowed_users=["123"])
        ch._client = MagicMock()
        ch._reply_channel = MagicMock()
        ch.send(OutboundMessage(text="hello"))


class TestAnnouncementsNeverGoToAGuild:
    """The last room the owner spoke in was the send target for everything,
    so a heartbeat composed from the DM conversation could land in a guild
    channel. A reply names its room; an announcement goes to the DM."""

    def _sending_channel(self):
        ch = _make_channel(allowed_users=["123"])
        ch._client = MagicMock()
        ch._reply_channel = MagicMock(name="dm")
        ch._loop = MagicMock()
        return ch

    def test_a_reply_is_sent_to_its_room_not_the_dm(self):
        ch = self._sending_channel()
        room = MagicMock(name="room")
        ch._client.get_channel.return_value = room
        mock_future = MagicMock()
        mock_future.result.return_value = None
        with patch(
            "contrib.channel_discord.asyncio.run_coroutine_threadsafe",
            return_value=mock_future,
        ):
            ch.send(OutboundMessage(text="for the room", group_id="4242"))
        ch._client.get_channel.assert_called_once_with(4242)
        room.send.assert_called_once_with("for the room")
        ch._reply_channel.send.assert_not_called()

    def test_an_announcement_goes_to_the_dm(self):
        ch = self._sending_channel()
        mock_future = MagicMock()
        mock_future.result.return_value = None
        with patch(
            "contrib.channel_discord.asyncio.run_coroutine_threadsafe",
            return_value=mock_future,
        ):
            ch.send(OutboundMessage(text="for you"))
        ch._client.get_channel.assert_not_called()
        ch._reply_channel.send.assert_called_once_with("for you")

    def test_an_uncached_room_is_fetched(self):
        ch = self._sending_channel()
        ch._client.get_channel.return_value = None
        room = MagicMock(name="room")
        fetch_future = MagicMock()
        fetch_future.result.return_value = room
        send_future = MagicMock()
        send_future.result.return_value = None
        with patch(
            "contrib.channel_discord.asyncio.run_coroutine_threadsafe",
            side_effect=[fetch_future, send_future],
        ):
            ch.send(OutboundMessage(text="hi", group_id="4242"))
        ch._client.fetch_channel.assert_called_once_with(4242)
        room.send.assert_called_once_with("hi")


class TestChannelId:
    def test_channel_id_is_discord(self):
        ch = _make_channel(allowed_users=["123"])
        assert ch.channel_id == "discord"


class TestPoll:
    def test_poll_returns_none(self):
        ch = _make_channel(allowed_users=["123"])
        assert ch.poll() is None


class TestSendAudio:
    def _sending_channel(self):
        ch = _make_channel(allowed_users=["123"])
        ch._client = MagicMock()
        ch._reply_channel = MagicMock()
        ch._loop = MagicMock()
        return ch

    def test_sends_voice_reply_as_file(self):
        ch = self._sending_channel()
        mock_future = MagicMock()
        mock_future.result.return_value = None

        with patch(
            "contrib.channel_discord.asyncio.run_coroutine_threadsafe",
            return_value=mock_future,
        ) as mock_rcts:
            ch.send(OutboundMessage(
                text="reply", audio=b"AUDIO", audio_mime="audio/ogg",
            ))
            assert mock_rcts.call_count == 2

    def test_no_audio_sends_text_only(self):
        ch = self._sending_channel()
        mock_future = MagicMock()
        mock_future.result.return_value = None

        with patch(
            "contrib.channel_discord.asyncio.run_coroutine_threadsafe",
            return_value=mock_future,
        ) as mock_rcts:
            ch.send(OutboundMessage(text="reply"))
            assert mock_rcts.call_count == 1


class TestReplyChannelSurvivesRestart:
    """The same restart problem Telegram had, on the reply channel.

    _reply_channel is a discord.py object and cannot be persisted, so the
    id is, and on_ready resolves it back through the client.
    """

    def test_channel_id_is_persisted_and_reloaded(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        ch = _make_channel(allowed_users=["1"], workspace=workspace)
        assert ch._reply_channel_id is None
        target = MagicMock()
        target.id = 987
        ch._remember_channel(target)

        restarted = _make_channel(allowed_users=["1"], workspace=workspace)
        assert restarted._reply_channel_id == 987
        assert restarted._reply_channel is None

    def test_no_workspace_means_no_persistence(self, tmp_path):
        ch = _make_channel(allowed_users=["1"])
        target = MagicMock()
        target.id = 5
        ch._remember_channel(target)
        assert ch._reply_channel is target
        assert ch._state_path is None

    def test_a_damaged_state_file_is_not_fatal(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "channel-discord.json").write_text("[]")

        ch = _make_channel(allowed_users=["1"], workspace=workspace)
        assert ch._reply_channel_id is None


class TestReplyChannelRestoredThroughTheApi:
    """A DM channel is not in discord.py's cache after a restart, so
    get_channel returned None, the log said the channel was "no longer
    reachable", and every heartbeat until the owner next spoke was silently
    dropped while recorded as delivered."""

    def _ready_channel(self, tmp_path, channel_id=987):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        ch = _make_channel(allowed_users=["1"], workspace=workspace)
        ch._reply_channel_id = channel_id
        ch._client = MagicMock()
        return ch

    def test_dm_not_in_cache_is_fetched(self, tmp_path):
        import asyncio
        ch = self._ready_channel(tmp_path)
        ch._client.get_channel.return_value = None
        dm = MagicMock()

        async def fetch(channel_id):
            assert channel_id == 987
            return dm

        ch._client.fetch_channel = fetch
        asyncio.run(ch._restore_reply_channel())
        assert ch._reply_channel is dm

    def test_cache_hit_skips_the_api(self, tmp_path):
        import asyncio
        ch = self._ready_channel(tmp_path)
        cached = MagicMock()
        ch._client.get_channel.return_value = cached
        ch._client.fetch_channel = MagicMock(side_effect=AssertionError("should not be called"))
        asyncio.run(ch._restore_reply_channel())
        assert ch._reply_channel is cached

    def test_unreachable_channel_leaves_no_target(self, tmp_path):
        import asyncio
        ch = self._ready_channel(tmp_path)
        ch._client.get_channel.return_value = None

        async def fetch(channel_id):
            raise Exception("Unknown Channel")

        ch._client.fetch_channel = fetch
        asyncio.run(ch._restore_reply_channel())
        assert ch._reply_channel is None

    def test_send_with_no_target_after_start_raises(self):
        """So the scheduler records a failed delivery instead of a success."""
        ch = _make_channel(allowed_users=["1"])
        ch._client = MagicMock()
        ch._loop = MagicMock()
        ch._reply_channel = None
        with pytest.raises(RuntimeError, match="no reply target"):
            ch.send(OutboundMessage(text="hello"))


class TestNonMediaAttachmentsAreKept:
    """A PDF used to fall out of the attachment loop unsaved and unmentioned.

    The operator was then told "(sent an empty message)" and the file was
    gone.
    """

    def test_attachment_saved_to_inbox(self, tmp_path):
        import asyncio

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        ch = _make_channel(allowed_users=["1"], workspace=workspace)

        saved = {}

        async def fake_save(dest):
            saved["dest"] = dest
            Path(dest).write_text("pdf bytes")

        attachment = MagicMock()
        attachment.filename = "report.pdf"
        attachment.id = 77
        attachment.save = fake_save

        dest = asyncio.run(ch._save_attachment(attachment, "file"))
        assert dest == workspace / "shared" / "inbox" / "discord_77_report.pdf"
        assert dest.read_text() == "pdf bytes"

    def test_traversal_in_the_filename_is_stripped(self, tmp_path):
        import asyncio

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        ch = _make_channel(allowed_users=["1"], workspace=workspace)

        async def fake_save(dest):
            Path(dest).write_text("x")

        attachment = MagicMock()
        attachment.filename = "../../etc/passwd"
        attachment.id = 1
        attachment.save = fake_save

        dest = asyncio.run(ch._save_attachment(attachment, "file"))
        assert dest.parent == workspace / "shared" / "inbox"
        assert dest.name == "discord_1_passwd"
