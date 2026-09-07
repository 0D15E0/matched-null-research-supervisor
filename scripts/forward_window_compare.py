#!/usr/bin/env python3
"""ensemble_vote (deployed) vs ehlers_trend (candidate) on 2024-01-01 onward.

WHAT THIS WINDOW IS. Not a clean holdout. experiments/holdout.json records
2024+ as contaminated for all 24 registry families since 2026-08-24: twelve
tournament runs (~53,000 genomes) used holdout splits that reach into it, and
addendum 15 read it directly when choosing the live exit rule. So for
ensemble_vote 2/0 this is DESCRIPTIVE, a window whose answer was already seen.

The asymmetry worth stating: the ehlers PARAMETER SET here came from the local
research loop, which is hard-bounded at 2023-12-31 and has never evaluated a
bar after it. The family was searched on contaminated windows; this point in it
was not. So the comparison flatters the incumbent if anything, and running it
spends the window for this candidate too.

Costs are the account's real ones: 0.125% per side (the venue's /feeinfo
reports 0.12% taker for this account) plus 0.05% slippage, the same figures the
a real account pays. A fee sweep shows how much of any gap is friction.
"""
import json, re, statistics, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT.parent / "cli_trader"
BINARY = CLI / "build" / "cli_trader"
WIDE8 = ["BTC", "ETH", "XRP", "LTC", "DOGE", "TRX", "ADA", "SOL"]
CORE4 = ["BTC", "ETH", "XRP", "LTC"]
START, END = "2024-01-01", "2026-09-06"
YEARS = [("2024", "2024-01-01", "2024-12-31"), ("2025", "2025-01-01", "2025-12-31"),
         ("2026 ytd", "2026-01-01", END)]
DEPLOYED = ("ensemble_vote", "enterVotes=2,exitVotes=0")
EHLERS = ("ehlers_trend", "cutoffPeriod=10,entrySigmas=0.9,slopeLag=2,volWindow=30")

def portfolio(coins, start, end, strategy, sparams, vt=0.30, vw=90, fee=0.00125, slip=0.0005):
    cmd = [str(BINARY), "portfolio", "--data-dir", str(CLI / "data"),
           "--envs", ",".join(f"{c}_USDT:14400" for c in coins), "--start", start, "--end", end,
           "--warmup-bars", "600", "--strategy", strategy, "--vol-target", str(vt),
           "--vol-window", str(vw), "--weights", "equal", "--vol-lookback", "120",
           "--rebalance", "30", "--fee", str(fee), "--slippage", str(slip)]
    if sparams:
        cmd += ["--sparams", sparams]
    out = subprocess.run(cmd, cwd=CLI, text=True, capture_output=True)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip()[:300])
    t = out.stdout
    g = lambda p: (lambda m: float(m.group(1)) if m else float("nan"))(re.search(p, t, re.M))
    sleeves = {m.group(1): {"sharpe": float(m.group(2)), "bh": float(m.group(3)),
                            "dd": float(m.group(5)), "trades": int(m.group(6))}
               for m in re.finditer(r"^\s+([A-Z0-9]+)_USDT\s+([-+0-9.]+)\s+([-+0-9.]+)\s+([-+0-9.]+)\s+([-+0-9.]+)\s+(\d+)", t, re.M)}
    return {"sharpe": g(r"^Sharpe \(ann\.\):\s+([-+0-9.]+)"),
            "basket": g(r"^Sharpe \(ann\.\):\s+[-+0-9.]+\s+([-+0-9.]+)"),
            "excess": g(r"^Sharpe \(ann\.\):\s+[-+0-9.]+\s+[-+0-9.]+\s+([-+0-9.]+)"),
            "se": g(r"^\s*\+/-\s+([-+0-9.]+)"), "cagr": g(r"^CAGR:\s+([-+0-9.]+)%"),
            "total": g(r"^Total return:\s+([-+0-9.]+)%"),
            "basket_total": g(r"^Total return:\s+[-+0-9.]+%\s+([-+0-9.]+)%"),
            "sortino": g(r"^Sortino \(ann\.\):\s+([-+0-9.]+)"),
            "dd": g(r"^Max drawdown:\s+([-+0-9.]+)%"),
            "basket_dd": g(r"^Max drawdown:\s+[-+0-9.]+%\s+([-+0-9.]+)%"),
            "corr": g(r"^Avg pairwise sleeve correlation:\s+([-+0-9.]+)"),
            "years": g(r"Common window:\s+\d+ bars, ([-0-9.]+) years"),
            "trades": sum(s["trades"] for s in sleeves.values()), "sleeves": sleeves}

