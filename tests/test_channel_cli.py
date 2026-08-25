from io import StringIO
from unittest.mock import patch


from faffmonkey.seams.channel_cli import CLIChannel
from faffmonkey.seams.channel_noop import NoopChannel
from faffmonkey.types import OutboundMessage


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
