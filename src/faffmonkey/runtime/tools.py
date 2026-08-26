from __future__ import annotations

import difflib
import fnmatch
import hashlib
import http.client
import ipaddress
import json
import logging
import os
import os.path
import re
import shlex
import signal
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path

from faffmonkey.runtime.blocklist import check_blocklist
from faffmonkey.runtime.ingest import ingest as ingest_content
from faffmonkey.runtime.retry import run_with_timeout
from faffmonkey.runtime.lint import lint_file
from faffmonkey.runtime.skills import (
    SKILL_TIMEOUT,
    invoke as skill_invoke,
    load_full as skill_load_full,
    skill_timeout,
)
from faffmonkey.seams.search_provider import SearchProvider
from faffmonkey.types import ToolCall, ToolResult

logger = logging.getLogger(__name__)

MAX_FETCH_BYTES = 50 * 1024
FETCH_TIMEOUT = 30
SHELL_TIMEOUT = 600
FILE_READ_TIMEOUT = 10
DNS_TIMEOUT = 5
MAX_LINES = 2000
MAX_OUTPUT_BYTES = 50 * 1024
MAX_DUMP_BYTES = 1024 * 1024
MAX_DUMP_FILES = 10
_CHUNK_SIZE = 64 * 1024
_MAX_MEMORY_BYTES = MAX_OUTPUT_BYTES * 2
_MAX_FILE_READ_BYTES = 10 * 1024 * 1024
_MAX_LIST_ENTRIES = 500
_MAX_FILE_READ_CONTENT_BYTES = 100 * 1024
_APPROVAL_TTL = 300
_GLOB_CHARS = frozenset('*?[')
# Sentinel for "this path did not exist when the command was approved".
# Not a possible sha256, so it cannot collide with a real hash.
_ABSENT = "absent"

_BLOCKED_NETWORKS = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("240.0.0.0/4"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("::/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("ff00::/8"),
]

_BLOCKED_HOSTNAMES = frozenset({
    "localhost",
    "metadata.google.internal",
    "metadata.internal",
    "instance-data",
})


def _validate_fetch_url(url: str) -> tuple[str | None, list[str]]:
    """Return (error_reason, resolved_ips).

    error_reason is not None when the URL targets a private/reserved address.
    resolved_ips contains validated IP address strings from DNS resolution.
    """
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return "invalid URL", []

    if parsed.scheme not in ("http", "https"):
        return f"scheme {parsed.scheme!r} is not allowed", []

    hostname = parsed.hostname
    if not hostname:
        return "missing hostname", []

    if hostname.lower().rstrip(".") in _BLOCKED_HOSTNAMES:
        return f"hostname {hostname!r} is blocked", []

    try:
        addrinfos = _getaddrinfo_with_timeout(hostname, parsed.port or 80)
    except socket.gaierror:
        return f"cannot resolve hostname {hostname!r}", []
    except TimeoutError:
        return f"DNS resolution timed out for {hostname!r}", []

    resolved_ips: list[str] = []
    for family, _, _, _, sockaddr in addrinfos:
        ip = ipaddress.ip_address(sockaddr[0])
        check_ips = [ip]
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            check_ips.append(ip.ipv4_mapped)
        for check_ip in check_ips:
            for net in _BLOCKED_NETWORKS:
                if check_ip in net:
                    return f"resolved IP {ip} is in blocked range {net}", []
        resolved_ips.append(sockaddr[0])

    if not resolved_ips:
        return f"no addresses resolved for {hostname!r}", []

    port = parsed.port
    if port is not None and port not in (80, 443, 8080, 8443):
        return f"port {port} is not allowed", []

    return None, resolved_ips


def _make_pinned_connection(resolved_ip: str):
    """Create HTTP/HTTPS connection classes pinned to a pre-resolved IP."""

    class PinnedHTTPConnection(http.client.HTTPConnection):
        def connect(self):
            self.sock = socket.create_connection(
                (resolved_ip, self.port), self.timeout,
            )

    class PinnedHTTPSConnection(http.client.HTTPSConnection):
        def connect(self):
            sock = socket.create_connection(
                (resolved_ip, self.port), self.timeout,
            )
            self.sock = self._context.wrap_socket(
                sock, server_hostname=self.host,
            )

    return PinnedHTTPConnection, PinnedHTTPSConnection


class _PinnedHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, conn_class):
        super().__init__()
        self._conn_class = conn_class

    def http_open(self, req):
        return self.do_open(self._conn_class, req)


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, conn_class):
        super().__init__()
        self._conn_class = conn_class

    def https_open(self, req):
        return self.do_open(self._conn_class, req)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            req.full_url, code,
            f"redirect to {newurl!r} blocked by SSRF protection",
            headers, fp,
        )


# Secrets, tool permissions and the cron schedule: the files that change
# what the agent is allowed to do. The identity files (SOUL.md, IDENTITY.md,
# USER.md, AGENTS.md, HEARTBEAT.md) and skills/ are deliberately not here;
# they are the agent's own. AGENTS.md carries the rule about asking before
# rewriting SOUL.md or IDENTITY.md, and chmod 444 is the opt-in for a file
# the operator wants frozen.
_PROTECTED_PATHS = frozenset({
    "state/.env".casefold(),
    "state/config.json".casefold(),
    "config/jobs.json".casefold(),
})

