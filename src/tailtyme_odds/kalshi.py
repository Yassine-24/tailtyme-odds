"""Read-only Kalshi market-data lookup and normalization."""

from __future__ import annotations

from contextvars import ContextVar
from datetime import datetime, timezone
import logging
import math
import os
import re
import threading
from typing import Any, Optional
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

from .provider_retry import request_with_retry
from .market_availability import is_live, market_is_open
from .rate_limit import (
    OutboundRateLimitExceeded,
    OutboundRateLimitUnavailable,
)

logger = logging.getLogger(__name__)

KALSHI_API_BASE = os.getenv(
    "KALSHI_API_BASE_URL",
    "https://external-api.kalshi.com/trade-api/v2",
).rstrip("/")
KALSHI_TIMEOUT_SECONDS = float(os.getenv("KALSHI_ODDS_TIMEOUT_SECONDS", "2.5"))
KALSHI_CACHE_SECONDS = max(0, int(os.getenv("KALSHI_ODDS_CACHE_SECONDS", "300")))
KALSHI_LIVE_CACHE_SECONDS = max(0, int(os.getenv("KALSHI_LIVE_ODDS_CACHE_SECONDS", "30")))
_live_fetch = ContextVar("kalshi_live_fetch", default=False)

KALSHI_NEGATIVE_CACHE_SECONDS = max(
    30, int(os.getenv("KALSHI_ODDS_NEGATIVE_CACHE_SECONDS", "300"))
)
KALSHI_FAR_FUTURE_NEGATIVE_CACHE_SECONDS = max(
    KALSHI_NEGATIVE_CACHE_SECONDS,
    int(os.getenv("KALSHI_FAR_FUTURE_NEGATIVE_CACHE_SECONDS", "1800")),
)
KALSHI_FAR_FUTURE_THRESHOLD_SECONDS = max(
    0, int(os.getenv("KALSHI_FAR_FUTURE_THRESHOLD_SECONDS", "21600"))
)
# Keep this application-side budget below Kalshi's public read budget while
# leaving enough room for a full visible slate after response caching.
KALSHI_API_RATE_LIMIT = max(1, int(os.getenv("KALSHI_API_RATE_LIMIT", "120")))
KALSHI_RESPONSE_CACHE_MAX_ENTRIES = max(
    64, int(os.getenv("KALSHI_RESPONSE_CACHE_MAX_ENTRIES", "512"))
)
TAILTIME_ODDS_MARGIN = max(0.0, float(os.getenv("TAILTIME_ODDS_MARGIN", "0.04")))

_KALSHI_GAME_SERIES = {
    "nba": "KXNBAGAME",
    "nfl": "KXNFLGAME",
    "mlb": "KXMLBGAME",
    "epl": "KXEPLGAME",
}
_KALSHI_SPORT_PREFIX = {"nba": "NBA", "nfl": "NFL", "mlb": "MLB", "epl": "EPL"}

_TEAM_TICKER_ALIASES = {
    # ESPN code: ordered Kalshi candidates. The first is the usual value.
    "nba": {
        "GS": ("GSW", "GS"),
        "GSW": ("GSW", "GS"),
        "NY": ("NYK", "NY"),
        "NYK": ("NYK", "NY"),
        "NO": ("NOP", "NO"),
        "NOP": ("NOP", "NO"),
        "SA": ("SAS", "SA"),
        "SAS": ("SAS", "SA"),
        "WSH": ("WAS", "WSH"),
        "WAS": ("WAS", "WSH"),
    },
    "nfl": {
        "WSH": ("WAS", "WSH"),
        "JAX": ("JAX", "JAC"),
        "JAC": ("JAC", "JAX"),
        "LVR": ("LV", "LVR"),
        "LV": ("LV", "LVR"),
        "LA": ("LAR", "LA"),
        "SFO": ("SF", "SFO"),
    },
    "mlb": {
        "CHW": ("CWS", "CHW"),
        "ATH": ("ATH", "OAK"),
        "OAK": ("ATH", "OAK"),
        "CWS": ("CWS", "CHW"),
        "ARI": ("AZ", "ARI"),
        "AZ": ("AZ", "ARI"),
    },
    "epl": {
        # ESPN is the canonical app/provider code. Kalshi uses football
        # abbreviations in its event tickers for several clubs.
        "BHA": ("BRI", "BHA"),
        "BRI": ("BRI", "BHA"),
        "CHE": ("CFC", "CHE"),
        "CFC": ("CFC", "CHE"),
        "LIV": ("LFC", "LIV"),
        "LFC": ("LFC", "LIV"),
        "MNC": ("MCI", "MNC"),
        "MCI": ("MCI", "MNC"),
        "MAN": ("MUN", "MAN"),
        "MUN": ("MUN", "MAN"),
    },
}
_TEAM_TEXT_ALIASES = {
    "ATH": ("ATHLETICS", "A'S"),
    "KC": ("KANSAS CITY",),
}
_cache: dict[str, tuple[float, Optional[dict[str, Any]]]] = {}
_response_cache: dict[str, tuple[float, Optional[dict[str, Any]]]] = {}
_cache_lock = threading.Lock()


