"""faff CLI entry point."""

import argparse
import inspect
import json
import signal
import sys
from pathlib import Path

# input() only offers line editing (backspace, arrows, history) once the
# readline module has been imported.
try:
    import readline
except ImportError:
    pass

from faffmonkey.cli.init import run_init
from faffmonkey.config import ConfigError, data_root
from faffmonkey.seams.channel_cli import discard_typeahead
from faffmonkey.wiring import WiringError

# Where a channel's implementation lands once its extension is installed.
# Module level and shared: doctor kept a private copy that had drifted to
# telegram only, so a discord config with no explicit module was reported
# broken while _build_channels resolved it fine.
BUILTIN_CHANNELS: dict[str, str] = {
    "telegram": "extensions.channel_telegram.TelegramChannel",
    "discord": "extensions.channel_discord.DiscordChannel",
}

# The contrib sources those extensions are installed from. Kept beside the
# map above so the two cannot drift.
CONTRIB_CHANNEL_SOURCES: dict[str, str] = {
    "telegram": "contrib.channel_telegram.TelegramChannel",
    "discord": "contrib.channel_discord.DiscordChannel",
}


def _state_dir_arg(value: str | None) -> Path:
    return Path(value).resolve() if value else data_root() / "state"


def _workspace_dir_arg(value: str | None) -> Path:
    return Path(value).resolve() if value else data_root() / "workspace"


def _base_dir_arg(value: str | None) -> Path:
    return Path(value).resolve() if value else data_root()


def _check_config_exists(state_dir: Path) -> bool:
    config_path = state_dir / "config.json"
    if not config_path.exists():
        # A pre-release install kept its data in the checkout; point at
        # the migration instead of suggesting a fresh init beside real data.
        try:
            from faffmonkey.cli.init import _find_project_root
            legacy = _find_project_root() / "state" / "config.json"
        except RuntimeError:
            legacy = None
        if legacy is not None and legacy.exists() and legacy.resolve() != config_path.resolve():
            print(f"Data found in {legacy.parent.parent} (the checkout).")
            print('Run "./bin/faff update" on the host to move it to the data root.')
            return False
        print('No config found. Run "faff init" to get started.')
        return False
    return True


def _check_provider_configured(state_dir: Path) -> bool:
    config_path = state_dir / "config.json"
    try:
        with open(config_path) as f:
            config = json.load(f)
    except (json.JSONDecodeError, OSError):
        print('Invalid config. Run "faff init" to recreate.')
        return False

    models = config.get("models", {})
    if not models or "main" not in models:
        print('No LLM provider configured. Run "faff setup provider".')
        return False

    main = models["main"]
    if not main.get("provider") or not main.get("model"):
        print('No LLM provider configured. Run "faff setup provider".')
        return False
    return True


def _require_config(state_dir: Path) -> bool:
    if not _check_config_exists(state_dir):
        return False
    if not _check_provider_configured(state_dir):
        return False
    return True


def cmd_init(args: argparse.Namespace) -> None:
    base_path = _base_dir_arg(args.path)
    run_init(base_path)


def cmd_setup(args: argparse.Namespace) -> None:
    state_dir = _state_dir_arg(args.state_dir)
    if not (state_dir / "config.json").exists():
        print('No config found. Run "faff init" first.')
        sys.exit(1)

    if args.setup_command == "provider":
        from faffmonkey.cli.setup_provider import run_setup_provider
        run_setup_provider(state_dir)
    elif args.setup_command == "search":
        from faffmonkey.cli.setup_search import run_setup_search
        run_setup_search(state_dir)
    elif args.setup_command == "telegram":
        from faffmonkey.cli.setup_telegram import run_setup_telegram
        run_setup_telegram(state_dir)
    elif args.setup_command == "discord":
        from faffmonkey.cli.setup_discord import run_setup_discord
        run_setup_discord(state_dir)
    elif args.setup_command == "voice":
        from faffmonkey.cli.setup_voice import run_setup_voice
        run_setup_voice(state_dir)
    else:
        print(f'Setup command "{args.setup_command}" is not yet implemented.')
        sys.exit(1)


