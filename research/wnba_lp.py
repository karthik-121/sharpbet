"""WNBA: 8 sportsbooks + Kalshi + Polymarket US, all-combinations LP per game for final margin, first-half
margin and game total. Basketball can't end tied (OT counts); whole-number book lines push (refund); a first
half can end tied (books' 2-way 1H moneyline refunds it unless the book lists a Draw; Kalshi 1H winner lists a
Tie contract when it has one). Kalshi 0.07*P*(1-P) fee; Polymarket feeCoefficient*P*(1-P).
Usage: python research/wnba_lp.py [dry] [YYYY-MM-DD]   (~5-6 credits per game)."""
import json
import os
import re
import sys
import time
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
DAY = next((a for a in sys.argv[1:] if re.match(r"\d{4}-\d{2}-\d{2}$", a)), date.today().isoformat())
BOOKS = "fanduel,draftkings,betmgm,williamhill_us,betrivers,espnbet,fanatics,hardrockbet"
OA, K, PM = "https://api.the-odds-api.com/v4", "https://external-api.kalshi.com/trade-api/v2", "https://gateway.polymarket.us/v1"
KF = 0.07
PER_GAME = ["alternate_spreads", "alternate_totals", "spreads_h1", "h2h_h1"]
dec = lambda a: 1 + a / 100 if a > 0 else 1 + 100 / -a  # noqa: E731


def jget(url):
    for a in range(6):
        try:
            time.sleep(0.1)
            r = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "sharpbet-research"}), timeout=30)
            return json.load(r), r.headers
        except urllib.error.HTTPError as e:
            if e.code != 429:
                raise
            time.sleep(1.5 * (a + 1))
    raise RuntimeError("rate limited")


now = datetime.now(timezone.utc)
events = [e for e in jget(f"{OA}/sports/basketball_wnba/events?apiKey={KEY}")[0]
          if datetime.fromisoformat(e["commence_time"].replace("Z", "+00:00")).astimezone().date().isoformat() == DAY
          and datetime.fromisoformat(e["commence_time"].replace("Z", "+00:00")) > now + timedelta(minutes=10)]
print(f"{DAY}: {len(events)} WNBA games | max credits {3 + len(events) * len(PER_GAME)}")
for e in events:
    print("  ", e["away_team"], "@", e["home_team"], e["commence_time"])
if DRY or not events:
    sys.exit(0)

main, h = jget(f"{OA}/sports/basketball_wnba/odds?apiKey={KEY}&regions=us&markets=h2h,spreads,totals&oddsFormat=american&bookmakers={BOOKS}")
spent = int(float(h.get("x-requests-last", 0)))
per = {}
for e in events:
    per[e["id"]], h = jget(f"{OA}/sports/basketball_wnba/events/{e['id']}/odds?apiKey={KEY}&regions=us&oddsFormat=american"
                           f"&bookmakers={BOOKS}&markets={','.join(PER_GAME)}")
    spent += int(float(h.get("x-requests-last", 0)))
print(f"odds api credits spent: {spent} | remaining: {h.get('x-requests-remaining')} | {datetime.now():%I:%M %p}")

kd = datetime.fromisoformat(DAY).strftime("%y%b").upper() + DAY[8:]
kal = {}
for s in ["KXWNBAGAME", "KXWNBASPREAD", "KXWNBATOTAL", "KXWNBA1HSPREAD", "KXWNBA1HWINNER"]:
    evs = [x for x in jget(f"{K}/events?series_ticker={s}&status=open&limit=200")[0].get("events", []) if kd in x["event_ticker"]]
    kal[s] = [(x, m) for x in evs for m in jget(f"{K}/markets?event_ticker={x['event_ticker']}&limit=200")[0].get("markets", [])
              if m.get("status") == "active"]
pm = {}
for off in range(0, 60000, 200):
    evs = jget(f"{PM}/events?closed=false&limit=200&offset={off}")[0].get("events", [])
    for x in evs:
        if (x.get("slug") or "").startswith("wnba-") and x["slug"].endswith(DAY):
            pm[x["slug"]] = x
    if len(evs) < 200:
        break
print(f"Kalshi markets: {sum(len(v) for v in kal.values())} | Polymarket games: {len(pm)}")
if kal["KXWNBASPREAD"]:
    print("Kalshi spread rule:", kal["KXWNBASPREAD"][0][1]["rules_primary"][:160])


