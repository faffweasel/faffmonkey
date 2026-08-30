"""Tests for faff doctor."""

import json
import sqlite3
from unittest.mock import patch

import pytest


from faffmonkey.cli.doctor import (
    GREEN,
    YELLOW,
    _check_bootstrap_files,
    _check_config,
    _check_context_window,
    _check_database,
    _check_dirs,
    _check_extensions,
    _check_heartbeat,
    _check_location,
    _check_routing,
    _check_skills,
    _check_timezone,
    _safe_url,
    run_doctor,
)
from faffmonkey.config import Config, CompactionConfig, HeartbeatConfig, ModelConfig
from faffmonkey.runtime.session import SCHEMA_VERSION
from zoneinfo import ZoneInfo


def _make_config(**overrides) -> Config:
    defaults = {
        "models": {
            "main": ModelConfig(
                provider="test", model="test-model",
                base_url="http://localhost:11434/v1", api_key="",
            ),
        },
        "routing": {"conversation": "main", "cron_default": "main"},
        "fallback_models": [],
        "timezone": ZoneInfo("UTC"),
        "heartbeat": HeartbeatConfig(),
        "compaction": CompactionConfig(),
        "channels": {},
        "tool_permissions": {},
    }
    defaults.update(overrides)
    return Config(**defaults)


class TestSafeUrl:
    def test_strips_user_and_password(self):
        assert _safe_url("http://user:pass@host.com:8080/path") == "http://host.com:8080/path"

    def test_strips_user_only(self):
        assert _safe_url("http://user@host.com/path") == "http://host.com/path"

    def test_no_credentials_unchanged(self):
        assert _safe_url("https://host.com/v1") == "https://host.com/v1"

    def test_preserves_port(self):
        assert _safe_url("http://u:p@localhost:11434/v1") == "http://localhost:11434/v1"

    def test_preserves_query_and_fragment(self):
        assert _safe_url("http://u:p@host.com/path?q=1#f") == "http://host.com/path?q=1#f"


class TestCheckDirs:
    def test_both_exist(self, tmp_path, capsys):
        (tmp_path / "workspace").mkdir()
        (tmp_path / "state").mkdir()
        result = _check_dirs(tmp_path)
        assert result == "ok"
        assert "ok" in capsys.readouterr().out

    def test_missing_workspace(self, tmp_path, capsys):
        (tmp_path / "state").mkdir()
        result = _check_dirs(tmp_path)
        assert result == "!!"
        assert "workspace/" in capsys.readouterr().out

    def test_missing_state(self, tmp_path, capsys):
        (tmp_path / "workspace").mkdir()
        result = _check_dirs(tmp_path)
        assert result == "!!"
        assert "state/" in capsys.readouterr().out

    def test_both_missing(self, tmp_path, capsys):
        result = _check_dirs(tmp_path)
        assert result == "!!"
        out = capsys.readouterr().out
        assert "workspace/" in out
        assert "state/" in out


class TestCheckRouting:
    """A slot vanished from models while a routing entry and a cron job
    still named it. Nothing reported it until the job failed at its tick."""

    def _dirs(self, tmp_path, raw_config, jobs=None):
        state = tmp_path / "state"
        state.mkdir()
        (state / "config.json").write_text(json.dumps(raw_config))
        workspace = tmp_path / "workspace"
        (workspace / "config").mkdir(parents=True)
        if jobs is not None:
            (workspace / "config" / "jobs.json").write_text(json.dumps(jobs))
        return state, workspace

    def test_written_route_to_a_missing_slot_is_red(self, tmp_path, capsys):
        state, ws = self._dirs(tmp_path, {"routing": {"dreaming": "dream"}})
        assert _check_routing(_make_config(), state, ws) == "!!"
        out = capsys.readouterr().out
        assert "routing.dreaming" in out and "'dream'" in out

    def test_job_model_naming_a_missing_slot_is_red(self, tmp_path, capsys):
        state, ws = self._dirs(tmp_path, {}, jobs=[
            {"id": "dream", "schedule": "0 3 * * *", "prompt": "dream", "model": "dream"},
        ])
        assert _check_routing(_make_config(), state, ws) == "!!"
        assert "job 'dream'" in capsys.readouterr().out

    def test_default_routes_to_absent_cheap_and_vision_are_not_flagged(self, tmp_path, capsys):
        state, ws = self._dirs(tmp_path, {})
        assert _check_routing(_make_config(), state, ws) == GREEN
        assert "slots: main" in capsys.readouterr().out

    def test_present_slots_are_green(self, tmp_path):
        state, ws = self._dirs(tmp_path, {"routing": {"dreaming": "main"}}, jobs=[
            {"id": "j", "schedule": "0 3 * * *", "prompt": "x", "model": "main"},
        ])
        assert _check_routing(_make_config(), state, ws) == GREEN


