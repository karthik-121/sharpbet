# Arbitrage research

Can an *arbitrage* (bets at several venues that profit on every outcome) be found with public data, and where?
These scripts answer that with a linear program instead of pairwise price checks.

## Method

For one game, enumerate **every possible final outcome**: each final margin, each total, or for tennis every
best-of-3 score line (each set 6–0 … 7–6, two or three sets: 1,470 lines). Every available bet becomes a payout
vector over those outcomes, using its venue's own settlement rules:

- sportsbooks: whole-number lines push (refund), an NFL moneyline tie refunds, a 2-way first-5-innings moneyline
  refunds a tie;
- Kalshi: yes/no contracts, a tie resolves No, taker fee `0.07 · P · (1−P)` per contract;
- Polymarket US: yes/no contracts, a moneyline tie settles at $0.50, fee `feeCoefficient · P · (1−P)`.

The LP chooses stakes to **maximize the guaranteed return** (the worst outcome) with total stake = 1. A positive
optimum is an arbitrage; the solution is the bet slip. Results also report how many dollars are available at
each exchange price.

| Script | Scope | Odds API credits |
|---|---|---|
| `mlb_slate_lp.py [dry] [reuse] [YYYY-MM-DD]` | Every MLB game that day: run differential, total, team totals, first 5 innings, player props, run in the 1st | ~25 per game |
| `tennis_kp_lp.py` | Kalshi vs Polymarket on every tennis match in the next 16 hours | 0 |
| `wnba_lp.py [dry] [YYYY-MM-DD]` | WNBA final margin, first-half margin and total | ~5 per game |

Install with `pip install -e '.[research]'`; set `ODDS_API_KEY` in `.env`. Kalshi and Polymarket market data is
public and free. No collected data is included in this repo.

## Findings (Sept 2026, measured snapshots)

- **Real arbs exist but are small: about 0.2%–2%.** They were mostly **sportsbook vs sportsbook player props**
  (e.g. RBIs, hits + runs + RBIs) and **far alternate totals** at slower books. Main lines on big games were priced
  within a few percent everywhere.
- **The optimizer found what pairwise checks miss**, e.g. a three-bet lock on a game total that relied on a
  whole-number line refunding on a push.
- **Arbs churn.** Of 7 arbs on one MLB slate, 1 survived 3.5 hours later while 4 new ones appeared.
- **Exchanges added less than expected.** Kalshi and Polymarket priced games close to each other and to the
  books; many attractive exchange quotes had only a few dollars behind them. Their bigger value is as a *hedge*
  venue: in a test, letting a single-sportsbook user hedge a 50% boost on an exchange raised the lock 19–48%.
- **A sharp reference book added no arbs.** Pinnacle, used only as a reference here, priced mostly main lines and
  priced them accurately.
- **Bugs look like opportunities.** The two largest "arbs" (+10% and +50%) were a settlement-rule mismatch and a
  parsing error. See [DESIGN.md](../DESIGN.md#5-lessons-from-the-arbitrage-research).

Arbitrage edges like these shrink quickly as sportsbooks limit accounts; this is research, not betting advice.
