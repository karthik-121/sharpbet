"""Which promo offer is worth more: compare options like "two 50% boosts ($300 max each)" vs "one 100%
boost ($250 max)" vs "a $100 bonus bet", on the same live prices, books, games and cash limit.

Each option is planned with the existing planners, unchanged: boosts with multiboost.plan_boosts (so an
option's boosts can hedge each other), bonus bets with plan.promo_plan. An option holding both shares
its cash limit; both orders (bonuses first, boosts first) are tried and the better kept. Options are
ranked by locked profit (less cash wins a tie). The answer depends on the user's cash and books (two
50% boosts can hedge each other with little cash; one 100% boost needs a big cash hedge), so when a
cash limit is set, the options are also run with no limit and a changed winner is reported.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import Market
from .multiboost import Boost, MultiBoostPlan, plan_boosts
from .plan import PromoPlan, book_name, multi_markdown, multi_to_dict, plan_markdown, plan_to_dict, promo_plan


@dataclass
class BonusPromo:
    book: str
    amount: float
    splittable: bool = True


@dataclass
class OptionResult:
    name: str
    promos: list
    boosts: MultiBoostPlan | None
    bonuses: list[PromoPlan] = field(default_factory=list)

    @property
    def promos_desc(self) -> str:
        return describe_promos(self.promos)

    @property
    def locked_profit(self) -> float:
        return round((self.boosts.locked_profit if self.boosts else 0)
                     + sum(p.locked_profit for p in self.bonuses), 2)

    @property
    def cash_required(self) -> float:
        return round((self.boosts.cash_required if self.boosts else 0)
                     + sum(p.cash_required for p in self.bonuses), 2)

    @property
    def return_on_cash(self) -> float | None:
        return self.locked_profit / self.cash_required if self.cash_required else None


@dataclass
class Comparison:
    results: list[OptionResult]          # best first
    max_cash: float | None
    unlimited: list[OptionResult] | None = None   # same options with no cash limit (only when max_cash set)

    @property
    def best(self) -> OptionResult:
        return self.results[0]

    @property
    def margin(self) -> float:
        return round(self.results[0].locked_profit - self.results[1].locked_profit, 2) if len(self.results) > 1 else 0.0


def evaluate_option(markets: list[Market], name: str, promos: list, hedge_books: list[str], *,
                    max_cash: float | None = None, allow_opposing: bool = True) -> OptionResult:
    """Best combined plan for one option's promos (Boost and BonusPromo objects) within max_cash."""
    boosts = [p for p in promos if isinstance(p, Boost)]
    bonuses = [p for p in promos if isinstance(p, BonusPromo)]
    books = list(dict.fromkeys([p.book for p in promos] + list(hedge_books)))

    def run(bonus_first: bool) -> OptionResult:
        left, plans, mp = max_cash, [], None

        def do_bonuses():
            nonlocal left
            for b in bonuses:
                p = promo_plan(markets, b.book, bonus=b.amount, max_cash=left, hedge_books=books,
                               bonus_splittable=b.splittable)
                plans.append(p)
                left = None if left is None else left - p.cash_required

        def do_boosts():
            nonlocal left, mp
            if boosts:
                mp = plan_boosts(markets, boosts, books, max_cash=left, allow_opposing=allow_opposing)
                left = None if left is None else left - mp.cash_required

        for step in ((do_bonuses, do_boosts) if bonus_first else (do_boosts, do_bonuses)):
            step()
        return OptionResult(name, promos, mp, plans)

    tries = [run(True)] if not (boosts and bonuses) else [run(True), run(False)]
    return max(tries, key=lambda r: (r.locked_profit, -r.cash_required))


def compare_options(markets: list[Market], options: list[tuple[str, list]], hedge_books: list[str], *,
                    max_cash: float | None = None, allow_opposing: bool = True) -> Comparison:
    """Rank options (name, promos) by locked profit on the same markets, books and cash limit."""
    def rank(cash):
        rs = [evaluate_option(markets, n, ps, hedge_books, max_cash=cash, allow_opposing=allow_opposing)
              for n, ps in options]
        return sorted(rs, key=lambda r: (-r.locked_profit, r.cash_required))
    return Comparison(rank(max_cash), max_cash, rank(None) if max_cash is not None else None)


# ---------------------------------------------------------------- output

def describe_promos(promos: list) -> str:
    """'2 × 50% FanDuel boost ($300 max each)', '$100 FanDuel bonus bet'."""
    groups: dict[str, int] = {}
    for p in promos:
        if isinstance(p, Boost):
            key = (f"{p.pct * 100:g}% {book_name(p.book)} boost"
                   + (f" ({p.restriction})" if p.restriction else "") + f"|${p.max_stake:,.0f} max")
        else:
            key = f"${p.amount:,.2f} {book_name(p.book)} bonus bet|"
        groups[key] = groups.get(key, 0) + 1
    parts = []
    for key, n in groups.items():
        what, cap = key.split("|")
        cap = f" ({cap}{' each' if n > 1 else ''})" if cap else ""
        parts.append(f"{n} × {what}{cap}" if n > 1 else f"{what}{cap}")
    return " + ".join(parts)