class TestCheckLocation:
    """A location.json with lat 0, lng 0 passed every check while the
    weather skill reported the Gulf of Guinea as Hanoi."""

    def _write(self, workspace, payload):
        (workspace / "config").mkdir(parents=True)
        (workspace / "config" / "location.json").write_text(json.dumps(payload))

    def test_placeholder_coordinates_flagged(self, tmp_path, capsys):
        self._write(tmp_path, {"current": {"city": "Hanoi", "lat": 0, "lng": 0}})
        assert _check_location(tmp_path) == YELLOW
        out = capsys.readouterr().out
        assert "Hanoi" in out and "placeholder" in out

    def test_real_coordinates_ok(self, tmp_path, capsys):
        self._write(tmp_path, {"current": {"city": "Hanoi", "lat": 21.028, "lng": 105.854}})
        assert _check_location(tmp_path) == GREEN
        assert "21.028" in capsys.readouterr().out

    def test_no_file_is_optional(self, tmp_path, capsys):
        assert _check_location(tmp_path) == GREEN
        assert "optional" in capsys.readouterr().out

    def test_unreadable_file_flagged(self, tmp_path, capsys):
        (tmp_path / "config").mkdir()
        (tmp_path / "config" / "location.json").write_text("{not json")
        assert _check_location(tmp_path) == YELLOW


class TestCheckConfig:
    def test_valid_config(self, tmp_path, capsys):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        config = {
            "timezone": "UTC",
            "models": {
                "main": {
                    "provider": "test", "model": "test-model",
                    "base_url": "http://localhost/v1",
                }
            },
        }
        (state_dir / "config.json").write_text(json.dumps(config))
        status, cfg = _check_config(state_dir)
        assert status == "ok"
        assert cfg is not None

    def test_missing_config(self, tmp_path, capsys):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        status, cfg = _check_config(state_dir)
        assert status == "!!"
        assert cfg is None

    def test_invalid_json(self, tmp_path, capsys):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "config.json").write_text("not json {{{")
        status, cfg = _check_config(state_dir)
        assert status == "!!"
        assert cfg is None
        assert "setup provider" not in capsys.readouterr().out

    def test_missing_models_suggests_setup_provider(self, tmp_path, capsys):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "config.json").write_text(json.dumps({"timezone": "UTC"}))
        status, cfg = _check_config(state_dir)
        assert status == "!!"
        assert cfg is None
        assert "Run: faff setup provider" in capsys.readouterr().out


