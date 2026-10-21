"""Fail-closed checks for new odds selections and live snapshots."""

from __future__ import annotations

from datetime import datetime, timezone
import math
import os

LIVE_ODDS_STALE_SECONDS = max(30, int(os.getenv("LIVE_ODDS_STALE_SECONDS", "120")))


def timestamp(value):
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def is_live(game, now=None):
    states = {str(getattr(game, field, "") or "").lower() for field in ("status", "provider_status")}
    if states & {"postponed", "post", "final", "completed", "cancelled", "canceled", "abandoned"}:
        return False
    start = timestamp(getattr(game, "start_time", None))
    return bool(states & {"in", "live", "in_progress"}) or bool(start and start <= (timestamp(now) or datetime.now(timezone.utc)))


def market_is_open(market, now=None, *, require_status=False):
    status = str(market.get("status") or "").lower()
    if (status or require_status) and status not in {"active", "open"}:
        return False
    if market.get("result") or market.get("settlement_ts"):
        return False
    current = timestamp(now) or datetime.now(timezone.utc)
    for field, expired in (("close_time", True), ("open_time", False)):
        if market.get(field):
            value = timestamp(market[field])
            if value is None or (value <= current if expired else value > current):
                return False
    return True


def total_is_determined(market, game):
    if str(market.get("market_type") or "").upper() != "TOTAL":
        return False
    try:
        line = float(market.get("line"))
        home = float(getattr(game, "home_score", None))
        away = float(getattr(game, "away_score", None))
    except (TypeError, ValueError):
        return False
    return all(math.isfinite(value) for value in (line, home, away)) and home + away > line


def live_snapshot_is_fresh(odds, now=None):
    captured = timestamp(odds.get("captured_at"))
    current = timestamp(now) or datetime.now(timezone.utc)
    return bool(captured and 0 <= (current - captured).total_seconds() <= LIVE_ODDS_STALE_SECONDS)
