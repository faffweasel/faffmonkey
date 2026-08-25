"""Tests for contrib/channel_telegram.py outbound sends."""

import logging
import sys
from unittest.mock import MagicMock, patch

import pytest

if "telegram" not in sys.modules:
    sys.modules["telegram"] = MagicMock()
    sys.modules["telegram.ext"] = MagicMock()

from faffmonkey.types import OutboundMessage
from contrib.channel_telegram import TelegramChannel, _group_id, _normalise_command


def _make_channel(**kwargs) -> TelegramChannel:
    with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "test-token"}):
        return TelegramChannel(**kwargs)


class TestSend:
    def _sending_channel(self) -> TelegramChannel:
        ch = _make_channel(allowed_users=["1"])
        ch._app = MagicMock()
        ch._last_chat_id = 42
        ch._loop = MagicMock()
        return ch

    def _patched_send(self, ch: TelegramChannel, message: OutboundMessage) -> MagicMock:
        mock_future = MagicMock()
        mock_future.result.return_value = None
        with patch(
            "contrib.channel_telegram.asyncio.run_coroutine_threadsafe",
            return_value=mock_future,
        ) as mock_rcts:
            ch.send(message)
        return mock_rcts

    def test_text_sent_via_event_loop(self):
        ch = self._sending_channel()
        mock_rcts = self._patched_send(ch, OutboundMessage(text="reply"))
        assert mock_rcts.call_count == 1
        ch._app.bot.send_message.assert_called_once_with(chat_id=42, text="reply")

    def test_ogg_audio_sent_as_voice(self):
        ch = self._sending_channel()
        mock_rcts = self._patched_send(ch, OutboundMessage(
            text="reply", audio=b"OGGDATA", audio_mime="audio/ogg",
        ))
        assert mock_rcts.call_count == 2
        ch._app.bot.send_voice.assert_called_once()
        assert ch._app.bot.send_voice.call_args.kwargs["chat_id"] == 42
        ch._app.bot.send_audio.assert_not_called()

    def test_other_audio_sent_as_audio_file(self):
        ch = self._sending_channel()
        mock_rcts = self._patched_send(ch, OutboundMessage(
            text="reply", audio=b"WAVDATA", audio_mime="audio/wav",
        ))
        assert mock_rcts.call_count == 2
        ch._app.bot.send_audio.assert_called_once()
        ch._app.bot.send_voice.assert_not_called()

    def test_send_failure_logged_not_raised(self, caplog):
        """The log line is the only evidence a send failed.

        The name claimed both halves and the test checked only that send()
        did not raise, so discarding the exception silently passed. A
        delivery that fails without a log is invisible to the operator.
        """
        ch = self._sending_channel()
        mock_future = MagicMock()
        mock_future.result.side_effect = TimeoutError("no reply")
        with caplog.at_level(logging.WARNING, logger="contrib.channel_telegram"):
            with patch(
                "contrib.channel_telegram.asyncio.run_coroutine_threadsafe",
                return_value=mock_future,
            ):
                ch.send(OutboundMessage(text="reply"))
        assert [r.getMessage() for r in caplog.records] == [
            "telegram send_message failed: no reply",
        ]

    def test_send_noop_without_loop(self):
        ch = _make_channel(allowed_users=["1"])
        ch._app = MagicMock()
        ch._last_chat_id = 42
        ch.send(OutboundMessage(text="reply"))
        ch._app.bot.send_message.assert_not_called()

    def test_send_noop_without_chat(self):
        ch = _make_channel(allowed_users=["1"])
        ch._app = MagicMock()
        ch._loop = MagicMock()
        ch.send(OutboundMessage(text="reply"))
        ch._app.bot.send_message.assert_not_called()


class TestStop:
    def test_stop_uses_call_soon_threadsafe(self):
        ch = _make_channel(allowed_users=["1"])
        ch._app = MagicMock()
        ch._loop = MagicMock()
        ch.stop()
        ch._loop.call_soon_threadsafe.assert_called_once_with(ch._app.stop_running)

    def test_stop_noop_before_start(self):
        ch = _make_channel(allowed_users=["1"])
        ch.stop()