def clear_kalshi_cache() -> None:
    """Clear normalized odds and raw provider-response caches."""
    with _cache_lock:
        _cache.clear()
        _response_cache.clear()


def _canonical_team(value: str | None, sport: str | None = None) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]", "", value or "").upper()
    aliases = _TEAM_TICKER_ALIASES.get((sport or "").lower(), {}).get(cleaned)
    return aliases[0] if aliases else cleaned


def _ticker_codes(value: str | None, sport: str | None = None) -> tuple[str, ...]:
    cleaned = re.sub(r"[^A-Za-z0-9]", "", value or "").upper()
    aliases = _TEAM_TICKER_ALIASES.get((sport or "").lower(), {}).get(cleaned)
    return aliases or (cleaned,)


def _date_key(game: Any) -> str:
    if game.event_date:
        return game.event_date.strftime("%y%b%d").upper()
    return game.start_time.strftime("%y%b%d").upper()


def _date_keys(game: Any) -> tuple[str, ...]:
    values = []
    if game.event_date:
        values.append(game.event_date.strftime("%y%b%d").upper())
    try:
        start = game.start_time.replace(tzinfo=timezone.utc) if game.start_time.tzinfo is None else game.start_time
        values.extend([
            start.astimezone(timezone.utc).strftime("%y%b%d").upper(),
            start.strftime("%y%b%d").upper(),
        ])
        if getattr(game, "venue_timezone", None):
            values.append(start.astimezone(ZoneInfo(game.venue_timezone)).strftime("%y%b%d").upper())
    except (ValueError, TypeError, KeyError):
        pass
    return tuple(dict.fromkeys(values)) or (_date_key(game),)


def candidate_event_tickers(game: Any) -> list[str]:
    """Return bounded, deterministic candidates; first candidate is preferred."""
    sport = (game.sport or "").lower()
    series = _KALSHI_GAME_SERIES.get(sport)
    if not series or not game.away_team_abbrev or not game.home_team_abbrev:
        return []

    away_codes = _ticker_codes(game.away_team_abbrev, sport)
    home_codes = _ticker_codes(game.home_team_abbrev, sport)
    if sport in {"nfl", "nba"}:
        # Both series use away-then-home codes without a scheduled time.
        # Keep date and team-code fallbacks bounded and deterministic.
        candidates = [
            f"{series}-{date_key}{away}{home}"
            for date_key in _date_keys(game)
            for away in away_codes
            for home in home_codes
        ]
        return candidates[:8]

    if sport == "epl":
        # EPL event tickers use the scheduled date and HOME then AWAY, e.g.
        # ESPN CHE/BHA -> Kalshi KXEPLGAME-26AUG30CFCBRI.
        candidates = [
            f"{series}-{date_key}{home}{away}"
            for date_key in _date_keys(game)
            for home in home_codes
            for away in away_codes
        ]
        return candidates[:8]

    # Kalshi's MLB game tickers use a four-digit US/Eastern scheduled time.
    # Keep UTC and venue-local variants as bounded fallbacks for provider data
    # that has historically used inconsistent timezone representations.
    candidates = []
    try:
        start = game.start_time.replace(tzinfo=timezone.utc) if game.start_time.tzinfo is None else game.start_time
        times = [
            start.astimezone(ZoneInfo("America/New_York")),
            start.astimezone(timezone.utc),
        ]
        if getattr(game, "venue_timezone", None):
            times.append(start.astimezone(ZoneInfo(game.venue_timezone)))
        for local_start in times:
            local_date = game.event_date or local_start.date()
            for away in away_codes:
                for home in home_codes:
                    candidate = f"{series}-{local_date.strftime('%y%b%d').upper()}{local_start:%H%M}{away}{home}"
                    if candidate not in candidates:
                        candidates.append(candidate)
    except (ValueError, TypeError, KeyError):
        return []
    return candidates


