#!/usr/bin/env python3
"""The case FOR the incumbent. Deliberately adversarial to ehlers_trend.

Everything so far has been chosen or tuned on windows that favour the
candidate. This asks the opposite questions: over the longest history
available, in how many years does each rule actually win, how much of the
candidate's edge is concentrated in a few sleeves, and how fragile is it
compared with the incumbent?
"""
import json, re, statistics, subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT.parent / "cli_trader"
BINARY = CLI / "build" / "cli_trader"
CORE4 = ["BTC", "ETH", "XRP", "LTC"]          # history back to 2016
DEPLOYED = ("ensemble_vote", "enterVotes=2,exitVotes=0")
EHLERS = ("ehlers_trend", "cutoffPeriod=10,entrySigmas=0.9,slopeLag=2,volWindow=30")

def portfolio(coins, start, end, strategy, sparams, vt=0.30, vw=90):
    cmd = [str(BINARY), "portfolio", "--data-dir", str(CLI/"data"),
           "--envs", ",".join(f"{c}_USDT:14400" for c in coins), "--start", start, "--end", end,
           "--warmup-bars", "600", "--strategy", strategy, "--vol-target", str(vt),
           "--vol-window", str(vw), "--weights", "equal", "--vol-lookback", "120",
           "--rebalance", "30", "--fee", "0.00125", "--slippage", "0.0005"]
    if sparams: cmd += ["--sparams", sparams]
    o = subprocess.run(cmd, cwd=CLI, text=True, capture_output=True)
    if o.returncode != 0: return None
    g = lambda p: (lambda m: float(m.group(1)) if m else float("nan"))(re.search(p, o.stdout, re.M))
    return {"sharpe": g(r"^Sharpe \(ann\.\):\s+([-+0-9.]+)"), "cagr": g(r"^CAGR:\s+([-+0-9.]+)%"),
            "dd": g(r"^Max drawdown:\s+([-+0-9.]+)%"), "se": g(r"^\s*\+/-\s+([-+0-9.]+)")}

print("=" * 96)
print("1. EVERY CALENDAR YEAR the data allows, 4 coins with the longest history, reference sizing")
print("=" * 96)
print(f"{'year':8s} | {'deployed':>26s} | {'ehlers':>26s} | {'gap':>6s}")
print(f"{'':8s} | {'Sharpe':>8s} {'CAGR':>8s} {'maxDD':>8s} | {'Sharpe':>8s} {'CAGR':>8s} {'maxDD':>8s} |")
gaps = []
for y in range(2017, 2027):
    s, e = f"{y}-01-01", (f"{y}-12-31" if y < 2026 else "2026-09-06")
    a = portfolio(CORE4, s, e, *DEPLOYED); c = portfolio(CORE4, s, e, *EHLERS)
    if not a or not c: continue
    gap = c["sharpe"] - a["sharpe"]; gaps.append((y, gap))
    tag = "" if y < 2024 else "  <- unseen by ehlers"
    print(f"{y if y<2026 else '2026ytd':8} | {a['sharpe']:8.2f} {a['cagr']:7.1f}% {a['dd']:7.1f}% | "
          f"{c['sharpe']:8.2f} {c['cagr']:7.1f}% {c['dd']:7.1f}% | {gap:+6.2f}{tag}")
wins = sum(1 for _, g in gaps if g > 0)
print(f"\n  ehlers wins {wins} of {len(gaps)} calendar years; mean gap {statistics.mean(g for _, g in gaps):+.2f}, "
      f"worst {min(g for _, g in gaps):+.2f}, best {max(g for _, g in gaps):+.2f}")
print(f"  years the INCUMBENT wins: {[y for y, g in gaps if g <= 0]}")

print(f"\n{'=' * 96}\n2. THE WHOLE HISTORY AS ONE WINDOW (no year boundaries to cherry-pick)\n{'=' * 96}")
for label, s, e in (("full 2017-2026", "2017-01-01", "2026-09-06"),
                    ("development only 2017-2023", "2017-01-01", "2023-12-31"),
                    ("forward 2024-2026", "2024-01-01", "2026-09-06")):
    a = portfolio(CORE4, s, e, *DEPLOYED); c = portfolio(CORE4, s, e, *EHLERS)
    print(f"  {label:28s} deployed {a['sharpe']:5.2f} (dd {a['dd']:5.1f}%)  |  "
          f"ehlers {c['sharpe']:5.2f} (dd {c['dd']:5.1f}%)  |  gap {c['sharpe']-a['sharpe']:+.2f}  +/-{a['se']:.2f}")

print(f"\n{'=' * 96}\n3. FRAGILITY: what happens to each rule if its parameters are slightly wrong?\n{'=' * 96}")
print("  ehlers_trend, one step off in each direction (2017-2026, 4 coins):")
base = portfolio(CORE4, "2017-01-01", "2026-09-06", *EHLERS)["sharpe"]
for name, vals in (("cutoffPeriod", [7, 8, 10, 12, 15]), ("slopeLag", [1, 2, 3, 5]),
                   ("entrySigmas", [0.7, 0.8, 0.9, 1.0]), ("volWindow", [20, 30, 60])):
    out = []
    for v in vals:
        p = dict(cutoffPeriod=10, entrySigmas=0.9, slopeLag=2, volWindow=30); p[name] = v
        sp = ",".join(f"{k}={p[k]:g}" for k in sorted(p))
        r = portfolio(CORE4, "2017-01-01", "2026-09-06", "ehlers_trend", sp)
        out.append(f"{v:g}:{r['sharpe']:.2f}" + ("*" if v == dict(cutoffPeriod=10, entrySigmas=0.9, slopeLag=2, volWindow=30)[name] else ""))
    print(f"    {name:14s} {'  '.join(out)}")
print("\n  ensemble_vote, every legal vote setting (its whole parameter space is 6 points):")
out = []
for a_ in (1, 2, 3):
    for b_ in (0, 1, 2):
        if b_ >= a_: continue
        r = portfolio(CORE4, "2017-01-01", "2026-09-06", "ensemble_vote", f"enterVotes={a_},exitVotes={b_}")
        out.append(f"{a_}/{b_}:{r['sharpe']:.2f}" + ("*" if (a_, b_) == (2, 0) else ""))
print(f"    {'  '.join(out)}")
