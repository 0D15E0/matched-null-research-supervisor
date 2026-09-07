#!/usr/bin/env python3
"""Does ehlers_trend survive on more coins, and is it carried by a few sleeves?

Addendum 19's rule: every rung of a universe ladder must be measured on ONE
fixed window, or a young coin silently truncates the common grid and the rungs
compare different histories. ADA starts 2022-01-06 and SOL 2021-11-22, so any
rung containing them binds the window. Two ladders here:

  A. all 20 coins available, window 2022-05-01..2023-12-31, which leaves every
     coin (including ADA) a full 600-bar warm-up before the scored period;
  B. no ADA/SOL, window 2021-02-01..2023-12-31, buying a longer history at the
     cost of two coins.

Coins are added in the liquidity-rank order addendum 19 fixed, never by result.
Development data only; every command asserts --end <= 2023-12-31.
"""
import json, re, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT.parent / "cli_trader"
BINARY = CLI / "build" / "cli_trader"
DEV_END = "2023-12-31"
CORE = ["BTC", "ETH", "XRP", "LTC"]
PLUS2 = ["DOGE", "TRX"]
YOUNG = ["ADA", "SOL"]
LIQUIDITY = ["ZEC", "BNB", "UNI", "XMR", "LINK", "XLM", "DASH", "AAVE", "BCH", "JST", "ETC", "ATOM"]

STRATEGIES = [
    ("ensemble_vote (DEPLOYED)", "ensemble_vote", "enterVotes=2,exitVotes=0"),
    ("ehlers_trend (candidate)", "ehlers_trend", "cutoffPeriod=10,entrySigmas=0.9,slopeLag=2,volWindow=30"),
    ("ehlers_trend (neighbour)", "ehlers_trend", "cutoffPeriod=10,entrySigmas=0.8,slopeLag=2,volWindow=30"),
]

def envs(coins):
    return ",".join(f"{c}_USDT:14400" for c in coins)

def portfolio(coins, start, end, strategy, sparams, vt=0.20, vw=30):
    assert end <= DEV_END, f"refusing to evaluate past the frozen boundary: {end}"
    cmd = [str(BINARY), "portfolio", "--data-dir", str(CLI / "data"), "--envs", envs(coins),
           "--start", start, "--end", end, "--warmup-bars", "600", "--strategy", strategy,
           "--vol-target", str(vt), "--vol-window", str(vw), "--weights", "equal",
           "--vol-lookback", "120", "--rebalance", "30"]
    if sparams:
        cmd += ["--sparams", sparams]
    out = subprocess.run(cmd, cwd=CLI, text=True, capture_output=True)
    if out.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)}\n{out.stderr.strip()}")
    t = out.stdout
    def grab(p):
        m = re.search(p, t, re.M)
        return float(m.group(1)) if m else float("nan")
    sleeves = {}
    for m in re.finditer(r"^\s+([A-Z0-9]+)_USDT\s+([-+0-9.]+)\s+([-+0-9.]+)\s+([-+0-9.]+)\s+([-+0-9.]+)\s+(\d+)", t, re.M):
        sleeves[m.group(1)] = {"sharpe": float(m.group(2)), "bh": float(m.group(3)),
                               "excess": float(m.group(4)), "dd": float(m.group(5)), "trades": int(m.group(6))}
    return {"sharpe": grab(r"^Sharpe \(ann\.\):\s+([-+0-9.]+)"),
            "basket": grab(r"^Sharpe \(ann\.\):\s+[-+0-9.]+\s+([-+0-9.]+)"),
            "excess": grab(r"^Sharpe \(ann\.\):\s+[-+0-9.]+\s+[-+0-9.]+\s+([-+0-9.]+)"),
            "cagr": grab(r"^CAGR:\s+([-+0-9.]+)%"), "dd": grab(r"^Max drawdown:\s+([-+0-9.]+)%"),
            "corr": grab(r"^Avg pairwise sleeve correlation:\s+([-+0-9.]+)"),
            "years": grab(r"Common window:\s+\d+ bars, ([-0-9.]+) years"),
            "bars": grab(r"Common window:\s+(\d+) bars"),
            "trades": sum(s["trades"] for s in sleeves.values()), "sleeves": sleeves}

