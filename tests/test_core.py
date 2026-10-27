"""Tests against numbers worked out by hand earlier."""
import pytest

from sharpbet import (Market, Ticket, Leg, to_decimal, grid_conversion, solve_grid, best_grid, resolve_grid,
                      bonus_hedge, boost_lock, boost_ev, boost_plan, check_bonus_math,
                      round_to_cents, devig, kelly_fraction)

M110 = to_decimal(-110)


def ml(game, a, b, book="fanduel", **extra):
    prices = {book: [to_decimal(a), to_decimal(b)]}
    for bk, (x, y) in extra.items():
        prices[bk] = [to_decimal(x), to_decimal(y)]
    return Market(game, "Moneyline", ["Home", "Away"], prices)


# ------------------------------------------------------------- odds

def test_odds_conversion():
    assert to_decimal(-110) == pytest.approx(1.909091, abs=1e-6)
    assert to_decimal("+300") == 4.0
    assert sum(devig([M110, M110])) == pytest.approx(1)


def test_kelly():
    assert kelly_fraction(0.5, 2.1) == pytest.approx(0.0454545, abs=1e-6)
    assert kelly_fraction(0.4, 2.0) == 0


# ------------------------------------------------------------- grid

@pytest.mark.parametrize("legs,expected", [(3, 0.7447), (4, 0.7677), (5, 0.7612), (6, 0.7408)])
def test_grid_matches_hand_table(legs, expected):
    assert grid_conversion([[M110, M110]] * legs) == pytest.approx(expected, abs=5e-5)


def test_four_leg_grid_slip():
    markets = [ml(f"Game {c}", -110, -110) for c in "ABCD"]
    slip = solve_grid(markets, "fanduel", 100)
    assert len(slip.tickets) == 16
    assert all(t.stake == 6.25 for t in slip.tickets)
    assert slip.locked_profit == pytest.approx(76.77, abs=0.01)
    assert sum(t.stake for t in slip.tickets) == pytest.approx(100)
    assert any("both sides" in w for w in slip.warnings)


def test_mixed_grid_beats_plain_grid():
    # Two flat 3-way markets (~2.86, 5% hold) + one -110 leg: ~81.0% per earlier hand math.
    d = 3 / 1.05
    three = [Market(f"Soccer {c}", "1X2", ["Home", "Draw", "Away"], {"fanduel": [d, d, d]}, kind="3way")
             for c in "AB"]
    assert grid_conversion([[d] * 3, [d] * 3, [M110, M110]]) == pytest.approx(0.810, abs=0.002)
    pool = three + [ml(f"Game {c}", -110, -110) for c in "ABCDE"]
    slip = best_grid(pool, "fanduel", 100)
    assert slip.conversion > 0.80


def test_optimizer_skips_push_lines_and_same_game():
    push = Market("Game Z", "Spread", ["Z -3", "Z +3"], {"fanduel": [M110, M110]}, kind="spread", line=3)
    pool = [push] + [ml(f"Game {c}", -110, -110) for c in "ABCD"]
    slip = best_grid(pool, "fanduel", 100)
    assert all(l.game != "Game Z" for l in slip.tickets[0].legs)


def test_resolve_after_price_move():
    markets = [ml(f"Game {c}", -110, -110) for c in "ABCD"]
    slip = solve_grid(markets, "fanduel", 100)
    placed = {n: slip.tickets[n - 1].decimal for n in range(1, 13)}
    new = resolve_grid(slip, placed, moved={13: to_decimal(1200)})
    assert sum(t.stake for t in new.tickets) == pytest.approx(100)
    # ticket 13 got worse odds so the lock drops slightly below 76.77
    assert 76.0 < new.locked_profit < 76.77


# ------------------------------------------------------------- hedge

def test_two_book_hedge_example():
    # $100 bonus on +300, hedge -275 elsewhere: hedge $220, lock $80.
    m = Market("Pats-Jags", "Total 50.5", ["Over", "Under"],
               {"betmgm": [4.0, to_decimal(-400)], "fanduel": [to_decimal(250), to_decimal(-275)]},
               kind="total", line=50.5)
    slip = bonus_hedge([m], "betmgm", 100, hedge_books=["fanduel"])
    assert slip.tickets[0].legs[0].selection == "Over"
    assert slip.tickets[1].stake == 220.0
    assert slip.locked_profit == pytest.approx(80, abs=0.01)
    assert slip.cash_required == 220.0


