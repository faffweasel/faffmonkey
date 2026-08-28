import json

import pytest

from faffmonkey.config import ConfigError, load_config, validate_config_schema

MAIN_MODEL = {
    "provider": "ollama-local", "model": "llama3",
    "base_url": "http://localhost:11434/v1",
}


def _write_config(tmp_path, **extra):
    """Write config.json with a minimal valid models block, plus extra keys.

    Pass models=... to replace the default main-model block outright.
    """
    data = {"models": {"main": dict(MAIN_MODEL)}}
    data.update(extra)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data))
    return path


@pytest.fixture
def valid_config_data():
    return {
        "models": {
            "main": {
                "provider": "ollama-local", "model": "llama3",
                "base_url": "http://localhost:11434/v1",
            },
            "cheap": {
                "provider": "ollama-local", "model": "llama3",
                "base_url": "http://localhost:11434/v1",
            },
            "vision": {
                "provider": "ollama-local", "model": "llava",
                "base_url": "http://localhost:11434/v1",
            },
        },
        "routing": {
            "conversation": "main",
            "compaction": "cheap",
            "heartbeat": "cheap",
            "cron_default": "main",
            "image_understanding": "vision",
        },
        "timezone": "UTC",
    }


@pytest.fixture
def config_file(tmp_path, valid_config_data):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(valid_config_data))
    return path


def test_load_valid_config(config_file):
    config = load_config(config_file)
    assert config.models["main"].provider == "ollama-local"
    assert config.models["main"].model == "llama3"
    assert config.models["main"].base_url == "http://localhost:11434/v1"
    assert config.models["main"].api_key == ""
    assert config.routing["conversation"] == "main"
    assert str(config.timezone) == "UTC"


def test_missing_config_file(tmp_path):
    with pytest.raises(ConfigError, match="config file not found"):
        load_config(tmp_path / "nonexistent.json")


def test_missing_models(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"timezone": "UTC"}))
    with pytest.raises(ConfigError, match="missing 'models'"):
        load_config(path)


def test_missing_main_model(tmp_path):
    path = _write_config(
        tmp_path,
        models={"cheap": {
            "provider": "ollama-local", "model": "llama3",
            "base_url": "http://localhost:11434/v1",
        }},
    )
    with pytest.raises(ConfigError, match="missing 'main' model slot"):
        load_config(path)


def test_missing_model_provider(tmp_path):
    path = _write_config(tmp_path, models={"main": {"model": "llama3"}})
    with pytest.raises(ConfigError, match="missing 'provider'"):
        load_config(path)


def test_missing_model_name(tmp_path):
    path = _write_config(tmp_path, models={"main": {"provider": "ollama-local"}})
    with pytest.raises(ConfigError, match="missing 'model'"):
        load_config(path)


def test_api_key_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-key-123")
    path = _write_config(
        tmp_path,
        models={
            "main": {
                "provider": "openrouter", "model": "google/gemini-2.5-flash",
                "base_url": "https://openrouter.ai/api/v1",
                "api_key_env": "OPENROUTER_API_KEY",
            },
        },
    )
    config = load_config(path)
    assert config.models["main"].api_key == "sk-test-key-123"
    assert config.models["main"].base_url == "https://openrouter.ai/api/v1"


