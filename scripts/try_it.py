"""Edit the prices below, then run:  python scripts/try_it.py

Odds are American (e.g. -110, 250). Order must match the outcomes list.
"""
from sharpbet import Market, to_decimal as D, boost_plan, compare_bonus_methods

BOOK = "fanduel"

markets = [
    Market("Yankees @ Rays", "Total 7.5", ["Over 7.5", "Under 7.5"],
           {"fanduel": [D(-110), D(-110)]}, kind="total", line=7.5),
    Market("Guardians @ Royals", "Moneyline", ["Guardians", "Royals"],
           {"fanduel": [D(310), D(-390)], "draftkings": [D(290), D(-350)]}),
    # Add more markets here. For a 3-way market:
    # Market("Leeds @ Norwich", "1X2", ["Leeds", "Draw", "Norwich"],
    #        {"fanduel": [D(150), D(240), D(185)]}, kind="3way"),
]

# --- Profit boost: 50% boost, $250 max stake --------------------------------
plan = boost_plan(markets, BOOK, boost_pct=0.50, max_stake=250)
for key in ("single_book_lock", "multi_book_lock"):
    if plan[key]:
        print(plan[key].to_markdown(), "\n")
if plan["no_lock_message"]:
    print(plan["no_lock_message"])

# --- Bonus bet: $100 ---------------------------------------------------------
print("\n$100 bonus bet, every method:")
for slip in compare_bonus_methods(markets, BOOK, 100):
    print(f"- {slip.method.split('.')[0]}: {slip.conversion:.1%} "
          f"(${slip.locked_profit:.2f}), cash needed ${slip.cash_required:.2f}")
