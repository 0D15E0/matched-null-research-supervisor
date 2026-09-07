#!/usr/bin/env python3
"""How many coins should this book trade? Absolute performance, not delta.

Yesterday's breadth check reported the GAP against the deployed rule widening
as coins were added. That is the wrong statistic for choosing a universe: a
widening gap can mean the candidate improved or that the incumbent degraded
faster. To choose how many coins to trade you want the candidate's own
risk-adjusted return.

Three questions, in order:
  A  absolute performance by universe size, on one fixed window per ladder
  B  is any effect about the COUNT or about WHICH coins? random subsets of each
     size, so count and selection are separated
  C  a rule you could actually run forward: rank coins by trailing standalone
     Sharpe on a training window, trade the top K on the window that follows

Development data only; every command asserts --end <= 2023-12-31.
"""
import json, random, re, statistics, subprocess, sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT.parent / "cli_trader"
BINARY = CLI / "build" / "cli_trader"
DEV_END = "2023-12-31"
EHLERS = ("ehlers_trend", "cutoffPeriod=10,entrySigmas=0.9,slopeLag=2,volWindow=30")
DEPLOYED = ("ensemble_vote", "enterVotes=2,exitVotes=0")
CORE = ["BTC", "ETH", "XRP", "LTC"]
PLUS2 = ["DOGE", "TRX"]
YOUNG = ["ADA", "SOL"]
LIQ = ["ZEC", "BNB", "UNI", "XMR", "LINK", "XLM", "DASH", "AAVE", "BCH", "JST", "ETC", "ATOM"]
U18 = CORE + PLUS2 + LIQ                    # 2.91-year window
U20 = CORE + PLUS2 + YOUNG + LIQ            # 1.67-year window
WIN18 = ("2021-02-01", DEV_END)
WIN20 = ("2022-05-01", DEV_END)

def portfolio(coins, start, end, strategy, sparams, vt=0.30, vw=90):
    assert end <= DEV_END
    cmd = [str(BINARY), "portfolio", "--data-dir", str(CLI / "data"),
           "--envs", ",".join(f"{c}_USDT:14400" for c in coins), "--start", start, "--end", end,
           "--warmup-bars", "600", "--strategy", strategy, "--vol-target", str(vt),
           "--vol-window", str(vw), "--weights", "equal", "--vol-lookback", "120", "--rebalance", "30"]
    if sparams:
        cmd += ["--sparams", sparams]
    out = subprocess.run(cmd, cwd=CLI, text=True, capture_output=True)
    if out.returncode != 0:
        return None
    t = out.stdout
    def grab(p):
        m = re.search(p, t, re.M)
        return float(m.group(1)) if m else float("nan")
    sleeves = {m.group(1): float(m.group(2)) for m in re.finditer(
        r"^\s+([A-Z0-9]+)_USDT\s+([-+0-9.]+)\s+", t, re.M)}
    return {"sharpe": grab(r"^Sharpe \(ann\.\):\s+([-+0-9.]+)"),
            "basket": grab(r"^Sharpe \(ann\.\):\s+[-+0-9.]+\s+([-+0-9.]+)"),
            "cagr": grab(r"^CAGR:\s+([-+0-9.]+)%"), "dd": grab(r"^Max drawdown:\s+([-+0-9.]+)%"),
            "corr": grab(r"^Avg pairwise sleeve correlation:\s+([-+0-9.]+)"),
            "se": grab(r"^\s*\+/-\s+([-+0-9.]+)"), "sleeves": sleeves}

