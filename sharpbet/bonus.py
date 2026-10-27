"""Bonus bet conversion.

Three ways to lock cash from a bonus bet (stake not returned):

1. Single-book grid  - split the bonus across every outcome combination of a parlay so
                       exactly one ticket wins. Needs no cash and only one book.
2. Single-book hedge - one bonus bet, cash hedges on the other outcomes at the same book.
3. Multi-book hedge  - one bonus bet, cash hedges at the best price across your other books.
"""
from __future__ import annotations

from itertools import combinations, product

from .checks import (round_to_cents, rule_checks, push_risk, grid_postponement_warning,
                     DEFAULT_MIN_STAKE)
from .models import Leg, Market, Slip, Ticket
from .odds import fmt_american


# ---------------------------------------------------------------- grid

def grid_conversion(leg_odds: list[list[float]]) -> float:
    """Conversion rate of a full-coverage bonus grid: 1 / sum over combos of 1/(D - 1),
    where D is the parlay's decimal odds. Stakes proportional to 1/(D - 1) pay equally."""
    total = 0.0
    for combo in product(*leg_odds):
        d = 1.0
        for x in combo:
            d *= x
        total += 1 / (d - 1)
    return 1 / total


def solve_grid(markets: list[Market], book: str, bonus: float,
               min_stake: float = DEFAULT_MIN_STAKE) -> Slip:
    """Exact grid slip for these markets at one book."""
    leg_odds = [m.odds_at(book) for m in markets]
    combos = list(product(*[range(len(o)) for o in leg_odds]))
    tickets, weights = [], []
    for combo in combos:
        legs = [Leg(m.game, m.label, m.outcomes[i], leg_odds[k][i])
                for k, (m, i) in enumerate(zip(markets, combo))]
        t = Ticket(book, legs, 0.0, is_bonus=True)
        tickets.append(t)
        weights.append(1 / (t.decimal - 1))
    raw = [bonus * w / sum(weights) for w in weights]
    for t, s in zip(tickets, round_to_cents(raw, bonus)):
        t.stake = s
    returns = [t.returns for t in tickets]
    slip = Slip(
        title=f"Place on {book}, using your bonus bet: {len(tickets)} parlays covering every outcome",
        tickets=tickets,
        locked_profit=min(returns),
        best_profit=max(returns),
        cash_required=0.0,
        bonus_used=bonus,
        conversion=min(returns) / bonus,
        method=(f"Single-book grid: {len(markets)} legs, {len(tickets)} tickets. "
                "Exactly one ticket wins, whatever happens."),
    )
    slip.warnings = rule_checks(markets, parlay=True, stakes=[t.stake for t in tickets],
                                min_stake=min_stake, same_book_both_sides=True)
    slip.warnings.append(grid_postponement_warning(markets, tickets, bonus))
    _check_grid_coverage(slip, markets)
    return slip


def _check_grid_coverage(slip: Slip, markets: list[Market]) -> None:
    seen = {tuple(l.selection for l in t.legs) for t in slip.tickets}
    expected = set(product(*[m.outcomes for m in markets]))
    if seen != expected:
        raise AssertionError("Grid does not cover every outcome exactly once")


_REF_LEG = [1 + 100 / 110] * 2  # a standard -110/-110 leg


def grid_value(odds: list[float]) -> float:
    """How good one market is as a grid leg: the conversion of a grid made of this market plus
    two standard -110/-110 legs. Ranks -108/-108 above -110/-110, and a balanced line above a
    lopsided one with the same hold (stakes scale with 1/(D - 1), which punishes short favorites)."""
    return grid_conversion([odds, _REF_LEG, _REF_LEG])


def rank_markets(markets: list[Market], book: str, top: int | None = None) -> list[dict]:
    """Every grid-eligible market at `book`, best grid leg first.
    Returns [{'market', 'game', 'label', 'prices', 'hold', 'grid_value'}].
    Skips push-prone lines and markets that don't list every outcome."""
    rows = []
    for m in markets:
        if book not in m.prices or len(m.outcomes) < 2 or push_risk(m):
            continue
        odds = m.odds_at(book)
        rows.append({
            "market": m, "game": m.game, "label": m.label,
            "prices": {o: fmt_american(d) for o, d in zip(m.outcomes, odds)},
            "hold": m.hold_at(book), "grid_value": grid_value(odds),
        })
    rows.sort(key=lambda r: r["grid_value"], reverse=True)
    return rows[:top] if top else rows


