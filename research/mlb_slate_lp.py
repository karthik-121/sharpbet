"""MLB slate: 8 sportsbooks + Kalshi + Polymarket US, all-combinations LP per game and per dimension
(run differential, total, team totals, first 5 margin/total, player props, run in the 1st inning).

Rules encoded: MLB games can't tie (extra innings); books' F5 moneyline refunds a tie unless the book lists a
Draw; book pushes (whole lines) refund. Kalshi F5 winner is 3-way (Tie contract); Polymarket F5 winner settles
No on a tie. Props assume the player plays: if scratched, books void, Kalshi/Polymarket settle at a fair market
price (small residual risk; flagged). Fees: Kalshi 0.07*P*(1-P); Polymarket market feeCoefficient*P*(1-P).
Usage: python research/mlb_slate_lp.py [dry] [YYYY-MM-DD]   (dry = free calls only, prints the credit estimate)."""
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from scipy.optimize import linprog

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sharpbet.data.env import load_env  # noqa: E402

load_env()
KEY = os.environ["ODDS_API_KEY"]
DRY = "dry" in sys.argv
REUSE = "reuse" in sys.argv   # re-score saved sportsbook + Kalshi data (no credits); Polymarket re-read live
DAY = next((a for a in sys.argv[1:] if re.match(r"\d{4}-\d{2}-\d{2}$", a)),
           (date.today() + timedelta(days=1)).isoformat())
OUT = Path(__file__).parent / f"mlb_{DAY}"
BOOKS = "fanduel,draftkings,betmgm,williamhill_us,betrivers,espnbet,fanatics,hardrockbet"
OA = "https://api.the-odds-api.com/v4"
K = "https://external-api.kalshi.com/trade-api/v2"
PM = "https://gateway.polymarket.us/v1"
KF = 0.07
GAME_MARKETS = ["alternate_spreads", "alternate_totals", "team_totals", "alternate_team_totals",
                "h2h_1st_5_innings", "spreads_1st_5_innings", "totals_1st_5_innings"]
PROPS = {  # odds api key (main, alternate) -> (Kalshi series, Polymarket type)
    "pitcher_strikeouts": ("KXMLBKS", "baseball_player_strikeouts"),
    "pitcher_hits_allowed": ("KXMLBHA", "baseball_player_hits_allowed"),
    "pitcher_earned_runs": ("KXMLBERA", "baseball_player_earned_runs_allowed"),
    "pitcher_outs": (None, "baseball_player_outs"),
    "batter_hits": ("KXMLBHIT", "baseball_player_hits"),
    "batter_total_bases": ("KXMLBTB", "baseball_player_total_bases"),
    "batter_home_runs": ("KXMLBHR", "baseball_player_home_runs"),
    "batter_rbis": ("KXMLBRBI", "baseball_player_rbis"),
    "batter_hits_runs_rbis": ("KXMLBHRR", "baseball_player_hits_runs_rbis"),
    "batter_stolen_bases": ("KXMLBSB", "baseball_player_stolen_bases"),
}
NO_ALT = {"pitcher_outs", "batter_stolen_bases"}
PROP_KEYS = [k for p in PROPS for k in ([p] if p in NO_ALT else [p, p + "_alternate"])]
TEAMS = {  # code -> Odds API full name (Kalshi and Polymarket use these codes)
    "ARI": "Arizona Diamondbacks", "AZ": "Arizona Diamondbacks", "ATL": "Atlanta Braves", "BAL": "Baltimore Orioles",
    "BOS": "Boston Red Sox", "CHC": "Chicago Cubs", "CWS": "Chicago White Sox", "CHW": "Chicago White Sox",
    "CIN": "Cincinnati Reds", "CLE": "Cleveland Guardians", "COL": "Colorado Rockies", "DET": "Detroit Tigers",
    "HOU": "Houston Astros", "KC": "Kansas City Royals", "LAA": "Los Angeles Angels", "LAD": "Los Angeles Dodgers",
    "MIA": "Miami Marlins", "MIL": "Milwaukee Brewers", "MIN": "Minnesota Twins", "NYM": "New York Mets",
    "NYY": "New York Yankees", "ATH": "Athletics", "OAK": "Athletics", "PHI": "Philadelphia Phillies",
    "PIT": "Pittsburgh Pirates", "SD": "San Diego Padres", "SF": "San Francisco Giants", "SEA": "Seattle Mariners",
    "STL": "St. Louis Cardinals", "TB": "Tampa Bay Rays", "TEX": "Texas Rangers", "TOR": "Toronto Blue Jays",
    "WSH": "Washington Nationals", "WAS": "Washington Nationals",
}
dec = lambda a: 1 + a / 100 if a > 0 else 1 + 100 / -a  # noqa: E731


