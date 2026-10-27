"""Adapter tests against real The Odds API responses saved 2026-09-27 ~01:51 UTC. No network."""
import copy
import io
import json
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from sharpbet import boost_plan, compare_bonus_methods, rank_markets
from sharpbet.checks import push_risk
from sharpbet.data import FetchResult, OddsAPI, OddsAPIError, fetch_markets, parse_events, plan_cost
from sharpbet.data import theoddsapi

FIX = Path(__file__).parent / "fixtures"  # files without an __american suffix are decimal
NOW = datetime(2026, 9, 27, 1, 52, tzinfo=timezone.utc)  # just after the pull
NFL_EVENT = "b225458a282e140c44c45651255f2f6c"  # Chargers @ Bills
FD = "fanduel"


def load(name):
    raw = json.loads((FIX / name).read_text())
    return raw if isinstance(raw, list) else [raw]


def markets(name, **kw):
    fmt = "american" if "__american" in name else "decimal"
    return parse_events(load(name), odds_format=fmt, now=NOW, **kw)


def find(ms, game, label):
    hits = [m for m in ms if m.game == game and m.label == label]
    assert len(hits) == 1, [(m.game, m.label) for m in ms if m.game == game]
    return hits[0]


# ------------------------------------------------------------- parsing

def test_epl_1x2_is_three_way_home_draw_away():
    m = find(markets("soccer_epl__h2h-spreads-totals.json"), "Leeds United @ Arsenal", "3-Way Moneyline")
    assert m.kind == "3way"
    assert m.outcomes == ["Arsenal", "Draw", "Leeds United"]
    assert m.prices[FD] == [2.78, 3.6, 2.4]
    assert m.prices["pinnacle"] == [2.83, 3.69, 2.47]
    assert m.event_id == "b870dcfba5e5920d3f1551a3aa1ea435"


def test_books_on_different_numbers_are_different_markets():
    ms = [m for m in markets("americanfootball_nfl__h2h-spreads-totals.json")
          if m.game == "Los Angeles Chargers @ Buffalo Bills" and m.kind == "spread"]
    by_line = {m.line: m for m in ms}
    assert set(by_line[7.5].prices) >= {FD} and FD not in by_line[7.0].prices
    assert by_line[7.5].outcomes == ["Los Angeles Chargers +7.5", "Buffalo Bills -7.5"]
    assert by_line[7.5].prices[FD] == [1.1, 7.4]
    assert push_risk(by_line[7.0]) and not push_risk(by_line[7.5])


def test_soccer_quarter_lines_are_push_risk():
    ms = markets("soccer_epl__h2h-spreads-totals.json")
    m = find(ms, "Leeds United @ Arsenal", "Spread")
    assert m.line == 1.25 and "quarter line" in push_risk(m)
    assert not any(m.kind != "3way" for m in rank_markets(ms, FD) for m in [m["market"]])


def test_alternates_drop_dead_prices_and_pair_both_sides():
    ms = markets(f"americanfootball_nfl__event_{NFL_EVENT}__alternate_spreads-alternate_totals-player_pass_yds.json")
    assert all(x > 1.0 for m in ms for p in m.prices.values() for x in p)
    alt = [m for m in ms if m.label == "Alt Spread" and FD in m.prices]
    for m in alt:  # each Market pairs team A at +x with team B at -x
        a, b = m.outcomes
        assert float(a.rsplit(" ", 1)[1]) == -float(b.rsplit(" ", 1)[1])
    assert len(alt) > 20


def test_player_prop_over_under():
    ms = markets(f"americanfootball_nfl__event_{NFL_EVENT}__alternate_spreads-alternate_totals-player_pass_yds.json")
    m = find(ms, "Los Angeles Chargers @ Buffalo Bills", "Josh Allen Passing Yards 238.5")
    assert m.kind == "prop" and m.line == 238.5
    assert m.outcomes == ["Josh Allen Over 238.5", "Josh Allen Under 238.5"]
    assert m.prices[FD] == [1.78, 2.07]


def test_nhl_regulation_three_way_at_fanduel():
    (m,) = markets("icehockey_nhl__event_9de33ce1013f2a3375dbded2c9fbc7d6__h2h_3_way.json")
    assert m.kind == "3way" and m.outcomes == ["Carolina Hurricanes", "Draw", "Florida Panthers"]
    assert m.prices[FD] == [2.7, 3.59, 2.42]


