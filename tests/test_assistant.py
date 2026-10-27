"""Assistant layer tests: tools on the synthetic sample prices, and the chat loop with a fake Claude client.
No network, no API spend."""
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from sharpbet.assistant import ChatSession, SpendCapReached, TOOLS, Toolbox, untraced_numbers
from sharpbet.assistant.prompt import SYSTEM_PROMPT
from sharpbet.data import parse_events

FIX = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 9, 27, 1, 55, tzinfo=timezone.utc)
FILES = {"nfl": ("americanfootball_nfl__h2h-spreads-totals.json", "decimal"),
         "mlb": ("baseball_mlb__h2h-spreads-totals__american.json", "american"),
         "ncaaf": ("americanfootball_ncaaf__h2h-spreads-totals.json", "decimal"),
         "epl": ("soccer_epl__h2h-spreads-totals.json", "decimal")}


class FixtureSource:
    def __init__(self):
        self.calls = []

    def __call__(self, sport, books):
        self.calls.append((sport, tuple(books)))
        f, fmt = FILES[sport]
        return parse_events(json.loads((FIX / f).read_text()), odds_format=fmt, now=NOW)


def toolbox(clock=lambda: NOW):
    return Toolbox(market_source=FixtureSource(), now_fn=clock)


# ------------------------------------------------------------- tools

def test_tool_schemas_match_handlers():
    tb = toolbox()
    for t in TOOLS:
        assert hasattr(tb, f"_{t['name']}")
        assert set(t["input_schema"]["required"]) <= set(t["input_schema"]["properties"])
    with pytest.raises(ValueError):
        tb.run("place_bet", {})


def test_boost_tool_returns_the_slip_and_rules():
    out = toolbox().run("plan_profit_boost", {"book": "draftkings", "boost_percent": 50, "max_wager": 250,
                                              "max_cash": 600, "books": ["fanduel", "draftkings"]})
    assert "## What to place" in out and "apply the boost" in out
    assert "50% on DraftKings, max wager $250.00, odds -200 or longer" in out
    cash = float(re.search(r"Cash to put down: \$([\d,.]+)", out).group(1).replace(",", ""))
    assert cash <= 600


def test_boost_defaults_to_only_the_promo_book_and_never_pinnacle():
    out = toolbox().run("plan_profit_boost", {"book": "fanduel", "boost_percent": 50, "max_wager": 200,
                                              "max_cash": 600})
    assert "**DraftKings**" not in out and "**Pinnacle**" not in out
    out = toolbox().run("plan_profit_boost", {"book": "fanduel", "boost_percent": 50, "max_wager": 200,
                                              "max_cash": 600, "books": ["fanduel", "pinnacle"]})
    assert "**Pinnacle**" not in out


def test_bonus_tool_avoid_and_resolve_partial_grid():
    tb = toolbox()
    out = tb.run("plan_bonus_bet", {"book": "fanduel", "amount": 200, "max_cash": 0,
                                    "avoid": ["mlb"], "within_days": 2})
    assert "avoiding mlb" in out and "Grid: place all of these" in out
    n = len(tb.last_grid.tickets)
    assert tb.last_grid is not None and n >= 16       # the most profitable grid (32 tickets on the fixture prices)
    keep = n - 4                                       # place all but the last 4; the next one's price moved
    re_out = tb.run("resolve_partial_grid", {"placed": list(range(1, keep + 1)),
                                             "moved": [{"ticket": keep + 1, "odds": 1000}]})
    assert f"Keep as placed: tickets {', '.join(map(str, range(1, keep + 1)))}." in re_out
    assert len(re.findall(r"^\d+\. \*\*FanDuel\*\*", re_out, re.M)) == 4
    assert "Tickets [99] don't exist" in tb.run("resolve_partial_grid", {"placed": [99]})


def test_bonus_not_splittable_means_one_bonus_ticket():
    tb = toolbox()
    out = tb.run("plan_bonus_bet", {"book": "fanduel", "amount": 200, "max_cash": 600,
                                    "splittable": False, "books": ["fanduel", "draftkings"]})
    assert out.count("bonus bet,") == 1 and tb.last_grid is None


def test_odds_are_cached_between_questions():
    t = [NOW]
    tb = toolbox(clock=lambda: t[0])
    args = {"book": "fanduel", "boost_percent": 50, "max_wager": 200, "max_cash": 600, "sports": ["nfl"]}
    tb.run("plan_profit_boost", args)
    tb.run("plan_profit_boost", {**args, "max_cash": 300})       # follow-up: no new pull
    assert len(tb.market_source.calls) == 1
    t[0] = NOW + timedelta(minutes=6)                               # stale: pull again
    tb.run("plan_profit_boost", args)
    assert len(tb.market_source.calls) == 2