_OPERATOR_PREFIXES = ("extensions/".casefold(),)


def _is_operator_controlled(relative_path: str) -> bool:
    normalised = os.path.normpath(relative_path.replace("\\", "/")).casefold()
    for prefix in _OPERATOR_PREFIXES:
        if normalised.startswith(prefix):
            return True
    return False


def _protected_hint(relative_path: str) -> str:
    """What the agent should do instead. "Confirm with the user" implied an
    approval that would unlock the file; there is none, and the agent asked,
    was told yes, tried again, and then invented reasons it still failed."""
    normalised = os.path.normpath(relative_path.replace("\\", "/")).casefold()
    if normalised == "config/jobs.json":
        return (
            "Cron jobs are managed with the cron-manager skill: "
            "update <id> '<json patch>', add, remove, enable, disable. "
            "File tools never write this file and no approval changes that."
        )
    return (
        "The operator edits this file by hand; ask them to make the "
        "change. No approval unlocks it for file tools."
    )


def _is_protected(relative_path: str) -> bool:
    normalised = os.path.normpath(relative_path.replace("\\", "/")).casefold()
    if normalised in _PROTECTED_PATHS:
        return True
    return any(normalised.startswith(prefix) for prefix in _OPERATOR_PREFIXES)


_SMART_QUOTES = str.maketrans({
    "‘": "'", "’": "'",
    "“": '"', "”": '"',
})


def _normalise_fuzzy(text: str) -> str:
    lines = text.split("\n")
    stripped = [line.rstrip() for line in lines]
    return "\n".join(stripped).translate(_SMART_QUOTES)


def _detect_line_ending(raw: bytes) -> bytes:
    crlf = raw.count(b"\r\n")
    cr = raw.count(b"\r") - crlf
    lf = raw.count(b"\n") - crlf
    if crlf >= lf and crlf >= cr and crlf > 0:
        return b"\r\n"
    if cr >= lf and cr > 0:
        return b"\r"
    return b"\n"


def _find_unique(text: str, needle: str) -> int | None:
    first = text.find(needle)
    if first == -1:
        return None
    if text.find(needle, first + 1) != -1:
        return None
    return first


def _find_unique_fuzzy(text: str, needle: str) -> int | None:
    fuzzy_text = _normalise_fuzzy(text)
    fuzzy_needle = _normalise_fuzzy(needle)
    return _find_unique(fuzzy_text, fuzzy_needle)


def _count_fuzzy(text: str, needle: str) -> int:
    fuzzy_text = _normalise_fuzzy(text)
    fuzzy_needle = _normalise_fuzzy(needle)
    return fuzzy_text.count(fuzzy_needle)


def _match_length(text: str, needle: str, offset: int) -> int:
    if text[offset:offset + len(needle)] == needle:
        return len(needle)
    fuzzy_text = _normalise_fuzzy(text)
    fuzzy_needle = _normalise_fuzzy(needle)
    if fuzzy_text[offset:offset + len(fuzzy_needle)] == fuzzy_needle:
        end = offset + len(fuzzy_needle)
        while end < len(text) and _normalise_fuzzy(text[offset:end]) != fuzzy_needle:
            end += 1
        if end <= len(text):
            while end > offset and _normalise_fuzzy(text[offset:end]) == fuzzy_needle:
                test = end - 1
                if _normalise_fuzzy(text[offset:test]) == fuzzy_needle:
                    end = test
                else:
                    break
        return end - offset
    return len(needle)


def _unified_diff(old: str, new: str, filename: str) -> str:
    old_lines = old.splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)
    diff = difflib.unified_diff(old_lines, new_lines, fromfile=filename, tofile=filename)
    return "".join(diff)


TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "file_read",
            "description": "Read a file within workspace/. Returns first `limit` lines starting from `offset`.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path within workspace."},
                    "offset": {"type": "integer", "description": "Line offset to start reading from (0-based).", "default": 0},
                    "limit": {"type": "integer", "description": "Maximum number of lines to return.", "default": 2000},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "file_list",
            "description": "List one directory within the workspace: names, with / after directories and sizes after files. Not recursive. Use \".\" for the workspace root.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path relative to the workspace root.", "default": "."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "file_write",
            "description": "Write a file. Paths are relative to the workspace root (no workspace/ prefix); files for the user go in documents/. Post-write lint checks syntax.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path relative to the workspace root, e.g. documents/notes.md."},
                    "content": {"type": "string", "description": "File content to write."},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "file_edit",
            "description": "Apply surgical edits to a file within workspace/. Each edit replaces old_text with new_text (must match exactly once). Post-write lint checks syntax. Returns unified diff.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path within workspace."},
                    "edits": {
                        "type": "array",
                        "description": "List of {old_text, new_text} replacements.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "old_text": {"type": "string", "description": "Text to find (must match exactly once)."},
                                "new_text": {"type": "string", "description": "Replacement text."},
                            },
                            "required": ["old_text", "new_text"],
                        },
                    },
                },
                "required": ["path", "edits"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web via the configured search provider.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query."},
                    "max_results": {"type": "integer", "description": "Max results.", "default": 5},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "Fetch URL contents as text (50KB max, 30s timeout).",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to fetch."},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "shell_exec",
            "description": "Execute a shell command inside the container (600s timeout).",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to execute."},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skill_invoke",
            "description": (
                "Run a command from a skill's SKILL.md. This is the only way "
                "to run a skill's scripts; never run them through shell_exec. "
                "Example: name=\"digest-engine\", "
                "input=\"feed_fetch --digest NAME --json\"."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Skill name."},
                    "input": {
                        "type": "string",
                        "description": "The command line exactly as SKILL.md documents it.",
                        "default": "",
                    },
                },
                "required": ["name"],
            },
        },
    },
]