def american_odds(probability: float) -> Optional[int]:
    if probability <= 0 or probability >= 1:
        return None
    value = -100 * probability / (1 - probability) if probability >= 0.5 else 100 * (1 - probability) / probability
    return int(round(value))


def display_probability(probability: float) -> float:
    """Apply TailTyme's display-only sportsbook-style overround."""
    return min(0.999, round(probability * (1 + TAILTIME_ODDS_MARGIN), 4))


def _number(value: Any) -> Optional[float]:
    try:
        number = float(value) if value is not None else None
        return number if number is not None and math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _probability(market: dict[str, Any]) -> Optional[float]:
    bid = _number(market.get("yes_bid_dollars"))
    ask = _number(market.get("yes_ask_dollars"))
    if bid is None or ask is None or not 0 < bid <= ask < 1:
        return None
    for field in ("yes_bid_size_fp", "yes_ask_size_fp"):
        if field in market and (_number(market[field]) or 0) <= 0:
            return None
    return round((bid + ask) / 2, 4)


def _line(market: dict[str, Any]) -> Optional[float]:
    for key in ("floor_strike", "cap_strike"):
        value = _number(market.get(key))
        if value is not None:
            return value
    return None


def _team_in_text(text: str, abbreviation: str, name: str | None, sport: str | None = None) -> bool:
    codes = _ticker_codes(abbreviation, sport)
    if any(code and re.search(rf"\b{re.escape(code)}\b", text) for code in codes):
        return True
    compact_text = re.sub(r"[^A-Z0-9]", "", text)
    name_text = (name or "").upper()
    name_words = name_text.split()
    name_aliases = [name_text, " ".join(name_words[:2]), name_words[0] if name_words else ""]
    aliases = (*name_aliases, *_TEAM_TEXT_ALIASES.get(_canonical_team(abbreviation, sport), ()))
    for alias in aliases:
        if not alias:
            continue
        if "'" in alias:
            if re.search(r"\bA['’]?S\b", text):
                return True
        elif len(re.sub(r"[^A-Z0-9]", "", alias)) > 3 and re.sub(r"[^A-Z0-9]", "", alias) in compact_text:
            return True
    return False


def _selection(market: dict[str, Any], game: Any) -> str:
    # yes_sub_title identifies the selected side; the full title describes
    # both teams and can otherwise create false matches.
    selection_text = str(market.get("yes_sub_title") or "").strip()
    selection_key = re.sub(r"[^A-Z0-9]", "", selection_text.upper())
    if selection_key in {"TIE", "DRAW"}:
        return "DRAW"
    text = str(selection_text or market.get("title") or "").upper()
    for abbreviation, name in (
        (game.away_team_abbrev, game.away_team),
        (game.home_team_abbrev, game.home_team),
    ):
        if _team_in_text(text, abbreviation, name, game.sport):
            # Keep the ESPN/TailTyme code as the normalized selection key.
            return re.sub(r"[^A-Za-z0-9]", "", abbreviation or "").upper()
    return re.sub(r"[^A-Za-z0-9]", "", selection_text or str(market.get("title") or "UNKNOWN")).upper()