def test_check_bonus_math_tool():
    """Real 2026-09-27 test: +150/+220/+190. Percentages come pre-formatted, so the model's
    '94.58%' is traced (it was flagged when the tool returned 0.9458)."""
    out = toolbox().run("check_bonus_math", {"odds": [150, 220, 190]})
    assert "An arbitrage calculator shows: 94.58%" in out and "True best bonus conversion: 60.70%" in out
    reply = "No: a calculator on those prices shows 94.58%, but a bonus bet converts at most 60.70%."
    assert untraced_numbers(reply, [out]) == []


# ------------------------------------------------------------- untraced numbers

def test_untraced_numbers_flags_made_up_figures():
    src = ["Locks $43.94. Cash needed: $599.98. +150 → boosted +225. 69.5%", "I have $600, 50% boost"]
    assert untraced_numbers("Lock $43.94 for $599.98 of your $600: +150 → +225 (50%)", src) == []
    assert untraced_numbers("About $44, roughly 70% at +226", src) == ["$44", "70%", "+226"]
    # a plain number the user typed counts as dollars; a percentage doesn't
    assert untraced_numbers("With your $600 and 600 cash", ["I can put down 600"]) == []
    assert untraced_numbers("You'd lock $50", ["50% boost"]) == ["$50"]


# ------------------------------------------------------------- chat loop with a fake client

def block(type_, **kw):
    return SimpleNamespace(type=type_, **kw)


def resp(content, stop, inp=1000, out=200):
    return SimpleNamespace(content=content, stop_reason=stop, model="claude-opus-5",
                           usage=SimpleNamespace(input_tokens=inp, output_tokens=out,
                                                 cache_creation_input_tokens=0, cache_read_input_tokens=0))


class FakeClient:
    def __init__(self, script):
        self.script = list(script)
        self.requests = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.requests.append({**kw, "messages": list(kw["messages"])})
        item = self.script.pop(0)
        return item(kw) if callable(item) else item


BOOST_ARGS = {"book": "draftkings", "boost_percent": 50, "max_wager": 250, "max_cash": 600,
              "books": ["fanduel", "draftkings"], "sports": ["nfl"]}


def _reply_quoting_tool(kw):
    """Second model turn: quote the lock from the tool result, like the real model should."""
    tool_out = kw["messages"][-1]["content"][0]["content"]
    lock = re.search(r"Locks (\$[\d,.]+)", tool_out).group(1)
    return resp([block("text", text=f"Best lock is {lock}.")], "end_turn")  # short lead only


def test_chat_runs_tools_and_returns_the_slip():
    client = FakeClient([
        resp([block("thinking", thinking=""),
              block("tool_use", id="tu_1", name="plan_profit_boost", input=BOOST_ARGS)], "tool_use"),
        _reply_quoting_tool,
    ])
    s = ChatSession(client, toolbox=toolbox(), clock=lambda: NOW)
    r = s.send("50% boost on dk, max 250. I have $600 and fanduel too")
    assert r.tool_calls == ["plan_profit_boost"] and r.text.startswith("Best lock is $")
    # the slip comes straight from the tool, for the interface to print verbatim
    assert r.slip.startswith("## What to place") and "apply the boost" in r.slip
    assert r.slip in client.requests[1]["messages"][-1]["content"][0]["content"]
    assert r.untraced == []
    # the tool result went back in one user message, matched to the tool_use id
    second = client.requests[1]["messages"]
    assert second[-1]["content"][0]["tool_use_id"] == "tu_1"
    assert second[-2]["content"][0].type == "thinking"  # full assistant content kept
    # request shape: frozen system prompt, fallbacks on, cost counted
    req = client.requests[0]
    assert req["model"] == "claude-opus-5" and req["fallbacks"] == "default"
    assert req["betas"] == ["server-side-fallback-2026-07-01"] and req["thinking"] == {"type": "adaptive"}
    assert req["system"] == SYSTEM_PROMPT and "Current time" in req["messages"][0]["content"]
    assert r.cost == pytest.approx(2 * (1000 * 5 + 200 * 25) / 1e6)


def test_chat_flags_numbers_the_model_made_up():
    client = FakeClient([resp([block("text", text="You'll lock about $50, maybe 12%.")], "end_turn")])
    r = ChatSession(client, toolbox=toolbox(), clock=lambda: NOW).send("I have a 50% boost on fanduel")
    assert r.untraced == ["$50", "12%"]