def validate_workspace_path(workspace: Path, relative_path: str) -> Path | None:
    if not isinstance(relative_path, str):
        return None
    try:
        resolved = (workspace / relative_path).resolve()
    except (ValueError, OSError):
        return None
    workspace_resolved = workspace.resolve()
    if not str(resolved).startswith(str(workspace_resolved) + "/") and resolved != workspace_resolved:
        return None
    return resolved


def _approval_key(command: str, cwd: str) -> str:
    raw = f"{command}\x00{cwd}"
    return hashlib.sha256(raw.encode()).hexdigest()


_CONTROL_CHAR_RE = re.compile(r"[^\x20-\x7e\n\t]")


def _sanitise_command(command: str) -> str:
    return _CONTROL_CHAR_RE.sub("", command)


def _read_file_with_timeout(path: Path, timeout: float = FILE_READ_TIMEOUT) -> str:
    return run_with_timeout(path.read_text, timeout, "file read")


def _getaddrinfo_with_timeout(hostname: str, port: int, timeout: float = DNS_TIMEOUT) -> list:
    return run_with_timeout(
        lambda: socket.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP),
        timeout,
        "DNS resolution",
    )


def _safe_write_text(path: Path, content: str) -> None:
    """Write using O_NOFOLLOW so the open itself fails if path is a symlink."""
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, 'w') as f:
        f.write(content)


def _hash_workspace_path(p: Path) -> str:
    """Hash a workspace file. For symlinks, include the link target in the hash."""
    if p.is_symlink():
        link_target = str(os.readlink(p)).encode()
        content = p.resolve().read_bytes()
        return hashlib.sha256(link_target + b'\x00' + content).hexdigest()
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _extract_workspace_file_hashes(command: str, workspace: Path) -> dict[str, str] | None:
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    workspace_resolved = workspace.resolve()
    ws_prefix = str(workspace_resolved) + "/"
    hashes: dict[str, str] = {}
    for token in tokens:
        if not token or token.startswith("-"):
            continue
        # Glob metacharacters in workspace-relative paths prevent reliable
        # file hashing: the set of matched files can change between approval
        # and execution. Return None to prevent caching the approval.
        if not token.startswith("/") and any(c in token for c in _GLOB_CHARS):
            return None
        raw_path = workspace / token
        try:
            candidate = raw_path.resolve()
        except (ValueError, OSError):
            continue
        if not str(candidate).startswith(ws_prefix):
            continue
        if candidate.is_file():
            try:
                hashes[str(raw_path)] = _hash_workspace_path(raw_path)
            except OSError:
                continue
        elif not candidate.exists() and ("/" in token or "." in token):
            # Record that it was absent. Skipping it meant an approval for
            # "bash deploy.sh", given while deploy.sh did not exist, still
            # stood after the agent wrote deploy.sh: the file was in no hash
            # map, so nothing compared it. An approval is a promise about
            # the filesystem the command will act on, and a path that did
            # not exist is a stronger reason to re-approve, not a weaker one.
            hashes[str(raw_path)] = _ABSENT
    return hashes


