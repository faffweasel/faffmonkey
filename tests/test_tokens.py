from faffmonkey.runtime.tokens import BudgetResult, check_budget, count_tokens


class TestCountTokens:
    def test_empty_string(self):
        assert count_tokens("") == 0

    def test_short_string(self):
        result = count_tokens("hello world")
        assert result == 4

    def test_ascii_uses_conservative_estimate(self):
        text = "a" * 350
        assert count_tokens(text) == 140

    def test_cjk_not_undercounted(self):
        cjk = "一" * 100
        ascii_equiv_len = len(cjk.encode("utf-8"))
        result = count_tokens(cjk)
        assert result >= int(ascii_equiv_len / 2.5)

    def test_emoji_not_undercounted(self):
        emoji = "\U0001f600" * 50
        result = count_tokens(emoji)
        assert result >= int(len(emoji.encode("utf-8")) / 2.5)

    def test_mixed_ascii_and_cjk(self):
        text = "hello " + "一" * 50
        result = count_tokens(text)
        assert result >= int(len(text.encode("utf-8")) / 2.5)

    def test_cjk_within_20_percent_of_upper_bound(self):
        cjk = "一二三四五六七八九十" * 10  # 100 CJK chars
        conservative_real = len(cjk) * 2  # ~2 tokens/char worst case
        estimate = count_tokens(cjk)
        assert estimate >= conservative_real * 0.8

    def test_emoji_zwj_within_20_percent(self):
        zwj_emoji = "\U0001F468‍\U0001F469‍\U0001F467‍\U0001F466"
        text = zwj_emoji * 10
        conservative_real = len(text) * 2  # ~2 tokens/codepoint worst case
        estimate = count_tokens(text)
        assert estimate >= conservative_real * 0.8

    def test_mixed_ascii_nonascii_within_20_percent(self):
        text = "hello world " + "一二三" * 30  # 12 ASCII + 90 CJK chars
        utf8_len = len(text.encode("utf-8"))
        non_ascii = sum(1 for b in text.encode("utf-8") if b > 0x7F)
        assert non_ascii / utf8_len > 0.5  # triggers tighter divisor
        conservative_real = 12 // 4 + 90 * 2  # ~3 + 180 = 183
        estimate = count_tokens(text)
        assert estimate >= conservative_real * 0.8

    def test_pure_ascii_unchanged(self):
        text = "The quick brown fox jumps over the lazy dog. " * 10
        utf8 = text.encode("utf-8")
        assert count_tokens(text) == int(len(utf8) / 2.5)

    def test_low_nonascii_uses_standard_divisor(self):
        text = "a" * 100 + "一"  # 103 bytes, 3 non-ASCII
        utf8 = text.encode("utf-8")
        non_ascii = sum(1 for b in utf8 if b > 0x7F)
        assert non_ascii / len(utf8) < 0.5
        assert count_tokens(text) == int(len(utf8) / 2.5)

    def test_returns_int(self):
        assert isinstance(count_tokens("any text"), int)


class TestCheckBudget:
    def test_within_budget(self):
        text = "a" * 350  # 140 tokens (conservative estimate)
        result = check_budget(text, model_context_window=1000, max_fraction=0.6)
        assert result.ok is True
        assert result.total_tokens == 140
        assert result.max_tokens == 600

    def test_over_budget(self):
        text = "a" * 3500  # 1400 tokens (conservative estimate)
        result = check_budget(text, model_context_window=1000, max_fraction=0.6)
        assert result.ok is False
        assert result.total_tokens == 1400
        assert result.max_tokens == 600

    def test_exactly_at_budget(self):
        text = "a" * 1500  # 600 tokens (conservative estimate)
        result = check_budget(text, model_context_window=1000, max_fraction=0.6)
        assert result.ok is True
        assert result.total_tokens == 600
        assert result.max_tokens == 600

    def test_custom_fraction(self):
        text = "a" * 700  # 200 tokens
        result = check_budget(text, model_context_window=1000, max_fraction=0.1)
        assert result.ok is False
        assert result.max_tokens == 100

    def test_returns_budget_result(self):
        result = check_budget("x", model_context_window=10000)
        assert isinstance(result, BudgetResult)