class TestCheckContextWindow:
    """A slot on the 128000 default is a warning, not a fact."""

    def _state(self, tmp_path, main: dict):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "config.json").write_text(json.dumps({
            "timezone": "UTC", "models": {"main": main},
        }))
        return state_dir

    def _config(self, **fields) -> Config:
        return _make_config(models={"main": ModelConfig(
            provider="test", model="kimi-k3:cloud",
            base_url="http://localhost:11434/v1", api_key="", **fields,
        )})

    _raw = {
        "provider": "test", "model": "kimi-k3:cloud",
        "base_url": "http://localhost:11434/v1",
    }

    def test_unset_window_names_the_default_and_what_the_provider_reports(self, tmp_path, capsys):
        state_dir = self._state(tmp_path, self._raw)
        with patch("faffmonkey.cli.doctor.detect_context_window", return_value=1048576):
            status = _check_context_window(self._config(), state_dir)
        out = capsys.readouterr().out
        assert status == YELLOW
        assert "128000 default" in out
        assert "kimi-k3:cloud reports 1048576" in out
        assert "faff setup provider" in out

    def test_unset_window_warns_even_when_the_provider_is_silent(self, tmp_path, capsys):
        state_dir = self._state(tmp_path, self._raw)
        with patch("faffmonkey.cli.doctor.detect_context_window", return_value=None):
            status = _check_context_window(self._config(), state_dir)
        out = capsys.readouterr().out
        assert status == YELLOW
        assert "128000 default" in out
        assert "reports" not in out

    def test_configured_window_matching_the_provider_is_ok(self, tmp_path, capsys):
        state_dir = self._state(tmp_path, {**self._raw, "context_window": 1048576})
        with patch("faffmonkey.cli.doctor.detect_context_window", return_value=1048576):
            status = _check_context_window(self._config(context_window=1048576), state_dir)
        out = capsys.readouterr().out
        assert status == GREEN
        assert "1048576 tokens" in out
        assert "matches the provider" in out

    def test_configured_window_differing_from_the_provider_warns(self, tmp_path, capsys):
        state_dir = self._state(tmp_path, {**self._raw, "context_window": 128000})
        with patch("faffmonkey.cli.doctor.detect_context_window", return_value=1048576):
            status = _check_context_window(self._config(context_window=128000), state_dir)
        out = capsys.readouterr().out
        assert status == YELLOW
        assert "128000 configured" in out
        assert "reports 1048576" in out

    def test_configured_window_with_silent_provider_is_ok(self, tmp_path, capsys):
        state_dir = self._state(tmp_path, {**self._raw, "context_window": 32000})
        with patch("faffmonkey.cli.doctor.detect_context_window", return_value=None):
            status = _check_context_window(self._config(context_window=32000), state_dir)
        out = capsys.readouterr().out
        assert status == GREEN
        assert "32000 tokens" in out

    def test_checks_the_conversation_slot_not_main(self, tmp_path, capsys):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "config.json").write_text(json.dumps({
            "timezone": "UTC",
            "models": {"main": self._raw, "big": {**self._raw, "context_window": 1048576}},
            "routing": {"conversation": "big"},
        }))
        config = _make_config(
            models={
                "main": ModelConfig(
                    provider="test", model="kimi-k3:cloud",
                    base_url="http://localhost:11434/v1", api_key="",
                ),
                "big": ModelConfig(
                    provider="test", model="kimi-k3:cloud",
                    base_url="http://localhost:11434/v1", api_key="",
                    context_window=1048576,
                ),
            },
            routing={"conversation": "big"},
        )
        with patch("faffmonkey.cli.doctor.detect_context_window", return_value=1048576):
            status = _check_context_window(config, state_dir)
        assert status == GREEN
        assert "big: 1048576" in capsys.readouterr().out


