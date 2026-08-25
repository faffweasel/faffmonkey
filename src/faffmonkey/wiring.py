import importlib
import importlib.machinery
import importlib.util
import inspect
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import faffmonkey.seams as _seams_pkg
from faffmonkey.config import Config, ModelConfig, load_config
from faffmonkey.seams.provider import Provider
from faffmonkey.seams.provider_openai_compat import OpenAICompatProvider
from faffmonkey.seams.search_provider import NoopSearchProvider, SearchProvider
from faffmonkey.seams.synthesiser import NoopSynthesiser, Synthesiser
from faffmonkey.seams.transcriber import NoopTranscriber, Transcriber

logger = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).resolve().parent
_SEAMS_DIR = Path(_seams_pkg.__file__).resolve().parent
_CONTRIB_DIR = _BASE_DIR.parent.parent / "contrib"


class WiringError(Exception):
    pass


_ALLOWED_IMPORT_PREFIXES = ("extensions.", "contrib.", "faffmonkey.seams.")


def _ensure_import_roots(workspace: Path | None) -> None:
    # extensions/ and contrib/ are namespace packages; their parent
    # directories must be on sys.path or find_spec cannot resolve them.
    # Appended (not prepended) so they can never shadow installed packages.
    roots = [_CONTRIB_DIR.parent]
    if workspace is not None:
        roots.append(workspace)
    for root in roots:
        path_str = str(root)
        if root.is_dir() and path_str not in sys.path:
            sys.path.append(path_str)


def _resolve_allowed_dirs(workspace: Path | None) -> list[Path]:
    allowed: list[Path] = [_SEAMS_DIR, _CONTRIB_DIR]
    if workspace is not None:
        ext_dir = workspace / "extensions"
        if ext_dir.is_symlink():
            raise WiringError(
                f"import blocked: extensions directory is a symlink: {ext_dir}"
            )
        allowed.append(ext_dir.resolve())
    return allowed


def _validate_spec_origin(
    module_path: str, spec: importlib.machinery.ModuleSpec | None,
    allowed_dirs: list[Path],
) -> None:
    if spec is None:
        raise WiringError(f"file not found for module {module_path!r}")
    if spec.origin == "frozen":
        return
    if spec.origin is None:
        raise WiringError(
            f"import blocked: {module_path!r} has no file origin "
            f"(namespace packages are not allowed)"
        )
    resolved = Path(spec.origin).resolve()
    if not any(resolved.is_relative_to(d) for d in allowed_dirs):
        raise WiringError(
            f"import blocked: {module_path!r} resolves to {resolved}, "
            f"which is outside allowed directories"
        )
    original = Path(spec.origin)
    for allowed_dir in allowed_dirs:
        try:
            relative = original.relative_to(allowed_dir)
        except ValueError:
            continue
        current = allowed_dir
        for part in relative.parts[:-1]:
            current = current / part
            if current.is_symlink():
                raise WiringError(f"symlink in import path: {current}")
        break