def per_coin_fees(coins, start, end, strategy, sparams, fee=0.00125, slip=0.0005):
    """Fees are only reported by the single-instrument path, so sum them there."""
    total_fees = total_trades = 0.0
    tim = []
    for c in coins:
        cmd = [str(BINARY), "backtest", "--symbol", f"{c}_USDT", "--period", "14400",
               "--data-dir", str(CLI / "data"), "--start", start, "--end", end,
               "--strategy", strategy, "--vol-target", "0.30", "--vol-window", "90",
               "--fee", str(fee), "--slippage", str(slip), "--equity", str(SLEEVE_EQUITY)]
        if sparams:
            cmd += ["--sparams", sparams]
        o = subprocess.run(cmd, cwd=CLI, text=True, capture_output=True).stdout
        g = lambda p: (lambda m: float(m.group(1)) if m else 0.0)(re.search(p, o, re.M))
        total_fees += g(r"^Fees paid:\s+([-+0-9.]+)")
        total_trades += g(r"^Num trades:\s+([0-9]+)")
        tim.append(g(r"^Time in market:\s+([-+0-9.]+)%"))
    return total_fees, int(total_trades), statistics.mean(tim)

def row(label, r):
    return (f"{label:26s} {r['sharpe']:7.2f} {r['excess']:+7.2f} {r['total']:8.1f}% {r['cagr']:7.1f}% "
            f"{r['dd']:7.1f}% {r['sortino']:8.2f} {r['corr']:6.3f} {r['trades']:7d}")

print("=" * 112)
print(f"FORWARD WINDOW {START} .. {END}   8-coin reference universe, reference sizing (vt 0.30 / vol-window 90)")
print("real costs: 0.125% fee per side + 0.05% slippage")
print("=" * 112)
print(f"{'strategy':26s} {'Sharpe':>7s} {'excess':>7s} {'total':>9s} {'CAGR':>8s} {'maxDD':>8s} {'Sortino':>8s} {'corr':>6s} {'trades':>7s}")
res = {}
for label, (s, sp) in (("ensemble_vote (DEPLOYED)", DEPLOYED), ("ehlers_trend (candidate)", EHLERS)):
    res[label] = portfolio(WIDE8, START, END, s, sp)
    print(row(label, res[label]))
b = res["ensemble_vote (DEPLOYED)"]
print(f"{'buy & hold basket':26s} {b['basket']:7.2f} {'':>7s} {b['basket_total']:8.1f}% {'':>8s} {b['basket_dd']:7.1f}%")
print(f"\n  window: {b['years']:.2f} years   Sharpe standard error: +/-{b['se']:.2f}")
d = res['ehlers_trend (candidate)']['sharpe'] - b['sharpe']
print(f"  ehlers minus deployed: {d:+.2f} Sharpe  ({abs(d)/b['se']:.2f} standard errors)")

print(f"\n{'=' * 112}\nFEES AND TURNOVER (summed over the 8 sleeves)\n{'=' * 112}")
print(f"{'strategy':26s} {'trades':>7s} {'fees $':>9s} {'fees % of book':>15s} {'time in market':>15s}")
for label, (s, sp) in (("ensemble_vote (DEPLOYED)", DEPLOYED), ("ehlers_trend (candidate)", EHLERS)):
    f, t, tim = per_coin_fees(WIDE8, START, END, s, sp)
    print(f"{label:26s} {t:7d} {f:9.2f} {f/(SLEEVE_EQUITY*8)*100:14.2f}% {tim:14.1f}%")