def _money(x: float) -> str:
    return f"${x:,.2f}"


def _roc(r: OptionResult) -> str:
    return "no cash needed" if not r.cash_required else f"{r.return_on_cash:.1%}"


def verdict(cmp: Comparison) -> str:
    rs = cmp.results
    if all(r.locked_profit <= 0 for r in rs):
        return "None of these options locks a profit right now."
    if len(rs) > 1 and cmp.margin < 0.01:
        return f"{rs[0].name} and {rs[1].name} lock the same amount ({_money(rs[0].locked_profit)})."
    tail = f", {_money(cmp.margin)} more than {rs[1].name} ({_money(rs[1].locked_profit)})" if len(rs) > 1 else ""
    return f"Best: {rs[0].name}, locks {_money(rs[0].locked_profit)}{tail}."


def unlimited_note(cmp: Comparison) -> str | None:
    if not cmp.unlimited or cmp.unlimited[0].name == cmp.best.name or cmp.unlimited[0].locked_profit <= 0:
        return None
    u = cmp.unlimited[0]
    return (f"The winner depends on cash: with no cash limit, {u.name} would lock {_money(u.locked_profit)} "
            f"but needs {_money(u.cash_required)} in cash.")


def winner_markdown(r: OptionResult) -> str:
    parts = [plan_markdown(p) for p in r.bonuses if p.slips]
    if r.boosts is not None:
        parts.append(multi_markdown(r.boosts))
    return "\n\n".join(parts)


def compare_markdown(cmp: Comparison) -> str:
    cash = f" with up to {_money(cmp.max_cash)} of cash" if cmp.max_cash is not None else " (no cash limit)"
    out = [f"## Which offer is worth more{cash}", "",
           "| Option | Promos | Locks | Cash to put down | Return on cash |", "|---|---|---|---|---|"]
    for r in cmp.results:
        out.append(f"| {r.name} | {r.promos_desc} | {_money(r.locked_profit)} | {_money(r.cash_required)} | {_roc(r)} |")
    out += ["", f"**{verdict(cmp)}**"]
    note = unlimited_note(cmp)
    if note:
        out.append(note)
    out.append("All options use the same games, prices, books and cash limit. Each lock holds only if every "
               "bet is placed at these odds.")
    if cmp.best.locked_profit > 0:
        out += ["", f"### Plan for {cmp.best.name}", "", winner_markdown(cmp.best)]
    return "\n".join(out)


def _renumber(sections: list[dict]) -> list[dict]:
    n = 1
    for s in sections:
        for t in s.get("tickets", []):
            t["n"] = n
            n += 1
    return sections


def winner_dict(r: OptionResult) -> dict:
    """The winning option's slip as data (same shape as plan_to_dict / multi_to_dict)."""
    ds = [plan_to_dict(p) for p in r.bonuses if p.slips]
    if r.boosts is not None:
        ds.append(multi_to_dict(r.boosts))
    ds = [d for d in ds if not d.get("empty")] or ds[:1]
    if not ds:
        return {"kind": "plan", "empty": True, "notes": []}
    if len(ds) == 1:
        return ds[0]
    merged = dict(ds[0])
    merged["sections"] = _renumber([s for d in ds for s in d.get("sections", [])])
    merged["locked_profit"] = r.locked_profit
    merged["cash_required"] = r.cash_required
    merged["books"] = sorted({b for d in ds for b in d.get("books", [])})
    seen, warnings = set(), []
    for w in (w for d in ds for w in d.get("warnings", [])):
        k = (w.get("about"), w.get("text"))
        if k not in seen:
            seen.add(k)
            warnings.append(w)
    merged["warnings"] = warnings
    for k in ("unused", "alternatives", "notes"):
        merged[k] = [x for d in ds for x in d.get(k, [])]
    return merged


def compare_to_dict(cmp: Comparison) -> dict:
    """Winner's slip plus a 'comparison' block the web UI shows above it. Every row carries its own
    option's slip, so the page can show any option's bets when its row is clicked."""
    d = winner_dict(cmp.best) if cmp.best.locked_profit > 0 else {"kind": "plan", "empty": True, "notes": []}
    d["comparison"] = {
        "max_cash": cmp.max_cash,
        "rows": [{"name": r.name, "promos": r.promos_desc, "locked_profit": r.locked_profit,
                  "cash_required": r.cash_required, "return_on_cash": _roc(r), "best": i == 0 and r.locked_profit > 0,
                  "slip": winner_dict(r) if r.locked_profit > 0 else
                  {"kind": "plan", "empty": True, "notes": [f"{r.name}: nothing locks a profit with these prices."]}}
                 for i, r in enumerate(cmp.results)],
        "verdict": verdict(cmp), "note": unlimited_note(cmp),
    }
    return d
