"""Tests for CLI data-root resolution (FAFF_HOME)."""

import argparse
from pathlib import Path

from faffmonkey.cli.__main__ import (
    _base_dir_arg,
    _state_dir_arg,
    _workspace_dir_arg,
    build_parser,
)
from faffmonkey.config import apply_compose_env, data_root


class TestDataRoot:
    """2026-08-24: data lived in the checkout by default and a deploy rsync
    deleted it. The default data root now lives outside the checkout."""

    def test_env_override_wins(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FAFF_HOME", str(tmp_path / "custom"))
        assert data_root() == (tmp_path / "custom").resolve()

    def test_defaults_to_dot_faffmonkey_in_home(self, tmp_path, monkeypatch):
        monkeypatch.delenv("FAFF_HOME", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        assert data_root() == (tmp_path / ".faffmonkey").resolve()

    def test_empty_env_falls_back_to_default(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FAFF_HOME", "")
        monkeypatch.setenv("HOME", str(tmp_path))
        assert data_root() == (tmp_path / ".faffmonkey").resolve()


class TestComposeEnv:
    """2026-08-27: a second agent's checkout had FAFF_HOME in its compose
    .env, which only compose read. Its telegram wizard wrote the token
    and the daily jobs into the first agent's data root."""

    def test_reads_faff_home_from_checkout_env(self, tmp_path, monkeypatch):
        monkeypatch.delenv("FAFF_HOME", raising=False)
        (tmp_path / ".env").write_text(
            "FAFF_UID=1000\n# comment\nFAFF_HOME=/srv/joy\n"
        )
        apply_compose_env(tmp_path)
        assert data_root() == Path("/srv/joy").resolve()

    def test_shell_wins_over_env_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FAFF_HOME", str(tmp_path / "shell"))
        (tmp_path / ".env").write_text("FAFF_HOME=/srv/joy\n")
        apply_compose_env(tmp_path)
        assert data_root() == (tmp_path / "shell").resolve()

    def test_relative_path_resolves_against_checkout_like_compose(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.delenv("FAFF_HOME", raising=False)
        (tmp_path / ".env").write_text("FAFF_HOME=.faffmonkey-joy\n")
        apply_compose_env(tmp_path)
        assert data_root() == (tmp_path / ".faffmonkey-joy").resolve()

    def test_tilde_and_quotes(self, tmp_path, monkeypatch):
        monkeypatch.delenv("FAFF_HOME", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".env").write_text('FAFF_HOME="~/.faffmonkey-joy"\n')
        apply_compose_env(tmp_path)
        assert data_root() == (tmp_path / ".faffmonkey-joy").resolve()

    def test_missing_file_or_key_keeps_default(self, tmp_path, monkeypatch):
        monkeypatch.delenv("FAFF_HOME", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        apply_compose_env(tmp_path)
        assert data_root() == (tmp_path / ".faffmonkey").resolve()
        (tmp_path / ".env").write_text("FAFF_UID=1000\nFAFF_HOME=\n")
        apply_compose_env(tmp_path)
        assert data_root() == (tmp_path / ".faffmonkey").resolve()


class TestArgResolution:
    def test_explicit_values_win(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FAFF_HOME", str(tmp_path / "ignored"))
        assert _state_dir_arg(str(tmp_path / "s")) == (tmp_path / "s").resolve()
        assert _workspace_dir_arg(str(tmp_path / "w")) == (tmp_path / "w").resolve()
        assert _base_dir_arg(str(tmp_path / "b")) == (tmp_path / "b").resolve()

    def test_none_resolves_under_data_root(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FAFF_HOME", str(tmp_path))
        assert _state_dir_arg(None) == tmp_path.resolve() / "state"
        assert _workspace_dir_arg(None) == tmp_path.resolve() / "workspace"
        assert _base_dir_arg(None) == tmp_path.resolve()

    def test_parser_defaults_are_unset(self):
        """The parser must not bake in cwd-relative paths; resolution
        happens at command time so $FAFF_HOME applies."""
        parser = build_parser()
        args = parser.parse_args(["chat"])
        assert args.state_dir is None
        assert args.workspace_dir is None
        args = parser.parse_args(["update"])
        assert args.base_dir is None
        args = parser.parse_args(["init"])
        assert args.path is None
