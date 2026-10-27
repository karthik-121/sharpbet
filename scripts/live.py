"""Pull live odds and print real slips for ONE promo per run: a bonus bet or a profit boost.
(A boost can't be applied to a bonus bet; they are separate promos.)

    python scripts/live.py --book fanduel --bonus 200                        # bonus bet at FanDuel
    python scripts/live.py --book draftkings --boost 0.5 --max-stake 250     # 50% boost at DraftKings, $250 max
    python scripts/live.py --book fanduel --boost 0.5 --max-stake 200        # 50% boost at FanDuel, $200 max
    python scripts/live.py --sports epl,mls --bonus 200     # soccer 1X2 for 3-way grids
    python scripts/live.py --sports nhl --extra h2h_3_way --max-events 8
    python scripts/live.py --avoid mlb --within-days 2      # no baseball legs (rain risk), settle by Monday
    python scripts/live.py --dry-run                        # show the credit cost, pull nothing
    python scripts/live.py --save tests/fixtures/raw        # also keep the raw API responses

Needs ODDS_API_KEY in .env (see .env.example). Every number printed comes from sharpbet.
"""
from __future__ import annotations

import argparse
import sys
from datetime import timedelta

from sharpbet import (boost_plan, compare_bonus_methods, promo_plan, rank_markets, select_markets,
                      to_decimal)
from sharpbet.data import (ALL_BOOKS_WORDS, FEATURED, US_BOOKS, OddsAPI, OddsAPIError, fetch_markets,
                           plan_cost, sport_key, stamp_slip)
from sharpbet.plan import book_name
from sharpbet.select import _matches