def test_api_key_env_not_set(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    path = _write_config(
        tmp_path,
        models={
            "main": {
                "provider": "openrouter", "model": "google/gemini-2.5-flash",
                "base_url": "https://openrouter.ai/api/v1",
                "api_key_env": "OPENROUTER_API_KEY",
            },
        },
    )
    with pytest.raises(ConfigError, match="env var.*not set"):
        load_config(path)


def test_custom_api_key_env(tmp_path, monkeypatch):
    monkeypatch.setenv("PROVIDER_API_KEY", "custom-key-456")
    path = _write_config(
        tmp_path,
        models={
            "main": {
                "provider": "ollama-local",
                "model": "llama3",
                "base_url": "http://localhost:11434/v1",
                "api_key_env": "PROVIDER_API_KEY",
            },
        },
    )
    config = load_config(path)
    assert config.models["main"].api_key == "custom-key-456"


def test_resolve_model(config_file):
    config = load_config(config_file)
    model = config.resolve_model("conversation")
    assert model.model == "llama3"
    assert model.provider == "ollama-local"


def test_resolve_model_with_override(config_file):
    config = load_config(config_file)
    model = config.resolve_model("conversation", override="vision")
    assert model.model == "llava"


def test_resolve_model_unknown_task(config_file):
    config = load_config(config_file)
    with pytest.raises(ConfigError, match="no routing"):
        config.resolve_model("unknown_task")


def test_resolve_model_unknown_slot(tmp_path):
    path = _write_config(tmp_path, routing={"conversation": "nonexistent"})
    config = load_config(path)
    with pytest.raises(ConfigError, match="no model configured for slot"):
        config.resolve_model("conversation")


def test_default_heartbeat(config_file):
    config = load_config(config_file)
    assert config.heartbeat.active_hours == (9, 22)
    assert config.heartbeat.ack_max_chars == 300
    assert config.heartbeat.enabled is True


def test_custom_heartbeat(tmp_path):
    path = _write_config(
        tmp_path,
        heartbeat={
            "active_hours": [8, 20],
            "ack_max_chars": 500,
            "enabled": False,
        },
    )
    config = load_config(path)
    assert config.heartbeat.active_hours == (8, 20)
    assert config.heartbeat.ack_max_chars == 500
    assert config.heartbeat.enabled is False


def test_default_compaction(config_file):
    config = load_config(config_file)
    assert config.compaction.threshold == 0.5
    assert config.compaction.target_ratio == 0.2
    assert config.compaction.protect_last_n == 20
    assert config.compaction.hard_message_limit == 400


def test_default_tool_permissions(config_file):
    config = load_config(config_file)
    assert config.tool_permissions["web_search"] == "always"
    assert config.tool_permissions["shell_exec"] == "ask"
    assert config.tool_permissions["skill_invoke"] == "always"


def test_custom_tool_permissions(tmp_path):
    path = _write_config(tmp_path, tools={"shell_exec": "always", "skill_invoke": "ask"})
    config = load_config(path)
    assert config.tool_permissions["shell_exec"] == "always"
    assert config.tool_permissions["skill_invoke"] == "ask"
    assert config.tool_permissions["web_search"] == "always"


def test_a_tools_block_written_before_file_list_existed_still_gets_it(tmp_path):
    """An install's config.json lists the tools it knew at init time. A
    tool added later must default on without the operator editing it."""
    path = _write_config(tmp_path, tools={
        "file_read": "always", "file_write": "always", "web_search": "always",
        "web_fetch": "always", "shell_exec": "ask", "skill_invoke": "always",
    })
    assert load_config(path).tool_permissions["file_list"] == "always"


def test_fallback_models(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setenv("VENICE_API_KEY", "vn-test")
    path = _write_config(
        tmp_path,
        fallback_models=[
            {
                "provider": "openrouter", "model": "google/gemini-2.5-flash",
                "base_url": "https://openrouter.ai/api/v1",
                "api_key_env": "OPENROUTER_API_KEY",
            },
            {
                "provider": "venice", "model": "llama-3.1-405b",
                "base_url": "https://api.venice.ai/api/v1",
                "api_key_env": "VENICE_API_KEY",
            },
        ],
    )
    config = load_config(path)
    assert len(config.fallback_models) == 2
    assert config.fallback_models[0].provider == "openrouter"
    assert config.fallback_models[1].provider == "venice"


def test_channel_config(tmp_path):
    path = _write_config(
        tmp_path,
        channels={
            "telegram": {
                "enabled": True,
                "allowed_users": ["123", "456"],
            },
        },
    )
    config = load_config(path)
    assert config.channels["telegram"].enabled is True
    assert config.channels["telegram"].allowed_users == ["123", "456"]


def test_invalid_timezone(tmp_path):
    path = _write_config(tmp_path, timezone="Not/A/Timezone")
    with pytest.raises(ConfigError, match="invalid timezone"):
        load_config(path)


def test_default_routing_applied(tmp_path):
    path = _write_config(tmp_path)
    config = load_config(path)
    assert config.routing["conversation"] == "main"
    assert config.routing["compaction"] == "cheap"
    assert config.routing["heartbeat"] == "cheap"
    assert config.routing["cron_default"] == "main"
    assert config.routing["image_understanding"] == "vision"


def test_missing_base_url_raises(tmp_path):
    path = _write_config(
        tmp_path,
        models={
            "main": {"provider": "custom_provider", "model": "my-model"},
        },
    )
    with pytest.raises(ConfigError, match="missing 'base_url'"):
        load_config(path)


def test_custom_base_url(tmp_path):
    path = _write_config(
        tmp_path,
        models={
            "main": {
                "provider": "custom",
                "model": "my-model",
                "base_url": "http://my-server:8080/v1",
                "allow_insecure": True,
            },
        },
    )
    config = load_config(path)
    assert config.models["main"].base_url == "http://my-server:8080/v1"
    assert config.models["main"].api_key == ""
    assert config.models["main"].allow_insecure is True


def test_tools_key_not_tool_permissions(tmp_path):
    path = _write_config(
        tmp_path,
        tools={
            "shell_exec": "always",
            "skill_invoke": "always",
            "web_fetch": "never",
        },
    )
    config = load_config(path)
    assert config.tool_permissions["shell_exec"] == "always"
    assert config.tool_permissions["skill_invoke"] == "always"
    assert config.tool_permissions["web_fetch"] == "never"
    assert config.tool_permissions["web_search"] == "always"
    assert config.tool_permissions["file_read"] == "always"


def test_shell_preapproved_from_tools_block(tmp_path):
    path = _write_config(
        tmp_path,
        tools={
            "shell_exec": "ask",
            "shell_preapproved": ["ls *", "cat workspace/*"],
        },
    )
    config = load_config(path)
    assert config.shell_preapproved == ["ls *", "cat workspace/*"]
    assert "shell_preapproved" not in config.tool_permissions


def test_http_remote_base_url_rejected(tmp_path):
    path = _write_config(
        tmp_path,
        models={
            "main": {
                "provider": "openai",
                "model": "gpt-4",
                "base_url": "http://api.openai.com/v1",
            },
        },
    )
    with pytest.raises(ConfigError, match="http://.*only allowed for localhost"):
        load_config(path)


def test_http_localhost_base_url_accepted(tmp_path):
    path = _write_config(
        tmp_path,
        models={
            "main": {
                "provider": "ollama-local",
                "model": "llama3",
                "base_url": "http://localhost:11434/v1",
            },
        },
    )
    config = load_config(path)
    assert config.models["main"].base_url == "http://localhost:11434/v1"


def test_http_127_base_url_accepted(tmp_path):
    path = _write_config(
        tmp_path,
        models={
            "main": {
                "provider": "ollama-local",
                "model": "llama3",
                "base_url": "http://127.0.0.1:11434/v1",
            },
        },
    )
    config = load_config(path)
    assert config.models["main"].base_url == "http://127.0.0.1:11434/v1"


def test_http_docker_internal_accepted(tmp_path):
    path = _write_config(
        tmp_path,
        models={
            "main": {
                "provider": "ollama",
                "model": "llama3",
                "base_url": "http://host.docker.internal:11434/v1",
            },
        },
    )
    config = load_config(path)
    assert config.models["main"].base_url == "http://host.docker.internal:11434/v1"


def test_http_remote_allowed_with_allow_insecure(tmp_path):
    path = _write_config(
        tmp_path,
        models={
            "main": {
                "provider": "custom",
                "model": "my-model",
                "base_url": "http://my-server:8080/v1",
                "allow_insecure": True,
            },
        },
    )
    config = load_config(path)
    assert config.models["main"].base_url == "http://my-server:8080/v1"


def test_ftp_base_url_rejected(tmp_path):
    path = _write_config(
        tmp_path,
        models={
            "main": {
                "provider": "custom",
                "model": "my-model",
                "base_url": "ftp://files.example.com/v1",
            },
        },
    )
    with pytest.raises(ConfigError, match="must use https:// or http://"):
        load_config(path)


def test_api_key_env_ld_preload_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("LD_PRELOAD", "/tmp/evil.so")
    path = _write_config(
        tmp_path,
        models={
            "main": {
                "provider": "custom",
                "model": "my-model",
                "base_url": "http://localhost:1234/v1",
                "api_key_env": "LD_PRELOAD",
            },
        },
    )
    with pytest.raises(ConfigError, match="api_key_env.*must match"):
        load_config(path)


def test_api_key_env_valid_patterns_accepted(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    path = _write_config(
        tmp_path,
        models={
            "main": {
                "provider": "openrouter",
                "model": "llama3",
                "base_url": "https://openrouter.ai/api/v1",
                "api_key_env": "OPENROUTER_API_KEY",
            },
        },
    )
    config = load_config(path)
    assert config.models["main"].api_key == "sk-test"


def test_api_key_env_token_suffix_accepted(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_TOKEN", "ant-test")
    path = _write_config(
        tmp_path,
        models={
            "main": {
                "provider": "custom",
                "model": "model",
                "base_url": "https://api.example.com/v1",
                "api_key_env": "ANTHROPIC_TOKEN",
            },
        },
    )
    config = load_config(path)
    assert config.models["main"].api_key == "ant-test"


def test_api_key_env_secret_suffix_accepted(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_SECRET", "s3cret")
    path = _write_config(
        tmp_path,
        models={
            "main": {
                "provider": "custom",
                "model": "model",
                "base_url": "https://api.example.com/v1",
                "api_key_env": "OPENAI_SECRET",
            },
        },
    )
    config = load_config(path)
    assert config.models["main"].api_key == "s3cret"


def test_api_key_env_path_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    path = _write_config(
        tmp_path,
        models={
            "main": {
                "provider": "custom",
                "model": "my-model",
                "base_url": "http://localhost:1234/v1",
                "api_key_env": "PATH",
            },
        },
    )
    with pytest.raises(ConfigError, match="api_key_env.*must match"):
        load_config(path)


def test_malformed_active_hours_string(tmp_path):
    path = _write_config(tmp_path, heartbeat={"active_hours": "9-22"})
    with pytest.raises(ConfigError, match="active_hours must be a list"):
        load_config(path)


def test_malformed_active_hours_wrong_length(tmp_path):
    path = _write_config(tmp_path, heartbeat={"active_hours": [9]})
    with pytest.raises(ConfigError, match="active_hours must be a list"):
        load_config(path)


def test_malformed_active_hours_non_int(tmp_path):
    path = _write_config(tmp_path, heartbeat={"active_hours": ["nine", "ten"]})
    with pytest.raises(ConfigError, match="active_hours must be a list"):
        load_config(path)


def test_models_not_a_dict(tmp_path):
    path = _write_config(tmp_path, models=[{"provider": "ollama", "model": "llama3"}])
    with pytest.raises(ConfigError, match="'models' must be a JSON object"):
        load_config(path)


def test_fallback_models_not_a_list(tmp_path):
    path = _write_config(tmp_path, fallback_models="not-a-list")
    with pytest.raises(ConfigError, match="'fallback_models' must be a JSON array"):
        load_config(path)


def test_timeout_zero_rejected(tmp_path):
    path = _write_config(
        tmp_path,
        models={"main": {
            "provider": "ollama-local", "model": "llama3",
            "base_url": "http://localhost:11434/v1",
            "timeout": 0,
        }},
    )
    with pytest.raises(ConfigError, match="timeout must be a positive integer"):
        load_config(path)


def test_timeout_negative_rejected(tmp_path):
    path = _write_config(
        tmp_path,
        models={"main": {
            "provider": "ollama-local", "model": "llama3",
            "base_url": "http://localhost:11434/v1",
            "timeout": -5,
        }},
    )
    with pytest.raises(ConfigError, match="timeout must be a positive integer"):
        load_config(path)


def test_channel_allowed_users(tmp_path):
    path = _write_config(
        tmp_path,
        channels={
            "telegram": {
                "enabled": True,
                "allowed_users": ["111222333", "444555666"],
            },
            "cli": {
                "enabled": True,
            },
        },
    )
    config = load_config(path)
    assert config.channels["telegram"].allowed_users == ["111222333", "444555666"]
    assert config.channels["cli"].allowed_users == []


class TestValidateConfigSchema:
    def test_valid_keys_no_warnings(self):
        raw = {"models": {}, "routing": {}, "timezone": "UTC"}
        assert validate_config_schema(raw) == []

    def test_unknown_key_warned(self):
        raw = {"models": {}, "evil_payload": "drop tables"}
        warnings = validate_config_schema(raw)
        assert any("evil_payload" in w for w in warnings)

    def test_wrong_type_warned(self):
        raw = {"models": "not a dict"}
        warnings = validate_config_schema(raw)
        assert any("'models' must be a JSON object" in w for w in warnings)

    def test_empty_config_no_warnings(self):
        assert validate_config_schema({}) == []


class TestAllowInsecureStringRejected:
    def test_string_false_rejected(self, tmp_path):
        path = _write_config(
            tmp_path,
            models={"main": {
                "provider": "custom", "model": "m",
                "base_url": "http://my-server:8080/v1",
                "allow_insecure": "false",
            }},
        )
        with pytest.raises(ConfigError, match="allow_insecure must be true or false, not a string"):
            load_config(path)

    def test_string_true_rejected(self, tmp_path):
        path = _write_config(
            tmp_path,
            models={"main": {
                "provider": "custom", "model": "m",
                "base_url": "http://my-server:8080/v1",
                "allow_insecure": "true",
            }},
        )
        with pytest.raises(ConfigError, match="allow_insecure must be true or false, not a string"):
            load_config(path)

    def test_bool_true_accepted(self, tmp_path):
        path = _write_config(
            tmp_path,
            models={"main": {
                "provider": "custom", "model": "m",
                "base_url": "http://my-server:8080/v1",
                "allow_insecure": True,
            }},
        )
        config = load_config(path)
        assert config.models["main"].allow_insecure is True


class TestJSONDecodeError:
    def test_invalid_json_raises_config_error(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text("{ not valid json !!!")
        with pytest.raises(ConfigError):
            load_config(path)

    def test_empty_file_raises_config_error(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text("")
        with pytest.raises(ConfigError):
            load_config(path)


class TestSchemaValidationCalledFromLoad:
    def test_unknown_key_logged(self, tmp_path, caplog):
        import logging
        path = _write_config(tmp_path, bogus_key=42)
        with caplog.at_level(logging.WARNING, logger="faffmonkey.config"):
            load_config(path)
        assert any("bogus_key" in r.message for r in caplog.records)


class TestHeartbeatRangeValidation:
    def test_interval_minutes_is_rejected_with_a_pointer_to_the_schedule(self, tmp_path):
        # Two sources of truth for the schedule, one of which nothing read.
        path = _write_config(tmp_path, heartbeat={"interval_minutes": 30})
        with pytest.raises(ConfigError, match="cron expression"):
            load_config(path)


class TestCompactionRangeValidation:
    def test_threshold_zero_rejected(self, tmp_path):
        path = _write_config(tmp_path, compaction={"threshold": 0})
        with pytest.raises(ConfigError, match="threshold must be between 0 and 1"):
            load_config(path)

    def test_threshold_one_rejected(self, tmp_path):
        path = _write_config(tmp_path, compaction={"threshold": 1})
        with pytest.raises(ConfigError, match="threshold must be between 0 and 1"):
            load_config(path)

    def test_threshold_negative_rejected(self, tmp_path):
        path = _write_config(tmp_path, compaction={"threshold": -0.5})
        with pytest.raises(ConfigError, match="threshold must be between 0 and 1"):
            load_config(path)

    def test_target_ratio_zero_rejected(self, tmp_path):
        path = _write_config(tmp_path, compaction={"target_ratio": 0})
        with pytest.raises(ConfigError, match="target_ratio must be between 0 and 1"):
            load_config(path)

    def test_target_ratio_string_rejected(self, tmp_path):
        path = _write_config(tmp_path, compaction={"target_ratio": "0.2"})
        with pytest.raises(ConfigError, match="target_ratio must be between 0 and 1"):
            load_config(path)

    def test_protect_last_n_negative_rejected(self, tmp_path):
        path = _write_config(tmp_path, compaction={"protect_last_n": -1})
        with pytest.raises(ConfigError, match="protect_last_n must be a positive integer"):
            load_config(path)

    def test_protect_last_n_zero_rejected(self, tmp_path):
        # 0 loaded cleanly and then indexed past the end of the message
        # list on every compaction.
        path = _write_config(tmp_path, compaction={"protect_last_n": 0})
        with pytest.raises(ConfigError, match="protect_last_n must be a positive integer"):
            load_config(path)

    def test_protect_last_n_one_accepted(self, tmp_path):
        path = _write_config(tmp_path, compaction={"protect_last_n": 1})
        assert load_config(path).compaction.protect_last_n == 1

    def test_hard_message_limit_zero_rejected(self, tmp_path):
        path = _write_config(tmp_path, compaction={"hard_message_limit": 0})
        with pytest.raises(ConfigError, match="hard_message_limit must be a positive integer"):
            load_config(path)

    def test_hard_message_limit_negative_rejected(self, tmp_path):
        path = _write_config(tmp_path, compaction={"hard_message_limit": -10})
        with pytest.raises(ConfigError, match="hard_message_limit must be a positive integer"):
            load_config(path)


class TestChannelValidation:
    def test_non_dict_channel_rejected(self, tmp_path):
        path = _write_config(tmp_path, channels={"telegram": "enabled"})
        with pytest.raises(ConfigError, match="channel 'telegram' must be a JSON object"):
            load_config(path)

    def test_non_list_allowed_users_rejected(self, tmp_path):
        path = _write_config(
            tmp_path,
            channels={"telegram": {
                "enabled": True,
                "allowed_users": "123",
            }},
        )
        with pytest.raises(ConfigError, match="allowed_users must be a list"):
            load_config(path)


class TestValidateBaseUrl:
    def test_https_accepted(self):
        from faffmonkey.config import validate_base_url
        assert validate_base_url("https://api.example.com/v1") is None

    def test_http_localhost_accepted(self):
        from faffmonkey.config import validate_base_url
        assert validate_base_url("http://localhost:11434/v1") is None

    def test_http_127_accepted(self):
        from faffmonkey.config import validate_base_url
        assert validate_base_url("http://127.0.0.1:8080/v1") is None

    def test_http_docker_internal_accepted(self):
        from faffmonkey.config import validate_base_url
        assert validate_base_url("http://host.docker.internal:8080/v1") is None

    def test_http_remote_rejected(self):
        from faffmonkey.config import validate_base_url
        err = validate_base_url("http://api.example.com/v1")
        assert err is not None
        assert "localhost" in err

    def test_http_remote_allowed_with_insecure(self):
        from faffmonkey.config import validate_base_url
        assert validate_base_url("http://api.example.com/v1", allow_insecure=True) is None

    def test_ftp_rejected(self):
        from faffmonkey.config import validate_base_url
        err = validate_base_url("ftp://example.com/v1")
        assert err is not None
        assert "https" in err


class TestSearchApiKeyEnvValidation:
    def test_search_api_key_env_arbitrary_env_rejected(self, tmp_path):
        path = _write_config(
            tmp_path,
            search={
                "provider": "brave",
                "api_key_env": "LD_PRELOAD",
            },
        )
        with pytest.raises(ConfigError, match="search: api_key_env.*must match"):
            load_config(path)

    def test_search_api_key_env_valid_accepted(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "bsk-test")
        path = _write_config(
            tmp_path,
            search={
                "provider": "brave",
                "api_key_env": "BRAVE_SEARCH_API_KEY",
            },
        )
        config = load_config(path)
        assert config.search.api_key_env == "BRAVE_SEARCH_API_KEY"

    def test_search_api_key_env_empty_accepted(self, tmp_path):
        path = _write_config(tmp_path, search={"provider": "brave"})
        config = load_config(path)
        assert config.search.api_key_env == ""

    def test_search_api_key_env_path_rejected(self, tmp_path):
        path = _write_config(
            tmp_path,
            search={
                "provider": "brave",
                "api_key_env": "PATH",
            },
        )
        with pytest.raises(ConfigError, match="search: api_key_env.*must match"):
            load_config(path)


class TestNonStringTimezone:
    def test_integer_timezone_falls_back_to_utc(self, tmp_path, caplog):
        import logging
        path = _write_config(tmp_path, timezone=42)
        with caplog.at_level(logging.ERROR, logger="faffmonkey.config"):
            config = load_config(path)
        assert str(config.timezone) == "UTC"
        assert any("timezone must be a string" in r.message for r in caplog.records)

    def test_list_timezone_falls_back_to_utc(self, tmp_path, caplog):
        import logging
        path = _write_config(tmp_path, timezone=["UTC"])
        with caplog.at_level(logging.ERROR, logger="faffmonkey.config"):
            config = load_config(path)
        assert str(config.timezone) == "UTC"


class TestNonDictSearch:
    def test_string_search_raises(self, tmp_path):
        path = _write_config(tmp_path, search="brave")
        with pytest.raises(ConfigError, match="'search' must be a dict"):
            load_config(path)

    def test_list_search_raises(self, tmp_path):
        path = _write_config(tmp_path, search=["brave"])
        with pytest.raises(ConfigError, match="'search' must be a dict"):
            load_config(path)


class TestAllowedUsersTypeCheck:
    def test_int_element_rejected(self, tmp_path):
        path = _write_config(
            tmp_path,
            channels={"telegram": {
                "enabled": True,
                "allowed_users": [123, 456],
            }},
        )
        with pytest.raises(ConfigError, match="allowed_users elements must be strings"):
            load_config(path)

    def test_mixed_types_rejected(self, tmp_path):
        path = _write_config(
            tmp_path,
            channels={"telegram": {
                "enabled": True,
                "allowed_users": ["123", 456],
            }},
        )
        with pytest.raises(ConfigError, match="allowed_users elements must be strings"):
            load_config(path)


class TestSecuritySensitiveUnknownKeys:
    def test_tool_permissions_key_raises(self, tmp_path):
        path = _write_config(tmp_path, tool_permissions={"shell_exec": "always"})
        with pytest.raises(ConfigError, match="security-relevant"):
            load_config(path)

    def test_permissions_key_raises(self, tmp_path):
        path = _write_config(tmp_path, permissions={"shell_exec": "always"})
        with pytest.raises(ConfigError, match="security-relevant"):
            load_config(path)

    def test_benign_unknown_key_only_warned(self, tmp_path, caplog):
        import logging
        path = _write_config(tmp_path, description="my agent")
        with caplog.at_level(logging.WARNING, logger="faffmonkey.config"):
            config = load_config(path)
        assert any("description" in r.message for r in caplog.records)
        assert config.models["main"].provider == "ollama-local"


class TestToolPermissionValidation:
    def test_invalid_permission_string_rejected(self, tmp_path):
        path = _write_config(tmp_path, tools={"shell_exec": "yolo"})
        with pytest.raises(ConfigError, match="tools.shell_exec.*permission must be one of"):
            load_config(path)

    def test_valid_permission_strings_accepted(self, tmp_path):
        path = _write_config(
            tmp_path,
            tools={
                "shell_exec": "always",
                "web_fetch": "ask",
                "file_write": "never",
            },
        )
        config = load_config(path)
        assert config.tool_permissions["shell_exec"] == "always"
        assert config.tool_permissions["web_fetch"] == "ask"
        assert config.tool_permissions["file_write"] == "never"


class TestShellPreapprovedValidation:
    def test_string_instead_of_list_rejected(self, tmp_path):
        path = _write_config(tmp_path, tools={"shell_preapproved": "ls *"})
        with pytest.raises(ConfigError, match="shell_preapproved must be a list of strings"):
            load_config(path)

    def test_list_with_non_string_elements_rejected(self, tmp_path):
        path = _write_config(tmp_path, tools={"shell_preapproved": ["ls *", 42]})
        with pytest.raises(ConfigError, match="shell_preapproved must be a list of strings"):
            load_config(path)

    def test_valid_list_accepted(self, tmp_path):
        path = _write_config(tmp_path, tools={"shell_preapproved": ["ls *", "cat workspace/*"]})
        config = load_config(path)
        assert config.shell_preapproved == ["ls *", "cat workspace/*"]


class TestBaseUrlEmbeddedCredentials:
    def test_userinfo_rejected(self, tmp_path):
        path = _write_config(
            tmp_path,
            models={"main": {
                "provider": "custom", "model": "m",
                "base_url": "https://user:pass@api.example.com/v1",
            }},
        )
        with pytest.raises(ConfigError, match="embedded credentials"):
            load_config(path)

    def test_username_only_rejected(self, tmp_path):
        path = _write_config(
            tmp_path,
            models={"main": {
                "provider": "custom", "model": "m",
                "base_url": "https://admin@api.example.com/v1",
            }},
        )
        with pytest.raises(ConfigError, match="embedded credentials"):
            load_config(path)

    def test_validate_base_url_direct(self):
        from faffmonkey.config import validate_base_url
        err = validate_base_url("https://user:pass@example.com/v1")
        assert err is not None
        assert "credentials" in err


class TestApiKeyEnvSuffixValidation:
    def test_no_suffix_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AWS_CREDENTIALS", "aws-key")
        path = _write_config(
            tmp_path,
            models={"main": {
                "provider": "custom", "model": "m",
                "base_url": "https://api.example.com/v1",
                "api_key_env": "AWS_CREDENTIALS",
            }},
        )
        with pytest.raises(ConfigError, match="must match"):
            load_config(path)

    def test_lowercase_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setenv("my_api_key", "k")
        path = _write_config(
            tmp_path,
            models={"main": {
                "provider": "custom", "model": "m",
                "base_url": "https://api.example.com/v1",
                "api_key_env": "my_api_key",
            }},
        )
        with pytest.raises(ConfigError, match="must match"):
            load_config(path)

    def test_custom_provider_api_key_accepted(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MISTRAL_API_KEY", "mk-test")
        path = _write_config(
            tmp_path,
            models={"main": {
                "provider": "mistral", "model": "m",
                "base_url": "https://api.mistral.ai/v1",
                "api_key_env": "MISTRAL_API_KEY",
            }},
        )
        config = load_config(path)
        assert config.models["main"].api_key == "mk-test"

    def test_custom_provider_token_accepted(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MY_PROVIDER_TOKEN", "tok-test")
        path = _write_config(
            tmp_path,
            models={"main": {
                "provider": "custom", "model": "m",
                "base_url": "https://api.example.com/v1",
                "api_key_env": "MY_PROVIDER_TOKEN",
            }},
        )
        config = load_config(path)
        assert config.models["main"].api_key == "tok-test"


class TestRoutingNonDict:
    def test_list_routing_raises(self, tmp_path):
        path = _write_config(tmp_path, routing=["conversation", "main"])
        with pytest.raises(ConfigError, match="'routing' must be a JSON object"):
            load_config(path)

    def test_string_routing_raises(self, tmp_path):
        path = _write_config(tmp_path, routing="default")
        with pytest.raises(ConfigError, match="'routing' must be a JSON object"):
            load_config(path)


class TestParseModelNonDict:
    def test_integer_model_slot_raises(self, tmp_path):
        path = _write_config(tmp_path, models={"main": 123})
        with pytest.raises(ConfigError, match="model 'main'.*must be a JSON object"):
            load_config(path)

    def test_string_model_slot_raises(self, tmp_path):
        path = _write_config(tmp_path, models={"main": "llama3"})
        with pytest.raises(ConfigError, match="model 'main'.*must be a JSON object"):
            load_config(path)

    def test_list_model_slot_raises(self, tmp_path):
        path = _write_config(tmp_path, models={"main": ["provider", "model"]})
        with pytest.raises(ConfigError, match="model 'main'.*must be a JSON object"):
            load_config(path)


class TestToolsNonDict:
    def test_list_tools_raises(self, tmp_path):
        path = _write_config(tmp_path, tools=["shell_exec", "always"])
        with pytest.raises(ConfigError, match="'tools' must be a JSON object"):
            load_config(path)

    def test_string_tools_raises(self, tmp_path):
        path = _write_config(tmp_path, tools="shell_exec=always")
        with pytest.raises(ConfigError, match="'tools' must be a JSON object"):
            load_config(path)


class TestHeartbeatTypeValidation:
    def test_string_ack_max_chars_rejected(self, tmp_path):
        path = _write_config(tmp_path, heartbeat={"ack_max_chars": "300"})
        with pytest.raises(ConfigError, match="ack_max_chars must be an integer"):
            load_config(path)

    def test_float_ack_max_chars_rejected(self, tmp_path):
        path = _write_config(tmp_path, heartbeat={"ack_max_chars": 300.5})
        with pytest.raises(ConfigError, match="ack_max_chars must be an integer"):
            load_config(path)

    def test_bool_ack_max_chars_rejected(self, tmp_path):
        path = _write_config(tmp_path, heartbeat={"ack_max_chars": True})
        with pytest.raises(ConfigError, match="ack_max_chars must be an integer"):
            load_config(path)

    def test_string_enabled_rejected(self, tmp_path):
        path = _write_config(tmp_path, heartbeat={"enabled": "true"})
        with pytest.raises(ConfigError, match="enabled must be a boolean"):
            load_config(path)

    def test_int_enabled_rejected(self, tmp_path):
        path = _write_config(tmp_path, heartbeat={"enabled": 1})
        with pytest.raises(ConfigError, match="enabled must be a boolean"):
            load_config(path)

    def test_valid_types_accepted(self, tmp_path):
        path = _write_config(tmp_path, heartbeat={"ack_max_chars": 500, "enabled": False})
        config = load_config(path)
        assert config.heartbeat.ack_max_chars == 500
        assert config.heartbeat.enabled is False


class TestTimezoneCharValidation:
    def test_shell_injection_chars_rejected(self, tmp_path):
        path = _write_config(tmp_path, timezone="UTC; rm -rf /")
        with pytest.raises(ConfigError, match="invalid timezone string"):
            load_config(path)

    def test_path_traversal_rejected(self, tmp_path):
        path = _write_config(tmp_path, timezone="../../../etc/passwd")
        with pytest.raises(ConfigError, match="invalid timezone string"):
            load_config(path)

    def test_valid_timezone_with_slash_accepted(self, tmp_path):
        path = _write_config(tmp_path, timezone="America/New_York")
        config = load_config(path)
        assert str(config.timezone) == "America/New_York"


class TestVoiceConfig:
    def test_defaults_when_absent(self, config_file):
        config = load_config(config_file)
        assert config.voice.transcriber == ""
        assert config.voice.synthesiser == ""
        assert config.voice.transcriber_model == "whisper-1"
        assert config.voice.synthesiser_model == "tts-1"
        assert config.voice.synthesiser_voice == "alloy"
        assert config.voice.api_key_env == ""
        assert config.voice.base_url == "https://api.openai.com/v1"

    def test_parses_voice_section(self, tmp_path, valid_config_data):
        valid_config_data["voice"] = {
            "transcriber": "openai",
            "transcriber_module": "extensions.transcriber_openai.OpenAITranscriber",
            "transcriber_model": "gpt-4o-transcribe",
            "synthesiser": "openai",
            "synthesiser_model": "gpt-4o-mini-tts",
            "synthesiser_voice": "nova",
            "api_key_env": "OPENAI_API_KEY",
            "base_url": "https://api.example.com/v1",
        }
        path = tmp_path / "config.json"
        path.write_text(json.dumps(valid_config_data))
        config = load_config(path)
        assert config.voice.transcriber == "openai"
        assert config.voice.transcriber_module == (
            "extensions.transcriber_openai.OpenAITranscriber"
        )
        assert config.voice.transcriber_model == "gpt-4o-transcribe"
        assert config.voice.synthesiser == "openai"
        assert config.voice.synthesiser_model == "gpt-4o-mini-tts"
        assert config.voice.synthesiser_voice == "nova"
        assert config.voice.api_key_env == "OPENAI_API_KEY"
        assert config.voice.base_url == "https://api.example.com/v1"

    def test_voice_not_object_rejected(self, tmp_path, valid_config_data):
        valid_config_data["voice"] = "openai"
        path = tmp_path / "config.json"
        path.write_text(json.dumps(valid_config_data))
        with pytest.raises(ConfigError, match="'voice' must be a JSON object"):
            load_config(path)

    def test_invalid_api_key_env_rejected(self, tmp_path, valid_config_data):
        valid_config_data["voice"] = {"api_key_env": "lowercase_key"}
        path = tmp_path / "config.json"
        path.write_text(json.dumps(valid_config_data))
        with pytest.raises(ConfigError, match="api_key_env"):
            load_config(path)

    def test_non_string_field_rejected(self, tmp_path, valid_config_data):
        valid_config_data["voice"] = {"transcriber": 42}
        path = tmp_path / "config.json"
        path.write_text(json.dumps(valid_config_data))
        with pytest.raises(ConfigError, match="transcriber must be a string"):
            load_config(path)

    def test_insecure_base_url_rejected(self, tmp_path, valid_config_data):
        valid_config_data["voice"] = {"base_url": "http://remote.example.com/v1"}
        path = tmp_path / "config.json"
        path.write_text(json.dumps(valid_config_data))
        with pytest.raises(ConfigError, match="voice:"):
            load_config(path)

    def test_schema_warning_for_non_object_voice(self):
        warnings = validate_config_schema({"voice": []})
        assert "'voice' must be a JSON object" in warnings


class TestGroupPolicy:
    """D4: the setup wizard wrote it and the config layer dropped it."""

    def test_default_is_mention(self, tmp_path):
        path = _write_config(tmp_path, channels={"discord": {"enabled": True}})
        assert load_config(path).channels["discord"].group_policy == "mention"

    def test_configured_value_survives(self, tmp_path):
        path = _write_config(tmp_path, channels={
            "discord": {"enabled": True, "group_policy": "open"},
        })
        assert load_config(path).channels["discord"].group_policy == "open"

    def test_unknown_policy_is_rejected(self, tmp_path):
        path = _write_config(tmp_path, channels={
            "discord": {"enabled": True, "group_policy": "everyone"},
        })
        with pytest.raises(ConfigError, match="group_policy must be one of"):
            load_config(path)


class TestDailyNoteConfig:
    def test_defaults_when_absent(self, tmp_path):
        from faffmonkey.config import load_config
        config = load_config(_write_config(tmp_path))
        assert (config.daily_note.every_turns, config.daily_note.every_minutes) == (10, 60)

    def test_block_overrides_per_key(self, tmp_path):
        from faffmonkey.config import load_config
        config = load_config(_write_config(tmp_path, daily_note={"every_minutes": 30}))
        assert (config.daily_note.every_turns, config.daily_note.every_minutes) == (10, 30)

    @pytest.mark.parametrize("bad", [0, -1, True, "10", 1.5])
    def test_rejects_non_positive_intervals(self, tmp_path, bad):
        from faffmonkey.config import ConfigError, load_config
        with pytest.raises(ConfigError):
            load_config(_write_config(tmp_path, daily_note={"every_turns": bad}))
