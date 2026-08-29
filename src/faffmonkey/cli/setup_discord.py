import getpass
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from faffmonkey import __version__
from faffmonkey.cli.setup_provider import (
    _append_env_var,
    _read_input,
    _sanitise_display,
    ensure_default_jobs,
    install_extension,
    merge_config,
)


def _validate_token(token: str) -> bool:
    """Check the bot token against Discord before writing it."""
    req = urllib.request.Request(
        "https://discord.com/api/v10/users/@me",
        headers={
            "Authorization": f"Bot {token}",
            # Discord's edge returns 403 to urllib's default User-Agent
            # before the token is checked.
            "User-Agent": f"faffmonkey/{__version__}",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            username = _sanitise_display(str(data.get("username", "unknown")))
            print(f"  Bot validated: {username}")
            return True
    except urllib.error.HTTPError as e:
        print(f"  Token validation failed: HTTP {e.code}")
        return False
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as e:
        print(f"  Token validation failed: {type(e).__name__}")
        return False


def run_setup_discord(
    state_dir: Path,
    base_dir: Path | None = None,
) -> None:
    if base_dir is None:
        base_dir = state_dir.parent

    install_extension(
        base_dir,
        "channel_discord.py",
        dep_line="discord.py>=2,<3",
        confirm_prompt=(
            "Discord requires the discord.py package. "
            "This will copy the extension from contrib/ and add the "
            "dependency to requirements.extra.txt. Continue? [y/n]"
        ),
        read_input=_read_input,
    )

    print()
    print("To create a Discord bot:")
    print("  1. Go to https://discord.com/developers/applications")
    print("  2. Click 'New Application', give it a name, click 'Create'")
    print("  3. Go to the 'Bot' tab")
    print("  4. Under 'Privileged Gateway Intents', enable 'Message Content Intent'")
    print("  5. Click 'Reset Token' to generate a bot token, paste it below")
    print()
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
    _append_env_var(env_path, "DISCORD_BOT_TOKEN", token)
    os.environ["DISCORD_BOT_TOKEN"] = token
    print(f"  Saved to {env_path}")

    print()
    print("To find your Discord user ID:")
    print("  1. Open Discord Settings > Advanced > enable 'Developer Mode'")
    print("  2. Right-click your username, click 'Copy User ID'")
    while True:
        user_id = _read_input("Your Discord user ID")
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
        "group_policy": "mention",
    }, subkey="discord")
    print(f"  Config updated: {config_path}")
    ensure_default_jobs(base_dir / "workspace")

    print()
    print("To invite the bot to your server:")
    print("  1. Go to your application in the Developer Portal")
    print("  2. Go to the 'OAuth2' tab")
    print("  3. Under 'OAuth2 URL Generator', select the 'bot' scope")
    print("  4. Under 'Bot Permissions', select 'Send Messages' and 'Read Message History'")
    print("  5. Copy the generated URL and open it in your browser")
    print()
    print("Note: bot responses in guild channels are visible to all channel members,")
    print("not just allowed_users. Use group_policy 'mention' (default) to limit when")
    print("the bot responds.")
    print()
    print('Discord configured. Run "faff run" to start the agent.')
