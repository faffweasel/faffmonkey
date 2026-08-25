from dataclasses import dataclass


@dataclass
class BudgetResult:
    ok: bool
    total_tokens: int
    max_tokens: int


def count_tokens(text: str) -> int:
    utf8 = text.encode("utf-8")
    utf8_len = len(utf8)
    char_estimate = int(len(text) / 3.5)
    # Non-ASCII chars (CJK, emoji, ZWJ sequences) cost more real tokens per
    # byte than ASCII. Use a tighter divisor when >50% of bytes are non-ASCII
    # so adversarial Unicode can't exceed the real context window by >~20%.
    non_ascii = sum(1 for b in utf8 if b > 0x7F)
    if utf8_len > 0 and non_ascii / utf8_len > 0.5:
        byte_estimate = int(utf8_len / 1.5)
    else:
        byte_estimate = int(utf8_len / 2.5)
    return max(char_estimate, byte_estimate)


def check_budget(
    bootstrap: str,
    model_context_window: int,
    max_fraction: float = 0.6,
) -> BudgetResult:
    total = count_tokens(bootstrap)
    max_tokens = int(model_context_window * max_fraction)
    return BudgetResult(
        ok=total <= max_tokens,
        total_tokens=total,
        max_tokens=max_tokens,
    )
