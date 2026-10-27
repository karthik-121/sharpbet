"""Demo mode for UI work: real slips from saved fixture prices, with a scripted stand-in for
Claude. Costs nothing (no Anthropic or Odds API calls). Not for real bets: prices are old.

    SHARPBET_DEMO=1 python web/server.py
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from sharpbet.assistant import ChatSession, Toolbox
from sharpbet.data import parse_events

FIX = Path(__file__).resolve().parents[1] / "tests" / "fixtures"
NOW = datetime(2026, 9, 27, 1, 55, tzinfo=timezone.utc)  # when the fixtures were pulled
FILES = {"nfl": ("americanfootball_nfl__h2h-spreads-totals.json", "decimal"),
         "mlb": ("baseball_mlb__h2h-spreads-totals__american.json", "american"),
         "ncaaf": ("americanfootball_ncaaf__h2h-spreads-totals.json", "decimal"),
         "epl": ("soccer_epl__h2h-spreads-totals.json", "decimal")}


def fixture_source(sport, books):
    f, fmt = FILES.get(sport, (None, None))
    if not f:
        return []
    return parse_events(json.loads((FIX / f).read_text()), odds_format=fmt, now=NOW)


def _b(type_, **kw):
    return SimpleNamespace(type=type_, **kw)


def _resp(content, stop):
    return SimpleNamespace(content=content, stop_reason=stop, model="claude-opus-5",
                           usage=SimpleNamespace(input_tokens=0, output_tokens=0,
                                                 cache_creation_input_tokens=0, cache_read_input_tokens=0))


class ScriptedClient:
    """Picks a tool from keywords in the last user message, then writes a short lead that quotes
    the tool's own numbers, the way the real model is instructed to."""

    def __init__(self, pace: float = 0.03):
        self.pace = pace
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create, stream=self._stream))

    def _stream(self, **kw):
        return FakeStream(self._create(**kw), self.pace)

    def _create(self, **kw):
        last = kw["messages"][-1]["content"]
        if isinstance(last, list):  # tool result came back: write the lead
            out = last[0]["content"]
            best = re.search(r"\*\*(Best: [^*]+)\*\*", out)
            if best:
                return _resp([_b("text", text=f"Demo mode (sample prices, not live). {best.group(1)} The comparison "
                                               "and the winning slip are below.")], "end_turn")
            m = re.search(r"Locks (\$[\d,.]+)", out)
            scan = re.search(r"Games scanned: ([^(.]+)", out)
            cash = re.search(r"(No cash needed|Cash to put down: \$[\d,]+\.\d\d)", out)
            lead = (f"Demo mode (sample prices, not live). Looking at games "
                    f"{scan.group(1).strip() if scan else ''}: best lock is **{m.group(1)}**"
                    f"{' (' + cash.group(1).lower() + ')' if cash else ''}. The full slip is below; place "
                    "every bet in one sitting, and stop and re-run if any price in the app differs. Want me "
                    "to re-run with different books, cash or dates?" if m else "Demo mode: nothing locks here.")
            return _resp([_b("text", text=lead)], "end_turn")
        text = last.lower()
        if "which" in text or " or " in text:
            fd = lambda pct, cap: {"book": "fanduel", "boost_percent": pct, "max_wager": cap}
            args = {"options": [{"name": "Two 50% boosts", "boosts": [fd(50, 300), fd(50, 300)]},
                                {"name": "One 100% boost", "boosts": [fd(100, 250)]}],
                    "max_cash": 1000, "books": ["fanduel", "draftkings"], "sports": ["nfl", "mlb"]}
            return _resp([_b("tool_use", id="demo1", name="compare_promo_options", input=args)], "tool_use")
        if "boosts" in text:
            args = {"boosts": [
                {"book": "fanduel", "boost_percent": 50, "max_wager": 200, "sports": ["nfl"]},
                {"book": "fanduel", "boost_percent": 50, "max_wager": 200, "sports": ["nfl"]},
                {"book": "fanduel", "boost_percent": 100, "max_wager": 100, "sports": ["nfl"]},
                {"book": "fanduel", "boost_percent": 50, "max_wager": 200, "sports": ["nfl"], "slot": "snf"}],
                "max_cash": 10000, "books": ["fanduel"], "sports": ["nfl"], "within_days": 1}
            return _resp([_b("tool_use", id="demo1", name="plan_profit_boosts", input=args)], "tool_use")
        if "boost" in text:
            args = {"book": "draftkings" if "draft" in text or "dk" in text else "fanduel", "boost_percent": 50,
                    "max_wager": 250, "max_cash": 600, "books": ["fanduel", "draftkings"], "within_days": 2}
            return _resp([_b("tool_use", id="demo1", name="plan_profit_boost", input=args)], "tool_use")
        if "bonus" in text:
            args = {"book": "fanduel", "amount": 200, "max_cash": 0, "within_days": 2}
            return _resp([_b("tool_use", id="demo1", name="plan_bonus_bet", input=args)], "tool_use")
        if "placed" in text:
            return _resp([_b("tool_use", id="demo1", name="resolve_partial_grid",
                             input={"placed": [1, 2, 3, 4], "moved": [{"ticket": 5, "odds": 1300}]})], "tool_use")
        return _resp([_b("text", text="Demo mode: try **a 50% boost on DraftKings** or **a $200 bonus bet**.")],
                     "end_turn")


class FakeStream:
    """Stands in for client.beta.messages.stream(): yields text events word by word, then the
    final message."""

    def __init__(self, message, pace: float = 0.0):
        self.message, self.pace = message, pace

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        for block in self.message.content:
            if getattr(block, "type", None) == "text":
                for word in re.findall(r"\S+\s*", block.text):
                    if self.pace:
                        time.sleep(self.pace)
                    yield SimpleNamespace(type="text", text=word)

    def get_final_message(self):
        return self.message


def demo_session() -> ChatSession:
    return ChatSession(ScriptedClient(), toolbox=Toolbox(market_source=fixture_source, now_fn=lambda: NOW),
                       clock=lambda: NOW)
