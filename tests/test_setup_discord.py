import hashlib
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from faffmonkey.cli.setup_discord import _validate_token, run_setup_discord


def _file_hash(path: Path) -> str:
    """The provenance hash install_extension records, recomputed here."""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]





import pytest


@pytest.fixture(autouse=True)
def _no_requirements_mirror(monkeypatch):
    """install_extension mirrors requirements.extra.txt into the real
    checkout; tests must not write outside tmp_path."""
    monkeypatch.setattr(
        "faffmonkey.cli.setup_provider._mirror_requirements",
        lambda *a, **k: None,
    )

class TestValidateToken:
    @patch("faffmonkey.cli.setup_discord.urllib.request.urlopen")
    def test_sends_a_user_agent(self, mock_urlopen: MagicMock) -> None:
        """Discord fronts the API with a filter that 403s urllib's default
        User-Agent before the token is ever checked, so every token looked
        invalid."""
        resp = MagicMock()
        resp.read.return_value = b'{"username": "bot"}'
        mock_urlopen.return_value.__enter__.return_value = resp

        assert _validate_token("a.b.c") is True
        req = mock_urlopen.call_args.args[0]
        assert req.get_header("User-agent", "").startswith("faffmonkey/")
        assert req.get_header("Authorization") == "Bot a.b.c"


class TestSetupDiscordAbort:
    @patch("faffmonkey.cli.setup_discord._read_input", return_value="n")
    @patch("faffmonkey.cli.setup_provider._find_project_root")
    def test_abort_on_deny(
        self,
        mock_root: MagicMock,
        mock_input: MagicMock,
        tmp_path: Path,
    ) -> None:
        project_root = tmp_path / "project"
        project_root.mkdir()
        (project_root / "contrib").mkdir()
        (project_root / "contrib" / "channel_discord.py").write_text("# stub")
        mock_root.return_value = project_root

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "config.json").write_text("{}")

        with pytest.raises(SystemExit):
            run_setup_discord(state_dir, base_dir=tmp_path)

    @patch("faffmonkey.cli.setup_provider._find_project_root")
    def test_missing_contrib_file(
        self,
        mock_root: MagicMock,
        tmp_path: Path,
    ) -> None:
        project_root = tmp_path / "project"
        project_root.mkdir()
        (project_root / "contrib").mkdir()
        mock_root.return_value = project_root

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "config.json").write_text("{}")

        base_dir = tmp_path / "base"
        base_dir.mkdir()
        (base_dir / "extensions").mkdir()

        with pytest.raises(SystemExit):
            run_setup_discord(state_dir, base_dir=base_dir)


class TestSetupDiscordEmptyToken:
    @patch("faffmonkey.cli.setup_discord.getpass.getpass", return_value="")
    @patch("faffmonkey.cli.setup_discord._read_input", return_value="y")
    @patch("faffmonkey.cli.setup_provider._find_project_root")
    def test_exits_on_empty_token(
        self,
        mock_root: MagicMock,
        mock_input: MagicMock,
        mock_getpass: MagicMock,
        tmp_path: Path,
    ) -> None:
        project_root = tmp_path / "project"
        project_root.mkdir()
        (project_root / "contrib").mkdir()
        (project_root / "contrib" / "channel_discord.py").write_text("# stub")
        mock_root.return_value = project_root

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "config.json").write_text("{}")

        base_dir = tmp_path / "base"
        base_dir.mkdir()
        (base_dir / "extensions").mkdir()

        with pytest.raises(SystemExit):
            run_setup_discord(state_dir, base_dir=base_dir)




class TestSetupDiscordExtensionExists:
    @patch("faffmonkey.cli.setup_discord.getpass.getpass", return_value="token123")
    @patch("faffmonkey.cli.setup_discord._read_input")
    @patch("faffmonkey.cli.setup_provider._find_project_root")
    def test_aborts_without_clobbering_a_file_we_did_not_install(
        self,
        mock_root: MagicMock,
        mock_input: MagicMock,
        mock_getpass: MagicMock,
        tmp_path: Path,
        capsys,
    ) -> None:
        project_root = tmp_path / "project"
        project_root.mkdir()
        (project_root / "contrib").mkdir()
        (project_root / "contrib" / "channel_discord.py").write_text("# contrib version")
        mock_root.return_value = project_root

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "config.json").write_text(json.dumps(
            {"models": {"main": {"provider": "t", "model": "m", "base_url": "http://x"}}}
        ))

        base_dir = tmp_path / "base"
        base_dir.mkdir()
        extensions_dir = base_dir / "extensions"
        extensions_dir.mkdir()
        existing = extensions_dir / "channel_discord.py"
        existing.write_text("# hand-written version")

        mock_input.side_effect = ["111222333"]

        with pytest.raises(SystemExit):
            run_setup_discord(state_dir, base_dir=base_dir)

        assert "did not come from contrib" in capsys.readouterr().out
        assert existing.read_text() == "# hand-written version"

    @patch("faffmonkey.cli.setup_discord._validate_token", return_value=True)
    @patch("faffmonkey.cli.setup_discord.getpass.getpass", return_value="token123")
    @patch("faffmonkey.cli.setup_discord._read_input")
    @patch("faffmonkey.cli.setup_provider._find_project_root")
    def test_refreshes_an_extension_we_installed(
        self,
        mock_root: MagicMock,
        mock_input: MagicMock,
        mock_getpass: MagicMock,
        mock_validate: MagicMock,
        tmp_path: Path,
    ) -> None:
        project_root = tmp_path / "project"
        project_root.mkdir()
        (project_root / "contrib").mkdir()
        (project_root / "contrib" / "channel_discord.py").write_text("# new contrib version")
        mock_root.return_value = project_root

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "config.json").write_text(json.dumps(
            {"models": {"main": {"provider": "t", "model": "m", "base_url": "http://x"}}}
        ))

        base_dir = tmp_path / "base"
        base_dir.mkdir()
        extensions_dir = base_dir / "extensions"
        extensions_dir.mkdir()
        existing = extensions_dir / "channel_discord.py"
        existing.write_text("# stale installed version")
        (extensions_dir / ".origin.json").write_text(json.dumps({
            "channel_discord.py": {"source": "contrib/channel_discord.py"},
        }))

        mock_input.side_effect = ["111222333"]

        run_setup_discord(state_dir, base_dir=base_dir)

        assert existing.read_text() == "# new contrib version"
        jobs = json.loads((base_dir / "workspace" / "config" / "jobs.json").read_text())
        assert [j["id"] for j in jobs] == ["heartbeat", "morning", "evening", "preconscious-decay"]
        assert jobs[0]["deliver"] == {"mode": "announce", "channel": "last"}


class TestSetupDiscordDispatcher:
    def test_cmd_setup_dispatches_discord(self, tmp_path: Path) -> None:
        from faffmonkey.cli.__main__ import build_parser, cmd_setup

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "config.json").write_text("{}")

        parser = build_parser()
        args = parser.parse_args(["setup", "discord", "--state-dir", str(state_dir)])
        assert args.setup_command == "discord"

        with patch("faffmonkey.cli.setup_discord.run_setup_discord") as mock_run:
            cmd_setup(args)
            mock_run.assert_called_once()