def _cli_tool_prompt(description: str) -> bool:
    discard_typeahead()
    try:
        answer = input(f"  Allow {description}? [y/N] ")
        return answer.strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


def cmd_chat(args: argparse.Namespace) -> None:
    state_dir = _state_dir_arg(args.state_dir)
    workspace_dir = _workspace_dir_arg(args.workspace_dir)
    if not _require_config(state_dir):
        sys.exit(1)

    from faffmonkey.runtime.bootstrap import load_bootstrap
    from faffmonkey.runtime.loop import AgentLoop
    from faffmonkey.runtime.tools import ToolRegistry
    from faffmonkey.runtime.trust import load_trust_store
    from faffmonkey.seams.channel_cli import CLIChannel
    from faffmonkey.wiring import wire

    # wiring's workspace is the directory containing extensions/, i.e. the
    # project base, not the agent workspace
    runtime = wire(state_dir, workspace=state_dir.parent)
    trust_store = load_trust_store(state_dir)
    bootstrap = load_bootstrap(workspace_dir, runtime.config, mode="full", wrap=True, trust_store=trust_store)

    tool_registry = ToolRegistry(
        workspace=workspace_dir,
        permissions=runtime.config.tool_permissions,
        shell_preapproved=runtime.config.shell_preapproved,
        prompt_fn=_cli_tool_prompt,
        tz=str(runtime.config.timezone),
        wrap=True,
        search_provider=runtime.search_provider,
        state_dir=state_dir,
    )

    channel = CLIChannel()
    loop = AgentLoop(
        resolve_provider=runtime.resolve_provider,
        config=runtime.config,
        channel=channel,
        system_prompt=bootstrap.text,
        context_window=runtime.config.resolve_model("conversation").context_window,
        # Rebuilt per turn so the clock, daily logs and MEMORY.md keep
        # advancing in a long-running process.
        system_prompt_fn=lambda: load_bootstrap(
            workspace_dir, runtime.config, mode="full", wrap=True,
            trust_store=load_trust_store(state_dir),
        ).text,
        db_path=state_dir / "sessions.db",
        state_dir=state_dir,
        tool_registry=tool_registry,
        workspace=workspace_dir,
        debug=args.debug,
        allow_overflow=args.allow_overflow,
        bootstrap_file_tokens=bootstrap.file_tokens,
        transcriber=runtime.transcriber,
        synthesiser=runtime.synthesiser,
    )

    from faffmonkey import __version__
    print(f"faffmonkey v{__version__} — {runtime.config.models['main'].model}")
    print("Type /help for commands. Ctrl+C to exit.\n")
    try:
        loop.run()
    except KeyboardInterrupt:
        pass
    print("\nBye.")


