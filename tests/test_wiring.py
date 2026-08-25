import importlib
import importlib.machinery
import importlib.util
import json
import types

import pytest

from faffmonkey.seams.provider import Provider
from faffmonkey.seams.search_provider import SearchNotConfigured
from faffmonkey.seams.transcriber import NoopTranscriber, TranscriptionNotConfigured
from faffmonkey.wiring import (
    WiringError,
    _import_class,
    _resolve_allowed_dirs,
    _validate_protocol,
    _validate_spec_origin,
    wire,
)


class TestImportClass:
    def test_import_builtin(self):
        cls = _import_class("faffmonkey.seams.provider_openai_compat.OpenAICompatProvider")
        assert cls.__name__ == "OpenAICompatProvider"

    def test_invalid_path_no_dot(self):
        with pytest.raises(WiringError, match="invalid module path"):
            _import_class("NoDots")

    def test_class_not_found(self):
        with pytest.raises(WiringError, match="class.*not found"):
            _import_class("faffmonkey.seams.provider_openai_compat.DoesNotExist")

    def test_blocked_stdlib_import(self):
        with pytest.raises(WiringError, match="import blocked"):
            _import_class("os.path.join")

    def test_blocked_arbitrary_package(self):
        with pytest.raises(WiringError, match="import blocked"):
            _import_class("subprocess.Popen")

    def test_allowed_extensions_namespace(self, monkeypatch, tmp_path):
        workspace = tmp_path / "workspace"
        ext_dir = workspace / "extensions"
        ext_dir.mkdir(parents=True)
        fake_file = ext_dir / "channel_telegram.py"
        fake_file.write_text("")

        fake_module = types.ModuleType("extensions.channel_telegram")
        fake_module.__file__ = str(fake_file)
        fake_module.TelegramChannel = type("TelegramChannel", (), {})

        fake_spec = importlib.machinery.ModuleSpec("extensions.channel_telegram", None)
        fake_spec.origin = str(fake_file)
        monkeypatch.setattr(
            importlib.util, "find_spec",
            lambda name: fake_spec,
        )
        monkeypatch.setattr(
            importlib, "import_module",
            lambda name, *a, **kw: fake_module,
        )
        cls = _import_class(
            "extensions.channel_telegram.TelegramChannel", workspace=workspace,
        )
        assert cls.__name__ == "TelegramChannel"

    def test_allowed_contrib_namespace(self):
        with pytest.raises(WiringError, match="file not found"):
            _import_class("contrib.nonexistent.SomeClass")

    def test_file_not_found_extensions(self):
        with pytest.raises(WiringError, match="file not found"):
            _import_class("extensions.nonexistent_module.SomeClass")

    def test_dependency_not_installed(self, monkeypatch, tmp_path):
        workspace = tmp_path / "workspace"
        ext_dir = workspace / "extensions"
        ext_dir.mkdir(parents=True)
        fake_file = ext_dir / "vendor_module.py"
        fake_file.write_text("")

        fake_spec = importlib.machinery.ModuleSpec("extensions.vendor_module", None)
        fake_spec.origin = str(fake_file)
        monkeypatch.setattr(
            importlib.util, "find_spec",
            lambda name: fake_spec,
        )

        def fake_import(name, *args, **kwargs):
            if name == "extensions.vendor_module":
                raise ImportError("libfoo.so not found")
            raise ModuleNotFoundError(f"No module named '{name}'")

        monkeypatch.setattr(importlib, "import_module", fake_import)
        with pytest.raises(WiringError, match="dependency not installed"):
            _import_class(
                "extensions.vendor_module.FakeClass", workspace=workspace,
            )


class TestValidateProtocol:
    def test_valid_provider(self):
        from faffmonkey.seams.provider_openai_compat import OpenAICompatProvider

        instance = OpenAICompatProvider("http://test/v1")
        _validate_protocol(instance, Provider, "test-provider")

    def test_invalid_provider_missing_method(self):
        class BadProvider:
            pass

        with pytest.raises(WiringError, match="does not satisfy Provider"):
            _validate_protocol(BadProvider(), Provider, "bad-provider")


