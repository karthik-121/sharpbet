"""The Odds API (https://the-odds-api.com, v4) -> Market objects.

Two layers:
- parse_event / parse_events: pure functions from saved JSON to Markets (tested offline).
- OddsAPI + fetch_markets: the network client. What gets pulled is decided by the query
  (sport, markets, books); plan_cost estimates credits before anything is spent.

Cost rules (from the docs): each request costs [markets] x [regions], where every 10 named
bookmakers count as one region. Featured markets (h2h, spreads, totals) come from one /odds
call per sport. Everything else (alternates, props, h2h_3_way, periods) needs one
/events/{id}/odds call per game. /sports and /events are free.
"""
from __future__ import annotations

import json
import math
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..models import Market
from ..odds import american_to_decimal
from .env import load_env

BASE_URL = "https://api.the-odds-api.com/v4"

# The major legal US sportsbooks in the feed. "Any book" / "all books" means these. Up to 10 named books
# cost the same as one region, so all 8 plus Pinnacle (reference only) is still one region's credits.
# Not every one always returns prices (Caesars and Fanatics were missing on 2026-09-27); the tools say so.
US_BOOKS = ["fanduel", "draftkings", "betmgm", "williamhill_us", "betrivers", "espnbet", "fanatics", "hardrockbet"]
ALL_BOOKS_WORDS = {"all", "any", "all books", "any book", "any books", "every book", "every sportsbook",
                   "all sportsbooks", "any sportsbook", "everything"}
FEATURED = {"h2h", "spreads", "totals"}

SPORTS = {
    "nfl": "americanfootball_nfl",
    "ncaaf": "americanfootball_ncaaf",
    "mlb": "baseball_mlb",
    "nba": "basketball_nba",
    "wnba": "basketball_wnba",
    "ncaab": "basketball_ncaab",
    "nhl": "icehockey_nhl",
    "epl": "soccer_epl",
    "mls": "soccer_usa_mls",
    "ucl": "soccer_uefa_champs_league",
    "laliga": "soccer_spain_la_liga",
    "seriea": "soccer_italy_serie_a",
    "bundesliga": "soccer_germany_bundesliga",
    "ligue1": "soccer_france_ligue_one",
}

PERIODS = {
    "q1": "1st Quarter", "q2": "2nd Quarter", "q3": "3rd Quarter", "q4": "4th Quarter",
    "h1": "1st Half", "h2": "2nd Half",
    "p1": "1st Period", "p2": "2nd Period", "p3": "3rd Period",
    "1st_1_innings": "1st Inning", "1st_3_innings": "1st 3 Innings",
    "1st_5_innings": "1st 5 Innings", "1st_7_innings": "1st 7 Innings",
}

_GAME_MARKET = re.compile(r"^(alternate_)?(h2h_3_way|h2h|spreads|totals|team_totals)(?:_(.+))?$")
_PROP_PREFIXES = ("player_", "batter_", "pitcher_")


def sport_key(name: str) -> str:
    return SPORTS.get(name.lower(), name)


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _fmt_pt(p: float) -> str:
    return f"{p:g}"


def _price(p, odds_format: str) -> float | None:
    """Decimal odds from an API price, or None if it isn't a usable price. Pull American: US
    books post American odds, and the API rounds decimal to 2 places (FanDuel -108 comes back
    as 1.93, not 1.9259), which skews stakes. +100 and -100 are both even money (2.00).
    Returns None for malformed prices (e.g. American 0 or +50) and dead lines (<= 1.001,
    i.e. -100000 or worse, or decimal 1.00): that outcome is skipped, never the whole pull."""
    try:
        d = american_to_decimal(p) if odds_format == "american" else float(p)
    except (TypeError, ValueError):
        return None
    return d if d > 1.001 else None


# ---------------------------------------------------------------- parsing

