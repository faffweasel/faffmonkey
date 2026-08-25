import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from faffmonkey.cli.setup_search import run_setup_search


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    contrib = root / "contrib"
    contrib.mkdir()
    (contrib / "search_provider_brave.py").write_text("# brave stub")
    return root


@pytest.fixture
def state_and_base(tmp_path: Path) -> tuple[Path, Path]:
    base_dir = tmp_path / "base"
    base_dir.mkdir()
    state_dir = base_dir / "state"
    state_dir.mkdir()
    (state_dir / "config.json").write_text(json.dumps({"timezone": "UTC"}))
    return state_dir, base_dir


class TestExtensionsDirUsesBaseDir:
    @patch("faffmonkey.cli.setup_provider._find_project_root")
    @patch("faffmonkey.cli.setup_search._read_input")
    def test_extensions_dir_derived_from_base_dir(
        self,
        mock_input: MagicMock,
        mock_root: MagicMock,
        project_root: Path,
        tmp_path: Path,
    ) -> None:
        mock_root.return_value = project_root

        base_dir = tmp_path / "deploy"
        base_dir.mkdir()
        state_dir = base_dir / "state"
        state_dir.mkdir()
        (state_dir / "config.json").write_text(json.dumps({"timezone": "UTC"}))

        mock_input.side_effect = ["1", "brave-api-key-123"]
        os.environ.pop("BRAVE_API_KEY", None)

        run_setup_search(state_dir, base_dir=base_dir)

        ext_dir = base_dir / "extensions"
        assert ext_dir.is_dir()
        assert (ext_dir / "search_provider_brave.py").exists()
        assert not (project_root / "extensions").exists()

    @patch("faffmonkey.cli.setup_provider._find_project_root")
    @patch("faffmonkey.cli.setup_search._read_input")
    def test_default_base_dir_is_state_parent(
        self,
        mock_input: MagicMock,
        mock_root: MagicMock,
        project_root: Path,
        state_and_base: tuple[Path, Path],
    ) -> None:
        state_dir, base_dir = state_and_base
        mock_root.return_value = project_root
        mock_input.side_effect = ["1", "brave-api-key-123"]
        os.environ.pop("BRAVE_API_KEY", None)

        run_setup_search(state_dir)

        ext_dir = base_dir / "extensions"
        assert ext_dir.is_dir()
        assert (ext_dir / "search_provider_brave.py").exists()