def cmd_run(args: argparse.Namespace) -> None:
    state_dir = _state_dir_arg(args.state_dir)
    workspace_dir = _workspace_dir_arg(args.workspace_dir)
    if not (state_dir / "config.json").exists():
        print(
            "No config found. Run: "
            "docker compose run --rm faffmonkey faff init"
        )
        sys.exit(1)
    if not _check_provider_configured(state_dir):
        sys.exit(1)

    from faffmonkey.runtime.bootstrap import load_bootstrap
    from faffmonkey.runtime.loop import AgentLoop
    from faffmonkey.runtime.tools import ToolRegistry
    from faffmonkey.runtime.trust import load_trust_store
    from faffmonkey.wiring import wire

    base_dir = state_dir.parent
    runtime = wire(state_dir, workspace=base_dir)
    trust_store = load_trust_store(state_dir)
    bootstrap = load_bootstrap(workspace_dir, runtime.config, mode="full", wrap=True, trust_store=trust_store)
    db_path = state_dir / "sessions.db"

    channels = _build_channels(runtime.config, workspace_dir, base_dir)
    if not channels:
        print("No channels configured. Run faff chat for CLI, or faff setup telegram.")
        sys.exit(1)

    import threading
    from faffmonkey.runtime.scheduler import Scheduler, _main_session_lock, load_jobs
    from faffmonkey.runtime.session import MAIN_SESSION_KEY

    channel_events: dict[str, threading.Event] = {
        name: threading.Event() for name, _ch in channels
    }
    dirty_events: dict[str, threading.Event] = {
        name: threading.Event() for name, _ch in channels
    }

    channel_senders = {name: ch for name, ch in channels}
    scheduler = Scheduler(
        config=runtime.config,
        workspace=workspace_dir,
        state_dir=state_dir,
        resolve_provider=runtime.resolve_provider,
        channels=channel_senders,
        search_provider=runtime.search_provider,
        session_rotated_events=list(channel_events.values()),
        history_dirty_events=dirty_events,
    )

    jobs = load_jobs(workspace_dir)
    if jobs:
        enabled = [j for j in jobs if j.enabled]
        print(f"Cron scheduler: {len(enabled)} active job(s)")

    cron_thread = threading.Thread(target=scheduler.start, daemon=True)
    cron_thread.start()

    channel_threads: list[threading.Thread] = []

    def _shutdown_handler(signum: int, frame: object) -> None:
        scheduler.stop_and_wait(timeout=15)
        cron_thread.join(timeout=5)
        for t in channel_threads:
            t.join(timeout=5)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown_handler)
    signal.signal(signal.SIGINT, _shutdown_handler)
    for name, channel in channels:
        print(f"Starting channel: {name}")
        channel_tool_registry = ToolRegistry(
            workspace=workspace_dir,
            permissions=runtime.config.tool_permissions,
            shell_preapproved=runtime.config.shell_preapproved,
            prompt_fn=None,
            tz=str(runtime.config.timezone),
            wrap=True,
            search_provider=runtime.search_provider,
            state_dir=state_dir,
        )
        loop = AgentLoop(
            resolve_provider=runtime.resolve_provider,
            config=runtime.config,
            channel=channel,
            system_prompt=bootstrap.text,
        context_window=runtime.config.resolve_model("conversation").context_window,
        # Rebuilt per turn so the clock, daily logs and MEMORY.md keep
        # advancing in a long-running process.
        system_prompt_fn=lambda: load_bootstrap(
            workspace_dir, runtime.config, mode="full", wrap=True,
            trust_store=load_trust_store(state_dir),
        ).text,
            db_path=db_path,
            state_dir=state_dir,
            channel_id=name,
            # One conversation, however many channels: every loop shares
            # the main session, hears about every other loop's writes,
            # and tells the scheduler where the user last spoke.
            session_key=MAIN_SESSION_KEY,
            tool_registry=channel_tool_registry,
            workspace=workspace_dir,
            allow_overflow=args.allow_overflow,
            bootstrap_file_tokens=bootstrap.file_tokens,
            session_lock=_main_session_lock,
            session_rotated=channel_events[name],
            history_dirty=dirty_events[name],
            history_dirty_peers=[e for n, e in dirty_events.items() if n != name],
            on_activity=scheduler.note_activity,
            transcriber=runtime.transcriber,
            synthesiser=runtime.synthesiser,
        )
        t = threading.Thread(target=loop.run, daemon=True, name=f"channel-{name}")
        t.start()
        channel_threads.append(t)

    for t in channel_threads:
        t.join()


