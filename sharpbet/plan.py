"""One recommended plan across all of a user's promos, and the 'What to place' summary.

The user's cash limit is shared: a bonus hedge and a boost lock can't each use the full amount.
promo_plan tries every bonus method that fits, gives the boost what's left, and keeps the
combination that locks the most profit (least cash on a tie).
"""
from __future__ import annotations

import re

from dataclasses import dataclass, field

from .boost import boost_lock_options, boost_ev
from .bonus import compare_bonus_methods
from .models import Market, Slip
from .odds import fmt_american, fmt_american_parlay


BOOK_NAMES = {"fanduel": "FanDuel", "draftkings": "DraftKings", "betmgm": "BetMGM", "bovada": "Bovada",
              "betonlineag": "BetOnline",
              "williamhill_us": "Caesars", "espnbet": "ESPN BET", "fanatics": "Fanatics",
              "betrivers": "BetRivers", "hardrockbet": "Hard Rock Bet", "pinnacle": "Pinnacle"}


def book_name(key: str) -> str:
    return BOOK_NAMES.get(key, key)


@dataclass
class PromoPlan:
    book: str
    bonus: Slip | None
    boost: Slip | None
    max_cash: float | None
    alternatives: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def slips(self) -> list[Slip]:
        return [s for s in (self.bonus, self.boost) if s]

    @property
    def locked_profit(self) -> float:
        return sum(s.locked_profit for s in self.slips)

    @property
    def cash_required(self) -> float:
        return sum(s.cash_required for s in self.slips)

    def to_markdown(self) -> str:
        return plan_markdown(self)


def promo_plan(markets: list[Market], book: str, *, bonus: float | None = None,
               boost_pct: float | None = None, boost_max_stake: float | None = None,
               max_cash: float | None = None, hedge_books: list[str] | None = None,
               boost_min_odds: float | None = None, boost_max_odds: float | None = None,
               sharp_book: str = "pinnacle", bonus_splittable: bool = True) -> PromoPlan:
    """Best combined plan for a bonus bet and/or a profit boost at `book`.
    hedge_books: books the user can bet at (default: only `book`). Never include a
    reference-only book such as Pinnacle. max_cash is shared across everything.
    bonus_splittable=False: the book only allows the bonus as one bet, so no grid."""
    hedge_books = hedge_books or [book]
    bonus_opts: list[Slip | None] = [None]
    if bonus:
        found = compare_bonus_methods(markets, book, bonus, hedge_books=hedge_books, max_cash=max_cash)
        if not bonus_splittable:
            found = [s for s in found if sum(t.is_bonus for t in s.tickets) == 1]
        bonus_opts = found or [None]
    boost_kw = dict(hedge_books=hedge_books, min_odds=boost_min_odds, max_odds=boost_max_odds, top=1)

    best = None
    for b in bonus_opts:
        left = None if max_cash is None else max_cash - (b.cash_required if b else 0)
        boost = None
        if boost_pct:
            opts = boost_lock_options(markets, book, boost_pct, boost_max_stake, max_cash=left, **boost_kw)
            boost = opts[0] if opts else None
        total = (b.locked_profit if b else 0) + (boost.locked_profit if boost else 0)
        cash = (b.cash_required if b else 0) + (boost.cash_required if boost else 0)
        if best is None or (total, -cash) > (best[0], -best[1]):
            best = (total, cash, b, boost, left)
    _, _, b, boost, left = best
    plan = PromoPlan(book, b, boost, max_cash)

    if boost_pct:
        single = boost_lock_options(markets, book, boost_pct, boost_max_stake, max_cash=left,
                                    **{**boost_kw, "hedge_books": [book]})
        if boost and any(t.book != book for t in boost.tickets) and single:
            s = single[0]
            plan.alternatives.append(
                f"Boost using only {book_name(book)}: ${s.locked_profit:,.2f} locked for ${s.cash_required:,.2f} "
                f"({_leg(s.tickets[0].legs[0])}).")
        if boost is None:
            ev = boost_ev(markets, book, boost_pct, boost_max_stake, sharp_book=sharp_book,
                          min_odds=boost_min_odds, max_odds=boost_max_odds, top=1)
            plan.notes.append("No market locks a profit with this boost" +
                              (f" within ${left:,.2f} of cash." if left is not None else "."))
            if ev:
                r = ev[0]
                plan.notes.append(
                    f"Unhedged option: {r['selection']} ({r['game']}) {r['odds']} → {r['boosted_odds']}, "
                    f"expected profit ${r['ev']:,.2f}, but it loses the ${boost_max_stake:,.2f} stake "
                    f"{1 - r['fair_prob']:.0%} of the time.")
    if bonus and b is not None:
        others = [s for s in bonus_opts if s is not None and s is not b]
        for s in others[:2]:
            plan.alternatives.append(
                f"Bonus via {s.method.split('.')[0].lower()}: ${s.locked_profit:,.2f} "
                f"({s.conversion:.1%}), ${s.cash_required:,.2f} cash.")
    if bonus and b is None:
        plan.notes.append("No bonus conversion found with these markets.")
    return plan


