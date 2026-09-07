#!/usr/bin/env python3
"""Full battery for one candidate against the deployed rule.

  python3 scripts/evaluate_candidate.py --strategy ehlers_trend \
      --sparams "cutoffPeriod=14,entrySigmas=0.5,slopeLag=1,volWindow=120" --label "converged"

Sections are labelled by how much the result can be trusted:
  CONTAMINATED  the window overlaps the data the parameters were chosen on
  TRANSFER      a universe the parameters were NOT chosen on (same period)
  CONTROL       the risk-matched de-lever, this repository's standing requirement
Development data only; every command asserts --end <= 2023-12-31.
"""
import argparse, itertools, json, re, statistics, subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT.parent / "cli_trader"
BINARY = CLI / "build" / "cli_trader"
DEV_END = "2023-12-31"
CORE = ["BTC", "ETH", "XRP", "LTC"]
PLUS2 = ["DOGE", "TRX"]
YOUNG = ["ADA", "SOL"]
LIQ = ["ZEC", "BNB", "UNI", "XMR", "LINK", "XLM", "DASH", "AAVE", "BCH", "JST", "ETC", "ATOM"]
FOLDS = (("2018-01-01", "2019-12-31"), ("2020-01-01", "2021-12-31"), ("2022-01-01", "2023-12-31"))
FULL = ("2018-01-01", DEV_END)
DEPLOYED = ("ensemble_vote", "enterVotes=2,exitVotes=0")

def portfolio(coins, start, end, strategy, sparams, vt=0.20, vw=30):
    assert end <= DEV_END, f"refusing to evaluate past the frozen boundary: {end}"
    cmd = [str(BINARY), "portfolio", "--data-dir", str(CLI / "data"),
           "--envs", ",".join(f"{c}_USDT:14400" for c in coins), "--start", start, "--end", end,
           "--warmup-bars", "600", "--strategy", strategy, "--vol-target", str(vt),
           "--vol-window", str(vw), "--weights", "equal", "--vol-lookback", "120", "--rebalance", "30"]
    if sparams:
        cmd += ["--sparams", sparams]
    out = subprocess.run(cmd, cwd=CLI, text=True, capture_output=True)
    if out.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)}\n{out.stderr.strip()}")
    t = out.stdout
    def grab(p):
        m = re.search(p, t, re.M)
        return float(m.group(1)) if m else float("nan")
    sleeves = {m.group(1): {"sharpe": float(m.group(2)), "dd": float(m.group(5)), "trades": int(m.group(6))}
               for m in re.finditer(r"^\s+([A-Z0-9]+)_USDT\s+([-+0-9.]+)\s+([-+0-9.]+)\s+([-+0-9.]+)\s+([-+0-9.]+)\s+(\d+)", t, re.M)}
    return {"sharpe": grab(r"^Sharpe \(ann\.\):\s+([-+0-9.]+)"),
            "basket": grab(r"^Sharpe \(ann\.\):\s+[-+0-9.]+\s+([-+0-9.]+)"),
            "excess": grab(r"^Sharpe \(ann\.\):\s+[-+0-9.]+\s+[-+0-9.]+\s+([-+0-9.]+)"),
            "cagr": grab(r"^CAGR:\s+([-+0-9.]+)%"), "dd": grab(r"^Max drawdown:\s+([-+0-9.]+)%"),
            "corr": grab(r"^Avg pairwise sleeve correlation:\s+([-+0-9.]+)"),
            "trades": sum(s["trades"] for s in sleeves.values()), "sleeves": sleeves}

ap = argparse.ArgumentParser()
ap.add_argument("--strategy", required=True)
ap.add_argument("--sparams", default="")
ap.add_argument("--label", default="candidate")
ap.add_argument("--compare", default="", help="a second sparams string of the same family")
ap.add_argument("--compare-label", default="alternative")
a = ap.parse_args()

ENTRIES = [("ensemble_vote (DEPLOYED)", *DEPLOYED), (f"{a.strategy} ({a.label})", a.strategy, a.sparams)]
if a.compare:
    ENTRIES.append((f"{a.strategy} ({a.compare_label})", a.strategy, a.compare))

print(f"\n{'='*106}\n1. FULL DEVELOPMENT WINDOW  {FULL[0]}..{FULL[1]}  4 coins   [CONTAMINATED if params were fitted here]\n{'='*106}")
print(f"{'strategy':30s} {'Sharpe':>7s} {'excess':>7s} {'CAGR':>7s} {'maxDD':>7s} {'corr':>6s} {'trades':>7s}")
full = {}
for name, s, sp in ENTRIES:
    r = portfolio(CORE, *FULL, s, sp); full[name] = r
    print(f"{name:30s} {r['sharpe']:7.2f} {r['excess']:+7.2f} {r['cagr']:6.1f}% {r['dd']:6.1f}% {r['corr']:6.3f} {r['trades']:7d}")