class TestWire:
    def test_wire_with_ollama(self, tmp_path):
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps({
            "models": {"main": {
                "provider": "ollama-local", "model": "llama3",
                "base_url": "http://localhost:11434/v1",
            }},
        }))
        runtime = wire(tmp_path)
        assert runtime.config.models["main"].provider == "ollama-local"

    def test_wire_noop_defaults(self, tmp_path):
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps({
            "models": {"main": {
                "provider": "ollama-local", "model": "llama3",
                "base_url": "http://localhost:11434/v1",
            }},
        }))
        runtime = wire(tmp_path)
        with pytest.raises(TranscriptionNotConfigured):
            runtime.transcriber.transcribe(b"", "")
        assert runtime.synthesiser.synthesise("") is None
        with pytest.raises(SearchNotConfigured):
            runtime.search_provider.search("test")


class TestResolveProvider:
    def test_resolves_provider_for_model(self, tmp_path):
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps({
            "models": {"main": {
                "provider": "ollama-local", "model": "llama3",
                "base_url": "http://localhost:11434/v1",
            }},
        }))
        runtime = wire(tmp_path)
        model = runtime.config.models["main"]
        provider = runtime.resolve_provider(model)
        assert isinstance(provider, Provider)

    def test_different_slots_get_different_providers(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps({
            "models": {
                "main": {
                    "provider": "openrouter",
                    "model": "google/gemini-2.5-flash",
                    "base_url": "https://openrouter.ai/api/v1",
                    "api_key_env": "OPENROUTER_API_KEY",
                },
                "cheap": {
                    "provider": "ollama-local",
                    "model": "llama3",
                    "base_url": "http://localhost:11434/v1",
                },
            },
        }))
        runtime = wire(tmp_path)

        main_provider = runtime.resolve_provider(runtime.config.models["main"])
        cheap_provider = runtime.resolve_provider(runtime.config.models["cheap"])

        assert main_provider.base_url == "https://openrouter.ai/api/v1"
        assert main_provider.api_key == "sk-test"
        assert cheap_provider.base_url == "http://localhost:11434/v1"
        assert cheap_provider.api_key == ""

    def test_defaults_to_openai_compat(self, tmp_path):
        from faffmonkey.seams.provider_openai_compat import OpenAICompatProvider

        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps({
            "models": {"main": {
                "provider": "ollama-local", "model": "llama3",
                "base_url": "http://localhost:11434/v1",
            }},
        }))
        runtime = wire(tmp_path)
        model = runtime.config.models["main"]
        provider = runtime.resolve_provider(model)
        assert isinstance(provider, OpenAICompatProvider)

    def test_module_field_uses_importlib(self, tmp_path):
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps({
            "models": {"main": {
                "provider": "custom",
                "model": "my-model",
                "base_url": "http://localhost:1234/v1",
                "module": "faffmonkey.seams.provider_openai_compat.OpenAICompatProvider",
            }},
        }))
        runtime = wire(tmp_path)
        model = runtime.config.models["main"]
        provider = runtime.resolve_provider(model)
        assert isinstance(provider, Provider)

    def test_any_provider_without_module_gets_openai_compat(self, tmp_path):
        from faffmonkey.seams.provider_openai_compat import OpenAICompatProvider

        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps({
            "models": {
                "main": {
                    "provider": "unknown_provider",
                    "model": "some-model",
                    "base_url": "http://localhost:1234/v1",
                },
            },
        }))
        runtime = wire(tmp_path)
        model = runtime.config.models["main"]
        provider = runtime.resolve_provider(model)
        assert isinstance(provider, OpenAICompatProvider)

    def test_validates_protocol(self, tmp_path, monkeypatch):
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps({
            "models": {"main": {
                "provider": "custom", "model": "llama3",
                "base_url": "http://localhost:11434/v1",
                "module": "faffmonkey.seams.provider_openai_compat.OpenAICompatProvider",
            }},
        }))
        runtime = wire(tmp_path)
        model = runtime.config.models["main"]

        class NotAProvider:
            def __init__(self, **kwargs):
                pass

        import faffmonkey.wiring
        monkeypatch.setattr(
            faffmonkey.wiring,
            "_import_class",
            lambda dotted_path, workspace=None: NotAProvider,
        )

        with pytest.raises(WiringError, match="does not satisfy Provider"):
            runtime.resolve_provider(model)