def test_tool_errors_go_back_to_the_model():
    client = FakeClient([
        resp([block("tool_use", id="tu_1", name="resolve_partial_grid", input={"placed": [1]})], "tool_use"),
        resp([block("text", text="Plan the bonus bet first.")], "end_turn"),
    ])
    s = ChatSession(client, toolbox=toolbox(), clock=lambda: NOW)
    s.send("placed ticket 1")
    assert "No grid to re-solve" in client.requests[1]["messages"][-1]["content"][0]["content"]
    client2 = FakeClient([
        resp([block("tool_use", id="tu_1", name="plan_profit_boost", input={"book": "fanduel"})], "tool_use"),
        resp([block("text", text="What's the max wager?")], "end_turn"),
    ])
    ChatSession(client2, toolbox=toolbox(), clock=lambda: NOW).send("boost on fanduel")
    assert client2.requests[1]["messages"][-1]["content"][0]["is_error"] is True


def test_refusal_rolls_back_the_turn():
    client = FakeClient([resp([block("text", text="Hi")], "end_turn"), resp([], "refusal")])
    s = ChatSession(client, toolbox=toolbox(), clock=lambda: NOW)
    s.send("hello")
    before = len(s.messages)
    r = s.send("something declined")
    assert len(s.messages) == before and "can't help" in r.text


def test_spend_cap_stops_the_session():
    client = FakeClient([resp([block("text", text="ok")], "end_turn", inp=100_000, out=10_000)] * 3)
    s = ChatSession(client, toolbox=toolbox(), clock=lambda: NOW, max_spend=0.50)
    s.send("hi")          # costs $0.75
    with pytest.raises(SpendCapReached):
        s.send("again")


def test_documented_default_min_odds_is_not_flagged():
    client = FakeClient([resp([block("text", text="I'll assume the usual -200 minimum odds.")], "end_turn")])
    r = ChatSession(client, toolbox=toolbox(), clock=lambda: NOW).send("50% boost on draftkings")
    assert r.untraced == []


def test_every_bet_shows_its_game_date():
    """Real 2026-09-26 mix-up: the Chargers played Sep 27 (Bills) and Oct 4 (Seahawks). Every
    bet line must carry its date, and the headline says when games are."""
    out = toolbox().run("plan_profit_boost", {"book": "fanduel", "boost_percent": 50, "max_wager": 200,
                                              "max_cash": 1000, "books": ["fanduel", "draftkings"],
                                              "sports": ["nfl"]})
    bets = [l for l in out.splitlines() if re.match(r"^\d+\. \*\*", l)]
    assert bets and all(re.search(r"\[[^\]]*(Sun|Mon|Tue|Wed|Thu|Fri|Sat) \w{3} \d{1,2}, \d", l) for l in bets)
    assert "Games: " in out


def test_tool_output_states_the_date_window():
    tb = toolbox()
    base = {"book": "fanduel", "boost_percent": 50, "max_wager": 200, "max_cash": 1000, "sports": ["nfl"]}
    assert "Games scanned: Sun Sep 27 to " in tb.run("plan_profit_boost", base) and "(no time limit given)" in \
        tb.run("plan_profit_boost", base)
    assert "Games scanned: Sun Sep 27 to Mon Sep 28 (within 2 days)" in tb.run("plan_profit_boost", {**base, "within_days": 2})


def test_prompt_requires_timeframe_and_stating_dates():
    assert "the timeframe" in SYSTEM_PROMPT and "Always say which dates you looked at" in SYSTEM_PROMPT
    assert "give its matchup, date and start time together" in SYSTEM_PROMPT


def test_differences_between_options_are_flagged():
    """Real 2026-09-26 chat: the model said 'about $18 less' ($52.54 - $34.74) on its own."""
    src = ["Locks $52.54 ...", "Locks $34.74 ..."]
    assert untraced_numbers("about $18 less than the two-book version", src) == ["$18"]
    assert "never \"about $18" in SYSTEM_PROMPT


def test_slip_is_separate_and_model_is_told_not_to_repeat_it():
    tb = toolbox()
    out = tb.run("plan_bonus_bet", {"book": "fanduel", "amount": 1000, "max_cash": 0, "within_days": 1,
                                    "avoid": ["mlb"]})
    assert "shown to the user automatically" in out and tb.display in out
    assert "(nfl, ncaaf, epl scanned;" in out and "Bets use: NFL, college football." in out   # issue 1: exact wording data
    assert "Never reproduce it" in SYSTEM_PROMPT
    tb.run("resolve_partial_grid", {"placed": [1, 2]})
    assert tb.display.startswith("## Re-solved grid")