def _build_channels(
    config: "Config", workspace_dir: Path, base_dir: Path,
) -> list[tuple[str, object]]:
    from faffmonkey.wiring import _import_class, _validate_protocol
    from faffmonkey.seams.channel import Channel

    result: list[tuple[str, object]] = []
    for name, ch_config in config.channels.items():
        if not ch_config.enabled:
            continue
        module = ch_config.module or BUILTIN_CHANNELS.get(name, "")
        if not module:
            print(f"  Warning: no module for channel {name!r}, skipping")
            continue
        # A channel that cannot be built is one broken channel, not a dead
        # process; the others still start.
        try:
            cls = _import_class(module, workspace=base_dir)
            kwargs = {
                "allowed_users": ch_config.allowed_users,
                "workspace": workspace_dir,
            }
            # Only Discord takes it, and passing it blindly would break every
            # other channel's constructor.
            if "group_policy" in inspect.signature(cls).parameters:
                kwargs["group_policy"] = ch_config.group_policy
            instance = cls(**kwargs)
            _validate_protocol(instance, Channel, f"channel({name})")
        except Exception as e:
            print(f"  Warning: channel {name!r} failed to start: {e}")
            continue
        result.append((name, instance))
    return result


def cmd_status(args: argparse.Namespace) -> None:
    state_dir = _state_dir_arg(args.state_dir)
    workspace_dir = _workspace_dir_arg(args.workspace_dir)
    # _check_config_exists alone only proves the file is there. On a fresh
    # init it is there and has an empty models block, so run_status died
    # with a raw traceback before the user had done anything wrong.
    if not _require_config(state_dir):
        sys.exit(1)
    from faffmonkey.cli.status import run_status
    run_status(state_dir, workspace_dir)


def cmd_doctor(args: argparse.Namespace) -> None:
    base = _base_dir_arg(args.base_dir)
    from faffmonkey.cli.doctor import run_doctor
    sys.exit(run_doctor(base))


def cmd_cron(args: argparse.Namespace) -> None:
    state_dir = _state_dir_arg(args.state_dir)
    workspace_dir = _workspace_dir_arg(args.workspace_dir)

    if not _require_config(state_dir):
        sys.exit(1)

    if args.cron_command == "list":
        from faffmonkey.cli.cron import run_cron_list
        run_cron_list(state_dir, workspace_dir)

    elif args.cron_command == "run":
        from faffmonkey.cli.cron import run_cron_run
        sys.exit(run_cron_run(state_dir, workspace_dir, args.job_id))

    elif args.cron_command == "history":
        from faffmonkey.config import load_config
        from faffmonkey.cli.cron import run_cron_history
        run_cron_history(
            state_dir, args.job_id,
            tz=load_config(state_dir / "config.json").timezone,
        )

    else:
        print("Usage: faff cron [list|run <jobId>|history <jobId>]")
        sys.exit(1)


def cmd_trust(args: argparse.Namespace) -> None:
    state_dir = _state_dir_arg(args.state_dir)
    workspace_dir = _workspace_dir_arg(args.workspace_dir)

    if args.trust_command == "status":
        from faffmonkey.cli.trust import run_trust_status
        run_trust_status(state_dir, workspace_dir)
    else:
        from faffmonkey.cli.trust import run_trust
        sys.exit(run_trust(state_dir, workspace_dir, args.trust_command))


def cmd_untrust(args: argparse.Namespace) -> None:
    state_dir = _state_dir_arg(args.state_dir)
    workspace_dir = _workspace_dir_arg(args.workspace_dir)
    from faffmonkey.cli.trust import run_untrust
    sys.exit(run_untrust(state_dir, workspace_dir, args.path))


def cmd_export(args: argparse.Namespace) -> None:
    state_dir = _state_dir_arg(args.state_dir)
    from faffmonkey.cli.export import run_export
    sys.exit(run_export(state_dir, args.session, args.format, args.output))


def cmd_skill(args: argparse.Namespace) -> None:
    workspace_dir = _workspace_dir_arg(args.workspace_dir)

    if args.skill_command == "install":
        from faffmonkey.cli.skill import run_skill_install
        sys.exit(run_skill_install(workspace_dir, args.name, force=args.force))
    elif args.skill_command == "list":
        from faffmonkey.cli.skill import run_skill_list
        sys.exit(run_skill_list(workspace_dir))
    else:
        print("Usage: faff skill [install <name> [--force] | list]")
        sys.exit(1)


def cmd_backup(args: argparse.Namespace) -> None:
    base = _base_dir_arg(args.base_dir)
    from faffmonkey.cli.backup import run_backup
    sys.exit(run_backup(base))