def _import_class(dotted_path: str, workspace: Path | None = None) -> type:
    module_path, _, class_name = dotted_path.rpartition(".")
    if not module_path:
        raise WiringError(f"invalid module path: {dotted_path!r}")
    if not any(module_path.startswith(p) for p in _ALLOWED_IMPORT_PREFIXES):
        raise WiringError(
            f"import blocked: {module_path!r} is not in an allowed namespace "
            f"(extensions.*, contrib.*, faffmonkey.seams.*)"
        )

    allowed_dirs = _resolve_allowed_dirs(workspace)

    if module_path.startswith("extensions."):
        ext_subpath = module_path[len("extensions."):]
        if "." in ext_subpath:
            raise WiringError(
                f"import blocked: nested packages under extensions/ "
                f"are not allowed (flat layout required): {module_path!r}"
            )
        if workspace is not None:
            ext_init = workspace / "extensions" / "__init__.py"
            if ext_init.exists() or ext_init.is_symlink():
                raise WiringError(
                    "extensions/__init__.py exists — refusing to import "
                    "(security: __init__.py executes on import)"
                )

    if module_path.startswith(("extensions.", "contrib.")):
        _ensure_import_roots(workspace)

    try:
        spec = importlib.util.find_spec(module_path)
    except (ModuleNotFoundError, ValueError) as e:
        raise WiringError(
            f"file not found for module {module_path!r}: {e}"
        ) from e
    _validate_spec_origin(module_path, spec, allowed_dirs)

    try:
        module = importlib.import_module(module_path)
    except ModuleNotFoundError as e:
        raise WiringError(
            f"file not found for {dotted_path!r}: {e}"
        ) from e
    except ImportError as e:
        raise WiringError(
            f"dependency not installed for {dotted_path!r}: {e}"
        ) from e

    cls = getattr(module, class_name, None)
    if cls is None:
        raise WiringError(
            f"class {class_name!r} not found in module {module_path!r}"
        )
    if not isinstance(cls, type):
        raise WiringError(
            f"{dotted_path} resolves to {type(cls).__name__}, not a class"
        )
    return cls


def _validate_protocol(instance: object, protocol: type, label: str) -> None:
    if not isinstance(instance, protocol):
        protocol_methods = {
            name for name in dir(protocol)
            if not name.startswith("_") and callable(getattr(protocol, name, None))
        }
        instance_methods = {
            name for name in dir(instance)
            if not name.startswith("_") and callable(getattr(instance, name, None))
        }
        missing = protocol_methods - instance_methods
        raise WiringError(
            f"{label} does not satisfy {protocol.__name__}: "
            f"missing methods: {sorted(missing)}"
        )


@dataclass
class Runtime:
    config: Config
    transcriber: Transcriber
    synthesiser: Synthesiser
    search_provider: SearchProvider
    workspace: Path | None = None

    def resolve_provider(self, model: ModelConfig) -> Provider:
        if model.module:
            cls = _import_class(model.module, workspace=self.workspace)
        else:
            cls = OpenAICompatProvider
        try:
            kwargs = {
                "base_url": model.base_url,
                "api_key": model.api_key,
                "timeout": model.timeout,
            }
            # Extension providers need not accept it; only pass it to those
            # that do, the same way _build_channels handles group_policy.
            if "allow_insecure" in inspect.signature(cls).parameters:
                kwargs["allow_insecure"] = model.allow_insecure
            instance = cls(**kwargs)
        except Exception as e:
            raise WiringError(
                f"provider({model.provider}) instantiation failed: {e}"
            ) from e
        _validate_protocol(instance, Provider, f"provider({model.provider})")
        return instance


# Ordered fallbacks for a config that names a provider but no module. The
# wizard writes the extensions path, so a hand-edited config that drops the
# module field has to resolve there first; contrib is only importable when
# the repo is on sys.path, which an installed agent's is not.
_SEARCH_PROVIDER_LOOKUP: dict[str, tuple[str, ...]] = {
    "brave": (
        "extensions.search_provider_brave.BraveSearchProvider",
        "contrib.search_provider_brave.BraveSearchProvider",
    ),
}

_TRANSCRIBER_LOOKUP: dict[str, str] = {
    "openai": "contrib.transcriber_openai.OpenAITranscriber",
}

_SYNTHESISER_LOOKUP: dict[str, str] = {
    "openai": "contrib.synthesiser_openai.OpenAISynthesiser",
}


def _voice_api_key(config: Config) -> str:
    """The voice API key, or a hard error when voice is enabled without one.

    A missing key must not wire a real transcriber holding an empty string,
    which fails on every audio message with nothing useful to say.

    This fires only when voice is actually configured. An install with no
    transcriber and no synthesiser never reaches here and is unaffected.
    """
    if not config.voice.api_key_env:
        return ""
    key = os.environ.get(config.voice.api_key_env, "")
    if not key:
        raise WiringError(
            f"voice is configured but {config.voice.api_key_env} is not set; "
            f"set it in state/.env or remove the voice block from config.json"
        )
    return key