def _game_market_sets(key: str, outcomes: list[dict], home: str, away: str, fmt: str):
    """Split one bookmaker market into complete outcome sets.
    Yields (group_key, label, kind, line, [(display_name, price)]) in canonical order."""
    m = _GAME_MARKET.match(key)
    if not m:
        return
    alt, base, period = m.groups()
    if period == "lay":
        return  # exchange lay markets are not bettable at a sportsbook
    suffix = f" ({PERIODS.get(period, period.replace('_', ' '))})" if period else ""
    by_name: dict[tuple, float] = {}
    for o in outcomes:
        d = _price(o.get("price"), fmt)
        if d is not None:
            by_name[(o["name"], o.get("point"), o.get("description"))] = d

    if base in ("h2h", "h2h_3_way"):
        three = base == "h2h_3_way" or any(n == "Draw" for n, _, _ in by_name)
        order = [home, "Draw", away] if three else [away, home]
        prices = [by_name.get((n, None, None)) for n in order]
        if None not in prices:
            label = ("3-Way Moneyline" if three else "Moneyline") + suffix
            yield (key,), label, "3way" if three else "moneyline", None, list(zip(order, prices))

    elif base == "spreads":
        for (name, pt, _), price in by_name.items():
            if name != away or pt is None:
                continue
            other = by_name.get((home, -pt, None))
            if other is None:
                continue
            label = ("Alt Spread" if alt else "Spread") + suffix
            yield ((key, pt), label, "spread", pt,
                   [(f"{away} {pt:+g}", price), (f"{home} {-pt:+g}", other)])

    elif base == "totals":
        for (name, pt, _), price in by_name.items():
            if name != "Over" or pt is None:
                continue
            under = by_name.get(("Under", pt, None))
            if under is None:
                continue
            label = ("Alt Total " if alt else "Total ") + _fmt_pt(pt) + suffix
            yield (key, pt), label, "total", pt, [(f"Over {_fmt_pt(pt)}", price),
                                                  (f"Under {_fmt_pt(pt)}", under)]

    elif base == "team_totals":
        for (name, pt, team), price in by_name.items():
            if name != "Over" or pt is None:
                continue
            under = by_name.get(("Under", pt, team))
            if under is None:
                continue
            label = f"{team} {'Alt ' if alt else ''}Team Total {_fmt_pt(pt)}{suffix}"
            yield (key, team, pt), label, "total", pt, [(f"{team} Over {_fmt_pt(pt)}", price),
                                                        (f"{team} Under {_fmt_pt(pt)}", under)]


def _prop_label(key: str) -> str:
    alt = key.endswith("_alternate")
    stem = key.removesuffix("_alternate")
    for p in _PROP_PREFIXES:
        stem = stem.removeprefix(p)
    words = stem.replace("_", " ").title()
    for short, full in (("Pass ", "Passing "), ("Rush ", "Rushing "), ("Reception ", "Receiving "),
                        ("Yds", "Yards"), ("Tds", "TDs"), ("Td", "TD")):
        words = words.replace(short, full)
    return words + (" (Alt)" if alt else "")


def _prop_sets(key: str, outcomes: list[dict], fmt: str):
    """Player props: description = player, name = Over/Under (or Yes/No), point = line.
    Props listed with only one side (e.g. anytime TD 'Yes') can't be covered, so are skipped."""
    by: dict[tuple, dict[str, float]] = {}
    for o in outcomes:
        d = _price(o.get("price"), fmt)
        if d is not None:
            by.setdefault((o.get("description"), o.get("point")), {})[o["name"]] = d
    stat = _prop_label(key)
    for (player, pt), sides in by.items():
        if player is None:
            continue
        for a, b in (("Over", "Under"), ("Yes", "No")):
            if a in sides and b in sides:
                line = "" if pt is None else f" {_fmt_pt(pt)}"
                yield ((key, player, pt), f"{player} {stat}{line}", "prop", pt,
                       [(f"{player} {a}{line}", sides[a]), (f"{player} {b}{line}", sides[b])])


