"""Bounded retries for transient provider HTTP failures."""

from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import random
import time
from typing import Any

import httpx

from .rate_limit import ProviderRateLimitExceeded, require_outbound_rate_limit

TRANSIENT_STATUS_CODES = {408, 425, 500, 502, 503, 504}


def provider_retry_after(response: httpx.Response, default: int = 60) -> int:
    """Read a bounded positive delay from a provider Retry-After header."""
    value = str(response.headers.get("Retry-After") or "").strip()
    try:
        return max(1, min(3600, int(float(value))))
    except (TypeError, ValueError):
        pass
    try:
        retry_at = parsedate_to_datetime(value)
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return max(1, min(3600, int((retry_at - datetime.now(timezone.utc)).total_seconds() + 0.999)))
    except (TypeError, ValueError, OverflowError):
        return max(1, min(3600, int(default)))


def request_with_retry(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    attempts: int = 3,
    backoff_seconds: float = 0.2,
    max_backoff_seconds: float = 1.5,
    jitter_seconds: float = 0.1,
    rate_limit_provider: str | None = None,
    rate_limit_per_window: int | None = None,
    **kwargs: Any,
) -> httpx.Response:
    """Retry only transient network/status failures, with small jitter."""
    attempts = max(1, min(attempts, 3))
    for attempt in range(attempts):
        try:
            if rate_limit_provider and rate_limit_per_window is not None:
                require_outbound_rate_limit(rate_limit_provider, rate_limit_per_window)
            request_method = getattr(client, method.lower(), None)
            if request_method is None:
                response = client.request(method, url, **kwargs)
            else:
                response = request_method(url, **kwargs)
            response.raise_for_status()
            return response
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429:
                raise ProviderRateLimitExceeded(provider_retry_after(exc.response)) from exc
            transient = exc.response.status_code in TRANSIENT_STATUS_CODES
            if not transient or attempt == attempts - 1:
                raise
        except (httpx.TimeoutException, httpx.TransportError):
            if attempt == attempts - 1:
                raise

        delay = min(max_backoff_seconds, backoff_seconds * (2**attempt))
        time.sleep(delay + random.uniform(0, jitter_seconds))

    raise RuntimeError("provider request retry loop exhausted")
