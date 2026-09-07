# What the loop has found

The supervisor's output is not a strategy. It is a record of what a bounded,
adversarially-gated search over a fixed development window produces, and of how
much of that survives when you keep testing it. This document is that record.

Everything here is development data, before 2024-01-01, except where a section
says otherwise. Nothing here has been deployed.

## The headline

| | |
|---|---:|
| candidates evaluated | 2,094 |
| labelled `frontier` | 76 |
| beat the incumbent on **all three** folds | 10 |
| coin-flip controls evaluated | 107 |
| coin-flip controls that won all three folds | 0 |

Nothing found so far beats the incumbent robustly. That is the
finding, and it agrees with the sibling repository's tournament, which reached
the same conclusion over ~53,000 genomes by a different route.

**The `frontier` label is much weaker than it sounds.** Of 76 entries, 66 lose
at least one fold. The reference rule is weak in 2018-19 and exceptional in the
2020-21 mania, so "beats the incumbent on the mean" mostly means "beat it in one
bear market". Twenty-one of the 76 are `faber_ma`, which is itself a member of
the reference ensemble.

**The all-folds test is the one that discriminates.** Only 0.5% of scored
candidates win every fold, where independent coin flips would give 12.5%, and
none of the 107 random controls managed it. That gap is what makes the filter
worth applying.

## Case study: the one candidate that survived scrutiny, and how far it fell

`ehlers_trend` with `cutoffPeriod=10, entrySigmas=0.9, slopeLag=2, volWindow=30`
was the loop's best all-folds result. Following it through every subsequent test
is the most useful thing in this document, because the number shrank at each
step and the reasons are all methodological rather than about the market.

| test | result |
|---|---|
| as the ledger reported it | +0.36 mean Sharpe over the incumbent, all three folds positive |
| risk-matched control (incumbent de-levered to the same drawdown) | +0.22 |
| parameter neighbours, 21 variants | a narrow ridge: all 4 immediate neighbours hold above +0.21, but 3 of 4 dimensions collapse two steps out; only 4 of 21 also win every fold |
| universe ladder, 11 rungs from 4 to 20 coins | positive on all 11, +0.17 to +0.37, drawdown falling with breadth |
| **anchored walk-forward, parameters re-selected on each training window** | **+0.10**, positive in 3 of 4 test years, worst −0.06 |
| 2024-2026 window, real fees, live sizing | +0.25 Sharpe, better drawdown, consistent across all three years |
| every calendar year 2017-2026, longest history | **incumbent 1.61 vs candidate 1.51** |

Two things did the damage.

**Selection bias, measured rather than assumed.** Re-running the selection on
each training window and scoring the year that follows gives +0.10. Using the
parameters chosen with sight of the whole period gives +0.32 on the same folds.
The difference, **+0.22, is the selection bias**, and it happens to equal the
entire risk-matched edge.

**Window choice.** Every favourable number came from windows starting in 2018 or
later. Extended to 2017, the incumbent wins, because a fast filter with a strict
entry threshold sits out parabolic moves: 2017 cost the candidate 0.65 Sharpe.
Its advantage is concentrated in 2018 and 2022, both bear markets. It is
bear-market insurance, paid for in bull years.

**A trap worth repeating.** The walk-forward's converged parameter set
(`cutoffPeriod=14, entrySigmas=0.5, slopeLag=1, volWindow=120`) scored *better*
than the original on the four coins it was fitted to, and then lost on 10 of 11
wider-universe rungs and halved its Sharpe on the eight-coin book. A stable
selection region is not the same as a better rule.

## What the search behaves like

- **Parameter families saturate fast.** 151 `tsmom` parameter sets produced a
  best of +0.27; the best of 106 coin-flip controls was +0.26. Evaluations 30
  through 151 moved the running best by less than one standard error. This is
  why the scheduler now retires saturated families.
- **Rule composition beat parameter tuning, once.** The two best results in the
  ledger are `generated_spec` rule trees found in the first hour of searching.
  A thousand candidates later, neither has been displaced.
- **Most composed rules are not viable.** Roughly two thirds die on the basket
  screen or trade nothing at all. The model favours entry conditions with three
  or four simultaneous requirements that rarely coincide.
- **Breadth does not have an optimum.** Across universes of 4 to 20 coins, at
  matched risk, the whole spread is under half a standard error, and the best
  size reshuffles every year. What breadth reliably buys is lower drawdown and a
  narrower spread of outcomes, not a better expected result.
- **Selecting coins by past performance fails.** Ranking by trailing standalone
  Sharpe and trading the top K did worse than trading everything, at every K.

## How to reproduce any of this

The scripts are in [`../scripts/`](../scripts) and write their raw output to
`state/*.json`, which is gitignored — regenerate rather than trust a stale copy.
Each asserts its own development boundary before running the binary.

```sh
python3 scripts/neighbour_check.py        # plateau or spike
python3 scripts/breadth_check.py          # universe ladders, per sleeve
python3 scripts/walk_forward.py           # anchored, with re-selection
python3 scripts/universe_optimal.py       # matched-risk universe comparison
python3 scripts/steelman_ensemble.py      # the case against the candidate
```

## The standing lesson

Every number in the case study above was produced honestly by the harness, and
the headline still shrank by two thirds under tests the harness does not itself
apply. The gates that survived contact with reality were: **win every fold**,
**compare at matched risk**, **re-select parameters inside the walk-forward**,
and **check the longest window you have, not the flattering one**. Only the
first is implemented in `classify_results`; the other three are still manual.
