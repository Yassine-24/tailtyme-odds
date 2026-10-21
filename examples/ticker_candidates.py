from datetime import date, datetime, timezone

from tailtyme_odds import Game, american_odds, candidate_event_tickers


game = Game(
    sport="nba",
    away_team_abbrev="NY",
    home_team_abbrev="GS",
    away_team="New York Knicks",
    home_team="Golden State Warriors",
    event_date=date(2026, 10, 21),
    start_time=datetime(2026, 10, 21, 2, tzinfo=timezone.utc),
)

print("Likely event tickers:")
for ticker in candidate_event_tickers(game):
    print(f"  {ticker}")

print("\nAmerican odds for a 58% market probability:", american_odds(0.58))