print(f"\n{'=' * 112}\nFEE SENSITIVITY: how much of the gap is friction?\n{'=' * 112}")
print(f"{'fee per side':>13s} | {'deployed Sharpe':>15s} {'ehlers Sharpe':>14s} {'gap':>7s}")
for fee in (0.0, 0.000625, 0.00125, 0.0025, 0.005):
    a = portfolio(WIDE8, START, END, *DEPLOYED, fee=fee)
    e = portfolio(WIDE8, START, END, *EHLERS, fee=fee)
    print(f"{fee*100:12.4f}% | {a['sharpe']:15.2f} {e['sharpe']:14.2f} {e['sharpe']-a['sharpe']:+7.2f}")

print(f"\n{'=' * 112}\nYEAR BY YEAR (is any edge consistent, or one good stretch?)\n{'=' * 112}")
print(f"{'period':10s} | {'deployed':>28s} | {'ehlers':>28s} | {'gap':>6s}")
print(f"{'':10s} | {'Sharpe':>8s} {'CAGR':>9s} {'maxDD':>9s} | {'Sharpe':>8s} {'CAGR':>9s} {'maxDD':>9s} |")
for name, s, e in YEARS:
    a = portfolio(WIDE8, s, e, *DEPLOYED)
    c = portfolio(WIDE8, s, e, *EHLERS)
    print(f"{name:10s} | {a['sharpe']:8.2f} {a['cagr']:8.1f}% {a['dd']:8.1f}% | "
          f"{c['sharpe']:8.2f} {c['cagr']:8.1f}% {c['dd']:8.1f}% | {c['sharpe']-a['sharpe']:+6.2f}")

print(f"\n{'=' * 112}\nRISK-MATCHED: the deployed rule de-levered to the candidate's drawdown\n{'=' * 112}")
ladder = {vt: portfolio(WIDE8, START, END, *DEPLOYED, vt=vt) for vt in (0.15, 0.20, 0.25, 0.30, 0.40)}
for vt, r in ladder.items():
    print(f"  deployed at vol-target {vt:.2f}: Sharpe {r['sharpe']:5.2f}  CAGR {r['cagr']:6.1f}%  dd {r['dd']:5.1f}%")
c = res["ehlers_trend (candidate)"]
vt, m = min(ladder.items(), key=lambda kv: abs(kv[1]["dd"] - c["dd"]))
print(f"\n  candidate: dd {c['dd']:.1f}%, Sharpe {c['sharpe']:.2f}, CAGR {c['cagr']:.1f}%")
print(f"  deployed at the same risk (vt {vt:.2f}, dd {m['dd']:.1f}%): Sharpe {m['sharpe']:.2f}, CAGR {m['cagr']:.1f}%")
print(f"  -> risk-matched gap: {c['sharpe']-m['sharpe']:+.2f} Sharpe, {c['cagr']-m['cagr']:+.1f} pp CAGR")

print(f"\n{'=' * 112}\nPER SLEEVE\n{'=' * 112}")
print(f"{'coin':6s} {'deployed':>9s} {'ehlers':>9s} {'gap':>7s} | {'dep dd':>7s} {'ehl dd':>7s} | {'dep trades':>11s} {'ehl trades':>11s}")
wins = 0
for c2 in WIDE8:
    a = res["ensemble_vote (DEPLOYED)"]["sleeves"].get(c2)
    e = res["ehlers_trend (candidate)"]["sleeves"].get(c2)
    if not a or not e: continue
    gap = e["sharpe"] - a["sharpe"]; wins += gap > 0
    print(f"{c2:6s} {a['sharpe']:9.2f} {e['sharpe']:9.2f} {gap:+7.2f} | {a['dd']:6.1f}% {e['dd']:6.1f}% | {a['trades']:11d} {e['trades']:11d}")
print(f"\n  ehlers beats the deployed rule on {wins} of 8 sleeves")
(ROOT/"state"/"forward_window_compare.json").write_text(json.dumps({k: {kk: vv for kk, vv in v.items() if kk != 'sleeves'} for k, v in res.items()}, indent=2)+"\n")