def test_single_book_hedge_uses_only_that_book():
    m = Market(
        "Game A", "Moneyline", ["Dog", "Fav"],
        {"fanduel": [4.0, to_decimal(-380)], "draftkings": [3.8, to_decimal(-300)]})
    single = bonus_hedge([m], "fanduel", 100, single_book=True)
    multi = bonus_hedge([m], "fanduel", 100)
    assert all(t.book == "fanduel" for t in single.tickets)
    assert multi.conversion > single.conversion


# ------------------------------------------------------------- boost

def test_boost_lock_example():
    # 50% boost: +200 -> +300. Other side -225. $25 max -> hedge $69.23, lock $5.77.
    m = Market("Game X", "Moneyline", ["Dog", "Fav"], {"fanduel": [3.0, to_decimal(-225)]})
    slip = boost_lock([m], "fanduel", 0.5, 25, single_book=True)
    assert slip.tickets[0].decimal == pytest.approx(4.0)
    assert slip.tickets[1].stake == 69.23
    assert slip.locked_profit == pytest.approx(5.77, abs=0.01)


def test_boost_no_lock_falls_back_to_ev():
    m = Market("Game Y", "Moneyline", ["A", "B"],
               {"fanduel": [M110, M110], "pinnacle": [to_decimal(-104), to_decimal(-104)]})
    plan = boost_plan([m], "fanduel", 0.05, 25)
    assert plan["single_book_lock"] is None and plan["multi_book_lock"] is None
    assert plan["no_lock_message"]
    assert plan["best_ev"][0]["fair_source"] == "pinnacle"


def test_boost_min_odds_respected():
    m = Market("Game X", "Moneyline", ["Dog", "Fav"], {"fanduel": [3.0, to_decimal(-225)]})
    assert boost_lock([m], "fanduel", 0.5, 25, min_odds=3.5) is None


# ------------------------------------------------------------- checks

def test_check_my_math_87_percent():
    odds = [500, 524, 524, 500, 920, 880, 806, 1380, 806]
    res = check_bonus_math([to_decimal(o) for o in odds])
    assert res["arb_calculator_shows"] == pytest.approx(0.8755, abs=5e-4)
    assert res["true_bonus_conversion_optimal"] < 0.7677  # worse than the 4-leg grid


def test_round_to_cents_hits_total():
    s = round_to_cents([33.333, 33.333, 33.334], 100)
    assert sum(s) == pytest.approx(100)


# ------------------------------------------------------------- market scan / shortlist

def test_grid_value_prefers_better_prices_and_balance():
    from sharpbet import grid_value
    d = to_decimal
    assert grid_value([d(-108), d(-108)]) > grid_value([d(-110), d(-110)])
    # same 3.85% hold, but the lopsided line is a worse grid leg
    even = [d(-108), d(-108)]
    lopsided = [1 / (0.75 * 1.0384615), 1 / (0.25 * 1.0384615)]
    assert sum(1 / x for x in even) == pytest.approx(sum(1 / x for x in lopsided), abs=1e-3)
    assert grid_value(even) > grid_value(lopsided)


def test_three_way_legs_survive_the_shortlist():
    """A full slate of -110 two-ways must not crowd out 1X2 legs that build a better grid
    (the old lowest-hold-overall cutoff found 76.77% here instead of 80.61%)."""
    slate = [Market(f"G{i}", "Spread", ["A", "B"], {"fanduel": [M110, M110]}, kind="spread", line=3.5)
             for i in range(16)]
    slate += [Market(f"S{i}", "1X2", ["H", "D", "A"],
                     {"fanduel": [to_decimal(-110), to_decimal(260), to_decimal(300)]}, kind="3way")
              for i in range(4)]
    from sharpbet import optimize_grid
    top = optimize_grid(slate, "fanduel", 200)[0]
    assert top["conversion"] == pytest.approx(0.8061, abs=5e-5)
    assert [m.label for m in top["markets"]] == ["1X2"] * 3
    slip = best_grid(slate, "fanduel", 200)  # after cent rounding
    assert slip.conversion == pytest.approx(0.8053, abs=5e-4) and len(slip.tickets) == 27