class TestImportPathVerification:
    def test_module_outside_allowed_dirs_rejected(self, monkeypatch, tmp_path):
        evil_path = str(tmp_path / "somewhere" / "evil.py")
        spec = importlib.util.spec_from_file_location("extensions.evil", evil_path)
        monkeypatch.setattr(
            importlib.util, "find_spec",
            lambda name: spec,
        )
        with pytest.raises(WiringError, match="outside allowed directories"):
            _import_class("extensions.evil.Evil")

    def test_module_inside_workspace_extensions_accepted(self, monkeypatch, tmp_path):
        ext_dir = tmp_path / "workspace" / "extensions"
        ext_dir.mkdir(parents=True)
        fake_file = ext_dir / "good.py"
        fake_file.write_text("class Good: pass")

        spec = importlib.util.spec_from_file_location("extensions.good", str(fake_file))
        monkeypatch.setattr(
            importlib.util, "find_spec",
            lambda name: spec,
        )
        fake_module = types.ModuleType("extensions.good")
        fake_module.__file__ = str(fake_file)
        fake_module.Good = type("Good", (), {})
        monkeypatch.setattr(
            importlib, "import_module",
            lambda name, *a, **kw: fake_module,
        )
        cls = _import_class(
            "extensions.good.Good", workspace=tmp_path / "workspace",
        )
        assert cls.__name__ == "Good"

    def test_module_with_no_origin_rejected(self, monkeypatch):
        fake_spec = importlib.machinery.ModuleSpec("extensions.builtin", None)
        fake_spec.origin = None
        monkeypatch.setattr(
            importlib.util, "find_spec",
            lambda name: fake_spec,
        )
        with pytest.raises(WiringError, match="no file origin"):
            _import_class("extensions.builtin.Builtin")

    def test_seams_namespace_path_accepted(self):
        cls = _import_class(
            "faffmonkey.seams.provider_openai_compat.OpenAICompatProvider",
        )
        assert cls.__name__ == "OpenAICompatProvider"


class TestFindSpecModuleNotFound:
    def test_find_spec_module_not_found_becomes_wiring_error(self, monkeypatch):
        def raise_not_found(name):
            raise ModuleNotFoundError(f"No module named '{name}'")

        monkeypatch.setattr(importlib.util, "find_spec", raise_not_found)
        with pytest.raises(WiringError, match="file not found for module"):
            _import_class("faffmonkey.seams.nonexistent_module.Foo")

    def test_find_spec_value_error_becomes_wiring_error(self, monkeypatch):
        def raise_value(name):
            raise ValueError("relative import without package")

        monkeypatch.setattr(importlib.util, "find_spec", raise_value)
        with pytest.raises(WiringError, match="file not found for module"):
            _import_class("faffmonkey.seams.provider_openai_compat.OpenAICompatProvider")


class TestInstantiationError:
    def test_provider_init_raises_becomes_wiring_error(self, tmp_path):
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps({
            "models": {"main": {
                "provider": "custom", "model": "llama3",
                "base_url": "http://localhost:11434/v1",
                "module": "faffmonkey.seams.provider_openai_compat.OpenAICompatProvider",
            }},
        }))
        runtime = wire(tmp_path)
        model = runtime.config.models["main"]

        class ExplodingProvider:
            def __init__(self, **kwargs):
                raise RuntimeError("kaboom")

        import faffmonkey.wiring
        original = faffmonkey.wiring._import_class

        def patched(dotted_path, workspace=None):
            return ExplodingProvider

        faffmonkey.wiring._import_class = patched
        try:
            with pytest.raises(WiringError, match="instantiation failed.*kaboom"):
                runtime.resolve_provider(model)
        finally:
            faffmonkey.wiring._import_class = original