def _leg(l, with_game: bool = False) -> str:
    """How the bet reads in the app. Team-name selections identify their game; totals, props
    and Draw don't, so those (and every straight bet) name the game too."""
    sel = l.selection.split(" (boosted from")[0]
    if l.label.startswith("Moneyline"):
        sel += " ML"
    elif l.label.startswith("3-Way"):
        sel += " (3-way ML)"
    if with_game or sel.startswith(("Over", "Under", "Draw")) or "Total" in l.label or " Over " in sel or " Under " in sel:
        sel += f" [{l.game}{_when(l)}]"
    elif l.start:
        sel += f" [{_when(l).lstrip(', ')}]"
    return f"{sel} {fmt_american(l.decimal)}"


def _when(l) -> str:
    if not l.start:
        return ""
    t = l.start.astimezone()
    return f", {t.strftime('%a %b')} {t.day}, {t.strftime('%I:%M %p').lstrip('0')}"


def _ticket_line(i: int, t) -> str:
    funding = "bonus bet" if t.is_bonus else "cash"
    legs = " + ".join(_leg(l, with_game=len(t.legs) == 1) for l in t.legs)
    kind = "Straight" if len(t.legs) == 1 else f"{len(t.legs)}-leg parlay"
    if t.quoted_decimal is not None and len(t.legs) == 1:  # boosted
        which = f"apply your {t.tag}" if t.tag else "apply the boost"
        return (f"{i}. **{book_name(t.book)}**, ${t.stake:,.2f} {funding}, {kind}, **{which}**: {legs} "
                f"→ boosted {fmt_american(t.decimal)}")
    odds = "" if len(t.legs) == 1 else f" at {fmt_american_parlay(t.decimal)}"
    return f"{i}. **{book_name(t.book)}**, ${t.stake:,.2f} {funding}, {kind}{odds}: {legs}"


