"""Profit boost optimizer.

A profit boost of b (0.5 = 50%) turns decimal odds d into 1 + (d - 1) * (1 + b).

- boost_lock: guaranteed profit by hedging the other outcomes with cash, at the same book
  (single_book=True) or the best price across books. A lock exists only when
  1/d_boosted + sum(1/d_hedge) < 1. max_cash caps the cash across all its tickets.
- boost_lock_options: the profit-vs-cash trade-off. Longer odds gain more from a boost but
  need much more hedge cash, so this returns the locks worth offering (more profit for more
  cash), for the assistant to ask "how much can you put up?".
- boost_ev:   expected value of the boosted bet against sharp no-vig odds, for when
  no lock exists or the user prefers to take the edge unhedged.
"""
from __future__ import annotations

import math

from .checks import round_to_cents, rule_checks, push_risk
from .models import Leg, Market, Slip, Ticket
from .odds import devig, fmt_american


def boosted_decimal(decimal: float, boost_pct: float) -> float:
    return 1 + (decimal - 1) * (1 + boost_pct)


def _eligible(d: float, min_odds: float | None, max_odds: float | None) -> bool:
    return (min_odds is None or d >= min_odds) and (max_odds is None or d <= max_odds)


def _lock_candidates(markets: list[Market], boost_book: str, boost_pct: float, *,
                     hedge_books: list[str] | None, single_book: bool,
                     min_odds: float | None, max_odds: float | None,
                     fixed_pick: tuple[str, str, str] | None) -> list[dict]:
    """Every boost + hedge that locks a profit, with its profit and cash per $1 boosted."""
    out = []
    for m in markets:
        if boost_book not in m.prices or push_risk(m):
            continue
        books = [boost_book] if single_book else (hedge_books or list(m.prices))
        for i, sel in enumerate(m.outcomes):
            if fixed_pick and (m.game, m.label, sel) != fixed_pick:
                continue
            d = m.odds_at(boost_book)[i]
            if not _eligible(d, min_odds, max_odds):
                continue
            d_b = boosted_decimal(d, boost_pct)
            hedges = []
            for j in range(len(m.outcomes)):
                if j == i:
                    continue
                opts = [(m.prices[b][j], b) for b in books if b in m.prices]
                if not opts:
                    break
                hedges.append((j, *max(opts)))
            else:
                inv = sum(1 / x for _, x, _ in hedges)
                per_dollar = d_b * (1 - inv) - 1
                if per_dollar > 0:
                    out.append({"per_dollar": per_dollar, "cash_per_dollar": 1 + d_b * inv,
                                "m": m, "i": i, "d": d, "d_b": d_b, "hedges": hedges})
    return out


def _stake_for(c: dict, max_stake: float, max_cash: float | None) -> float:
    """Boosted stake: the max the boost allows, scaled down to fit max_cash across all tickets."""
    stake = max_stake
    if max_cash is not None:
        stake = min(stake, max_cash / c["cash_per_dollar"])
    return math.floor(stake * 100 + 1e-9) / 100


def _lock_slip(c: dict, boost_book: str, boost_pct: float, stake: float, max_stake: float,
               max_cash: float | None) -> Slip:
    m, i, d, d_b, hedges = c["m"], c["i"], c["d"], c["d_b"], c["hedges"]
    while True:  # hedge cent rounding can overshoot max_cash by a cent or two
        boosted = Ticket(boost_book, [Leg(m.game, m.label, f"{m.outcomes[i]} (boosted from {fmt_american(d)})", d)],
                         stake, False, quoted_decimal=d_b)
        raw = [boosted.returns / x for _, x, _ in hedges]
        hedge_tickets = [Ticket(b, [Leg(m.game, m.label, m.outcomes[j], x)], s, False)
                         for (j, x, b), s in zip(hedges, round_to_cents(raw))]
        cash = boosted.stake + sum(t.stake for t in hedge_tickets)
        if max_cash is None or cash <= max_cash + 1e-9 or stake <= 0.01:
            break
        stake = round(stake - 0.01, 2)
    profits = [t.returns - cash for t in [boosted] + hedge_tickets]
    same_book = all(t.book == boost_book for t in hedge_tickets)
    slip = Slip(
        title=f"{boost_pct:.0%} profit boost on {boost_book}: locked profit",
        tickets=[boosted] + hedge_tickets,
        locked_profit=min(profits),
        best_profit=max(profits),
        cash_required=cash,
        bonus_used=0.0,
        method=("Single-book boost lock." if same_book else "Multi-book boost lock.")
        + " Place the boosted bet first and confirm the boosted odds on the slip, then hedge.",
    )
    if stake < round(max_stake, 2):
        slip.notes.append(f"Boosted stake cut from ${max_stake:,.2f} to ${stake:,.2f} to fit "
                          f"${max_cash:,.2f} of cash.")
    slip.notes.append(f"Return on cash staked: {slip.locked_profit / cash:.1%}.")
    slip.warnings = rule_checks([m], parlay=False, stakes=[t.stake for t in slip.tickets],
                                same_book_both_sides=same_book)
    return slip


