#!/usr/bin/env python3
"""Parameter-neighbour robustness check: is a candidate a plateau or a spike?

This repository's own standard, from the member-parameter sweep in
PROFITABILITY_PLAN addendum 16: a real effect sits on a flat region of
parameter space, so its immediate neighbours score close to it. A result whose
neighbours collapse is an artifact of the search, not a property of the market.

Varies one parameter at a time around the candidate, holding the rest fixed,
and reports each variant's mean Sharpe delta against the deployed rule over the
three development folds. Development data only; every command carries an
explicit --end no later than 2023-12-31.

  python3 scripts/neighbour_check.py
"""
import json, re, statistics, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT.parent / "cli_trader"
BINARY = CLI / "build" / "cli_trader"
DEV_END = "2023-12-31"
ENVS4 = "BTC_USDT:14400,ETH_USDT:14400,XRP_USDT:14400,LTC_USDT:14400"
ENVS8 = ENVS4 + ",DOGE_USDT:14400,TRX_USDT:14400,ADA_USDT:14400,SOL_USDT:14400"
FOLDS = (("2018-01-01", "2019-12-31"), ("2020-01-01", "2021-12-31"), ("2022-01-01", "2023-12-31"))
FULL = ("2018-01-01", DEV_END)

STRATEGY = "ehlers_trend"
BASE = {"cutoffPeriod": 10, "entrySigmas": 0.9, "slopeLag": 2, "volWindow": 30}
# One parameter at a time. Ranges are the registry's own bounds:
# cutoffPeriod [5..120], slopeLag [1..40], volWindow [20..200], entrySigmas [0..1]
NEIGHBOURS = {
    "cutoffPeriod": [5, 7, 8, 10, 12, 15, 20, 30],
    "slopeLag":     [1, 2, 3, 4, 6, 10],
    "volWindow":    [20, 25, 30, 40, 60, 90],
    "entrySigmas":  [0.5, 0.7, 0.8, 0.9, 1.0],
}

def portfolio(envs, start, end, strategy, sparams, vol_target=0.20, vol_window=30):
    assert end <= DEV_END, f"refusing to evaluate past the frozen boundary: {end}"
    cmd = [str(BINARY), "portfolio", "--data-dir", str(CLI / "data"), "--envs", envs,
           "--start", start, "--end", end, "--warmup-bars", "600", "--strategy", strategy,
           "--vol-target", str(vol_target), "--vol-window", str(vol_window),
           "--weights", "equal", "--vol-lookback", "120", "--rebalance", "30"]
    if sparams:
        cmd += ["--sparams", sparams]
    out = subprocess.run(cmd, cwd=CLI, text=True, capture_output=True)
    if out.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)}\n{out.stderr.strip()}")
    def grab(pattern):
        m = re.search(pattern, out.stdout, re.M)
        return float(m.group(1)) if m else float("nan")
    trades = sum(int(x) for x in re.findall(
        r"^\s+[A-Z0-9_]+\s+[-+0-9.]+\s+[-+0-9.]+\s+[-+0-9.]+\s+[-+0-9.]+\s+(\d+)", out.stdout, re.M))
    return {"sharpe": grab(r"^Sharpe \(ann\.\):\s+([-+0-9.]+)"),
            "dd": grab(r"^Max drawdown:\s+([-+0-9.]+)%"),
            "cagr": grab(r"^CAGR:\s+([-+0-9.]+)%"), "trades": trades}

def sparams(values):
    return ",".join(f"{k}={values[k]:g}" for k in sorted(values))

def evaluate(values, envs=ENVS4, vt=0.20, vw=30):
    folds = [portfolio(envs, s, e, STRATEGY, sparams(values), vt, vw) for s, e in FOLDS]
    full = portfolio(envs, *FULL, STRATEGY, sparams(values), vt, vw)
    return folds, full

