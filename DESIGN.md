# Design notes

## 1. The model talks; Python computes

An LLM that does arithmetic on money will eventually be wrong, and a bet slip with one wrong stake can turn a
lock into a loss. So the rule is absolute: **every number a user sees comes from a Python function.**

- The assistant (Claude, manual tool loop) only parses the request and calls tools such as `plan_bonus_bet`,
  `plan_profit_boosts` and `compare_promo_options`. Tools return finished text and structured data.
- **Untraced-number guard.** After each reply, every `$` amount, American odds and percentage in the text is
  checked against the tool outputs and the user's own messages. Anything the model introduced is flagged.
  When the model needed a sanctioned phrase with numbers ("boosts are usually more like 25%, 50% or 100%"),
  the phrase lives in a tool description so the guard can see it.
- **Slips are never re-typed.** The slip is printed verbatim from the tool and rendered in the web UI from
  structured data (`Slip.to_dict()`), so text and tables can't disagree. The model is told not to reproduce it:
  copying a 16-ticket grid cost ~4× per reply and risked a transcription error the guard can't catch (a swapped
  stake still "appears" in the tool output).
- **Ask before assuming.** The prompt makes the model ask for the book, max wager, cash and timeframe in one
  message, and never invent a cash limit. Unusual inputs (a 1% or 2000% boost) are confirmed first.

## 2. Bonus-bet grids

A bonus bet's stake isn't returned, so arbitrage calculators overstate its value. The single-book grid covers
every combination of k independent markets with parlays, staking each ticket ∝ 1/(D−1) so every outcome returns
the same amount; the conversion is `1 / Σ 1/(D−1)`.

- **Market selection.** Each market is scored as a grid leg; a shortlist keeps the best 2-way and 3-way legs
  separately (3-way soccer/hockey legs were being crowded out), then all leg combinations are searched exactly.
- **Cent rounding that sums exactly** to the bonus, legs in different games, half-point lines only (whole numbers
  push, quarter lines half-push), and a warning with the worst payout if one leg is postponed.
- **Grid size on request.** The most profitable grid can be 24–32 tickets; users can ask for "8 bets" and see
  what it costs. Sizes that lock *less* than a smaller grid are never offered.
- **Partial placement.** If prices move mid-placement, the unplaced tickets are re-sized around what was
  actually placed.

## 3. Boosts

Boosted odds are `1 + (d−1)(1+b)`; a lock exists iff `1/d_boosted + Σ 1/d_hedge < 1`. The multi-boost planner
fills each outcome best-price-first (a boost up to its max wager, then cash) and raises the payout while
`1 − Σ 1/d` stays positive, then searches every assignment of boosts to (game, side). Real apps sometimes show a
slightly different boosted price; a re-size tool re-solves around the price the app shows and refuses gaps over
2% as likely typos.

## 4. Live data

- **Query-driven pulls** from The Odds API with credit estimates first; American odds (the decimal feed rounds to
  2 places, which once mis-sized a grid by $0.22); malformed prices skip one line, never the whole pull.
- **Started games are always skipped**, and games within 30 minutes of start too.
- Book quirks are encoded where they matter (e.g. FanDuel displays parlay odds rounded down but pays the exact
  product).

## 5. Lessons from the arbitrage research

- **Rule matching matters more than math.** A "+10% arb" between an exchange's first-5-innings market (3-way, tie
  included) and a sportsbook's (2-way, tie refunded) was fake. Overtime, pushes, ties, retirements and players
  who sit all settle differently across venues, and every pairing has to encode them.
- **Distrust big numbers.** A "+50% arb" came from parsing exchange questions that name both teams; the fix was to
  read the subject of the question, use the live order book, and skip crossed books. Anything over ~2% is
  treated as a bug until proven otherwise.
- **Price without size is meaningless.** Many exchange quotes had a few dollars behind them; results now report
  how much can actually be bet.
- **An optimizer beats pairwise checks.** The best lock found needed three bets and a whole-number push refund —
  invisible to a "best Over vs best Under" scan.

## 6. Web app

FastAPI with server-sent events: streamed text, tool-status events, and a stop button. Stopping keeps the user's
message, drops the partial answer and any unfinished tool call (a tool call with no result would break the next
request), and keeps odds already fetched. Pages and assets are sent `no-cache` with versioned asset URLs, after a
stale cached script made a new feature look broken.
