import logging
import re
import secrets
import unicodedata

logger = logging.getLogger(__name__)

_INVISIBLE = re.compile(
    '['
    '​-‏'     # zero-width space, ZWNJ, ZWJ, LRM, RLM
    '﻿'            # BOM / ZWNBS
    '⁠-⁤'     # word joiner, invisible times/separator/plus
    '⁥'            # reserved
    '­'            # soft hyphen
    ' - '     # line/paragraph separators, embedding controls
    '⁦-⁩'     # bidi isolates
    '͏'            # combining grapheme joiner
    '᠎'            # Mongolian Vowel Separator
    '؜'        # Arabic Letter Mark
    'ᅟᅠ'      # Hangul Choseong/Jungseong fillers
    'ㅤ'            # Hangul filler
    'ﾠ'            # halfwidth Hangul filler
    '︀-️'    # variation selectors
    '\U000E0100-\U000E01EF'  # variation selectors supplement
    '\U000E0001-\U000E007F'  # tags block
    ']'
)

_CONFUSABLES: dict[str, str] = {
    'а': 'a',  # Cyrillic а
    'е': 'e',  # Cyrillic е
    'о': 'o',  # Cyrillic о
    'р': 'p',  # Cyrillic р
    'с': 'c',  # Cyrillic с
    'у': 'y',  # Cyrillic у
    'х': 'x',  # Cyrillic х
    'і': 'i',  # Cyrillic і U+0456
    'ѕ': 's',  # Cyrillic ѕ U+0455
    'ԁ': 'd',  # Cyrillic ԁ U+0501
    'А': 'A',  # Cyrillic А
    'В': 'B',  # Cyrillic В
    'С': 'C',  # Cyrillic С
    'Е': 'E',  # Cyrillic Е
    'Н': 'H',  # Cyrillic Н
    'О': 'O',  # Cyrillic О
    'Р': 'P',  # Cyrillic Р
    'Т': 'T',  # Cyrillic Т
    'У': 'Y',  # Cyrillic У
    'Х': 'X',  # Cyrillic Х
    'ο': 'o',  # Greek ο
    'α': 'a',  # Greek α
    'ε': 'e',  # Greek ε
    'ρ': 'p',  # Greek ρ
    'ι': 'i',  # Greek ι U+03B9
    'ν': 'v',  # Greek ν U+03BD
    'υ': 'u',  # Greek υ U+03C5
    'τ': 't',  # Greek τ U+03C4
    'κ': 'k',  # Greek κ U+03BA
    'օ': 'o',  # Armenian օ U+0585
    'п': 'n',  # Cyrillic п U+043F
    'г': 'r',  # Cyrillic г U+0433
    'ɡ': 'g',  # Latin Small Letter Script G U+0261
}

_S = r'[\s.\-_:;/\\|]+'

