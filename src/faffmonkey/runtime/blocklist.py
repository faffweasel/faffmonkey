"""Hardline command blocklist: defence-in-depth inside a container boundary.

Container isolation is the actual security boundary. This blocklist catches
obvious destructive, persistence, and anti-forensics patterns before they
reach tool-permission checking. It is NOT a sandbox and does not attempt
broad exfiltration prevention.
"""

import re
import shlex

_ANSI_C_RE = re.compile(
    r'\\x([0-9a-fA-F]{1,2})'
    r'|\\u([0-9a-fA-F]{4})'
    r'|\\U([0-9a-fA-F]{8})'
    r'|\\([0-7]{1,3})'
    r"|\\([abfnrtv\\'\"])"
)

_SIMPLE_ESCAPES: dict[str, str] = {
    'a': '\a', 'b': '\b', 'f': '\f', 'n': '\n',
    'r': '\r', 't': '\t', 'v': '\v',
    '\\': '\\', "'": "'", '"': '"',
}

_BRACE_RE = re.compile(r"\{([^}]*,)[^}]*\}")
_SHELL_VAR_EXPAND_RE = re.compile(
    r"\$\{[A-Z_][A-Z0-9_]*:?[-+=]([^}]*)\}"
)
_SHELL_VAR_RE = re.compile(r"\$\{[A-Z_][A-Z0-9_]*(?:[^}]*)?\}|\$[A-Z_][A-Z0-9_]*")
_ANSI_C_TOKEN_RE = re.compile(r"\$'([^']*)'")
_ASSIGNMENT_RE = re.compile(r"\b([a-zA-Z_][a-zA-Z0-9_]*)=([^\s;|&<>]+)")
_RESOLVE_VAR_RE = re.compile(
    r"\$\{([a-zA-Z_][a-zA-Z0-9_]*)\}|\$([a-zA-Z_][a-zA-Z0-9_]*)"
)

