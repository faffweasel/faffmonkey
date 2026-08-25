"""What a setup wizard is for: producing a config the runtime can start on.

Every wizard was tested by asserting the sequence of input() calls and the
keys it wrote. None of them checked the only property that matters, which
is that the result loads through the real parser and wires up. A wizard
that writes a config faff refuses to start on has failed, however many
prompts it got right.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from faffmonkey.cli.init import _find_project_root
from faffmonkey.config import load_config

from tests.e2e.scripted_provider import message

REPO_ROOT = _find_project_root()


@pytest.fixture
def install(install_factory):
    """A real install whose wizards find the repo's contrib/ directory."""
    with install_factory([message("ok")]) as inst:
        with patch(
            "faffmonkey.cli.setup_provider._find_project_root",
            return_value=REPO_ROOT,
        ):
            yield inst


def _wired(install):
    """Parse and wire exactly as `faff run` does."""
    from faffmonkey.wiring import wire
    load_config(install.config_path)
    return wire(install.state, workspace=install.base)


class TestProviderWizard:
    def test_the_config_it_writes_loads_and_wires(self, install, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
        inputs = iter(["3", "some-model", "y"])

        with patch(
            "faffmonkey.cli.setup_provider._read_input",
            side_effect=lambda prompt, default="": next(inputs, default),
        ), patch(
            "faffmonkey.cli.setup_provider._test_connection", return_value=True,
        ):
            from faffmonkey.cli.setup_provider import run_setup_provider
            run_setup_provider(install.state)

        config = load_config(install.config_path)
        assert config.models["main"].model == "some-model"
        assert config.resolve_model("conversation").model == "some-model"
        _wired(install)


class TestSearchWizard:
    def test_the_config_it_writes_loads_and_resolves_the_seam(
        self, install, monkeypatch,
    ):
        monkeypatch.setenv("BRAVE_API_KEY", "test-key")

        with patch(
            "faffmonkey.cli.setup_search._read_input",
            side_effect=lambda prompt, default="": default or "1",
        ):
            from faffmonkey.cli.setup_search import run_setup_search
            run_setup_search(install.state, base_dir=install.base)

        runtime = _wired(install)
        # The wizard's module string has to name a class that exists.
        from faffmonkey.seams.search_provider import SearchProvider
        assert isinstance(runtime.search_provider, SearchProvider)
        assert type(runtime.search_provider).__name__ == "BraveSearchProvider"


class TestVoiceWizard:
    def test_the_config_it_writes_loads_and_resolves_both_seams(
        self, install, monkeypatch,
    ):
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        answers = {
            "Enable transcription (speech-to-text)? [y/n]": "y",
            "Enable synthesis (text-to-speech)? [y/n]": "y",
        }

        with patch(
            "faffmonkey.cli.setup_voice._read_input",
            side_effect=lambda prompt, default="": answers.get(prompt, default),
        ):
            from faffmonkey.cli.setup_voice import run_setup_voice
            run_setup_voice(install.state, base_dir=install.base)

        runtime = _wired(install)
        assert type(runtime.transcriber).__name__ == "OpenAITranscriber"
        assert type(runtime.synthesiser).__name__ == "OpenAISynthesiser"


@pytest.mark.parametrize("channel", ["telegram", "discord"])
class TestChannelWizards:
    """Both channels, one contract. They were tested separately and drifted."""

    def _run(self, channel, install, user_id="12345"):
        module = f"faffmonkey.cli.setup_{channel}"
        inputs = iter(["y", user_id])
        with patch(f"{module}._read_input", side_effect=lambda p, d="": next(inputs, d)), \
             patch(f"{module}.getpass.getpass", return_value="a-token"), \
             patch(f"{module}._validate_token", return_value=True):
            run = getattr(
                __import__(module, fromlist=["run"]), f"run_setup_{channel}",
            )
            run(install.state, base_dir=install.base)

    def test_the_config_it_writes_loads(self, channel, install):
        self._run(channel, install)

        config = load_config(install.config_path)
        assert config.channels[channel].enabled is True
        assert config.channels[channel].allowed_users == ["12345"]

    def test_the_channel_resolves_to_a_real_class(self, channel, install):
        """No module key is written; BUILTIN_CHANNELS has to cover it."""
        import sys

        for mod in ("telegram", "telegram.ext", "discord"):
            sys.modules.setdefault(mod, MagicMock())

        self._run(channel, install)

        from faffmonkey.cli.__main__ import BUILTIN_CHANNELS
        config = load_config(install.config_path)
        assert config.channels[channel].module in ("", None)
        assert channel in BUILTIN_CHANNELS

    def test_it_refuses_to_write_over_a_broken_config(self, channel, install):
        """The channel wizards used to skip the schema check the others ran."""
        install.write_config({"models": {"main": {"provider": "p"}}, "tool_permisions": {}})

        with pytest.raises(SystemExit):
            self._run(channel, install)

    def test_a_non_numeric_user_id_is_reprompted(self, channel, install):
        module = f"faffmonkey.cli.setup_{channel}"
        inputs = iter(["y", "not-a-number", "999"])
        with patch(f"{module}._read_input", side_effect=lambda p, d="": next(inputs, d)), \
             patch(f"{module}.getpass.getpass", return_value="a-token"), \
             patch(f"{module}._validate_token", return_value=True):
            run = getattr(
                __import__(module, fromlist=["run"]), f"run_setup_{channel}",
            )
            run(install.state, base_dir=install.base)

        config = load_config(install.config_path)
        assert config.channels[channel].allowed_users == ["999"]

    def test_a_bad_token_aborts_before_anything_is_written(
        self, channel, install,
    ):
        env_path = install.state / ".env"
        env_path.write_text("EXISTING_API_KEY=keepme\n")
        before = json.loads(install.config_path.read_text())

        module = f"faffmonkey.cli.setup_{channel}"
        inputs = iter(["y", "12345"])
        with patch(f"{module}._read_input", side_effect=lambda p, d="": next(inputs, d)), \
             patch(f"{module}.getpass.getpass", return_value="bad"), \
             patch(f"{module}._validate_token", return_value=False):
            run = getattr(
                __import__(module, fromlist=["run"]), f"run_setup_{channel}",
            )
            with pytest.raises(SystemExit):
                run(install.state, base_dir=install.base)

        assert json.loads(install.config_path.read_text()) == before
        # The token must not reach .env, and what was there must survive.
        assert env_path.read_text() == "EXISTING_API_KEY=keepme\n"
