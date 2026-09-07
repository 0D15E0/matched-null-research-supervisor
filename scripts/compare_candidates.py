#!/usr/bin/env python3
"""Side-by-side backtest of the loop's all-folds shortlist against the deployed rule.

Runs on the research protocol the ledger used (four coins, 4h, three
chronological folds plus the full development window, 20% vol target, 30-bar
vol window, 600 warm-up bars, equal sleeve weights) so every number here is
directly comparable to what the supervisor recorded.

Nothing touches data after the frozen boundary: every command carries an
explicit --end no later than 2023-12-31, asserted before the binary runs.

  python3 scripts/compare_candidates.py            # protocol universe
  python3 scripts/compare_candidates.py --deployment   # + the 8-coin book, 2021-23
"""
import argparse, json, re, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT.parent / "cli_trader"
BINARY = CLI / "build" / "cli_trader"
DEV_END = "2023-12-31"
ENVS4 = "BTC_USDT:14400,ETH_USDT:14400,XRP_USDT:14400,LTC_USDT:14400"
ENVS8 = ENVS4 + ",DOGE_USDT:14400,TRX_USDT:14400,ADA_USDT:14400,SOL_USDT:14400"
FOLDS = (("2018-01-01", "2019-12-31"), ("2020-01-01", "2021-12-31"), ("2022-01-01", "2023-12-31"))
FULL = ("2018-01-01", DEV_END)
DEPLOY = ("2021-01-01", DEV_END)

# The shortlist: every candidate that beat the deployed rule on ALL THREE folds,
# plus the deployed rule itself as the baseline.
CANDIDATES = [
    ("ensemble_vote (DEPLOYED)", "ensemble_vote", "enterVotes=2,exitVotes=0"),
    ("ehlers_trend",             "ehlers_trend",  "cutoffPeriod=10,entrySigmas=0.9,slopeLag=2,volWindow=30"),
    ("pure_ichimoku",            "pure_ichimoku", ""),
    ("momo_breakout A",          "momo_breakout", "lookback=35,rangeFast=12,rangeSlow=70,stopPct=8,tpPct=18"),
    ("momo_breakout B",          "momo_breakout", "lookback=40,rangeFast=20,rangeSlow=90,stopPct=3,tpPct=11.5"),
]

