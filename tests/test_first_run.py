import json


from faffmonkey.cli.__main__ import _check_config_exists, _check_provider_configured, _require_config


class TestFirstRunDetection:
    def test_no_config_file(self, tmp_path, capsys):
        assert _check_config_exists(tmp_path) is False
        out = capsys.readouterr().out
        assert "faff init" in out

    def test_config_exists(self, tmp_path):
        (tmp_path / "config.json").write_text("{}")
        assert _check_config_exists(tmp_path) is True

    def test_no_models_in_config(self, tmp_path, capsys):
        (tmp_path / "config.json").write_text(json.dumps({"timezone": "UTC"}))
        assert _check_provider_configured(tmp_path) is False
        out = capsys.readouterr().out
        assert "faff setup provider" in out

    def test_empty_models(self, tmp_path, capsys):
        (tmp_path / "config.json").write_text(json.dumps({"models": {}}))
        assert _check_provider_configured(tmp_path) is False
        out = capsys.readouterr().out
        assert "faff setup provider" in out

    def test_main_model_no_provider(self, tmp_path, capsys):
        config = {"models": {"main": {"model": "llama3"}}}
        (tmp_path / "config.json").write_text(json.dumps(config))
        assert _check_provider_configured(tmp_path) is False

    def test_main_model_no_model_name(self, tmp_path, capsys):
        config = {"models": {"main": {"provider": "ollama-local"}}}
        (tmp_path / "config.json").write_text(json.dumps(config))
        assert _check_provider_configured(tmp_path) is False

    def test_valid_config_passes(self, tmp_path):
        config = {
            "models": {
                "main": {"provider": "ollama-local", "model": "llama3"}
            }
        }
        (tmp_path / "config.json").write_text(json.dumps(config))
        assert _check_provider_configured(tmp_path) is True

    def test_require_config_no_config(self, tmp_path, capsys):
        assert _require_config(tmp_path) is False
        out = capsys.readouterr().out
        assert "faff init" in out

    def test_require_config_no_provider(self, tmp_path, capsys):
        (tmp_path / "config.json").write_text(json.dumps({"models": {}}))
        assert _require_config(tmp_path) is False
        out = capsys.readouterr().out
        assert "faff setup provider" in out

    def test_require_config_valid(self, tmp_path):
        config = {
            "models": {
                "main": {"provider": "ollama-local", "model": "llama3"}
            }
        }
        (tmp_path / "config.json").write_text(json.dumps(config))
        assert _require_config(tmp_path) is True

    def test_corrupt_json(self, tmp_path, capsys):
        (tmp_path / "config.json").write_text("not json at all")
        assert _check_provider_configured(tmp_path) is False
        out = capsys.readouterr().out
        assert "faff init" in out.lower() or "invalid" in out.lower()


class TestStatusOnAFreshInit:
    """P7-H1/D20: faff status crashed before the user had done anything."""

    def test_status_refuses_cleanly_with_no_provider(self, tmp_path, capsys):
        import argparse
        import pytest as _pytest
        from faffmonkey.cli.__main__ import cmd_status

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "config.json").write_text(json.dumps({
            "timezone": "UTC", "models": {},
        }))

        args = argparse.Namespace(
            state_dir=str(state_dir), workspace_dir=str(tmp_path / "workspace"),
        )
        with _pytest.raises(SystemExit) as exc:
            cmd_status(args)

        assert exc.value.code == 1
        assert "faff setup provider" in capsys.readouterr().out


class TestTopLevelErrorHandler:
    """D20: a ConfigError reached the user as a nine-frame traceback."""

    def test_config_error_becomes_a_message_and_exit_1(self, capsys):
        import pytest as _pytest
        from unittest.mock import patch
        from faffmonkey.config import ConfigError
        from faffmonkey.cli import __main__ as cli

        def boom(args):
            raise ConfigError("missing 'models' in config")

        with patch.dict(cli.COMMANDS, {"status": boom}), \
             patch("sys.argv", ["faff", "status"]):
            with _pytest.raises(SystemExit) as exc:
                cli.main()

        assert exc.value.code == 1
        assert "missing 'models' in config" in capsys.readouterr().err

    def test_debug_flag_lets_the_traceback_through(self):
        import pytest as _pytest
        from unittest.mock import patch
        from faffmonkey.config import ConfigError
        from faffmonkey.cli import __main__ as cli

        def boom(args):
            raise ConfigError("boom")

        with patch.dict(cli.COMMANDS, {"status": boom}), \
             patch("sys.argv", ["faff", "--debug", "status"]):
            with _pytest.raises(ConfigError):
                cli.main()