DISCLAIMER = (
    "No result is guaranteed. A locked profit holds only if every ticket is placed at exactly "
    "these prices and nothing is voided. Many sportsbooks restrict betting both sides or hedging "
    "promos, and may limit accounts. 21+ only. Gambling problem? Call 1-800-GAMBLER."
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sports", default="nfl,ncaaf,mlb,epl", help="comma list: nfl,mlb,nhl,epl,mls,... or API keys")
    ap.add_argument("--markets", default="h2h,spreads,totals", help="featured market keys")
    ap.add_argument("--extra", default="", help="per-game market keys, e.g. h2h_3_way,alternate_totals")
    ap.add_argument("--max-events", type=int, default=5, help="games per sport for --extra markets")
    ap.add_argument("--book", required=True, help="book the promo is on: fanduel, draftkings, ...")
    ap.add_argument("--books", default="fanduel,draftkings",
                    help="books you can bet at (hedges), comma list, or 'all' for every major US book")
    ap.add_argument("--sharp", default="pinnacle", help="reference book for fair odds, or 'none'")
    promo = ap.add_mutually_exclusive_group(required=True)
    promo.add_argument("--bonus", type=float, help="bonus bet amount to convert, e.g. 200")
    promo.add_argument("--boost", type=float, help="profit boost on a cash bet, 0.5 = 50%%")
    ap.add_argument("--max-stake", type=float,
                    help="the boost's max wager (required with --boost; it varies by book and promo)")
    ap.add_argument("--min-odds", default="-200",
                    help="boost minimum odds before boosting (American); default -200, 'none' for no limit")
    ap.add_argument("--max-odds", type=float, help="boost maximum odds (American)")
    ap.add_argument("--max-cash", type=float, help="most cash you can put down (bonus hedges, or the boosted bet plus its hedge)")
    ap.add_argument("--top", type=int, default=8, help="best grid legs to list")
    ap.add_argument("--max-age", type=int, help="drop prices older than this many minutes")
    ap.add_argument("--within-days", type=float, help="only games starting within this many days "
                    "(a bonus is tied up until its last leg settles)")
    ap.add_argument("--avoid", default="", help="sports never to use, e.g. mlb or baseball,soccer")
    ap.add_argument("--min-lead", type=float, default=30,
                    help="skip games starting within this many minutes (default 30)")
    ap.add_argument("--budget", type=int, default=60, help="refuse pulls estimated above this many credits")
    ap.add_argument("--save", help="directory to save raw API responses (fixtures)")
    ap.add_argument("--dry-run", action="store_true", help="print the credit estimate only")
    a = ap.parse_args()
    if a.boost and a.max_stake is None:
        ap.error("--boost needs --max-stake: the most this boost lets you wager at "
                 f"{a.book} (e.g. --max-stake 200)")
    a.book = a.book.strip().lower()

    avoid = [s.strip() for s in a.avoid.split(",") if s.strip()]
    sports = [s.strip() for s in a.sports.split(",") if s.strip()]
    sports = [s for s in sports if not _matches(sport_key(s), avoid)]  # don't pay to pull them
    if not sports:
        print("Every sport in --sports is in --avoid; nothing to pull.")
        return 1
    markets = [m for m in (a.markets + "," + a.extra).split(",") if m.strip()]
    books = [b.strip() for b in a.books.split(",") if b.strip()]
    if any(b.lower() in ALL_BOOKS_WORDS for b in books):
        books = list(US_BOOKS)
    if a.book not in books:
        books.insert(0, a.book)
    pull_books = books + ([a.sharp] if a.sharp != "none" and a.sharp not in books else [])
    n_extra = a.max_events if any(m not in FEATURED for m in markets) else 0
    estimate = len(sports) * plan_cost(markets, pull_books, n_extra)
    print(f"Pulling {', '.join(markets)} for {', '.join(sports)} at {', '.join(pull_books)}: "
          f"up to {estimate} credits.")
    if a.dry_run:
        return 0

    try:
        api = OddsAPI(save_dir=a.save)
        all_markets, notes = [], []
        for sport in sports:
            res = fetch_markets(api, sport, markets, pull_books, max_events=a.max_events,
                                budget=a.budget,
                                max_age=timedelta(minutes=a.max_age) if a.max_age else None)
            all_markets += res.markets
            notes += res.notes
            n_games = len({m.event_key for m in res.markets})
            print(f"  {sport}: {n_games} games, {len(res.markets)} markets, {res.credits_spent} credits")
    except OddsAPIError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    for n in notes:
        print(f"  note: {n}")
    print(f"Credits left this month: {api.remaining}\n")

    before = len(all_markets)
    all_markets = select_markets(all_markets, avoid=avoid, within_days=a.within_days,
                                 min_lead_minutes=a.min_lead)
    print(f"Using {len(all_markets)} of {before} markets (starting in {a.min_lead:g}+ min"
          + (f", within {a.within_days:g} days" if a.within_days is not None else "")
          + (f", avoiding {', '.join(avoid)}" if avoid else "") + ").\n")
    at_book = [m for m in all_markets if a.book in m.prices]
    if not at_book:
        print(f"No markets priced at {a.book}.")
        return 0

    # --- which markets are worth using (bonus grids) -----------------------------------
    if a.bonus:
        print_leg_scores(at_book, a.book, a.top)
    kw = {"hedge_books": books}  # never hedge at the reference book
    if a.sharp != "none":
        kw["sharp_book"] = a.sharp
    if a.boost and str(a.min_odds).lower() != "none":
        kw["min_odds"] = to_decimal(float(a.min_odds))
    if a.max_odds is not None:
        kw["max_odds"] = to_decimal(a.max_odds)
    if a.max_cash is not None:
        kw["max_cash"] = a.max_cash
    if a.boost:
        print_boost(all_markets, a, kw)
    if a.bonus:
        print_bonus(all_markets, a, books)

    # --- the one recommended plan -----------------------------------------------------
    plan = promo_plan(all_markets, a.book, bonus=a.bonus, boost_pct=a.boost, boost_max_stake=a.max_stake,
                      max_cash=a.max_cash, hedge_books=books,
                      boost_min_odds=kw.get("min_odds"), boost_max_odds=kw.get("max_odds"),
                      sharp_book=kw.get("sharp_book", "pinnacle"))
    for s in plan.slips:
        stamp_slip(s, all_markets)
    print("\n" + "=" * 80 + "\n")
    print(plan.to_markdown())

    print(f"\n{DISCLAIMER}")
    return 0


def print_leg_scores(at_book, book, top):
    print(f"## Best grid legs at {book_name(book)} right now")
    print("| Game | Market | Prices | Hold | Leg score |\n|---|---|---|---|---|")
    for r in rank_markets(at_book, book, top=top):
        prices = " / ".join(f"{k} {v}" for k, v in r["prices"].items())
        print(f"| {r['game']} | {r['label']} | {prices} | {r['hold']:.2%} | {r['grid_value']:.2%} |")
    print("Leg score ranks markets as grid legs (conversion if paired with two -110/-110 legs); "
          "the final grid combines the best legs with each other, so it scores higher.\n")



def print_boost(all_markets, a, kw):
    plan = boost_plan(all_markets, a.book, a.boost, a.max_stake, **kw)
    limits = f", odds {a.min_odds} or longer" if "min_odds" in kw else ", no minimum odds"
    print(f"## {a.boost:.0%} profit boost on {book_name(a.book)}, max wager ${a.max_stake:,.0f}{limits}\n")
    for key, title in (("single_book", f"Only {book_name(a.book)}"), ("multi_book", "Across your books")):
        opts = plan[f"{key}_options"]
        if not opts:
            continue
        if key == "multi_book" and all(t.book == a.book for o in opts for t in o.tickets):
            print("### Across your books: same as above (no better hedge price at your other books)\n")
            continue
        print(f"### {title}: profit vs cash")
        print("| Boost | Locked profit | Cash needed |\n|---|---|---|")
        for s in opts:
            l = s.tickets[0].legs[0]
            print(f"| {l.game}: {l.selection} at ${s.tickets[0].stake:,.2f} | "
                  f"${s.locked_profit:,.2f} | ${s.cash_required:,.2f} |")
        stamp_slip(opts[0], all_markets)
        print(f"\n{opts[0].to_markdown()}\n")
    if plan["no_lock_message"]:
        print(plan["no_lock_message"])
    print("Best boost by expected value (unhedged):")
    print("| Game | Market | Pick | Odds → boosted | Fair prob | EV | Fair from |\n|---|---|---|---|---|---|---|")
    for r in plan["best_ev"][:5]:
        print(f"| {r['game']} | {r['market']} | {r['selection']} | {r['odds']} → {r['boosted_odds']} | "
              f"{r['fair_prob']:.1%} | ${r['ev']:,.2f} ({r['ev_pct']:.1%}) | {r['fair_source']} |")
    print()


def print_bonus(all_markets, a, books):
    print(f"## ${a.bonus:,.0f} bonus bet on {book_name(a.book)}\n")
    slips = compare_bonus_methods(all_markets, a.book, a.bonus, hedge_books=books,
                                  max_cash=a.max_cash)
    for s in slips:
        print(f"- {s.method.split('.')[0]}: {s.conversion:.1%} (${s.locked_profit:,.2f}), "
              f"cash needed ${s.cash_required:,.2f}")
    for s in slips:
        stamp_slip(s, all_markets)
        print(f"\n{s.to_markdown()}")


if __name__ == "__main__":
    sys.exit(main())
