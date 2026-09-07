#!/usr/bin/env python3
"""Full scrutiny battery for one candidate, the way ehlers_trend was audited.

Sections, in the order the evidence should be read:
  1  per-fold baseline and fold-1 concentration
  2  risk-matched control: incumbent de-levered to the candidate's drawdown
  3  parameter neighbours: plateau, ridge or spike
  4  breadth: universe ladders and per-sleeve
  5  anchored walk-forward WITH re-selection (the honest number) + selection bias
  6  the 2024+ window (contaminated for the incumbent; descriptive only)
  7  every calendar year the data allows (the steelman)

Development boundary is asserted everywhere it matters. Usage:
  candidate_audit.py --strategy fracdiff --sparams "d=0.2,..." --section 1,2
"""
import argparse, itertools, json, re, statistics, subprocess, sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT.parent / "cli_trader"
BINARY = CLI / "build" / "cli_trader"
DEV_END = "2023-12-31"
INCUMBENT = ("ensemble_vote", "enterVotes=2,exitVotes=0")
CORE4 = ["BTC", "ETH", "XRP", "LTC"]
FOLDS = [("2018-19", "2018-01-01", "2019-12-31"),
         ("2020-21", "2020-01-01", "2021-12-31"),
         ("2022-23", "2022-01-01", DEV_END)]

ap = argparse.ArgumentParser()
ap.add_argument("--strategy", required=True)
ap.add_argument("--sparams", default="")
ap.add_argument("--section", default="1,2,3,4,5,6,7")
A = ap.parse_args()
CAND = (A.strategy, A.sparams)
WANT = {s.strip() for s in A.section.split(",")}

def portfolio(coins, start, end, strategy, sparams, vt=0.20, vw=30, fee=None, slip=None, dev=True):
    if dev: assert end <= DEV_END, f"development boundary: {end}"
    cmd = [str(BINARY), "portfolio", "--data-dir", str(CLI / "data"),
           "--envs", ",".join(f"{c}_USDT:14400" for c in coins), "--start", start, "--end", end,
           "--warmup-bars", "600", "--strategy", strategy, "--vol-target", str(vt),
           "--vol-window", str(vw), "--weights", "equal", "--vol-lookback", "120", "--rebalance", "30"]
    if sparams: cmd += ["--sparams", sparams]
    if fee is not None: cmd += ["--fee", str(fee)]
    if slip is not None: cmd += ["--slippage", str(slip)]
    o = subprocess.run(cmd, cwd=CLI, text=True, capture_output=True)
    if o.returncode != 0: return None
    t = o.stdout
    g = lambda p: (lambda m: float(m.group(1)) if m else float("nan"))(re.search(p, t, re.M))
    sleeves = {m.group(1): {"sharpe": float(m.group(2)), "dd": float(m.group(5)), "trades": int(m.group(6))}
               for m in re.finditer(r"^\s+([A-Z0-9]+)_USDT\s+([-+0-9.]+)\s+([-+0-9.]+)\s+([-+0-9.]+)\s+([-+0-9.]+)\s+(\d+)", t, re.M)}
    return {"sharpe": g(r"^Sharpe \(ann\.\):\s+([-+0-9.]+)"),
            "basket": g(r"^Sharpe \(ann\.\):\s+[-+0-9.]+\s+([-+0-9.]+)"),
            "cagr": g(r"^CAGR:\s+([-+0-9.]+)%"), "dd": g(r"^Max drawdown:\s+([-+0-9.]+)%"),
            "se": g(r"^\s*\+/-\s+([-+0-9.]+)"), "corr": g(r"^Avg pairwise sleeve correlation:\s+([-+0-9.]+)"),
            "trades": sum(s["trades"] for s in sleeves.values()), "sleeves": sleeves}

def excess(coins, start, end, strategy, sparams, **kw):
    a = portfolio(coins, start, end, strategy, sparams, **kw)
    b = portfolio(coins, start, end, *INCUMBENT, **kw)
    if not a or not b: return None
    return a["sharpe"] - b["sharpe"], a, b

def hdr(n, title):
    print(f"\n{'=' * 104}\n{n}. {title}\n{'=' * 104}")

print(f"CANDIDATE: {CAND[0]}  {CAND[1]}")
print(f"INCUMBENT: {INCUMBENT[0]}  {INCUMBENT[1]}")
print(f"protocol: 4 coins {'/'.join(CORE4)} 4h, vt 0.20 / vol-window 30, 600 warm-up bars")