def _normalize_market(market: dict[str, Any], market_type: str, game: Any, event_ticker: str) -> Optional[dict[str, Any]]:
    if not market_is_open(market, require_status=True):
        return None
    probability = _probability(market)
    if probability is None:
        return None
    line = _line(market)
    # Kalshi EPL and NBA spread contracts describe the selected team winning
    # by more than a positive strike. TailTyme stores the equivalent signed
    # team line so Games/PostBet display the same value that settlement grades.
    # Keep other sports' existing contract conventions unchanged.
    sport = str(game.sport or "").lower()
    if market_type == "SPREAD" and line is not None and line > 0:
        if sport == "epl":
            line = -abs(line)
        elif sport == "nba":
            contract_text = " ".join(str(market.get(key) or "") for key in ("yes_sub_title", "title"))
            if re.search(
                r"\b(?:win|wins|beat|beats|defeat|defeats|victory)\w*\b"
                r".*\b(?:over|more|more\s+than|at least)\b",
                contract_text,
                flags=re.IGNORECASE,
            ):
                line = -abs(line)
    if market_type == "TOTAL":
        selection_text = str(market.get("yes_sub_title") or market.get("title") or "").upper()
        selection = "UNDER" if re.search(r"\bUNDER\b", selection_text) and not re.search(r"\bOVER\b", selection_text) else "OVER"
    else:
        selection = _selection(market, game)
    return {
        "market_type": market_type,
        "selection": selection,
        "line": line,
        "probability": probability,
        "tailtime_odds": american_odds(probability),
        "display_probability": display_probability(probability),
        "display_odds": american_odds(display_probability(probability)),
        "source": "kalshi",
        "source_event_ticker": event_ticker,
        "source_market_ticker": market.get("ticker"),
        "title": market.get("title"),
        "status": market.get("status"),
        "result": market.get("result"),
        "close_time": market.get("close_time"),
        "open_time": market.get("open_time"),
        "settlement_ts": market.get("settlement_ts"),
        "can_close_early": market.get("can_close_early"),
        "yes_bid": _number(market.get("yes_bid_dollars")),
        "yes_ask": _number(market.get("yes_ask_dollars")),
        "last_price": _number(market.get("last_price_dollars")),
        "captured_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def _representative(markets: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    valid = [market for market in markets if market.get("probability") is not None]
    return min(valid, key=lambda market: abs(market["probability"] - 0.5)) if valid else None


def _get_json(client: httpx.Client, path: str) -> Optional[dict[str, Any]]:
    now = datetime.now(timezone.utc).timestamp()
    if KALSHI_CACHE_SECONDS:
        with _cache_lock:
            cached = _response_cache.get(path)
            cache_ttl = KALSHI_NEGATIVE_CACHE_SECONDS if cached and cached[1] is None else (min(KALSHI_CACHE_SECONDS, KALSHI_LIVE_CACHE_SECONDS) if _live_fetch.get() else KALSHI_CACHE_SECONDS)
            if cached is not None and now - cached[0] < cache_ttl:
                return cached[1]

    def cache_response(payload: Optional[dict[str, Any]]) -> None:
        if not KALSHI_CACHE_SECONDS:
            return
        with _cache_lock:
            _response_cache[path] = (now, payload)
            while len(_response_cache) > KALSHI_RESPONSE_CACHE_MAX_ENTRIES:
                oldest = min(_response_cache, key=lambda key: _response_cache[key][0])
                del _response_cache[oldest]

    try:
        response = request_with_retry(
            client,
            "GET",
            f"{KALSHI_API_BASE}{path}",
            rate_limit_provider="kalshi",
            rate_limit_per_window=KALSHI_API_RATE_LIMIT,
        )
        if response.status_code == 404:
            cache_response(None)
            return None
        payload = response.json()
        if not isinstance(payload, dict):
            return None
        cache_response(payload)
        return payload
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            cache_response(None)
            return None
        logger.warning("kalshi_request_failed path=%s status=%s", path, exc.response.status_code)
        return None
    except OutboundRateLimitExceeded:
        # Let the sync scheduler stop this batch instead of treating a local
        # quota rejection like a missing Kalshi event and probing every
        # remaining candidate/family.
        raise
    except OutboundRateLimitUnavailable:
        # A configured shared limiter is required for safe provider access.
        # The caller can defer the batch until the limiter is healthy again.
        raise
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("kalshi_request_failed path=%s error=%s", path, exc)
        return None


def _discovery_candidates(payload: dict[str, Any], game: Any) -> list[str]:
    """Find bounded event candidates from one sport-series listing."""
    sport = (game.sport or "").lower()
    series = _KALSHI_GAME_SERIES.get(sport)
    if not series:
        return []
    dates = _date_keys(game)
    away_codes = _ticker_codes(game.away_team_abbrev, sport)
    home_codes = _ticker_codes(game.home_team_abbrev, sport)
    events = []
    for market in payload.get("markets") or []:
        ticker = market.get("event_ticker")
        if not ticker or not ticker.startswith(f"{series}-") or ticker in events:
            continue
        key = ticker.split("-", 1)[1]
        if any(date_key in key for date_key in dates) and any(code in key for code in away_codes) and any(code in key for code in home_codes):
            events.append(ticker)
    return events[:4]


def _family_markets(client: httpx.Client, series: str, game_key: str) -> list[dict[str, Any]]:
    """Fetch one market family directly, with one bounded series fallback."""
    event_ticker = f"{series}-{game_key}"
    payload = _get_json(client, f"/events/{event_ticker}") or {}
    markets = payload.get("markets") or []
    if markets:
        return markets
    cursor = None
    for _ in range(3):
        path = f"/markets?series_ticker={series}&status=open&limit=100"
        if cursor:
            path += f"&cursor={quote(cursor, safe='')}"
        discovery = _get_json(client, path) or {}
        matches = [
            market for market in discovery.get("markets") or []
            if market.get("event_ticker") == event_ticker
        ]
        if matches:
            return matches
        cursor = discovery.get("cursor")
        if not cursor:
            break
    return []


def _series_discovery_candidates(client: httpx.Client, series: str, game: Any) -> list[str]:
    candidates = []
    cursor = None
    for _ in range(5):
        path = f"/markets?series_ticker={series}&status=open&limit=100"
        if cursor:
            path += f"&cursor={quote(cursor, safe='')}"
        payload = _get_json(client, path) or {}
        candidates.extend(_discovery_candidates(payload, game))
        candidates = list(dict.fromkeys(candidates))
        if len(candidates) >= 4:
            return candidates[:4]
        cursor = payload.get("cursor")
        if not cursor:
            break
    return candidates[:4]


def _cache_key(game: Any) -> str:
    return ":".join([
        (game.sport or "").lower(), str(game.event_date or game.start_time.date()),
        (game.away_team_abbrev or "").upper(), (game.home_team_abbrev or "").upper(),
        str(game.start_time), str(getattr(game, "external_id", "") or ""),
    ])


def _negative_cache_ttl(game: Any) -> int:
    """Back off unopened far-future events without delaying nearby markets."""
    start = getattr(game, "start_time", None)
    if not isinstance(start, datetime):
        return KALSHI_NEGATIVE_CACHE_SECONDS
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    else:
        start = start.astimezone(timezone.utc)
    seconds_until_start = (start - datetime.now(timezone.utc)).total_seconds()
    if seconds_until_start > KALSHI_FAR_FUTURE_THRESHOLD_SECONDS:
        return KALSHI_FAR_FUTURE_NEGATIVE_CACHE_SECONDS
    return KALSHI_NEGATIVE_CACHE_SECONDS


def _event_matches_game(event_payload: dict[str, Any], game: Any, ticker: str) -> bool:
    event = event_payload.get("event") or {}
    if event.get("event_ticker") != ticker:
        return False
    if (game.sport or "").lower() == "epl" and ticker not in candidate_event_tickers(game):
        # EPL tickers are HOME then AWAY. A series discovery result with the
        # two club codes reversed must never be accepted for the same fixture.
        return False
    if (game.sport or "").lower() == "nba" and ticker not in candidate_event_tickers(game):
        # NBA tickers are AWAY then HOME. Require the exact code order as well
        # as a matching event title so a reversed fixture cannot be selected.
        return False
    text = " ".join(str(event.get(key) or "") for key in ("title", "sub_title")).upper()
    away_codes = _ticker_codes(game.away_team_abbrev, game.sport)
    home_codes = _ticker_codes(game.home_team_abbrev, game.sport)
    # The subtitle is the reliable structured team check in current responses.
    away_match = any(re.search(rf"\b{re.escape(code)}\b", text) for code in away_codes if code)
    home_match = any(re.search(rf"\b{re.escape(code)}\b", text) for code in home_codes if code)
    if not away_match:
        away_match = _team_in_text(text, game.away_team_abbrev, game.away_team, game.sport)
    if not home_match:
        home_match = _team_in_text(text, game.home_team_abbrev, game.home_team, game.sport)
    if not away_match or not home_match:
        return False
    return event.get("category") == "Sports"


def fetch_kalshi_odds(game: Any) -> Optional[dict[str, Any]]:
    token = _live_fetch.set(is_live(game))
    try:
        return _fetch_kalshi_odds(game)
    finally:
        _live_fetch.reset(token)


def _fetch_kalshi_odds(game: Any) -> Optional[dict[str, Any]]:
    """Fetch at most one verified event per market family."""
    sport = (game.sport or "").lower()
    if sport not in _KALSHI_GAME_SERIES:
        return None
    key = _cache_key(game)
    now = datetime.now(timezone.utc).timestamp()
    with _cache_lock:
        cached = _cache.get(key)
        cache_ttl = _negative_cache_ttl(game) if cached and cached[1] is None else (min(KALSHI_CACHE_SECONDS, KALSHI_LIVE_CACHE_SECONDS) if _live_fetch.get() else KALSHI_CACHE_SECONDS)
        if cached is not None and now - cached[0] < cache_ttl:
            return cached[1]

    candidates = candidate_event_tickers(game)
    if not candidates:
        return None
    sport_prefix = _KALSHI_SPORT_PREFIX[sport]
    selected_game_event = None
    selected_ticker = None
    try:
        with httpx.Client(timeout=KALSHI_TIMEOUT_SECONDS) as client:
            for candidate in candidates:
                payload = _get_json(client, f"/events/{candidate}")
                if payload and _event_matches_game(payload, game, candidate):
                    selected_game_event = payload
                    selected_ticker = candidate
                    break
            if not selected_game_event:
                series = _KALSHI_GAME_SERIES[sport]
                for candidate in _series_discovery_candidates(client, series, game):
                    payload = _get_json(client, f"/events/{candidate}")
                    if payload and _event_matches_game(payload, game, candidate):
                        selected_game_event = payload
                        selected_ticker = candidate
                        break
            if not selected_game_event:
                logger.warning(
                    "Kalshi game event not found: sport=%s away=%s home=%s dates=%s candidates=%s",
                    game.sport, game.away_team_abbrev, game.home_team_abbrev, _date_keys(game), candidates,
                )
                result = None
            else:
                game_key = selected_ticker.split("-", 1)[1]
                families = {
                    "MONEYLINE": selected_game_event.get("markets") or [],
                    "SPREAD": _family_markets(client, f"KX{sport_prefix}SPREAD", game_key),
                    "TOTAL": _family_markets(client, f"KX{sport_prefix}TOTAL", game_key),
                }
                normalized = [
                    item
                    for market_type, markets in families.items()
                    for market in markets
                    if (item := _normalize_market(
                        market,
                        market_type,
                        game,
                        market.get("event_ticker")
                        or (selected_ticker if market_type == "MONEYLINE" else f"KX{sport_prefix}{market_type}-{game_key}"),
                    ))
                ]
                moneyline = [m for m in normalized if m["market_type"] == "MONEYLINE"]
                spread = [m for m in normalized if m["market_type"] == "SPREAD"]
                total = [m for m in normalized if m["market_type"] == "TOTAL"]
                away = re.sub(r"[^A-Za-z0-9]", "", game.away_team_abbrev or "").upper()
                home = re.sub(r"[^A-Za-z0-9]", "", game.home_team_abbrev or "").upper()
                by_selection = {m["selection"]: m for m in moneyline}
                draw = by_selection.get("DRAW")
                result = {
                    "provider": "TailTyme Odds",
                    "source": "kalshi",
                    "event_ticker": selected_ticker,
                    "captured_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    "markets": normalized,
                    "available_market_types": sorted({market["market_type"] for market in normalized}),
                    "details": " · ".join(
                        f"{team} {by_selection[team]['display_odds']:+d}"
                        for team in (away, home) if team in by_selection and by_selection[team].get("tailtime_odds") is not None
                    ) or None,
                    "spread": _display_market(_representative(spread)),
                    "overUnder": _display_market(_representative(total)),
                    "awayMoneyline": by_selection.get(away, {}).get("display_odds"),
                    "homeMoneyline": by_selection.get(home, {}).get("display_odds"),
                    # Keep the legacy home/away fields while exposing the
                    # third EPL result for clients that still read the
                    # compact odds shape.
                    "drawMoneyline": draw.get("display_odds") if draw else None,
                }
                if len({market["market_type"] for market in normalized}) < 3:
                    logger.info(
                        "Kalshi partial market coverage: game=%s available=%s",
                        selected_ticker, sorted({market["market_type"] for market in normalized}),
                    )
    except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
        logger.debug("Kalshi odds normalization failed for %s: %s", key, exc)
        result = None

    with _cache_lock:
        _cache[key] = (now, result)
    return result


def _display_market(market: Optional[dict[str, Any]]) -> Optional[str]:
    if not market:
        return None
    line = market.get("line")
    odds = market.get("display_odds")
    if odds is None:
        return f"{market.get('selection')} {line}" if line is not None else market.get("selection")
    return f"{market.get('selection')} {line:+g} ({odds:+d})" if line is not None else f"{market.get('selection')} ({odds:+d})"