class TestCheckExtensions:
    @pytest.fixture(autouse=True)
    def _contrib_root_is_tmp(self, tmp_path, monkeypatch):
        # classify_origin resolves contrib against the checkout; these
        # tests build their contrib under tmp_path.
        monkeypatch.setattr(
            "faffmonkey.cli.update._find_project_root", lambda: tmp_path
        )

    def test_no_extensions_dir(self, tmp_path, capsys):
        result = _check_extensions(tmp_path)
        assert result == "ok"

    def test_empty_extensions(self, tmp_path, capsys):
        (tmp_path / "extensions").mkdir()
        (tmp_path / "extensions" / ".origin.json").write_text("{}")
        result = _check_extensions(tmp_path)
        assert result == "ok"

    def test_stale_extension(self, tmp_path, capsys):
        import hashlib
        ext_dir = tmp_path / "extensions"
        ext_dir.mkdir()
        contrib_dir = tmp_path / "contrib"
        contrib_dir.mkdir()

        original = b"# original version"
        install_hash = hashlib.sha256(original).hexdigest()[:12]
        (ext_dir / "channel_telegram.py").write_bytes(original)
        (contrib_dir / "channel_telegram.py").write_text("# updated version")
        (ext_dir / ".origin.json").write_text(json.dumps({
            "channel_telegram.py": {
                "source": "contrib/channel_telegram.py",
                "copied_at": "2026-01-01T00:00:00Z",
                "contrib_source_hash": install_hash,
            }
        }))

        result = _check_extensions(tmp_path)
        assert result == "--"
        assert "contrib version updated" in capsys.readouterr().out

    def test_up_to_date_extension(self, tmp_path, capsys):
        import hashlib
        ext_dir = tmp_path / "extensions"
        ext_dir.mkdir()
        contrib_dir = tmp_path / "contrib"
        contrib_dir.mkdir()

        content = b"# telegram channel"
        (contrib_dir / "channel_telegram.py").write_bytes(content)
        (ext_dir / "channel_telegram.py").write_bytes(content)
        h = hashlib.sha256(content).hexdigest()[:12]
        (ext_dir / ".origin.json").write_text(json.dumps({
            "channel_telegram.py": {
                "source": "contrib/channel_telegram.py",
                "copied_at": "2026-01-01T00:00:00Z",
                "contrib_source_hash": h,
            }
        }))

        result = _check_extensions(tmp_path)
        assert result == "ok"

    def test_source_traversal_rejected(self, tmp_path, capsys):
        ext_dir = tmp_path / "extensions"
        ext_dir.mkdir()
        (ext_dir / "evil.py").write_text("# evil")
        (ext_dir / ".origin.json").write_text(json.dumps({
            "evil.py": {
                "source": "../../etc/passwd",
                "copied_at": "2026-01-01T00:00:00Z",
                "contrib_source_hash": "abcdef123456",
            }
        }))

        result = _check_extensions(tmp_path)
        assert result == "!!"
        out = capsys.readouterr().out
        assert "escapes contrib/" in out

    def test_tampered_extension(self, tmp_path, capsys):
        import hashlib
        ext_dir = tmp_path / "extensions"
        ext_dir.mkdir()
        contrib_dir = tmp_path / "contrib"
        contrib_dir.mkdir()

        original = b"# original"
        tampered = b"# tampered by attacker"
        install_hash = hashlib.sha256(original).hexdigest()[:12]

        (contrib_dir / "channel_telegram.py").write_bytes(original)
        (ext_dir / "channel_telegram.py").write_bytes(tampered)
        (ext_dir / ".origin.json").write_text(json.dumps({
            "channel_telegram.py": {
                "source": "contrib/channel_telegram.py",
                "copied_at": "2026-01-01T00:00:00Z",
                "contrib_source_hash": install_hash,
            }
        }))

        result = _check_extensions(tmp_path)
        assert result == "!!"
        out = capsys.readouterr().out
        assert "modified since install" in out

    def test_tampered_with_forged_install_hash(self, tmp_path, capsys):
        import hashlib
        ext_dir = tmp_path / "extensions"
        ext_dir.mkdir()
        contrib_dir = tmp_path / "contrib"
        contrib_dir.mkdir()

        original = b"# original"
        tampered = b"# tampered by attacker"
        forged_hash = hashlib.sha256(tampered).hexdigest()[:12]

        (contrib_dir / "channel_telegram.py").write_bytes(original)
        (ext_dir / "channel_telegram.py").write_bytes(tampered)
        (ext_dir / ".origin.json").write_text(json.dumps({
            "channel_telegram.py": {
                "source": "contrib/channel_telegram.py",
                "copied_at": "2026-01-01T00:00:00Z",
                "contrib_source_hash": forged_hash,
            }
        }))

        result = _check_extensions(tmp_path)
        assert result == "--"
        out = capsys.readouterr().out
        assert "contrib version updated" in out

    def test_unverifiable_no_source(self, tmp_path, capsys):
        import hashlib
        ext_dir = tmp_path / "extensions"
        ext_dir.mkdir()

        content = b"# custom extension"
        (ext_dir / "custom_plugin.py").write_bytes(content)
        (ext_dir / ".origin.json").write_text(json.dumps({
            "custom_plugin.py": {
                "copied_at": "2026-01-01T00:00:00Z",
                "contrib_source_hash": hashlib.sha256(content).hexdigest()[:12],
            }
        }))

        result = _check_extensions(tmp_path)
        assert result == "--"
        out = capsys.readouterr().out
        assert "unverifiable" in out

    def test_old_contrib_hash_entry_unverifiable(self, tmp_path, capsys):
        import hashlib
        ext_dir = tmp_path / "extensions"
        ext_dir.mkdir()
        contrib_dir = tmp_path / "contrib"
        contrib_dir.mkdir()

        content = b"# telegram channel"
        (contrib_dir / "channel_telegram.py").write_bytes(content)
        (ext_dir / "channel_telegram.py").write_bytes(content)
        h = hashlib.sha256(content).hexdigest()[:12]
        (ext_dir / ".origin.json").write_text(json.dumps({
            "channel_telegram.py": {
                "source": "contrib/channel_telegram.py",
                "copied_at": "2026-01-01T00:00:00Z",
                "contrib_hash": h,
            }
        }))

        result = _check_extensions(tmp_path)
        assert result == "--"
        out = capsys.readouterr().out
        assert "unverifiable" in out


