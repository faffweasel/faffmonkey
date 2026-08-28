import json
import os
import stat
import urllib.error
import urllib.request
from unittest.mock import patch

import pytest

from unittest.mock import MagicMock

from faffmonkey.cli.setup_provider import (
    _append_env_var,
    _load_providers,
    _safe_url,
    _test_connection,
    _update_config_models,
    _validate_env_value,
    detect_context_window,
    run_setup_provider,
)


class _FakeResp:
    def __init__(self, data: bytes):
        self._data = data

    def read(self) -> bytes:
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


def _fake_opener(routes: dict[str, object]) -> MagicMock:
    """An opener answering each URL suffix in routes with that JSON body
    and 404 for anything else, recording every URL it was asked for."""
    opener = MagicMock()
    opener.urls = []

    def open_(req, timeout=None):
        opener.urls.append(req.full_url)
        for suffix, body in routes.items():
            if req.full_url.endswith(suffix):
                return _FakeResp(json.dumps(body).encode())
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)

    opener.open.side_effect = open_
    return opener


@pytest.fixture
def provider_dir(tmp_path):
    d = tmp_path / "providers"
    d.mkdir()
    (d / "ollama-local.json").write_text(json.dumps({
        "name": "Ollama (local)",
        "provider_key": "ollama-local",
        "base_url": "http://localhost:11434/v1",
        "api_key_env": "",
        "default_model": "llama3",
        "notes": "Local Ollama instance.",
    }))
    (d / "openrouter.json").write_text(json.dumps({
        "name": "OpenRouter",
        "provider_key": "openrouter",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key_env": "OPENROUTER_API_KEY",
        "default_model": "google/gemini-2.5-flash",
        "notes": "Get your API key at openrouter.ai/keys",
    }))
    return d


