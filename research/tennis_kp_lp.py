"""Tennis: Kalshi vs Polymarket US, all-combinations LP per match over every possible best-of-3 score line.

Every outcome is a full score line (each set 6-0..6-4, 7-5, 7-6 for either player; 2 or 3 sets: 1,470 lines),
so match winner, set 1/2 winner, total games, games spread, sets spread, total sets and exact score can all be
combined. Retirement rules match on both venues (retirement after a ball is played: opponent wins the match;
other markets settle if already decided, else fair market price), so completed-match outcomes are what count.
Fees: Kalshi 0.07*P*(1-P); Polymarket feeCoefficient*P*(1-P). Reports how many dollars the thinnest leg allows.
Free: no Odds API credits. Usage: python research/tennis_kp_lp.py"""
import itertools
import json
import re
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import numpy as np
from scipy.optimize import linprog

K = "https://external-api.kalshi.com/trade-api/v2"
PM = "https://gateway.polymarket.us/v1"
KF = 0.07
NOW = datetime.now(timezone.utc)


def get(u):
    for a in range(6):
        try:
            time.sleep(0.12)
            return json.load(urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "sharpbet-research"}),
                                                    timeout=30))
        except urllib.error.HTTPError as e:
            if e.code != 429:
                raise
            time.sleep(1.5 * (a + 1))


def sur(name):
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z ]", "", s).split()[-1] if s.strip() else ""


# ---------------------------------------------------------------- score lines (A's view)
SET_W = [(6, g) for g in range(5)] + [(7, 5), (7, 6)]      # winner's games, loser's games
OUTCOMES = []
for pattern in [("A", "A"), ("B", "B"), ("A", "B", "A"), ("B", "A", "A"), ("A", "B", "B"), ("B", "A", "B")]:
    for sets in itertools.product(SET_W, repeat=len(pattern)):
        ga = sum(w if p == "A" else l for p, (w, l) in zip(pattern, sets))
        gb = sum(l if p == "A" else w for p, (w, l) in zip(pattern, sets))
        sa, sb = pattern.count("A"), pattern.count("B")
        OUTCOMES.append(dict(win="A" if sa == 2 else "B", set1=pattern[0], set2=pattern[1], games=ga + gb,
                             gdiff=ga - gb, sa=sa, sb=sb, nsets=len(pattern)))

# ---------------------------------------------------------------- Polymarket matches starting soon
pm = []
for off in range(0, 60000, 200):
    evs = get(f"{PM}/events?closed=false&limit=200&offset={off}").get("events", [])
    pm += [e for e in evs if (e.get("slug") or "").split("-")[0] in ("atp", "wta") and e.get("startTime")
           and NOW + timedelta(minutes=5) < datetime.fromisoformat(e["startTime"].replace("Z", "+00:00")) < NOW + timedelta(hours=16)
           and len(e.get("markets", [])) > 3]
    if len(evs) < 200:
        break
print(f"{datetime.now():%I:%M %p} | Polymarket tennis matches not started (next 16h): {len(pm)}")

# ---------------------------------------------------------------- Kalshi tennis events (by surname pair)
kal = {}
for s in ["KXATPMATCH", "KXWTAMATCH", "KXATPCHALLENGERMATCH", "KXWTACHALLENGERMATCH", "KXATPSETWINNER", "KXWTASETWINNER",
          "KXATPGSPREAD", "KXWTAGSPREAD", "KXATPGTOTAL", "KXWTAGTOTAL", "KXATPEXACTMATCH", "KXWTAEXACTMATCH"]:
    try:
        evs = get(f"{K}/events?series_ticker={s}&status=open&limit=200").get("events", [])
    except urllib.error.HTTPError:
        continue
    for e in evs:
        t = e.get("title", "").split(":")[0]
        parts = re.split(r"\s+vs\.?\s+", t)
        if len(parts) != 2:
            continue
        key = frozenset(sur(p) for p in parts)
        kal.setdefault(key, []).append((s, e))
print(f"Kalshi tennis events: {sum(len(v) for v in kal.values())} across {len(kal)} matches")