def test_doubleheader_is_two_games():
    """Same team names, different event ids: legs may be combined; same event id may not."""
    from sharpbet import optimize_grid
    g1 = Market("NYY @ TB", "Moneyline", ["NYY", "TB"], {"fanduel": [M110, M110]}, event_id="e1")
    g2 = Market("NYY @ TB", "Moneyline", ["NYY", "TB"], {"fanduel": [M110, M110]}, event_id="e2")
    g1_total = Market("NYY @ TB", "Total 8.5", ["Over 8.5", "Under 8.5"], {"fanduel": [M110, M110]},
                      kind="total", line=8.5, event_id="e1")
    ranked = optimize_grid([g1, g2, g1_total], "fanduel", 100, min_legs=2, max_legs=2)
    assert ranked and all(len({m.event_key for m in r["markets"]}) == 2 for r in ranked)
    assert all({"e1", "e2"} == {m.event_id for m in r["markets"]} for r in ranked)


# ------------------------------------------------------------- boost vs cash

def _boost_slate():
    ms = [Market("Yankees @ Rays", "Total 7.5", ["Over 7.5", "Under 7.5"],
                 {"fanduel": [M110, M110]}, kind="total", line=7.5)]
    for g, (a, b) in zip(["Mets @ Braves", "Cubs @ Cardinals", "Reds @ Dodgers", "Rockies @ Padres"],
                         [(150, -175), (200, -245), (310, -390), (500, -700)]):
        ms.append(Market(g, "Moneyline", g.split(" @ "), {"fanduel": [to_decimal(a), to_decimal(b)]}))
    return ms


def test_boost_options_trade_profit_for_cash():
    from sharpbet import boost_lock_options
    opts = boost_lock_options(_boost_slate(), "fanduel", 0.5, 250, single_book=True)
    got = [(round(s.locked_profit, 2), round(s.cash_required, 2)) for s in opts]
    assert got[:2] == [(45.45, 767.05), (31.38, 559.52)]  # +150 dog beats the balanced -110 total
    assert all(a[0] > b[0] and a[1] > b[1] for a, b in zip(got, got[1:]))
    # the +310 dog locks less than the +150 dog yet needs far more cash, so it isn't offered
    assert not any("Reds" in s.tickets[0].legs[0].selection for s in opts)


def test_boost_lock_fits_max_cash():
    slip = boost_lock(_boost_slate(), "fanduel", 0.5, 250, single_book=True, max_cash=600)
    assert slip.cash_required <= 600
    assert slip.tickets[0].stake == pytest.approx(195.55)  # Mets, stake cut to fit
    assert slip.locked_profit == pytest.approx(35.55, abs=0.01)  # beats the $31.38 full-stake total
    assert any("cut from $250.00" in n for n in slip.notes)
    assert boost_lock(_boost_slate(), "fanduel", 0.5, 250, single_book=True).locked_profit == pytest.approx(45.45, abs=0.01)


def test_bonus_hedge_respects_max_cash():
    # +300 hedged at -275 converts 80% but needs $220; +150 hedged at -190 converts less, needs less.
    big = Market("Pats-Jags", "Total 50.5", ["Over", "Under"],
                 {"fanduel": [4.0, to_decimal(-275)]}, kind="total", line=50.5)
    small = Market("Game B", "Moneyline", ["Dog", "Fav"], {"fanduel": [to_decimal(150), to_decimal(-190)]})
    assert bonus_hedge([big, small], "fanduel", 100).locked_profit == pytest.approx(80, abs=0.01)
    capped = bonus_hedge([big, small], "fanduel", 100, max_cash=200)
    assert capped.tickets[0].legs[0].game == "Game B" and capped.cash_required <= 200
    # $50 still fits one hedge: bonus on the -190 favorite, $21.05 on the dog
    assert bonus_hedge([big, small], "fanduel", 100, max_cash=50).cash_required == pytest.approx(21.05)
    with pytest.raises(ValueError):
        bonus_hedge([big, small], "fanduel", 100, max_cash=5)  # cheapest hedge needs $9.09


