"""Comparing promo offers (sharpbet/compare.py): each option is planned with the existing planners,
unchanged, on the same markets, books and cash."""
import pytest

from sharpbet.compare import (BonusPromo, compare_markdown, compare_options, compare_to_dict, describe_promos,
                              evaluate_option)
from sharpbet.models import Market
from sharpbet.multiboost import Boost, plan_boosts
from sharpbet.odds import to_decimal
from sharpbet.plan import promo_plan

D = to_decimal


def total(game="G1", over=-110, under=-110, book="fanduel"):
    return Market(game, "Total 44.5", ["Over 44.5", "Under 44.5"], {book: [D(over), D(under)]},
                  kind="total", line=44.5, event_id=game)


def two_fifties(cap=200):
    return [Boost("fanduel", 0.5, cap, "50% boost #1"), Boost("fanduel", 0.5, cap, "50% boost #2")]


def test_option_equals_existing_planner():
    ms = [total()]
    r = evaluate_option(ms, "A", two_fifties(), ["fanduel"])
    ref = plan_boosts(ms, two_fifties(), ["fanduel"])
    assert r.locked_profit == pytest.approx(ref.locked_profit, abs=0.01) == 72.73   # CLAUDE.md reference
    assert r.cash_required == 400.00


def test_two_fifties_vs_one_hundred_ranked_with_margin():
    ms = [total()]
    one = [Boost("fanduel", 1.0, 100, "100% boost")]
    c = compare_options(ms, [("One 100% boost", one), ("Two 50% boosts", two_fifties())], ["fanduel"])
    a = plan_boosts(ms, two_fifties(), ["fanduel"]).locked_profit
    b = plan_boosts(ms, one, ["fanduel"]).locked_profit
    assert c.best.name == "Two 50% boosts" and a > b
    assert c.margin == pytest.approx(round(a, 2) - round(b, 2), abs=0.01)
    assert f"${c.margin:,.2f} more than One 100% boost" in compare_markdown(c)


def test_bonus_option_equals_promo_plan():
    ms = [total("G1"), total("G2"), total("G3")]
    r = evaluate_option(ms, "Bonus", [BonusPromo("fanduel", 100)], ["fanduel"], max_cash=0)
    ref = promo_plan(ms, "fanduel", bonus=100, max_cash=0)
    assert r.locked_profit == pytest.approx(ref.locked_profit, abs=0.01) and r.cash_required == 0


def test_mixed_option_shares_the_cash_limit():
    ms = [total("G1"), total("G2"), total("G3", over=+105, under=-125)]
    promos = [BonusPromo("fanduel", 100), Boost("fanduel", 0.5, 250, "50% boost")]
    r = evaluate_option(ms, "Mix", promos, ["fanduel"], max_cash=300)
    assert r.cash_required <= 300 and r.boosts is not None and r.bonuses
    assert r.locked_profit == pytest.approx(r.boosts.locked_profit + r.bonuses[0].locked_profit, abs=0.01)


def test_winner_can_change_with_more_cash():
    # one 100% boost needs a big cash hedge; two 50% boosts hedge each other with little cash
    ms = [total()]
    one = [Boost("fanduel", 1.0, 1000, "100% boost")]
    opts = [("Two 50% boosts", two_fifties()), ("One 100% boost", one)]
    tight = compare_options(ms, opts, ["fanduel"], max_cash=400)
    loose = compare_options(ms, opts, ["fanduel"])
    assert tight.best.name == "Two 50% boosts" and loose.best.name == "One 100% boost"
    assert "with no cash limit, One 100% boost would lock" in compare_markdown(tight)
    assert compare_to_dict(tight)["comparison"]["note"].startswith("The winner depends on cash")


def test_nothing_locks():
    ms = [total(over=-150, under=+110)]   # no boost of 1% locks anything here
    tiny = [Boost("fanduel", 0.01, 100, "1% boost")]
    c = compare_options(ms, [("A", tiny), ("B", list(tiny))], ["fanduel"])
    assert "None of these options locks a profit" in compare_markdown(c)
    assert compare_to_dict(c)["empty"] is True


def test_describe_promos():
    assert describe_promos(two_fifties(300)) == "2 × 50% FanDuel boost ($300 max each)"
    assert describe_promos([BonusPromo("draftkings", 100), Boost("fanduel", 1.0, 250, "x")]) == \
        "$100.00 DraftKings bonus bet + 100% FanDuel boost ($250 max)"


def test_web_data_has_comparison_rows_and_winner_slip():
    ms = [total()]
    c = compare_options(ms, [("A", two_fifties()), ("B", [Boost("fanduel", 1.0, 100, "100% boost")])], ["fanduel"])
    d = compare_to_dict(c)
    rows = d["comparison"]["rows"]
    assert [r["name"] for r in rows] == ["A", "B"] and rows[0]["best"] and not rows[1]["best"]
    assert d["locked_profit"] == rows[0]["locked_profit"] and d["sections"]