def jget(url, tries=6):
    for a in range(tries):
        try:
            time.sleep(0.1)
            r = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "sharpbet-research"}), timeout=30)
            return json.load(r), r.headers
        except urllib.error.HTTPError as e:
            if e.code != 429:
                raise
            time.sleep(1.5 * (a + 1))
    raise RuntimeError("rate limited: " + url.split("?")[0])


def norm(name):
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\b(jr|sr|ii|iii)\b\.?", "", s)
    return re.sub(r"[^a-z ]", "", s).strip()


# ---------------------------------------------------------------- games
events = [e for e in jget(f"{OA}/sports/baseball_mlb/events?apiKey={KEY}")[0] if e["commence_time"][:10] == DAY
          or (datetime.fromisoformat(e["commence_time"].replace("Z", "+00:00")).astimezone().date().isoformat() == DAY)]
now = datetime.now(timezone.utc)
events = [e for e in events if datetime.fromisoformat(e["commence_time"].replace("Z", "+00:00")) > now + timedelta(minutes=10)]
est = 3 + len(events) * (len(GAME_MARKETS) + len(PROP_KEYS))
print(f"{DAY}: {len(events)} MLB games not started | max credits {est}")
for e in events:
    print("  ", e["away_team"], "@", e["home_team"], e["commence_time"])
if DRY or not events:
    sys.exit(0)
OUT.mkdir(exist_ok=True)

# ---------------------------------------------------------------- sportsbooks
if REUSE:
    saved = json.loads((OUT / "books.json").read_text())
    main, per_event = saved["main"], saved["events"]
    kal = json.loads((OUT / "kalshi.json").read_text())
    print("re-scoring saved sportsbook + Kalshi data (no credits)")
else:
  main, h = jget(f"{OA}/sports/baseball_mlb/odds?apiKey={KEY}&regions=us&markets=h2h,spreads,totals&oddsFormat=american&bookmakers={BOOKS}")
  spent = int(float(h.get("x-requests-last", 0)))
  per_event = {}
  for e in events:
      d, h = jget(f"{OA}/sports/baseball_mlb/events/{e['id']}/odds?apiKey={KEY}&regions=us&oddsFormat=american"
                  f"&bookmakers={BOOKS}&markets={','.join(GAME_MARKETS + PROP_KEYS)}")
      per_event[e["id"]] = d
      spent += int(float(h.get("x-requests-last", 0)))
  remaining = h.get("x-requests-remaining")
  (OUT / "books.json").write_text(json.dumps({"main": main, "events": per_event}))
  print(f"odds api credits spent: {spent} | remaining: {remaining} | {datetime.now():%I:%M %p}")

  # ---------------------------------------------------------------- Kalshi
  kdate = datetime.fromisoformat(DAY).strftime("%y%b").upper() + DAY[8:]   # 26SEP29
  kal = {}   # series -> list of markets (all games that day)
  for s in ["KXMLBGAME", "KXMLBSPREAD", "KXMLBTOTAL", "KXMLBTEAMTOTAL", "KXMLBF5", "KXMLBF5SPREAD", "KXMLBF5TOTAL",
            "KXMLBRFI"] + [v[0] for v in PROPS.values() if v[0]]:
      evs = [x for x in jget(f"{K}/events?series_ticker={s}&status=open&limit=200")[0].get("events", [])
             if kdate in x["event_ticker"]]
      kal[s] = [m for x in evs for m in jget(f"{K}/markets?event_ticker={x['event_ticker']}&limit=500")[0].get("markets", [])
                if m.get("status") == "active"]
  (OUT / "kalshi.json").write_text(json.dumps(kal))

# ---------------------------------------------------------------- Polymarket US
pm_events = {}
for off in range(0, 60000, 200):
    evs = jget(f"{PM}/events?closed=false&limit=200&offset={off}")[0].get("events", [])
    for x in evs:
        if (x.get("slug") or "").startswith("mlb-") and x["slug"].endswith(DAY):
            pm_events[x["slug"]] = x
    if len(evs) < 200:
        break
