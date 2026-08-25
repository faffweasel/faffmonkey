import getpass
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from faffmonkey.cli.setup_provider import (
    _append_env_var,
    _read_input,
    _sanitise_display,
    ensure_default_jobs,
    install_extension,
    merge_config,
)


def _validate_token(token: str) -> bool:
    url = f"https://api.telegram.org/bot{urllib.parse.quote(token, safe='')}/getMe"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            if data.get("ok"):
                bot = data.get("result", {})
                name = _sanitise_display(bot.get("first_name", "unknown"))
                username = _sanitise_display(bot.get("username", "unknown"))
                print(f"  Bot validated: {name} (@{username})")
                return True
            print("  Token validation failed: API returned ok=false")
            return False
    except urllib.error.HTTPError as e:
        print(f"  Token validation failed: HTTP {e.code}")
        return False
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        print(f"  Token validation failed: {type(e).__name__}")
        return False


def run_setup_telegram(
    state_dir: Path,
    base_dir: Path | None = None,
) -> None:
    if base_dir is None:
        base_dir = state_dir.parent

    install_extension(
        base_dir,
        "channel_telegram.py",
        dep_line="python-telegram-bot>=21,<22",
        confirm_prompt=(
            "Telegram requires the python-telegram-bot package. "
            "This will copy the extension from contrib/ and add the "
            "dependency to requirements.extra.txt. Continue? [y/n]"
        ),
        read_input=_read_input,
    )

    print()
    print("Open Telegram, message @BotFather, send /newbot, follow the prompts, paste the token here")
    try:
        token = getpass.getpass("Bot token: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        raise SystemExit(1)
    if not token:
        print("Bot token is required.")
        raise SystemExit(1)

    print("\nValidating bot token...")
    if not _validate_token(token):
        print("\nToken validation failed. Check the token and try again.")
        raise SystemExit(1)

    env_path = state_dir / ".env"
    _append_env_var(env_path, "TELEGRAM_BOT_TOKEN", token)
    os.environ["TELEGRAM_BOT_TOKEN"] = token
    print(f"  Saved to {env_path}")

    print()
    print("Message @userinfobot on Telegram, paste the number here")
    while True:
        user_id = _read_input("Your Telegram user ID")
        if not user_id:
            print("User ID is required.")
            raise SystemExit(1)
        try:
            user_id = str(int(user_id))
            break
        except ValueError:
            print("  User ID must be a number. Try again.")

    config_path = state_dir / "config.json"
    # No "module": BUILTIN_CHANNELS in cli/__main__.py maps the name to the
    # installed extension path.
    merge_config(config_path, "channels", {
        "enabled": True,
        "allowed_users": [user_id],
    }, subkey="telegram")
    print(f"  Config updated: {config_path}")
    ensure_default_jobs(base_dir / "workspace")

    print('\nTelegram configured. Run "faff run" to start the agent.')
