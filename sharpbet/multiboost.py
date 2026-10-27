"""Several profit boosts at once: which boost goes on which game and side, hedged with other boosts
and/or cash, for the most total locked profit within one shared cash limit.

Per market (solve_market), each outcome can be covered by at most one boost plus cash at the best
of the user's books. Covering outcome j so it pays P costs P / d for each instrument used; filling
the best-priced instrument first (a boosted price, up to its max wager; then cash, unlimited) gives
the cheapest cost for any P. Profit(P) = P - sum of costs, which rises while 1 - sum_j 1/d_j(current)
> 0, so P is raised breakpoint by breakpoint until that stops or the cash limit binds. With one boost
this is the usual boost + cash hedge; with boosts on both sides it's boost-vs-boost (50% Over at +136
vs 50% Under at +136, $200 each, locks $72.73 on $400), topped up with cash when caps differ.

Across markets (plan_boosts), every assignment of boosts to (market, side) is tried, each boost used
at most once and never two on the same bet, and the best combination within the cash limit is kept.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from itertools import permutations
from typing import Callable

from .boost import boosted_decimal
from .checks import push_risk, rule_checks
from .models import Leg, Market, Slip, Ticket
from .odds import fmt_american

OPPOSING_BOOSTS_WARNING = (
    "This uses profit boosts on both sides of the same game. Some sportsbooks' boost terms forbid "
    "that or treat it as promo abuse, and may void the boosts or limit the account. Check your "
    "book's boost terms before placing.")


@dataclass
class Boost:
    book: str
    pct: float                          # 0.5 = 50%
    max_stake: float
    label: str = ""
    min_odds: float | None = None       # decimal, on pre-boost odds
    max_odds: float | None = None
    allow: Callable[[Market], bool] | None = None   # restriction: sport, time slot, teams
    restriction: str = ""               # human-readable, e.g. "SNF only"

    def fits(self, m: Market, j: int) -> bool:
        if self.book not in m.prices or (self.allow and not self.allow(m)):
            return False
        d = m.prices[self.book][j]
        return (self.min_odds is None or d >= self.min_odds) and (self.max_odds is None or d <= self.max_odds)


@dataclass
class MarketPlan:
    market: Market
    assign: dict[int, int]              # outcome index -> boost index
    tickets: list[Ticket]
    locked_profit: float
    best_profit: float
    cash: float

    @property
    def key(self):
        return (self.market.event_key, self.market.label)


def solve_market(m: Market, assign: dict[int, int], boosts: list[Boost], hedge_books: list[str],
                 cash_limit: float | None = None) -> MarketPlan | None:
    """Best lock on one market with these boosts on these outcomes (see module docstring).
    None if nothing locks a profit."""
    if push_risk(m):
        return None
    n = len(m.outcomes)
    inst = []  # per outcome: [(decimal, payout cap, kind, book, boost index, pre-boost decimal)], best first
    for j in range(n):
        opts = []
        if j in assign:
            b = boosts[assign[j]]
            d = m.prices[b.book][j]
            db = boosted_decimal(d, b.pct)
            opts.append((db, b.max_stake * db, "boost", b.book, assign[j], d))
        cash = [(m.prices[bk][j], bk) for bk in hedge_books if bk in m.prices]
        if cash:
            d, bk = max(cash)
            opts.append((d, math.inf, "cash", bk, None, d))
        if not opts:
            return None
        opts.sort(key=lambda o: -o[0])
        inst.append(opts)

    pos, used, P, cost = [0] * n, [0.0] * n, 0.0, 0.0
    while True:
        cur = [inst[j][pos[j]] for j in range(n)]
        if all(c[2] == "cash" for c in cur):
            break  # boosts used up; any remaining edge would be plain arbitrage, not this tool's job
        rate = sum(1 / c[0] for c in cur)  # cash per $1 of payout on every outcome
        if rate >= 1:
            break
        step = min(cur[j][1] - used[j] for j in range(n))
        if cash_limit is not None:
            step = min(step, (cash_limit - cost) / rate)
        if step <= 1e-9:
            break
        P += step
        cost += step * rate
        done = False
        for j in range(n):
            used[j] += step
            if used[j] >= cur[j][1] - 1e-9:
                if pos[j] + 1 < len(inst[j]):
                    pos[j], used[j] = pos[j] + 1, 0.0
                else:
                    done = True
        if done or (cash_limit is not None and cost >= cash_limit - 1e-9):
            break
    if P <= 0:
        return None

    tickets = []
    for j in range(n):
        left = P
        for d, cap, kind, bk, bi, pre in inst[j]:
            if left <= 1e-9:
                break
            take = min(left, cap)
            left -= take
            stake = math.floor(take / d * 100 + 1e-9) / 100  # floor: never over a max wager or the cash limit
            if stake < 0.01:
                continue
            if kind == "boost":
                b = boosts[bi]
                tickets.append(Ticket(bk, [Leg(m.game, m.label, f"{m.outcomes[j]} (boosted from {fmt_american(pre)})",
                                                pre, m.start)], stake, False, quoted_decimal=d, tag=b.label))
            else:
                tickets.append(Ticket(bk, [Leg(m.game, m.label, m.outcomes[j], d, m.start)], stake, False))
    cash_total = round(sum(t.stake for t in tickets), 2)
    payouts = [sum(t.returns for t in tickets if _outcome(t, m) == j) for j in range(n)]
    locked = min(payouts) - cash_total
    if locked < 0.01:
        return None
    return MarketPlan(m, dict(assign), tickets, locked, max(payouts) - cash_total, cash_total)


def _outcome(t: Ticket, m: Market) -> int:
    sel = t.legs[0].selection.split(" (boosted from")[0]
    return m.outcomes.index(sel)


def _assignments(eligible: list[list[int]]):
    """Every way to put distinct boosts on some outcomes (at most one per outcome), non-empty."""
    n = len(eligible)

    def rec(j, taken, cur):
        if j == n:
            if cur:
                yield dict(cur)
            return
        yield from rec(j + 1, taken, cur)
        for bi in eligible[j]:
            if bi not in taken:
                cur[j] = bi
                yield from rec(j + 1, taken | {bi}, cur)
                del cur[j]
    yield from rec(0, frozenset(), {})


@dataclass
class MultiBoostPlan:
    boosts: list[Boost]
    plans: list[MarketPlan]
    max_cash: float | None
    unused: list[tuple[int, str]] = field(default_factory=list)   # (boost index, reason)
    quoted_at: datetime | None = None   # oldest source update among the markets used

    @property
    def locked_profit(self) -> float:
        return sum(p.locked_profit for p in self.plans)

    @property
    def cash_required(self) -> float:
        return round(sum(p.cash for p in self.plans), 2)

    def slips(self) -> list[Slip]:
        out = []
        for p in self.plans:
            used = [self.boosts[bi].label for bi in sorted(p.assign.values())]
            s = Slip(title=f"{p.market.game} · {p.market.label}", tickets=p.tickets,
                     locked_profit=p.locked_profit, best_profit=p.best_profit, cash_required=p.cash,
                     bonus_used=0.0, method="Uses " + " + ".join(used) + ".")
            if self.quoted_at:
                s.quoted_at = self.quoted_at
            same_book = len({t.book for t in p.tickets}) == 1
            s.warnings = rule_checks([p.market], parlay=False, stakes=[t.stake for t in p.tickets],
                                     same_book_both_sides=same_book)
            if len(p.assign) >= 2:
                s.warnings.insert(0, OPPOSING_BOOSTS_WARNING)
            out.append(s)
        return out


def plan_boosts(markets: list[Market], boosts: list[Boost], hedge_books: list[str], *,
                max_cash: float | None = None, keep: int = 4, allow_opposing: bool = True) -> MultiBoostPlan:
    """Most total locked profit from several boosts at once, within max_cash (all tickets).
    allow_opposing=False: never put boosts on both sides of one game (some books forbid it)."""
    cands: dict[frozenset, list[tuple[MarketPlan, dict]]] = {}
    fits_anywhere = set()
    for m in markets:
        if push_risk(m):
            continue
        eligible = [[bi for bi, b in enumerate(boosts) if b.fits(m, j)] for j in range(len(m.outcomes))]
        for bs in eligible:
            fits_anywhere.update(bs)
        if not any(eligible):
            continue
        for assign in _assignments(eligible):
            if not allow_opposing and len(assign) > 1:
                continue
            p = solve_market(m, assign, boosts, hedge_books)
            if p:
                cands.setdefault(frozenset(assign.values()), []).append((p, assign))
    for k in cands:  # best few per boost combination, one per market
        seen, best = set(), []
        for p, a in sorted(cands[k], key=lambda x: -x[0].locked_profit):
            if p.key not in seen:
                seen.add(p.key)
                best.append((p, a))
            if len(best) == keep:
                break
        cands[k] = best

    best = {"profit": 0.0, "cash": 0.0, "plans": []}
    n = len(boosts)

    def dfs(i, decided, markets_used, budget, chosen, total):
        if i == n:
            cash = sum(p.cash for p in chosen)
            if total > best["profit"] + 1e-9 or (abs(total - best["profit"]) < 1e-9 and cash < best["cash"]):
                best.update(profit=total, cash=cash, plans=list(chosen))
            return
        if i in decided:
            dfs(i + 1, decided, markets_used, budget, chosen, total)
            return
        dfs(i + 1, decided | {i}, markets_used, budget, chosen, total)          # leave boost i unused
        for k, lst in cands.items():
            if i not in k or k & decided:
                continue
            for p, a in lst:
                if p.key in markets_used or (not allow_opposing and p.market.event_key in {k[0] for k in markets_used}):
                    continue
                if budget is not None and p.cash > budget + 1e-9:
                    p = solve_market(p.market, a, boosts, hedge_books, cash_limit=budget)
                    if p is None:
                        continue
                dfs(i + 1, decided | k, markets_used | {p.key}, None if budget is None else budget - p.cash,
                    chosen + [p], total + p.locked_profit)

    dfs(0, frozenset(), frozenset(), max_cash, [], 0.0)
    plan = MultiBoostPlan(boosts, sorted(best["plans"], key=lambda p: -p.locked_profit), max_cash)
    used = {bi for p in plan.plans for bi in p.assign.values()}
    for bi, b in enumerate(boosts):
        if bi in used:
            continue
        if bi not in fits_anywhere:
            why = "no eligible games at " + b.book + (f" ({b.restriction})" if b.restriction else "") + \
                  " in this window"
        elif not any(bi in k for k in cands):
            why = "no market locks a profit with it right now"
        else:
            why = "the other boosts lock more with your cash; save it or use it later"
        plan.unused.append((bi, why))
    return plan