def shortlist_markets(markets: list[Market], book: str, per_shape: int = 12,
                      per_game: int = 2) -> list[Market]:
    """Best grid legs for each market shape (2-way, 3-way, ...), at most `per_game` per game
    and shape. Shapes are shortlisted separately because a 3-way market usually has more hold
    than a -110 two-way yet makes a better grid leg."""
    taken: dict[int, list[Market]] = {}
    per: dict[tuple[int, str], int] = {}
    for r in rank_markets(markets, book):
        m = r["market"]
        shape = len(m.outcomes)
        if len(taken.setdefault(shape, [])) >= per_shape or per.get((shape, m.event_key), 0) >= per_game:
            continue
        taken[shape].append(m)
        per[(shape, m.event_key)] = per.get((shape, m.event_key), 0) + 1
    return [m for ms in taken.values() for m in ms]


def optimize_grid(markets: list[Market], book: str, bonus: float, *,
                  min_legs: int = 2, max_legs: int = 5, max_tickets: int = 64,
                  top_n: int = 12, min_stake: float = DEFAULT_MIN_STAKE) -> list[dict]:
    """Try every leg combination from the best grid legs at `book` (top_n per market shape,
    see shortlist_markets) and rank grids by exact conversion.
    Returns [{'conversion', 'markets', 'tickets'}], best first.
    Push-prone lines are skipped; legs must be in different games."""
    pool = shortlist_markets(markets, book, per_shape=top_n)
    ranked = []
    for k in range(min_legs, max_legs + 1):
        for legs in combinations(pool, k):
            if len({m.event_key for m in legs}) < k:
                continue
            n = 1
            for m in legs:
                n *= len(m.outcomes)
            if n > max_tickets or bonus / n < min_stake * 2:  # rough min-stake guard
                continue
            conv = grid_conversion([m.odds_at(book) for m in legs])
            ranked.append({"conversion": conv, "markets": list(legs), "tickets": n})
    ranked.sort(key=lambda r: r["conversion"], reverse=True)
    return ranked


def best_grid(markets: list[Market], book: str, bonus: float, **kw) -> Slip:
    ranked = optimize_grid(markets, book, bonus, **kw)
    if not ranked:
        raise ValueError(f"No valid grid found at {book} with these markets")
    slip = solve_grid(ranked[0]["markets"], book, bonus, kw.get("min_stake", DEFAULT_MIN_STAKE))
    if len(ranked) > 1:  # compare like with like: the runner-up after cent rounding too
        r = solve_grid(ranked[1]["markets"], book, bonus, kw.get("min_stake", DEFAULT_MIN_STAKE))
        slip.notes.append(f"Next best grid: {len(r.tickets)} tickets, ${r.locked_profit:,.2f} "
                          f"({r.conversion:.2%}) vs ${slip.locked_profit:,.2f} ({slip.conversion:.2%}) here.")
    return slip


def resolve_grid(slip: Slip, placed: dict[int, float], moved: dict[int, float] | None = None) -> Slip:
    """Re-solve a grid after partial placement.

    placed: {ticket number (1-based): decimal odds it was actually placed at}. Its stake is kept.
    moved:  {ticket number: new decimal odds} for tickets not yet placed.
    Remaining bonus is spread over unplaced tickets so they all pay the same.
    """
    moved = moved or {}
    tickets = []
    for n, t in enumerate(slip.tickets, 1):
        q = placed.get(n, moved.get(n))
        tickets.append(Ticket(t.book, t.legs, t.stake, t.is_bonus,
                              quoted_decimal=q if q is not None else t.quoted_decimal))
    remaining = [i for i in range(len(tickets)) if (i + 1) not in placed]
    spent = sum(tickets[i].stake for i in range(len(tickets)) if (i + 1) in placed)
    left = slip.bonus_used - spent
    if remaining:
        w = [1 / (tickets[i].decimal - 1) for i in remaining]
        stakes = round_to_cents([left * x / sum(w) for x in w], left)
        for i, s in zip(remaining, stakes):
            tickets[i].stake = s
    returns = [t.returns for t in tickets]
    new = Slip(
        title=slip.title + " (re-solved)",
        tickets=tickets,
        locked_profit=min(returns),
        best_profit=max(returns),
        cash_required=0.0,
        bonus_used=slip.bonus_used,
        conversion=min(returns) / slip.bonus_used,
        method=slip.method,
        warnings=list(slip.warnings),
    )
    new.notes.append(f"Keep tickets {sorted(placed)} as placed; place the others at the new stakes.")
    return new


