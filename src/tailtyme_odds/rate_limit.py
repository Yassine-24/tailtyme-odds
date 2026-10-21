"""Process-local outbound quota guard used by the standalone adapter."""

from __future__ import annotations

from collections import defaultdict, deque
import threading
import time


class OutboundRateLimitExceeded(Exception):
    def __init__(self, retry_after: int):
        self.retry_after = retry_after
        super().__init__("Outbound provider rate limit exceeded")


class OutboundRateLimitUnavailable(Exception):
    """Reserved for applications that replace the local guard with a shared one."""


class ProviderRateLimitExceeded(Exception):
    def __init__(self, retry_after: int):
        self.retry_after = max(1, int(retry_after))
        super().__init__("Remote provider rate limit exceeded")


_attempts: dict[str, deque[float]] = defaultdict(deque)
_lock = threading.Lock()


def require_outbound_rate_limit(provider: str, limit: int, window_seconds: int = 60) -> None:
    """Enforce a per-process sliding window before an outbound request."""
    now = time.monotonic()
    cutoff = now - window_seconds
    key = f"{provider}:global"
    with _lock:
        timestamps = _attempts[key]
        while timestamps and timestamps[0] <= cutoff:
            timestamps.popleft()
        if len(timestamps) >= limit:
            retry_after = max(1, int(timestamps[0] + window_seconds - now + 0.999))
            raise OutboundRateLimitExceeded(retry_after)
        timestamps.append(now)
