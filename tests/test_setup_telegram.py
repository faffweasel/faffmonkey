import hashlib
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from faffmonkey.cli.setup_provider import _sanitise_display
from faffmonkey.cli.setup_telegram import _validate_token, run_setup_telegram


def _file_hash(path: Path) -> str:
    """The provenance hash install_extension records, recomputed here."""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]



import pytest


@pytest.fixture(autouse=True)
def _no_requirements_mirror(request, monkeypatch):
    """install_extension mirrors requirements.extra.txt into the real
    checkout; tests must not write outside tmp_path. TestRequirementsMirror
    tests the mirror itself against tmp paths, so it opts out."""
    if request.cls is not None and request.cls.__name__ == "TestRequirementsMirror":
        return
    monkeypatch.setattr(
        "faffmonkey.cli.setup_provider._mirror_requirements",
        lambda *a, **k: None,
    )

class TestSanitiseDisplay:
    def test_strips_ansi_escape(self):
        assert _sanitise_display("\x1b[31mred\x1b[0m") == "red"

    def test_strips_non_printable(self):
        assert _sanitise_display("hello\x00world\x07") == "helloworld"

    def test_preserves_normal_text(self):
        assert _sanitise_display("TestBot") == "TestBot"

    def test_strips_mixed_ansi_and_control(self):
        assert _sanitise_display("\x1b[1;32m\x07Evil\x1b[0m\x08Bot") == "EvilBot"

    def test_empty_string(self):
        assert _sanitise_display("") == ""


class TestValidateTokenSanitisation:
    @patch("faffmonkey.cli.setup_telegram.urllib.request.urlopen")
    def test_ansi_in_bot_name_stripped(self, mock_urlopen, capsys):
        resp = MagicMock()
        resp.read.return_value = json.dumps({
            "ok": True,
            "result": {
                "first_name": "\x1b[31mEvil\x1b[0m",
                "username": "evil\x07bot",
            },
        }).encode()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = resp
        assert _validate_token("123:ABC") is True
        out = capsys.readouterr().out
        assert "\x1b[" not in out
        assert "\x07" not in out
        assert "Evil" in out
        assert "evilbot" in out


class TestValidateToken:
    @patch("faffmonkey.cli.setup_telegram.urllib.request.urlopen")
    def test_valid_token(self, mock_urlopen: MagicMock) -> None:
        resp = MagicMock()
        resp.read.return_value = json.dumps({
            "ok": True,
            "result": {"first_name": "TestBot", "username": "test_bot"},
        }).encode()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = resp
        assert _validate_token("123:ABC") is True

    @patch("faffmonkey.cli.setup_telegram.urllib.request.urlopen")
    def test_invalid_token_api_false(self, mock_urlopen: MagicMock) -> None:
        resp = MagicMock()
        resp.read.return_value = json.dumps({"ok": False}).encode()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = resp
        assert _validate_token("bad") is False








class TestSetupTelegramRun:
    @patch("faffmonkey.cli.setup_telegram._validate_token", return_value=True)
    @patch("faffmonkey.cli.setup_telegram.getpass.getpass", return_value="token123")
    @patch("faffmonkey.cli.setup_telegram._read_input")
    @patch("faffmonkey.cli.setup_provider._find_project_root")
    def test_adds_the_heartbeat_job_for_telegram(
        self,
        mock_root: MagicMock,
        mock_input: MagicMock,
        mock_getpass: MagicMock,
        mock_validate: MagicMock,
        tmp_path: Path,
    ) -> None:
        project_root = tmp_path / "project"
        (project_root / "contrib").mkdir(parents=True)
        (project_root / "contrib" / "channel_telegram.py").write_text("# contrib")
        mock_root.return_value = project_root

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "config.json").write_text(json.dumps(
            {"models": {"main": {"provider": "t", "model": "m", "base_url": "http://x"}}}
        ))
        base_dir = tmp_path / "base"
        extensions_dir = base_dir / "extensions"
        extensions_dir.mkdir(parents=True)
        (extensions_dir / "channel_telegram.py").write_text("# contrib")
        (extensions_dir / ".origin.json").write_text(json.dumps({
            "channel_telegram.py": {"source": "contrib/channel_telegram.py"},
        }))
        mock_input.side_effect = ["111222333"]

        run_setup_telegram(state_dir, base_dir=base_dir)

        jobs = json.loads((base_dir / "workspace" / "config" / "jobs.json").read_text())
        assert [j["id"] for j in jobs] == ["heartbeat", "morning", "evening", "preconscious-decay"]
        assert jobs[0]["context"] == "heartbeat"
        assert jobs[0]["deliver"] == {"mode": "announce", "channel": "last"}


class TestSetupTelegramDispatcher:
    def test_cmd_setup_dispatches_telegram(self, tmp_path: Path) -> None:
        from faffmonkey.cli.__main__ import build_parser, cmd_setup

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "config.json").write_text("{}")

        parser = build_parser()
        args = parser.parse_args(["setup", "telegram", "--state-dir", str(state_dir)])
        assert args.setup_command == "telegram"

        with patch("faffmonkey.cli.setup_telegram.run_setup_telegram") as mock_run:
            cmd_setup(args)
            mock_run.assert_called_once()


class TestProvenanceSourceIsRelative:
    """P7-H4: an absolute source made doctor report a fault that did not exist."""

    def test_source_is_project_relative(self, tmp_path):
        from faffmonkey.cli.setup_provider import install_extension

        base = tmp_path / "project"
        (base / "contrib").mkdir(parents=True)
        (base / "contrib" / "channel_telegram.py").write_text("# channel")
        (base / "extensions").mkdir()

        with patch(
            "faffmonkey.cli.setup_provider._find_project_root", return_value=base,
        ):
            install_extension(
                base, "channel_telegram.py",
                dep_line="python-telegram-bot==21.6",
                confirm_prompt="Install?",
                read_input=lambda prompt, default: "y",
            )

        origin = json.loads((base / "extensions" / ".origin.json").read_text())
        assert origin["channel_telegram.py"]["source"] == "contrib/channel_telegram.py"


class TestRequirementsMirror:
    """The build reads the checkout's requirements.extra.txt while the data
    root holds the source of truth; without the mirror a re-clone silently
    builds an image with no extension dependencies."""

    def test_mirror_copies_into_project_root(self, tmp_path):
        from faffmonkey.cli.setup_provider import _mirror_requirements

        data_root = tmp_path / "data"
        data_root.mkdir()
        src = data_root / "requirements.extra.txt"
        src.write_text("python-telegram-bot>=21,<22\n")
        project = tmp_path / "checkout"
        project.mkdir()

        _mirror_requirements(src, project)
        assert (project / "requirements.extra.txt").read_text() == src.read_text()

    def test_same_path_is_a_no_op(self, tmp_path):
        from faffmonkey.cli.setup_provider import _mirror_requirements

        src = tmp_path / "requirements.extra.txt"
        src.write_text("dep==1\n")
        _mirror_requirements(src, tmp_path)
        assert src.read_text() == "dep==1\n"