def portfolio(envs, start, end, strategy, sparams, vol_target=0.20, vol_window=30):
    assert end <= DEV_END, f"refusing to evaluate past the frozen boundary: {end}"
    cmd = [str(BINARY), "portfolio", "--data-dir", str(CLI / "data"), "--envs", envs,
           "--start", start, "--end", end, "--warmup-bars", "600",
           "--strategy", strategy, "--vol-target", str(vol_target), "--vol-window", str(vol_window),
           "--weights", "equal", "--vol-lookback", "120", "--rebalance", "30"]
    if sparams:
        cmd += ["--sparams", sparams]
    out = subprocess.run(cmd, cwd=CLI, text=True, capture_output=True)
    if out.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)}\n{out.stderr.strip()}")
    t = out.stdout
    def grab(pattern, group=1):
        m = re.search(pattern, t, re.M)
        return float(m.group(group)) if m else float("nan")
    trades = sum(int(x) for x in re.findall(
        r"^\s+[A-Z0-9_]+\s+[-+0-9.]+\s+[-+0-9.]+\s+[-+0-9.]+\s+[-+0-9.]+\s+(\d+)", t, re.M))
    return {
        "sharpe": grab(r"^Sharpe \(ann\.\):\s+([-+0-9.]+)"),
        "basket_sharpe": grab(r"^Sharpe \(ann\.\):\s+[-+0-9.]+\s+([-+0-9.]+)"),
        "excess": grab(r"^Sharpe \(ann\.\):\s+[-+0-9.]+\s+[-+0-9.]+\s+([-+0-9.]+)"),
        "se": grab(r"^\s*\+/-\s+([-+0-9.]+)"),
        "cagr": grab(r"^CAGR:\s+([-+0-9.]+)%"),
        "dd": grab(r"^Max drawdown:\s+([-+0-9.]+)%"),
        "corr": grab(r"^Avg pairwise sleeve correlation:\s+([-+0-9.]+)"),
        "trades": trades, "command": cmd,
    }

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--deployment", action="store_true", help="also run the 8-coin book, 2021-23")
    args = ap.parse_args()
    results = {}

    print(f"\n{'='*104}\nFULL DEVELOPMENT WINDOW  {FULL[0]}..{FULL[1]}   4 coins @ 4h, vt 0.20, vol-window 30\n{'='*104}")
    print(f"{'strategy':26s} {'Sharpe':>8s} {'+/-':>6s} {'basket':>7s} {'excess':>7s} {'CAGR':>8s} {'maxDD':>7s} {'trades':>7s} {'corr':>6s}")
    for label, strategy, sparams in CANDIDATES:
        r = portfolio(ENVS4, *FULL, strategy, sparams)
        results[label] = {"full": r, "folds": []}
        print(f"{label:26s} {r['sharpe']:8.2f} {r['se']:6.2f} {r['basket_sharpe']:7.2f} {r['excess']:+7.2f} "
              f"{r['cagr']:7.1f}% {r['dd']:6.1f}% {r['trades']:7d} {r['corr']:6.3f}")

    print(f"\n{'='*104}\nPER FOLD  (Sharpe, and delta against the deployed rule in the same fold)\n{'='*104}")
    deployed_folds = []
    for start, end in FOLDS:
        deployed_folds.append(portfolio(ENVS4, start, end, "ensemble_vote", "enterVotes=2,exitVotes=0"))
    print(f"{'strategy':26s} " + " ".join(f"{a[:7]+'-'+b[2:4]:>18s}" for a, b in FOLDS) + f" {'mean delta':>11s} {'worst':>7s}")
    for label, strategy, sparams in CANDIDATES:
        cells, deltas = [], []
        for i, (start, end) in enumerate(FOLDS):
            r = portfolio(ENVS4, start, end, strategy, sparams)
            results[label]["folds"].append(r)
            d = r["sharpe"] - deployed_folds[i]["sharpe"]
            deltas.append(d)
            cells.append(f"{r['sharpe']:+6.2f} ({d:+5.2f})")
        print(f"{label:26s} " + " ".join(f"{c:>18s}" for c in cells)
              + f" {sum(deltas)/3:+11.2f} {min(deltas):+7.2f}")

    print(f"\n{'='*104}\nRISK-MATCHED CONTROL: the deployed rule de-levered, full window\n"
          f"(this repository's standing requirement - a shallower drawdown is purchasable with the vol target)\n{'='*104}")
    print(f"{'ensemble_vote at':26s} {'Sharpe':>8s} {'CAGR':>8s} {'maxDD':>7s}")
    ladder = {}
    for vt in (0.08, 0.10, 0.12, 0.15, 0.20):
        r = portfolio(ENVS4, *FULL, "ensemble_vote", "enterVotes=2,exitVotes=0", vol_target=vt)
        ladder[vt] = r
        print(f"{'  vol-target %.2f' % vt:26s} {r['sharpe']:8.2f} {r['cagr']:7.1f}% {r['dd']:6.1f}%")
    print(f"\n{'candidate':26s} {'maxDD':>7s} {'Sharpe':>8s} | {'deployed at same risk':>22s} {'Sharpe':>8s}  {'verdict':>9s}")
    for label, _, _ in CANDIDATES[1:]:
        c = results[label]["full"]
        vt, r = min(ladder.items(), key=lambda kv: abs(kv[1]["dd"] - c["dd"]))
        gap = c["sharpe"] - r["sharpe"]
        print(f"{label:26s} {c['dd']:6.1f}% {c['sharpe']:8.2f} | {'vt %.2f (dd %.1f%%)' % (vt, r['dd']):>22s} "
              f"{r['sharpe']:8.2f}  {gap:+9.2f}")

    if args.deployment:
        print(f"\n{'='*104}\nWIDER UNIVERSE (descriptive): 8 coins {DEPLOY[0]}..{DEPLOY[1]}, reference sizing (vt 0.30 / vol-window 90)\n{'='*104}")
        print(f"{'strategy':26s} {'Sharpe':>8s} {'basket':>7s} {'excess':>7s} {'CAGR':>8s} {'maxDD':>7s} {'trades':>7s}")
        for label, strategy, sparams in CANDIDATES:
            r = portfolio(ENVS8, *DEPLOY, strategy, sparams, vol_target=0.30, vol_window=90)
            print(f"{label:26s} {r['sharpe']:8.2f} {r['basket_sharpe']:7.2f} {r['excess']:+7.2f} "
                  f"{r['cagr']:7.1f}% {r['dd']:6.1f}% {r['trades']:7d}")

    out = ROOT / "state" / "candidate_comparison.json"
    out.write_text(json.dumps(results, indent=2, default=str) + "\n")
    print(f"\nwrote {out}")

if __name__ == "__main__":
    sys.exit(main())
