"""The conversation loop: send the user's message, run the tools Claude asks for, return its reply.

A manual loop (not the SDK tool runner) because the chat keeps one history across many turns,
counts the cost of every request against a spending cap, and must run offline in tests with a
fake client.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime

from .prompt import SYSTEM_PROMPT
from .tools import TOOLS, Toolbox

MODEL = "claude-opus-5"
MAX_TOKENS = 16000
MAX_TOOL_ROUNDS = 8

# $ per million tokens: (input, output). Cache writes bill at 1.25x input, reads at 0.1x.
PRICES = {"claude-opus-5": (5.00, 25.00), "claude-opus-4-8": (5.00, 25.00),
          "claude-sonnet-5": (2.00, 10.00)}


class SpendCapReached(RuntimeError):
    pass


class Stopped(Exception):
    """The user pressed stop; the turn was rolled back."""


TOOL_STATUS = {
    "plan_bonus_bet": "Pulling live odds and building your bonus slip…",
    "plan_profit_boost": "Pulling live odds and pricing your boost…",
    "plan_profit_boosts": "Pulling live odds and fitting your boosts together…",
    "compare_promo_options": "Pulling live odds and comparing your offers…",
    "resolve_partial_grid": "Re-solving your grid…",
    "resize_boost": "Re-sizing the hedge for the odds you see…",
    "check_bonus_math": "Checking the math…",
}


@dataclass
class Reply:
    text: str
    cost: float          # this turn
    total_cost: float    # whole session
    tool_calls: list[str] = field(default_factory=list)
    untraced: list[str] = field(default_factory=list)  # numbers in the reply no tool or user gave
    slip: str | None = None  # the tool's slip for this turn, printed verbatim after `text`
    slip_data: dict | None = None  # the same slip as data (web UI tables)


_NUMBER = re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?|(?<![\w.])[+-]\d{3,5}\b|\b\d+(?:\.\d+)?%")
# a plain number the user typed ("600 bucks"), not a percentage or part of odds/dollars
_BARE = re.compile(r"(?<![\w.$+\-,])\d[\d,]*(?:\.\d+)?(?![\d,.]*\s*%)")


def _norm(tok: str) -> str:
    t = tok.replace(" ", "").replace(",", "").rstrip(".")
    if "." in t and not t.endswith("%"):
        t = t.rstrip("0").rstrip(".")
    return t


def untraced_numbers(reply: str, sources: list[str]) -> list[str]:
    """Dollar amounts, American odds and percentages in `reply` that appear in none of `sources`
    (tool outputs and the user's own messages). Enforces 'the LLM never does math'."""
    known = {_norm(t) for src in sources for t in _NUMBER.findall(src)}
    known |= {_norm("$" + t) for src in sources for t in _BARE.findall(src)}
    return [t.rstrip(",.") for t in _NUMBER.findall(reply) if _norm(t) not in known]


# Odds/percent defaults documented in the tool descriptions (e.g. "-200"), which the model may
# quote before any tool runs. Only _NUMBER-style tokens, never bare example numbers.
_TOOL_DEFAULTS = " ".join(_NUMBER.findall(json.dumps(TOOLS)))


def request_cost(usage, model: str) -> float:
    inp, out = PRICES.get(model, PRICES[MODEL])
    g = lambda k: getattr(usage, k, 0) or 0
    return (g("input_tokens") * inp + g("cache_creation_input_tokens") * inp * 1.25
            + g("cache_read_input_tokens") * inp * 0.1 + g("output_tokens") * out) / 1e6


class ChatSession:
    def __init__(self, client=None, *, model: str = MODEL, toolbox: Toolbox | None = None,
                 max_spend: float = 1.00, clock=datetime.now):
        if client is None:
            import anthropic
            from ..data.env import load_env
            load_env()
            client = anthropic.Anthropic()
        self.client = client
        self.model = model
        self.toolbox = toolbox or Toolbox()
        self.max_spend = max_spend
        self.clock = clock
        self.messages: list[dict] = []
        self.total_cost = 0.0
        # tool outputs, user messages and the tools' documented defaults (e.g. -200), for untraced_numbers
        self.sources: list[str] = [_TOOL_DEFAULTS]

    def _params(self) -> dict:
        return dict(
            model=self.model,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            tools=TOOLS,  # tool inputs are tiny, so no eager input streaming: the API validates them
            messages=self.messages,
            thinking={"type": "adaptive"},
            cache_control={"type": "ephemeral"},   # caches system + tools + history prefix
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",                     # re-run on another model if Opus 5 declines
        )

    def _create(self, on_event=None, should_stop=None):
        if self.total_cost >= self.max_spend:
            raise SpendCapReached(f"This session has spent ${self.total_cost:.2f} (cap ${self.max_spend:.2f}).")
        if on_event is None:
            return self.client.beta.messages.create(**self._params())
        with self.client.beta.messages.stream(**self._params()) as stream:
            for event in stream:
                if should_stop and should_stop():
                    # Anthropic bills what was used before the stop: count the input side, which the
                    # API reports when the reply starts (the few output tokens so far go uncounted).
                    snap = getattr(stream, "current_message_snapshot", None)
                    if snap is not None and getattr(snap, "usage", None) is not None:
                        self.total_cost += request_cost(snap.usage, self.model)
                    raise Stopped()
                if getattr(event, "type", None) == "text":
                    on_event({"type": "text", "delta": event.text})
            return stream.get_final_message()

    def send(self, text: str, on_event=None, should_stop=None) -> Reply:
        """One user turn. With on_event, Claude's text streams as {"type": "text", "delta"} events
        and tool runs send {"type": "status", "text"}. should_stop() is checked throughout; if it
        returns True, or anything fails, the whole turn is rolled back so the history stays valid."""
        start, src_start, grid = len(self.messages), len(self.sources), self.toolbox.last_grid
        try:
            return self._turn(text, on_event, should_stop)
        except Stopped:
            # Keep what the user asked (so "actually make it $300" still makes sense), drop the
            # partial answer and any unfinished tool steps (a tool request with no result would
            # make the next API call fail). Odds already pulled stay in the toolbox cache.
            user_msg = self.messages[start]
            del self.messages[start:]
            del self.sources[src_start + 1:]
            self.toolbox.last_grid = grid
            self.messages.append({"role": "user", "content": user_msg["content"] +
                                  "\n[The user stopped this reply before it finished. Nothing from it "
                                  "was shown to them.]"})
            raise
        except BaseException:
            del self.messages[start:]
            del self.sources[src_start:]
            self.toolbox.last_grid = grid
            raise

    def _turn(self, text, on_event, should_stop) -> Reply:
        now = self.clock().astimezone().strftime("%a %b %d %Y, %I:%M %p %Z")
        start = len(self.messages)
        self.sources.append(text)
        self.messages.append({"role": "user", "content": f"[Current time: {now}]\n{text}"})
        cost, calls, said = 0.0, [], []
        self.toolbox.display = self.toolbox.display_data = None
        emit = on_event or (lambda e: None)
        for _ in range(MAX_TOOL_ROUNDS + 1):
            if should_stop and should_stop():
                raise Stopped()
            resp = self._create(on_event, should_stop)
            c = request_cost(resp.usage, getattr(resp, "model", self.model) or self.model)
            cost += c
            self.total_cost += c
            if resp.stop_reason == "refusal":
                del self.messages[start:]  # roll back the whole declined turn so the chat can continue
                return Reply("Sorry, I can't help with that one. I can help you plan a profit boost "
                             "or a bonus bet.", cost, self.total_cost, calls)
            # keep the full content (thinking and tool_use blocks) so the next request is valid
            self.messages.append({"role": "assistant", "content": resp.content})
            said += [b.text for b in resp.content if getattr(b, "type", None) == "text"]
            if resp.stop_reason == "tool_use":
                results = []
                for block in resp.content:
                    if getattr(block, "type", None) != "tool_use":
                        continue
                    calls.append(block.name)
                    emit({"type": "status", "text": TOOL_STATUS.get(block.name, "Working…")})
                    try:
                        out = self.toolbox.run(block.name, dict(block.input))
                        results.append({"type": "tool_result", "tool_use_id": block.id, "content": out})
                        self.sources.append(out)
                    except Exception as e:  # report to Claude instead of crashing the chat
                        results.append({"type": "tool_result", "tool_use_id": block.id,
                                        "content": f"Tool error: {e}", "is_error": True})
                self.messages.append({"role": "user", "content": results})  # all results in one message
                emit({"type": "status", "text": "Writing it up…"})
                continue
            if resp.stop_reason == "pause_turn":
                continue
            text_out = "\n\n".join(t.strip() for t in said if t.strip())
            if resp.stop_reason == "max_tokens":
                text_out += "\n\n(Reply cut off at the length limit.)"
            return Reply(text_out.strip(), cost, self.total_cost, calls,
                         untraced_numbers(text_out, self.sources), self.toolbox.display,
                         self.toolbox.display_data)
        return Reply("I got stuck calling tools. Try rephrasing, or give me the details again.",
                     cost, self.total_cost, calls)

    def reset(self) -> None:
        self.messages.clear()
        self.sources[:] = [_TOOL_DEFAULTS]
        self.toolbox.last_grid = None
        self.toolbox.last_boost = self.toolbox.boost_roster = None