def boost_lock_options(markets: list[Market], boost_book: str, boost_pct: float, max_stake: float, *,
                       hedge_books: list[str] | None = None, single_book: bool = False,
                       min_odds: float | None = None, max_odds: float | None = None,
                       fixed_pick: tuple[str, str, str] | None = None,
                       max_cash: float | None = None, top: int = 3) -> list[Slip]:
    """Boost locks worth offering, most profit first: each one needs more cash than the next
    but locks more (options with less profit for more cash are dropped). With max_cash, each
    boosted stake is scaled down so all its tickets fit in that much cash.
    Locked profit scales with the boosted stake, so a longer line can lock more yet need far
    more hedge cash; this list is that trade-off. Empty if no market locks a profit."""
    cands = _lock_candidates(markets, boost_book, boost_pct, hedge_books=hedge_books,
                             single_book=single_book, min_odds=min_odds, max_odds=max_odds,
                             fixed_pick=fixed_pick)
    sized = []
    for c in cands:
        stake = _stake_for(c, max_stake, max_cash)
        if stake >= 0.01:
            sized.append((stake * c["per_dollar"], stake * c["cash_per_dollar"], stake, c))
    sized.sort(key=lambda x: (-x[0], x[1]))
    frontier, seen, cheapest = [], set(), math.inf
    for profit, cash, stake, c in sized:
        key = (c["m"].event_key, c["m"].label)
        if cash > cheapest - 1.0 or key in seen:  # must save at least $1 of cash to be worth offering
            continue
        frontier.append(_lock_slip(c, boost_book, boost_pct, stake, max_stake, max_cash))
        seen.add(key)
        cheapest = cash
        if len(frontier) == top:
            break
    return frontier


def boost_lock(markets: list[Market], boost_book: str, boost_pct: float, max_stake: float, *,
               hedge_books: list[str] | None = None, single_book: bool = False,
               min_odds: float | None = None, max_odds: float | None = None,
               fixed_pick: tuple[str, str, str] | None = None,
               max_cash: float | None = None) -> Slip | None:
    """Best guaranteed-profit use of a profit boost, or None if no market locks a profit.
    min_odds / max_odds are the book's eligibility limits, in decimal, on pre-boost odds.
    max_cash caps the total cash across the boosted bet and its hedges."""
    opts = boost_lock_options(markets, boost_book, boost_pct, max_stake, hedge_books=hedge_books,
                              single_book=single_book, min_odds=min_odds, max_odds=max_odds,
                              fixed_pick=fixed_pick, max_cash=max_cash, top=1)
    return opts[0] if opts else None


def boost_ev(markets: list[Market], boost_book: str, boost_pct: float, max_stake: float, *,
             sharp_book: str = "pinnacle", min_odds: float | None = None,
             max_odds: float | None = None, top: int = 5) -> list[dict]:
    """Rank boosted bets by expected profit vs no-vig fair odds.
    Uses `sharp_book` prices for fair odds when available, else devigs the boost book itself."""
    rows = []
    for m in markets:
        if boost_book not in m.prices:
            continue
        fair_src = sharp_book if sharp_book in m.prices else boost_book
        fair = devig(m.prices[fair_src])
        for i, sel in enumerate(m.outcomes):
            d = m.odds_at(boost_book)[i]
            if not _eligible(d, min_odds, max_odds):
                continue
            d_b = boosted_decimal(d, boost_pct)
            p = fair[i]
            ev = max_stake * (p * (d_b - 1) - (1 - p))
            rows.append({
                "game": m.game, "market": m.label, "selection": sel,
                "odds": fmt_american(d), "boosted_odds": fmt_american(d_b),
                "fair_prob": p, "stake": max_stake, "ev": ev, "ev_pct": ev / max_stake,
                "fair_source": fair_src,
            })
    rows.sort(key=lambda r: r["ev"], reverse=True)
    return rows[:top]