def test_no_tool_means_no_slip():
    client = FakeClient([resp([block("text", text="How much cash can you put down?")], "end_turn")])
    r = ChatSession(client, toolbox=toolbox(), clock=lambda: NOW).send("50% boost on fanduel")
    assert r.slip is None


def test_book_names_are_mapped_and_missing_prices_are_not_no_lock():
    """Real 2026-09-27 bug: 'caesars' isn't an API key, so the tool found no prices and the chat
    said nothing locks. Saved fixtures have no Caesars prices, which reproduces it."""
    from sharpbet.assistant.tools import book_key
    assert book_key("Caesars") == "williamhill_us" and book_key("DK") == "draftkings" and book_key("fd") == "fanduel"
    tb = toolbox()
    out = tb.run("plan_profit_boost", {"book": "caesars", "boost_percent": 25, "max_wager": 50, "max_cash": 200})
    assert "NO PRICES" in out and "Caesars" in out and "not 'no lock'" in out and tb.display is None
    assert "Books that returned prices in this pull: DraftKings, FanDuel." in out  # from data, not a fixed list
    out = tb.run("plan_profit_boost", {"book": "fd", "boost_percent": 50, "max_wager": 200, "max_cash": 600,
                                       "books": ["fd", "caesars"], "sports": ["nfl"]})
    assert "no prices found at Caesars" in out and "## What to place" in out


def test_help_resources_are_pinned():
    assert "1-800-GAMBLER" in SYSTEM_PROMPT and "800GAM" in SYSTEM_PROMPT and "1800gamblerchat.org" in SYSTEM_PROMPT
    assert "Don't give other links or numbers from memory" in SYSTEM_PROMPT


# ------------------------------------------------------------- structured slip (web UI)

def test_slip_data_matches_the_text_slip():
    tb = toolbox()
    tb.run("plan_bonus_bet", {"book": "fanduel", "amount": 200, "max_cash": 0, "within_days": 1})
    d, text = tb.display_data, tb.display
    assert d["kind"] == "plan" and f"${d['locked_profit']:,.2f}" in text
    sec = d["sections"][0]
    assert sec["type"] == "bonus" and len(sec["tickets"]) == len(tb.last_grid.tickets) >= 16
    for t in sec["tickets"]:   # every ticket row's numbers appear in the text slip line for it
        line = next(l for l in text.splitlines() if l.startswith(f"{t['n']}. **"))
        assert f"${t['stake']:,.2f}" in line and f"at {t['odds']}" in line
        assert all(leg["when"] and leg["odds"] in line for leg in t["legs"])
    tb.run("plan_profit_boost", {"book": "fanduel", "boost_percent": 50, "max_wager": 200, "max_cash": 600,
                                 "books": ["fanduel", "draftkings"], "sports": ["nfl"]})
    boost = tb.display_data["sections"][0]
    assert boost["type"] == "boost" and boost["tickets"][0]["boost"]["to"] in tb.display
    tb.run("plan_bonus_bet", {"book": "fanduel", "amount": 200, "max_cash": 0, "within_days": 1})
    tb.run("resolve_partial_grid", {"placed": [1, 2, 3]})
    assert tb.display_data["kind"] == "resolve" and tb.display_data["keep"] == [1, 2, 3]
    assert len(tb.display_data["tickets"]) == len(tb.last_grid.tickets) - 3   # the unplaced tickets, re-sized


# ------------------------------------------------------------- streaming and stop

import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "web"))
from demo import FakeStream  # noqa: E402


class StreamingFake(FakeClient):
    def __init__(self, script):
        super().__init__(script)
        self.beta.messages.stream = lambda **kw: FakeStream(self._create(**kw))


def test_streaming_sends_status_and_text_then_same_reply():
    events = []
    client = StreamingFake([
        resp([block("tool_use", id="tu_1", name="plan_profit_boost", input=BOOST_ARGS)], "tool_use"),
        _reply_quoting_tool,
    ])
    r = ChatSession(client, toolbox=toolbox(), clock=lambda: NOW).send("boost", on_event=events.append)
    kinds = [e["type"] for e in events]
    assert kinds[0] == "status" and "odds" in events[0]["text"] and "text" in kinds
    assert "".join(e["delta"] for e in events if e["type"] == "text") == r.text
    assert r.slip_data["kind"] == "plan"


