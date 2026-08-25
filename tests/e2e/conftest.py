"""A real install, driven end to end.

`faff init` runs for real into tmp_path, so the templates, the generated
config, the config parser and the wiring are all exercised. Only the model
is faked, by pointing base_url at a local scripted HTTP server.

Nothing here writes inside the repo: every path hangs off tmp_path.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from faffmonkey.cli.init import run_init
from faffmonkey.config import Config, load_config
from faffmonkey.runtime.session import SessionStore

from tests.e2e.scripted_provider import ScriptedProvider


@dataclass
class Install:
    """One initialised faffmonkey install plus its scripted model."""

    base: Path
    provider: ScriptedProvider

    @property
    def workspace(self) -> Path:
        return self.base / "workspace"

    @property
    def state(self) -> Path:
        return self.base / "state"

    @property
    def config_path(self) -> Path:
        return self.state / "config.json"

    @property
    def script(self) -> Any:
        return self.provider.script

    def config(self) -> Config:
        """Parsed by the real loader, so a bad config fails the test."""
        return load_config(self.config_path)

    def read_config(self) -> dict:
        return json.loads(self.config_path.read_text())

    def write_config(self, config: dict) -> None:
        self.config_path.write_text(json.dumps(config, indent=2) + "\n")

    def set_jobs(self, jobs: list[dict]) -> None:
        path = self.workspace / "config" / "jobs.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(jobs, indent=2) + "\n")

    def runtime(self):
        """The real wiring, so a config the runtime would reject fails here."""
        from faffmonkey.wiring import wire
        return wire(self.state, workspace=self.base)

    def loop(self, channel=None, **overrides):
        """An AgentLoop built the way `faff chat` builds it."""
        from faffmonkey.runtime.bootstrap import load_bootstrap
        from faffmonkey.runtime.loop import AgentLoop
        from faffmonkey.runtime.tools import ToolRegistry
        from faffmonkey.runtime.trust import load_trust_store
        from faffmonkey.seams.channel_noop import NoopChannel

        runtime = self.runtime()
        trust_store = load_trust_store(self.state)
        bootstrap = load_bootstrap(
            self.workspace, runtime.config, mode="full",
            wrap=True, trust_store=trust_store,
        )
        registry = ToolRegistry(
            workspace=self.workspace,
            permissions=runtime.config.tool_permissions,
            shell_preapproved=runtime.config.shell_preapproved,
            prompt_fn=lambda description: False,
            tz=str(runtime.config.timezone),
            wrap=True,
            search_provider=runtime.search_provider,
            state_dir=self.state,
        )
        kwargs = dict(
            resolve_provider=runtime.resolve_provider,
            config=runtime.config,
            channel=channel or NoopChannel(),
            system_prompt=bootstrap.text,
            db_path=self.state / "sessions.db",
            state_dir=self.state,
            tool_registry=registry,
            workspace=self.workspace,
            allow_overflow=True,
            bootstrap_file_tokens=bootstrap.file_tokens,
        )
        kwargs.update(overrides)
        return AgentLoop(**kwargs)

    def scheduler(self, channels: dict | None = None, **overrides):
        """A Scheduler built the way `faff run` builds it."""
        from faffmonkey.runtime.scheduler import Scheduler

        runtime = self.runtime()
        kwargs = dict(
            config=runtime.config,
            workspace=self.workspace,
            state_dir=self.state,
            resolve_provider=runtime.resolve_provider,
            channels=channels or {},
            search_provider=runtime.search_provider,
        )
        kwargs.update(overrides)
        return Scheduler(**kwargs)

    def history(self, channel_id: str = "cli") -> list:
        """The persisted conversation, read the way the runtime reads it."""
        store = SessionStore(self.state / "sessions.db")
        try:
            session = store.get_or_create_main_session(channel_id)
            return store.get_history(session.id)
        finally:
            store.close()

    def history_text(self, channel_id: str = "cli") -> str:
        return "\n".join(
            m.content or "" for m in self.history(channel_id)
        )


def make_install(base: Path, provider: ScriptedProvider) -> Install:
    base.mkdir(parents=True, exist_ok=True)
    # "" accepts the detected timezone and skips every identity question.
    with patch("builtins.input", return_value=""):
        run_init(base)

    install = Install(base=base, provider=provider)
    config = install.read_config()
    model = {
        "provider": "e2e",
        "model": "e2e-model",
        "base_url": provider.base_url,
    }
    config["models"] = {"main": dict(model), "cheap": dict(model), "vision": dict(model)}
    config["timezone"] = "UTC"
    install.write_config(config)
    return install


@pytest.fixture
def install_factory(tmp_path):
    """Build an install around a scripted response list.

    Used as: `with install_factory([message("hi")]) as install:`
    """
    created: list[ScriptedProvider] = []

    class _Factory:
        def __init__(self) -> None:
            self._n = 0

        def __call__(self, responses: list[dict]) -> "_Ctx":
            self._n += 1
            return _Ctx(tmp_path / f"install{self._n}", responses, created)

    class _Ctx:
        def __init__(self, base: Path, responses: list[dict], sink: list) -> None:
            self._base = base
            self._responses = responses
            self._sink = sink

        def __enter__(self) -> Install:
            self._provider = ScriptedProvider(self._responses)
            self._provider.__enter__()
            self._sink.append(self._provider)
            return make_install(self._base, self._provider)

        def __exit__(self, *exc: object) -> None:
            self._provider.__exit__(*exc)
            self._sink.remove(self._provider)

    yield _Factory()

    for provider in list(created):
        provider.__exit__(None, None, None)
