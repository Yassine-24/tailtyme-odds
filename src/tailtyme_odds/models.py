"""Small input model for scheduled games passed to the odds adapter."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime


@dataclass(frozen=True, slots=True)
class Game:
    """A scheduled game with provider and display team identifiers.

    ``start_time`` should be timezone-aware. ``event_date`` may be supplied
    when a league's event date differs from the UTC date of ``start_time``.
    """

    sport: str
    away_team_abbrev: str
    home_team_abbrev: str
    away_team: str
    home_team: str
    start_time: datetime
    event_date: date | None = None
    venue_timezone: str | None = None
    external_id: str | None = None
    status: str | None = None
    provider_status: str | None = None
    home_score: float | None = None
    away_score: float | None = None
