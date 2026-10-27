"""Shared data types: markets, tickets and bet slips."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .odds import fmt_american, fmt_american_parlay, overround


@dataclass
class Market:
    """One market in one game, priced at one or more books.

    prices maps book -> decimal odds, in the same order as `outcomes`.
    kind: 'moneyline' | 'spread' | 'total' | '3way' | 'prop'
    event_id tells apart two games with the same name (e.g. an MLB doubleheader).
    sport drives sport-specific warnings (MLB weather postponements).
    updated_at is when the oldest of these prices was last updated by the data source.
    """
    game: str
    label: str
    outcomes: list[str]
    prices: dict[str, list[float]]
    kind: str = "moneyline"
    line: float | None = None
    event_id: str | None = None
    sport: str | None = None  # API sport key, e.g. 'baseball_mlb'
    start: datetime | None = None
    updated_at: datetime | None = None

    @property
    def event_key(self) -> str:
        """Identity of the game, for same-game checks."""
        return self.event_id or self.game

    def odds_at(self, book: str) -> list[float]:
        if book not in self.prices:
            raise KeyError(f"{self.game} {self.label}: no prices at {book}")
        return self.prices[book]

    def hold_at(self, book: str) -> float:
        return overround(self.odds_at(book)) - 1


@dataclass
class Leg:
    game: str
    label: str
    selection: str
    decimal: float
    start: datetime | None = None  # the game's start, for display ("Sun Oct 4, 4:25 PM")


@dataclass
class Ticket:
    book: str
    legs: list[Leg]
    stake: float
    is_bonus: bool
    quoted_decimal: float | None = None  # overrides the leg product when a price moves or is boosted
    tag: str | None = None  # display label, e.g. which boost this ticket uses ("50% boost #1")

    @property
    def decimal(self) -> float:
        if self.quoted_decimal is not None:
            return self.quoted_decimal
        d = 1.0
        for leg in self.legs:
            d *= leg.decimal
        return d

    @property
    def returns(self) -> float:
        """Cash that lands in the account if the ticket wins."""
        if self.is_bonus:
            return self.stake * (self.decimal - 1)  # bonus stake is not returned
        return self.stake * self.decimal

    @property
    def bet_type(self) -> str:
        return "Straight" if len(self.legs) == 1 else f"{len(self.legs)}-leg parlay"


@dataclass
class Slip:
    """An exact list of bets to place, plus the outcome it locks in."""
    title: str
    tickets: list[Ticket]
    locked_profit: float  # worst-case profit across outcomes, after cent rounding
    best_profit: float
    cash_required: float
    bonus_used: float
    conversion: float | None = None  # locked_profit / bonus, for bonus conversions
    method: str = ""
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    quoted_at: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "method": self.method,
            "quoted_at": self.quoted_at.isoformat(timespec="minutes"),
            "tickets": [
                {
                    "n": i + 1,
                    "book": t.book,
                    "funding": "bonus bet" if t.is_bonus else "cash",
                    "bet_type": t.bet_type,
                    "legs": [
                        {"game": l.game, "market": l.label, "selection": l.selection,
                         "odds": fmt_american(l.decimal)} for l in t.legs
                    ],
                    "odds": fmt_american_parlay(t.decimal) if len(t.legs) > 1 else fmt_american(t.decimal),
                    "stake": round(t.stake, 2),
                    "returns_if_wins": round(t.returns, 2),
                }
                for i, t in enumerate(self.tickets)
            ],
            "locked_profit": round(self.locked_profit, 2),
            "best_profit": round(self.best_profit, 2),
            "cash_required": round(self.cash_required, 2),
            "bonus_used": round(self.bonus_used, 2),
            "conversion": None if self.conversion is None else round(self.conversion, 4),
            "warnings": self.warnings,
            "notes": self.notes,
        }

    def to_markdown(self) -> str:
        quoted = self.quoted_at.strftime("%I:%M %p").lstrip("0")
        out = [f"**{self.title}**", f"Prices as of {quoted}. {self.method}", ""]
        multi_leg = any(len(t.legs) > 1 for t in self.tickets)
        if multi_leg:
            games = [l.game for l in self.tickets[0].legs]
            out.append("| # | Book | Funding | " + " | ".join(games) + " | Parlay odds | Stake | Returns |")
            out.append("|---" * (len(games) + 6) + "|")
            for i, t in enumerate(self.tickets, 1):
                sels = " | ".join(f"{l.selection} ({fmt_american(l.decimal)})" for l in t.legs)
                out.append(f"| {i} | {t.book} | {'Bonus' if t.is_bonus else 'Cash'} | {sels} | "
                           f"{fmt_american_parlay(t.decimal)} | ${t.stake:,.2f} | ${t.returns:,.2f} |")
        else:
            out.append("| # | Book | Funding | Game | Market | Selection | Odds | Stake | Returns |")
            out.append("|---|---|---|---|---|---|---|---|---|")
            for i, t in enumerate(self.tickets, 1):
                l = t.legs[0]
                out.append(f"| {i} | {t.book} | {'Bonus' if t.is_bonus else 'Cash'} | {l.game} | {l.label} | "
                           f"{l.selection} | {fmt_american(t.decimal)} | ${t.stake:,.2f} | ${t.returns:,.2f} |")
        out.append("")
        lock = f"**Locked profit: ${self.locked_profit:,.2f}**"
        if self.best_profit - self.locked_profit >= 0.01:
            lock += f" (up to ${self.best_profit:,.2f} depending on outcome, from cent rounding)"
        if self.conversion is not None:
            lock += f" — {self.conversion:.1%} of the ${self.bonus_used:,.2f} bonus"
        if self.cash_required:
            lock += f". Cash needed: ${self.cash_required:,.2f}"
        out.append(lock + ".")
        out.append("Every ticket must be placed at these odds. If any price moves, re-solve before placing the rest.")
        for n in self.notes:
            out.append(f"- {n}")
        for w in self.warnings:
            out.append(f"- Warning: {w}")
        return "\n".join(out)
