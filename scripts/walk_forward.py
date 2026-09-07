#!/usr/bin/env python3
"""Anchored walk-forward: does the PROCEDURE that found ehlers_trend generalize?

The candidate's parameters were chosen by looking at the whole 2018-2023
development period. Re-running those parameters on the same period is not an
out-of-sample test, it is the selection restated. The honest test re-runs the
SELECTION on each training window and scores the winner on the year that
follows, which is what `evolve-strategy --wf-folds` does for genomes.

Each test year is scored three ways:

  deployed          ensemble_vote at its published defaults. No parameters are
                    chosen, so it has no selection bias to shed. The baseline.
  ehlers (full)     the candidate's parameters, which were selected using data
                    that includes the test year. The CONTAMINATED number, shown
                    only so the gap to the honest one is visible.
  ehlers (honest)   parameters re-selected on the training window alone, by
                    maximum portfolio Sharpe over a 1,440-point grid.
  ensemble (honest) the same selection applied to ensemble_vote's 9 settings,
                    so the comparison is selection-against-selection too.

Universe is the four coins with history back to 2018. Development data only;
every command asserts --end <= 2023-12-31.
"""
import itertools, json, re, statistics, subprocess, sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT.parent / "cli_trader"
BINARY = CLI / "build" / "cli_trader"
DEV_END = "2023-12-31"
ENVS = "BTC_USDT:14400,ETH_USDT:14400,XRP_USDT:14400,LTC_USDT:14400"
TRAIN_START = "2018-01-01"
TEST_YEARS = [2020, 2021, 2022, 2023]
CANDIDATE = "cutoffPeriod=10,entrySigmas=0.9,slopeLag=2,volWindow=30"

GRID_EHLERS = [
    f"cutoffPeriod={c},entrySigmas={e:g},slopeLag={s},volWindow={v}"
    for c, s, v, e in itertools.product(
        (5, 7, 10, 14, 20, 28, 40, 60), (1, 2, 3, 5, 8, 15), (20, 30, 45, 70, 120),
        (0.0, 0.15, 0.3, 0.5, 0.7, 0.9))
]
GRID_ENSEMBLE = [f"enterVotes={a},exitVotes={b}" for a in (1, 2, 3) for b in (0, 1, 2) if b < a]

def portfolio(start, end, strategy, sparams):
    assert end <= DEV_END, f"refusing to evaluate past the frozen boundary: {end}"
    cmd = [str(BINARY), "portfolio", "--data-dir", str(CLI / "data"), "--envs", ENVS,
           "--start", start, "--end", end, "--warmup-bars", "600", "--strategy", strategy,
           "--vol-target", "0.20", "--vol-window", "30", "--weights", "equal",
           "--vol-lookback", "120", "--rebalance", "30"]
    if sparams:
        cmd += ["--sparams", sparams]
    out = subprocess.run(cmd, cwd=CLI, text=True, capture_output=True)
    if out.returncode != 0:
        return None
    def grab(p):
        m = re.search(p, out.stdout, re.M)
        return float(m.group(1)) if m else float("nan")
    trades = sum(int(x) for x in re.findall(
        r"^\s+[A-Z0-9_]+\s+[-+0-9.]+\s+[-+0-9.]+\s+[-+0-9.]+\s+[-+0-9.]+\s+(\d+)", out.stdout, re.M))
    return {"sharpe": grab(r"^Sharpe \(ann\.\):\s+([-+0-9.]+)"),
            "excess": grab(r"^Sharpe \(ann\.\):\s+[-+0-9.]+\s+[-+0-9.]+\s+([-+0-9.]+)"),
            "cagr": grab(r"^CAGR:\s+([-+0-9.]+)%"), "dd": grab(r"^Max drawdown:\s+([-+0-9.]+)%"),
            "trades": trades}

def select(strategy, grid, start, end, workers=6):
    """Best parameters on a training window, by portfolio Sharpe."""
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda sp: (sp, portfolio(start, end, strategy, sp)), grid))
    scored = [(r["sharpe"], sp) for sp, r in results if r and r["sharpe"] == r["sharpe"]]
    if not scored:
        return None, None
    best = max(scored)
    return best[1], {"train_sharpe": best[0], "evaluated": len(scored),
                     "median_train_sharpe": statistics.median(s for s, _ in scored)}