_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\brm\s+(-[a-zA-Z]*f[a-zA-Z]*\s+)?/"),
    re.compile(r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*f[a-zA-Z]*\s+/"),
    re.compile(r"\brm\s+-[a-zA-Z]*f[a-zA-Z]*r[a-zA-Z]*\s+/"),
    re.compile(r"\brm\b[^;|&]*--(?:recursive|force)[^;|&]*/"),
    re.compile(r"\brm\s+-[a-zA-Z]*(?:rf|fr)[a-zA-Z]*\s+~/?(?:\s|$)"),
    re.compile(r"\brm\s+-[a-zA-Z]*(?:rf|fr)[a-zA-Z]*\s+\.(?:\s|$)"),
    re.compile(r"\brm\s+-[a-zA-Z]*(?:rf|fr)[a-zA-Z]*\s+\*"),
    re.compile(r"\brm\b[^;|&]*--(?:recursive|force)[^;|&]*(?:~/?(?:\s|$)|\.(?:\s|$)|\*)"),
    re.compile(r"\bdd\b.*\bof\s*=\s*/dev/"),
    re.compile(r"(?:^\s*shutdown\b|\bshutdown\s+-[hr])", re.MULTILINE),
    re.compile(r"\breboot\b"),
    re.compile(r"\binit\s+0\b"),
    re.compile(r"\binit\s+6\b"),
    re.compile(r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"),
    re.compile(r"\bfork\s*bomb\b", re.IGNORECASE),
    re.compile(r"\bmkfs\b"),
    re.compile(r"\bwipefs\b"),
    re.compile(r">\s*/dev/sda"),
    re.compile(r"\bchmod\s+-R\s+777\s+/"),
    re.compile(r"\bchown\s+-R\b.*\s+/"),
    re.compile(r"(?:^\s*|(?:\b(?:env|command|builtin)\s+))eval\s+", re.MULTILINE),
    re.compile(r"(?:^\s*|(?:\b(?:env|command|builtin)\s+))exec\s+(?![0-9<>&])", re.MULTILINE),
    re.compile(r"\|\s*(bash|sh|zsh|fish|python[0-9.]*|perl|ruby|node)\b"),
    re.compile(r"(?:bash|sh|zsh|fish|python[23]?|perl|ruby|node)\s+<\("),
    re.compile(r">\(\s*(?:bash|sh|zsh|fish|python[23]?|perl|ruby|node)\b"),
    re.compile(r"\b(?:python[23]?|ruby|perl|node|bash|sh|zsh|fish)\b[^;|&\n]*<<-?\s*['\"]?\w+"),
    re.compile(r"\bpython[0-9.]*\s+-c\b"),
    re.compile(r"\bperl\s+-[pn]?e\b"),
    re.compile(r"\bruby\s+-e\b"),
    re.compile(r"\bnode\s+-e\b"),
    re.compile(r"\bn(?:c|cat)\b.*-e\b"),
    re.compile(r"\bsocat\b"),
    re.compile(r"/dev/tcp/"),
    re.compile(r"\bbash\s+-i\b.*>&"),
    re.compile(r"\bbase64\s+(-d|--decode)\b"),
    re.compile(r"\bxxd\s+-r\b"),
    re.compile(r"\bpython[0-9.]*\s+-m\s+base64\b"),
    re.compile(r"\bcrontab\s+"),
    re.compile(r"\bfind\b.*\s-delete\b"),
    re.compile(r"\bfind\b.*\s-exec\b"),
    re.compile(r">>?\s*(?:/etc/cron|/var/spool/cron)"),
    re.compile(r"\btee\b.*(?:/etc/cron|/var/spool/cron)"),
    re.compile(r"\bhistory\s+-c\b"),
    re.compile(r"\bunset\s+HISTFILE\b"),
]

_SPLIT_RE = re.compile(r"""&&|(?<!&)&(?!&)|\|\||[;\n|]|`[^`]*`|\$'[^']*'""")


def _decode_ansi_c(s: str) -> str:
    def _replace(m: re.Match[str]) -> str:
        if m.group(1):
            return chr(int(m.group(1), 16))
        if m.group(2):
            return chr(int(m.group(2), 16))
        if m.group(3):
            return chr(int(m.group(3), 16))
        if m.group(4):
            return chr(int(m.group(4), 8))
        if m.group(5):
            return _SIMPLE_ESCAPES[m.group(5)]
        return m.group(0)
    return _ANSI_C_RE.sub(_replace, s)


def _neutralize_shell_vars(command: str) -> str:
    expanded = _SHELL_VAR_EXPAND_RE.sub(r" \1 ", command)
    return _SHELL_VAR_RE.sub(" ", expanded)


def _resolve_simple_assignments(command: str) -> str:
    assignments: dict[str, str] = {}
    for m in _ASSIGNMENT_RE.finditer(command):
        assignments[m.group(1)] = m.group(2)
    if not assignments:
        return command
    def _replace(m: re.Match[str]) -> str:
        name = m.group(1) or m.group(2)
        return assignments.get(name, m.group(0))
    return _RESOLVE_VAR_RE.sub(_replace, command)


def _reconstitute_ansi_c(command: str) -> str:
    def _replace(m: re.Match[str]) -> str:
        return _decode_ansi_c(m.group(1))
    return _ANSI_C_TOKEN_RE.sub(_replace, command)


def _collapse_quotes(command: str) -> str:
    try:
        return " ".join(shlex.split(command))
    except ValueError:
        return command


def _expand_braces(command: str) -> str:
    """Collapse brace expansions to their first non-empty alternative.

    {rm,} -> rm, {chmod,x} -> chmod. This defeats bypasses where the
    shell expands braces but the blocklist sees no dangerous token.
    """
    def _replace(m: re.Match[str]) -> str:
        inner = m.group(0)[1:-1]
        parts = inner.split(",")
        for part in parts:
            if part.strip():
                return part.strip()
        return m.group(0)
    return _BRACE_RE.sub(_replace, command)


def _extract_subshells(command: str) -> list[str]:
    results: list[str] = []
    stack: list[int] = []
    for i, ch in enumerate(command):
        if ch == '(':
            stack.append(i + 1)
        elif ch == ')' and stack:
            start = stack.pop()
            body = command[start:i].strip()
            if body:
                results.append(body)
    return results


def _split_fragments(command: str) -> list[str]:
    fragments = []
    for m in _SPLIT_RE.finditer(command):
        token = m.group()
        if token.startswith("`"):
            inner = _reconstitute_ansi_c(token[1:-1])
            if inner.strip():
                fragments.append(inner.strip())
        elif token.startswith("$'"):
            inner = token[2:-1]
            decoded = _decode_ansi_c(inner)
            if decoded.strip():
                fragments.append(decoded.strip())
    parts = _SPLIT_RE.split(command)
    for part in parts:
        stripped = part.strip()
        if stripped:
            fragments.append(stripped)
    return fragments


def _matches(text: str) -> bool:
    return any(p.search(text) for p in _PATTERNS)


def check_blocklist(command: str) -> bool:
    resolved = _resolve_simple_assignments(command)
    neutralized = _neutralize_shell_vars(command)
    resolved_neutralized = _neutralize_shell_vars(resolved)
    collapsed = _collapse_quotes(neutralized)
    expanded = _expand_braces(neutralized)
    collapsed_expanded = _expand_braces(collapsed)
    for text in (command, resolved, neutralized, resolved_neutralized,
                 collapsed, expanded, collapsed_expanded):
        reconstituted = _reconstitute_ansi_c(text)
        for candidate in (text, reconstituted):
            if _matches(candidate):
                return True
            for fragment in _split_fragments(candidate):
                if _matches(fragment):
                    return True
            for body in _extract_subshells(candidate):
                if _matches(body):
                    return True
                for fragment in _split_fragments(body):
                    if _matches(fragment):
                        return True
    return False