def _wire_transcriber(config: Config, workspace: Path | None = None) -> Transcriber:
    if not config.voice.transcriber:
        return NoopTranscriber()

    module_path = config.voice.transcriber_module
    if not module_path:
        module_path = _TRANSCRIBER_LOOKUP.get(config.voice.transcriber, "")
    if not module_path:
        logger.warning("unknown transcriber: %s", config.voice.transcriber)
        return NoopTranscriber()

    cls = _import_class(module_path, workspace=workspace)
    try:
        instance = cls(
            api_key=_voice_api_key(config),
            base_url=config.voice.base_url,
            model=config.voice.transcriber_model,
        )
    except Exception as e:
        raise WiringError(
            f"transcriber({config.voice.transcriber}) instantiation failed: {e}"
        ) from e
    _validate_protocol(instance, Transcriber, f"transcriber({config.voice.transcriber})")
    return instance


def _wire_synthesiser(config: Config, workspace: Path | None = None) -> Synthesiser:
    if not config.voice.synthesiser:
        return NoopSynthesiser()

    module_path = config.voice.synthesiser_module
    if not module_path:
        module_path = _SYNTHESISER_LOOKUP.get(config.voice.synthesiser, "")
    if not module_path:
        logger.warning("unknown synthesiser: %s", config.voice.synthesiser)
        return NoopSynthesiser()

    cls = _import_class(module_path, workspace=workspace)
    try:
        instance = cls(
            api_key=_voice_api_key(config),
            base_url=config.voice.base_url,
            model=config.voice.synthesiser_model,
            voice=config.voice.synthesiser_voice,
        )
    except Exception as e:
        raise WiringError(
            f"synthesiser({config.voice.synthesiser}) instantiation failed: {e}"
        ) from e
    _validate_protocol(instance, Synthesiser, f"synthesiser({config.voice.synthesiser})")
    return instance


def _wire_search_provider(config: Config, workspace: Path | None = None) -> SearchProvider:
    if not config.search.provider:
        return NoopSearchProvider()

    candidates = (
        (config.search.module,) if config.search.module
        else _SEARCH_PROVIDER_LOOKUP.get(config.search.provider, ())
    )
    if not candidates:
        logger.warning("unknown search provider: %s", config.search.provider)
        return NoopSearchProvider()

    api_key = ""
    if config.search.api_key_env:
        # Same posture as _voice_api_key. An empty key wired a real provider
        # that started clean, advertised web_search to the model, and failed
        # on the first call with a message naming neither the env var nor
        # the file it belongs in.
        api_key = os.environ.get(config.search.api_key_env, "")
        if not api_key:
            raise WiringError(
                f"search is configured but {config.search.api_key_env} is not set; "
                f"set it in state/.env or remove the search block from config.json"
            )

    cls = None
    last_error: WiringError | None = None
    for module_path in candidates:
        try:
            cls = _import_class(module_path, workspace=workspace)
            break
        except WiringError as e:
            last_error = e
    if cls is None:
        raise last_error or WiringError(
            f"search({config.search.provider}) has no importable module"
        )

    try:
        instance = cls(api_key=api_key)
    except Exception as e:
        raise WiringError(
            f"search({config.search.provider}) instantiation failed: {e}"
        ) from e
    _validate_protocol(instance, SearchProvider, f"search({config.search.provider})")
    return instance


def wire(state_dir: Path, workspace: Path | None = None) -> Runtime:
    config = load_config(state_dir / "config.json")
    return Runtime(
        config=config,
        transcriber=_wire_transcriber(config, workspace=workspace),
        synthesiser=_wire_synthesiser(config, workspace=workspace),
        search_provider=_wire_search_provider(config, workspace=workspace),
        workspace=workspace,
    )