class TestCheckBootstrapFiles:
    def test_all_present(self, tmp_path, capsys):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        for name in ["SOUL.md", "IDENTITY.md", "USER.md", "AGENTS.md"]:
            (workspace / name).write_text("# " + name + "\nSome real content here.")
        result = _check_bootstrap_files(workspace)
        assert result == "ok"

    def test_missing_file(self, tmp_path, capsys):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "SOUL.md").write_text("# Soul\nContent.")
        result = _check_bootstrap_files(workspace)
        assert result == "--"

    def test_empty_file(self, tmp_path, capsys):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        for name in ["SOUL.md", "IDENTITY.md", "USER.md", "AGENTS.md"]:
            (workspace / name).write_text("")
        result = _check_bootstrap_files(workspace)
        assert result == "--"


class TestCheckDatabase:
    def test_no_database(self, tmp_path, capsys):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        result = _check_database(state_dir)
        assert result == "--"

    def test_valid_database(self, tmp_path, capsys):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        db_path = state_dir / "sessions.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
        conn.commit()
        conn.close()
        result = _check_database(state_dir)
        assert result == "ok"

    def test_outdated_schema(self, tmp_path, capsys):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        db_path = state_dir / "sessions.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (0,))
        conn.commit()
        conn.close()
        result = _check_database(state_dir)
        assert result == "--"
        assert "expected" in capsys.readouterr().out


class TestCheckTimezone:
    def test_valid_timezone(self, capsys):
        config = _make_config(timezone=ZoneInfo("Asia/Bangkok"))
        result = _check_timezone(config)
        assert result == "ok"


