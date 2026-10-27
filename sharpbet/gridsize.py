"""Bonus grids of the size the user asks for: "make it 8 bets", "12 bets", "3-leg parlays",
"under 10 tickets".

The default grid is the most profitable one (bonus.best_grid). Users may trade a little profit for
fewer bets to place. This module reuses the existing grid search and solver unchanged
(bonus.optimize_grid, bonus.solve_grid) and only filters by size.

Ticket counts depend on the legs: 2-way legs give 4, 8, 16, 32; a 3-way leg (soccer 1X2, NHL
regulation) gives 6, 12, 24, ... So "8 bets" and "3-leg parlays" aren't always the same thing.
Saved 2026-09-26 prices, $1,000 FanDuel bonus: 8 tickets $770.72, 12 $791.18, 16 $800.81,
24 $802.50 (the default pick), 32 $800.30.
"""
from __future__ import annotations

from dataclasses import dataclass

from .bonus import optimize_grid, solve_grid
from .checks import DEFAULT_MIN_STAKE
from .models import Market, Slip


@dataclass
class GridOption:
    tickets: int
    legs: int
    slip: Slip

    @property
    def locked_profit(self) -> float:
        return self.slip.locked_profit

    def describe(self) -> str:
        return (f"{self.tickets} bets ({self.legs}-leg parlays): ${self.locked_profit:,.2f} "
                f"({self.slip.conversion:.1%})")


def grid_options(markets: list[Market], book: str, bonus: float, *,
                 min_stake: float = DEFAULT_MIN_STAKE) -> list[GridOption]:
    """The best grid for each ticket count available from these markets, fewest tickets first."""
    best: dict[int, dict] = {}
    for r in optimize_grid(markets, book, bonus, min_stake=min_stake):   # best conversion first
        best.setdefault(r["tickets"], r)
    return [GridOption(n, len(r["markets"]), solve_grid(r["markets"], book, bonus, min_stake))
            for n, r in sorted(best.items())]


@dataclass
class SizedGrid:
    choice: GridOption | None          # None: no grid of the requested size
    options: list[GridOption]          # every size available, fewest tickets first
    request: str                       # "8 bets", "3-leg parlays", "at most 10 bets"

    @property
    def best(self) -> GridOption | None:
        return max(self.options, key=lambda o: o.locked_profit, default=None)

    def worthwhile(self) -> list[GridOption]:
        """Sizes worth considering: each locks more than every smaller grid (9 bets locking less than
        8 never is). The other sizes can still be asked for by name."""
        out, top = [], float("-inf")
        for o in self.options:              # fewest tickets first
            if o.locked_profit > top + 0.005:
                out.append(o)
                top = o.locked_profit
        return out

    def others(self) -> list[str]:
        """Other worthwhile sizes, each with how much more or less it locks than the chosen grid."""
        ref = self.choice.locked_profit if self.choice else None
        out = []
        for o in self.worthwhile():
            if self.choice and o.tickets == self.choice.tickets:
                continue
            line = o.describe()
            if ref is not None:
                d = round(o.locked_profit - ref, 2)
                line += (f", ${d:,.2f} more" if d > 0 else f", ${-d:,.2f} less" if d < 0 else ", the same")
            if self.best and o.tickets == self.best.tickets:
                line += " (most profitable)"
            out.append(line)
        return out


def sized_grid(markets: list[Market], book: str, bonus: float, *, tickets: int | None = None,
               legs: int | None = None, max_tickets: int | None = None,
               min_stake: float = DEFAULT_MIN_STAKE) -> SizedGrid:
    """Most profitable grid that matches the request (exact ticket count, exact parlay length,
    and/or a ticket ceiling)."""
    opts = grid_options(markets, book, bonus, min_stake=min_stake)
    fits = [o for o in opts
            if (tickets is None or o.tickets == tickets) and (legs is None or o.legs == legs)
            and (max_tickets is None or o.tickets <= max_tickets)]
    parts = [f"{tickets} bets" if tickets else "", f"{legs}-leg parlays" if legs else "",
             f"at most {max_tickets} bets" if max_tickets else ""]
    return SizedGrid(max(fits, key=lambda o: o.locked_profit, default=None), opts,
                     ", ".join(p for p in parts if p) or "any size")


def fewer_bets_line(markets: list[Market], book: str, bonus: float, grid: Slip, *,
                    max_small: int = 12, show: int = 2, min_default: int = 8,
                    min_stake: float = DEFAULT_MIN_STAKE) -> str | None:
    """One line under the default grid offering smaller grids and what each gives up, e.g.
    "Short on time? 12 bets locks $791.18 ($11.32 less) · 8 bets locks $770.72 ($31.78 less).
    Just ask for either." People hesitate to place 16+ bets (time, and odds moving mid-placement); wording from user testing.
    None when the grid is already small or nothing smaller is worth it."""
    n = len(grid.tickets)
    if n < min_default:
        return None
    sg = SizedGrid(None, grid_options(markets, book, bonus, min_stake=min_stake), "")
    small = [o for o in sg.worthwhile() if o.tickets < n and o.tickets <= max_small][-show:][::-1]
    if not small:
        return None
    parts = [f"{o.tickets} bets locks ${o.locked_profit:,.2f} (${grid.locked_profit - o.locked_profit:,.2f} less)"
             for o in small]
    return f"Short on time? {' · '.join(parts)}. Just ask for {'either' if len(parts) > 1 else 'it'}."