def boost_plan(markets: list[Market], boost_book: str, boost_pct: float, max_stake: float,
               **kw) -> dict:
    """Everything the assistant needs to answer 'I have a boost, what do I do?'.
    Pass max_cash when the user says how much they can put up across both sides."""
    lock_kw = {k: v for k, v in kw.items() if k in ("hedge_books", "min_odds", "max_odds", "max_cash")}
    ev_kw = {k: v for k, v in kw.items() if k in ("sharp_book", "min_odds", "max_odds")}
    single = boost_lock_options(markets, boost_book, boost_pct, max_stake, single_book=True, **lock_kw)
    multi = boost_lock_options(markets, boost_book, boost_pct, max_stake, **lock_kw)
    return {
        "single_book_lock": single[0] if single else None,
        "multi_book_lock": multi[0] if multi else None,
        "single_book_options": single,
        "multi_book_options": multi,
        "best_ev": boost_ev(markets, boost_book, boost_pct, max_stake, **ev_kw),
        "no_lock_message": None if (single or multi) else
        "No market locks a profit with this boost right now; the best +EV uses are listed instead.",
    }


def resize_boost_lock(slip: Slip, boosted_decimal: float, *, max_cash: float | None = None,
                      keep_stake: bool = False) -> Slip:
    """Re-size a boost lock around the boosted odds the book actually shows.

    Books can price a boost slightly differently from 1 + (d - 1)(1 + b): FanDuel showed +445 for a
    50% boost on +295 (we compute +442.5) and +226 on +150 (we compute +225). Same formula for the
    hedge: each hedge stake = boosted returns / hedge odds, so every outcome pays the same.
    keep_stake: the boosted bet is already placed, so only the hedges change. Otherwise the boosted
    stake is cut if needed to keep all tickets within max_cash. May return locked_profit <= 0,
    meaning the odds shown no longer lock a profit."""
    boosted, hedges = slip.tickets[0], slip.tickets[1:]
    inv = sum(1 / t.decimal for t in hedges)
    stake = boosted.stake
    if not keep_stake and max_cash is not None:
        stake = min(stake, math.floor(max_cash / (1 + boosted_decimal * inv) * 100 + 1e-9) / 100)
    while True:  # hedge cent rounding can overshoot max_cash by a cent or two
        new_boosted = Ticket(boosted.book, boosted.legs, stake, False, quoted_decimal=boosted_decimal)
        raw = [new_boosted.returns / t.decimal for t in hedges]
        new_hedges = [Ticket(t.book, t.legs, s, False) for t, s in zip(hedges, round_to_cents(raw))]
        cash = stake + sum(t.stake for t in new_hedges)
        if keep_stake or max_cash is None or cash <= max_cash + 1e-9 or stake <= 0.01:
            break
        stake = round(stake - 0.01, 2)
    profits = [t.returns - cash for t in [new_boosted] + new_hedges]
    old = fmt_american(boosted.decimal)
    new = Slip(
        title=slip.title, tickets=[new_boosted] + new_hedges,
        locked_profit=min(profits), best_profit=max(profits), cash_required=cash, bonus_used=0.0,
        method=slip.method, warnings=list(slip.warnings), quoted_at=slip.quoted_at,
    )
    new.notes.append(f"Re-sized for the boosted odds the book shows: {fmt_american(boosted_decimal)} "
                     f"(the plan assumed {old}).")
    if keep_stake and max_cash is not None and cash > max_cash + 1e-9:
        new.notes.append(f"The boosted bet is already placed, so this needs ${cash:,.2f}, over your "
                         f"${max_cash:,.2f}.")
    if cash:
        new.notes.append(f"Return on cash staked: {new.locked_profit / cash:.1%}.")
    return new