if "1" in WANT:
    hdr(1, "PER-FOLD BASELINE  (does the mean survive fold-by-fold reading?)")
    print(f"  {'fold':10s} {'candidate':>10s} {'incumbent':>10s} {'excess':>8s} {'cand dd':>8s} {'inc dd':>8s} {'trades':>7s}")
    ex = []
    for name, s, e in FOLDS:
        r = excess(CORE4, s, e, *CAND)
        if not r: print(f"  {name:10s}  FAILED"); continue
        d, a, b = r; ex.append(d)
        print(f"  {name:10s} {a['sharpe']:10.2f} {b['sharpe']:10.2f} {d:+8.2f} {a['dd']:7.1f}% {b['dd']:7.1f}% {a['trades']:7d}")
    if ex:
        pos = [x for x in ex if x > 0]
        print(f"\n  mean excess {statistics.mean(ex):+.2f} | all folds positive: {'YES' if len(pos)==len(ex) else 'NO'}")
        if len(pos) == len(ex):
            print(f"  fold-1 share of the total edge: {ex[0]/sum(ex):.0%}   (an even contributor sits near 33%)")

if "2" in WANT:
    hdr(2, "RISK-MATCHED CONTROL  (incumbent de-levered to the candidate's drawdown)")
    full = ("2018-01-01", DEV_END)
    c = portfolio(CORE4, *full, *CAND)
    print(f"  candidate over {full[0]}..{full[1]}: Sharpe {c['sharpe']:.2f}, dd {c['dd']:.1f}%, CAGR {c['cagr']:.1f}%")
    print(f"\n  {'incumbent vol target':>21s} {'Sharpe':>7s} {'CAGR':>8s} {'maxDD':>8s}")
    lad = {}
    with ThreadPoolExecutor(max_workers=6) as p:
        vts = [0.08, 0.10, 0.15, 0.20, 0.25, 0.30]
        for vt, r in zip(vts, p.map(lambda v: portfolio(CORE4, *full, *INCUMBENT, vt=v), vts)):
            if r: lad[vt] = r; print(f"  {vt:21.2f} {r['sharpe']:7.2f} {r['cagr']:7.1f}% {r['dd']:7.1f}%")
    vt, m = min(lad.items(), key=lambda kv: abs(kv[1]["dd"] - c["dd"]))
    print(f"\n  matched at vt {vt:.2f} (dd {m['dd']:.1f}% vs candidate {c['dd']:.1f}%)")
    print(f"  RISK-MATCHED EXCESS: {c['sharpe']-m['sharpe']:+.2f} Sharpe, {c['cagr']-m['cagr']:+.1f} pp CAGR")
    print(f"  (incumbent Sharpe across the ladder: {min(r['sharpe'] for r in lad.values()):.2f} to {max(r['sharpe'] for r in lad.values()):.2f} - flat, as expected)")

if "3" in WANT:
    hdr(3, "PARAMETER NEIGHBOURS  (plateau, narrow ridge, or a spike?)")
    base = dict(kv.split("=") for kv in CAND[1].split(",")) if CAND[1] else {}
    base = {k: (float(v) if "." in v else int(v)) for k, v in base.items()}
    # one step either side in each dimension, within the registry's published range
    STEPS = {"fracdiff": {"d": [0.1, 0.15, 0.2, 0.3, 0.4], "entryZ": [0.4, 0.6, 0.8, 1.0, 1.4],
                          "exitZ": [-2.0, -1.5, -1.0, -0.5, 0.0], "zWindow": [20, 35, 50, 80, 150],
                          "momentum": [0, 1]}}
    grid = STEPS.get(CAND[0])
    if not grid:
        print("  no neighbour grid defined for this family"); WANT.discard("3")
    else:
        def fmt(d): return ",".join(f"{k}={d[k]:g}" for k in sorted(d))
        def score(sp):
            ex = []
            for _, s, e in FOLDS:
                r = excess(CORE4, s, e, CAND[0], sp)
                if not r: return None
                ex.append(r[0])
            return statistics.mean(ex), ex
        jobs = []
        for k, vals in grid.items():
            for v in vals:
                d = dict(base); d[k] = v
                jobs.append((k, v, fmt(d)))
        seen, uniq = set(), []
        for k, v, sp in jobs:
            if sp in seen and v != base.get(k): continue
            seen.add(sp); uniq.append((k, v, sp))
        with ThreadPoolExecutor(max_workers=5) as p:
            res = list(p.map(lambda j: score(j[2]), uniq))
        print(f"  {'dimension':11s} {'value':>7s} {'mean':>7s} {'allF':>5s}  folds")
        held = allf = 0
        for (k, v, sp), r in zip(uniq, res):
            if not r: continue
            m, ex = r
            ok = all(x > 0 for x in ex); allf += ok
            held += m >= 0.20
            star = " *" if v == base.get(k) else "  "
            print(f"  {k:11s} {v:7g} {m:+7.2f} {'YES' if ok else '   ':>5s}  " +
                  " ".join(f"{x:+.2f}" for x in ex) + star)
        n = sum(1 for r in res if r)
        print(f"\n  {held} of {n} neighbours hold >= +0.20 mean; {allf} of {n} also win every fold")