def test_compare_methods_with_no_cash_is_grid_only():
    from sharpbet import compare_bonus_methods
    pool = [ml(f"Game {c}", -110, -110) for c in "ABCD"]
    pool.append(Market("Pats-Jags", "Total 50.5", ["Over", "Under"],
                       {"fanduel": [4.0, to_decimal(-275)]}, kind="total", line=50.5))
    everything = compare_bonus_methods(pool, "fanduel", 100)
    assert any(s.cash_required == 220.0 for s in everything)  # the $220 hedge is offered
    no_cash = compare_bonus_methods(pool, "fanduel", 100, max_cash=0)
    assert [s.cash_required for s in no_cash] == [0.0] and no_cash[0].conversion > 0.76


# ------------------------------------------------------------- postponement

def test_grid_void_payout_matches_hand_math():
    """4 x -110 grid, $100: if one leg is voided, two $6.25 tickets win as 3-leg parlays at
    +596: 2 x 6.25 x (1.9091^3 - 1) = $74.47 instead of $76.77."""
    from sharpbet.checks import grid_void_payout
    markets = [ml(f"Game {c}", -110, -110) for c in "ABCD"]
    markets[1].sport = "baseball_mlb"
    slip = solve_grid(markets, "fanduel", 100)
    assert grid_void_payout(slip.tickets, "Game B") == pytest.approx(74.47, abs=0.01)
    w = [x for x in slip.warnings if "postponed" in x][0]
    assert "$74.47" in w and "Most likely: Game B (rain)" in w


def test_every_straight_bet_gets_a_sport_specific_postponement_warning():
    nfl = Market("Jets @ Bills", "Moneyline", ["Jets", "Bills"],
                 {"fanduel": [4.0, to_decimal(-400)], "draftkings": [3.8, to_decimal(-300)]},
                 sport="americanfootball_nfl")
    mlb = Market("Mets @ Braves", "Moneyline", ["Mets", "Braves"],
                 {"fanduel": [4.0, to_decimal(-400)], "draftkings": [3.8, to_decimal(-300)]},
                 sport="baseball_mlb")
    nhl = Market("Bruins @ Rangers", "Moneyline", ["Bruins", "Rangers"],
                 {"fanduel": [4.0, to_decimal(-400)], "draftkings": [3.8, to_decimal(-300)]},
                 sport="icehockey_nhl")
    for m, cause in ((nfl, "severe weather"), (mlb, "rain or weather"), (nhl, "rare")):
        slip = bonus_hedge([m], "fanduel", 100)
        assert any("Postponement risk" in w and cause in w for w in slip.warnings)


# ------------------------------------------------------------- choosing markets

def test_select_markets_sports_avoid_and_timing():
    from datetime import datetime, timedelta, timezone
    from sharpbet import select_markets
    now = datetime(2026, 9, 27, 2, 0, tzinfo=timezone.utc)

    def mk(sport, minutes):
        return Market(f"{sport} {minutes}", "Moneyline", ["A", "B"], {"fanduel": [M110, M110]},
                      sport=sport, start=now + timedelta(minutes=minutes))
    ms = [mk("baseball_mlb", 600), mk("americanfootball_nfl", 900), mk("americanfootball_ncaaf", 20),
          mk("americanfootball_ncaaf", 120), mk("soccer_epl", 60 * 24 * 14)]
    sel = lambda **kw: sorted(m.game for m in select_markets(ms, now=now, **kw))
    # default: skip the game kicking off in 20 minutes
    assert "americanfootball_ncaaf 20" not in sel()
    assert sel(avoid=["mlb"]) == sel(avoid=["baseball"]) and not any("baseball" in g for g in sel(avoid=["mlb"]))
    assert sel(sports=["football"]) == ["americanfootball_ncaaf 120", "americanfootball_nfl 900"]
    assert sel(sports=["college football"]) == ["americanfootball_ncaaf 120"]
    assert sel(sports=["football"], avoid=["nfl"]) == ["americanfootball_ncaaf 120"]
    assert "soccer_epl 20160" not in sel(within_days=2)
    assert "americanfootball_ncaaf 20" in sel(min_lead_minutes=0)


# ------------------------------------------------------------- combined plan

