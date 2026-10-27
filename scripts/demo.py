"""Run: python scripts/demo.py  — prints example bet slips from made-up prices."""
from sharpbet import (Market, to_decimal as D, best_grid, bonus_hedge, compare_bonus_methods,
                      boost_plan, resolve_grid, check_bonus_math)


def two_way(game, label, a, b, sel=("Home", "Away"), kind="moneyline", line=None, **books):
    prices = {bk: [D(x), D(y)] for bk, (x, y) in books.items()}
    return Market(game, label, list(sel), prices, kind, line)


markets = [
    two_way("Yankees @ Rays", "Total 8.5", 0, 0, ("Over 8.5", "Under 8.5"), "total", 8.5,
            fanduel=(-110, -110), draftkings=(-105, -115), pinnacle=(-104, -106)),
    two_way("Jets @ Bills", "Spread", 0, 0, ("Jets +3.5", "Bills -3.5"), "spread", 3.5,
            fanduel=(-110, -110), draftkings=(-112, -108), pinnacle=(-105, -105)),
    two_way("Heat @ Celtics", "Spread", 0, 0, ("Heat +2.5", "Celtics -2.5"), "spread", 2.5,
            fanduel=(-110, -110), pinnacle=(-103, -107)),
    two_way("Chiefs @ Raiders", "Total 45.5", 0, 0, ("Over 45.5", "Under 45.5"), "total", 45.5,
            fanduel=(-110, -110), draftkings=(-110, -110)),
    two_way("Dodgers @ Padres", "Moneyline", 0, 0, ("Dodgers", "Padres"),
            fanduel=(-150, 126), draftkings=(-142, 130), pinnacle=(-140, 128)),
    two_way("Guardians @ Royals", "Moneyline", 0, 0, ("Guardians", "Royals"),
            fanduel=(310, -390), draftkings=(290, -350), pinnacle=(300, -360)),
    Market("Atlanta @ Orlando (MLS)", "1X2", ["Atlanta", "Draw", "Orlando"],
           {"fanduel": [D(160), D(250), D(170)]}, kind="3way"),
    Market("Leeds @ Norwich", "1X2", ["Leeds", "Draw", "Norwich"],
           {"fanduel": [D(150), D(240), D(185)]}, kind="3way"),
]

print("=" * 80, "\n$100 FanDuel bonus bet: every method, best first\n")
for s in compare_bonus_methods(markets, "fanduel", 100):
    print(f"- {s.method.split('.')[0]}: {s.conversion:.1%} (${s.locked_profit:.2f}), cash needed ${s.cash_required:.2f}")

print("\n" + "=" * 80, "\nSingle-book grid slip\n")
grid = best_grid(markets, "fanduel", 100)
print(grid.to_markdown())

print("\n" + "=" * 80, "\nRe-solve: placed 1-6, ticket 7 moved\n")
placed = {n: grid.tickets[n - 1].decimal for n in range(1, 7)}
print(resolve_grid(grid, placed, moved={7: grid.tickets[6].decimal * 0.97}).to_markdown())

print("\n" + "=" * 80, "\nMulti-book hedge slip\n")
print(bonus_hedge(markets, "fanduel", 100).to_markdown())

print("\n" + "=" * 80, "\n50% FanDuel profit boost, $25 max\n")
plan = boost_plan(markets, "fanduel", 0.5, 25)
for key in ("single_book_lock", "multi_book_lock"):
    if plan[key]:
        print(plan[key].to_markdown(), "\n")
print("Best +EV if you skip the hedge:")
for r in plan["best_ev"][:3]:
    print(f"  {r['selection']} {r['odds']} -> {r['boosted_odds']}: EV ${r['ev']:.2f} ({r['ev_pct']:.0%}) vs {r['fair_source']}")

print("\n" + "=" * 80, "\nCheck my math: the 9-way soccer grid\n")
print(check_bonus_math([D(o) for o in [500, 524, 524, 500, 920, 880, 806, 1380, 806]])["explanation"])
