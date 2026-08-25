import pytest

from faffmonkey.runtime.retry import retry_with_fallback
from faffmonkey.types import AuthError, ProviderUnavailableError, RetryableError


class TestRetryWithFallback:
    def test_primary_succeeds_first_try(self):
        result = retry_with_fallback(
            primary=lambda: "ok",
            fallbacks=[],
            sleep_fn=lambda _: None,
        )
        assert result == "ok"

    def test_primary_retries_on_retryable_then_succeeds(self):
        calls = []

        def primary():
            calls.append(1)
            if len(calls) < 3:
                raise RetryableError("fail")
            return "recovered"

        result = retry_with_fallback(
            primary=primary,
            fallbacks=[],
            sleep_fn=lambda _: None,
        )
        assert result == "recovered"
        assert len(calls) == 3

    def test_exponential_backoff_delays(self):
        delays = []

        def primary():
            raise RetryableError("fail")

        def fallback():
            return "fallback-ok"

        result = retry_with_fallback(
            primary=primary,
            fallbacks=[fallback],
            sleep_fn=lambda d: delays.append(d),
        )
        assert result == "fallback-ok"
        assert len(delays) >= 2
        assert delays[0] < delays[1]

    def test_retry_after_header_respected(self):
        delays = []
        calls = []

        def primary():
            calls.append(1)
            if len(calls) == 1:
                raise RetryableError("rate limited", retry_after=10.0)
            return "ok"

        result = retry_with_fallback(
            primary=primary,
            fallbacks=[],
            sleep_fn=lambda d: delays.append(d),
        )
        assert result == "ok"
        assert delays[0] == 10.0

    def test_falls_back_after_primary_exhausted(self):
        result = retry_with_fallback(
            primary=lambda: (_ for _ in ()).throw(RetryableError("fail")),
            fallbacks=[lambda: "from-fallback"],
            sleep_fn=lambda _: None,
        )
        assert result == "from-fallback"

    def test_auth_error_skips_to_fallback_immediately(self):
        primary_calls = []
        fallback_calls = []

        def primary():
            primary_calls.append(1)
            raise AuthError("bad key")

        def fallback():
            fallback_calls.append(1)
            return "fallback-ok"

        result = retry_with_fallback(
            primary=primary,
            fallbacks=[fallback],
            sleep_fn=lambda _: None,
        )
        assert result == "fallback-ok"
        assert len(primary_calls) == 1
        assert len(fallback_calls) == 1

    def test_auth_error_in_fallback_skips_to_next(self):
        def primary():
            raise RetryableError("fail")

        def fallback_a():
            raise AuthError("bad key A")

        def fallback_b():
            return "B-ok"

        result = retry_with_fallback(
            primary=primary,
            fallbacks=[fallback_a, fallback_b],
            sleep_fn=lambda _: None,
        )
        assert result == "B-ok"

    def test_all_providers_exhausted(self):
        def primary():
            raise RetryableError("fail")

        def fallback():
            raise RetryableError("also fail")

        with pytest.raises(RetryableError, match="all providers exhausted"):
            retry_with_fallback(
                primary=primary,
                fallbacks=[fallback],
                sleep_fn=lambda _: None,
            )

    def test_no_fallbacks_raises_all_exhausted(self):
        def primary():
            raise RetryableError("primary dead")

        with pytest.raises(RetryableError, match="all providers exhausted"):
            retry_with_fallback(
                primary=primary,
                fallbacks=[],
                sleep_fn=lambda _: None,
            )

    def test_fallback_retries_before_giving_up(self):
        fallback_calls = []

        def primary():
            raise RetryableError("fail")

        def fallback():
            fallback_calls.append(1)
            if len(fallback_calls) < 3:
                raise RetryableError("not yet")
            return "fallback-recovered"

        result = retry_with_fallback(
            primary=primary,
            fallbacks=[fallback],
            sleep_fn=lambda _: None,
        )
        assert result == "fallback-recovered"
        assert len(fallback_calls) == 3

    def test_retry_after_capped_at_max_delay(self):
        delays = []
        calls = []

        def primary():
            calls.append(1)
            if len(calls) == 1:
                raise RetryableError("slow down", retry_after=300.0)
            return "ok"

        result = retry_with_fallback(
            primary=primary,
            fallbacks=[],
            sleep_fn=lambda d: delays.append(d),
        )
        assert result == "ok"
        assert delays[0] == 30.0

    def test_provider_unavailable_falls_back(self):
        def primary():
            raise ProviderUnavailableError("connection refused")

        def fallback():
            return "fallback-ok"

        result = retry_with_fallback(
            primary=primary,
            fallbacks=[fallback],
            sleep_fn=lambda _: None,
        )
        assert result == "fallback-ok"

    def test_provider_unavailable_no_fallbacks_raises(self):
        def primary():
            raise ProviderUnavailableError("connection refused")

        with pytest.raises(RetryableError, match="all providers exhausted"):
            retry_with_fallback(
                primary=primary,
                fallbacks=[],
                sleep_fn=lambda _: None,
            )

    def test_provider_unavailable_in_fallback_skips_to_next(self):
        def primary():
            raise RetryableError("fail")

        def fallback_a():
            raise ProviderUnavailableError("down")

        def fallback_b():
            return "B-ok"

        result = retry_with_fallback(
            primary=primary,
            fallbacks=[fallback_a, fallback_b],
            sleep_fn=lambda _: None,
        )
        assert result == "B-ok"

    def test_retry_after_nan_results_in_zero_delay(self):
        delays = []
        calls = []

        def primary():
            calls.append(1)
            if len(calls) == 1:
                raise RetryableError("rate limited", retry_after=float("nan"))
            return "ok"

        result = retry_with_fallback(
            primary=primary,
            fallbacks=[],
            sleep_fn=lambda d: delays.append(d),
        )
        assert result == "ok"
        assert delays[0] == 0.0

    def test_retry_after_negative_clamped_to_zero(self):
        delays = []
        calls = []

        def primary():
            calls.append(1)
            if len(calls) == 1:
                raise RetryableError("negative", retry_after=-5.0)
            return "ok"

        result = retry_with_fallback(
            primary=primary,
            fallbacks=[],
            sleep_fn=lambda d: delays.append(d),
        )
        assert result == "ok"
        assert delays[0] == 0.0

    def test_retry_after_inf_bounded_by_max_delay(self):
        delays = []
        calls = []

        def primary():
            calls.append(1)
            if len(calls) == 1:
                raise RetryableError("slow down", retry_after=float("inf"))
            return "ok"

        result = retry_with_fallback(
            primary=primary,
            fallbacks=[],
            sleep_fn=lambda d: delays.append(d),
        )
        assert result == "ok"
        assert delays[0] == 30.0