print(f"\n{'='*106}\n2. PER FOLD (Sharpe, delta vs deployed)\n{'='*106}")
dep_folds = [portfolio(CORE, s, e, *DEPLOYED) for s, e in FOLDS]
print(f"{'strategy':30s} " + " ".join(f"{x[0][:7]}-{x[1][2:4]:>2s}".rjust(17) for x in FOLDS) + f" {'mean':>7s} {'worst':>7s}")
for name, s, sp in ENTRIES:
    cells, deltas = [], []
    for i, (st, en) in enumerate(FOLDS):
        r = portfolio(CORE, st, en, s, sp)
        d = r["sharpe"] - dep_folds[i]["sharpe"]; deltas.append(d)
        cells.append(f"{r['sharpe']:+6.2f}({d:+5.2f})")
    print(f"{name:30s} " + " ".join(c.rjust(17) for c in cells) + f" {statistics.mean(deltas):+7.2f} {min(deltas):+7.2f}")

print(f"\n{'='*106}\n3. RISK-MATCHED CONTROL   [CONTROL]\n{'='*106}")
ladder = {vt: portfolio(CORE, *FULL, *DEPLOYED, vt=vt) for vt in (0.08, 0.10, 0.12, 0.15, 0.20)}
for vt, r in ladder.items():
    print(f"  deployed at vol-target {vt:.2f}: Sharpe {r['sharpe']:.2f}  CAGR {r['cagr']:5.1f}%  dd {r['dd']:5.1f}%")
for name, s, sp in ENTRIES[1:]:
    c = full[name]; vt, r = min(ladder.items(), key=lambda kv: abs(kv[1]["dd"] - c["dd"]))
    print(f"\n  {name}: dd {c['dd']:.1f}% Sharpe {c['sharpe']:.2f}  vs deployed at vt {vt:.2f} "
          f"(dd {r['dd']:.1f}%) Sharpe {r['sharpe']:.2f}   ->  {c['sharpe']-r['sharpe']:+.2f}")

print(f"\n{'='*106}\n4. UNIVERSE LADDER   [TRANSFER: parameters were chosen on 4 coins]\n{'='*106}")
for lab, rungs, st, en in (("all coins, full warm-up for ADA", [CORE, CORE+PLUS2, CORE+PLUS2+YOUNG,
                            CORE+PLUS2+YOUNG+LIQ[:4], CORE+PLUS2+YOUNG+LIQ[:8], CORE+PLUS2+YOUNG+LIQ], "2022-05-01", DEV_END),
                           ("no ADA/SOL, longer window", [CORE, CORE+PLUS2, CORE+PLUS2+LIQ[:4],
                            CORE+PLUS2+LIQ[:8], CORE+PLUS2+LIQ], "2021-02-01", DEV_END)):
    print(f"\n  -- {lab}: {st}..{en}")
    print(f"  {'coins':>5s} " + " ".join(f"{n.split('(')[1][:-1]:>16s}" for n, _, _ in ENTRIES))
    for coins in rungs:
        vals, base = [], None
        for n, s, sp in ENTRIES:
            r = portfolio(coins, st, en, s, sp)
            if base is None: base = r["sharpe"]
            vals.append(f"{r['sharpe']:5.2f}({r['sharpe']-base:+5.2f})" if base != r["sharpe"] else f"{r['sharpe']:5.2f}       ")
        print(f"  {len(coins):>5d} " + " ".join(v.rjust(16) for v in vals))

print(f"\n{'='*106}\n5. PER-SLEEVE on 20 coins 2022-05-01..{DEV_END}   [TRANSFER]\n{'='*106}")
ALL20 = CORE + PLUS2 + YOUNG + LIQ
dep = portfolio(ALL20, "2022-05-01", DEV_END, *DEPLOYED)
can = portfolio(ALL20, "2022-05-01", DEV_END, a.strategy, a.sparams)
wins = sum(1 for c in ALL20 if c in dep["sleeves"] and can["sleeves"][c]["sharpe"] > dep["sleeves"][c]["sharpe"])
print(f"  beats the deployed rule on {wins} of {len(dep['sleeves'])} sleeves individually")
print("  losers: " + ", ".join(f"{c} ({can['sleeves'][c]['sharpe']:+.2f} vs {dep['sleeves'][c]['sharpe']:+.2f})"
                               for c in ALL20 if c in dep["sleeves"] and can["sleeves"][c]["sharpe"] <= dep["sleeves"][c]["sharpe"]))

print(f"\n{'='*106}\n6. REFERENCE SIZING, 8-coin universe 2021-01-01..{DEV_END}, vt 0.30 / vol-window 90   [descriptive]\n{'='*106}")
for name, s, sp in ENTRIES:
    r = portfolio(CORE+PLUS2+YOUNG, "2021-01-01", DEV_END, s, sp, vt=0.30, vw=90)
    print(f"  {name:30s} Sharpe {r['sharpe']:5.2f}  excess {r['excess']:+5.2f}  CAGR {r['cagr']:5.1f}%  dd {r['dd']:5.1f}%  trades {r['trades']}")

print(f"\nwrote {(ROOT/'state'/f'eval_{a.label}.json')}")
(ROOT / "state" / f"eval_{a.label}.json").write_text(json.dumps({"full": full}, indent=2, default=str) + "\n")
