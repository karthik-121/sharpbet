"""Tools the assistant can call. Each returns finished text built by sharpbet, so the model
relays numbers instead of computing them.

Toolbox holds per-conversation state: pulled odds (cached a few minutes, so follow-up questions
don't spend API credits) and the last grid (for partial-placement re-solves).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

from ..bonus import resolve_grid
from ..checks import check_bonus_math
from ..models import Market, Slip
from ..odds import american_to_decimal
from ..plan import book_name, plan_to_dict, promo_plan, ticket_dict, _ticket_line
from ..select import select_markets

DEFAULT_SPORTS = ["nfl", "ncaaf", "mlb", "epl"]
# The one approved way to mention typical boost sizes when asking a user to confirm an unusual one.
# It lives in the tool descriptions, so the untraced-number guard treats these figures as known.
TYPICAL_BOOSTS = "boosts are usually more like 25%, 50% or 100%"

# What users (and the model) call books -> The Odds API keys. Real 2026-09-27 bug: "caesars" isn't a
# key (Caesars is "williamhill_us"), so the tool found no prices and reported "nothing locks".
BOOK_ALIASES = {
    "fanduel": "fanduel", "fd": "fanduel", "fan duel": "fanduel",
    "draftkings": "draftkings", "dk": "draftkings", "draft kings": "draftkings",
    "betmgm": "betmgm", "mgm": "betmgm", "bet mgm": "betmgm",
    "caesars": "williamhill_us", "caesars sportsbook": "williamhill_us", "williamhill_us": "williamhill_us",
    "william hill": "williamhill_us", "williamhill": "williamhill_us",
    "espnbet": "espnbet", "espn bet": "espnbet", "espn": "espnbet",
    "fanatics": "fanatics", "betrivers": "betrivers", "bet rivers": "betrivers",
    "hardrockbet": "hardrockbet", "hard rock": "hardrockbet", "hard rock bet": "hardrockbet",
    "bovada": "bovada", "betonline": "betonlineag", "betonlineag": "betonlineag",
}


def book_key(name: str) -> str:
    n = name.strip().lower()
    return BOOK_ALIASES.get(n, n.replace(" ", ""))


def expand_books(names: list[str]) -> list[str]:
    """Book names -> feed keys, in order, without duplicates. "all"/"any book" -> every US book."""
    from ..data.theoddsapi import ALL_BOOKS_WORDS, US_BOOKS
    out = []
    for n in names:
        keys = US_BOOKS if n.strip().lower() in ALL_BOOKS_WORDS else [book_key(n)]
        out += [k for k in keys if k not in out]
    return out
SHOWN_NOTE = ("[The slip below is shown to the user automatically, right after your message. Do not "
              "repeat it or list its bets; write only your short lead.]")
_SPORT_NAMES = {"americanfootball_nfl": "NFL", "americanfootball_ncaaf": "college football",
                "baseball_mlb": "MLB", "icehockey_nhl": "NHL", "basketball_nba": "NBA",
                "basketball_wnba": "WNBA", "soccer_epl": "EPL", "soccer_usa_mls": "MLS",
                "soccer_uefa_champs_league": "Champions League"}
SHARP_BOOK = "pinnacle"  # reference prices only; never a hedge book
CACHE_TTL = timedelta(minutes=5)

# market_source(sport, books) -> Markets. Live: The Odds API. Tests: saved fixtures.
MarketSource = Callable[[str, list[str]], list[Market]]


def live_market_source(sport: str, books: list[str]) -> list[Market]:
    from ..data import OddsAPI, fetch_markets
    api = _live_api()
    return fetch_markets(api, sport, ["h2h", "spreads", "totals"], books).markets


_API = None


def _live_api():
    global _API
    if _API is None:
        from ..data import OddsAPI
        _API = OddsAPI()
    return _API


# ---------------------------------------------------------------- schemas

_SELECTION = {
    "sports": {"type": "array", "items": {"type": "string"},
               "description": "Sports/leagues to use, e.g. nfl, ncaaf, mlb, nhl, nba, epl, mls, soccer, "
                              "football. Default: nfl, ncaaf, mlb, epl."},
    "avoid": {"type": "array", "items": {"type": "string"},
              "description": "Sports never to use, e.g. ['mlb'] when the user wants to avoid rain risk."},
    "within_days": {"type": "number",
                    "description": "Only games starting within this many days from now (how soon it must "
                                   "settle). Omit if the user doesn't care."},
    "books": {"type": "array", "items": {"type": "string"},
              "description": "Books the user can bet at for hedges, by name (fanduel, draftkings, betmgm, "
                             "caesars, betrivers, ...). Pass [\"all\"] when the user says any/all books or "
                             "every sportsbook: that means FanDuel, DraftKings, BetMGM, Caesars, BetRivers, "
                             "ESPN BET, Fanatics and Hard Rock Bet, at no extra cost. Include the promo book. Not "
                             "every book has prices in the odds feed; the tool reports which are missing, so "
                             "never claim coverage the tool hasn't confirmed."},
}

TOOLS = [
    {
        "name": "plan_bonus_bet",
        "description": (
            "Find the best way to convert a bonus bet (stake not returned) into locked cash, using live "
            "odds. Compares the single-book grid (bonus split across parlays covering every outcome), "
            "a single-book cash hedge and a multi-book hedge, within the user's cash. Returns the exact "
            "'What to place' summary to show the user."),
        "input_schema": {
            "type": "object",
            "properties": {
                "book": {"type": "string", "description": "Book the bonus bet is on, e.g. fanduel, caesars."},
                "amount": {"type": "number", "description": "Bonus bet amount in dollars."},
                "max_cash": {"type": "number",
                             "description": "Most cash the user can put down for hedges. 0 = none. Omit if they "
                                            "say 'as much as needed' (no limit)."},
                "splittable": {"type": "boolean",
                               "description": "Can the bonus be split across several bets? Default true."},
                "tickets": {"type": "integer",
                            "description": "Only when the user asks for a specific number of bets in the grid "
                                           "('make it 8 bets', '12 bets'). Omit for the most profitable grid."},
                "legs": {"type": "integer",
                         "description": "Only when the user asks for a parlay length ('3-leg parlays')."},
                "max_tickets": {"type": "integer",
                                "description": "Only when the user wants at most this many bets ('under 10 bets', "
                                               "'fewer bets' after a grid of N -> N-1)."},
                **_SELECTION,
            },
            "required": ["book", "amount"],
        },
    },
    {
        "name": "plan_profit_boost",
        "description": (
            "Find the best locked profit from a profit boost on a CASH bet (a boost never goes on a bonus "
            "bet), hedging the other side at the user's books, within the user's cash. Also returns "
            "alternatives (profit vs cash) and, if nothing locks, the best unhedged expected-value option. "
            "Returns the exact 'What to place' summary to show the user."),
        "input_schema": {
            "type": "object",
            "properties": {
                "book": {"type": "string", "description": "Book the boost is on, e.g. draftkings, caesars."},
                "boost_percent": {"type": "number",
                                  "description": f"Profit boost in percent, e.g. 50. For reference, {TYPICAL_BOOSTS}."},
                "max_wager": {"type": "number", "description": "The boost's max wager in dollars."},
                "max_cash": {"type": "number",
                             "description": "Most cash the user can put down across the boosted bet and hedge. "
                                            "Omit if they say 'as much as needed' (no limit)."},
                "min_odds": {"type": "integer",
                             "description": "Boost's minimum odds before boosting, American. Default -200."},
                "max_odds": {"type": "integer", "description": "Boost's maximum odds, American, if any."},
                **_SELECTION,
            },
            "required": ["book", "boost_percent", "max_wager"],
        },
    },
    {
        "name": "plan_profit_boosts",
        "description": (
            "Use whenever the user has TWO OR MORE profit boosts. Plans all of them together: which boost goes "
            "on which game and side, hedged with the user's other boosts (e.g. 50% on the Over vs 50% on the "
            "Under) and/or cash, for the most total locked profit within one shared cash limit. Each boost is "
            "used at most once, never two on the same bet. Returns one combined 'What to place' slip, "
            "including which boosts weren't used and why."),
        "input_schema": {
            "type": "object",
            "properties": {
                "boosts": {
                    "type": "array", "description": "Every boost the user listed, one entry each (list duplicates separately).",
                    "items": {"type": "object", "properties": {
                        "book": {"type": "string", "description": "Book the boost is on."},
                        "boost_percent": {"type": "number", "description": "e.g. 50 or 100."},
                        "max_wager": {"type": "number"},
                        "min_odds": {"type": "integer", "description": "Minimum pre-boost odds, American. Default -200."},
                        "max_odds": {"type": "integer"},
                        "sports": {"type": "array", "items": {"type": "string"},
                                   "description": "Sports this boost is restricted to, e.g. ['nfl']. Omit if any."},
                        "slot": {"type": "string",
                                 "description": "Time-slot restriction: snf, mnf, tnf, sunday_early (the 1 PM ET games), "
                                                "sunday_late (4:05/4:25 PM ET games), prime_time, sunday, saturday."},
                        "games": {"type": "array", "items": {"type": "string"},
                                  "description": "Restrict to specific games by team name, e.g. ['Rams']."},
                    }, "required": ["book", "boost_percent", "max_wager"]}},
                "boosts_on_both_sides": {"type": "boolean",
                                         "description": "Allow boosts on both sides of one game (default true). False "
                                                        "if the user wants to avoid that (some books forbid it)."},
                "total_boosts": {"type": "integer",
                                 "description": "How many boosts the user says they have in total, counted from their "
                                                "own words (e.g. 'I have 4 boosts' -> 4). Must equal len(boosts)."},
                "max_cash": {"type": "number",
                             "description": "Most cash the user can put down across ALL boosted bets and hedges. "
                                            "Omit if they say 'as much as needed'."},
                **_SELECTION,
            },
            "required": ["boosts", "total_boosts"],
        },
    },
    {
        "name": "resolve_partial_grid",
        "description": (
            "Re-solve the most recent bonus grid after the user placed some tickets and/or prices moved. "
            "Keeps placed tickets and re-sizes the unplaced ones so every outcome still pays the same."),
        "input_schema": {
            "type": "object",
            "properties": {
                "placed": {"type": "array", "items": {"type": "integer"},
                           "description": "Ticket numbers already placed at their listed odds."},
                "placed_at_different_odds": {
                    "type": "array", "description": "Tickets placed, but at odds other than listed.",
                    "items": {"type": "object", "properties": {
                        "ticket": {"type": "integer"}, "odds": {"type": "integer"}},
                        "required": ["ticket", "odds"]}},
                "moved": {
                    "type": "array", "description": "Unplaced tickets whose parlay odds changed in the app.",
                    "items": {"type": "object", "properties": {
                        "ticket": {"type": "integer"}, "odds": {"type": "integer"}},
                        "required": ["ticket", "odds"]}},
            },
            "required": ["placed"],
        },
    },
    {
        "name": "resize_boost",
        "description": (
            "Use when the book shows different boosted odds than the boost slip said (books sometimes "
            "price boosts a little higher, e.g. FanDuel showed +445 where the slip said +443). Re-sizes the "
            "most recent boost plan around the odds the app shows so every outcome locks the same amount. "
            "Refuses odds far from the expected price (likely a typo, wrong bet, or wrong boost percent)."),
        "input_schema": {
            "type": "object",
            "properties": {
                "boosted_odds": {"type": "integer",
                                 "description": "Boosted odds the app shows on the slip, American, e.g. 445."},
                "already_placed": {"type": "boolean",
                                   "description": "True if the boosted bet is already placed (keep its stake). "
                                                  "Default false."},
            },
            "required": ["boosted_odds"],
        },
    },
    {
        "name": "check_bonus_math",
        "description": (
            "Check a user's own 'cover every outcome' bonus-bet plan or calculator result. Arbitrage "
            "calculators assume the stake is returned; bonus bets don't return it. Returns what an arb "
            "calculator shows vs the true best conversion, and the worst case for the user's stakes."),
        "input_schema": {
            "type": "object",
            "properties": {
                "odds": {"type": "array", "items": {"type": "integer"},
                         "description": "American odds of each outcome (or each parlay) being covered."},
                "bonus": {"type": "number", "description": "Bonus amount. Default 100."},
                "stakes": {"type": "array", "items": {"type": "number"},
                           "description": "The user's stakes per outcome, same order, if they gave them."},
            },
            "required": ["odds"],
        },
    },
]


_BOOST_ITEM = next(t for t in TOOLS if t["name"] == "plan_profit_boosts")["input_schema"]["properties"]["boosts"]["items"]
TOOLS.append({
    "name": "compare_promo_options",
    "description": (
        "Use when the user must CHOOSE between offers and asks which is worth more, e.g. 'two 50% boosts "
        "($300 max each) or one 100% boost ($250 max)?', or 'a $100 bonus bet or a 50% boost?'. Plans every "
        "option on the same live prices, games, books and cash limit (an option's boosts can hedge each "
        "other; an option can mix boosts and bonus bets) and ranks them by locked profit. Returns the "
        "comparison, how much more the best option locks, whether the winner changes with more cash, and "
        "the best option's 'What to place' slip."),
    "input_schema": {
        "type": "object",
        "properties": {
            "options": {
                "type": "array", "minItems": 2,
                "description": "Each offer the user is choosing between, with ALL of its promos.",
                "items": {"type": "object", "properties": {
                    "name": {"type": "string",
                             "description": "Short name the user would recognize, e.g. 'Two 50% boosts' or 'Option A'."},
                    "boosts": {"type": "array", "items": _BOOST_ITEM,
                               "description": "Profit boosts in this option, one entry each (duplicates separately)."},
                    "bonus_bets": {"type": "array", "description": "Bonus bets in this option, one entry each.",
                                   "items": {"type": "object", "properties": {
                                       "book": {"type": "string"},
                                       "amount": {"type": "number", "description": "Bonus bet amount in dollars."},
                                       "splittable": {"type": "boolean",
                                                      "description": "Can it be split across bets? Default true."}},
                                       "required": ["book", "amount"]}},
                }, "required": ["name"]}},
            "max_cash": {"type": "number",
                         "description": "Most cash the user can put down for whichever option they take. Omit if "
                                        "they say 'as much as needed'."},
            "boosts_on_both_sides": {"type": "boolean",
                                     "description": "Allow boosts on both sides of one game (default true)."},
            **_SELECTION,
        },
        "required": ["options"],
    },
})


# ---------------------------------------------------------------- handlers

@dataclass
class Toolbox:
    market_source: MarketSource = live_market_source
    now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    _cache: dict = field(default_factory=dict)
    last_grid: Slip | None = None
    last_boost: Slip | None = None          # for resize_boost
    last_boost_cash: float | None = None
    boost_roster: list[str] | None = None   # boost labels from the last multi-boost plan in this conversation
    display: str | None = None  # latest user-facing slip; the interface prints it verbatim
    display_data: dict | None = None  # the same slip as data, for the web UI to render as tables

    def run(self, name: str, args: dict) -> str:
        fn = getattr(self, f"_{name}", None)
        if fn is None or name not in {t["name"] for t in TOOLS}:
            raise ValueError(f"Unknown tool: {name}")
        return fn(**args)

    # --- data
    def _markets(self, book, books: list[str] | None, sports: list[str] | None,
                 avoid: list[str] | None, within_days: float | None) -> tuple[list[Market], list[str], str]:
        promo_books = [book] if isinstance(book, str) else list(book)
        hedge_books = expand_books(books or promo_books)
        for pb in reversed(promo_books):
            if pb not in hedge_books:
                hedge_books.insert(0, pb)
        hedge_books = [b for b in hedge_books if b != SHARP_BOOK]
        pull_books = sorted(set(hedge_books) | {SHARP_BOOK})
        from ..select import _matches
        from ..data.theoddsapi import sport_key
        wanted = [s for s in (sports or DEFAULT_SPORTS) if not _matches(sport_key(s), avoid or [])]
        now = self.now_fn()
        markets = []
        for sport in wanted:
            key = (sport, tuple(pull_books))
            hit = self._cache.get(key)
            if hit is None or now - hit[0] > CACHE_TTL:
                hit = (now, self.market_source(sport, pull_books))
                self._cache[key] = hit
            markets += hit[1]
        chosen = select_markets(markets, avoid=avoid, within_days=within_days, now=now)
        starts = sorted(m.start.astimezone() for m in chosen if m.start)
        day = lambda d: f"{d.strftime('%a %b')} {d.day}"
        window = (f"Games scanned: {day(starts[0])}" + (f" to {day(starts[-1])}" if day(starts[0]) != day(starts[-1]) else "")
                  + (" (no time limit given)" if within_days is None else f" (within {within_days:g} days)")
                  + ". " if starts else "No games in this window. ")
        note = (f"Now: {now.astimezone().strftime('%a %b %d %I:%M %p %Z')}. {window}Scanned {len(chosen)} markets "
                f"({', '.join(wanted) or 'none'} scanned; games starting 30+ min from now"
                + (f", within {within_days:g} days" if within_days else "")
                + (f", avoiding {', '.join(avoid)}" if avoid else "") + ").")
        return chosen, hedge_books, note

    # --- tools
    def _no_prices(self, book: str, markets: list[Market], hedge_books: list[str]) -> str | None:
        have = sorted({book_name(b) for m in markets for b in m.prices if b != SHARP_BOOK})
        if not any(book in m.prices for m in markets):
            return (f"NO PRICES: the odds feed returned no prices at {book_name(book)} for these games, so "
                    "nothing can be planned there. This is not 'no lock'. Books that returned prices in "
                    f"this pull: {', '.join(have) or 'none'}. (Coverage varies; e.g. Caesars was missing "
                    "from the feed on 2026-09-27.)")
        missing = [b for b in hedge_books if b != book and not any(b in m.prices for m in markets)]
        return (f"Note: no prices found at {', '.join(book_name(b) for b in missing)}; hedges there weren't "
                "considered.") if missing else None

    def _plan_bonus_bet(self, book, amount, max_cash=None, splittable=True, books=None, sports=None,
                        avoid=None, within_days=None, tickets=None, legs=None, max_tickets=None) -> str:
        book = book_key(book)
        markets, hedge_books, note = self._markets(book, books, sports, avoid, within_days)
        warn = self._no_prices(book, markets, hedge_books)
        if warn and warn.startswith("NO PRICES"):
            self.display = self.display_data = None
            return f"{note}\n{warn}"
        note = f"{note}\n{warn}" if warn else note
        if tickets or legs or max_tickets:
            return self._sized_grid(book, amount, markets, note, tickets, legs, max_tickets)
        plan = promo_plan(markets, book, bonus=amount, max_cash=max_cash, hedge_books=hedge_books,
                          bonus_splittable=splittable)
        _stamp(plan.slips, markets)
        self.last_grid = plan.bonus if plan.bonus and plan.bonus.cash_required == 0 and \
            sum(t.is_bonus for t in plan.bonus.tickets) > 1 else None
        if self.last_grid is not None:
            from ..gridsize import fewer_bets_line
            line = fewer_bets_line(markets, book, amount, self.last_grid)
            if line:
                plan.alternatives.insert(0, line)
        return self._show(note, plan, markets)

    def _sized_grid(self, book, amount, markets, note, tickets, legs, max_tickets) -> str:
        """A grid of the size the user asked for, with every other available size and its lock."""
        from ..gridsize import sized_grid
        from ..plan import PromoPlan
        sg = sized_grid(markets, book, amount, tickets=tickets, legs=legs, max_tickets=max_tickets)
        sizes = "; ".join(o.describe() for o in sg.options) or "none"
        if sg.choice is None:
            self.display = self.display_data = None
            return (f"{note}\nNO GRID of {sg.request} from these games (ticket counts depend on the legs: 2-way "
                    f"legs give 4, 8, 16, 32; a 3-way leg gives 6, 12, 24). Sizes available: {sizes}. Offer the "
                    "closest sizes; nothing was planned.")
        slip = sg.choice.slip
        plan = PromoPlan(book, slip, None, None, alternatives=sg.others())
        _stamp(plan.slips, markets)
        self.last_grid = slip
        head = (f"Grid sized as asked ({sg.request}): {sg.choice.describe()}."
                + (f" The most profitable grid is {sg.best.describe()}." if sg.best and sg.best is not sg.choice else
                   " This is also the most profitable grid."))
        return self._show(f"{note}\n{head}", plan, markets)

    def _plan_profit_boost(self, book, boost_percent, max_wager, max_cash=None, min_odds=-200, max_odds=None,
                           books=None, sports=None, avoid=None, within_days=None) -> str:
        book = book_key(book)
        markets, hedge_books, note = self._markets(book, books, sports, avoid, within_days)
        warn = self._no_prices(book, markets, hedge_books)
        if warn and warn.startswith("NO PRICES"):
            self.display = self.display_data = None
            return f"{note}\n{warn}"
        note = f"{note}\n{warn}" if warn else note
        plan = promo_plan(markets, book, boost_pct=boost_percent / 100, boost_max_stake=max_wager,
                          max_cash=max_cash, hedge_books=hedge_books,
                          boost_min_odds=american_to_decimal(min_odds) if min_odds is not None else None,
                          boost_max_odds=american_to_decimal(max_odds) if max_odds is not None else None,
                          sharp_book=SHARP_BOOK)
        _stamp(plan.slips, markets)
        self.last_boost, self.last_boost_cash = plan.boost, max_cash
        rules = (f"Boost rules used: {boost_percent:g}% on {book_name(book)}, max wager ${max_wager:,.2f}"
                 + (f", odds {min_odds:+d} or longer" if min_odds is not None else "")
                 + (f", up to {max_odds:+d}" if max_odds is not None else "") + ".")
        return self._show(f"{note}\n{rules}", plan, markets)

    def _show(self, header: str, plan, markets: list[Market]) -> str:
        """Store the slip for the interface to print, and return the model's copy with a note
        not to repeat it."""
        slip = plan.to_markdown()
        self.display = slip
        self.display_data = plan_to_dict(plan)
        sports = {m.sport for m in markets for s in plan.slips for t in s.tickets for l in t.legs
                  if (m.game, m.label) == (l.game, l.label) and m.sport}
        used = f"Bets use: {', '.join(sorted(_SPORT_NAMES.get(x, x) for x in sports))}.\n" if sports else ""
        return f"{header}\n{used}{SHOWN_NOTE}\n\n{slip}"

    def _resolve_partial_grid(self, placed, placed_at_different_odds=None, moved=None) -> str:
        if self.last_grid is None:
            return "No grid to re-solve yet: plan the bonus bet first."
        slip = self.last_grid
        n = len(slip.tickets)
        bad = [t for t in placed if not 1 <= t <= n]
        if bad:
            return f"Tickets {bad} don't exist; the grid has tickets 1-{n}."
        placed_odds = {t: slip.tickets[t - 1].decimal for t in placed}
        for p in placed_at_different_odds or []:
            placed_odds[p["ticket"]] = american_to_decimal(p["odds"])
        moved_odds = {m["ticket"]: american_to_decimal(m["odds"]) for m in moved or []}
        new = resolve_grid(slip, placed_odds, moved_odds)
        self.last_grid = new
        keep = sorted(placed_odds)
        todo = [i for i in range(1, n + 1) if i not in placed_odds]
        lines = [f"## Re-solved grid", "",
                 f"**Locks ${new.locked_profit:,.2f} ({new.conversion:.1%}) if the remaining tickets are placed "
                 f"at these odds.** Was ${slip.locked_profit:,.2f}.",
                 f"Keep as placed: tickets {', '.join(map(str, keep)) or 'none'}.", "", "### Place these now"]
        lines += [_ticket_line(i, new.tickets[i - 1]) for i in todo]
        self.display = "\n".join(lines)
        self.display_data = {"kind": "resolve", "locked_profit": round(new.locked_profit, 2),
                             "conversion": round(new.conversion, 4), "was": round(slip.locked_profit, 2),
                             "keep": keep, "tickets": [ticket_dict(i, new.tickets[i - 1]) for i in todo]}
        return f"{SHOWN_NOTE}\n\n{self.display}"

    def _plan_profit_boosts(self, boosts, total_boosts=None, max_cash=None, boosts_on_both_sides=True,
                            books=None, sports=None, avoid=None, within_days=None) -> str:
        from ..multiboost import plan_boosts
        from ..plan import multi_markdown, multi_to_dict
        if not boosts:
            return "No boosts given."
        if total_boosts is not None and total_boosts != len(boosts):
            self.display = self.display_data = None
            return (f"CHECK: the user said they have {total_boosts} boosts, but {len(boosts)} were passed. Nothing "
                    "was planned. Include every boost the user listed (one entry each, duplicates separately), or "
                    "ask which ones they mean.")
        parsed = _parse_boosts(boosts)
        if isinstance(parsed, str):
            return parsed
        promo_books = list(dict.fromkeys(b.book for b in parsed))
        all_sports = sports or sorted({x for spec in boosts for x in (spec.get("sports") or [])}) or None
        markets, hedge_books, note = self._markets(promo_books, books, all_sports, avoid, within_days)
        missing = [b for b in promo_books if not any(b in m.prices for m in markets)]
        mp = plan_boosts(markets, parsed, hedge_books, max_cash=max_cash, allow_opposing=boosts_on_both_sides)
        times = [p.market.updated_at for p in mp.plans if p.market.updated_at]
        if times:
            mp.quoted_at = min(times).astimezone()
        text = multi_markdown(mp)
        self.display, self.display_data = text, multi_to_dict(mp)
        header = note + (f"\nNO PRICES at {', '.join(book_name(b) for b in missing)} in this pull." if missing else "")
        labels = [b.label for b in parsed]
        if self.boost_roster and len(labels) < len(self.boost_roster):
            dropped = [x for x in self.boost_roster if x not in labels] or self.boost_roster[len(labels):]
            header += (f"\nROSTER: earlier in this conversation the user had {len(self.boost_roster)} boosts "
                       f"({', '.join(self.boost_roster)}); this plan has {len(labels)}. Missing: "
                       f"{', '.join(dropped)}. Ask the user whether those were used or removed; if not, re-run with "
                       "all of them.")
        self.boost_roster = labels
        return f"{header}\n{SHOWN_NOTE}\n\n{text}"

    def _compare_promo_options(self, options, max_cash=None, boosts_on_both_sides=True, books=None, sports=None,
                               avoid=None, within_days=None) -> str:
        from ..compare import BonusPromo, compare_markdown, compare_options, compare_to_dict
        if len(options) < 2:
            return "CHECK: give at least two options to compare."
        parsed, promo_books, restricted = [], [], set()
        for o in options:
            if not o.get("boosts") and not o.get("bonus_bets"):
                return f"CHECK: option '{o.get('name')}' has no boosts or bonus bets. Ask the user what it includes."
            promos = _parse_boosts(o.get("boosts") or []) if o.get("boosts") else []
            if isinstance(promos, str):
                return promos
            promos += [BonusPromo(book_key(b["book"]), float(b["amount"]), b.get("splittable", True))
                       for b in o.get("bonus_bets") or []]
            parsed.append((o["name"], promos))
            promo_books += [p.book for p in promos]
            restricted |= {x for spec in o.get("boosts") or [] for x in (spec.get("sports") or [])}
        promo_books = list(dict.fromkeys(promo_books))
        # a restricted boost only narrows its own option; scan every sport any option can use
        any_unrestricted = any(b.get("bonus_bets") or any(not sp.get("sports") for sp in b.get("boosts") or [])
                               for b in options)
        all_sports = sports or (None if any_unrestricted else sorted(restricted) or None)
        markets, hedge_books, note = self._markets(promo_books, books, all_sports, avoid, within_days)
        missing = [b for b in promo_books if not any(b in m.prices for m in markets)]
        if missing:
            self.display = self.display_data = None
            return (f"{note}\nNO PRICES at {', '.join(book_name(b) for b in missing)} in this pull, so these options "
                    "can't be compared fairly. This is not 'no lock'.")
        cmp = compare_options(markets, parsed, hedge_books, max_cash=max_cash, allow_opposing=boosts_on_both_sides)
        for r in cmp.results + (cmp.unlimited or []):
            _stamp([s for p in r.bonuses for s in p.slips], markets)
            if r.boosts is not None:
                times = [p.market.updated_at for p in r.boosts.plans if p.market.updated_at]
                if times:
                    r.boosts.quoted_at = min(times).astimezone()  # legs already carry game starts
        text = compare_markdown(cmp)
        self.display, self.display_data = text, compare_to_dict(cmp)
        return f"{note}\n{SHOWN_NOTE}\n\n{text}"

    RESIZE_TOLERANCE = 0.02  # seen in real slips: +226 vs +225 (0.4%), +445 vs +442.5 (0.6%)

    def _resize_boost(self, boosted_odds, already_placed=False) -> str:
        from ..boost import resize_boost_lock
        from ..plan import PromoPlan
        from ..odds import fmt_american
        slip = self.last_boost
        if slip is None:
            return "No boost plan to re-size yet: plan the boost first."
        expected = slip.tickets[0].decimal
        try:
            shown = american_to_decimal(boosted_odds)
        except ValueError:
            return f"CHECK: {boosted_odds} isn't valid American odds. Nothing was changed."
        gap = (shown - 1) / (expected - 1) - 1
        if abs(gap) > self.RESIZE_TOLERANCE:
            return (f"CHECK: the app's odds {fmt_american(shown)} are {abs(gap):.0%} "
                    f"{'above' if gap > 0 else 'below'} the {fmt_american(expected)} expected for this boost. "
                    "Real differences are under 1%, so this is likely a typo, a different bet, or a different "
                    "boost percent. Ask the user to double-check all three; if the boost percent differs, plan "
                    "the boost again with the right percent. Nothing was changed; don't place the hedge yet.")
        new = resize_boost_lock(slip, shown, max_cash=self.last_boost_cash, keep_stake=already_placed)
        if new.locked_profit <= 0:
            self.display = self.display_data = None
            return (f"NO LOCK at {fmt_american(shown)}: with the odds the app shows, hedging no longer locks a "
                    "profit. Tell the user not to place the hedge"
                    + (" (the boosted bet is already placed, so it now rides unhedged unless they choose "
                       "otherwise)." if already_placed else " or the boosted bet."))
        self.last_boost = new
        plan = PromoPlan(slip.tickets[0].book, None, new, self.last_boost_cash, notes=list(new.notes))
        header = (f"Re-sized for {fmt_american(shown)} (the plan assumed {fmt_american(expected)}); "
                  f"{'boosted bet already placed' if already_placed else 'boosted bet not placed yet'}.")
        return self._show(header, plan, [])

    def _check_bonus_math(self, odds, bonus=100, stakes=None) -> str:
        """Finished text with percentages and dollars already formatted, so the model only quotes."""
        r = check_bonus_math([american_to_decimal(o) for o in odds], bonus, stakes)
        prices = ", ".join(f"{o:+d}" for o in odds)
        lines = [f"Outcomes covered: {prices}.",
                 f"An arbitrage calculator shows: {r['arb_calculator_shows']:.2%} (assumes the stake is returned).",
                 f"True best bonus conversion: {r['true_bonus_conversion_optimal']:.2%} "
                 f"(${r['true_bonus_conversion_optimal'] * bonus:,.2f} on a ${bonus:,.2f} bonus), with stakes "
                 "sized to 1/(decimal odds - 1).",
                 r["explanation"]]
        if stakes is not None:
            lines.append(f"With the user's stakes: worst case {r['your_stakes_worst_case']:.2%}, "
                         f"best case {r['your_stakes_best_case']:.2%}.")
        return "\n".join(lines)


def _parse_boosts(boosts: list[dict]):
    """Boost specs from a tool call -> Boost objects labeled the way the user thinks of them
    ("50% SNF boost #2"), or a CHECK message for an unknown time slot."""
    from ..multiboost import Boost
    from ..select import SLOTS, _matches, game_matches, in_slot, slot_key

    def base_label(spec):
        # name a boost the way the user thinks of it: "50% SNF boost", "100% NFL boost"
        slot = slot_key(spec.get("slot") or "") or spec.get("slot")
        short = (slot.upper() if slot in ("snf", "mnf", "tnf") else
                 {"sunday_early": "1 PM", "sunday_late": "late-afternoon", "prime_time": "prime-time"}.get(slot)
                 or (" ".join(spec.get("games") or []) or None)
                 or ("/".join(x.upper() for x in spec.get("sports") or []) or None))
        return f"{float(spec['boost_percent']):g}%" + (f" {short}" if short else "") + " boost"

    parsed, counts, seen = [], {}, {}
    for spec in boosts:
        counts[base_label(spec)] = counts.get(base_label(spec), 0) + 1
    for spec in boosts:
        pct = float(spec["boost_percent"])
        base = base_label(spec)
        seen[base] = seen.get(base, 0) + 1
        label = base + (f" #{seen[base]}" if counts[base] > 1 else "")
        sp = [x for x in spec.get("sports") or []]
        slot = spec.get("slot")
        if slot and (slot_key(slot) or slot) not in SLOTS:
            return f"CHECK: unknown time slot '{slot}'. Use one of: {', '.join(SLOTS)}."
        slot = slot_key(slot) or slot if slot else None
        teams = spec.get("games") or []
        restriction = ", ".join(x for x in [
            "/".join(s.upper() for s in sp) if sp else "",
            SLOTS[slot][3] if slot else "", ("games: " + ", ".join(teams)) if teams else ""] if x)

        def allow(m, sp=sp, slot=slot, teams=teams):
            return ((not sp or _matches(m.sport, sp)) and (not slot or in_slot(m, slot))
                    and (not teams or game_matches(m, teams)))
        mo = spec.get("min_odds", -200)
        parsed.append(Boost(book_key(spec["book"]), pct / 100, float(spec["max_wager"]), label,
                            american_to_decimal(mo) if mo is not None else None,
                            american_to_decimal(spec["max_odds"]) if spec.get("max_odds") is not None else None,
                            allow, restriction))
    return parsed


def _stamp(slips: list[Slip], markets: list[Market]) -> None:
    from ..data.theoddsapi import stamp_slip
    for s in slips:
        stamp_slip(s, markets)
