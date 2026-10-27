"""Choose which markets the tools may use: sports to use or avoid, how soon games start.

The user says this in conversation ("no baseball", "only football", "settled by Sunday");
the LLM passes it here before calling the bonus/boost tools.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .models import Market

# Everyday names -> API sport key or key prefix (a prefix matches every league in that sport).
SPORT_ALIASES = {
    "nfl": "americanfootball_nfl", "ncaaf": "americanfootball_ncaaf",
    "college football": "americanfootball_ncaaf", "cfb": "americanfootball_ncaaf",
    "football": "americanfootball",
    "mlb": "baseball_mlb", "baseball": "baseball",
    "nba": "basketball_nba", "wnba": "basketball_wnba", "ncaab": "basketball_ncaab",
    "college basketball": "basketball_ncaab", "basketball": "basketball",
    "nhl": "icehockey_nhl", "hockey": "icehockey",
    "epl": "soccer_epl", "premier league": "soccer_epl", "mls": "soccer_usa_mls",
    "ucl": "soccer_uefa_champs_league", "champions league": "soccer_uefa_champs_league",
    "laliga": "soccer_spain_la_liga", "seriea": "soccer_italy_serie_a",
    "bundesliga": "soccer_germany_bundesliga", "ligue1": "soccer_france_ligue_one",
    "soccer": "soccer",
}


def _matches(sport: str | None, names: list[str]) -> bool:
    if not sport:
        return False
    for n in names:
        key = SPORT_ALIASES.get(n.strip().lower(), n.strip().lower())
        if sport.startswith(key):
            return True
    return False


def select_markets(markets: list[Market], *, sports: list[str] | None = None,
                   avoid: list[str] | None = None, within_days: float | None = None,
                   min_lead_minutes: float = 30, now: datetime | None = None) -> list[Market]:
    """Markets the user is willing to use.

    sports: only these ('nfl', 'football', 'soccer', 'baseball_mlb', ...). None = all.
    avoid: never these (e.g. ['mlb'] for rain risk). Wins over `sports`.
    within_days: only games starting within this many days (a grid ties up the bonus until
        its last leg settles).
    min_lead_minutes: skip games starting sooner than this; placing a grid takes minutes, and
        a leg that goes live mid-placement breaks the lock.
    """
    now = now or datetime.now(timezone.utc)
    out = []
    for m in markets:
        if sports and not _matches(m.sport, sports):
            continue
        if avoid and _matches(m.sport, avoid):
            continue
        if m.start is not None:
            if m.start < now + timedelta(minutes=min_lead_minutes):
                continue
            if within_days is not None and m.start > now + timedelta(days=within_days):
                continue
        out.append(m)
    return out


# ---------------------------------------------------------------- time slots ("SNF", "the 1 PM games")

from zoneinfo import ZoneInfo  # noqa: E402

ET = ZoneInfo("America/New_York")
# slot -> (weekday Mon=0..Sun=6 or None for any day, earliest ET hour, latest ET hour exclusive, description)
SLOTS = {
    "snf": (6, 19, 24, "Sunday Night Football (Sunday, 7 PM ET or later)"),
    "mnf": (0, 19, 24, "Monday Night Football (Monday, 7 PM ET or later)"),
    "tnf": (3, 19, 24, "Thursday Night Football (Thursday, 7 PM ET or later)"),
    "sunday_early": (6, 12, 15, "Sunday early games (the 1 PM ET slate)"),
    "sunday_late": (6, 15, 19, "Sunday late afternoon (the 4:05 / 4:25 PM ET slate)"),
    "prime_time": (None, 19, 24, "prime time (7 PM ET or later, any day)"),
    "sunday": (6, 0, 24, "all Sunday games"),
    "saturday": (5, 0, 24, "all Saturday games"),
}
SLOT_ALIASES = {
    "sunday night": "snf", "sunday night football": "snf", "snf": "snf",
    "monday night": "mnf", "monday night football": "mnf", "mnf": "mnf",
    "thursday night": "tnf", "thursday night football": "tnf", "tnf": "tnf",
    "1pm": "sunday_early", "1 pm": "sunday_early", "early": "sunday_early", "sunday early": "sunday_early",
    "sunday_early": "sunday_early", "early slate": "sunday_early",
    "late afternoon": "sunday_late", "late": "sunday_late", "4pm": "sunday_late", "4 pm": "sunday_late",
    "sunday_late": "sunday_late", "late slate": "sunday_late",
    "prime time": "prime_time", "primetime": "prime_time", "prime_time": "prime_time", "night": "prime_time",
    "sunday": "sunday", "saturday": "saturday",
}


def slot_key(name: str) -> str | None:
    return SLOT_ALIASES.get(name.strip().lower())


def in_slot(m: Market, slot: str) -> bool:
    """Does this game start in the named slot? Times are Eastern, as NFL slots are named."""
    key = slot_key(slot) or slot
    if key not in SLOTS or m.start is None:
        return False
    day, lo, hi, _ = SLOTS[key]
    t = m.start.astimezone(ET)
    return (day is None or t.weekday() == day) and lo <= t.hour < hi


def game_matches(m: Market, teams: list[str]) -> bool:
    """Restrict to specific games by team name (any word of the matchup, case-insensitive)."""
    g = m.game.lower()
    return any(t.strip().lower() in g for t in teams if t.strip())