def test_plan_shares_cash_between_bonus_and_boost():
    from sharpbet import promo_plan
    # a hedgeable +300/-275 market (bonus hedge needs $220 cash) plus the boost slate
    ms = _boost_slate() + [Market("Pats @ Jags", "Moneyline", ["Pats", "Jags"],
                                  {"fanduel": [4.0, to_decimal(-275)]})]
    plan = promo_plan(ms, "fanduel", bonus=100, boost_pct=0.5, boost_max_stake=250, max_cash=600)
    assert plan.cash_required <= 600
    # every bonus option was tried with the boost getting what's left; the pick is the best total
    from sharpbet import compare_bonus_methods, boost_lock
    for b in compare_bonus_methods(ms, "fanduel", 100, hedge_books=["fanduel"], max_cash=600):
        bl = boost_lock(ms, "fanduel", 0.5, 250, hedge_books=["fanduel"], max_cash=600 - b.cash_required)
        assert plan.locked_profit >= b.locked_profit + (bl.locked_profit if bl else 0) - 1e-9
    md = plan.to_markdown()
    assert md.startswith("## What to place") and "apply the boost" in md
    assert f"${plan.locked_profit:,.2f}" in md


def test_plan_without_cash_limit_and_bonus_only():
    from sharpbet import promo_plan
    pool = [ml(f"Game {c}", -110, -110) for c in "ABCD"]
    plan = promo_plan(pool, "fanduel", bonus=100, max_cash=0)
    assert plan.boost is None and plan.bonus.cash_required == 0
    assert plan.locked_profit == pytest.approx(76.77, abs=0.02)
    assert "16. **FanDuel**" in plan.to_markdown()


def test_boost_is_always_on_a_cash_bet_never_a_bonus_bet():
    """Books don't allow a profit boost on a bonus bet: every boost ticket is cash-funded."""
    from sharpbet import boost_lock_options
    for single in (True, False):
        for s in boost_lock_options(_boost_slate(), "fanduel", 0.5, 250, single_book=single,
                                    hedge_books=["fanduel"]):
            assert not any(t.is_bonus for t in s.tickets) and s.bonus_used == 0


def test_parlay_odds_display_rounds_down_like_fanduel():
    """Real FanDuel slip 2026-09-26: Titans +118, Rams -126, Vikings -122, Panthers -130 is
    exactly +1158.84 and the app shows +1158, but pays the exact odds ($13.72 → $158.99)."""
    from sharpbet.odds import fmt_american_parlay
    d = 1
    for a in (118, -126, -122, -130):
        d *= to_decimal(a)
    assert fmt_american_parlay(d) == "+1158"
    assert fmt_american_parlay(to_decimal(-110) ** 4) == "+1228"
    t = Ticket("fanduel", [Leg("g", "Moneyline", "x", to_decimal(a)) for a in (118, -126, -122, -130)], 13.72, True)
    assert round(t.returns, 2) == 158.99  # payout uses exact odds, as FanDuel does


def test_plan_unsplittable_bonus_skips_the_grid():
    from sharpbet import promo_plan
    pool = [ml(f"Game {c}", -110, -110) for c in "ABCD"]
    pool.append(Market("Pats @ Jags", "Moneyline", ["Pats", "Jags"], {"fanduel": [4.0, to_decimal(-275)]}))
    plan = promo_plan(pool, "fanduel", bonus=100, bonus_splittable=False)
    assert sum(t.is_bonus for t in plan.bonus.tickets) == 1  # one bonus bet + cash hedge


def test_resize_boost_lock_to_the_odds_the_book_shows():
    """Real FanDuel slip 2026-09-27: Chargers +295 with a 50% boost. We compute +442.5; the app
    showed +445 and paid $1,049.67 on $192.60, so the $807.39 hedge left the Bills side at $44.87
    and the Chargers side at $49.68. Re-sizing around +445 splits the extra evenly."""
    from sharpbet import resize_boost_lock
    m = Market("Chargers @ Bills", "Moneyline", ["Chargers", "Bills"],
               {"fanduel": [to_decimal(295), to_decimal(-370)], "draftkings": [to_decimal(270), to_decimal(-340)]})
    slip = boost_lock([m], "fanduel", 0.5, 200, hedge_books=["fanduel", "draftkings"], max_cash=1000)
    assert slip.tickets[0].stake == pytest.approx(192.60) and slip.locked_profit == pytest.approx(44.87, abs=0.01)
    shown = to_decimal(445)
    new = resize_boost_lock(slip, shown, max_cash=1000)
    assert new.cash_required <= 1000 and new.best_profit - new.locked_profit < 0.02
    assert slip.locked_profit < new.locked_profit < 49.68           # between the two old outcomes
    placed = resize_boost_lock(slip, shown, keep_stake=True, max_cash=1000)
    assert placed.tickets[0].stake == pytest.approx(192.60) and any("already placed" in n for n in placed.notes)
    worse = resize_boost_lock(slip, to_decimal(300), max_cash=1000)  # book shows far less: no lock
    assert worse.locked_profit < 0


