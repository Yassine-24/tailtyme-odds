# TailTyme Odds

A small, read-only Python adapter that finds Kalshi game markets and normalizes their quotes into moneyline, spread, and total odds.

This package was extracted from the odds integration built for TailTyme. The original application remains a separate project; this repository contains a standalone version of the provider lookup and normalization logic.

## What it does

- Looks up NBA, NFL, MLB, and English Premier League game markets.
- Maps ESPN-style team abbreviations to Kalshi ticker variants.
- Builds bounded event-ticker candidates using game dates, scheduled times, and team aliases.
- Falls back to a bounded series-market search and verifies the event teams before accepting a match.
- Uses open, unsettled markets with valid bid and ask quotes, then calculates the midpoint probability.
- Normalizes moneyline, spread, and total markets, including NBA and EPL signed spread lines and EPL draw selections.
- Converts probabilities to American odds and applies a configurable display margin.
- Caches provider responses, retries transient failures, and applies a process-local request limit.

The package only reads public market data. It does not place wagers, handle accounts, or move money.

## Install

Requires Python 3.11 or newer.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

## Try the offline example

```bash
python examples/ticker_candidates.py
```

The example uses deterministic ticker and odds conversions; it does not make a network request.

## Fetch a game's markets

```python
from datetime import datetime, timezone

from tailtyme_odds import Game, fetch_kalshi_odds

game = Game(
    sport="nba",
    away_team_abbrev="NY",
    home_team_abbrev="GS",
    away_team="New York Knicks",
    home_team="Golden State Warriors",
    start_time=datetime(2026, 10, 21, 2, tzinfo=timezone.utc),
)

snapshot = fetch_kalshi_odds(game)
if snapshot is None:
    print("No verified open event was available")
else:
    for market in snapshot["markets"]:
        print(market["market_type"], market["selection"], market["display_odds"])
```

The returned snapshot includes the source event and market tickers, capture time, market status, quote, normalized probability, and display odds. A missing event or invalid/closed market is omitted rather than guessed.

## Configuration

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `KALSHI_API_BASE_URL` | Kalshi public trade API v2 | Override the provider base URL |
| `KALSHI_ODDS_TIMEOUT_SECONDS` | `2.5` | HTTP timeout per request |
| `KALSHI_ODDS_CACHE_SECONDS` | `300` | Positive response cache duration |
| `KALSHI_LIVE_ODDS_CACHE_SECONDS` | `30` | Cache duration for live games |
| `KALSHI_ODDS_NEGATIVE_CACHE_SECONDS` | `300` | Cache duration when no event is found |
| `KALSHI_FAR_FUTURE_NEGATIVE_CACHE_SECONDS` | `1800` | Longer miss cache for far-future games |
| `KALSHI_FAR_FUTURE_THRESHOLD_SECONDS` | `21600` | Start time beyond which a miss uses the longer cache |
| `KALSHI_API_RATE_LIMIT` | `120` | Maximum process-local provider requests per minute |
| `KALSHI_RESPONSE_CACHE_MAX_ENTRIES` | `512` | Maximum cached raw provider responses |
| `TAILTIME_ODDS_MARGIN` | `0.04` | Display-only probability margin |
| `LIVE_ODDS_STALE_SECONDS` | `120` | Freshness window for live-odds snapshots |

The rate limiter is process-local. A multi-worker deployment should put a shared limiter in the calling application rather than relying on this package-level guard.

## Source and reuse

The odds logic originated in `backend/app/utils/kalshi_odds.py` in the private TailTyme full-stack repository. This repo keeps the extracted adapter attributable to that project. No license has been selected; public visibility does not grant permission to copy, redistribute, or reuse the code.