def plan_markdown(plan: PromoPlan) -> str:
    """The 'What to place' summary: every bet, in placement order, and the totals."""
    if not plan.slips:
        return "## What to place\nNothing locks a profit right now.\n" + "\n".join(f"- {n}" for n in plan.notes)
    books = sorted({book_name(t.book) for s in plan.slips for t in s.tickets})
    quoted = min(s.quoted_at for s in plan.slips).strftime("%I:%M %p").lstrip("0")
    starts = sorted(l.start.astimezone() for s in plan.slips for t in s.tickets for l in t.legs if l.start)
    games_when = ""
    if starts:
        first, last = starts[0], starts[-1]
        fmt = lambda d: f"{d.strftime('%a %b')} {d.day}"
        games_when = (f" Games: {fmt(first)}." if fmt(first) == fmt(last)
                      else f" Games: {fmt(first)} to {fmt(last)}; settles after the last one.")
    if plan.cash_required == 0:
        cash = "No cash needed"
    else:
        cash = f"Cash to put down: ${plan.cash_required:,.2f}" + (
            f" (your limit: ${plan.max_cash:,.2f})" if plan.max_cash else "")
    out = ["## What to place", "",
           f"**Locks ${plan.locked_profit:,.2f} if every bet below is placed at these odds.** "
           f"{cash}. Books: {', '.join(books)}. Prices as of {quoted}.{games_when}", ""]
    n = 1
    if plan.bonus:
        s = plan.bonus
        head = (f"### {n}. Bonus bet: ${s.bonus_used:,.2f} → ${s.locked_profit:,.2f} ({s.conversion:.1%})")
        how = ("Grid: place all of these; exactly one wins." if s.cash_required == 0
               else "Place the bonus bet first, then the cash hedge right away.")
        out += [head, how, ""] + [_ticket_line(i, t) for i, t in enumerate(s.tickets, 1)] + [""]
        n += 1
    if plan.boost:
        s = plan.boost
        t0 = s.tickets[0]
        out += [f"### {n}. Profit boost → ${s.locked_profit:,.2f} locked",
                f"Apply the boost to bet 1 and check the app shows {fmt_american(t0.decimal)}. If it shows a "
                "slightly different number, say what it shows and the hedge will be re-sized; then place bet 1 "
                "and the hedge.", ""]
        out += [_ticket_line(i, t) for i, t in enumerate(s.tickets, 1)] + [""]
    out += ["### Before you place",
            "- Place every bet in one sitting, right away: odds change quickly, and the lock only holds "
            "at these prices.",
            "- If any price on the book differs from the one listed, stop and re-run before placing the rest.",
            "- Don't place bets on games that have started."]
    seen = set()
    for name, s in (("Bonus bet", plan.bonus), ("Boost", plan.boost)):
        for w in (s.warnings if s else []):
            if w not in seen:
                seen.add(w)
                out.append(f"- {name}: {w}")
    for x in plan.notes:
        out.append(f"- {x}")
    if plan.alternatives:
        out += ["", "**Other options:**"] + [f"- {a}" for a in plan.alternatives]
    return "\n".join(out)


# ---------------------------------------------------------------- structured (for the web UI)

def leg_dict(l) -> dict:
    return {"selection": l.selection.split(" (boosted from")[0], "market": l.label, "game": l.game,
            "start": l.start.isoformat() if l.start else None,
            "when": _when(l).lstrip(", ") or None, "odds": fmt_american(l.decimal)}


def ticket_dict(n: int, t) -> dict:
    boosted = t.quoted_decimal is not None and len(t.legs) == 1
    return {
        "n": n, "book": book_name(t.book), "funding": "Bonus bet" if t.is_bonus else "Cash",
        "bet_type": "Straight" if len(t.legs) == 1 else f"{len(t.legs)}-leg parlay",
        "odds": fmt_american_parlay(t.decimal) if len(t.legs) > 1 else fmt_american(t.decimal),
        "boost": {"from": fmt_american(t.legs[0].decimal), "to": fmt_american(t.decimal),
                  "label": t.tag} if boosted else None,
        "stake": round(t.stake, 2), "returns": round(t.returns, 2),
        "legs": [leg_dict(l) for l in t.legs],
        "copy": _ticket_line(n, t).replace("**", ""),
    }


