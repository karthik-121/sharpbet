"""Grids of the size the user asks for (sharpbet/gridsize.py), on the synthetic fixture prices. The default
(most profitable) grid is unchanged; sizing is only a layer on request."""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from sharpbet.bonus import best_grid
from sharpbet.data.theoddsapi import parse_events
from sharpbet.gridsize import grid_options, sized_grid

FIX = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 9, 27, 1, 55, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def markets():
    ms = []
    for f, fmt in [("americanfootball_nfl__h2h-spreads-totals", "decimal"),
                   ("baseball_mlb__h2h-spreads-totals__american", "american"),
                   ("americanfootball_ncaaf__h2h-spreads-totals", "decimal"),
                   ("soccer_epl__h2h-spreads-totals", "decimal")]:
        ms += parse_events(json.loads((FIX / f"{f}.json").read_text()), odds_format=fmt, now=NOW)
    return ms


def test_sizes_available_and_their_locks(markets):
    opts = {o.tickets: (o.legs, round(o.locked_profit, 2)) for o in grid_options(markets, "fanduel", 1000)}
    assert opts[8] == (3, 777.38) and opts[12] == (3, 802.85) and opts[16] == (4, 811.62)
    assert opts[24] == (4, 817.68) and opts[32] == (5, 816.63)


def test_default_is_still_the_most_profitable_grid(markets):
    default = best_grid(markets, "fanduel", 1000)
    best = max(grid_options(markets, "fanduel", 1000), key=lambda o: o.locked_profit)
    assert len(default.tickets) == best.tickets == 24
    assert default.locked_profit == pytest.approx(best.locked_profit, abs=0.01)


def test_eight_bets_and_three_legs(markets):
    sg = sized_grid(markets, "fanduel", 1000, tickets=8)
    assert sg.choice.tickets == 8 and sg.choice.legs == 3 and len(sg.choice.slip.tickets) == 8
    assert all(len(t.legs) == 3 for t in sg.choice.slip.tickets)
    assert sum(t.stake for t in sg.choice.slip.tickets) == pytest.approx(1000, abs=0.001)
    others = sg.others()
    assert "16 bets (4-leg parlays): $811.62 (81.2%), $34.24 more" in others
    assert any("24 bets" in o and "(most profitable)" in o for o in others)
    # only sizes where more bets lock more: 9 (< 8), 18/27 (< 16) and 32+ (< 24) are left out
    assert [o.split(" bets")[0] for o in others] == ["4", "6", "12", "16", "24"]
    # a size left out of the list can still be asked for by name
    assert sized_grid(markets, "fanduel", 1000, tickets=9).choice.tickets == 9
    # "3-leg parlays" -> the best 3-leg grid, which here has a 3-way leg (18 tickets)
    assert sized_grid(markets, "fanduel", 1000, legs=3).choice.tickets == 18


def test_ceiling_and_impossible_size(markets):
    assert sized_grid(markets, "fanduel", 1000, max_tickets=10).choice.tickets == 8   # $777.38 beats 9 tickets ($776.25)
    sg = sized_grid(markets, "fanduel", 1000, tickets=10)
    assert sg.choice is None and sg.options and sg.request == "10 bets"


def test_fewer_bets_line_under_the_default_grid(markets):
    from sharpbet.gridsize import fewer_bets_line
    grid = best_grid(markets, "fanduel", 1000)                  # 24 bets, $817.68
    assert fewer_bets_line(markets, "fanduel", 1000, grid) == (
        "Short on time? 12 bets locks $802.85 ($14.83 less) · 8 bets locks $777.38 ($40.30 less). "
        "Just ask for either.")
    small = sized_grid(markets, "fanduel", 1000, tickets=6).choice.slip
    assert fewer_bets_line(markets, "fanduel", 1000, small) is None   # already a small grid