if "4" in WANT:
    hdr(4, "BREADTH  (does it survive outside the four coins it was selected on?)")
    LADDERS = [("2021-02-01", DEV_END, ["BTC","ETH","XRP","LTC","DOGE","TRX","ZEC","BNB","UNI","XMR","LINK","XLM","DASH","AAVE","BCH","JST","ETC","ATOM"], [4,6,8,10,14,18]),
               ("2022-05-01", DEV_END, ["BTC","ETH","XRP","LTC","DOGE","TRX","ADA","SOL","ZEC","BNB","UNI","XMR","LINK","XLM","DASH","AAVE","BCH","JST","ETC","ATOM"], [4,8,12,16,20])]
    rungs = 0; wins = 0
    for st, en, uni, sizes in LADDERS:
        print(f"\n  -- {st}..{en}")
        print(f"  {'coins':>5s} {'candidate':>10s} {'incumbent':>10s} {'excess':>8s} {'cand dd':>8s}")
        for n in sizes:
            r = excess(uni[:n], st, en, *CAND)
            if not r: continue
            d, a, b = r; rungs += 1; wins += d > 0
            print(f"  {n:5d} {a['sharpe']:10.2f} {b['sharpe']:10.2f} {d:+8.2f} {a['dd']:7.1f}%")
    print(f"\n  positive on {wins} of {rungs} universe rungs")
    print("\n  -- per sleeve, 20 coins, 2022-05-01..%s" % DEV_END)
    uni = LADDERS[1][2]
    a = portfolio(uni, "2022-05-01", DEV_END, *CAND)
    b = portfolio(uni, "2022-05-01", DEV_END, *INCUMBENT)
    beat = [c for c in a["sleeves"] if a["sleeves"][c]["sharpe"] > b["sleeves"].get(c, {}).get("sharpe", 9)]
    print(f"  beats the incumbent on {len(beat)} of {len(a['sleeves'])} sleeves individually")
    worst = sorted(a["sleeves"], key=lambda c: a["sleeves"][c]["sharpe"] - b["sleeves"].get(c, {}).get("sharpe", 0))[:4]
    print("  worst sleeves: " + ", ".join(f"{c} {a['sleeves'][c]['sharpe']-b['sleeves'].get(c,{}).get('sharpe',0):+.2f}" for c in worst))

if "5" in WANT:
    hdr(5, "ANCHORED WALK-FORWARD WITH RE-SELECTION  (the honest number)")
    GRIDS = {"fracdiff": [f"d={d},entryZ={ez},exitZ={xz},momentum=1,zWindow={zw}"
                          for d in (0.2, 0.3, 0.4) for ez in (0.6, 0.8, 1.0)
                          for xz in (-1.5, -1.0) for zw in (35, 50, 80)]}
    grid = GRIDS.get(CAND[0])
    if not grid:
        print("  no walk-forward grid defined")
    else:
        WF = [("2020", "2018-01-01", "2019-12-31", "2020-01-01", "2020-12-31"),
              ("2021", "2018-01-01", "2020-12-31", "2021-01-01", "2021-12-31"),
              ("2022", "2018-01-01", "2021-12-31", "2022-01-01", "2022-12-31"),
              ("2023", "2018-01-01", "2022-12-31", "2023-01-01", DEV_END)]
        print(f"  grid: {len(grid)} configs, re-selected on each training window by excess Sharpe")
        print(f"\n  {'test yr':8s} {'selected on the training window':46s} {'test exc':>9s} {'full-sample cfg':>9s}")
        honest, frozen = [], []
        for name, trs, tre, tes, tee in WF:
            with ThreadPoolExecutor(max_workers=5) as p:
                sc = list(p.map(lambda sp: (excess(CORE4, trs, tre, CAND[0], sp) or (None,))[0], grid))
            best = max((s for s in sc if s is not None), default=None)
            pick = grid[sc.index(best)]
            h = excess(CORE4, tes, tee, CAND[0], pick)
            f = excess(CORE4, tes, tee, *CAND)
            if h: honest.append(h[0])
            if f: frozen.append(f[0])
            print(f"  {name:8s} {pick:46s} {h[0]:+9.2f} {f[0]:+9.2f}")
        print(f"\n  HONEST (re-selected each year):   mean {statistics.mean(honest):+.2f}, "
              f"positive in {sum(1 for x in honest if x>0)} of {len(honest)}, worst {min(honest):+.2f}")
        print(f"  frozen full-sample parameters:    mean {statistics.mean(frozen):+.2f}")
        print(f"  MEASURED SELECTION BIAS:          {statistics.mean(frozen)-statistics.mean(honest):+.2f} Sharpe")

