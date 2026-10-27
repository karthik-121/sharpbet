"""sharpbet: math core for a conversational sharp sports-betting assistant.

Every number the assistant shows should come from these functions, never from the LLM.
"""
from .odds import (american_to_decimal, decimal_to_american, to_decimal, fmt_american,
                   overround, devig, ev_per_dollar, kelly_fraction)
from .models import Market, Leg, Ticket, Slip
from .bonus import (grid_conversion, grid_value, rank_markets, shortlist_markets, solve_grid,
                    optimize_grid, best_grid, resolve_grid, bonus_hedge, compare_bonus_methods)
from .boost import boosted_decimal, boost_lock, boost_lock_options, boost_ev, boost_plan, resize_boost_lock
from .checks import check_bonus_math, round_to_cents, rule_checks
from .select import select_markets, SPORT_ALIASES
from .plan import PromoPlan, promo_plan, plan_markdown

__all__ = [n for n in dir() if not n.startswith("_")]