def plan_to_dict(plan: PromoPlan) -> dict:
    """The same plan as plan_markdown, as data: the web UI renders tables from this, so the page
    and the text slip always show the same numbers."""
    if not plan.slips:
        return {"kind": "plan", "empty": True, "notes": plan.notes}
    starts = sorted(l.start.astimezone() for s in plan.slips for t in s.tickets for l in t.legs if l.start)
    day = lambda d: f"{d.strftime('%a %b')} {d.day}"
    sections = []
    if plan.bonus:
        s = plan.bonus
        grid = s.cash_required == 0
        sections.append({
            "type": "bonus", "title": f"Bonus bet: ${s.bonus_used:,.2f} → ${s.locked_profit:,.2f} ({s.conversion:.1%})",
            "instruction": "Grid: place all of these; exactly one wins." if grid
            else "Place the bonus bet first, then the cash hedge right away.",
            "locked_profit": round(s.locked_profit, 2), "conversion": round(s.conversion, 4),
            "tickets": [ticket_dict(i, t) for i, t in enumerate(s.tickets, 1)]})
    if plan.boost:
        s = plan.boost
        sections.append({
            "type": "boost", "title": f"Profit boost → ${s.locked_profit:,.2f} locked",
            "instruction": f"Apply the boost to bet 1 and check the app shows "
                           f"{fmt_american(s.tickets[0].decimal)}. If it shows a slightly different number, "
                           "tell me what it shows and I'll re-size the hedge.",
            "locked_profit": round(s.locked_profit, 2),
            "tickets": [ticket_dict(i, t) for i, t in enumerate(s.tickets, 1)]})
    warnings = []
    for name, s in (("Bonus bet", plan.bonus), ("Boost", plan.boost)):
        for w in (s.warnings if s else []):
            if not any(x["text"] == w for x in warnings):
                warnings.append({"about": name, "text": w})
    return {
        "kind": "plan", "empty": False,
        "locked_profit": round(plan.locked_profit, 2), "cash_required": round(plan.cash_required, 2),
        "max_cash": plan.max_cash,
        "books": sorted({book_name(t.book) for s in plan.slips for t in s.tickets}),
        "quoted_at": min(s.quoted_at for s in plan.slips).strftime("%I:%M %p").lstrip("0"),
        "games_from": day(starts[0]) if starts else None, "games_to": day(starts[-1]) if starts else None,
        "sections": sections, "warnings": warnings,
        "alternatives": plan.alternatives, "notes": plan.notes,
    }


# ---------------------------------------------------------------- several boosts at once

_POSTPONE = re.compile(r"^Postponement risk \((.+): ([^:]+)\)\. (.*)$", re.S)


def merge_warnings(pairs: list[tuple[str, str]]) -> list[dict]:
    """Collapse per-game warnings into a short list: a warning on every game once as "All games",
    postponement warnings with the same cause into one line naming the games, others labeled with
    the games they apply to. The both-sides boost warning comes first."""
    games = list(dict.fromkeys(g for g, _ in pairs))
    by_text: dict[str, list[str]] = {}
    postpone: dict[str, tuple[list[str], str]] = {}
    for g, w in pairs:
        m = _POSTPONE.match(w)
        if m:
            cause, rest = m.group(2), m.group(3)
            gs, _ = postpone.setdefault(cause, ([], rest))
            if g not in gs:
                gs.append(g)
        else:
            by_text.setdefault(w, [])
            if g not in by_text[w]:
                by_text[w].append(g)
    label = lambda gs: "All games" if len(games) > 1 and len(gs) == len(games) else ", ".join(gs)
    out = [{"about": label(gs), "text": w} for w, gs in by_text.items()]
    out.sort(key=lambda x: 0 if "both sides of the same game" in x["text"] else 1)
    for cause, (gs, rest) in postpone.items():
        out.append({"about": "Postponement risk", "text": f"{', '.join(gs)}: {cause}. {rest}"})
    return out


def _boost_desc(b) -> str:
    return (f"{b.label}: {b.pct:.0%} at {book_name(b.book)}, max ${b.max_stake:,.2f}"
            + (f", {b.restriction}" if b.restriction else ""))