def test_started_games_and_stale_prices_are_dropped():
    name = "soccer_epl__h2h-spreads-totals.json"
    assert parse_events(load(name), odds_format="decimal", now=NOW + timedelta(days=60)) == []  # live: skipped
    assert markets(name)
    later = NOW + timedelta(minutes=30)
    assert parse_events(load(name), odds_format="decimal", now=later, max_age=timedelta(minutes=10)) == []
    assert parse_events(load(name), odds_format="decimal", now=later, max_age=timedelta(hours=1))


def test_doubleheader_names_include_start_time():
    ev = next(e for e in load("baseball_mlb__h2h-spreads-totals.json")
              if datetime.fromisoformat(e["commence_time"].replace("Z", "+00:00")) > NOW)
    ev2 = copy.deepcopy(ev)
    ev2["id"] = "second-game"
    ev2["commence_time"] = (datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00"))
                            + timedelta(hours=4)).isoformat()
    ms = parse_events([ev, ev2], odds_format="decimal", now=NOW)
    games = {m.event_id: m.game for m in ms}
    assert len(set(games.values())) == 2 and all("(" in g for g in games.values())


def test_updated_at_comes_from_source():
    m = find(markets("soccer_epl__h2h-spreads-totals.json"), "Leeds United @ Arsenal", "3-Way Moneyline")
    assert m.updated_at.tzinfo is not None and m.updated_at <= NOW


# ------------------------------------------------------------- end to end on the sample prices

def all_fixture_markets():
    out = []
    for f in ("americanfootball_nfl__h2h-spreads-totals.json", "baseball_mlb__h2h-spreads-totals.json",
              "soccer_epl__h2h-spreads-totals.json", "americanfootball_ncaaf__h2h-spreads-totals.json"):
        out += markets(f)
    return out


def test_real_slate_bonus_and_boost():
    ms = all_fixture_markets()
    slips = compare_bonus_methods(ms, FD, 200, hedge_books=[FD, "draftkings"])
    grid = next(s for s in slips if s.cash_required == 0)
    # the grid covers every outcome, uses different games, and pays the same on every ticket
    assert len({l.game for l in grid.tickets[0].legs}) == len(grid.tickets[0].legs)
    # cent rounding: one cent of stake on the longest ticket moves its payout by ~odds cents
    longest = max(t.decimal for t in grid.tickets)
    assert grid.best_profit - grid.locked_profit < 0.01 * longest
    assert 0.75 < grid.conversion < 0.95
    assert all(t.book == FD for s in slips for t in s.tickets if t.is_bonus)
    assert all(t.book != "pinnacle" for s in slips for t in s.tickets)  # reference only
    plan = boost_plan(ms, FD, 0.5, 250, hedge_books=[FD, "draftkings"], max_cash=600)
    for s in plan["single_book_options"] + plan["multi_book_options"]:
        assert s.cash_required <= 600 and s.locked_profit > 0
        assert all(t.book != "pinnacle" for t in s.tickets)
    assert plan["best_ev"][0]["fair_source"] in ("pinnacle", FD)


# ------------------------------------------------------------- client (no network)

def test_plan_cost():
    assert plan_cost(["h2h", "spreads", "totals"], ["fanduel", "draftkings", "pinnacle"]) == 3
    assert plan_cost(["h2h", "alternate_spreads"], ["fanduel"], n_events=5) == 6
    assert plan_cost(["h2h"], [f"b{i}" for i in range(11)]) == 2


def test_errors_never_contain_the_key(monkeypatch):
    def boom(url, timeout):
        raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, io.BytesIO(b'{"message":"bad key"}'))
    monkeypatch.setattr(theoddsapi.urllib.request, "urlopen", boom)
    api = OddsAPI(api_key="SECRET123")
    with pytest.raises(OddsAPIError) as e:
        api.sports()
    assert "SECRET123" not in str(e.value) and "401" in str(e.value)


class FakeAPI:
    """Serves the saved fixtures and records calls."""
    spent_this_session = 0
    odds_format = "decimal"

    def __init__(self):
        self.calls = []

    def odds(self, sport, mk, books):
        self.calls.append(("odds", sport, tuple(mk)))
        return load("americanfootball_nfl__h2h-spreads-totals.json")

    def events(self, sport):
        self.calls.append(("events", sport))
        return [{k: e[k] for k in ("id", "commence_time", "home_team", "away_team")}
                for e in load("americanfootball_nfl__h2h-spreads-totals.json")]

    def event_odds(self, sport, eid, mk, books):
        self.calls.append(("event_odds", eid, tuple(mk)))
        return load(f"americanfootball_nfl__event_{NFL_EVENT}__alternate_spreads-alternate_totals-player_pass_yds.json")[0]


