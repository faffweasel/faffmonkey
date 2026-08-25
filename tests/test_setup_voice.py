import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from faffmonkey.cli.setup_voice import run_setup_voice


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    contrib = root / "contrib"
    contrib.mkdir()
    (contrib / "transcriber_openai.py").write_text("# transcriber stub")
    (contrib / "synthesiser_openai.py").write_text("# synthesiser stub")
    return root


@pytest.fixture
def state_and_base(tmp_path: Path) -> tuple[Path, Path]:
    base_dir = tmp_path / "base"
    base_dir.mkdir()
    state_dir = base_dir / "state"
    state_dir.mkdir()
    (state_dir / "config.json").write_text(json.dumps({"timezone": "UTC"}))
    return state_dir, base_dir


@patch("faffmonkey.cli.setup_provider._find_project_root")
@patch("faffmonkey.cli.setup_voice._read_input")
class TestSetupVoice:
    def test_both_enabled(
        self, mock_input: MagicMock, mock_root: MagicMock,
        project_root: Path, state_and_base: tuple[Path, Path], monkeypatch,
    ) -> None:
        mock_root.return_value = project_root
        state_dir, base_dir = state_and_base
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        mock_input.side_effect = [
            "y", "y", "OPENAI_API_KEY",
            "https://api.openai.com/v1", "whisper-1", "tts-1", "alloy",
        ]
        # The key is read with getpass, never the echoing prompt: the
        # provider and channel wizards hid it and this one showed it.
        with patch("faffmonkey.cli.setup_voice.getpass.getpass", return_value="sk-test-123"):
            run_setup_voice(state_dir, base_dir=base_dir)

        ext_dir = base_dir / "extensions"
        assert (ext_dir / "transcriber_openai.py").exists()
        assert (ext_dir / "synthesiser_openai.py").exists()
        origin = json.loads((ext_dir / ".origin.json").read_text())
        assert "transcriber_openai.py" in origin
        assert "synthesiser_openai.py" in origin
        assert "OPENAI_API_KEY=sk-test-123" in (state_dir / ".env").read_text()
        config = json.loads((state_dir / "config.json").read_text())
        assert config["voice"] == {
            "api_key_env": "OPENAI_API_KEY",
            "base_url": "https://api.openai.com/v1",
            "transcriber": "openai",
            "transcriber_module": "extensions.transcriber_openai.OpenAITranscriber",
            "transcriber_model": "whisper-1",
            "synthesiser": "openai",
            "synthesiser_module": "extensions.synthesiser_openai.OpenAISynthesiser",
            "synthesiser_model": "tts-1",
            "synthesiser_voice": "alloy",
        }

    def test_transcription_only(
        self, mock_input: MagicMock, mock_root: MagicMock,
        project_root: Path, state_and_base: tuple[Path, Path], monkeypatch,
    ) -> None:
        mock_root.return_value = project_root
        state_dir, base_dir = state_and_base
        monkeypatch.setenv("OPENAI_API_KEY", "already-set")
        mock_input.side_effect = [
            "y", "n", "OPENAI_API_KEY", "https://api.openai.com/v1", "whisper-1",
        ]

        run_setup_voice(state_dir, base_dir=base_dir)

        ext_dir = base_dir / "extensions"
        assert (ext_dir / "transcriber_openai.py").exists()
        assert not (ext_dir / "synthesiser_openai.py").exists()
        config = json.loads((state_dir / "config.json").read_text())
        assert config["voice"]["transcriber"] == "openai"
        assert "synthesiser" not in config["voice"]

    def test_env_var_already_set_skips_key_prompt(
        self, mock_input: MagicMock, mock_root: MagicMock,
        project_root: Path, state_and_base: tuple[Path, Path], monkeypatch,
    ) -> None:
        mock_root.return_value = project_root
        state_dir, base_dir = state_and_base
        monkeypatch.setenv("OPENAI_API_KEY", "already-set")
        mock_input.side_effect = [
            "y", "y", "OPENAI_API_KEY",
            "https://api.openai.com/v1", "whisper-1", "tts-1", "alloy",
        ]

        run_setup_voice(state_dir, base_dir=base_dir)

        assert not (state_dir / ".env").exists()

    def test_both_disabled_does_nothing(
        self, mock_input: MagicMock, mock_root: MagicMock,
        project_root: Path, state_and_base: tuple[Path, Path],
    ) -> None:
        mock_root.return_value = project_root
        state_dir, base_dir = state_and_base
        mock_input.side_effect = ["n", "n"]

        run_setup_voice(state_dir, base_dir=base_dir)

        assert not (base_dir / "extensions").exists()
        config = json.loads((state_dir / "config.json").read_text())
        assert "voice" not in config

    def test_key_pasted_at_name_prompt_is_reprompted_not_echoed(
        self, mock_input: MagicMock, mock_root: MagicMock,
        project_root: Path, state_and_base: tuple[Path, Path], monkeypatch, capsys,
    ) -> None:
        """Pasting the key where the variable name goes exited the wizard
        with the full key printed in an 'Invalid name' error."""
        mock_root.return_value = project_root
        state_dir, base_dir = state_and_base
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        mock_input.side_effect = [
            "y", "y", "sk-proj-r4SECRET", "OPENAI_API_KEY",
            "https://api.openai.com/v1", "whisper-1", "tts-1", "alloy",
        ]

        with patch("faffmonkey.cli.setup_voice.getpass.getpass", return_value="sk-proj-r4SECRET"):
            run_setup_voice(state_dir, base_dir=base_dir)

        out = capsys.readouterr().out
        assert "sk-proj-r4SECRET" not in out
        assert "wants the name" in out
        assert "OPENAI_API_KEY=sk-proj-r4SECRET" in (state_dir / ".env").read_text()

    def test_invalid_base_url_exits(
        self, mock_input: MagicMock, mock_root: MagicMock,
        project_root: Path, state_and_base: tuple[Path, Path], monkeypatch,
    ) -> None:
        mock_root.return_value = project_root
        state_dir, base_dir = state_and_base
        monkeypatch.setenv("OPENAI_API_KEY", "already-set")
        mock_input.side_effect = [
            "y", "y", "OPENAI_API_KEY", "http://not-local.example.com/v1",
        ]

        with pytest.raises(SystemExit):
            run_setup_voice(state_dir, base_dir=base_dir)

    def test_missing_contrib_file_exits(
        self, mock_input: MagicMock, mock_root: MagicMock,
        state_and_base: tuple[Path, Path], tmp_path: Path, monkeypatch,
    ) -> None:
        empty_root = tmp_path / "empty-project"
        (empty_root / "contrib").mkdir(parents=True)
        mock_root.return_value = empty_root
        state_dir, base_dir = state_and_base
        monkeypatch.setenv("OPENAI_API_KEY", "already-set")
        mock_input.side_effect = [
            "y", "n", "OPENAI_API_KEY", "https://api.openai.com/v1",
        ]

        with pytest.raises(SystemExit):
            run_setup_voice(state_dir, base_dir=base_dir)