def _update(user_id: str, chat_id: int | None = 99) -> MagicMock:
    update = MagicMock()
    update.effective_message = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    if chat_id is None:
        update.effective_chat = None
    else:
        update.effective_chat = MagicMock()
        update.effective_chat.id = chat_id
    return update


class TestAcceptRecordsChatOnlyForAllowedSenders:
    """The reply chat must not be recorded before the allow-list check.

    Recording first let any stranger who messaged the bot redirect the
    owner's in-flight reply, and every later cron announcement, to the
    stranger's chat.
    """

    def test_allowed_sender_records_chat(self):
        ch = _make_channel(allowed_users=["1"])
        assert ch._accept(_update("1", chat_id=99)) == "1"
        assert ch._last_chat_id == 99

    def test_stranger_is_dropped_and_records_nothing(self):
        ch = _make_channel(allowed_users=["1"])
        assert ch._accept(_update("666", chat_id=1234)) is None
        assert ch._last_chat_id is None

    def test_stranger_cannot_hijack_an_established_chat(self):
        ch = _make_channel(allowed_users=["1"])
        ch._accept(_update("1", chat_id=99))
        assert ch._accept(_update("666", chat_id=1234)) is None
        assert ch._last_chat_id == 99

    def test_update_without_chat_does_not_blank_a_good_value(self):
        ch = _make_channel(allowed_users=["1"])
        ch._accept(_update("1", chat_id=99))
        ch._accept(_update("1", chat_id=None))
        assert ch._last_chat_id == 99

    def test_missing_message_or_user_is_dropped(self):
        ch = _make_channel(allowed_users=["1"])
        no_msg = _update("1")
        no_msg.effective_message = None
        assert ch._accept(no_msg) is None
        no_user = _update("1")
        no_user.effective_user = None
        assert ch._accept(no_user) is None
        assert ch._last_chat_id is None


