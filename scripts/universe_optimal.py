#!/usr/bin/env python3
"""Which universe size is optimal for ehlers_trend, at MATCHED RISK?

Comparing universes at a fixed vol target is not a fair comparison: a wider
universe has a lower drawdown, so it is running less risk and could be levered
back up. The honest question is which universe delivers the most return at the
SAME drawdown. That is this repository's standing control applied to breadth
instead of to a rule.

Also asks whether any apparent peak is stable across sub-periods, because a
peak that moves between windows is noise.

Development data only; every command asserts --end <= 2023-12-31.
"""
import json, re, statistics, subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT.parent / "cli_trader"
BINARY = CLI / "build" / "cli_trader"
DEV_END = "2023-12-31"
EHLERS = ("ehlers_trend", "cutoffPeriod=10,entrySigmas=0.9,slopeLag=2,volWindow=30")
U18 = ["BTC","ETH","XRP","LTC","DOGE","TRX","ZEC","BNB","UNI","XMR","LINK","XLM","DASH","AAVE","BCH","JST","ETC","ATOM"]
WINDOW = ("2021-02-01", DEV_END)
SUBWINDOWS = [("2021-02-01","2021-12-31"), ("2022-01-01","2022-12-31"), ("2023-01-01","2023-12-31")]
VOL_TARGETS = [0.15, 0.20, 0.25, 0.30, 0.40, 0.50]
TARGET_DD = 15.0

def portfolio(coins, start, end, vt):
    assert end <= DEV_END
    cmd = [str(BINARY), "portfolio", "--data-dir", str(CLI/"data"),
           "--envs", ",".join(f"{c}_USDT:14400" for c in coins), "--start", start, "--end", end,
           "--warmup-bars", "600", "--strategy", EHLERS[0], "--sparams", EHLERS[1],
           "--vol-target", str(vt), "--vol-window", "90", "--weights", "equal",
           "--vol-lookback", "120", "--rebalance", "30"]
    out = subprocess.run(cmd, cwd=CLI, text=True, capture_output=True)
    if out.returncode != 0: return None
    g = lambda p: (lambda m: float(m.group(1)) if m else float("nan"))(re.search(p, out.stdout, re.M))
    return {"sharpe": g(r"^Sharpe \(ann\.\):\s+([-+0-9.]+)"), "cagr": g(r"^CAGR:\s+([-+0-9.]+)%"),
            "dd": g(r"^Max drawdown:\s+([-+0-9.]+)%"), "se": g(r"^\s*\+/-\s+([-+0-9.]+)")}

SIZES = [4, 6, 8, 10, 12, 14, 16, 18]
print("=" * 100)
print(f"1. MATCHED RISK: what does each universe return at ~{TARGET_DD:.0f}% drawdown?   {WINDOW[0]}..{WINDOW[1]}")
print("=" * 100)
print(f"  {'coins':>5s} | {'vol ladder (drawdown% -> CAGR%)':48s} | {'vt at ~15% DD':>13s} {'CAGR there':>11s} {'Sharpe':>7s}")
matched = {}
for n in SIZES:
    coins = U18[:n]
    with ThreadPoolExecutor(max_workers=6) as pool:
        ladder = {vt: r for vt, r in zip(VOL_TARGETS, pool.map(lambda vt: portfolio(coins, *WINDOW, vt), VOL_TARGETS)) if r}
    shown = " ".join(f"{r['dd']:.0f}->{r['cagr']:.0f}" for r in ladder.values())
    vt, best = min(ladder.items(), key=lambda kv: abs(kv[1]["dd"] - TARGET_DD))
    matched[n] = {"vt": vt, **best}
    print(f"  {n:>5d} | {shown:48s} | {vt:13.2f} {best['cagr']:10.1f}% {best['sharpe']:7.2f}  (dd {best['dd']:.1f}%)")
best_n = max(matched, key=lambda n: matched[n]["cagr"])
spread = max(m["cagr"] for m in matched.values()) - min(m["cagr"] for m in matched.values())
print(f"\n  best at matched risk: {best_n} coins ({matched[best_n]['cagr']:.1f}% CAGR at {matched[best_n]['dd']:.1f}% DD)")
print(f"  spread across all sizes: {spread:.1f} percentage points of CAGR")

print("\n" + "=" * 100)
print("2. IS ANY PEAK STABLE?  same ladder, one sub-period at a time, at the reference vol target 0.30")
print("=" * 100)
print(f"  {'coins':>5s} | " + " ".join(f"{a[:4]:>16s}" for a, _ in SUBWINDOWS) + f" | {'rank stability':>14s}")
rows = {}
for n in SIZES:
    coins = U18[:n]
    rs = [portfolio(coins, s, e, 0.30) for s, e in SUBWINDOWS]
    rows[n] = [r["sharpe"] if r else float("nan") for r in rs]
    print(f"  {n:>5d} | " + " ".join(f"{v:16.2f}" for v in rows[n]))
print()
for i, (s, e) in enumerate(SUBWINDOWS):
    order = sorted(SIZES, key=lambda n: -rows[n][i])
    print(f"  {s[:4]} best-to-worst universe size: {order}")
print(f"\n  a stable optimum would put the same size first every year.")

print("\n" + "=" * 100)
print("3. POWER: can this window tell these universes apart at all?")
print("=" * 100)
r8, r18 = matched[8], matched[18]
se = portfolio(U18[:8], *WINDOW, 0.30)["se"]
print(f"  Sharpe standard error on this 2.9-year window: {se:.2f}")
print(f"  largest Sharpe gap between any two sizes at matched risk: "
      f"{max(m['sharpe'] for m in matched.values()) - min(m['sharpe'] for m in matched.values()):.2f}")
print(f"  -> the entire spread is {(max(m['sharpe'] for m in matched.values()) - min(m['sharpe'] for m in matched.values()))/se:.1f} standard errors")
(ROOT/"state"/"universe_optimal.json").write_text(json.dumps({"matched": matched, "subwindows": rows}, indent=2)+"\n")
