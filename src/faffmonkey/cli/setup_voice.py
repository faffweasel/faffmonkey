import getpass
import os
from pathlib import Path

from faffmonkey.cli.setup_provider import (
    _append_env_var,
    _read_api_key_env_name,
    _read_input,
    install_extension,
    merge_config,
)
from faffmonkey.config import validate_base_url

TRANSCRIBER_CONTRIB = "transcriber_openai.py"
SYNTHESISER_CONTRIB = "synthesiser_openai.py"


def _yes(prompt: str, default: str = "y") -> bool:
    return _read_input(f"{prompt} [y/n]", default).strip().lower() in ("y", "yes")


def run_setup_voice(
    state_dir: Path,
    base_dir: Path | None = None,
) -> None:
    if base_dir is None:
        base_dir = state_dir.parent

    print("Voice Setup (OpenAI-compatible STT/TTS)")
    print("=" * 40)
    print()
    print("Transcription turns inbound voice messages into text.")
    print("Synthesis speaks replies back when you send a voice message.")
    print("Both use an OpenAI-compatible API. No extra pip packages needed.")
    print()

    want_stt = _yes("Enable transcription (speech-to-text)?")
    want_tts = _yes("Enable synthesis (text-to-speech)?")
    if not want_stt and not want_tts:
        print("Nothing to set up.")
        return

    api_key_env = _read_api_key_env_name("OPENAI_API_KEY", read_input=_read_input)

    env_path = state_dir / ".env"
    if not os.environ.get(api_key_env, ""):
        # The provider and channel wizards read secrets with getpass; this
        # one echoed the key to the terminal as it was typed.
        try:
            api_key = getpass.getpass(f"API key ({api_key_env}): ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            raise SystemExit(1)
        if not api_key:
            print("API key is required.")
            raise SystemExit(1)
        _append_env_var(env_path, api_key_env, api_key)
        os.environ[api_key_env] = api_key
        print(f"  Saved to {env_path}")

    base_url = _read_input("API base URL", "https://api.openai.com/v1")
    url_err = validate_base_url(base_url)
    if url_err is not None:
        print(f"  Invalid base URL: {url_err}")
        raise SystemExit(1)

    voice: dict = {"api_key_env": api_key_env, "base_url": base_url}

    if want_stt:
        install_extension(base_dir, TRANSCRIBER_CONTRIB)
        voice["transcriber"] = "openai"
        voice["transcriber_module"] = "extensions.transcriber_openai.OpenAITranscriber"
        voice["transcriber_model"] = _read_input("Transcription model", "whisper-1")

    if want_tts:
        install_extension(base_dir, SYNTHESISER_CONTRIB)
        voice["synthesiser"] = "openai"
        voice["synthesiser_module"] = "extensions.synthesiser_openai.OpenAISynthesiser"
        voice["synthesiser_model"] = _read_input("Synthesis model", "tts-1")
        voice["synthesiser_voice"] = _read_input("Synthesis voice", "alloy")

    config_path = state_dir / "config.json"
    merge_config(config_path, "voice", voice)
    print(f"\n  Config written to {config_path}")
    print("  If running in Docker, rebuild the image: docker compose build")
    print("\nVoice configured.")