def test_stop_keeps_the_question_and_drops_the_partial_answer():
    """Stopped mid-turn (after the tool ran): the user's message stays, marked as stopped; the
    unfinished tool step is dropped so the next request is valid; odds stay cached."""
    stop = {"now": False}
    tb = toolbox()

    def stop_after_tool(kw):
        stop["now"] = True   # the user presses stop while the model writes its lead
        return resp([block("text", text="Best lock is")], "end_turn")

    client = StreamingFake([
        resp([block("tool_use", id="tu_1", name="plan_profit_boost", input=BOOST_ARGS)], "tool_use"),
        stop_after_tool,
        resp([block("text", text="Sure, with $300 instead.")], "end_turn"),
    ])
    s = ChatSession(client, toolbox=tb, clock=lambda: NOW)
    from sharpbet.assistant import Stopped
    with pytest.raises(Stopped):
        s.send("50% boost on dk max 250, $600", on_event=lambda e: None, should_stop=lambda: stop["now"])
    assert len(s.messages) == 1 and s.messages[0]["role"] == "user"
    assert "50% boost on dk" in s.messages[0]["content"] and "stopped this reply" in s.messages[0]["content"]
    assert len(tb.market_source.calls) == 1        # the odds pull is cached, not lost
    stop["now"] = False
    r = s.send("actually $300")                     # conversation continues normally
    assert r.text == "Sure, with $300 instead."
    roles = [m["role"] for m in client.requests[-1]["messages"]]
    assert roles == ["user", "user"]                # the API merges consecutive user turns


def test_other_failures_roll_back_the_whole_turn():
    client = FakeClient([RuntimeError])
    def boom(**kw):
        raise RuntimeError("network down")
    client.beta.messages.create = boom
    s = ChatSession(client, toolbox=toolbox(), clock=lambda: NOW)
    with pytest.raises(RuntimeError):
        s.send("hello")
    assert s.messages == []


def test_resize_boost_tool_accepts_small_gaps_and_refuses_wild_ones():
    tb = toolbox()
    tb.run("plan_profit_boost", {"book": "fanduel", "boost_percent": 50, "max_wager": 200, "max_cash": 1000,
                                 "books": ["fanduel", "draftkings"], "sports": ["nfl"], "within_days": 1.5})
    shown_expected = tb.last_boost.tickets[0].decimal
    from sharpbet.odds import decimal_to_american
    near = round(decimal_to_american(1 + (shown_expected - 1) * 1.006))   # ~0.6% higher, like +445 vs +442.5
    out = tb.run("resize_boost", {"boosted_odds": near})
    assert out.startswith("Re-sized for") and tb.display_data["sections"][0]["type"] == "boost"
    assert "## What to place" in tb.display
    wild = round(decimal_to_american(1 + (shown_expected - 1) * 1.15))    # a typo-sized gap
    assert tb.run("resize_boost", {"boosted_odds": wild}).startswith("CHECK:")
    assert tb.run("resize_boost", {"boosted_odds": 50}).startswith("CHECK:")  # not valid odds
    assert toolbox().run("resize_boost", {"boosted_odds": 445}).startswith("No boost plan")


# ------------------------------------------------------------- several boosts (the real 2026-09-27 request)

FOUR_BOOSTS = [
    {"book": "fanduel", "boost_percent": 50, "max_wager": 200, "sports": ["nfl"]},
    {"book": "fanduel", "boost_percent": 50, "max_wager": 200, "sports": ["nfl"]},
    {"book": "fanduel", "boost_percent": 100, "max_wager": 100, "sports": ["nfl"]},
    {"book": "fanduel", "boost_percent": 50, "max_wager": 200, "sports": ["nfl"], "slot": "snf"},
]


def test_four_boosts_are_all_addressed_and_snf_stays_on_sunday_night():
    tb = toolbox()
    out = tb.run("plan_profit_boosts", {"boosts": FOUR_BOOSTS, "total_boosts": 4, "max_cash": 10000,
                                        "books": ["fanduel"], "sports": ["nfl"], "within_days": 1})
    d = tb.display_data
    tags = [t["boost"]["label"] for sec in d["sections"] for t in sec["tickets"] if t["boost"]]
    used, unused = set(tags), [u["boost"] for u in d["unused"]]
    assert len(used) + len(unused) == 4 and len(tags) == len(used)   # every boost placed once or explained
    snf = [t for sec in d["sections"] for t in sec["tickets"] if t["boost"] and "SNF" in t["boost"]["label"]]
    assert all(t["legs"][0]["game"] == "Los Angeles Rams @ Denver Broncos" for t in snf)
    assert "8:20 PM" in snf[0]["legs"][0]["when"]
    assert "Uses 4 of your 4 boosts" in out
    assert d["cash_required"] <= 10000