class TestChatIdSurvivesRestart:
    """A cron job that fires before the operator speaks has to reach them.

    _last_chat_id was in-process only, so after a container restart send()
    found no target, returned without raising, and the scheduler recorded
    the run as delivered. The briefing was in the session and had gone
    nowhere.
    """

    def test_chat_id_is_persisted_and_reloaded(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        ch = _make_channel(allowed_users=["1"], workspace=workspace)
        assert ch._last_chat_id is None
        ch._remember_chat_id(4242)

        restarted = _make_channel(allowed_users=["1"], workspace=workspace)
        assert restarted._last_chat_id == 4242

    def test_no_workspace_means_no_persistence(self, tmp_path):
        ch = _make_channel(allowed_users=["1"])
        ch._remember_chat_id(7)
        assert ch._last_chat_id == 7
        assert ch._state_path is None

    def test_a_damaged_state_file_is_not_fatal(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "channel-telegram.json").write_text("{not json")

        ch = _make_channel(allowed_users=["1"], workspace=workspace)
        assert ch._last_chat_id is None


class TestPollingThread:
    def test_worker_thread_has_an_event_loop_when_polling_starts(self):
        """run_polling calls asyncio.get_event_loop(), which raises on a
        worker thread with no loop set. Every Telegram start died with
        "There is no current event loop in thread 'telegram-polling'" and
        the agent exited."""
        import asyncio
        import threading

        ch = _make_channel(allowed_users=["1"])
        seen: dict[str, object] = {}

        def fake_run_polling(**kwargs: object) -> None:
            try:
                seen["loop"] = asyncio.get_event_loop()
            except RuntimeError as e:
                seen["error"] = e

        ch._app = MagicMock()
        ch._app.run_polling.side_effect = fake_run_polling
        thread = threading.Thread(target=ch._poll_forever)
        thread.start()
        thread.join(timeout=5)

        assert "error" not in seen, seen.get("error")
        assert isinstance(seen["loop"], asyncio.AbstractEventLoop)


class TestSlashCommands:
    """/help in Telegram did nothing: the text handler was registered with
    ~filters.COMMAND, so commands never reached the runtime that handles
    them."""

    def test_text_handler_does_not_exclude_commands(self):
        from telegram.ext import MessageHandler, filters

        MessageHandler.reset_mock()
        ch = _make_channel(allowed_users=["1"])
        ch.start()
        ch.stop()

        # filters.TEXT & ~filters.COMMAND is a different object from
        # filters.TEXT, so registering the bare filter is what this checks.
        text_filters = [c.args[0] for c in MessageHandler.call_args_list]
        assert filters.TEXT in text_filters

    @pytest.mark.parametrize("text, expected", [
        ("/help@faffbot", "/help"),
        ("/model@faffbot main gpt-4o", "/model main gpt-4o"),
        ("/help", "/help"),
        ("email me@example.com", "email me@example.com"),
    ])
    def test_group_command_suffix_is_stripped(self, text, expected):
        assert _normalise_command(text) == expected

    @pytest.mark.parametrize("text, expected", [
        ("/start", "/help"),
        ("/start@faffbot", "/help"),
        ("/started", "/started"),
        ("start", "start"),
    ])
    def test_start_button_is_answered_with_help(self, text, expected):
        """Telegram's Start button sends /start; the runtime had no such
        command, so a new chat opened with "Unknown command"."""
        assert _normalise_command(text) == expected


class TestGroupMessagesAreMarked:
    """A group reply is read by everyone in the group, so the loop keeps
    group conversations apart from the owner's direct one. The channel is
    what knows which is which."""

    @pytest.mark.parametrize("chat_type,expected", [
        ("private", None),
        ("group", "-100"),
        ("supergroup", "-100"),
        ("channel", None),
    ])
    def test_group_id_follows_chat_type(self, chat_type, expected):
        update = _update("1", chat_id=-100)
        update.effective_chat.type = chat_type
        assert _group_id(update) == expected

    def test_no_chat_is_not_a_group(self):
        assert _group_id(_update("1", chat_id=None)) is None


class TestAnnouncementsNeverGoToAGroup:
    """The last chat the owner spoke in was the send target for everything,
    so a heartbeat composed from the private conversation could land in a
    group. Only private chats are remembered; a reply carries its room."""

    def _sending_channel(self) -> TelegramChannel:
        ch = _make_channel(allowed_users=["1"])
        ch._app = MagicMock()
        ch._loop = MagicMock()
        return ch

    def _chat_ids(self, ch: TelegramChannel, message: OutboundMessage) -> list[int]:
        mock_future = MagicMock()
        mock_future.result.return_value = None
        with patch(
            "contrib.channel_telegram.asyncio.run_coroutine_threadsafe",
            return_value=mock_future,
        ):
            ch.send(message)
        return [c.kwargs["chat_id"] for c in ch._app.bot.send_message.call_args_list]

    def test_a_group_message_does_not_become_the_announcement_target(self):
        ch = _make_channel(allowed_users=["1"])
        private = _update("1", chat_id=99)
        private.effective_chat.type = "private"
        group = _update("1", chat_id=-100)
        group.effective_chat.type = "supergroup"
        ch._accept(private)
        ch._accept(group)
        assert ch._last_chat_id == 99

    def test_a_reply_goes_to_its_room_and_an_announcement_to_the_private_chat(self):
        ch = self._sending_channel()
        ch._last_chat_id = 99
        assert self._chat_ids(ch, OutboundMessage(text="for the room", group_id="-100")) == [-100]
        assert self._chat_ids(ch, OutboundMessage(text="for you")) == [-100, 99]

    def test_a_reply_to_a_room_needs_no_private_chat(self):
        ch = self._sending_channel()
        assert self._chat_ids(ch, OutboundMessage(text="hi", group_id="-100")) == [-100]