class TestSpecOriginValidation:
    def test_spec_origin_outside_allowed_dirs_rejected(self, tmp_path, monkeypatch):
        evil_file = tmp_path / "evil" / "payload.py"
        evil_file.parent.mkdir(parents=True)
        evil_file.write_text("class Evil: pass")

        spec = importlib.util.spec_from_file_location(
            "extensions.evil", str(evil_file),
        )
        allowed_dirs = [tmp_path / "safe"]
        (tmp_path / "safe").mkdir()

        with pytest.raises(WiringError, match="outside allowed directories"):
            _validate_spec_origin("extensions.evil", spec, allowed_dirs)

    def test_spec_origin_inside_allowed_dir_passes(self, tmp_path):
        safe_dir = tmp_path / "extensions"
        safe_dir.mkdir()
        good_file = safe_dir / "good.py"
        good_file.write_text("class Good: pass")

        spec = importlib.util.spec_from_file_location(
            "extensions.good", str(good_file),
        )
        _validate_spec_origin("extensions.good", spec, [safe_dir])

    def test_spec_none_raises(self):
        with pytest.raises(WiringError, match="file not found"):
            _validate_spec_origin("extensions.ghost", None, [])

    def test_spec_origin_none_rejected(self):
        spec = importlib.machinery.ModuleSpec("extensions.namespace", None)
        spec.origin = None
        with pytest.raises(WiringError, match="no file origin"):
            _validate_spec_origin("extensions.namespace", spec, [])

    def test_nested_extension_blocked_before_find_spec(self, tmp_path):
        ext_dir = tmp_path / "workspace" / "extensions"
        ext_dir.mkdir(parents=True)
        malicious_pkg = ext_dir / "evil"
        malicious_pkg.mkdir()
        init_file = malicious_pkg / "__init__.py"
        init_file.write_text("raise RuntimeError('executed malicious code')")
        target_file = malicious_pkg / "payload.py"
        target_file.write_text("class Payload: pass")

        with pytest.raises(WiringError, match="nested packages under extensions/"):
            _import_class(
                "extensions.evil.payload.Payload",
                workspace=tmp_path / "workspace",
            )


class TestNonClassResolvesRaises:
    def test_function_attribute_rejected(self, monkeypatch, tmp_path):
        workspace = tmp_path / "workspace"
        ext_dir = workspace / "extensions"
        ext_dir.mkdir(parents=True)
        fake_file = ext_dir / "funcs.py"
        fake_file.write_text("")

        fake_module = types.ModuleType("extensions.funcs")
        fake_module.__file__ = str(fake_file)
        fake_module.not_a_class = lambda api_key="": None

        fake_spec = importlib.machinery.ModuleSpec("extensions.funcs", None)
        fake_spec.origin = str(fake_file)
        monkeypatch.setattr(
            importlib.util, "find_spec", lambda name: fake_spec,
        )
        monkeypatch.setattr(
            importlib, "import_module", lambda name, *a, **kw: fake_module,
        )
        with pytest.raises(WiringError, match="resolves to function, not a class"):
            _import_class(
                "extensions.funcs.not_a_class", workspace=workspace,
            )