def pm_prices(slug):
    b = get(f"{PM}/markets/{slug}/book")["marketData"]
    offers = [o for o in (b.get("offers") or b.get("asks") or []) if float(o.get("qty") or 0) > 0]
    bids = [o for o in (b.get("bids") or []) if float(o.get("qty") or 0) > 0]   # ignore phantom quotes with no size
    best_o = min(offers, key=lambda o: float(o["px"]["value"]), default=None)
    best_b = max(bids, key=lambda o: float(o["px"]["value"]), default=None)
    la = float(best_o["px"]["value"]) if best_o else None
    lb = float(best_b["px"]["value"]) if best_b else None
    if la is not None and lb is not None and lb > la:
        return None
    return (la, float(best_o["qty"]) if best_o else 0.0, (1 - lb) if lb is not None else None,
            float(best_b["qty"]) if best_b else 0.0)


results = []
for ev in pm:
    title = ev["title"].replace(" vs. ", " vs ")
    pa, pb = [p.strip() for p in title.split(" vs ")]
    A, B = sur(pa), sur(pb)
    key = frozenset((A, B))
    if key not in kal:
        continue
    side = lambda name: "A" if sur(name) == A else "B" if sur(name) == B else None  # noqa: E731
    inst = []   # (label, cost per $1 payout, payout(o), dollars available)

    def add(label, price, fee, size, f):
        if price and 0 < price < 1:
            inst.append((label, price + fee * price * (1 - price), f, (size or 0) * price))

    # Polymarket
    for m in ev["markets"]:
        if not m.get("active") or m.get("closed"):
            continue
        typ, q, slug = m.get("sportsMarketType"), m["question"], m["slug"]
        ls = next((x for x in m["marketSides"] if x.get("long")), None)
        if ls is None:
            continue
        fee, ld = float(m.get("feeCoefficient") or 0.07), ls["description"]
        f = None
        if typ == "tennis_match_winner":
            s = side(ld)
            f = s and (lambda o, s=s: o["win"] == s)
            lab = f"{ld} wins"
        elif typ in ("tennis_set_1_winner", "tennis_set_2_winner"):
            mm = re.match(r"Will (.+?) win set (\d)", q)
            s = mm and side(mm.group(1))
            if s:
                k = "set1" if mm.group(2) == "1" else "set2"
                yes = ld == "Yes"
                f = (lambda o, s=s, k=k: o[k] == s) if yes else (lambda o, s=s, k=k: o[k] != s)
                lab = f"{mm.group(1)} wins set {mm.group(2)} {'YES' if yes else 'NO'}"
        elif typ in ("tennis_match_games_spread", "tennis_match_sets_spread"):
            mm = re.match(r"Will (?:the )?(.+?) cover", q)
            s = mm and side(mm.group(1))
            if s:
                h, sg = float(ld), (1 if s == "A" else -1)
                if "games" in typ:
                    f = lambda o, h=h, sg=sg: o["gdiff"] * sg + h > 0  # noqa: E731
                else:
                    f = lambda o, h=h, sg=sg: (o["sa"] - o["sb"]) * sg + h > 0  # noqa: E731
                lab = f"{mm.group(1)} {h:+g} {'games' if 'games' in typ else 'sets'}"
        elif typ in ("tennis_match_total_games", "tennis_match_total_sets"):
            mm = re.search(r"more than ([\d.]+)", q)
            if mm:
                L, over, k = float(mm.group(1)), ld == "Over", ("games" if "games" in typ else "nsets")
                f = (lambda o, L=L, k=k: o[k] > L) if over else (lambda o, L=L, k=k: o[k] < L)
                lab = f"{'Over' if over else 'Under'} {L} {'games' if k == 'games' else 'sets'}"
        elif typ == "tennis_match_exact_score":
            mm = re.search(r"es-(\d)-(\d)$", slug)   # first number = first-named player's sets
            if mm and ld == "Yes":
                a, b = int(mm.group(1)), int(mm.group(2))
                f = lambda o, a=a, b=b: o["sa"] == a and o["sb"] == b  # noqa: E731
                lab = f"exact {a}-{b} ({pa.split()[-1]}-{pb.split()[-1]})"
        if not f:
            continue
        pr = pm_prices(slug)
        if not pr:
            continue
        la, lq, sa_, sq = pr
        add(f"Poly {lab}", la, fee, lq, f)
        add(f"Poly NOT({lab})", sa_, fee, sq, lambda o, f=f: not f(o))

    # Kalshi
    for series, e in kal[key]:
        for m in get(f"{K}/markets?event_ticker={e['event_ticker']}&limit=100").get("markets", []):
            if m.get("status") != "active":
                continue
            y = m.get("yes_sub_title", "")
            f = None
            if "MATCH" in series and "EXACT" not in series:
                s = side(y)
                f = s and (lambda o, s=s: o["win"] == s)
            elif "SETWINNER" in series:
                n = e["event_ticker"].rsplit("-", 1)[-1]
                s = side(y)
                if s and n in ("1", "2"):
                    f = lambda o, s=s, k=f"set{n}": o[k] == s  # noqa: E731
            elif "GSPREAD" in series:
                mm = re.match(r"(.+?) -([\d.]+) games", y)
                s = mm and side(mm.group(1))
                if s:
                    L, sg = float(mm.group(2)), (1 if s == "A" else -1)
                    f = lambda o, L=L, sg=sg: o["gdiff"] * sg > L  # noqa: E731
            elif "GTOTAL" in series:
                mm = re.search(r"Over ([\d.]+) games", y)
                if mm:
                    L = float(mm.group(1))
                    f = lambda o, L=L: o["games"] > L  # noqa: E731
            elif "EXACT" in series:
                mm = re.match(r"(.+?) wins (\d)-(\d)", y)
                s = mm and side(mm.group(1))
                if s:
                    w, l = int(mm.group(2)), int(mm.group(3))
                    a, b = (w, l) if s == "A" else (l, w)
                    f = lambda o, a=a, b=b: o["sa"] == a and o["sb"] == b  # noqa: E731
            if not f:
                continue
            ya, na = float(m.get("yes_ask_dollars") or 0), float(m.get("no_ask_dollars") or 0)
            # buying NO at the no-ask fills against YES bids, so NO size = yes-bid size
            ys, ns = float(m.get("yes_ask_size_fp") or 0), float(m.get("yes_bid_size_fp") or m.get("no_ask_size_fp") or 0)
            if ys > 0:
                add(f"Kalshi {y} YES", ya, KF, ys, f)
            if ns > 0:
                add(f"Kalshi {y} NO", na, KF, ns, lambda o, f=f: not f(o))

    if len(inst) < 2:
        continue
    C = np.array([c for _, c, _, _ in inst])
    P = np.array([[float(f(o)) for o in OUTCOMES] for _, _, f, _ in inst]).T
    n = len(inst)
    res = linprog(np.r_[np.zeros(n), -1], A_ub=np.hstack([-(P - C[None, :]), np.ones((len(OUTCOMES), 1))]),
                  b_ub=np.zeros(len(OUTCOMES)), A_eq=np.r_[C, 0][None, :], b_eq=[1],
                  bounds=[(0, None)] * n + [(None, None)], method="highs")
    if res.status != 0:
        continue
    legs = [(inst[i][0], res.x[i] * C[i], inst[i][3]) for i in range(n) if res.x[i] * C[i] > 1e-4]
    cap = min((avail / w for _, w, avail in legs if avail), default=0)   # total $ the thinnest leg allows
    venues = sorted({l.split()[0] for l, *_ in inst})
    start = datetime.fromisoformat(ev["startTime"].replace("Z", "+00:00")).astimezone()
    results.append((res.x[-1], f"{pa} vs {pb} ({start:%a %I:%M %p})", legs, n, cap, venues))

results.sort(key=lambda r: -r[0])
print(f"matches with both venues: {len(results)} | arbs: {sum(1 for r in results if r[0] > 1e-6)} | "
      f"within 1%: {sum(1 for r in results if -0.01 < r[0] <= 1e-6)}\n")
for t, name, legs, n, cap, venues in results[:20]:
    print(f"{t * 100:+.2f}%  {name}  ({n} bets; {', '.join(venues)})  ~${cap:,.0f} deployable at these prices")
    for lab, w, avail in sorted(legs, key=lambda z: -z[1]):
        print(f"     ${w * 1000:8.2f} per $1,000  {lab}   (${avail:,.0f} at this price)")