def parse_event(event: dict, *, odds_format: str = "american", now: datetime | None = None,
                max_age: timedelta | None = None, game_name: str | None = None) -> list[Market]:
    """One event (from /odds or /events/{id}/odds) -> Markets. odds_format must match the
    oddsFormat the response was requested with.

    Each Market holds one exact line; books quoting a different number (FanDuel -3.5 vs
    DraftKings -3) land in different Markets, so a hedge is never silently a middle.
    Books whose prices are older than `max_age` are dropped. Games that have started are
    always skipped: live prices carry far more vig and move too fast to lock."""
    now = now or datetime.now(timezone.utc)
    start = _ts(event["commence_time"])
    if start <= now:
        return []
    home, away = event["home_team"], event["away_team"]
    game = game_name or f"{away} @ {home}"
    groups: dict[tuple, dict] = {}
    for bm in event.get("bookmakers", []):
        book = bm["key"]
        for mk in bm.get("markets", []):
            updated = _ts(mk.get("last_update") or bm["last_update"])
            if max_age is not None and now - updated > max_age:
                continue
            key = mk["key"]
            sets = (_prop_sets(key, mk["outcomes"], odds_format) if key.startswith(_PROP_PREFIXES)
                    else _game_market_sets(key, mk["outcomes"], home, away, odds_format))
            for gkey, label, kind, line, pairs in sets:
                g = groups.setdefault(gkey, {"label": label, "kind": kind, "line": line,
                                             "outcomes": [n for n, _ in pairs],
                                             "prices": {}, "updated": []})
                if [n for n, _ in pairs] != g["outcomes"]:
                    continue
                g["prices"][book] = [float(p) for _, p in pairs]
                g["updated"].append(updated)
    return [Market(game, g["label"], g["outcomes"], g["prices"], kind=g["kind"], line=g["line"],
                   event_id=event["id"], sport=event.get("sport_key"), start=start,
                   updated_at=min(g["updated"]))
            for g in groups.values() if g["prices"]]


def parse_events(events: list[dict], **kw) -> list[Market]:
    """Several events -> Markets. Two different games with the same teams (an MLB series or
    doubleheader) get their day and start time added, e.g. 'Rays @ Phillies (Sun 3:05 PM)'."""
    now = kw.get("now") or datetime.now(timezone.utc)
    ids_by_name: dict[str, set] = {}
    for ev in events:
        if _ts(ev["commence_time"]) <= now:
            continue  # started games are skipped, so they don't count as a repeat
        ids_by_name.setdefault(f"{ev['away_team']} @ {ev['home_team']}", set()).add(ev["id"])
    out = []
    for ev in events:
        name = f"{ev['away_team']} @ {ev['home_team']}"
        if len(ids_by_name.get(name, ())) > 1:
            local = _ts(ev["commence_time"]).astimezone()
            name += f" ({local.strftime('%a')} {local.strftime('%I:%M %p').lstrip('0')})"
        out.extend(parse_event(ev, game_name=name, **kw))
    return out


# ---------------------------------------------------------------- network

class OddsAPIError(RuntimeError):
    pass


def plan_cost(markets: list[str], books: list[str], n_events: int = 0) -> int:
    """Upper-bound credit cost of a fetch_markets call (the API may charge less when a
    market returns no data). n_events = games pulled for non-featured markets."""
    regions = max(1, math.ceil(len(books) / 10))
    featured = [m for m in markets if m in FEATURED]
    extra = [m for m in markets if m not in FEATURED]
    return len(featured) * regions + n_events * len(extra) * regions


