import logging
import math
import threading
import time
from collections.abc import Callable
from typing import TypeVar

from faffmonkey.types import AuthError, ProviderUnavailableError, RetryableError

logger = logging.getLogger(__name__)

T = TypeVar("T")

MAX_RETRIES = 3
BASE_DELAY = 1.0
MAX_DELAY = 30.0


def run_with_timeout(fn: Callable[[], T], timeout: float, label: str) -> T:
    """Run fn in a daemon thread; raise TimeoutError if it outlives timeout.

    The thread is abandoned on timeout (daemon threads cannot be killed),
    so fn must not hold locks the caller needs.
    """
    result: list[T] = []
    exc: list[Exception] = []

    def _run() -> None:
        try:
            result.append(fn())
        except Exception as e:
            exc.append(e)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise TimeoutError(f"{label} timed out after {timeout}s")
    if exc:
        raise exc[0]
    return result[0]


def _attempt_with_retries(
    fn: Callable[[], T],
    label: str,
    max_retries: int = MAX_RETRIES,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> T:
    last_error: Exception | None = None
    for attempt in range(max_retries):
        try:
            return fn()
        except AuthError:
            raise
        except RetryableError as e:
            last_error = e
            if attempt < max_retries - 1:
                if e.retry_after is not None:
                    delay = e.retry_after
                    if math.isnan(delay):
                        delay = 0.0
                    delay = max(0.0, min(delay, MAX_DELAY))
                else:
                    delay = min(BASE_DELAY * (2 ** attempt), MAX_DELAY)
                logger.warning(
                    "%s: attempt %d/%d failed (%s), retrying in %.1fs",
                    label, attempt + 1, max_retries, e, delay,
                )
                sleep_fn(delay)
    raise last_error  # type: ignore[misc]


def retry_with_fallback(
    primary: Callable[[], T],
    fallbacks: list[Callable[[], T]],
    label: str = "provider",
    sleep_fn: Callable[[float], None] = time.sleep,
) -> T:
    try:
        return _attempt_with_retries(primary, f"{label}[primary]", sleep_fn=sleep_fn)
    except AuthError:
        logger.warning("%s[primary]: auth error, trying fallbacks", label)
    except (RetryableError, ProviderUnavailableError):
        logger.warning("%s[primary]: exhausted retries, trying fallbacks", label)

    for i, fallback in enumerate(fallbacks):
        fb_label = f"{label}[fallback-{i}]"
        try:
            return _attempt_with_retries(fallback, fb_label, sleep_fn=sleep_fn)
        except AuthError:
            logger.warning("%s: auth error, skipping", fb_label)
            continue
        except (RetryableError, ProviderUnavailableError):
            logger.warning("%s: exhausted retries", fb_label)
            continue

    raise RetryableError(f"{label}: all providers exhausted")
