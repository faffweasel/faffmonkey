from io import StringIO
from unittest.mock import patch


from faffmonkey.cli.__main__ import _cli_tool_prompt
from faffmonkey.seams import channel_cli
from faffmonkey.seams.channel_cli import CLIChannel, discard_typeahead
from faffmonkey.seams.channel_noop import NoopChannel
from faffmonkey.types import OutboundMessage


class _Tty:
    def __init__(self, is_tty: bool) -> None:
        self._is_tty = is_tty

    def isatty(self) -> bool:
        return self._is_tty

    def fileno(self) -> int:
        return 0


class TestTypeahead:
    """2026-08-27: text typed while the agent was busy was echoed raw by
    the tty and then submitted by the next prompt, so a stray "y" could
    answer an approval prompt the user never saw."""

    def test_flushes_tty_before_chat_prompt(self):
        order = []
        with patch.object(channel_cli.sys, "stdin", _Tty(True)), \
             patch.object(channel_cli.termios, "tcflush",
                          side_effect=lambda *a: order.append("flush")), \
             patch("builtins.input",
                   side_effect=lambda *a: order.append("input") or "hi"):
            CLIChannel().receive()
        assert order == ["flush", "input"]

    def test_flushes_tty_before_approval_prompt(self):
        order = []
        with patch.object(channel_cli.sys, "stdin", _Tty(True)), \
             patch.object(channel_cli.termios, "tcflush",
                          side_effect=lambda *a: order.append("flush")), \
             patch("builtins.input",
                   side_effect=lambda *a: order.append("input") or "y"):
            assert _cli_tool_prompt("shell_exec") is True
        assert order == ["flush", "input"]

    def test_no_tty_no_flush(self):
        with patch.object(channel_cli.sys, "stdin", _Tty(False)), \
             patch.object(channel_cli.termios, "tcflush") as flush:
            discard_typeahead()
        flush.assert_not_called()

    def test_tty_error_is_swallowed(self):
        with patch.object(channel_cli.sys, "stdin", _Tty(True)), \
             patch.object(channel_cli.termios, "tcflush",
                          side_effect=channel_cli.termios.error(5, "eio")):
            discard_typeahead()


class TestCLIChannel:
    def test_channel_id(self):
        ch = CLIChannel()
        assert ch.channel_id == "cli"

    def test_is_allowed_always_true(self):
        ch = CLIChannel()
        assert ch.is_allowed("anyone") is True
        assert ch.is_allowed("") is True

    def test_receive_returns_message(self):
        ch = CLIChannel()
        with patch("builtins.input", return_value="hello"):
            msg = ch.receive()
        assert msg is not None
        assert msg.text == "hello"
        assert msg.sender_id == "cli-user"
        assert msg.channel_id == "cli"

    def test_receive_empty_returns_none(self):
        ch = CLIChannel()
        with patch("builtins.input", return_value=""):
            msg = ch.receive()
        assert msg is None

    def test_receive_whitespace_returns_none(self):
        ch = CLIChannel()
        with patch("builtins.input", return_value="   "):
            msg = ch.receive()
        assert msg is None

    def test_receive_eof_returns_none(self):
        ch = CLIChannel()
        with patch("builtins.input", side_effect=EOFError):
            msg = ch.receive()
        assert msg is None

    def test_receive_keyboard_interrupt_returns_none(self):
        ch = CLIChannel()
        with patch("builtins.input", side_effect=KeyboardInterrupt):
            msg = ch.receive()
        assert msg is None

    def test_send_writes_to_stdout(self):
        ch = CLIChannel()
        buf = StringIO()
        with patch("sys.stdout", buf):
            ch.send(OutboundMessage(text="hello back"))
        assert "agent> hello back\n" == buf.getvalue()

    def test_send_with_attachments(self, tmp_path):
        ch = CLIChannel()
        buf = StringIO()
        fake_path = tmp_path / "file.txt"
        with patch("sys.stdout", buf):
            ch.send(OutboundMessage(text="here you go", attachments=[fake_path]))
        output = buf.getvalue()
        assert "agent> here you go\n" in output
        assert "[attachment:" in output

    def test_start_stop_leave_the_channel_open(self):
        """Only EOF in receive() closes a CLIChannel.

        The old test called both and asserted nothing, so making stop() set
        _closed would have passed while every later receive() returned None
        forever. No test anywhere called CLIChannel.is_closed().
        """
        ch = CLIChannel()
        ch.start()
        assert ch.is_closed() is False
        ch.stop()
        assert ch.is_closed() is False
        with patch("builtins.input", return_value="still here"):
            msg = ch.receive()
        assert msg is not None
        assert msg.text == "still here"


class TestNoopChannel:
    def test_is_allowed_always_false(self):
        ch = NoopChannel()
        assert ch.is_allowed("anyone") is False
        assert ch.is_allowed("") is False