class OddsAPI:
    """Thin client. The key comes from ODDS_API_KEY (.env), never from code.
    save_dir: write every raw response there as JSON (for test fixtures)."""

    odds_format = "american"  # exact US prices; see _price

    def __init__(self, api_key: str | None = None, *, save_dir: str | Path | None = None,
                 timeout: float = 20):
        load_env()
        self._key = api_key or os.environ.get("ODDS_API_KEY")
        if not self._key:
            raise OddsAPIError("No API key. Copy .env.example to .env and set ODDS_API_KEY.")
        self.save_dir = Path(save_dir) if save_dir else None
        self.timeout = timeout
        self.remaining: int | None = None
        self.used: int | None = None
        self.spent_this_session = 0

    def _get(self, path: str, save_as: str | None = None, **params):
        q = {k: v for k, v in params.items() if v is not None}
        q["apiKey"] = self._key
        url = f"{BASE_URL}{path}?{urllib.parse.urlencode(q)}"
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as r:
                body = json.loads(r.read())
                h = r.headers
        except urllib.error.HTTPError as e:  # message never includes the URL (it holds the key)
            detail = e.read().decode(errors="replace")[:300]
            raise OddsAPIError(f"The Odds API {e.code} on {path}: {detail}") from None
        except urllib.error.URLError as e:
            raise OddsAPIError(f"Network error on {path}: {e.reason}") from None
        if h.get("x-requests-remaining") is not None:
            self.remaining = int(float(h["x-requests-remaining"]))
            self.used = int(float(h["x-requests-used"]))
            self.spent_this_session += int(float(h.get("x-requests-last", 0)))
        if self.save_dir and save_as:
            self.save_dir.mkdir(parents=True, exist_ok=True)
            (self.save_dir / f"{save_as}.json").write_text(json.dumps(body, indent=1))
        return body

    def sports(self) -> list[dict]:
        """In-season sports. Free."""
        return self._get("/sports")

    def events(self, sport: str) -> list[dict]:
        """Upcoming games without odds. Free."""
        return self._get(f"/sports/{sport_key(sport)}/events")

    def odds(self, sport: str, markets: list[str], books: list[str]) -> list[dict]:
        """Featured markets for every upcoming game in a sport."""
        sk = sport_key(sport)
        return self._get(f"/sports/{sk}/odds", save_as=f"{sk}__{'-'.join(markets)}__{self.odds_format}",
                         bookmakers=",".join(books), markets=",".join(markets),
                         oddsFormat=self.odds_format, dateFormat="iso")

    def event_odds(self, sport: str, event_id: str, markets: list[str], books: list[str]) -> dict:
        """Any market (alternates, props, 3-way, periods) for one game."""
        sk = sport_key(sport)
        return self._get(f"/sports/{sk}/events/{event_id}/odds",
                         save_as=f"{sk}__event_{event_id}__{'-'.join(markets)}__{self.odds_format}",
                         bookmakers=",".join(books), markets=",".join(markets),
                         oddsFormat=self.odds_format, dateFormat="iso")


def stamp_slip(slip, markets: list[Market]) -> None:
    """Set slip.quoted_at to the oldest source update among the markets its tickets use (so
    'Prices as of' reflects the data), and give each leg its game's start time for display."""
    used = {(l.game, l.label) for t in slip.tickets for l in t.legs}
    times = [m.updated_at for m in markets if (m.game, m.label) in used and m.updated_at]
    if times:
        slip.quoted_at = min(times).astimezone()
    starts = {(m.game, m.label): m.start for m in markets if m.start}
    for t in slip.tickets:
        for l in t.legs:
            l.start = l.start or starts.get((l.game, l.label))


@dataclass
class FetchResult:
    markets: list[Market]
    events: list[dict]
    credits_estimated: int
    credits_spent: int
    notes: list[str] = field(default_factory=list)


def fetch_markets(api: OddsAPI, sport: str, markets: list[str], books: list[str], *,
                  max_events: int = 5, event_ids: list[str] | None = None,
                  budget: int | None = None, max_age: timedelta | None = None,
                  now: datetime | None = None) -> FetchResult:
    """Pull exactly what a query needs and return Markets.

    markets: API market keys, e.g. ['h2h','spreads','totals'] or ['h2h_3_way','player_pass_yds'].
    Non-featured markets are pulled per game: `event_ids` if given, else the next
    `max_events` games. Refuses to run if the estimate exceeds `budget` credits."""
    now = now or datetime.now(timezone.utc)
    featured = [m for m in markets if m in FEATURED]
    extra = [m for m in markets if m not in FEATURED]
    spent_before = api.spent_this_session
    events: list[dict] = []
    notes = []

    targets: list[str] = []
    if extra:
        if event_ids:
            targets = list(event_ids)
        else:
            upcoming = sorted((e for e in api.events(sport) if _ts(e["commence_time"]) > now),
                              key=lambda e: e["commence_time"])
            targets = [e["id"] for e in upcoming[:max_events]]
            if len(upcoming) > max_events:
                notes.append(f"{', '.join(extra)}: pulled the next {max_events} of "
                             f"{len(upcoming)} games (raise max_events for more).")
    estimate = plan_cost(markets, books, len(targets))
    if budget is not None and estimate > budget:
        raise OddsAPIError(f"This pull would cost up to {estimate} credits (budget {budget}).")

    if featured:
        events.extend(api.odds(sport, featured, books))
    for eid in targets:
        try:
            events.append(api.event_odds(sport, eid, extra, books))
        except OddsAPIError as e:
            notes.append(str(e))
    parsed = parse_events(events, odds_format=api.odds_format, now=now, max_age=max_age)
    return FetchResult(parsed, events, estimate, api.spent_this_session - spent_before, notes)