rows = []
print(f"grid: {len(GRID_EHLERS)} ehlers settings, {len(GRID_ENSEMBLE)} ensemble settings, "
      f"{len(TEST_YEARS)} anchored folds\n")
for year in TEST_YEARS:
    train_end = f"{year - 1}-12-31"
    test_start, test_end = f"{year}-01-01", f"{year}-12-31"
    print(f"fold {year}: train {TRAIN_START}..{train_end}  ->  test {test_start}..{test_end}", file=sys.stderr)

    e_params, e_info = select("ehlers_trend", GRID_EHLERS, TRAIN_START, train_end)
    v_params, v_info = select("ensemble_vote", GRID_ENSEMBLE, TRAIN_START, train_end)

    row = {
        "year": year, "train_end": train_end,
        "deployed": portfolio(test_start, test_end, "ensemble_vote", "enterVotes=2,exitVotes=0"),
        "ehlers_full": portfolio(test_start, test_end, "ehlers_trend", CANDIDATE),
        "ehlers_honest": portfolio(test_start, test_end, "ehlers_trend", e_params),
        "ensemble_honest": portfolio(test_start, test_end, "ensemble_vote", v_params),
        "ehlers_selected": e_params, "ehlers_train": e_info,
        "ensemble_selected": v_params, "ensemble_train": v_info,
    }
    rows.append(row)

print(f"\n{'='*108}\nANCHORED WALK-FORWARD, 4 coins @ 4h, vt 0.20 - Sharpe on each UNSEEN test year\n{'='*108}")
print(f"{'test year':10s} {'deployed':>9s} {'ehlers(full)':>13s} {'ehlers(honest)':>15s} {'ensemble(honest)':>17s}   "
      f"{'honest delta':>12s}")
for r in rows:
    d = r["ehlers_honest"]["sharpe"] - r["deployed"]["sharpe"]
    print(f"{r['year']:<10d} {r['deployed']['sharpe']:9.2f} {r['ehlers_full']['sharpe']:13.2f} "
          f"{r['ehlers_honest']['sharpe']:15.2f} {r['ensemble_honest']['sharpe']:17.2f}   {d:+12.2f}")
mean = lambda k: statistics.mean(r[k]["sharpe"] for r in rows)
print(f"{'MEAN':10s} {mean('deployed'):9.2f} {mean('ehlers_full'):13.2f} {mean('ehlers_honest'):15.2f} "
      f"{mean('ensemble_honest'):17.2f}   {mean('ehlers_honest')-mean('deployed'):+12.2f}")
honest_deltas = [r["ehlers_honest"]["sharpe"] - r["deployed"]["sharpe"] for r in rows]
full_deltas = [r["ehlers_full"]["sharpe"] - r["deployed"]["sharpe"] for r in rows]
print(f"\n  honest selection beats the deployed rule in {sum(1 for d in honest_deltas if d > 0)}/{len(rows)} test years"
      f"  (mean {statistics.mean(honest_deltas):+.2f}, worst {min(honest_deltas):+.2f})")
print(f"  full-sample parameters would have shown {statistics.mean(full_deltas):+.2f}"
      f"  -> selection bias {statistics.mean(full_deltas) - statistics.mean(honest_deltas):+.2f} Sharpe")

print(f"\n{'='*108}\nWHAT THE SELECTION PICKED EACH TIME (is the region stable, or is it chasing noise?)\n{'='*108}")
for r in rows:
    print(f"  train ..{r['train_end']}  ->  {r['ehlers_selected']}")
    print(f"      train Sharpe {r['ehlers_train']['train_sharpe']:.2f} (median of grid "
          f"{r['ehlers_train']['median_train_sharpe']:.2f}, {r['ehlers_train']['evaluated']} settings)"
          f" | test Sharpe {r['ehlers_honest']['sharpe']:.2f}  dd {r['ehlers_honest']['dd']:.1f}%")
print(f"\n  candidate for reference: {CANDIDATE}")
out = ROOT / "state" / "ehlers_walk_forward.json"
out.write_text(json.dumps(rows, indent=2, default=str) + "\n")
print(f"\nwrote {out}")
