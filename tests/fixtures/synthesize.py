"""Make the test fixtures synthetic.

The fixtures keep the exact shape of real Odds API responses (events, teams, start times, books, market keys,
lines), but every price is generated here from a simple model, so no third-party odds data is redistributed.

Model, per game (seeded by the event id, so it is deterministic):
- final margin ~ Normal(mu, sd) and total ~ Normal(T, st), with sport-specific sizes;
- each line's fair probability comes from that model (P(team margin + handicap > 0), P(total > line)),
  so alternate-line ladders stay consistent; moneylines include a draw where the market has one;
- each book adds its own vig (Pinnacle less) plus a little noise, so best prices differ across books; every
  market keeps the book's full margin (real books never post near-zero-hold markets).

Run once from the repo root: python tests/fixtures/synthesize.py   (rewrites the .json files in place).
"""
import hashlib
import json
import math
import random
from pathlib import Path

HERE = Path(__file__).parent
SPORT = {  # margin sd, mean total, total sd
    "americanfootball_nfl": (13.0, 45.0, 10.0), "americanfootball_ncaaf": (16.0, 54.0, 12.0),
    "baseball_mlb": (4.2, 8.6, 3.2), "soccer_epl": (1.6, 2.8, 1.5), "icehockey_nhl": (2.3, 6.0, 2.0),
}


def phi(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def rng(*parts):
    return random.Random(int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:16], 16))


CAP = 0.99   # largest implied probability posted (about -10000)


def to_price(q, american):
    q = min(max(q, 0.004), CAP)
    d = 1 / q
    if not american:
        return round(d, 2)
    return int(round((d - 1) * 100)) if d >= 2 else int(-round(100 / (d - 1)))


def fair_probs(ev, mk, outcomes, model, player_means):
    """Fair probability for each outcome of one market at one line."""
    sd, mu, total, st = model
    home = ev["home_team"]
    key = mk["key"]
    names = [o["name"] for o in outcomes]
    if key.startswith("h2h"):
        p_home = phi(mu / sd)
        if "Draw" in names:
            draw = max(0.12, 0.27 - 0.08 * abs(mu) / sd)
            ph, pa = p_home * (1 - draw), (1 - p_home) * (1 - draw)
            return [draw if n == "Draw" else (ph if n == home else pa) for n in names]
        return [p_home if n == home else 1 - p_home for n in names]
    if "spreads" in key:
        return [phi(((mu if o["name"] == home else -mu) + o["point"]) / sd) for o in outcomes]
    if "totals" in key:
        return [1 - phi((o["point"] - total) / st) if o["name"] == "Over" else phi((o["point"] - total) / st)
                for o in outcomes]
    if key.startswith(("player_", "batter_", "pitcher_")):
        out = []
        for o in outcomes:
            m = player_means.setdefault((ev["id"], key, o.get("description")),
                                        o["point"] * (1 + rng(ev["id"], key, o.get("description")).uniform(-0.12, 0.12)))
            p_over = 1 - phi((o["point"] - m) / (0.25 * m + 1))
            out.append(p_over if o["name"] == "Over" else 1 - p_over)
        return out
    return [1 / len(outcomes)] * len(outcomes)


def synthesize(path: Path):
    data = json.loads(path.read_text())
    events = data if isinstance(data, list) else [data]
    american = path.stem.endswith("__american")   # not "americanfootball"
    player_means = {}
    for ev in events:
        sd, T, st = SPORT.get(ev.get("sport_key", ""), (10.0, 40.0, 8.0))
        r = rng("game", ev["id"])
        spread = 1.0 if ev.get("sport_key", "").startswith("soccer") else 0.55   # soccer: clear favorites are common
        model = (sd, r.uniform(-spread, spread) * sd, T * r.uniform(0.85, 1.15), st)
        for bm in ev.get("bookmakers", []):
            rb = rng("book", ev["id"], bm["key"])
            vig = rb.uniform(0.015, 0.025) if bm["key"] == "pinnacle" else rb.uniform(0.03, 0.06)
            for mk in bm.get("markets", []):
                outs = mk["outcomes"]
                if len(outs) == 3:   # 1X2 / regulation 3-way markets carry more margin (about 5-8% at US books)
                    vig = rb.uniform(0.025, 0.035) if bm["key"] == "pinnacle" else rb.uniform(0.055, 0.08)
                fair = fair_probs(ev, mk, outs, model, player_means)
                # one API market can hold many lines (every alternate spread, every player's prop line), so the
                # margin is applied per line: each Over/Under or team/team pair (or the whole 1X2) is one group
                groups = {}
                for i, o in enumerate(outs):
                    if mk["key"].startswith("h2h"):
                        g = "all"
                    elif "spreads" in mk["key"]:
                        # "A -3.5 / B +3.5" and "B -3.5 / A +3.5" are different pairs at the same number
                        teams = {ev["home_team"], ev["away_team"]}
                        other = next(iter(teams - {o["name"]}), o["name"])
                        g = (o["name"] if o["point"] < 0 else other if o["point"] > 0 else "pk", abs(o["point"]))
                    else:
                        g = (o.get("description"), o.get("point"))
                    groups.setdefault(g, []).append(i)
                for idx in groups.values():
                    # noise moves price between outcomes, then the book's full margin is restored: real books
                    # never post (near) zero-hold markets, and the grid/hedge math is very sensitive to hold
                    q = {i: max(fair[i] + rng("px", ev["id"], bm["key"], mk["key"], outs[i]["name"],
                                              outs[i].get("point"), outs[i].get("description")).gauss(0, 0.006), 0.002)
                         for i in idx}
                    scale = (1 + vig) / sum(q.values()) if len(idx) > 1 else 1 + vig
                    q = {i: v * scale for i, v in q.items()}
                    top = max(q, key=q.get)
                    if len(idx) > 1 and q[top] > CAP:   # very lopsided line: cap the favorite (about -10000) and put
                        extra = q[top] - CAP            # the excess margin on the longshot side, as books do
                        q[top] = CAP
                        for i in idx:
                            if i != top:
                                q[i] += extra / (len(idx) - 1)
                    for i in idx:
                        outs[i]["price"] = to_price(q[i], american)
    path.write_text(json.dumps(data, indent=1))


if __name__ == "__main__":
    for f in sorted(HERE.glob("*.json")):
        synthesize(f)
        print("synthesized", f.name)