def ladder(label, rungs, start, end):
    print(f"\n{'='*112}\n{label}   window {start}..{end}\n{'='*112}")
    print(f"{'coins':>6s} {'strategy':26s} {'Sharpe':>7s} {'basket':>7s} {'excess':>7s} {'CAGR':>7s} "
          f"{'maxDD':>7s} {'corr':>6s} {'trades':>7s} {'years':>6s}   delta vs deployed")
    table = {}
    for coins in rungs:
        base = None
        for name, strategy, sparams in STRATEGIES:
            r = portfolio(coins, start, end, strategy, sparams)
            if base is None:
                base = r["sharpe"]
            delta = r["sharpe"] - base
            table.setdefault(len(coins), {})[name] = r
            tag = "" if name.startswith("ensemble") else f"{delta:+.2f}"
            print(f"{len(coins):>6d} {name:26s} {r['sharpe']:7.2f} {r['basket']:7.2f} {r['excess']:+7.2f} "
                  f"{r['cagr']:6.1f}% {r['dd']:6.1f}% {r['corr']:6.3f} {r['trades']:7d} {r['years']:6.2f}   {tag:>8s}")
        print()
    return table

FULL20 = CORE + PLUS2 + YOUNG + LIQUIDITY
rungs_a = [CORE, CORE+PLUS2, CORE+PLUS2+YOUNG,
           CORE+PLUS2+YOUNG+LIQUIDITY[:4], CORE+PLUS2+YOUNG+LIQUIDITY[:8], FULL20]
a = ladder("LADDER A: all coins, fixed window with full warm-up for the youngest (ADA)",
           rungs_a, "2022-05-01", DEV_END)

NOYOUNG = CORE + PLUS2 + LIQUIDITY
rungs_b = [CORE, CORE+PLUS2, CORE+PLUS2+LIQUIDITY[:4], CORE+PLUS2+LIQUIDITY[:8], NOYOUNG]
b = ladder("LADDER B: no ADA/SOL, longer window", rungs_b, "2021-02-01", DEV_END)

print(f"\n{'='*112}\nPER-SLEEVE on all 20 coins (2022-05-01..{DEV_END}): where does the edge come from?\n{'='*112}")
dep = portfolio(FULL20, "2022-05-01", DEV_END, "ensemble_vote", "enterVotes=2,exitVotes=0")
cand = portfolio(FULL20, "2022-05-01", DEV_END, "ehlers_trend", "cutoffPeriod=10,entrySigmas=0.9,slopeLag=2,volWindow=30")
print(f"{'coin':6s} {'deployed':>9s} {'ehlers':>9s} {'delta':>7s} {'ehlers dd':>10s} {'trades':>7s}")
wins = 0
for coin in FULL20:
    d, c = dep["sleeves"].get(coin), cand["sleeves"].get(coin)
    if not d or not c:
        continue
    delta = c["sharpe"] - d["sharpe"]
    wins += delta > 0
    print(f"{coin:6s} {d['sharpe']:9.2f} {c['sharpe']:9.2f} {delta:+7.2f} {c['dd']:9.1f}% {c['trades']:7d}")
print(f"\n  ehlers_trend beats the deployed rule on {wins} of {len(dep['sleeves'])} sleeves individually")
out = ROOT / "state" / "ehlers_breadth_check.json"
out.write_text(json.dumps({"ladder_a": a, "ladder_b": b,
                           "per_sleeve": {"deployed": dep["sleeves"], "ehlers": cand["sleeves"]}},
                          indent=2, default=str) + "\n")
print(f"\nwrote {out}")