class TestAppendEnvVar:
    def test_append_to_empty_file(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("")
        _append_env_var(env, "MY_KEY", "my_value")
        assert "MY_KEY=my_value" in env.read_text()

    def test_append_to_nonexistent_file(self, tmp_path):
        env = tmp_path / ".env"
        _append_env_var(env, "MY_KEY", "my_value")
        assert "MY_KEY=my_value" in env.read_text()

    def test_update_existing_commented_key(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("# MY_KEY=old_placeholder\nOTHER=val\n")
        _append_env_var(env, "MY_KEY", "new_value")
        content = env.read_text()
        assert "MY_KEY=new_value" in content
        assert "OTHER=val" in content
        assert "# MY_KEY" not in content

    def test_update_existing_active_key(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("MY_KEY=old_value\n")
        _append_env_var(env, "MY_KEY", "new_value")
        content = env.read_text()
        assert "MY_KEY=new_value" in content
        assert "old_value" not in content

    def test_does_not_duplicate(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("OTHER=x\n")
        _append_env_var(env, "MY_KEY", "val1")
        _append_env_var(env, "MY_KEY", "val2")
        content = env.read_text()
        assert content.count("MY_KEY=") == 1
        assert "MY_KEY=val2" in content

    def test_removes_all_duplicate_keys(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("MY_KEY=first\nOTHER=x\nMY_KEY=second\n")
        _append_env_var(env, "MY_KEY", "new_value")
        content = env.read_text()
        assert content.count("MY_KEY=") == 1
        assert "MY_KEY=new_value" in content
        assert "OTHER=x" in content

    def test_removes_commented_key_when_setting(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("# MY_KEY=placeholder\nOTHER=x\n")
        _append_env_var(env, "MY_KEY", "real_value")
        content = env.read_text()
        assert "# MY_KEY" not in content
        assert "MY_KEY=real_value" in content
        assert "OTHER=x" in content


class TestLoadProviders:
    def test_loads_from_json_files(self, provider_dir):
        providers = _load_providers(provider_dir)
        names = [p["name"] for p in providers]
        assert "Ollama (local)" in names
        assert "OpenRouter" in names

    def test_sorted_by_filename(self, provider_dir):
        providers = _load_providers(provider_dir)
        assert providers[0]["provider_key"] == "ollama-local"
        assert providers[1]["provider_key"] == "openrouter"

    def test_custom_option_always_last(self, provider_dir):
        providers = _load_providers(provider_dir)
        assert providers[-1]["provider_key"] == "custom"
        assert providers[-1]["name"] == "Custom OpenAI-compatible"

    def test_empty_directory(self, tmp_path):
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        providers = _load_providers(empty_dir)
        assert len(providers) == 1
        assert providers[0]["provider_key"] == "custom"

    def test_missing_directory(self, tmp_path):
        providers = _load_providers(tmp_path / "nonexistent")
        assert len(providers) == 1
        assert providers[0]["provider_key"] == "custom"

    def test_no_hardcoded_provider_list_in_module(self):
        import inspect
        import faffmonkey.cli.setup_provider as mod
        source = inspect.getsource(mod)
        assert "PROVIDERS = [" not in source
        assert "openrouter.ai" not in source
        assert "venice.ai" not in source
        assert "ollama.com" not in source




class TestDetectContextWindow:
    """Shapes come from the providers' own responses, not from the parser."""

    def test_openrouter_model_list(self):
        opener = _fake_opener({"/models": {"data": [{
            "id": "moonshotai/kimi-k3",
            "context_length": 1048576,
            "top_provider": {"context_length": 1048576, "max_completion_tokens": 943718},
        }]}})
        with patch("faffmonkey.cli.setup_provider._no_redirect_opener", opener):
            assert detect_context_window(
                "https://openrouter.ai/api/v1", "key", "moonshotai/kimi-k3",
            ) == 1048576
        assert opener.urls == ["https://openrouter.ai/api/v1/models"]

    def test_venice_model_spec(self):
        opener = _fake_opener({"/models": {"data": [{
            "id": "qwen-3-8-27b",
            "model_spec": {"availableContextTokens": 131072},
        }], "object": "list", "type": "text"}})
        with patch("faffmonkey.cli.setup_provider._no_redirect_opener", opener):
            assert detect_context_window(
                "https://api.venice.ai/api/v1", "key", "qwen-3-8-27b",
            ) == 131072

    def test_ollama_show_when_the_openai_list_has_no_length(self):
        opener = _fake_opener({
            "/v1/models": {"data": [{"id": "kimi-k3:cloud", "object": "model"}]},
            "/api/ps": {"models": []},
            "/api/show": {
                "model_info": {
                    "general.architecture": "kimi-k3",
                    "kimi-k3.context_length": 1048576,
                },
                "parameters": "",
            },
        })
        with patch("faffmonkey.cli.setup_provider._no_redirect_opener", opener):
            assert detect_context_window(
                "https://ollama.example/v1", "key", "kimi-k3:cloud",
            ) == 1048576
        # The native API sits above the /v1 prefix.
        assert "https://ollama.example/api/show" in opener.urls

    def test_ollama_prefers_the_window_a_loaded_model_runs_with(self):
        opener = _fake_opener({
            "/v1/models": {"data": [{"id": "llama3:latest"}]},
            "/api/ps": {"models": [{
                "name": "llama3:latest", "model": "llama3:latest",
                "context_length": 4096,
            }]},
            "/api/show": {"model_info": {"llama.context_length": 131072}},
        })
        with patch("faffmonkey.cli.setup_provider._no_redirect_opener", opener):
            assert detect_context_window(
                "http://localhost:11434/v1", "", "llama3:latest",
            ) == 4096

    def test_ollama_num_ctx_caps_the_trained_length(self):
        opener = _fake_opener({
            "/v1/models": {"data": []},
            "/api/ps": {"models": []},
            "/api/show": {
                "model_info": {"llama.context_length": 131072},
                "parameters": "num_ctx                        8192\nstop    \"<|eot_id|>\"",
            },
        })
        with patch("faffmonkey.cli.setup_provider._no_redirect_opener", opener):
            assert detect_context_window(
                "http://localhost:11434/v1", "", "llama3:latest",
            ) == 8192

    def test_none_when_no_endpoint_reports_it(self):
        opener = _fake_opener({"/models": {"data": [{"id": "some-model"}]}})
        with patch("faffmonkey.cli.setup_provider._no_redirect_opener", opener):
            assert detect_context_window("https://api.example/v1", "key", "some-model") is None

    def test_none_when_the_provider_is_down(self):
        opener = MagicMock()
        opener.open.side_effect = urllib.error.URLError("refused")
        with patch("faffmonkey.cli.setup_provider._no_redirect_opener", opener):
            assert detect_context_window("http://localhost:1/v1", "", "m") is None

    def test_rejects_values_that_are_not_a_positive_count(self):
        opener = _fake_opener({"/models": {"data": [
            {"id": "a", "context_length": True},
            {"id": "b", "context_length": 0},
            {"id": "c", "context_length": "128000"},
        ]}})
        with patch("faffmonkey.cli.setup_provider._no_redirect_opener", opener):
            for model in ("a", "b", "c"):
                assert detect_context_window("https://api.example/v1", "key", model) is None


class TestWizardWritesContextWindow:
    """Every slot the wizard writes carries an explicit context_window."""

    def _state(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "config.json").write_text(json.dumps({"timezone": "UTC"}))
        (state_dir / ".env").write_text("")
        return state_dir

    def test_reported_window_is_written_without_asking(self, tmp_path, provider_dir, monkeypatch):
        state_dir = self._state(tmp_path)
        monkeypatch.setenv("OPENROUTER_API_KEY", "k")
        inputs = iter([
            "2",   # OpenRouter
            "",    # default model
            "y",   # reuse for cheap/vision
        ])
        monkeypatch.setattr("builtins.input", lambda prompt: next(inputs))
        with patch(
            "faffmonkey.cli.setup_provider._test_connection", return_value=True
        ), patch(
            "faffmonkey.cli.setup_provider.detect_context_window", return_value=1048576
        ):
            run_setup_provider(state_dir, provider_dir=provider_dir)

        models = json.loads((state_dir / "config.json").read_text())["models"]
        for slot in ("main", "cheap", "vision"):
            assert models[slot]["context_window"] == 1048576

    def test_asks_when_the_provider_does_not_report_one(self, tmp_path, provider_dir, monkeypatch, capsys):
        state_dir = self._state(tmp_path)
        monkeypatch.setenv("OPENROUTER_API_KEY", "k")
        inputs = iter([
            "2",       # OpenRouter
            "",        # default model
            "y",       # reuse for cheap/vision
            "lots",    # not a number
            "32000",   # context window
        ])
        monkeypatch.setattr("builtins.input", lambda prompt: next(inputs))
        with patch(
            "faffmonkey.cli.setup_provider._test_connection", return_value=True
        ), patch(
            "faffmonkey.cli.setup_provider.detect_context_window", return_value=None
        ):
            run_setup_provider(state_dir, provider_dir=provider_dir)

        models = json.loads((state_dir / "config.json").read_text())["models"]
        for slot in ("main", "cheap", "vision"):
            assert models[slot]["context_window"] == 32000
        assert "positive whole number" in capsys.readouterr().out

    def test_each_distinct_model_gets_its_own_window(self, tmp_path, provider_dir, monkeypatch):
        state_dir = self._state(tmp_path)
        monkeypatch.setenv("OPENROUTER_API_KEY", "k")
        inputs = iter([
            "2",           # OpenRouter
            "big-model",   # main
            "n",           # different cheap/vision
            "small-model", # cheap
            "big-model",   # vision
        ])
        monkeypatch.setattr("builtins.input", lambda prompt: next(inputs))
        windows = {"big-model": 1000000, "small-model": 32000}
        with patch(
            "faffmonkey.cli.setup_provider._test_connection", return_value=True
        ), patch(
            "faffmonkey.cli.setup_provider.detect_context_window",
            side_effect=lambda base_url, api_key, model: windows[model],
        ):
            run_setup_provider(state_dir, provider_dir=provider_dir)

        models = json.loads((state_dir / "config.json").read_text())["models"]
        assert models["main"]["context_window"] == 1000000
        assert models["cheap"]["context_window"] == 32000
        assert models["vision"]["context_window"] == 1000000


class TestOllamaModelListing:
    def test_lists_models_when_ollama_running(self, tmp_path, provider_dir, monkeypatch):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "config.json").write_text(json.dumps({"timezone": "UTC"}))
        (state_dir / ".env").write_text("")

        tags_response = json.dumps({
            "models": [
                {"name": "llama3:latest"},
                {"name": "phi3:latest"},
                {"name": "llava:latest"},
            ]
        }).encode()

        inputs = iter([
            "1",    # Ollama (local)
            "2",    # pick phi3:latest from model list
            "y",    # reuse for cheap/vision
        ])
        monkeypatch.setattr("builtins.input", lambda prompt: next(inputs))

        def fake_urlopen(req, timeout=None):

            class FakeResp:
                def __init__(self, data):
                    self._data = data

                def read(self):
                    return self._data

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    pass

            return FakeResp(tags_response)

        with patch(
            "faffmonkey.cli.setup_provider._test_connection", return_value=True
        ), patch(
            "faffmonkey.cli.setup_provider.urllib.request.urlopen", side_effect=fake_urlopen
        ), patch(
            "faffmonkey.cli.setup_provider.detect_context_window", return_value=8192
        ):
            run_setup_provider(state_dir, provider_dir=provider_dir)

        config = json.loads((state_dir / "config.json").read_text())
        assert config["models"]["main"]["model"] == "phi3:latest"

    def test_fallback_when_ollama_not_running(self, tmp_path, provider_dir, monkeypatch):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "config.json").write_text(json.dumps({"timezone": "UTC"}))
        (state_dir / ".env").write_text("")

        inputs = iter([
            "1",       # Ollama (local)
            "llama3",  # manual model name
            "y",       # reuse
        ])
        monkeypatch.setattr("builtins.input", lambda prompt: next(inputs))

        import urllib.error

        def fail_urlopen(req, timeout=None):
            raise urllib.error.URLError("Connection refused")

        with patch(
            "faffmonkey.cli.setup_provider._test_connection", return_value=True
        ), patch(
            "faffmonkey.cli.setup_provider.urllib.request.urlopen", side_effect=fail_urlopen
        ), patch(
            "faffmonkey.cli.setup_provider.detect_context_window", return_value=8192
        ):
            run_setup_provider(state_dir, provider_dir=provider_dir)

        config = json.loads((state_dir / "config.json").read_text())
        assert config["models"]["main"]["model"] == "llama3"
        assert config["models"]["main"]["provider"] == "ollama-local"




class TestBaseUrlValidation:
    def test_rejects_http_remote_url(self, tmp_path, provider_dir, monkeypatch, capsys):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "config.json").write_text(json.dumps({"timezone": "UTC"}))
        (state_dir / ".env").write_text("")

        inputs = iter([
            "3",                            # Custom
            "http://evil.example.com/v1",   # non-HTTPS remote base URL
            "",                             # no API key env
            "custom",                       # provider name
            "some-model",                   # model name
        ])
        monkeypatch.setattr("builtins.input", lambda prompt: next(inputs))

        with pytest.raises(SystemExit):
            run_setup_provider(state_dir, provider_dir=provider_dir)

        out = capsys.readouterr().out
        assert "Invalid base URL" in out


class TestEnvValueValidation:
    def test_rejects_newline(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("")
        with pytest.raises(ValueError, match="non-printable"):
            _append_env_var(env, "KEY", "bad\nvalue")

    def test_rejects_carriage_return(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("")
        with pytest.raises(ValueError, match="non-printable"):
            _append_env_var(env, "KEY", "bad\rvalue")

    def test_rejects_null_byte(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("")
        with pytest.raises(ValueError, match="non-printable"):
            _append_env_var(env, "KEY", "bad\x00value")

    def test_allows_tab(self):
        _validate_env_value("K", "value\twith\ttabs")

    def test_allows_normal_value(self):
        _validate_env_value("K", "sk-or-v1-abc123")

    def test_rejects_equals_in_value(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("")
        with pytest.raises(ValueError, match="invalid characters"):
            _append_env_var(env, "KEY", "foo=bar")

    def test_strips_trailing_whitespace(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("")
        _append_env_var(env, "KEY", "  value  ")
        assert "KEY=value\n" in env.read_text()


class TestEnvFilePermissions:
    def test_env_mode_0600_after_append(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("")
        _append_env_var(env, "KEY", "value")
        mode = stat.S_IMODE(os.stat(env).st_mode)
        assert mode == 0o600

    def test_env_mode_0600_after_update(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("KEY=old\n")
        _append_env_var(env, "KEY", "new")
        mode = stat.S_IMODE(os.stat(env).st_mode)
        assert mode == 0o600


class TestSymlinkRefusal:
    def test_refuses_symlinked_env(self, tmp_path):
        real = tmp_path / "real.env"
        real.write_text("")
        link = tmp_path / ".env"
        link.symlink_to(real)
        with pytest.raises(ValueError, match="symlink"):
            _append_env_var(link, "KEY", "value")


class TestSafeUrl:
    def test_strips_user_and_password(self):
        assert _safe_url("http://user:pass@host.com:8080/v1") == "http://host.com:8080/v1"

    def test_no_credentials_unchanged(self):
        assert _safe_url("https://api.example.com/v1") == "https://api.example.com/v1"


class TestProviderResponseTruncated:
    def test_long_response_truncated(self, capsys):
        long_text = "x" * 3000
        body = json.dumps({"choices": [{"message": {"content": long_text}}]}).encode()

        mock_resp = MagicMock()
        mock_resp.read.return_value = body
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("faffmonkey.cli.setup_provider._no_redirect_opener.open", return_value=mock_resp):
            result = _test_connection("http://localhost/v1", "", "model")

        assert result is True
        out = capsys.readouterr().out
        assert "x" * 1025 not in out
        assert "x" * 100 in out

    def test_response_with_secret_redacted(self, capsys):
        text = "Hello sk-aaaaaaaaaaaaaaaaaaaaaaaaaaaa world"
        body = json.dumps({"choices": [{"message": {"content": text}}]}).encode()

        mock_resp = MagicMock()
        mock_resp.read.return_value = body
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("faffmonkey.cli.setup_provider._no_redirect_opener.open", return_value=mock_resp):
            _test_connection("http://localhost/v1", "", "model")

        out = capsys.readouterr().out
        assert "sk-aaaaaa" not in out
        assert "[REDACTED]" in out


class TestNoRedirectOnTestConnection:
    def test_redirect_does_not_follow(self, capsys):
        error = urllib.error.HTTPError(
            "http://evil.example.com/steal",
            302,
            "Found",
            {"Location": "http://evil.example.com/steal"},
            None,
        )

        with patch(
            "faffmonkey.cli.setup_provider._no_redirect_opener.open",
            side_effect=error,
        ):
            result = _test_connection(
                "http://localhost:8080/v1", "sk-secret-key", "model",
            )

        assert result is False
        out = capsys.readouterr().out
        assert "302" in out

    def test_uses_no_redirect_opener(self):
        body = json.dumps({
            "choices": [{"message": {"content": "hello"}}],
        }).encode()

        mock_resp = MagicMock()
        mock_resp.read.return_value = body
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch(
            "faffmonkey.cli.setup_provider._no_redirect_opener.open",
            return_value=mock_resp,
        ) as mock_open:
            result = _test_connection(
                "http://localhost:8080/v1", "sk-key", "model",
            )

        assert result is True
        mock_open.assert_called_once()
        req = mock_open.call_args[0][0]
        assert req.get_header("Authorization") == "Bearer sk-key"

    def test_no_redirect_handler_returns_none(self):
        from faffmonkey.cli.setup_provider import _NoRedirectHandler
        handler = _NoRedirectHandler()
        result = handler.redirect_request(
            urllib.request.Request("http://a.com"),
            None, 302, "Found", {}, "http://evil.com/steal",
        )
        assert result is None


class TestTmpFilePermissions:
    def test_tmp_file_created_with_0600(self, tmp_path):
        env = tmp_path / ".env"
        _append_env_var(env, "SECRET_KEY", "s3cret")
        mode = stat.S_IMODE(os.stat(env).st_mode)
        assert mode == 0o600

    def test_tmp_not_world_readable_during_write(self, tmp_path):
        env = tmp_path / ".env"
        observed_modes: list[int] = []
        original_replace = os.replace

        def spy_replace(src, dst):
            observed_modes.append(stat.S_IMODE(os.stat(src).st_mode))
            return original_replace(src, dst)

        with patch("faffmonkey.cli.setup_provider.os.replace", side_effect=spy_replace):
            _append_env_var(env, "KEY", "value")

        assert observed_modes
        for mode in observed_modes:
            assert mode & 0o077 == 0


class TestEnsureDefaultJobs:
    """Fresh installs had heartbeat enabled in config and no job to run it,
    so doctor went yellow and nothing ever fired. Then they had a heartbeat
    and nothing else: no morning greeting, no evening memory flush, and the
    daily log never got written, because the jobs the design assumes were
    examples in a SKILL.md rather than anything a wizard created."""

    def test_creates_the_daily_skeleton_the_scheduler_accepts(self, tmp_path, capsys):
        from faffmonkey.cli.setup_provider import ensure_default_jobs
        from faffmonkey.runtime.scheduler import LAST_CHANNEL, load_jobs

        workspace = tmp_path / "workspace"
        (workspace / "config").mkdir(parents=True)
        (workspace / "config" / "jobs.json").write_text("[]\n")

        ensure_default_jobs(workspace)

        jobs = {j.id: j for j in load_jobs(workspace)}
        assert set(jobs) == {"heartbeat", "morning", "evening", "preconscious-decay"}
        assert jobs["heartbeat"].context == "heartbeat"
        assert jobs["heartbeat"].deliver_channel == LAST_CHANNEL
        assert jobs["morning"].session == "agent"
        assert jobs["morning"].deliver_channel == LAST_CHANNEL
        assert jobs["evening"].session == "main"
        assert jobs["evening"].rotate_session is True
        assert jobs["evening"].deliver_mode == "none"
        assert "heartbeat, morning, evening, preconscious-decay" in capsys.readouterr().out

    def test_never_adds_a_second_one(self, tmp_path):
        from faffmonkey.cli.setup_provider import ensure_default_jobs

        workspace = tmp_path / "workspace"
        ensure_default_jobs(workspace)
        ensure_default_jobs(workspace)

        jobs = json.loads((workspace / "config" / "jobs.json").read_text())
        assert [j["id"] for j in jobs] == ["heartbeat", "morning", "evening", "preconscious-decay"]

    def test_operator_jobs_are_left_alone_and_the_rest_filled_in(self, tmp_path, capsys):
        from faffmonkey.cli.setup_provider import ensure_default_jobs

        workspace = tmp_path / "workspace"
        (workspace / "config").mkdir(parents=True)
        mine = [
            {"id": "my-hb", "schedule": "*/30 * * * *", "prompt": "x",
             "context": "heartbeat", "deliver": {"mode": "announce", "channel": "discord"}},
            {"id": "evening", "schedule": "0 21 * * *", "prompt": "mine", "session": "main",
             "deliver": {"mode": "none"}, "rotate_session": True},
        ]
        (workspace / "config" / "jobs.json").write_text(json.dumps(mine))

        ensure_default_jobs(workspace)

        jobs = json.loads((workspace / "config" / "jobs.json").read_text())
        assert jobs[:2] == mine
        assert [j["id"] for j in jobs[2:]] == ["morning", "preconscious-decay"]
        out = capsys.readouterr().out
        assert "'my-hb' already present" in out and "'evening' already present" in out

    def test_unreadable_jobs_file_is_not_clobbered(self, tmp_path, capsys):
        from faffmonkey.cli.setup_provider import ensure_default_jobs

        workspace = tmp_path / "workspace"
        (workspace / "config").mkdir(parents=True)
        (workspace / "config" / "jobs.json").write_text("[{not json")

        ensure_default_jobs(workspace)

        assert (workspace / "config" / "jobs.json").read_text() == "[{not json"
        assert "Not adding default jobs" in capsys.readouterr().out
