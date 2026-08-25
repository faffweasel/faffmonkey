import os
from pathlib import Path

from faffmonkey.cli.setup_provider import (
    _append_env_var,
    _read_input,
    install_extension,
    merge_config,
)

SEARCH_PROVIDERS = [
    {
        "name": "Brave Search",
        "provider_key": "brave",
        "api_key_env": "BRAVE_API_KEY",
        "contrib_file": "search_provider_brave.py",
        "class": "BraveSearchProvider",
        "notes": "Get your API key at brave.com/search/api",
    },
]


def run_setup_search(
    state_dir: Path,
    base_dir: Path | None = None,
) -> None:
    if base_dir is None:
        base_dir = state_dir.parent

    print("Search Provider Setup")
    print("=" * 40)
    print()
    print("Choose a search provider:")
    for i, p in enumerate(SEARCH_PROVIDERS, 1):
        print(f"  {i}) {p['name']}")
    print()

    choice = _read_input("Provider number", "1")
    try:
        idx = int(choice) - 1
        if not 0 <= idx < len(SEARCH_PROVIDERS):
            raise ValueError
    except ValueError:
        print(f"Invalid choice: {choice}")
        raise SystemExit(1)

    provider = SEARCH_PROVIDERS[idx]
    print(f"\nSelected: {provider['name']}")
    print(f"  {provider['notes']}")

    contrib_file = provider["contrib_file"]
    install_extension(base_dir, contrib_file)

    api_key_env = provider["api_key_env"]
    api_key = os.environ.get(api_key_env, "")
    env_path = state_dir / ".env"
    if not api_key:
        api_key = _read_input(f"API key ({api_key_env})")
        if not api_key:
            print("API key is required.")
            raise SystemExit(1)
        _append_env_var(env_path, api_key_env, api_key)
        os.environ[api_key_env] = api_key
        print(f"  Saved to {env_path}")

    module = f"extensions.{contrib_file.removesuffix('.py')}.{provider['class']}"
    config_path = state_dir / "config.json"
    merge_config(config_path, "search", {
        "provider": provider["provider_key"],
        "module": module,
        "api_key_env": api_key_env,
    })
    print(f"\n  Config written to {config_path}")
    print(
        '\nSearch configured. The web_search tool is now active.'
    )