(OUT / "polymarket.json").write_text(json.dumps(pm_events))
print(f"Kalshi markets: {sum(len(v) for v in kal.values())} | Polymarket events: {len(pm_events)} "
      f"({sum(len(x['markets']) for x in pm_events.values())} markets)")


def pm_quotes(m):
    """(long ask, short ask, long $ available, short $ available) from the live order book, ignoring zero-size
    quotes. (The listing's bestBid/bestAsk quotes gave impossible crossed prices on 2026-09-28, so not used.)"""
    b = jget(f"{PM}/markets/{m['slug']}/book")[0]["marketData"]
    offers = [o for o in (b.get("offers") or b.get("asks") or []) if float(o.get("qty") or 0) > 0]
    bids = [o for o in (b.get("bids") or []) if float(o.get("qty") or 0) > 0]
    bo = min(offers, key=lambda o: float(o["px"]["value"]), default=None)
    bb = max(bids, key=lambda o: float(o["px"]["value"]), default=None)
    la = float(bo["px"]["value"]) if bo else None
    lb = float(bb["px"]["value"]) if bb else None
    if la is not None and lb is not None and lb > la:
        return None, None, 0, 0   # crossed or stale book: skip
    sa = (1 - lb) if lb is not None else None
    return la, sa, (float(bo["qty"]) * la if bo else 0), (float(bb["qty"]) * sa if bb else 0)


def ksz(m, yes):
    """Kalshi $ available at the ask: YES from yes_ask_size; NO fills against YES bids (yes_bid_size)."""
    if yes:
        return float(m.get("yes_ask_size_fp") or 0) * float(m.get("yes_ask_dollars") or 0)
    return float(m.get("yes_bid_size_fp") or m.get("no_ask_size_fp") or 0) * float(m.get("no_ask_dollars") or 0)


# ---------------------------------------------------------------- per game
def solve(outcomes, inst):
    if len(inst) < 2:
        return None
    C = np.array([x[1] for x in inst])
    P = np.array([[x[2](o) for o in outcomes] for x in inst]).T
    n = len(inst)
    res = linprog(np.r_[np.zeros(n), -1], A_ub=np.hstack([-(P - C[None, :]), np.ones((len(outcomes), 1))]),
                  b_ub=np.zeros(len(outcomes)), A_eq=np.r_[C, 0][None, :], b_eq=[1],
                  bounds=[(0, None)] * n + [(None, None)], method="highs")
    if res.status != 0:
        return None
    legs = [(inst[i][0], res.x[i] * C[i], inst[i][3]) for i in range(n) if res.x[i] * C[i] > 1e-4]
    return res.x[-1], legs, n


def add_contract(lst, label, price, fee, pays, size=0.0):
    if price and 0 < price < 1 and size and size >= 1:   # skip dust (< $1 available)
        lst.append((label, price + fee * price * (1 - price), pays, size))


def add_book(lst, label, american, wins, push=lambda x: False):
    d = dec(american)
    lst.append((label, 1.0, lambda x, d=d, w=wins, p=push: d if w(x) else (1.0 if p(x) else 0.0), None))