class ToolRegistry:
    def __init__(
        self,
        workspace: Path,
        permissions: dict[str, str],
        shell_preapproved: list[str],
        prompt_fn: Callable[[str], bool] | None = None,
        tz: str = "UTC",
        wrap: bool = True,
        search_provider: SearchProvider | None = None,
        shell_timeout: int = SHELL_TIMEOUT,
        state_dir: Path | None = None,
    ) -> None:
        self._workspace = workspace
        self._state_dir = state_dir
        self._permissions = dict(permissions)
        self._shell_preapproved = list(shell_preapproved)
        self._prompt_fn = prompt_fn
        self._tz = tz
        self._wrap = wrap
        self._search_provider = search_provider
        self._shell_timeout = shell_timeout
        self._approved: dict[str, tuple[dict[str, str], float]] = {}
        self._cmd_output_counter = 0
        tmp_dir = workspace / "tmp"
        if tmp_dir.is_dir():
            for p in tmp_dir.glob("cmd_output_*.txt"):
                try:
                    n = int(p.stem.split("_", 2)[2])
                    if n > self._cmd_output_counter:
                        self._cmd_output_counter = n
                except (ValueError, IndexError):
                    pass
        self._handlers: dict[str, Callable[[dict], ToolResult]] = {
            "file_read": self._handle_file_read,
            "file_list": self._handle_file_list,
            "file_write": self._handle_file_write,
            "file_edit": self._handle_file_edit,
            "web_search": self._handle_web_search,
            "web_fetch": self._handle_web_fetch,
            "shell_exec": self._handle_shell_exec,
            "skill_invoke": self._handle_skill_invoke,
        }

    def dispatch_timeout(self, call: ToolCall) -> float:
        """The outer dispatch ceiling for this call, above the tool's own
        budget. It must sit above the skill layer's declared timeouts, or
        the loop abandons the thread while the subprocess runs to
        completion and the result is lost and retried at full price.
        """
        margin = 30.0
        if call.name == "skill_invoke" and isinstance(call.arguments, dict):
            name = call.arguments.get("name")
            if isinstance(name, str) and name:
                return skill_timeout(self._workspace / "skills" / name) + margin
            return SKILL_TIMEOUT + margin
        if call.name == "shell_exec":
            return self._shell_timeout + margin
        return 120.0

    def dispatch(self, call: ToolCall) -> ToolResult:
        handler = self._handlers.get(call.name)
        if handler is None:
            return ToolResult(id=call.id, content=f"unknown tool: {call.name}", is_error=True)

        # Belt and braces for the provider's own coercion: a tool call
        # rehydrated from history could still carry a non-object, and
        # `call.arguments | {...}` below raises TypeError on one. An error
        # result lets the model correct itself; an exception kills the turn.
        if not isinstance(call.arguments, dict):
            return ToolResult(
                id=call.id,
                content=f"tool {call.name!r} needs a JSON object for arguments",
                is_error=True,
            )

        perm = self._permissions.get(call.name, "never")
        if perm == "never":
            return ToolResult(id=call.id, content=f"tool {call.name!r} is disabled", is_error=True)

        if perm not in ("never", "ask", "always"):
            logger.warning("unknown permission %r for tool %r, treating as 'never'", perm, call.name)
            return ToolResult(id=call.id, content=f"tool {call.name!r} is disabled", is_error=True)

        if perm == "ask" and not self._check_approval(call):
            return ToolResult(id=call.id, content="tool execution denied by user", is_error=True)

        if call.name == "shell_exec" and perm == "ask":
            toctou_err = self._verify_toctou(call)
            if toctou_err is not None:
                return ToolResult(id=call.id, content=toctou_err, is_error=True)

        return handler(call.arguments | {"_call_id": call.id})

    def _check_approval(self, call: ToolCall) -> bool:
        if call.name == "shell_exec":
            command = call.arguments.get("command", "")
            command = _sanitise_command(command)
            call.arguments["command"] = command

            if check_blocklist(command):
                logger.warning("blocklisted command: %s", command)
                return False

            for pattern in self._shell_preapproved:
                if fnmatch.fnmatch(command, pattern):
                    return True

            cwd = str(self._workspace)
            key = _approval_key(command, cwd)
            if key in self._approved:
                _, ts = self._approved[key]
                if time.monotonic() - ts > _APPROVAL_TTL:
                    del self._approved[key]
                else:
                    return True

            if self._prompt_fn is not None:
                display_cmd = command.replace("\n", "\\n").replace("\t", "\\t")
                if self._prompt_fn(f"shell_exec: {display_cmd}"):
                    file_hashes = _extract_workspace_file_hashes(
                        command, self._workspace,
                    )
                    if file_hashes is not None:
                        self._approved[key] = (file_hashes, time.monotonic())
                    return True
            return False

        if self._prompt_fn is not None:
            return self._prompt_fn(f"{call.name}: {call.arguments}")
        return False

    def _verify_toctou(self, call: ToolCall) -> str | None:
        command = call.arguments.get("command", "")
        cwd = str(self._workspace)
        key = _approval_key(command, cwd)
        entry = self._approved.get(key)
        if not entry:
            return None
        hashes, _ts = entry
        root = self._workspace.resolve()
        for path_str, old_hash in hashes.items():
            p = Path(path_str)
            if old_hash == _ABSENT:
                if not p.exists():
                    continue
                change = "created"
            else:
                try:
                    unchanged = _hash_workspace_path(p) == old_hash
                except OSError:
                    unchanged = False
                if unchanged:
                    continue
                change = "modified"
            del self._approved[key]
            try:
                rel = str(p.relative_to(root))
            except ValueError:
                rel = path_str
            return f"File {change} between approval and execution: {rel}. Re-approve to proceed."
        return None

    def _wrap_output(self, content: str) -> str:
        if self._wrap:
            return ingest_content(content)
        return content

    @staticmethod
    def _reap_children(pgid: int) -> None:
        for _ in range(16):
            try:
                pid, _ = os.waitpid(-pgid, os.WNOHANG)
                if pid == 0:
                    break
            except ChildProcessError:
                break

    def _handle_file_list(self, args: dict) -> ToolResult:
        """file_read could open any workspace file the agent could name and
        nothing let it find the names: shell_exec is denied under faff run,
        where no one can answer an ask, so over a channel the agent had no
        way to see what was in documents/ or memory/."""
        call_id = args["_call_id"]
        path_str = args.get("path", ".")
        if not isinstance(path_str, str) or not path_str:
            return ToolResult(id=call_id, content="'path' must be a non-empty string", is_error=True)
        resolved = validate_workspace_path(self._workspace, path_str)
        if resolved is None:
            return ToolResult(id=call_id, content=f"path rejected: {self._wrap_output(path_str)}", is_error=True)
        if (self._workspace / path_str).is_symlink():
            return ToolResult(id=call_id, content=f"path rejected: {self._wrap_output(path_str)} is a symlink", is_error=True)
        if not resolved.is_dir():
            return ToolResult(id=call_id, content=f"not a directory: {self._wrap_output(path_str)}", is_error=True)
        try:
            entries = sorted(resolved.iterdir(), key=lambda p: p.name)
        except OSError as e:
            return ToolResult(id=call_id, content=f"list error: {self._wrap_output(str(e))}", is_error=True)
        lines: list[str] = []
        for entry in entries[:_MAX_LIST_ENTRIES]:
            if entry.is_symlink():
                continue
            if entry.is_dir():
                lines.append(f"{entry.name}/")
            else:
                try:
                    lines.append(f"{entry.name}  {entry.stat().st_size}")
                except OSError:
                    lines.append(entry.name)
        if len(entries) > _MAX_LIST_ENTRIES:
            lines.append(f"[{len(entries) - _MAX_LIST_ENTRIES} more entries not shown]")
        if not lines:
            return ToolResult(id=call_id, content="(empty directory)")
        return ToolResult(id=call_id, content=self._wrap_output("\n".join(lines)))

    def _handle_file_read(self, args: dict) -> ToolResult:
        call_id = args["_call_id"]
        path_str = args.get("path")
        if not isinstance(path_str, str) or not path_str:
            return ToolResult(id=call_id, content="missing or invalid 'path' argument", is_error=True)
        offset = args.get("offset", 0)
        if not isinstance(offset, int) or isinstance(offset, bool):
            return ToolResult(id=call_id, content="'offset' must be an integer", is_error=True)
        limit = args.get("limit", MAX_LINES)
        if not isinstance(limit, int) or isinstance(limit, bool):
            return ToolResult(id=call_id, content="'limit' must be an integer", is_error=True)
        resolved = validate_workspace_path(self._workspace, path_str)
        if resolved is None:
            return ToolResult(id=call_id, content=f"path rejected: {self._wrap_output(path_str)}", is_error=True)
        if (self._workspace / path_str).is_symlink():
            return ToolResult(id=call_id, content=f"path rejected: {self._wrap_output(path_str)} is a symlink", is_error=True)
        if not resolved.exists():
            return ToolResult(id=call_id, content=f"file not found: {self._wrap_output(path_str)}", is_error=True)
        try:
            file_size = resolved.stat().st_size
        except OSError as e:
            return ToolResult(id=call_id, content=f"read error: {self._wrap_output(str(e))}", is_error=True)
        if file_size > _MAX_FILE_READ_BYTES:
            return ToolResult(
                id=call_id,
                content=f"file too large: {file_size} bytes exceeds {_MAX_FILE_READ_BYTES} byte limit",
                is_error=True,
            )
        try:
            content = _read_file_with_timeout(resolved)
        except TimeoutError:
            return ToolResult(id=call_id, content=f"file read timed out ({FILE_READ_TIMEOUT}s)", is_error=True)
        except OSError as e:
            return ToolResult(id=call_id, content=f"read error: {self._wrap_output(str(e))}", is_error=True)

        lines = content.splitlines(keepends=True)
        total = len(lines)
        selected = lines[offset:offset + limit]
        result_text = "".join(selected)

        if len(selected) < total:
            end = offset + len(selected)
            result_text += f"\n[Showing lines {offset + 1}-{end} of {total}. Use offset={end} to continue.]"

        result_bytes = result_text.encode("utf-8", errors="replace")
        if len(result_bytes) > _MAX_FILE_READ_CONTENT_BYTES:
            result_text = result_bytes[:_MAX_FILE_READ_CONTENT_BYTES].decode("utf-8", errors="replace")
            result_text += "\n[Content truncated at 100 KB]"

        return ToolResult(id=call_id, content=self._wrap_output(result_text))

    def _handle_file_write(self, args: dict) -> ToolResult:
        call_id = args["_call_id"]
        path_str = args.get("path")
        if not isinstance(path_str, str) or not path_str:
            return ToolResult(id=call_id, content="missing or invalid 'path' argument", is_error=True)
        content = args.get("content")
        if not isinstance(content, str):
            return ToolResult(id=call_id, content="missing or invalid 'content' argument", is_error=True)
        resolved = validate_workspace_path(self._workspace, path_str)
        if resolved is None:
            return ToolResult(id=call_id, content=f"path rejected: {self._wrap_output(path_str)}", is_error=True)

        if (self._workspace / path_str).is_symlink():
            return ToolResult(id=call_id, content=f"path rejected: {self._wrap_output(path_str)} is a symlink", is_error=True)

        workspace_resolved = self._workspace.resolve()
        rel_resolved = str(resolved.relative_to(workspace_resolved))
        # Every doc the model has read calls the directory "workspace/", so
        # it wrote workspace/cake.md and got workspace/workspace/cake.md.
        first = rel_resolved.split("/", 1)[0].casefold()
        if first == "workspace":
            inner = rel_resolved.split("/", 1)[1] if "/" in rel_resolved else ""
            return ToolResult(
                id=call_id,
                content=(
                    f"path rejected: {self._wrap_output(path_str)}. Paths are "
                    f"relative to the workspace root already; write to "
                    f"{self._wrap_output(inner or 'documents/<name>')} instead."
                ),
                is_error=True,
            )
        # Likewise "state/commands.json" became workspace/state/commands.json,
        # a file nothing reads, and the agent reported the job done.
        if first == "state":
            return ToolResult(
                id=call_id,
                content=(
                    f"path rejected: {self._wrap_output(path_str)}. state/ is "
                    "outside the workspace and only the operator edits it; "
                    "tell the user what to put there."
                ),
                is_error=True,
            )
        if _is_operator_controlled(rel_resolved):
            return ToolResult(
                id=call_id,
                content="extensions/ is operator-controlled and cannot be modified by file tools.",
                is_error=True,
            )
        if _is_protected(rel_resolved):
            return ToolResult(
                id=call_id,
                content=(
                    f"protected file: {self._wrap_output(path_str)}. "
                    f"{_protected_hint(rel_resolved)}"
                ),
                is_error=True,
            )

        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            _safe_write_text(resolved, content)
        except OSError as e:
            return ToolResult(id=call_id, content=f"write error: {self._wrap_output(str(e))}", is_error=True)

        lint_error = lint_file(resolved)
        if lint_error is not None:
            return ToolResult(
                id=call_id,
                content=self._wrap_output(
                    f"wrote {path_str} ({len(content)} bytes). lint warning: {lint_error}"
                ),
            )
        return ToolResult(id=call_id, content=f"wrote {self._wrap_output(path_str)} ({len(content)} bytes)")

    def _handle_file_edit(self, args: dict) -> ToolResult:
        call_id = args["_call_id"]
        path_str = args.get("path")
        if not isinstance(path_str, str) or not path_str:
            return ToolResult(id=call_id, content="missing or invalid 'path' argument", is_error=True)
        edits = args.get("edits")
        if not isinstance(edits, list) or not edits:
            return ToolResult(id=call_id, content="missing or invalid 'edits' argument", is_error=True)

        resolved = validate_workspace_path(self._workspace, path_str)
        if resolved is None:
            return ToolResult(id=call_id, content=f"path rejected: {self._wrap_output(path_str)}", is_error=True)
        if not resolved.exists():
            return ToolResult(id=call_id, content=f"file not found: {self._wrap_output(path_str)}", is_error=True)

        if (self._workspace / path_str).is_symlink():
            return ToolResult(id=call_id, content=f"path rejected: {self._wrap_output(path_str)} is a symlink", is_error=True)

        workspace_resolved = self._workspace.resolve()
        rel_resolved = str(resolved.relative_to(workspace_resolved))
        if _is_operator_controlled(rel_resolved):
            return ToolResult(
                id=call_id,
                content="extensions/ is operator-controlled and cannot be modified by file tools.",
                is_error=True,
            )
        if _is_protected(rel_resolved):
            return ToolResult(
                id=call_id,
                content=(
                    f"protected file: {self._wrap_output(path_str)}. "
                    f"{_protected_hint(rel_resolved)}"
                ),
                is_error=True,
            )

        for i, edit in enumerate(edits):
            if not isinstance(edit, dict):
                return ToolResult(id=call_id, content=f"edit {i}: not an object", is_error=True)
            if "old_text" not in edit or "new_text" not in edit:
                return ToolResult(id=call_id, content=f"edit {i}: missing old_text or new_text", is_error=True)
            if not isinstance(edit["old_text"], str) or not isinstance(edit["new_text"], str):
                return ToolResult(id=call_id, content=f"edit {i}: old_text and new_text must be strings", is_error=True)

        try:
            raw = resolved.read_bytes()
        except OSError as e:
            return ToolResult(id=call_id, content=f"read error: {e}", is_error=True)

        original_ending = _detect_line_ending(raw)
        content = raw.decode("utf-8", errors="replace")
        normalised = content.replace("\r\n", "\n").replace("\r", "\n")

        located: list[tuple[int, int, str]] = []
        for i, edit in enumerate(edits):
            old_text = edit["old_text"]
            new_text = edit["new_text"]

            old_norm = old_text.replace("\r\n", "\n").replace("\r", "\n")
            new_norm = new_text.replace("\r\n", "\n").replace("\r", "\n")

            if old_norm == new_norm:
                continue

            offset = _find_unique(normalised, old_norm)
            if offset is None:
                offset = _find_unique_fuzzy(normalised, old_norm)
            if offset is None:
                count = normalised.count(old_norm)
                if count == 0:
                    count = _count_fuzzy(normalised, old_norm)
                if count > 1:
                    return ToolResult(
                        id=call_id,
                        content=f"edit {i}: old_text appears {count} times, be more specific",
                        is_error=True,
                    )
                return ToolResult(
                    id=call_id,
                    content=f"edit {i}: old_text not found in file",
                    is_error=True,
                )

            match_len = _match_length(normalised, old_norm, offset)
            located.append((offset, match_len, new_norm))

        if not located:
            return ToolResult(id=call_id, content=f"no changes needed for {path_str}")

        located.sort(key=lambda x: x[0])
        for j in range(len(located) - 1):
            end_j = located[j][0] + located[j][1]
            start_next = located[j + 1][0]
            if end_j > start_next:
                return ToolResult(
                    id=call_id,
                    content=f"edits overlap at offset {start_next}",
                    is_error=True,
                )

        result = []
        prev_end = 0
        for offset, length, new_text in located:
            result.append(normalised[prev_end:offset])
            result.append(new_text)
            prev_end = offset + length
        result.append(normalised[prev_end:])
        new_content = "".join(result)

        if original_ending == b"\r\n":
            new_content = new_content.replace("\n", "\r\n")
        elif original_ending == b"\r":
            new_content = new_content.replace("\n", "\r")

        try:
            _safe_write_text(resolved, new_content)
        except OSError as e:
            return ToolResult(id=call_id, content=f"write error: {e}", is_error=True)

        diff = _unified_diff(content, new_content, path_str)

        lint_error = lint_file(resolved)
        if lint_error is not None:
            return ToolResult(
                id=call_id,
                content=self._wrap_output(
                    f"edited {path_str} ({len(located)} edit(s)). lint warning: {lint_error}\n{diff}"
                ),
            )
        return ToolResult(
            id=call_id,
            content=self._wrap_output(f"edited {path_str} ({len(located)} edit(s))\n{diff}"),
        )

    def _handle_web_search(self, args: dict) -> ToolResult:
        call_id = args["_call_id"]
        query = args.get("query")
        if not isinstance(query, str) or not query:
            return ToolResult(id=call_id, content="missing or invalid 'query' argument", is_error=True)
        max_results = args.get("max_results", 5)
        if not isinstance(max_results, int) or isinstance(max_results, bool):
            max_results = 5

        if self._search_provider is None:
            return ToolResult(
                id=call_id,
                content="web_search is not available. No search provider is configured.",
                is_error=True,
            )

        try:
            results = self._search_provider.search(query, max_results=max_results)
        except Exception as e:
            return ToolResult(id=call_id, content=f"search error: {self._wrap_output(str(e))}", is_error=True)

        if not results:
            return ToolResult(id=call_id, content="no results found")

        parts: list[str] = []
        for r in results:
            entry = f"**{r.title}**\n{r.url}\n{r.snippet}"
            parts.append(self._wrap_output(entry))
        return ToolResult(id=call_id, content="\n\n".join(parts))

    def _handle_web_fetch(self, args: dict) -> ToolResult:
        call_id = args["_call_id"]
        url = args.get("url")
        if not isinstance(url, str) or not url:
            return ToolResult(id=call_id, content="missing or invalid 'url' argument", is_error=True)

        blocked_reason, resolved_ips = _validate_fetch_url(url)
        if blocked_reason is not None:
            return ToolResult(
                id=call_id,
                content=f"web_fetch blocked: {self._wrap_output(blocked_reason)}. Only public HTTP/HTTPS URLs are allowed.",
                is_error=True,
            )

        http_conn, https_conn = _make_pinned_connection(resolved_ips[0])
        opener = urllib.request.build_opener(
            _PinnedHTTPHandler(http_conn),
            _PinnedHTTPSHandler(https_conn),
            _NoRedirectHandler,
        )

        try:
            req = urllib.request.Request(url, headers={"User-Agent": "faffmonkey/0.1"})
            with opener.open(req, timeout=FETCH_TIMEOUT) as resp:
                data = resp.read(MAX_FETCH_BYTES + 1)
                if len(data) > MAX_FETCH_BYTES:
                    body = (
                        data[:MAX_FETCH_BYTES].decode("utf-8", errors="replace")
                        + "\n\n[Content truncated at 50 KB]"
                    )
                    return ToolResult(
                        id=call_id,
                        content=self._wrap_output(body),
                    )
                return ToolResult(
                    id=call_id,
                    content=self._wrap_output(data.decode("utf-8", errors="replace")),
                )
        except urllib.error.HTTPError as e:
            return ToolResult(id=call_id, content=f"HTTP {e.code}: {self._wrap_output(str(e.reason))}", is_error=True)
        except urllib.error.URLError as e:
            return ToolResult(id=call_id, content=f"fetch error: {self._wrap_output(str(e.reason))}", is_error=True)
        except (TimeoutError, OSError) as e:
            return ToolResult(id=call_id, content=f"fetch error: {self._wrap_output(str(e))}", is_error=True)

    def _tail_truncate(self, output: str) -> str:
        lines = output.splitlines(keepends=True)
        needs_truncation = len(lines) > MAX_LINES or len(output.encode()) > MAX_OUTPUT_BYTES

        if not needs_truncation:
            return output

        tail = lines[-MAX_LINES:]
        tail_text = "".join(tail)
        tail_bytes = tail_text.encode("utf-8", errors="replace")
        if len(tail_bytes) > MAX_OUTPUT_BYTES:
            tail_text = tail_bytes[-MAX_OUTPUT_BYTES:].decode("utf-8", errors="replace")

        self._cmd_output_counter += 1
        tmp_dir = self._workspace / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)

        existing = sorted(tmp_dir.glob("cmd_output_*.txt"), key=lambda p: p.stat().st_mtime)
        while len(existing) >= MAX_DUMP_FILES:
            existing.pop(0).unlink(missing_ok=True)

        dump_path = tmp_dir / f"cmd_output_{self._cmd_output_counter}.txt"
        dump_bytes = output.encode("utf-8", errors="replace")[:MAX_DUMP_BYTES]
        dump_path.write_bytes(dump_bytes)

        relative = f"tmp/{dump_path.name}"
        return f"[Output truncated. Full output: {relative}]\n{tail_text}"

    def _shell_env(self) -> dict[str, str]:
        """The environment a shell command gets: inherited plus the seams.

        Deliberately NOT identical to a skill subprocess environment. A
        skill gets SKILL_DATA, which is derived from the skill's own name
        and whose directory invoke() creates; shell_exec has no skill name
        to derive it from, so there is no correct value to supply and the
        variable is absent here. Scripts that hard-exit without SKILL_DATA
        must be run through skill_invoke, not shell_exec.

        commands.json values are not in os.environ, so they are added here;
        otherwise a skill's instruction to "run $IMAGE_GEN_CMD" from a shell
        does nothing.
        """
        from faffmonkey.runtime.skills import load_commands

        state_dir = self._state_dir or (self._workspace.parent / "state")
        return {
            **os.environ,
            **load_commands(state_dir),
            "WORKSPACE": str(self._workspace),
            "TZ": self._tz,
        }

    def _handle_shell_exec(self, args: dict) -> ToolResult:
        call_id = args["_call_id"]
        command = args.get("command")
        if not isinstance(command, str) or not command:
            return ToolResult(id=call_id, content="missing or invalid 'command' argument", is_error=True)

        command = _sanitise_command(command)
        if not command:
            return ToolResult(id=call_id, content="missing or invalid 'command' argument", is_error=True)

        if check_blocklist(command):
            return ToolResult(id=call_id, content="command blocked by security policy", is_error=True)

        try:
            proc = subprocess.Popen(
                command,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(self._workspace),
                env=self._shell_env(),
                start_new_session=True,
            )

            stdout_chunks: list[bytes] = []
            stderr_chunks: list[bytes] = []
            total_bytes = [0]
            killed_for_memory = [False]
            lock = threading.Lock()

            def _read_pipe(pipe, chunks: list[bytes]) -> None:
                while True:
                    chunk = pipe.read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    with lock:
                        total_bytes[0] += len(chunk)
                        chunks.append(chunk)
                        if total_bytes[0] > _MAX_MEMORY_BYTES:
                            killed_for_memory[0] = True
                            try:
                                os.killpg(proc.pid, signal.SIGKILL)
                            except OSError:
                                proc.kill()
                            break

            t_out = threading.Thread(target=_read_pipe, args=(proc.stdout, stdout_chunks), daemon=True)
            t_err = threading.Thread(target=_read_pipe, args=(proc.stderr, stderr_chunks), daemon=True)
            t_out.start()
            t_err.start()

            try:
                proc.wait(timeout=self._shell_timeout)
            except subprocess.TimeoutExpired:
                # killpg won't catch double-forked daemons; container cgroup limits
                # are the real boundary for leaked processes.
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    proc.kill()
                proc.wait()
                self._reap_children(proc.pid)
                return ToolResult(id=call_id, content=f"command timed out ({self._shell_timeout}s)", is_error=True)

            t_out.join(timeout=5)
            t_err.join(timeout=5)

            stdout = b"".join(stdout_chunks).decode("utf-8", errors="replace")
            stderr = b"".join(stderr_chunks).decode("utf-8", errors="replace")

            output = stdout
            if stderr:
                output += ("\n" if output else "") + stderr
            if killed_for_memory[0]:
                output += "\n[Output killed: exceeded memory limit]"
            if proc.returncode != 0:
                output += f"\n[exit code: {proc.returncode}]"
            content = output or "(no output)"
            return ToolResult(id=call_id, content=self._wrap_output(self._tail_truncate(content)))
        except OSError as e:
            return ToolResult(id=call_id, content=f"exec error: {self._wrap_output(str(e))}", is_error=True)

    def _handle_skill_invoke(self, args: dict) -> ToolResult:
        call_id = args["_call_id"]
        name = args.get("name")
        if not isinstance(name, str) or not name:
            return ToolResult(id=call_id, content="missing or invalid 'name' argument", is_error=True)
        skill_input = args.get("input", "")
        if not isinstance(skill_input, str):
            return ToolResult(id=call_id, content="'input' must be a string", is_error=True)

        full_md = skill_load_full(self._workspace, name)
        if full_md is None:
            return ToolResult(id=call_id, content=f"skill not found: {name}", is_error=True)

        # shlex, not str.split: every documented skill call quotes its
        # arguments ("remind add \"call mum\" \"tomorrow 9am\""), and
        # whitespace splitting shreds them into unusable fragments.
        try:
            parts = shlex.split(skill_input) if skill_input else []
        except ValueError as e:
            return ToolResult(
                id=call_id,
                content=f"could not parse skill input: {e}",
                is_error=True,
            )
        action = parts[0] if parts else ""
        action_args = parts[1:] if len(parts) > 1 else []

        if not action:
            return ToolResult(
                id=call_id,
                content=self._wrap_output(self._tail_truncate(f"[SKILL.md for {name}]\n\n{full_md}")),
            )

        output, attachments, is_error = skill_invoke(
            self._workspace, name, action, action_args, tz=self._tz,
            state_dir=self._state_dir,
        )

        result_parts = [output]
        if attachments:
            result_parts.append("Attachments: " + ", ".join(str(a) for a in attachments))

        content = "\n".join(result_parts)
        content = self._tail_truncate(content)
        content = self._wrap_output(content)

        return ToolResult(
            id=call_id,
            content=content,
            is_error=is_error,
            attachments=attachments,
        )