def multi_markdown(mp) -> str:
    """'What to place' for several boosts: one section per game, then boosts not used and why."""
    slips = mp.slips()
    if not slips:
        lines = ["## What to place", "Nothing locks a profit with these boosts right now.", ""]
        lines += [f"- {_boost_desc(mp.boosts[bi])}: {why}." for bi, why in mp.unused]
        return "\n".join(lines)
    books = sorted({book_name(t.book) for s in slips for t in s.tickets})
    quoted = min(s.quoted_at for s in slips).strftime("%I:%M %p").lstrip("0")
    starts = sorted(p.market.start.astimezone() for p in mp.plans if p.market.start)
    day = lambda d: f"{d.strftime('%a %b')} {d.day}"
    games = (f" Games: {day(starts[0])}." if day(starts[0]) == day(starts[-1])
             else f" Games: {day(starts[0])} to {day(starts[-1])}.") if starts else ""
    cash = f"Cash to put down: ${mp.cash_required:,.2f}" + (f" (your limit: ${mp.max_cash:,.2f})" if mp.max_cash else "")
    used = len(mp.boosts) - len(mp.unused)
    out = ["## What to place", "",
           f"**Locks ${mp.locked_profit:,.2f} in total if every bet below is placed at these odds.** {cash}. "
           f"Books: {', '.join(books)}. Prices as of {quoted}.{games} Uses {used} of your {len(mp.boosts)} boosts.", ""]
    n = 1
    for i, (p, s) in enumerate(zip(mp.plans, slips), 1):
        out += [f"### {i}. {p.market.game} · {p.market.label} → ${p.locked_profit:,.2f} locked · "
                f"${p.cash:,.2f} cash · {p.locked_profit / p.cash:.1%} return",
                f"{s.method} Apply each boost to its bet and check the app shows the boosted odds listed; if it "
                "shows a slightly different number, say what it shows. Place the boosted bets first, then any "
                "cash hedge.", ""]
        for t in s.tickets:
            out.append(_ticket_line(n, t))
            n += 1
        out.append("")
    if mp.unused:
        out += ["### Boosts not used"] + [f"- {_boost_desc(mp.boosts[bi])}: {why}." for bi, why in mp.unused] + [""]
    out += ["### Before you place",
            "- Place every bet in one sitting, right away: odds change quickly, and the locks only hold at these prices.",
            "- If any price on the book differs from the one listed, stop and re-run before placing the rest.",
            "- Don't place bets on games that have started."]
    for w in merge_warnings([(p.market.game, w) for p, s in zip(mp.plans, slips) for w in s.warnings]):
        out.append(f"- {w['about']}: {w['text']}")
    return "\n".join(out)


def multi_to_dict(mp) -> dict:
    """Same content as multi_markdown, as data for the web UI (same shape as plan_to_dict)."""
    slips = mp.slips()
    unused = [{"boost": _boost_desc(mp.boosts[bi]), "why": why} for bi, why in mp.unused]
    if not slips:
        return {"kind": "plan", "empty": True, "notes": [f"{u['boost']}: {u['why']}." for u in unused]}
    starts = sorted(p.market.start.astimezone() for p in mp.plans if p.market.start)
    day = lambda d: f"{d.strftime('%a %b')} {d.day}"
    sections, n = [], 1
    for p, s in zip(mp.plans, slips):
        tickets = []
        for t in s.tickets:
            tickets.append(ticket_dict(n, t))
            n += 1
        sections.append({"type": "boost", "title": f"{p.market.game} · {p.market.label}",
                         "cash": round(p.cash, 2), "return_on_cash": round(p.locked_profit / p.cash, 4),
                         "instruction": s.method + " Apply each boost to its bet and check the app shows the boosted "
                                        "odds listed; if it shows a slightly different number, tell me.",
                         "locked_profit": round(p.locked_profit, 2), "tickets": tickets})
    warnings = merge_warnings([(p.market.game, w) for p, s in zip(mp.plans, slips) for w in s.warnings])
    return {
        "kind": "plan", "empty": False,
        "locked_profit": round(mp.locked_profit, 2), "cash_required": mp.cash_required, "max_cash": mp.max_cash,
        "books": sorted({book_name(t.book) for s in slips for t in s.tickets}),
        "quoted_at": min(s.quoted_at for s in slips).strftime("%I:%M %p").lstrip("0"),
        "games_from": day(starts[0]) if starts else None, "games_to": day(starts[-1]) if starts else None,
        "sections": sections, "warnings": warnings, "unused": unused,
        "alternatives": [], "notes": [f"Uses {len(mp.boosts) - len(mp.unused)} of your {len(mp.boosts)} boosts."],
    }