results = []
for e in events:
    home, away = e["home_team"], e["away_team"]
    hc = next((c for c, n in TEAMS.items() if n == home), None)
    ac = next((c for c, n in TEAMS.items() if n == away), None)
    game = f"{away} @ {home}"
    books_ev = dict(per_event.get(e["id"], {}))
    books_ev.setdefault("bookmakers", [])
    main_ev = next((x for x in main if x["id"] == e["id"]), {"bookmakers": []})
    kmatch = lambda m: (hc and ac) and f"{ac}{hc}" in m["event_ticker"]  # noqa: E731
    pslug = next((s for s in pm_events if s == f"mlb-{(ac or '').lower()}-{(hc or '').lower()}-{DAY}"), None)
    pmk = pm_events[pslug]["markets"] if pslug else []
    sg = lambda team: 1 if team == home else -1  # noqa: E731   x = home runs - away runs
    dims = {k: [] for k in ["run differential", "total runs", f"{home} team total", f"{away} team total",
                            "F5 margin", "F5 total", "run in 1st inning"]}

    # books
    for src in (main_ev, books_ev):
        for bm in src.get("bookmakers", []):
            for mk in bm["markets"]:
                key, outs = mk["key"], mk["outcomes"]
                three = any(o["name"] == "Draw" for o in outs)
                for o in outs:
                    lab = f"{bm['key']} {mk['key']} {o.get('description', '')} {o['name']} {o.get('point', '')} {o['price']:+d}"
                    if key == "h2h":
                        add_book(dims["run differential"], lab, o["price"], lambda x, s=sg(o["name"]): x * s > 0)
                    elif key in ("spreads", "alternate_spreads"):
                        s, pt = sg(o["name"]), o["point"]
                        add_book(dims["run differential"], lab, o["price"], lambda x, s=s, pt=pt: x * s + pt > 0,
                                 lambda x, s=s, pt=pt: x * s + pt == 0)
                    elif key in ("totals", "alternate_totals", "totals_1st_5_innings"):
                        L, ov = o["point"], o["name"] == "Over"
                        add_book(dims["F5 total" if "1st_5" in key else "total runs"], lab, o["price"],
                                 lambda t, L=L, ov=ov: t > L if ov else t < L, lambda t, L=L: t == L)
                    elif key in ("team_totals", "alternate_team_totals"):
                        dk = f"{o['description']} team total"
                        if dk in dims:
                            L, ov = o["point"], o["name"] == "Over"
                            add_book(dims[dk], lab, o["price"], lambda t, L=L, ov=ov: t > L if ov else t < L,
                                     lambda t, L=L: t == L)
                    elif key == "h2h_1st_5_innings":
                        if o["name"] == "Draw":
                            add_book(dims["F5 margin"], lab, o["price"], lambda x: x == 0)
                        else:
                            s = sg(o["name"])
                            add_book(dims["F5 margin"], lab + (" (3-way)" if three else ""), o["price"],
                                     lambda x, s=s: x * s > 0, (lambda x: False) if three else (lambda x: x == 0))
                    elif key == "spreads_1st_5_innings":
                        s, pt = sg(o["name"]), o["point"]
                        add_book(dims["F5 margin"], lab, o["price"], lambda x, s=s, pt=pt: x * s + pt > 0,
                                 lambda x, s=s, pt=pt: x * s + pt == 0)
                    else:
                        base = key.replace("_alternate", "")
                        if base in PROPS and o.get("description") and o.get("point") is not None:
                            dk = f"prop {base} {norm(o['description'])}"
                            dims.setdefault(dk, [])
                            L, ov = o["point"], o["name"] == "Over"
                            add_book(dims[dk], lab, o["price"], lambda t, L=L, ov=ov: t > L if ov else t < L,
                                     lambda t, L=L: t == L)

    # Kalshi
    def code_side(code):
        return 1 if code == hc else -1 if code == ac else None
    for m in [m for m in kal["KXMLBGAME"] if kmatch(m)]:
        s = code_side(m["ticker"].split("-")[-1])
        if s:
            add_contract(dims["run differential"], f"Kalshi {m['yes_sub_title']} win YES", float(m["yes_ask_dollars"]), KF, lambda x, s=s: float(x * s > 0), size=ksz(m, True))
            add_contract(dims["run differential"], f"Kalshi {m['yes_sub_title']} win NO", float(m["no_ask_dollars"]), KF, lambda x, s=s: float(not x * s > 0), size=ksz(m, False))
    for series, dim in (("KXMLBSPREAD", "run differential"), ("KXMLBF5SPREAD", "F5 margin")):
        for m in [m for m in kal[series] if kmatch(m)]:
            mm = re.match(r"([A-Z]+)(\d+)$", m["ticker"].split("-")[-1])
            s = code_side(mm.group(1)) if mm else None
            L = float(mm.group(2)) - 0.5 if mm else None
            if s:
                add_contract(dims[dim], f"Kalshi {m['yes_sub_title']} YES", float(m["yes_ask_dollars"]), KF, lambda x, s=s, L=L: float(x * s > L), size=ksz(m, True))
                add_contract(dims[dim], f"Kalshi {m['yes_sub_title']} NO", float(m["no_ask_dollars"]), KF, lambda x, s=s, L=L: float(not x * s > L), size=ksz(m, False))
    for m in [m for m in kal["KXMLBF5"] if kmatch(m)]:
        c = m["ticker"].split("-")[-1]
        f = (lambda x: x == 0) if c == "TIE" else (lambda x, s=code_side(c): x * s > 0)
        add_contract(dims["F5 margin"], f"Kalshi {m['yes_sub_title']} YES", float(m["yes_ask_dollars"]), KF, lambda x, f=f: float(f(x)), size=ksz(m, True))
        add_contract(dims["F5 margin"], f"Kalshi {m['yes_sub_title']} NO", float(m["no_ask_dollars"]), KF, lambda x, f=f: float(not f(x)), size=ksz(m, False))
    for series, dim in (("KXMLBTOTAL", "total runs"), ("KXMLBF5TOTAL", "F5 total")):
        for m in [m for m in kal[series] if kmatch(m)]:
            L = float(re.search(r"over ([\d.]+)", m["yes_sub_title"], re.I).group(1))
            add_contract(dims[dim], f"Kalshi Over {L} YES", float(m["yes_ask_dollars"]), KF, lambda t, L=L: float(t > L), size=ksz(m, True))
            add_contract(dims[dim], f"Kalshi Under {L} (NO)", float(m["no_ask_dollars"]), KF, lambda t, L=L: float(not t > L), size=ksz(m, False))
    for m in [m for m in kal["KXMLBTEAMTOTAL"] if kmatch(m)]:
        mm = re.match(r"([A-Z]+)(\d+)$", m["ticker"].split("-")[-1])
        if not mm or code_side(mm.group(1)) is None:
            continue
        team = TEAMS[mm.group(1)]
        L = float(mm.group(2)) - 0.5
        add_contract(dims[f"{team} team total"], f"Kalshi {m['yes_sub_title']} YES", float(m["yes_ask_dollars"]), KF, lambda t, L=L: float(t > L), size=ksz(m, True))
        add_contract(dims[f"{team} team total"], f"Kalshi {m['yes_sub_title']} NO", float(m["no_ask_dollars"]), KF, lambda t, L=L: float(not t > L), size=ksz(m, False))
    for m in [m for m in kal["KXMLBRFI"] if kmatch(m)]:
        add_contract(dims["run in 1st inning"], "Kalshi YRFI YES", float(m["yes_ask_dollars"]), KF, lambda r: float(r == 1), size=ksz(m, True))
        add_contract(dims["run in 1st inning"], "Kalshi NRFI (NO)", float(m["no_ask_dollars"]), KF, lambda r: float(r == 0), size=ksz(m, False))
    for base, (series, _) in PROPS.items():
        for m in [m for m in kal.get(series, []) if series and kmatch(m)]:
            mm = re.match(r"(.+): (\d+)\+$", m["yes_sub_title"])
            if not mm:
                continue
            dk = f"prop {base} {norm(mm.group(1))}"
            dims.setdefault(dk, [])
            N = int(mm.group(2))
            add_contract(dims[dk], f"Kalshi {m['yes_sub_title']} YES", float(m["yes_ask_dollars"]), KF, lambda t, N=N: float(t >= N), size=ksz(m, True))
            add_contract(dims[dk], f"Kalshi {m['yes_sub_title']} NO", float(m["no_ask_dollars"]), KF, lambda t, N=N: float(t < N), size=ksz(m, False))

    # Polymarket
    for m in pmk:
        if not m.get("active") or m.get("closed"):
            continue
        typ, q, slug = m.get("sportsMarketType"), m["question"], m["slug"]
        ls = next((x for x in m["marketSides"] if x.get("long")), None)
        if ls is None:
            continue
        fee = float(m.get("feeCoefficient") or 0.07)
        la, sa, lsz, ssz = pm_quotes(m)
        ld = ls["description"]

        def both(dim, label, f):
            add_contract(dims[dim], f"Poly {label} @{la}", la, fee, lambda x, f=f: float(f(x)), size=lsz)
            add_contract(dims[dim], f"Poly NOT({label}) @{sa and round(sa, 3)}", sa, fee, lambda x, f=f: float(not f(x)), size=ssz)
        mt = re.match(r"Will (?:the )?(.+?) (?:cover|win|score)\b", q)
        subj = mt.group(1) if mt else ""
        team_in_q = home if subj == home else away if subj == away else None   # questions name both teams
        if typ == "baseball_team_full_game_winner":
            s = sg(ld) if ld in (home, away) else None
            if s:
                both("run differential", f"{ld} win", lambda x, s=s: x * s > 0)
        elif typ in ("baseball_team_full_game_spread", "baseball_team_first_five_spread") and team_in_q:
            s, hcap = sg(team_in_q), float(ld)
            both("F5 margin" if "first_five" in typ else "run differential", f"{team_in_q} {hcap:+g}",
                 lambda x, s=s, hcap=hcap: x * s + hcap > 0)
        elif typ in ("baseball_team_full_game_total", "baseball_team_first_five_total"):
            mm = re.search(r"more than ([\d.]+)", q)
            if mm:
                L = float(mm.group(1))
                f = (lambda t, L=L: t > L) if ld == "Over" else (lambda t, L=L: t < L)
                both("F5 total" if "first_five" in typ else "total runs", f"{ld} {L}", f)
        elif typ == "baseball_team_total_runs" and team_in_q:
            mm = re.search(r"more than ([\d.]+)", q)
            if mm:
                L = float(mm.group(1))
                yes = ld == "Yes"
                both(f"{team_in_q} team total", f"{team_in_q} over {L} {'YES' if yes else 'NO'}",
                     (lambda t, L=L: t > L) if yes else (lambda t, L=L: not t > L))
        elif typ == "baseball_team_first_five_winner" and team_in_q:
            s, yes = sg(team_in_q), ld == "Yes"
            both("F5 margin", f"{team_in_q} wins F5 {'YES' if yes else 'NO'} (tie=No)",
                 (lambda x, s=s: x * s > 0) if yes else (lambda x, s=s: not x * s > 0))
        elif typ == "baseball_team_first_inning_run":
            yes = ld == "Yes"
            both("run in 1st inning", f"YRFI {'YES' if yes else 'NO'}", (lambda r: r == 1) if yes else (lambda r: r == 0))
        else:
            base = next((b for b, (_, pt) in PROPS.items() if pt == typ), None)
            mm = re.match(r"Will (.+?) record at least (\d+) ", q)
            if base and mm:
                dk = f"prop {base} {norm(mm.group(1))}"
                dims.setdefault(dk, [])
                N, yes = int(mm.group(2)), ld == "Yes"
                both(dk, f"{mm.group(1)} {N}+ {base} {'YES' if yes else 'NO'}",
                     (lambda t, N=N: t >= N) if yes else (lambda t, N=N: t < N))

    spaces = {"run differential": [x for x in range(-25, 26) if x != 0], "total runs": list(range(0, 41)),
              "F5 margin": list(range(-20, 21)), "F5 total": list(range(0, 31)), "run in 1st inning": [0, 1]}
    for dim, inst in dims.items():
        venues = {lab.split()[0] for lab, *_ in inst}
        if len(inst) < 2 or (len(venues) < 2 and not any(v in venues for v in ("Kalshi", "Poly"))):
            continue
        outs = spaces.get(dim, list(range(0, 41)))
        r = solve(outs, inst)
        if r:
            results.append((r[0], game, dim, r[1], r[2], sorted(venues)))

# ---------------------------------------------------------------- report
results.sort(key=lambda x: -x[0])
print(f"\nGroups solved: {len(results)} | positive (arb): {sum(1 for r in results if r[0] > 1e-6)} | "
      f"within 1%: {sum(1 for r in results if -0.01 < r[0] <= 1e-6)}")
for t, game, dim, legs, n, venues in results[:25]:
    flag = "  [prop: player-participation rules differ]" if dim.startswith("prop") else ""
    caps = [a / w for _, w, a in legs if a is not None and w > 0]
    cap = f"~${min(caps):,.0f} total fillable at these prediction-market prices" if caps else "sportsbooks only (book limits unknown)"
    print(f"\n{t * 100:+.2f}%  {game} | {dim} ({n} bets; {', '.join(venues)}) | {cap}{flag}")
    for lab, w, a in sorted(legs, key=lambda z: -z[1]):
        print(f"    ${w * 1000:8.2f} per $1,000  {lab}" + (f"   (${a:,.0f} available)" if a is not None else ""))
(OUT / "results.json").write_text(json.dumps([(t, g, d, l, n, v) for t, g, d, l, n, v in results]))
