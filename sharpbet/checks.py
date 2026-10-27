"""Rounding, rule checks and 'check my math'."""
from __future__ import annotations

import math

from .models import Market
from .odds import overround

DEFAULT_MIN_STAKE = 0.10
MANY_TICKETS = 20


def round_to_cents(raw: list[float], total: float | None = None) -> list[float]:
    """Round stakes to cents. If `total` is given, the rounded stakes sum to it exactly
    (largest-remainder method), so a $100.00 bonus is used to the cent."""
    if total is None:
        return [round(x, 2) for x in raw]
    target = round(total * 100)
    floors = [math.floor(x * 100 + 1e-9) for x in raw]
    short = target - sum(floors)
    order = sorted(range(len(raw)), key=lambda i: raw[i] * 100 - floors[i], reverse=True)
    for i in order[:max(0, short)]:
        floors[i] += 1
    return [c / 100 for c in floors]


def push_risk(m: Market) -> str | None:
    """Only half-point lines (x.5) are push-proof. Whole numbers can push; quarter lines
    (Asian handicaps like -1.25 or 2.75) split the stake and can half-push. Either breaks a lock."""
    if m.kind in ("spread", "total", "prop") and m.line is not None:
        frac = abs(float(m.line)) % 1
        if abs(frac - 0.5) > 1e-9:
            what = "a whole number" if frac == 0 else "a quarter line (split stake, can half-push)"
            return (f"{m.game} {m.label} is on {what} ({m.line:g}) and can push. "
                    "Use a half-point line or the moneyline.")
    return None


# Why a game in each sport might not be played as scheduled, most likely first.
_POSTPONE_CAUSE = [
    ("baseball", "rain or weather (common)"),
    ("americanfootball", "severe weather (occasional)"),
    ("soccer", "weather or pitch conditions (occasional)"),
    ("rugby", "severe weather (occasional)"),
    ("cricket", "rain (common)"),
]


def postponement_cause(m: Market) -> str:
    for prefix, cause in _POSTPONE_CAUSE:
        if m.sport and m.sport.startswith(prefix):
            return cause
    return "a postponement (rare)"


def _high_risk(m: Market) -> bool:
    return "(common)" in postponement_cause(m)


def _straight_postponement(markets: list[Market]) -> str:
    games = "; ".join(f"{m.game}: {postponement_cause(m)}" for m in markets)
    return (f"Postponement risk ({games}). If a game isn't played as scheduled, books usually void "
            "the bets and refund cash stakes; a bonus bet or boost may come back as a new token or "
            "may be lost, depending on the book's terms. Check the forecast before placing.")


def grid_void_payout(tickets: list, game: str) -> float:
    """Worst-case cash from a bonus grid if the leg in `game` is voided (postponed).
    Books then price each parlay without that leg, so the tickets that differ only in that
    leg all win, at the shorter parlay's odds."""
    payout: dict[tuple, float] = {}
    for t in tickets:
        rest = [l for l in t.legs if l.game != game]
        d = 1.0
        for l in rest:
            d *= l.decimal
        key = tuple(l.selection for l in rest)
        payout[key] = payout.get(key, 0.0) + t.stake * (d - 1)
    return min(payout.values())


def grid_postponement_warning(markets: list[Market], tickets: list, bonus: float) -> str:
    """One warning for a grid: the worst payout if any single leg is voided, and which legs
    are most likely to be postponed."""
    worst_game, worst = min(((m.game, grid_void_payout(tickets, m.game)) for m in markets),
                            key=lambda x: x[1])
    risky = [m.game for m in markets if _high_risk(m)]
    likely = (f" Most likely: {', '.join(risky)} (rain)." if risky
              else " None of these legs is in a sport where that's common.")
    return (f"If any game is postponed, books void that leg and pay each parlay without it, so "
            f"several tickets win at shorter odds. Worst case with one leg voided "
            f"({worst_game}): ${worst:,.2f} ({worst / bonus:.1%}) instead of the lock above; each "
            f"extra voided leg lowers it further.{likely} Check the forecast before placing.")


def rule_checks(markets: list[Market], *, parlay: bool, stakes: list[float],
                min_stake: float = DEFAULT_MIN_STAKE, same_book_both_sides: bool = False) -> list[str]:
    warnings = []
    for m in markets:
        w = push_risk(m)
        if w:
            warnings.append(w)
    if parlay:
        games = [m.event_key for m in markets]
        if len(set(games)) != len(games):
            warnings.append("Two legs are in the same game. Most books block same-game legs "
                            "in a normal parlay, and correlation breaks the math.")
    if not parlay:  # grids get a version with the void-case payout (grid_postponement_warning)
        warnings.append(_straight_postponement(markets))
    small = [s for s in stakes if s < min_stake]
    if small:
        warnings.append(f"{len(small)} ticket(s) are below the ${min_stake:.2f} minimum stake. "
                        "Use a larger bonus or fewer legs.")
    if len(stakes) > MANY_TICKETS:
        warnings.append(f"{len(stakes)} tickets on one promo is very visible to the book and can "
                        "lead to promo restrictions or account limits.")
    if same_book_both_sides:
        warnings.append("This covers every outcome at one sportsbook. Many books' terms prohibit or "
                        "flag betting both sides; they may limit the account or void promo winnings.")
    return warnings


def check_bonus_math(decimals: list[float], bonus: float = 100.0,
                     stakes: list[float] | None = None) -> dict:
    """Check a user's 'cover every outcome' plan for a bonus bet.

    Arbitrage calculators assume the stake is returned, so they report 1 / sum(1/d).
    With a bonus bet the stake is NOT returned, so the true conversion is 1 / sum(1/(d-1))
    when stakes are sized to pay equally. If `stakes` are given, the actual worst case is reported.
    """
    arb_calc_number = 1 / overround(decimals)
    optimal = 1 / sum(1 / (d - 1) for d in decimals)
    result = {
        "arb_calculator_shows": arb_calc_number,
        "true_bonus_conversion_optimal": optimal,
        "explanation": (
            f"An arbitrage calculator shows {arb_calc_number:.1%} because it assumes your stake comes back. "
            f"A bonus bet's stake does not come back, so the best this set of outcomes can convert is "
            f"{optimal:.1%} (stakes sized to 1/(odds - 1))."
        ),
    }
    if stakes is not None:
        scale = bonus / sum(stakes)
        profits = [s * scale * (d - 1) for s, d in zip(stakes, decimals)]
        result["your_stakes_worst_case"] = min(profits) / bonus
        result["your_stakes_best_case"] = max(profits) / bonus
    return result