# ---------------------------------------------------------------- hedge

def bonus_hedge(markets: list[Market], bonus_book: str, bonus: float, *,
                hedge_books: list[str] | None = None, single_book: bool = False,
                fixed_pick: tuple[str, str, str] | None = None,
                max_cash: float | None = None) -> Slip:
    """Best one-bonus-bet + cash-hedge conversion.

    single_book=True hedges only at `bonus_book`. Otherwise each hedge uses the best price
    among `hedge_books` (default: every book in the market data, including bonus_book).
    fixed_pick=(game, label, selection) re-sizes the hedge for a bonus bet already placed.
    max_cash: only hedges whose total cash fits (longer bonus odds convert better but need
    more hedge cash). Raises ValueError if none fits.
    Conversion = (d_bonus - 1) * (1 - sum(1/d_hedge)).
    """
    best = None
    for m in markets:
        if bonus_book not in m.prices or push_risk(m):
            continue
        books = [bonus_book] if single_book else (hedge_books or list(m.prices))
        for i, sel in enumerate(m.outcomes):
            if fixed_pick and (m.game, m.label, sel) != fixed_pick:
                continue
            d_b = m.odds_at(bonus_book)[i]
            hedges = []
            for j in range(len(m.outcomes)):
                if j == i:
                    continue
                opts = [(m.prices[b][j], b) for b in books if b in m.prices]
                if not opts:
                    break
                hedges.append((j, *max(opts)))
            else:
                conv = (d_b - 1) * (1 - sum(1 / d for _, d, _ in hedges))
                if max_cash is not None:
                    cash = sum(bonus * (d_b - 1) / d for _, d, _ in hedges)
                    if cash + 0.01 * len(hedges) > max_cash:  # allow for cent rounding
                        continue
                if best is None or conv > best[0]:
                    best = (conv, m, i, d_b, hedges)
    if best is None:
        if max_cash is not None:
            raise ValueError(f"No hedge for this bonus bet fits within ${max_cash:,.2f} of cash")
        raise ValueError("No hedgeable market found for this bonus bet")
    _, m, i, d_b, hedges = best
    bonus_ticket = Ticket(bonus_book, [Leg(m.game, m.label, m.outcomes[i], d_b)], bonus, True)
    target = bonus * (d_b - 1)
    raw = [target / d for _, d, _ in hedges]
    hedge_tickets = [Ticket(b, [Leg(m.game, m.label, m.outcomes[j], d)], s, False)
                     for (j, d, b), s in zip(hedges, round_to_cents(raw))]
    cash = sum(t.stake for t in hedge_tickets)
    profits = [bonus_ticket.returns - cash] + [t.returns - cash for t in hedge_tickets]
    same_book = all(t.book == bonus_book for t in hedge_tickets)
    slip = Slip(
        title=(f"Bonus bet on {bonus_book}, cash hedge on "
               f"{', '.join(sorted({t.book for t in hedge_tickets}))}"),
        tickets=[bonus_ticket] + hedge_tickets,
        locked_profit=min(profits),
        best_profit=max(profits),
        cash_required=cash,
        bonus_used=bonus,
        conversion=min(profits) / bonus,
        method=("Single-book hedge." if same_book else "Multi-book hedge.")
        + " Place the bonus bet first, then the hedge immediately.",
    )
    slip.warnings = rule_checks([m], parlay=False, stakes=[t.stake for t in slip.tickets],
                                same_book_both_sides=same_book)
    return slip


def compare_bonus_methods(markets: list[Market], bonus_book: str, bonus: float,
                          hedge_books: list[str] | None = None,
                          max_cash: float | None = None) -> list[Slip]:
    """Run every method that applies and return slips, best conversion first.
    max_cash: the cash the user can put down. The grid needs none; hedges that need more
    are left out (max_cash=0 leaves only the grid)."""
    slips = []
    for fn in (
        lambda: best_grid(markets, bonus_book, bonus),
        lambda: bonus_hedge(markets, bonus_book, bonus, single_book=True, max_cash=max_cash),
        lambda: bonus_hedge(markets, bonus_book, bonus, hedge_books=hedge_books, max_cash=max_cash),
    ):
        try:
            slips.append(fn())
        except ValueError:
            pass
    slips.sort(key=lambda s: s.conversion or 0, reverse=True)
    return slips