class TestCheckHeartbeat:
    """D2: doctor reported a config interval that nothing read."""

    def _workspace(self, tmp_path, jobs):
        workspace = tmp_path / "workspace"
        (workspace / "config").mkdir(parents=True)
        (workspace / "config" / "jobs.json").write_text(json.dumps(jobs))
        return workspace

    def test_enabled_with_a_job_reports_the_real_schedule(self, tmp_path, capsys):
        config = _make_config(heartbeat=HeartbeatConfig(enabled=True))
        ws = self._workspace(tmp_path, [
            {"id": "hb", "schedule": "0 * * * *", "context": "heartbeat"},
        ])
        assert _check_heartbeat(config, ws) == "ok"
        out = capsys.readouterr().out
        assert "0 * * * *" in out
        assert "09:00-22:00" in out

    def test_enabled_with_no_job_is_not_ok(self, tmp_path, capsys):
        config = _make_config(heartbeat=HeartbeatConfig(enabled=True))
        ws = self._workspace(tmp_path, [])
        assert _check_heartbeat(config, ws) == "--"
        assert "no heartbeat job" in capsys.readouterr().out

    def test_enabled_with_only_disabled_jobs_is_not_ok(self, tmp_path, capsys):
        config = _make_config(heartbeat=HeartbeatConfig(enabled=True))
        ws = self._workspace(tmp_path, [
            {"id": "hb", "schedule": "0 * * * *", "context": "heartbeat", "enabled": False},
        ])
        assert _check_heartbeat(config, ws) == "--"
        assert "disabled" in capsys.readouterr().out

    def test_disabled(self, tmp_path, capsys):
        config = _make_config(heartbeat=HeartbeatConfig(enabled=False))
        assert _check_heartbeat(config, tmp_path) == "--"


class TestCheckSkills:
    def test_no_skills_dir(self, tmp_path, capsys):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        result = _check_skills(workspace)
        assert result == "--"

    def test_skills_with_skill_md(self, tmp_path, capsys):
        workspace = tmp_path / "workspace"
        skills_dir = workspace / "skills" / "test-skill"
        skills_dir.mkdir(parents=True)
        (skills_dir / "SKILL.md").write_text("---\nname: test\n---\nA test skill.")
        (workspace / "skills-data").mkdir()
        result = _check_skills(workspace)
        assert result == "ok"


class TestRunDoctor:
    def test_fresh_install_no_state(self, tmp_path, capsys):
        exit_code = run_doctor(tmp_path)
        assert exit_code == 1
        out = capsys.readouterr().out
        assert "faff init" in out

    def test_fresh_init_suggests_setup_provider(self, tmp_path, capsys):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (tmp_path / "workspace").mkdir()
        (state_dir / "config.json").write_text(json.dumps({"timezone": "UTC"}))
        exit_code = run_doctor(tmp_path)
        assert exit_code == 1
        out = capsys.readouterr().out
        assert "No LLM provider configured. Run: faff setup provider" in out
        assert "run: faff init" not in out

    def test_configured_system(self, tmp_path, capsys):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "SOUL.md").write_text("# Soul\nYou are helpful.")
        (workspace / "IDENTITY.md").write_text("# Identity\nFaffmonkey.")
        (workspace / "USER.md").write_text("# User\nAlex.")
        (workspace / "AGENTS.md").write_text("# Agents\nNone.")

        config = {
            "timezone": "UTC",
            "models": {
                "main": {
                    "provider": "test", "model": "test-model",
                    "base_url": "http://localhost:11434/v1",
                }
            },
        }
        (state_dir / "config.json").write_text(json.dumps(config))

        with patch(
            "faffmonkey.runtime.scheduler.provider_preflight", return_value=True
        ), patch(
            "faffmonkey.cli.doctor.detect_context_window", return_value=None
        ):
            exit_code = run_doctor(tmp_path)

        out = capsys.readouterr().out
        assert "Ready to run" in out
        assert exit_code == 0

    def test_rejects_bad_base_url_scheme(self, tmp_path, capsys):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        config = {
            "timezone": "UTC",
            "models": {
                "main": {
                    "provider": "test", "model": "test-model",
                    "base_url": "http://evil.example.com/v1",
                }
            },
        }
        (state_dir / "config.json").write_text(json.dumps(config))

        exit_code = run_doctor(tmp_path)

        out = capsys.readouterr().out
        assert "only allowed for localhost" in out
        assert exit_code == 1

    def test_origin_json_nonexistent_source_rejected(self, tmp_path, capsys):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        extensions = tmp_path / "extensions"
        extensions.mkdir()
        contrib = tmp_path / "contrib"
        contrib.mkdir()

        ext_file = extensions / "search_provider_brave.py"
        ext_file.write_text("# extension")

        (extensions / ".origin.json").write_text(json.dumps({
            "search_provider_brave.py": {
                "source": "contrib/nonexistent.py",
                "copied_at": "2026-01-01T00:00:00Z",
                "contrib_hash": "abc123",
            }
        }))

        config = {
            "timezone": "UTC",
            "models": {
                "main": {
                    "provider": "test", "model": "test-model",
                    "base_url": "http://localhost:11434/v1",
                }
            },
        }
        (state_dir / "config.json").write_text(json.dumps(config))

        exit_code = run_doctor(tmp_path)
        out = capsys.readouterr().out
        assert "does not exist" in out
        assert exit_code == 1

    def test_rejects_ftp_base_url(self, tmp_path, capsys):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        config = {
            "timezone": "UTC",
            "models": {
                "main": {
                    "provider": "test", "model": "test-model",
                    "base_url": "ftp://evil.example.com/v1",
                }
            },
        }
        (state_dir / "config.json").write_text(json.dumps(config))

        exit_code = run_doctor(tmp_path)

        out = capsys.readouterr().out
        assert "https" in out
        assert exit_code == 1


