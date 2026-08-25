import os
import re
import unicodedata

from faffmonkey.runtime.ingest import _INVISIBLE

_WHITESPACE_RUN = re.compile(r"\s+")

_LOOKALIKE_TRANSLATE = str.maketrans(
    '‐‑‒–—―−﹘﹣－'
    '＿﹍﹎﹏',
    '-' * 10 + '_' * 4,
)

_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"-----BEGIN\s[\w\s]*PRIVATE\sKEY-----[\s\S]*?-----END\s[\w\s]*PRIVATE\sKEY-----"),
    re.compile(r"github_pat_[a-zA-Z0-9_]{80,}"),
    re.compile(r"gh[pousr]_[a-zA-Z0-9]{36,}"),
    re.compile(r"gl(?:pat|rt|cbt|ptt)-[A-Za-z0-9_-]{20,}"),
    re.compile(r"hf_[A-Za-z0-9]{30,}"),
    re.compile(r"sk-[a-zA-Z0-9_-]{20,}"),
    re.compile(r"[spr]k_(live|test)_[a-zA-Z0-9]{20,}"),
    re.compile(r"xox[pbarse]-[a-zA-Z0-9-]+"),
    re.compile(r"npm_[a-zA-Z0-9]{36,}"),
    re.compile(r"AIza[a-zA-Z0-9_-]{35}"),
    re.compile(r"vn_[a-zA-Z0-9]{20,}"),
    re.compile(r'\bAC[0-9a-f]{32}\b'),
    re.compile(r'\bSK[0-9a-f]{32}\b'),
    re.compile(r'\bSG\.[A-Za-z0-9_-]{22}\.[A-Za-z0-9_-]{43}\b'),
    re.compile(r'\bkey-[a-z0-9]{32}\b'),
    re.compile(r'\bdop_v1_[a-f0-9]{64}\b'),
    re.compile(r'\bHRKU-[A-Za-z0-9_-]{40,}\b'),
    re.compile(r"eyJ[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+"),
    re.compile(r"(?:Authorization:\s*)?Bearer\s[a-zA-Z0-9_/.+=-]{40,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"ASIA[0-9A-Z]{16}"),
    re.compile(r"[0-9]{5,}:[A-Za-z0-9_-]{35,}"),
    re.compile(r"sbp_[a-zA-Z0-9]{20,}"),
    re.compile(r"sb_secret_[a-zA-Z0-9_-]{20,}"),
]

# Discord bot tokens omitted: base64.base64.base64 format is indistinguishable from JWTs

REDACTED = "[REDACTED]"

_SECRET_ENV_NAME_RE = re.compile(r'(API_KEY|TOKEN|SECRET|PASSWORD)$|_KEY$')
_SECRET_URL_NAME_RE = re.compile(r'_URL$')
_URL_CREDENTIAL_RE = re.compile(r'(://[^:@/\s]+:)([^@\s]+)(@)')


def _get_secret_values() -> list[str]:
    values: set[str] = set()
    for name, val in os.environ.items():
        if not val or len(val) < 8:
            continue
        if _SECRET_ENV_NAME_RE.search(name):
            values.add(val)
        if _SECRET_URL_NAME_RE.search(name):
            m = _URL_CREDENTIAL_RE.search(val)
            if m:
                password = m.group(2)
                if len(password) >= 8:
                    values.add(password)
    return sorted(values, key=len, reverse=True)


def _build_normalized(text: str) -> tuple[str, list[int]]:
    norm_chars: list[str] = []
    pos_map: list[int] = []
    for i, ch in enumerate(text):
        nfkc = unicodedata.normalize('NFKC', ch)
        nfkc = nfkc.translate(_LOOKALIKE_TRANSLATE)
        for c in nfkc:
            norm_chars.append(c)
            pos_map.append(i)
    return ''.join(norm_chars), pos_map


def _merge_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not spans:
        return []
    spans.sort()
    merged = [spans[0]]
    for start, end in spans[1:]:
        if start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _apply_redactions(text: str, spans: list[tuple[int, int]]) -> str:
    merged = _merge_spans(spans)
    for start, end in reversed(merged):
        text = text[:start] + REDACTED + text[end:]
    return text


def _redact_normalized(text: str) -> str:
    norm_text, pos_map = _build_normalized(text)
    if not pos_map:
        return text
    spans: list[tuple[int, int]] = []
    for pattern in _PATTERNS:
        for m in pattern.finditer(norm_text):
            spans.append((pos_map[m.start()], pos_map[m.end() - 1] + 1))
    return _apply_redactions(text, spans)


# A wrapped secret is broken by a terminal once, maybe twice. A sentence
# that happens to contain "sk-" is broken at every word. Two is the line
# between catching an evasion and eating the rest of the paragraph.
_MAX_WRAP_GAPS = 2


def _redact_whitespace_wrapped(text: str) -> str:
    """Catch a secret broken across whitespace, without swallowing prose.

    Two rules keep this from destroying ordinary text. A match may span at
    most _MAX_WRAP_GAPS whitespace runs, and each contiguous run is redacted
    as its own span rather than the whole first-to-last range. The previous
    version did neither, so "Ask-me about the following items one two three
    four" matched the sk- pattern once whitespace was stripped and collapsed
    to "A[REDACTED]".
    """
    stripped_chars: list[str] = []
    strip_pos: list[int] = []
    for i, ch in enumerate(text):
        if not ch.isspace():
            strip_pos.append(i)
            stripped_chars.append(ch)
    stripped = "".join(stripped_chars)
    if not stripped:
        return text

    norm_text, norm_pos = _build_normalized(stripped)
    if not norm_pos:
        return text

    spans: list[tuple[int, int]] = []
    for pattern in _PATTERNS[1:]:
        for m in pattern.finditer(norm_text):
            indices = [strip_pos[norm_pos[i]] for i in range(m.start(), m.end())]
            runs: list[tuple[int, int]] = []
            run_start = prev = indices[0]
            for idx in indices[1:]:
                if idx != prev + 1:
                    runs.append((run_start, prev + 1))
                    run_start = idx
                prev = idx
            runs.append((run_start, prev + 1))
            if len(runs) - 1 > _MAX_WRAP_GAPS:
                continue
            spans.extend(runs)

    return _apply_redactions(text, spans)


def _redact_secret_values(text: str) -> str:
    for secret in _get_secret_values():
        text = text.replace(secret, REDACTED)
    return text


def _redact_url_credentials(text: str) -> str:
    return _URL_CREDENTIAL_RE.sub(rf'\1{REDACTED}\3', text)


def redact(text: str) -> str:
    """Remove secrets, and change nothing else.

    Two behaviours were removed here deliberately.

    The whitespace run collapse rewrote every outbound message and every
    stored tool result onto a single line, destroying the formatting of
    code, logs and tables for a redaction pass that usually matched
    nothing.

    The whitespace-stripping scan joined the text into one string, matched
    patterns against it, and mapped the hit back as a single span. It
    could not tell a line-wrapped key from ordinary prose: "Ask-me about
    the following items one two three four" strips to a string the sk-
    pattern matches, and the span then covered the whole sentence, so an
    ordinary word containing "sk-" destroyed everything after it. A secret
    contains no whitespace, so a match is bounded at whitespace and the
    contiguous scan below is the one that can find it. The multi-line
    PRIVATE KEY block is matched by its own pattern, which spans newlines
    explicitly.
    """
    text = _INVISIBLE.sub("", text)
    text = _redact_normalized(text)
    text = _redact_whitespace_wrapped(text)
    text = _redact_url_credentials(text)
    return _redact_secret_values(text)