print("computing the deployed rule's baseline...", file=sys.stderr)
inc_folds = [portfolio(ENVS4, s, e, "ensemble_vote", "enterVotes=2,exitVotes=0") for s, e in FOLDS]
inc_full = portfolio(ENVS4, *FULL, "ensemble_vote", "enterVotes=2,exitVotes=0")
print(f"deployed: folds {[round(f['sharpe'],2) for f in inc_folds]}  full Sharpe {inc_full['sharpe']:.2f} dd {inc_full['dd']:.1f}%\n")

base_folds, base_full = evaluate(BASE)
base_mean = statistics.mean(f["sharpe"] - i["sharpe"] for f, i in zip(base_folds, inc_folds))
print(f"CANDIDATE  {sparams(BASE)}")
print(f"  mean delta {base_mean:+.2f}  folds {['%+.2f' % (f['sharpe']-i['sharpe']) for f,i in zip(base_folds, inc_folds)]}"
      f"  full Sharpe {base_full['sharpe']:.2f}  dd {base_full['dd']:.1f}%\n")

results = {"candidate": {"sparams": sparams(BASE), "mean_delta": base_mean,
                         "full_sharpe": base_full["sharpe"], "full_dd": base_full["dd"]}, "neighbours": {}}
print(f"{'parameter':14s} {'value':>7s} {'mean delta':>11s} {'worst fold':>11s} "
      f"{'fold deltas':>26s} {'Sharpe':>7s} {'maxDD':>7s} {'trades':>7s}")
print("-" * 100)
for name, values in NEIGHBOURS.items():
    for v in values:
        trial = dict(BASE); trial[name] = v
        folds, full = evaluate(trial)
        deltas = [f["sharpe"] - i["sharpe"] for f, i in zip(folds, inc_folds)]
        mark = "  <- candidate" if v == BASE[name] else ""
        print(f"{name:14s} {v:>7g} {statistics.mean(deltas):+11.2f} {min(deltas):+11.2f} "
              f"{str(['%+.2f' % d for d in deltas]):>26s} {full['sharpe']:7.2f} {full['dd']:6.1f}% {full['trades']:7d}{mark}")
        results["neighbours"].setdefault(name, []).append(
            {"value": v, "mean_delta": statistics.mean(deltas), "worst": min(deltas),
             "fold_deltas": deltas, "full_sharpe": full["sharpe"], "full_dd": full["dd"], "trades": full["trades"]})
    print()

print("=" * 100)
print("VERDICT INPUTS")
for name, rows in results["neighbours"].items():
    others = [r["mean_delta"] for r in rows if r["value"] != BASE[name]]
    print(f"  {name:14s} candidate {base_mean:+.2f} | neighbours {min(others):+.2f}..{max(others):+.2f}"
          f"  median {statistics.median(others):+.2f}  positive {sum(1 for o in others if o > 0)}/{len(others)}")
flat = [r for name, rows in results["neighbours"].items() for r in rows if r["value"] != BASE[name]]
means = [r["mean_delta"] for r in flat]
allfold = [r for r in flat if r["worst"] > 0]
print(f"\n  all {len(flat)} neighbours: median mean-delta {statistics.median(means):+.2f}, "
      f"{sum(1 for m in means if m > 0)} positive, {sum(1 for m in means if m > 0.20)} above +0.20")
print(f"  neighbours that ALSO win every fold (the property that made the candidate interesting): "
      f"{len(allfold)} of {len(flat)}")
for r in sorted(allfold, key=lambda r: -r["mean_delta"]):
    print(f"    mean {r['mean_delta']:+.2f} worst {r['worst']:+.2f} Sharpe {r['full_sharpe']:.2f} "
          f"dd {r['full_dd']:.1f}% trades {r['trades']}")
out = ROOT / "state" / "ehlers_neighbour_check.json"
out.write_text(json.dumps(results, indent=2) + "\n")
print(f"\nwrote {out}")