def test_two_snf_boosts_hedge_each_other_and_warn():
    """Both boosts are limited to the one Sunday night game, so they go on opposite sides (boost vs boost),
    and the both-sides warning comes first, labeled with that game."""
    tb = toolbox()
    two_snf = [{"book": "fanduel", "boost_percent": 50, "max_wager": 200, "sports": ["nfl"], "slot": "snf"}] * 2
    tb.run("plan_profit_boosts", {"boosts": two_snf, "total_boosts": 2, "books": ["fanduel"], "sports": ["nfl"],
                                  "within_days": 1})
    d = tb.display_data
    boosted = [t["legs"][0]["selection"] for sec in d["sections"] for t in sec["tickets"] if t["boost"]]
    assert sorted(s.split()[0] for s in boosted) == ["Over", "Under"] and not d["unused"]
    w = d["warnings"][0]
    assert "both sides of the same game" in w["text"] and w["about"] == "Los Angeles Rams @ Denver Broncos"


def test_four_boosts_with_little_cash_and_bad_slot():
    tb = toolbox()
    tb.run("plan_profit_boosts", {"boosts": FOUR_BOOSTS, "max_cash": 500, "books": ["fanduel"], "sports": ["nfl"],
                                  "within_days": 1})
    assert tb.display_data["cash_required"] <= 500
    assert tb.run("plan_profit_boosts", {"boosts": [{"book": "fd", "boost_percent": 50, "max_wager": 10,
                                                     "slot": "brunch"}], "max_cash": 100}).startswith("CHECK")


def test_prompt_routes_multiple_boosts_to_one_call():
    assert "call plan_profit_boosts ONCE" in SYSTEM_PROMPT and "address EVERY boost" in SYSTEM_PROMPT
    assert "slot snf" in SYSTEM_PROMPT


def test_a_forgotten_boost_is_caught_now_and_later():
    tb = toolbox()
    # the model passes 3 of the user's 4 boosts: refused, nothing planned
    out = tb.run("plan_profit_boosts", {"boosts": FOUR_BOOSTS[:3], "total_boosts": 4, "max_cash": 10000,
                                        "sports": ["nfl"], "within_days": 1})
    assert out.startswith("CHECK: the user said they have 4 boosts, but 3") and tb.display is None
    # all 4 planned, then a follow-up drops the SNF boost: flagged
    tb.run("plan_profit_boosts", {"boosts": FOUR_BOOSTS, "total_boosts": 4, "max_cash": 10000,
                                  "sports": ["nfl"], "within_days": 1})
    out = tb.run("plan_profit_boosts", {"boosts": FOUR_BOOSTS[:3], "total_boosts": 3, "max_cash": 500,
                                        "sports": ["nfl"], "within_days": 1})
    assert "ROSTER" in out and "50% SNF boost" in out.split("Missing:")[1].split(".")[0]


def test_per_game_cash_adds_up_and_is_in_the_text():
    tb = toolbox()
    out = tb.run("plan_profit_boosts", {"boosts": FOUR_BOOSTS, "total_boosts": 4, "books": ["fanduel"],
                                        "sports": ["nfl"], "within_days": 1})
    d = tb.display_data
    assert sum(sec["cash"] for sec in d["sections"]) == pytest.approx(d["cash_required"], abs=0.01)
    for sec in d["sections"]:   # the per-game cash Claude should quote is in its text (not added up by it)
        assert f"${sec['cash']:,.2f} cash" in out
    assert "Cash to put down:" in out and "your limit" not in out   # no limit given -> none shown


def test_boosts_can_be_kept_off_both_sides_of_a_game():
    tb = toolbox()
    tb.run("plan_profit_boosts", {"boosts": FOUR_BOOSTS, "total_boosts": 4, "books": ["fanduel"], "sports": ["nfl"],
                                  "within_days": 1, "boosts_on_both_sides": False})
    games = [t["legs"][0]["game"] for sec in tb.display_data["sections"] for t in sec["tickets"] if t["boost"]]
    assert len(games) == len(set(games))            # at most one boost per game
    assert "both sides of the same game" not in tb.display