def pm_quotes(slug):
    b = jget(f"{PM}/markets/{slug}/book")[0]["marketData"]
    offers = [o for o in (b.get("offers") or b.get("asks") or []) if float(o.get("qty") or 0) > 0]
    bids = [o for o in (b.get("bids") or []) if float(o.get("qty") or 0) > 0]
    la = min((float(o["px"]["value"]) for o in offers), default=None)
    lb = max((float(o["px"]["value"]) for o in bids), default=None)
    if la is not None and lb is not None and lb > la:
        return None, None
    return la, (1 - lb) if lb is not None else None


def solve(outcomes, inst):
    C = np.array([c for _, c, _ in inst])
    P = np.array([[f(x) for x in outcomes] for *_, f in inst]).T
    n = len(inst)
    res = linprog(np.r_[np.zeros(n), -1], A_ub=np.hstack([-(P - C[None, :]), np.ones((len(outcomes), 1))]),
                  b_ub=np.zeros(len(outcomes)), A_eq=np.r_[C, 0][None, :], b_eq=[1],
                  bounds=[(0, None)] * n + [(None, None)], method="highs")
    return res.x[-1], [(inst[i][0], res.x[i] * C[i]) for i in range(n) if res.x[i] * C[i] > 1e-4]


for e in events:
    home, away = e["home_team"], e["away_team"]

    def side(text):
        t = text.lower()
        for team, s in ((home, 1), (away, -1)):
            city, nick = " ".join(team.split()[:-1]).lower(), team.split()[-1].lower()
            if team.lower() in t or nick in t or (city and t.startswith(city)) or t in team.lower():
                return s
        return None
    D = {"final margin": [], "first-half margin": [], "total points": []}

    def book(dim, lab, am, wins, push=lambda x: False):
        d = dec(am)
        D[dim].append((lab, 1.0, lambda x, d=d, w=wins, p=push: d if w(x) else (1.0 if p(x) else 0.0)))

    def contract(dim, lab, price, fee, f):
        if price and 0 < price < 1:
            D[dim].append((lab, price + fee * price * (1 - price), f))

    for src in (next((x for x in main if x["id"] == e["id"]), {}), per.get(e["id"], {})):
        for bm in src.get("bookmakers", []):
            for mk in bm["markets"]:
                three = any(o["name"] == "Draw" for o in mk["outcomes"])
                for o in mk["outcomes"]:
                    lab = f"{bm['key']} {mk['key']} {o['name'].split()[-1]} {o.get('point', '')} {o['price']:+d}"
                    if mk["key"] == "h2h":
                        s = side(o["name"])
                        book("final margin", lab, o["price"], lambda x, s=s: x * s > 0)
                    elif mk["key"] in ("spreads", "alternate_spreads", "spreads_h1"):
                        s, pt = side(o["name"]), o["point"]
                        book("first-half margin" if mk["key"].endswith("_h1") else "final margin", lab, o["price"],
                             lambda x, s=s, pt=pt: x * s + pt > 0, lambda x, s=s, pt=pt: x * s + pt == 0)
                    elif mk["key"] in ("totals", "alternate_totals"):
                        L, ov = o["point"], o["name"] == "Over"
                        book("total points", lab, o["price"], lambda t, L=L, ov=ov: t > L if ov else t < L, lambda t, L=L: t == L)
                    elif mk["key"] == "h2h_h1":
                        if o["name"] == "Draw":
                            book("first-half margin", lab, o["price"], lambda x: x == 0)
                        else:
                            s = side(o["name"])
                            book("first-half margin", lab + (" (3-way)" if three else ""), o["price"], lambda x, s=s: x * s > 0,
                                 (lambda x: False) if three else (lambda x: x == 0))

    match = lambda x: side(x.get("title", "").split(":")[0].split(" vs ")[0].replace("Game 2: ", "").replace("Game 1: ", "")) is not None \
        and side(x.get("title", "").split(":")[0].split(" vs ")[-1]) is not None  # noqa: E731
    for series, dim in (("KXWNBASPREAD", "final margin"), ("KXWNBA1HSPREAD", "first-half margin")):
        for x, m in kal[series]:
            if not match(x):
                continue
            y = m["yes_sub_title"]
            mm = re.search(r"(?:over|-)\s*([\d.]+)", y)
            s = side(y)
            if mm and s:
                L = float(mm.group(1))
                contract(dim, f"Kalshi {y} YES", float(m["yes_ask_dollars"]), KF, lambda v, s=s, L=L: float(v * s > L))
                contract(dim, f"Kalshi {y} NO", float(m["no_ask_dollars"]), KF, lambda v, s=s, L=L: float(not v * s > L))
    for x, m in kal["KXWNBAGAME"]:
        if match(x):
            s = side(m["yes_sub_title"])
            if s:
                contract("final margin", f"Kalshi {m['yes_sub_title']} win YES", float(m["yes_ask_dollars"]), KF, lambda v, s=s: float(v * s > 0))
                contract("final margin", f"Kalshi {m['yes_sub_title']} win NO", float(m["no_ask_dollars"]), KF, lambda v, s=s: float(not v * s > 0))
    for x, m in kal["KXWNBA1HWINNER"]:
        if match(x):
            y = m["yes_sub_title"]
            f = (lambda v: v == 0) if "tie" in y.lower() else (lambda v, s=side(y): s is not None and v * s > 0)
            contract("first-half margin", f"Kalshi 1H {y} YES", float(m["yes_ask_dollars"]), KF, lambda v, f=f: float(f(v)))
            contract("first-half margin", f"Kalshi 1H {y} NO", float(m["no_ask_dollars"]), KF, lambda v, f=f: float(not f(v)))
    for x, m in kal["KXWNBATOTAL"]:
        if match(x):
            mm = re.search(r"([\d.]+)", m["yes_sub_title"])
            if mm:
                L = float(mm.group(1))
                contract("total points", f"Kalshi Over {L}", float(m["yes_ask_dollars"]), KF, lambda t, L=L: float(t > L))
                contract("total points", f"Kalshi Under {L}", float(m["no_ask_dollars"]), KF, lambda t, L=L: float(not t > L))

    ev = next((v for v in pm.values() if side(v["title"].split(" vs. ")[0].replace("Game 2: ", "").replace("Game 1: ", ""))
               and side(v["title"].split(" vs. ")[-1])), None)
    for m in (ev or {}).get("markets", []):
        typ, q = m.get("sportsMarketType"), m["question"]
        ls = next((x for x in m["marketSides"] if x.get("long")), None)
        if not ls or not m.get("active") or m.get("closed"):
            continue
        fee, ld = float(m.get("feeCoefficient") or 0.07), ls["description"]
        if typ == "basketball_team_full_game_winner":
            s = side(ld)
            if not s:
                continue
            f, lab, dim = (lambda v, s=s: v * s > 0), f"{ld} win", "final margin"
        elif typ == "basketball_team_full_game_spread":
            mt = re.match(r"Will (?:the )?(.+?) cover", q)
            s = mt and side(mt.group(1))
            if not s:
                continue
            hcap = float(ld)
            f, lab, dim = (lambda v, s=s, hcap=hcap: v * s + hcap > 0), f"{mt.group(1)} {hcap:+g}", "final margin"
        elif typ == "basketball_team_full_game_total":
            mm = re.search(r"more than ([\d.]+)", q)
            if not mm:
                continue
            L, over = float(mm.group(1)), ld == "Over"
            f = (lambda t, L=L: t > L) if over else (lambda t, L=L: t < L)
            lab, dim = f"{'Over' if over else 'Under'} {L}", "total points"
        else:
            continue
        la, sa = pm_quotes(m["slug"])
        contract(dim, f"Poly {lab} @{la}", la, fee, lambda v, f=f: float(f(v)))
        contract(dim, f"Poly NOT({lab}) @{sa and round(sa, 3)}", sa, fee, lambda v, f=f: float(not f(v)))

    print(f"\n=== {away} @ {home} ({datetime.fromisoformat(e['commence_time'].replace('Z', '+00:00')).astimezone():%a %I:%M %p})")
    spaces = {"final margin": [v for v in range(-70, 71) if v != 0], "first-half margin": list(range(-45, 46)),
              "total points": list(range(80, 261))}
    for dim, inst in D.items():
        if len(inst) < 2:
            continue
        venues = {l.split()[0] for l, *_ in inst}
        nk, npm = sum(1 for l, *_ in inst if l.startswith("Kalshi")), sum(1 for l, *_ in inst if l.startswith("Poly"))
        t, legs = solve(spaces[dim], inst)
        print(f"  {dim}: best guaranteed {t * 100:+.2f}%  ({len(inst)} bets: {nk} Kalshi, {npm} Polymarket, "
              f"{len(inst) - nk - npm} sportsbook)")
        for lab, w in sorted(legs, key=lambda z: -z[1]):
            print(f"      ${w * 1000:8.2f} per $1,000  {lab}")