class TestCheckCommands:
    def _base(self, tmp_path):
        state = tmp_path / "state"
        state.mkdir()
        return state, tmp_path

    def test_missing_file_is_green(self, tmp_path, capsys):
        from faffmonkey.cli.doctor import _check_commands, GREEN
        state, base = self._base(tmp_path)
        assert _check_commands(state, base) == GREEN
        assert "optional" in capsys.readouterr().out

    def test_invalid_json_is_red(self, tmp_path, capsys):
        from faffmonkey.cli.doctor import _check_commands, RED
        state, base = self._base(tmp_path)
        (state / "commands.json").write_text("not json")
        assert _check_commands(state, base) == RED

    def test_non_object_is_red(self, tmp_path, capsys):
        from faffmonkey.cli.doctor import _check_commands, RED
        state, base = self._base(tmp_path)
        (state / "commands.json").write_text('["x"]')
        assert _check_commands(state, base) == RED

    def test_valid_command_with_existing_script_is_green(self, tmp_path, capsys):
        from faffmonkey.cli.doctor import _check_commands, GREEN
        state, base = self._base(tmp_path)
        script = base / "workspace" / "skills" / "img" / "scripts" / "gen.py"
        script.parent.mkdir(parents=True)
        script.write_text("")
        (state / "commands.json").write_text(
            '{"IMAGE_GEN_CMD": "python3 skills/img/scripts/gen.py"}'
        )
        assert _check_commands(state, base) == GREEN

    def test_missing_script_is_yellow(self, tmp_path, capsys):
        from faffmonkey.cli.doctor import _check_commands, YELLOW
        state, base = self._base(tmp_path)
        (state / "commands.json").write_text(
            '{"IMAGE_GEN_CMD": "python3 skills/img/scripts/gen.py"}'
        )
        assert _check_commands(state, base) == YELLOW
        assert "script not found" in capsys.readouterr().out

    def test_reserved_key_is_yellow(self, tmp_path, capsys):
        from faffmonkey.cli.doctor import _check_commands, YELLOW
        state, base = self._base(tmp_path)
        (state / "commands.json").write_text('{"PATH": "/evil"}')
        assert _check_commands(state, base) == YELLOW


class TestDoctorRepairsSchemaVersion:
    """D21: an interrupted first run left the table with no row, forever."""

    def test_missing_version_row_is_restored(self, tmp_path):
        import sqlite3
        from faffmonkey.cli.doctor import _check_database, YELLOW
        from faffmonkey.runtime.session import SCHEMA_VERSION, SessionStore

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        store = SessionStore(state_dir / "sessions.db")
        store.close()

        conn = sqlite3.connect(str(state_dir / "sessions.db"))
        conn.execute("DELETE FROM schema_version")
        conn.commit()
        conn.close()

        assert _check_database(state_dir) == YELLOW

        conn = sqlite3.connect(str(state_dir / "sessions.db"))
        row = conn.execute("SELECT version FROM schema_version").fetchone()
        conn.close()
        assert row[0] == SCHEMA_VERSION