print("=" * 104)
print("A. ABSOLUTE PERFORMANCE BY UNIVERSE SIZE   (reference sizing: vt 0.30, vol-window 90)")
print("=" * 104)
for label, uni, (st, en) in (("18 coins available, 2.9-year window", U18, WIN18),
                             ("20 coins available, 1.7-year window", U20, WIN20)):
    print(f"\n-- {label}: {st}..{en}")
    print(f"  {'coins':>5s} | {'ehlers Sharpe':>13s} {'CAGR':>7s} {'maxDD':>7s} {'Sharpe/DD':>10s} {'corr':>6s} "
          f"| {'deployed':>9s} {'delta':>6s}")
    rungs = [uni[:n] for n in (4, 6, 8, 10, 12, 14, 16, len(uni))]
    for coins in rungs:
        e = portfolio(coins, st, en, *EHLERS)
        d = portfolio(coins, st, en, *DEPLOYED)
        if not e or not d:
            continue
        print(f"  {len(coins):>5d} | {e['sharpe']:13.2f} {e['cagr']:6.1f}% {e['dd']:6.1f}% "
              f"{e['sharpe']/e['dd']*100:10.3f} {e['corr']:6.3f} | {d['sharpe']:9.2f} {e['sharpe']-d['sharpe']:+6.2f}")
    print(f"  (Sharpe standard error on this window: {e['se']:.2f})")

print("\n" + "=" * 104)
print("B. IS IT THE COUNT OR THE COINS?  random subsets of the 18-coin universe, 2.9-year window")
print("=" * 104)
rng = random.Random(20260906)
print(f"  {'K':>3s} {'draws':>6s} | {'ehlers Sharpe: mean':>19s} {'median':>7s} {'min':>6s} {'max':>6s} "
      f"| {'mean maxDD':>10s} | {'first-K rung':>12s}")
subset_results = {}
for k in (4, 6, 8, 10, 12, 14, 18):
    draws = [U18[:]] if k == 18 else [rng.sample(U18, k) for _ in range(12)]
    with ThreadPoolExecutor(max_workers=6) as pool:
        rs = [r for r in pool.map(lambda c: portfolio(c, *WIN18, *EHLERS), draws) if r]
    sh = [r["sharpe"] for r in rs]; dd = [r["dd"] for r in rs]
    subset_results[k] = sh
    first = portfolio(U18[:k], *WIN18, *EHLERS)
    print(f"  {k:>3d} {len(rs):>6d} | {statistics.mean(sh):19.2f} {statistics.median(sh):7.2f} "
          f"{min(sh):6.2f} {max(sh):6.2f} | {statistics.mean(dd):9.1f}% | {first['sharpe']:12.2f}")

print("\n" + "=" * 104)
print("C. A RULE YOU COULD RUN FORWARD: trade the top-K coins by TRAILING standalone Sharpe")
print("=" * 104)
TRAIN = ("2021-02-01", "2022-12-31")
TEST = ("2023-01-01", DEV_END)
train = portfolio(U18, *TRAIN, *EHLERS)
ranked = sorted(train["sleeves"], key=lambda c: -train["sleeves"][c])
print(f"  training {TRAIN[0]}..{TRAIN[1]} ranks: " + ", ".join(f"{c}({train['sleeves'][c]:+.2f})" for c in ranked[:8]) + " ...")
print(f"\n  {'K':>3s} | {'test Sharpe':>11s} {'CAGR':>7s} {'maxDD':>7s} | selected")
for k in (4, 6, 8, 10, 12, 18):
    sel = ranked[:k]
    r = portfolio(sel, *TEST, *EHLERS)
    print(f"  {k:>3d} | {r['sharpe']:11.2f} {r['cagr']:6.1f}% {r['dd']:6.1f}% | {' '.join(sel)}")
liveish = portfolio(CORE + PLUS2, *TEST, *EHLERS)
print(f"\n  for reference, the fixed 6 the reference universe would use (no ADA/SOL in this window): "
      f"Sharpe {liveish['sharpe']:.2f}, dd {liveish['dd']:.1f}%")
dep = portfolio(CORE + PLUS2, *TEST, *DEPLOYED)
print(f"  the deployed rule on those same 6 over the test window:                    "
      f"Sharpe {dep['sharpe']:.2f}, dd {dep['dd']:.1f}%")
(ROOT / "state" / "universe_size.json").write_text(json.dumps({"subsets": subset_results}, indent=2) + "\n")