def test_fetch_routes_featured_and_per_game_markets():
    api = FakeAPI()
    res = fetch_markets(api, "nfl", ["h2h", "alternate_spreads"], [FD], max_events=2, now=NOW)
    kinds = [c[0] for c in api.calls]
    # the free /events call comes first so the budget is checked before any credits are spent
    assert kinds == ["events", "odds", "event_odds", "event_odds"]
    assert api.calls[1][2] == ("h2h",) and api.calls[2][2] == ("alternate_spreads",)
    assert isinstance(res, FetchResult) and res.credits_estimated == 1 + 2
    assert any("next 2 of" in n for n in res.notes)


def test_fetch_refuses_over_budget():
    with pytest.raises(OddsAPIError):
        fetch_markets(FakeAPI(), "nfl", ["h2h", "alternate_spreads"], [FD], max_events=10, budget=5, now=NOW)


def test_featured_only_pull_makes_one_call():
    api = FakeAPI()
    fetch_markets(api, "nfl", ["h2h", "spreads", "totals"], [FD, "draftkings", "pinnacle"], now=NOW)
    assert api.calls == [("odds", "nfl", ("h2h", "spreads", "totals"))]


# ------------------------------------------------------------- American odds (exact prices)

AMERICAN_MLB = "baseball_mlb__h2h-spreads-totals__american.json"  # pulled 2026-09-27 ~02:15 UTC


def test_american_odds_are_exact():
    """The API rounds decimal odds to 2 places (a real -108 came back as 1.93, mis-sizing a grid by $0.22).
    American integers are exact. (Fixture prices are synthetic; see tests/fixtures/synthesize.py.)"""
    ms = parse_events(load(AMERICAN_MLB), odds_format="american", now=NOW)
    m = find(ms, "Atlanta Braves @ Miami Marlins", "Moneyline")
    raw = next(e for e in load(AMERICAN_MLB) if e["home_team"] == "Miami Marlins")
    for book in (FD, "pinnacle"):
        h2h = next(mk for b in raw["bookmakers"] if b["key"] == book for mk in b["markets"] if mk["key"] == "h2h")
        am = {o["name"]: o["price"] for o in h2h["outcomes"]}
        exact = [1 + a / 100 if a > 0 else 1 + 100 / -a for a in (am["Atlanta Braves"], am["Miami Marlins"])]
        assert m.prices[book] == exact                      # American integers convert exactly
    dec = find(markets("baseball_mlb__h2h-spreads-totals.json"), "Atlanta Braves @ Miami Marlins", "Moneyline")
    assert all(round(d, 2) == d for d in dec.prices[FD])     # the decimal feed is rounded to 2 places


def test_repeat_name_only_when_both_games_are_upcoming():
    """Saturday's Rays @ Phillies had started, so Sunday's game needs no time suffix."""
    ms = parse_events(load(AMERICAN_MLB), odds_format="american", now=NOW)
    assert any(m.game == "Tampa Bay Rays @ Philadelphia Phillies" for m in ms)
    assert not any("(" in m.game for m in ms)


def test_client_requests_american(monkeypatch):
    seen = {}

    class Resp(io.BytesIO):
        headers = {}
        def __enter__(self): return self
        def __exit__(self, *a): pass

    def fake(url, timeout):
        seen["url"] = url
        return Resp(b"[]")
    monkeypatch.setattr(theoddsapi.urllib.request, "urlopen", fake)
    OddsAPI(api_key="k").odds("mlb", ["h2h"], [FD])
    assert "oddsFormat=american" in seen["url"]


def _one_game(prices):
    return {"id": "x", "commence_time": "2026-09-27T17:00:00Z", "home_team": "Home", "away_team": "Away",
            "bookmakers": [{"key": FD, "last_update": "2026-09-27T01:50:00Z", "markets": [
                {"key": "h2h", "outcomes": [{"name": "Away", "price": prices[0]}, {"name": "Home", "price": prices[1]}]}]}]}


@pytest.mark.parametrize("prices,expected", [
    ((100, -120), [2.0, 1 + 100 / 120]),   # even money works
    ((-100, -100), [2.0, 2.0]),            # -100 is the same price as +100
    ((150, -100000), None),                # dead line: market dropped
    ((0, -110), None),                     # malformed price: market dropped, no crash
    ((50, -110), None),
    ((None, -110), None),
])
def test_american_edge_prices(prices, expected):
    ms = parse_events([_one_game(prices)], odds_format="american", now=NOW)
    if expected is None:
        assert ms == []
    else:
        assert ms[0].prices[FD] == pytest.approx(expected)