def test_multi_boost_warnings_are_merged_and_labeled():
    """Real 2026-09-27 slip: the same-book warning was labeled with one game though it applied to all,
    and there was one postponement paragraph per game. (The both-sides warning is tested above.)"""
    tb = toolbox()
    tb.run("plan_profit_boosts", {"boosts": FOUR_BOOSTS, "total_boosts": 4, "books": ["fanduel"], "sports": ["nfl"],
                                  "within_days": 1})
    w = tb.display_data["warnings"]
    abouts = [x["about"] for x in w]
    assert abouts.count("All games") == 1 and abouts.count("Postponement risk") == 1
    post = next(x for x in w if x["about"] == "Postponement risk")["text"]
    assert all(sec["title"].split(" · ")[0] in post for sec in tb.display_data["sections"])
    assert "- All games: This covers every outcome" in tb.display


# ------------------------------------------------------------- "any/all books"

def test_all_books_means_every_major_us_book_at_no_extra_cost():
    from sharpbet.assistant.tools import expand_books
    from sharpbet.data import US_BOOKS, plan_cost
    assert expand_books(["all"]) == US_BOOKS and len(US_BOOKS) == 8
    assert expand_books(["FanDuel", "any book"]) == US_BOOKS              # no duplicates, FanDuel first
    assert expand_books(["dk", "Caesars"]) == ["draftkings", "williamhill_us"]
    assert "pinnacle" not in US_BOOKS                                      # reference only, never a hedge
    assert plan_cost(["h2h", "spreads", "totals"], US_BOOKS + ["pinnacle"]) == 3   # 9 books = one region


def test_tools_pull_all_books_and_report_the_missing_ones():
    tb = toolbox()
    out = tb.run("plan_profit_boost", {"book": "fanduel", "boost_percent": 50, "max_wager": 200, "max_cash": 600,
                                       "books": ["all"], "sports": ["nfl"], "within_days": 1})
    pulled = set(tb.market_source.calls[0][1])
    assert pulled == {"fanduel", "draftkings", "betmgm", "williamhill_us", "betrivers", "espnbet", "fanatics",
                      "hardrockbet", "pinnacle"}
    # the sample prices only have FanDuel and DraftKings: the others are named as missing, not guessed at
    assert "no prices found at" in out and "Caesars" in out and "## What to place" in out
    assert 'books=["all"]' in SYSTEM_PROMPT


def test_as_much_cash_as_needed_means_no_invented_limit():
    """Real 2026-09-27 test: the user said 'as much cash as needed' and the result said 'within $10,000.00 of
    cash', because the single-boost tool required a limit and the model made one up."""
    tb = toolbox()
    out = tb.run("plan_profit_boost", {"book": "fanduel", "boost_percent": 50, "max_wager": 200,
                                       "books": ["fanduel"], "sports": ["nfl"], "within_days": 1})
    assert "## What to place" in out and "Cash to put down:" in out and "your limit" not in out
    out = tb.run("plan_profit_boost", {"book": "draftkings", "boost_percent": 1, "max_wager": 200,
                                       "books": ["all"], "sports": ["nfl"], "within_days": 1})
    assert "Nothing locks" in out and "within $" not in out          # no made-up cash figure
    out = tb.run("plan_bonus_bet", {"book": "fanduel", "amount": 200, "sports": ["nfl"], "within_days": 1})
    assert "## What to place" in out
    for t in TOOLS:
        if t["name"] in ("plan_bonus_bet", "plan_profit_boost", "plan_profit_boosts"):
            assert "max_cash" not in t["input_schema"]["required"]


def test_unusual_numbers_are_confirmed_without_quoting_ranges():
    assert "always ask them to confirm it" in SYSTEM_PROMPT and "Don't quote typical ranges" in SYSTEM_PROMPT
    assert "can you confirm the exact percent in the app?" in SYSTEM_PROMPT
    assert "Never invent a limit" in SYSTEM_PROMPT


def test_approved_typical_boost_phrase_is_not_flagged_but_other_numbers_are():
    """User testing liked the nudge ("something like 10%/100%") but not the warning it triggered. The fixed
    phrase is allowed; made-up alternatives are still flagged."""
    from sharpbet.assistant.tools import TYPICAL_BOOSTS
    assert TYPICAL_BOOSTS in SYSTEM_PROMPT
    ok = FakeClient([resp([block("text", text=f"A 1% boost is unusually small ({TYPICAL_BOOSTS}); can you confirm "
                                                "the exact percent in the app?")], "end_turn")])
    assert ChatSession(ok, toolbox=toolbox(), clock=lambda: NOW).send("1% boost on dk").untraced == []
    bad = FakeClient([resp([block("text", text="Is it 1% or something like 10%/15%?")], "end_turn")])
    assert ChatSession(bad, toolbox=toolbox(), clock=lambda: NOW).send("1% boost on dk").untraced == ["10%", "15%"]