class TestWireSearchProvider:
    def _write_config(self, tmp_path, search: dict) -> None:
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps({
            "models": {"main": {
                "provider": "ollama-local", "model": "llama3",
                "base_url": "http://localhost:11434/v1",
            }},
            "search": search,
        }))

    def test_missing_env_var_is_a_hard_error(self, tmp_path, monkeypatch):
        """Search configured without its key fails at startup, not per query.

        An empty key wired a real provider, so the agent started clean and
        advertised web_search to the model. The first search returned a tool
        error naming neither the env var nor the file it belongs in. Voice
        has behaved this way since the same defect was fixed there.
        """
        monkeypatch.delenv("SEARCH_TEST_API_KEY", raising=False)
        self._write_config(tmp_path, {
            "provider": "brave",
            "api_key_env": "SEARCH_TEST_API_KEY",
        })
        with pytest.raises(WiringError, match="SEARCH_TEST_API_KEY"):
            wire(tmp_path)

    def test_key_present_wires_the_provider(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SEARCH_TEST_API_KEY", "env-key")
        self._write_config(tmp_path, {
            "provider": "brave",
            "api_key_env": "SEARCH_TEST_API_KEY",
        })
        runtime = wire(tmp_path)
        from contrib.search_provider_brave import BraveSearchProvider
        assert isinstance(runtime.search_provider, BraveSearchProvider)

    def test_no_module_field_tries_the_extensions_path_first(self, tmp_path, monkeypatch):
        """The wizard writes an extensions path; the lookup table said contrib.

        An operator who drops the module field, thinking `provider: "brave"`
        is enough, hit a contrib path that is only importable when the repo
        is on sys.path, which an installed agent's is not.
        """
        monkeypatch.setenv("SEARCH_TEST_API_KEY", "env-key")
        self._write_config(tmp_path, {
            "provider": "brave",
            "api_key_env": "SEARCH_TEST_API_KEY",
        })
        from faffmonkey import wiring as wiring_module

        real_import = wiring_module._import_class
        tried = []

        def fake_import(path, workspace=None):
            tried.append(path)
            return real_import(path, workspace=workspace)

        monkeypatch.setattr(wiring_module, "_import_class", fake_import)
        runtime = wire(tmp_path)

        assert tried[0] == "extensions.search_provider_brave.BraveSearchProvider"
        assert tried[-1] == "contrib.search_provider_brave.BraveSearchProvider"
        from contrib.search_provider_brave import BraveSearchProvider
        assert isinstance(runtime.search_provider, BraveSearchProvider)

    def test_search_not_configured_is_unaffected(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SEARCH_TEST_API_KEY", raising=False)
        self._write_config(tmp_path, {})
        runtime = wire(tmp_path)
        with pytest.raises(SearchNotConfigured):
            runtime.search_provider.search("test")


class TestNestedExtensionsBlocked:
    def test_nested_package_rejected(self, tmp_path):
        workspace = tmp_path / "workspace"
        ext_dir = workspace / "extensions"
        ext_dir.mkdir(parents=True)

        with pytest.raises(WiringError, match="nested packages under extensions/"):
            _import_class(
                "extensions.pkg.mod.SomeClass", workspace=workspace,
            )

    def test_deeply_nested_package_rejected(self):
        with pytest.raises(WiringError, match="nested packages under extensions/"):
            _import_class("extensions.a.b.c.SomeClass")

    def test_flat_extension_allowed(self, monkeypatch, tmp_path):
        workspace = tmp_path / "workspace"
        ext_dir = workspace / "extensions"
        ext_dir.mkdir(parents=True)
        fake_file = ext_dir / "flat_module.py"
        fake_file.write_text("")

        fake_module = types.ModuleType("extensions.flat_module")
        fake_module.__file__ = str(fake_file)
        fake_module.Good = type("Good", (), {})

        fake_spec = importlib.machinery.ModuleSpec("extensions.flat_module", None)
        fake_spec.origin = str(fake_file)
        monkeypatch.setattr(
            importlib.util, "find_spec", lambda name: fake_spec,
        )
        monkeypatch.setattr(
            importlib, "import_module", lambda name, *a, **kw: fake_module,
        )
        cls = _import_class(
            "extensions.flat_module.Good", workspace=workspace,
        )
        assert cls.__name__ == "Good"

    def test_nested_rejected_before_find_spec_runs(self, monkeypatch, tmp_path):
        find_spec_called = False
        original_find_spec = importlib.util.find_spec

        def tracking_find_spec(name):
            nonlocal find_spec_called
            find_spec_called = True
            return original_find_spec(name)

        monkeypatch.setattr(importlib.util, "find_spec", tracking_find_spec)

        with pytest.raises(WiringError, match="nested packages under extensions/"):
            _import_class("extensions.evil.payload.Payload")
        assert not find_spec_called


class TestExtensionsInitBlocked:
    def test_init_py_exists_raises(self, tmp_path, monkeypatch):
        workspace = tmp_path / "workspace"
        ext_dir = workspace / "extensions"
        ext_dir.mkdir(parents=True)
        (ext_dir / "__init__.py").write_text("print('pwned')")

        with pytest.raises(WiringError, match="extensions/__init__.py exists"):
            _import_class(
                "extensions.some_module.SomeClass", workspace=workspace,
            )

    def test_init_py_symlink_raises(self, tmp_path, monkeypatch):
        workspace = tmp_path / "workspace"
        ext_dir = workspace / "extensions"
        ext_dir.mkdir(parents=True)
        real_file = tmp_path / "real_init.py"
        real_file.write_text("print('pwned')")
        (ext_dir / "__init__.py").symlink_to(real_file)

        with pytest.raises(WiringError, match="extensions/__init__.py exists"):
            _import_class(
                "extensions.some_module.SomeClass", workspace=workspace,
            )


class TestIntermediateSymlinkBlocked:
    def test_symlinked_subdirectory_rejected(self, tmp_path):
        allowed_dir = tmp_path / "extensions"
        allowed_dir.mkdir()
        real_subdir = allowed_dir / "real"
        real_subdir.mkdir()
        real_file = real_subdir / "payload.py"
        real_file.write_text("class Payload: pass")

        symlinked_sub = allowed_dir / "evil"
        symlinked_sub.symlink_to(real_subdir)

        spec = importlib.util.spec_from_file_location(
            "extensions.evil.payload",
            str(allowed_dir / "evil" / "payload.py"),
        )
        with pytest.raises(WiringError, match="symlink in import path"):
            _validate_spec_origin(
                "extensions.evil.payload", spec, [allowed_dir],
            )


class TestSymlinkedExtensionsDir:
    def test_symlinked_extensions_dir_rejected(self, tmp_path):
        real_dir = tmp_path / "real_extensions"
        real_dir.mkdir()
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        symlink = workspace / "extensions"
        symlink.symlink_to(real_dir)

        with pytest.raises(WiringError, match="extensions directory is a symlink"):
            _resolve_allowed_dirs(workspace)

    def test_real_extensions_dir_accepted(self, tmp_path):
        workspace = tmp_path / "workspace"
        ext_dir = workspace / "extensions"
        ext_dir.mkdir(parents=True)

        allowed = _resolve_allowed_dirs(workspace)
        assert ext_dir.resolve() in allowed

    def test_no_workspace_returns_base_dirs(self):
        allowed = _resolve_allowed_dirs(None)
        assert len(allowed) == 2


class TestWireVoice:
    def _write_config(self, tmp_path, voice: dict) -> None:
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps({
            "models": {"main": {
                "provider": "ollama-local", "model": "llama3",
                "base_url": "http://localhost:11434/v1",
            }},
            "voice": voice,
        }))

    def test_wires_openai_transcriber(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "env-key")
        self._write_config(tmp_path, {
            "transcriber": "openai",
            "transcriber_model": "whisper-1",
            "api_key_env": "OPENAI_API_KEY",
            "base_url": "https://api.example.com/v1",
        })
        runtime = wire(tmp_path)
        from contrib.transcriber_openai import OpenAITranscriber
        assert isinstance(runtime.transcriber, OpenAITranscriber)
        assert runtime.transcriber.api_key == "env-key"
        assert runtime.transcriber.base_url == "https://api.example.com/v1"
        assert runtime.transcriber.model == "whisper-1"

    def test_wires_openai_synthesiser(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "env-key")
        self._write_config(tmp_path, {
            "synthesiser": "openai",
            "synthesiser_model": "tts-1",
            "synthesiser_voice": "nova",
            "api_key_env": "OPENAI_API_KEY",
        })
        runtime = wire(tmp_path)
        from contrib.synthesiser_openai import OpenAISynthesiser
        assert isinstance(runtime.synthesiser, OpenAISynthesiser)
        assert runtime.synthesiser.api_key == "env-key"
        assert runtime.synthesiser.model == "tts-1"
        assert runtime.synthesiser.voice == "nova"

    def test_unknown_transcriber_falls_back_to_noop(self, tmp_path):
        self._write_config(tmp_path, {"transcriber": "nonexistent"})
        runtime = wire(tmp_path)
        with pytest.raises(TranscriptionNotConfigured):
            runtime.transcriber.transcribe(b"", "")

    def test_unknown_synthesiser_falls_back_to_noop(self, tmp_path):
        self._write_config(tmp_path, {"synthesiser": "nonexistent"})
        runtime = wire(tmp_path)
        assert runtime.synthesiser.synthesise("hello") is None

    def test_missing_env_var_is_a_hard_error(self, tmp_path, monkeypatch):
        """Voice configured without its key fails at startup, not per message.

        This previously asserted that a real transcriber was wired holding
        an empty key. It then failed on every audio message, and the user
        was told only that the message could not be transcribed. A missing
        model API key has always been a hard config error; this is the same
        mistake and now behaves the same way.
        """
        monkeypatch.delenv("VOICE_TEST_API_KEY", raising=False)
        self._write_config(tmp_path, {
            "transcriber": "openai",
            "api_key_env": "VOICE_TEST_API_KEY",
        })
        with pytest.raises(WiringError, match="VOICE_TEST_API_KEY"):
            wire(tmp_path)

    def test_voice_not_configured_is_unaffected(self, tmp_path, monkeypatch):
        monkeypatch.delenv("VOICE_TEST_API_KEY", raising=False)
        self._write_config(tmp_path, {})
        runtime = wire(tmp_path)
        assert isinstance(runtime.transcriber, NoopTranscriber)

    def test_protocol_violation_raises(self, tmp_path, monkeypatch):
        self._write_config(tmp_path, {"transcriber": "openai"})

        class NotATranscriber:
            def __init__(self, **kwargs):
                pass

        import faffmonkey.wiring as wiring_module
        monkeypatch.setattr(
            wiring_module, "_import_class",
            lambda path, workspace=None: NotATranscriber,
        )
        with pytest.raises(WiringError, match="does not satisfy Transcriber"):
            wire(tmp_path)

    def test_instantiation_failure_raises(self, tmp_path, monkeypatch):
        self._write_config(tmp_path, {"synthesiser": "openai"})

        class ExplodingSynthesiser:
            def __init__(self, **kwargs):
                raise ValueError("boom")

        import faffmonkey.wiring as wiring_module
        monkeypatch.setattr(
            wiring_module, "_import_class",
            lambda path, workspace=None: ExplodingSynthesiser,
        )
        with pytest.raises(WiringError, match="instantiation failed"):
            wire(tmp_path)


class TestExtensionsImportEndToEnd:
    def test_real_extensions_module_loads_through_wire(self, tmp_path):
        import sys

        base = tmp_path / "deploy"
        ext_dir = base / "extensions"
        ext_dir.mkdir(parents=True)
        (ext_dir / "voicetest_transcriber.py").write_text(
            "class FakeTranscriber:\n"
            "    def __init__(self, api_key='', base_url='', model=''):\n"
            "        self.model = model\n"
            "    def transcribe(self, audio, mime_type):\n"
            "        return 'transcribed'\n"
        )
        state_dir = base / "state"
        state_dir.mkdir()
        (state_dir / "config.json").write_text(json.dumps({
            "models": {"main": {
                "provider": "ollama-local", "model": "llama3",
                "base_url": "http://localhost:11434/v1",
            }},
            "voice": {
                "transcriber": "custom",
                "transcriber_module": (
                    "extensions.voicetest_transcriber.FakeTranscriber"
                ),
            },
        }))

        try:
            runtime = wire(state_dir, workspace=base)
            assert runtime.transcriber.transcribe(b"", "") == "transcribed"
        finally:
            if str(base) in sys.path:
                sys.path.remove(str(base))
            for name in list(sys.modules):
                if name == "extensions" or name.startswith("extensions."):
                    del sys.modules[name]