_INJECTION_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r'ignore' + _S + r'(previous|all|above|prior)' + _S + r'instructions', re.IGNORECASE),
     "prompt injection attempt"),
    (re.compile(r'system' + _S + r'prompt' + _S + r'override', re.IGNORECASE),
     "system prompt override attempt"),
    (re.compile(r'do' + _S + r'not' + _S + r'tell' + _S + r'the' + _S + r'user', re.IGNORECASE),
     "deception attempt"),
    (re.compile(r'curl' + _S + r'[^\n]*(KEY|TOKEN|SECRET|PASSWORD)', re.IGNORECASE),
     "credential exfiltration attempt"),
    (re.compile(r'wget' + _S + r'[^\n]*(KEY|TOKEN|SECRET|PASSWORD)', re.IGNORECASE),
     "credential exfiltration attempt"),
    (re.compile(r'cat' + _S + r'[^\n]*(\.env|credentials|\.ssh)', re.IGNORECASE),
     "secret file read attempt"),
    (re.compile(r'you' + _S + r'are' + _S + r'(now|actually|really)' + _S, re.IGNORECASE),
     "identity override attempt"),
    (re.compile(r'new' + _S + r'instructions?\s*:', re.IGNORECASE),
     "instruction injection attempt"),
    (re.compile(r'\bdisregard\b' + _S + r'.{0,20}\b(prompt|instructions?|rules?|above)\b',
                re.IGNORECASE | re.MULTILINE),
     "prompt injection attempt"),
    (re.compile(r'\bforget\b' + _S + r'.{0,20}\b(previous|above|prior|all)\b',
                re.IGNORECASE | re.MULTILINE),
     "prompt injection attempt"),
    (re.compile(r'\boverride\b' + _S + r'.{0,20}\b(instructions?|rules?|prompt|system)\b',
                re.IGNORECASE | re.MULTILINE),
     "prompt injection attempt"),
    (re.compile(r'\b(your|assume|adopt|take)\b' + _S + r'.{0,10}\b(new\s+)?role\b',
                re.IGNORECASE | re.MULTILINE),
     "identity override attempt"),
    (re.compile(r'\b(reveal|show|output|print|display)\b' + _S + r'.{0,20}\b(system\s+prompt|api\s*key|secret|token|password|credential)\b',
                re.IGNORECASE | re.MULTILINE),
     "credential exfiltration attempt"),
]


def strip_invisible(text: str) -> str:
    return _INVISIBLE.sub('', text)


def _to_ascii_skeleton(text: str) -> str:
    text = unicodedata.normalize('NFKC', text)
    return ''.join(_CONFUSABLES.get(ch, ch) for ch in text)


_MAX_SCAN_LENGTH = 65_536
_TAIL_SCAN_LENGTH = 4096

def scan_patterns(text: str, path: str = "<unknown>") -> str | None:
    head = _to_ascii_skeleton(text[:_MAX_SCAN_LENGTH])
    tail = _to_ascii_skeleton(text[-_TAIL_SCAN_LENGTH:]) if len(text) > _MAX_SCAN_LENGTH else ""
    for pattern, reason in _INJECTION_PATTERNS:
        if pattern.search(head):
            logger.warning("injection pattern detected in %s: %s", path, reason)
            return reason
        if tail and pattern.search(tail):
            logger.warning("injection pattern detected in %s (tail): %s", path, reason)
            return reason
    return None


_UNTRUSTED_TAG = re.compile(r'<\s*(?=/?\s*untrusted)', re.IGNORECASE)


def _escape_untrusted_tags(content: str) -> str:
    return _UNTRUSTED_TAG.sub('&lt;', content)


def wrap_untrusted(content: str) -> str:
    nonce = secrets.token_hex(8)
    escaped = _escape_untrusted_tags(content)
    return f'<untrusted nonce="{nonce}">\n{escaped}\n</untrusted-{nonce}>'


def redact_injection_patterns(text: str) -> str:
    result = text
    changed = False
    for pattern, _reason in _INJECTION_PATTERNS:
        result, n = pattern.subn("[REDACTED: injection pattern detected]", result)
        if n > 0:
            changed = True
    if not changed:
        result = _to_ascii_skeleton(text)
        for pattern, _reason in _INJECTION_PATTERNS:
            result = pattern.sub("[REDACTED: injection pattern detected]", result)
    return result


def flag_response(text: str, path: str, label: str) -> tuple[str, str | None]:
    """Scan a model's own output for injection patterns. Returns the text
    safe to keep (a warning prefix plus redaction when flagged) and the
    hit, or the text unchanged and None. Every place that stores or
    delivers model output goes through here so the treatment cannot drift.
    """
    hit = scan_patterns(strip_invisible(text), path)
    if hit is None:
        return text, None
    return f"[WARNING: {label} flagged: {hit}]\n{redact_injection_patterns(text)}", hit


def ingest(content: str, path: str = "<unknown>") -> str:
    cleaned = strip_invisible(content)
    reason = scan_patterns(cleaned, path)
    if reason is not None:
        return f"[Content blocked: {reason}. Review {path} manually.]"
    return wrap_untrusted(cleaned)