# ------------------------------------------------------------- comparing offers

A_TWO_50S = {"name": "Two 50% boosts", "boosts": [{"book": "fanduel", "boost_percent": 50, "max_wager": 300},
                                                  {"book": "fanduel", "boost_percent": 50, "max_wager": 300}]}
B_ONE_100 = {"name": "One 100% boost", "boosts": [{"book": "fanduel", "boost_percent": 100, "max_wager": 250}]}


def test_compare_offers_tool_ranks_and_shows_the_winner():
    tb = toolbox()
    out = tb.run("compare_promo_options", {"options": [A_TWO_50S, B_ONE_100], "max_cash": 1000,
                                           "books": ["fanduel", "draftkings"], "sports": ["nfl", "mlb"]})
    cmp = tb.display_data["comparison"]
    best, second = cmp["rows"]
    assert best["best"] and best["locked_profit"] >= second["locked_profit"]
    margin = round(best["locked_profit"] - second["locked_profit"], 2)
    assert f"${margin:,.2f} more than {second['name']}" in out          # the difference comes from Python
    assert "## Which offer is worth more with up to $1,000.00 of cash" in out and "### Plan for" in out
    assert all(r["cash_required"] <= 1000 for r in cmp["rows"])
    assert tb.display_data["sections"]                                  # the winner's slip, as data


def test_compare_offers_with_a_bonus_bet_and_checks():
    tb = toolbox()
    out = tb.run("compare_promo_options", {"options": [
        B_ONE_100, {"name": "$100 bonus", "bonus_bets": [{"book": "fanduel", "amount": 100}]}],
        "max_cash": 0, "sports": ["nfl"]})
    rows = {r["name"]: r for r in tb.display_data["comparison"]["rows"]}
    assert rows["$100 bonus"]["cash_required"] == 0 and rows["$100 bonus"]["locked_profit"] > 0
    assert "SHOWN" in out or "shown to the user" in out
    assert tb.run("compare_promo_options", {"options": [B_ONE_100]}).startswith("CHECK")
    assert tb.run("compare_promo_options", {"options": [B_ONE_100, {"name": "?"}]}).startswith("CHECK")


def test_prompt_routes_offer_choices_to_compare_tool():
    assert "call compare_promo_options ONCE" in SYSTEM_PROMPT and "$300 each or total" in SYSTEM_PROMPT


# ------------------------------------------------------------- grid size on request

def test_bonus_grid_size_on_request_and_default_unchanged():
    tb = toolbox()
    base = {"book": "fanduel", "amount": 1000, "max_cash": 0}
    tb.run("plan_bonus_bet", base)
    default_tickets = len(tb.last_grid.tickets)
    out = tb.run("plan_bonus_bet", {**base, "tickets": 8})
    assert "Grid sized as asked (8 bets)" in out and len(tb.last_grid.tickets) == 8
    assert len(tb.display_data["sections"][0]["tickets"]) == 8
    assert any("more" in a and "(most profitable)" in a for a in tb.display_data["alternatives"])
    tb.run("plan_bonus_bet", base)                       # asking again without a size: back to the best
    assert len(tb.last_grid.tickets) == default_tickets
    out = tb.run("plan_bonus_bet", {**base, "tickets": 10})
    assert out.split("\n")[-1].startswith("NO GRID of 10 bets") or "NO GRID of 10 bets" in out
    assert "Sizes available:" in out and tb.display is None


def test_prompt_lets_the_user_pick_grid_size():
    assert "Never say the grid size can't be changed" in SYSTEM_PROMPT and "max_tickets" in SYSTEM_PROMPT


def test_default_bonus_answer_offers_fewer_bets_only_by_default():
    tb = toolbox()
    tb.run("plan_bonus_bet", {"book": "fanduel", "amount": 1000, "max_cash": 0})
    assert tb.display_data["alternatives"][0].startswith("Short on time? ")
    assert "Just ask for either." in tb.display
    tb.run("plan_bonus_bet", {"book": "fanduel", "amount": 1000, "max_cash": 0, "tickets": 8})
    assert not any(a.startswith("Short on time?") for a in tb.display_data["alternatives"])