if "6" in WANT:
    hdr(6, "THE 2024+ WINDOW  (CONTAMINATED for the incumbent; descriptive only)")
    W8 = ["BTC","ETH","XRP","LTC","DOGE","TRX","ADA","SOL"]
    print("  8 coins, reference sizing vt 0.30 / vol-window 90, real costs 0.125% + 0.05% slippage")
    print(f"\n  {'period':10s} {'candidate':>10s} {'incumbent':>10s} {'excess':>8s} {'cand dd':>8s} {'inc dd':>8s}")
    for nm, s, e in (("2024", "2024-01-01", "2024-12-31"), ("2025", "2025-01-01", "2025-12-31"),
                     ("2026 ytd", "2026-01-01", "2026-09-06"), ("full", "2024-01-01", "2026-09-06")):
        a = portfolio(W8, s, e, *CAND, vt=0.30, vw=90, fee=0.00125, slip=0.0005, dev=False)
        b = portfolio(W8, s, e, *INCUMBENT, vt=0.30, vw=90, fee=0.00125, slip=0.0005, dev=False)
        if a and b:
            print(f"  {nm:10s} {a['sharpe']:10.2f} {b['sharpe']:10.2f} {a['sharpe']-b['sharpe']:+8.2f} {a['dd']:7.1f}% {b['dd']:7.1f}%")

if "7" in WANT:
    hdr(7, "EVERY CALENDAR YEAR THE DATA ALLOWS  (the steelman)")
    print(f"  {'year':8s} {'candidate':>10s} {'incumbent':>10s} {'excess':>8s}")
    gaps = []
    for y in range(2017, 2027):
        s, e = f"{y}-01-01", (f"{y}-12-31" if y < 2026 else "2026-09-06")
        a = portfolio(CORE4, s, e, *CAND, vt=0.30, vw=90, fee=0.00125, slip=0.0005, dev=False)
        b = portfolio(CORE4, s, e, *INCUMBENT, vt=0.30, vw=90, fee=0.00125, slip=0.0005, dev=False)
        if not a or not b: continue
        d = a["sharpe"] - b["sharpe"]; gaps.append((y, d))
        tag = "  <- unseen in development" if y >= 2024 else ""
        print(f"  {y if y<2026 else '2026ytd':8} {a['sharpe']:10.2f} {b['sharpe']:10.2f} {d:+8.2f}{tag}")
    print(f"\n  candidate wins {sum(1 for _,g in gaps if g>0)} of {len(gaps)} years; "
          f"mean {statistics.mean(g for _,g in gaps):+.2f}, worst {min(g for _,g in gaps):+.2f}")
    print(f"  years the INCUMBENT wins: {[y for y,g in gaps if g<=0]}")
    for nm, s, e in (("full 2017-2026", "2017-01-01", "2026-09-06"),
                     ("development 2017-2023", "2017-01-01", DEV_END)):
        a = portfolio(CORE4, s, e, *CAND, vt=0.30, vw=90, fee=0.00125, slip=0.0005, dev=False)
        b = portfolio(CORE4, s, e, *INCUMBENT, vt=0.30, vw=90, fee=0.00125, slip=0.0005, dev=False)
        print(f"  {nm:24s} candidate {a['sharpe']:5.2f} (dd {a['dd']:5.1f}%)  incumbent {b['sharpe']:5.2f} (dd {b['dd']:5.1f}%)  gap {a['sharpe']-b['sharpe']:+.2f}  +/-{a['se']:.2f}")