class TestDoctorChecksJobs:
    """P7-M4: doctor said "ready to run" with every job rejected."""

    def _workspace(self, tmp_path, text):
        workspace = tmp_path / "workspace"
        (workspace / "config").mkdir(parents=True)
        (workspace / "config" / "jobs.json").write_text(text)
        return workspace

    def test_unparseable_jobs_file_is_red(self, tmp_path, capsys):
        from faffmonkey.cli.doctor import _check_jobs, RED
        ws = self._workspace(tmp_path, "{not json")
        assert _check_jobs(ws) == RED
        assert "unreadable" in capsys.readouterr().out

    def test_rejected_entries_are_red(self, tmp_path, capsys):
        from faffmonkey.cli.doctor import _check_jobs, RED
        ws = self._workspace(tmp_path, json.dumps([
            {"id": "good", "schedule": "0 7 * * *", "prompt": "hi"},
            {"id": "bad hour", "schedule": "0 25 * * *", "prompt": "hi"},
        ]))
        assert _check_jobs(ws) == RED
        assert "1 of 2 job(s) rejected" in capsys.readouterr().out

    def test_valid_jobs_are_green(self, tmp_path, capsys):
        from faffmonkey.cli.doctor import _check_jobs, GREEN
        ws = self._workspace(tmp_path, json.dumps([
            {"id": "good", "schedule": "0 7 * * *", "prompt": "hi"},
        ]))
        assert _check_jobs(ws) == GREEN
        assert "1 job(s) valid" in capsys.readouterr().out


class TestCommandPathsAreWorkspaceRelative:
    """2026-08-24: commands run with cwd=workspace, but doctor resolved
    their script paths against the data root and reported a correctly
    installed skill's script as not found."""

    def test_workspace_relative_script_is_found(self, tmp_path, capsys):
        from faffmonkey.cli.doctor import _check_commands, GREEN

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        scripts = tmp_path / "workspace" / "skills" / "venice-ai-media" / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "venice-image.py").write_text("# script")
        (state_dir / "commands.json").write_text(json.dumps({
            "IMAGE_GEN_CMD": "python3 skills/venice-ai-media/scripts/venice-image.py",
        }))

        result = _check_commands(state_dir, tmp_path)
        assert result == GREEN
        assert "ok" in capsys.readouterr().out

    def test_genuinely_missing_script_still_flagged(self, tmp_path, capsys):
        from faffmonkey.cli.doctor import _check_commands, YELLOW

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (tmp_path / "workspace").mkdir()
        (state_dir / "commands.json").write_text(json.dumps({
            "IMAGE_GEN_CMD": "python3 skills/nope/scripts/gone.py",
        }))

        result = _check_commands(state_dir, tmp_path)
        assert result == YELLOW
        assert "script not found" in capsys.readouterr().out


class TestCheckHeartbeatSession:
    """A heartbeat wake is an agent turn; a job left in the pre-0.2.0 shape
    still runs, as a tool-less completion that can act on nothing."""

    def test_isolated_heartbeat_job_is_flagged(self, tmp_path, capsys):
        config = _make_config(heartbeat=HeartbeatConfig(enabled=True))
        workspace = tmp_path / "workspace"
        (workspace / "config").mkdir(parents=True)
        (workspace / "config" / "jobs.json").write_text(json.dumps([
            {"id": "heartbeat", "schedule": "0 * * * *", "context": "heartbeat",
             "session": "isolated", "prompt": "check"},
        ]))

        assert _check_heartbeat(config, workspace) == "--"

        out = capsys.readouterr().out
        assert "agent turn" in out and "faff update" in out