# ------------------------------------------------------------- several boosts at once

def _total_market():
    return Market("A @ B", "Total 47.5", ["Over 47.5", "Under 47.5"], {"fanduel": [M110, M110]},
                  kind="total", line=47.5)


def test_solver_boost_vs_boost_matches_hand_math():
    """The motivating example: two 50% boosts on Over/Under -110 -> +136/+136, $200 each: $472.73 either
    way on $400 = $72.73. And 100% ($100, +182) vs 50% ($200, +136) with a $100 cash top-up: the same."""
    from sharpbet.multiboost import Boost, solve_market
    bs = [Boost("fanduel", .5, 200, "50% #1"), Boost("fanduel", .5, 200, "50% #2"), Boost("fanduel", 1.0, 100, "100%")]
    p = solve_market(_total_market(), {0: 0, 1: 1}, bs, ["fanduel"])
    assert p.locked_profit == pytest.approx(72.73, abs=0.01) and p.cash == pytest.approx(400)
    p = solve_market(_total_market(), {0: 2, 1: 0}, bs, ["fanduel"])
    assert p.locked_profit == pytest.approx(72.73, abs=0.01) and p.cash == pytest.approx(400)
    cash = [t for t in p.tickets if t.tag is None]
    assert len(cash) == 1 and cash[0].stake == pytest.approx(100) and cash[0].legs[0].selection == "Over 47.5"


def test_solver_single_boost_matches_boost_lock():
    from sharpbet.multiboost import Boost, solve_market
    for m in _boost_slate():
        old = boost_lock([m], "fanduel", 0.5, 250, single_book=True, fixed_pick=(m.game, m.label, m.outcomes[0]))
        new = solve_market(m, {0: 0}, [Boost("fanduel", .5, 250, "50%")], ["fanduel"])
        if old is None:
            assert new is None
        else:   # the new solver floors stakes to the cent; the old one rounds
            assert new.locked_profit == pytest.approx(old.locked_profit, abs=0.03)


def test_plan_boosts_pairs_boosts_uses_each_once_and_respects_cash():
    from sharpbet.multiboost import Boost, plan_boosts, OPPOSING_BOOSTS_WARNING
    ms = [_total_market()] + _boost_slate()
    bs = [Boost("fanduel", .5, 200, "50% #1"), Boost("fanduel", .5, 200, "50% #2")]
    mp = plan_boosts(ms, bs, ["fanduel"])
    tags = [t.tag for p in mp.plans for t in p.tickets if t.tag]
    assert sorted(tags) == ["50% #1", "50% #2"]                     # each boost used exactly once
    bets = [(p.key, t.legs[0].selection.split(" (")[0]) for p in mp.plans for t in p.tickets if t.tag]
    assert len(set(bets)) == len(bets)                              # never two boosts on one bet
    assert mp.locked_profit >= 72.73 - 0.01                         # at least the Over/Under pairing
    if any(len(p.assign) == 2 for p in mp.plans):
        assert any(OPPOSING_BOOSTS_WARNING in w for s in mp.slips() for w in s.warnings)
    tight = plan_boosts(ms, bs, ["fanduel"], max_cash=250)
    assert tight.cash_required <= 250 and tight.locked_profit > 0


def test_plan_boosts_explains_unused_boosts():
    from sharpbet.multiboost import Boost, plan_boosts
    bs = [Boost("fanduel", .5, 200, "50%"), Boost("fanduel", .5, 200, "50% SNF", allow=lambda m: False,
                                                  restriction="SNF only")]
    mp = plan_boosts([_total_market()], bs, ["fanduel"])
    assert [bi for bi, _ in mp.unused] == [1] and "no eligible games" in mp.unused[0][1]