def cmd_restore(args: argparse.Namespace) -> None:
    base = _base_dir_arg(args.base_dir)
    from faffmonkey.cli.backup import run_restore
    sys.exit(run_restore(base, args.snapshot, force=args.force))


def cmd_update(args: argparse.Namespace) -> None:
    base = _base_dir_arg(args.base_dir)
    from faffmonkey.cli.update import run_update
    sys.exit(run_update(base))


def cmd_update_extension(args: argparse.Namespace) -> None:
    base = _base_dir_arg(args.base_dir)
    from faffmonkey.cli.update import run_update_extension
    sys.exit(run_update_extension(base, args.name))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="faff",
        description="faffmonkey: a minimal, self-hosted personal AI agent",
    )
    parser.add_argument(
        "--debug", action="store_true", default=False,
        help="show tracebacks instead of a one-line error",
    )
    sub = parser.add_subparsers(dest="command")

    init_parser = sub.add_parser("init", help="initialise project structure")
    init_parser.add_argument(
        "--path", default=None, help="data root (default: $FAFF_HOME, i.e. ~/.faffmonkey)"
    )

    setup_parser = sub.add_parser("setup", help="configure components")
    setup_parser.add_argument(
        "setup_command",
        help="component to set up (provider, search, telegram, discord, voice)",
    )
    setup_parser.add_argument(
        "--state-dir", default=None, help="state directory (default: $FAFF_HOME/state)"
    )

    chat_parser = sub.add_parser("chat", help="interactive chat session")
    chat_parser.add_argument(
        "--state-dir", default=None, help="state directory (default: $FAFF_HOME/state)"
    )
    chat_parser.add_argument(
        "--workspace-dir", default=None,
        help="workspace directory (default: $FAFF_HOME/workspace)",
    )
    chat_parser.add_argument(
        "--debug", action="store_true", default=False,
        help="log LLM request/response shape to stderr",
    )
    chat_parser.add_argument(
        "--allow-overflow", action="store_true", default=False,
        help="skip bootstrap context budget check",
    )

    run_parser = sub.add_parser("run", help="start all channels, cron, and heartbeat")
    run_parser.add_argument(
        "--state-dir", default=None, help="state directory (default: $FAFF_HOME/state)"
    )
    run_parser.add_argument(
        "--workspace-dir", default=None,
        help="workspace directory (default: $FAFF_HOME/workspace)",
    )
    run_parser.add_argument(
        "--allow-overflow", action="store_true", default=False,
        help="skip bootstrap context budget check",
    )

    status_parser = sub.add_parser("status", help="runtime status and recent activity")
    status_parser.add_argument(
        "--state-dir", default=None, help="state directory (default: $FAFF_HOME/state)"
    )
    status_parser.add_argument(
        "--workspace-dir", default=None,
        help="workspace directory (default: $FAFF_HOME/workspace)",
    )

    doctor_parser = sub.add_parser("doctor", help="diagnose misconfigurations")
    doctor_parser.add_argument(
        "--base-dir", default=None, help="data root (default: $FAFF_HOME, i.e. ~/.faffmonkey)"
    )

    cron_parser = sub.add_parser("cron", help="cron job management")
    cron_parser.add_argument(
        "--state-dir", default=None, help="state directory (default: $FAFF_HOME/state)"
    )
    cron_parser.add_argument(
        "--workspace-dir", default=None,
        help="workspace directory (default: $FAFF_HOME/workspace)",
    )
    cron_sub = cron_parser.add_subparsers(dest="cron_command")
    cron_sub.add_parser("list", help="list all cron jobs")
    run_cron = cron_sub.add_parser("run", help="manually trigger a cron job")
    run_cron.add_argument("job_id", help="job ID to run")
    history = cron_sub.add_parser("history", help="show run history for a job")
    history.add_argument("job_id", help="job ID to show history for")

    trust_parser = sub.add_parser("trust", help="manage file trust status")
    trust_parser.add_argument(
        "trust_command", help="'status' or path to trust"
    )
    trust_parser.add_argument(
        "--state-dir", default=None, help="state directory (default: $FAFF_HOME/state)"
    )
    trust_parser.add_argument(
        "--workspace-dir", default=None,
        help="workspace directory (default: $FAFF_HOME/workspace)",
    )

    untrust_parser = sub.add_parser("untrust", help="revoke trust for a file")
    untrust_parser.add_argument("path", help="workspace-relative path to untrust")
    untrust_parser.add_argument(
        "--state-dir", default=None, help="state directory (default: $FAFF_HOME/state)"
    )
    untrust_parser.add_argument(
        "--workspace-dir", default=None,
        help="workspace directory (default: $FAFF_HOME/workspace)",
    )

    export_parser = sub.add_parser("export", help="export conversation history")
    export_parser.add_argument(
        "--session", default=None, help="session ID to export (default: active main session)"
    )
    export_parser.add_argument(
        "--format", choices=["json", "openai"], default="openai",
        help="output format (default: openai)",
    )
    export_parser.add_argument(
        "--output", default=None, help="output file path (default: stdout)"
    )
    export_parser.add_argument(
        "--state-dir", default=None, help="state directory (default: $FAFF_HOME/state)"
    )

    skill_parser = sub.add_parser("skill", help="install and list contrib skills")
    skill_parser.add_argument(
        "--workspace-dir", default=None,
        help="workspace directory (default: $FAFF_HOME/workspace)",
    )
    skill_sub = skill_parser.add_subparsers(dest="skill_command")
    skill_install = skill_sub.add_parser("install", help="install a contrib skill")
    skill_install.add_argument("name", help="skill name from contrib/skills/")
    skill_install.add_argument(
        "--force", action="store_true", default=False,
        help="overwrite a locally modified install",
    )
    skill_sub.add_parser("list", help="list installed and available skills")

    backup_parser = sub.add_parser("backup", help="backup state to tarball")
    backup_parser.add_argument(
        "--base-dir", default=None, help="data root (default: $FAFF_HOME, i.e. ~/.faffmonkey)"
    )

    restore_parser = sub.add_parser("restore", help="restore state from a backup tarball")
    restore_parser.add_argument(
        "snapshot", help="snapshot filename in state/backups/, or a path"
    )
    restore_parser.add_argument(
        "--base-dir", default=None, help="data root (default: $FAFF_HOME, i.e. ~/.faffmonkey)"
    )
    restore_parser.add_argument(
        "--force", action="store_true",
        help="skip snapshotting the current state before overwriting it",
    )

    update_parser = sub.add_parser("update", help="run pre-upgrade migrations")
    update_parser.add_argument(
        "--base-dir", default=None, help="data root (default: $FAFF_HOME, i.e. ~/.faffmonkey)"
    )

    update_ext_parser = sub.add_parser(
        "update-extension", help="update a contrib extension"
    )
    update_ext_parser.add_argument("name", help="extension name to update")
    update_ext_parser.add_argument(
        "--base-dir", default=None, help="data root (default: $FAFF_HOME, i.e. ~/.faffmonkey)"
    )

    return parser


COMMANDS = {
    "init": cmd_init,
    "setup": cmd_setup,
    "chat": cmd_chat,
    "run": cmd_run,
    "status": cmd_status,
    "doctor": cmd_doctor,
    "cron": cmd_cron,
    "trust": cmd_trust,
    "untrust": cmd_untrust,
    "export": cmd_export,
    "skill": cmd_skill,
    "backup": cmd_backup,
    "restore": cmd_restore,
    "update": cmd_update,
    "update-extension": cmd_update_extension,
}


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    try:
        COMMANDS[args.command](args)
    except (WiringError, ConfigError) as e:
        # A misconfiguration is an operator problem, not a crash. The
        # traceback is available under --debug for anyone who wants it.
        if getattr(args, "debug", False):
            raise
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
