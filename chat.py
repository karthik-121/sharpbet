"""Terminal chat with sharpbet.

    python chat.py                 # $1.00 spending cap per session
    python chat.py --max-spend 2   # raise the cap

Needs ANTHROPIC_API_KEY and ODDS_API_KEY in .env. Commands: /cost, /reset, /quit.
"""
from __future__ import annotations

import argparse
import os
import sys

from sharpbet.assistant import ChatSession, SpendCapReached
from sharpbet.data import load_env

BANNER = """sharpbet: exact bet slips for profit boosts and bonus bets.
Tell me what you've got, e.g. "50% boost on DraftKings, max wager $250" or "$200 bonus bet on FanDuel".
21+ only. No result is guaranteed. Gambling problem? Call 1-800-GAMBLER.
Commands: /cost  /reset  /quit
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-spend", type=float, default=1.00, help="stop after this many dollars of API use")
    a = ap.parse_args()
    load_env()
    missing = [k for k in ("ANTHROPIC_API_KEY", "ODDS_API_KEY") if not os.environ.get(k)]
    if missing:
        print(f"Missing {', '.join(missing)} in .env (see .env.example).", file=sys.stderr)
        return 1
    try:
        session = ChatSession(max_spend=a.max_spend)
    except Exception as e:
        print(f"Couldn't start: {e}\nCheck ANTHROPIC_API_KEY in .env.", file=sys.stderr)
        return 1
    print(BANNER)
    while True:
        try:
            text = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not text:
            continue
        if text in ("/quit", "/exit"):
            break
        if text == "/cost":
            from sharpbet.assistant import tools
            odds_left = getattr(tools._API, "remaining", None)
            print(f"Anthropic: ${session.total_cost:.3f} of your ${session.max_spend:.2f} session cap used "
                  "(balance: console.anthropic.com → Billing).")
            print(f"Odds API: {odds_left if odds_left is not None else 'no pull yet this session'} "
                  "credits left this month.\n")
            continue
        if text == "/reset":
            session.reset()
            print("Started a new conversation.\n")
            continue
        try:
            reply = session.send(text)
        except SpendCapReached as e:
            print(f"{e} Restart with --max-spend to continue.")
            break
        except Exception as e:
            print(f"Something went wrong: {e}\n")
            continue
        print(f"\nsharpbet> {reply.text}\n")
        if reply.slip:
            print(reply.slip + "\n")
        if reply.untraced:
            print(f"  WARNING: these numbers didn't come from a tool: {', '.join(reply.untraced)}. "
                  "Don't rely on them; ask again.\n")
        print(f"  (${reply.cost:.3f} this reply, ${reply.total_cost:.3f} this session)\n")
    print(f"Session cost: ${session.total_cost:.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
