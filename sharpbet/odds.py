"""Odds conversion, devigging, expected value and Kelly sizing.

All internal math uses decimal odds (-110 -> 1.9091, +300 -> 4.0).
"""
from __future__ import annotations

import math
from typing import Iterable


def american_to_decimal(american: float) -> float:
    a = float(american)
    if -100 < a < 100:
        raise ValueError(f"Invalid American odds: {american}")
    return 1 + a / 100 if a > 0 else 1 + 100 / -a


def decimal_to_american(decimal: float) -> float:
    d = float(decimal)
    if d <= 1:
        raise ValueError(f"Invalid decimal odds: {decimal}")
    return (d - 1) * 100 if d >= 2 else -100 / (d - 1)


def fmt_american(decimal: float) -> str:
    a = round(decimal_to_american(decimal))
    return f"+{a}" if a > 0 else str(a)


def fmt_american_parlay(decimal: float) -> str:
    """Parlay odds as a book displays them: rounded DOWN to a whole number (never overstating
    the payout). FanDuel shows an exact +1158.84 as +1158; straight-bet odds are already whole."""
    a = decimal_to_american(decimal)
    a = math.floor(a + 1e-9)
    return f"+{a}" if a > 0 else str(a)


def to_decimal(odds) -> float:
    """Accept American odds (-110, '+300') or decimal written as 'd4.0'."""
    if isinstance(odds, str):
        s = odds.strip()
        if s.lower().startswith("d"):
            return float(s[1:])
        return american_to_decimal(float(s))
    return american_to_decimal(odds)


def overround(decimals: Iterable[float]) -> float:
    """Sum of implied probabilities (1.0476 on a -110/-110 market = 4.76% hold)."""
    return sum(1 / d for d in decimals)


def devig(decimals: list[float]) -> list[float]:
    """Multiplicative devig: fair probabilities that sum to 1."""
    s = overround(decimals)
    return [(1 / d) / s for d in decimals]


def ev_per_dollar(fair_prob: float, decimal: float) -> float:
    """Expected profit per $1 of cash staked at these odds."""
    return fair_prob * (decimal - 1) - (1 - fair_prob)


def kelly_fraction(fair_prob: float, decimal: float) -> float:
    """Full-Kelly fraction of bankroll; 0 when there is no edge."""
    b = decimal - 1
    return max(0.0, (b * fair_prob - (1 - fair_prob)) / b)
