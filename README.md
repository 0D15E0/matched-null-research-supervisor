# Matched-Null Research Supervisor

Proposed repository name: `matched-null-research-supervisor`.

This project hosts the local-only Ollama research supervisor for the
deterministic trading engine in
[matched-null_cli_trader_bot](https://github.com/0D15E0/matched-null_cli_trader_bot).
The local checkout is expected at `../cli_trader` during development.

The linked CLI trader repository is the source of the candle engine, strategy
registry, portfolio evaluator, transaction-cost model, and development-fold
backtests. This repository orchestrates local model proposals and feeds only
validated, development-bound candidates into that engine.

The supervisor uses Ollama on `127.0.0.1:11434` to propose and review trading
hypotheses. It does not replace the deterministic C++ backtester, and it has no
permission to access holdout data, edit live deployment files, or place orders.

## Where the incumbent came from

This loop did not start with a model. It started with the CLI used by hand:
fetching candles, validating stores, and backtesting one idea at a time, the
way anyone would. That manual phase grew into the sibling repository's
tournaments — twenty-four strategy families, roughly 53,000 candidates, with
coin-flip controls bred alongside them — and the result was a clean negative:
out of sample, nothing reliably beat holding the coins, and in-sample rank
predicted nothing among the plausible ideas.

What was left standing was arithmetic rather than a forecast: volatility-
targeted sizing, diversification across coins, and a trend filter whose value
is the drawdown it avoids. Since *which* trend rule barely mattered, the
survivor was a majority vote of three published rules at their defaults:
`ensemble_vote` with `enterVotes=2, exitVotes=0`.

That rule is the **incumbent** here. Every proposal the loop generates is
evaluated by the same engine, on the same folds, at the same costs, and asked
one question: does it beat the incumbent? So far nothing has done so robustly;
[docs/FINDINGS.md](docs/FINDINGS.md) keeps the score. The full account of how
the incumbent was chosen is the sibling repository's
[STRATEGY.md](https://github.com/0D15E0/matched-null_cli_trader_bot/blob/main/docs/STRATEGY.md).

## Start here

- [Documentation: how it works, the loop, the CLI contract, the rule language](docs/README.md)
- [Research plan](LOCAL_OLLAMA_RESEARCH_PLAN.md)
- [What the loop has found](docs/FINDINGS.md)
- [Operations runbook](RUNBOOK.md)
- [Active TODO](TODO.md)
- [Generator agent contract](agents/GENERATOR.md)
- [Reviewer agent contract](agents/REVIEWER.md)
- [Supervisor contract](agents/SUPERVISOR.md)
- [active v4 mission](missions/local-trend-discovery-v4.json)
- [historical v3, v2 and v1 missions](missions/)

## Current status

Phase 0/1 is operational:

The active mission is v4: the same evaluation protocol as v2 in the same ledger, with a
new search policy (whole-registry rotation, mode quotas, saturation, near-duplicate and
spec-novelty gates). The v1 ledger is archived and not mixed with v2/v3/v4 evidence.

- `supervisor.py` calls Ollama only on loopback and validates structured JSON;
- the mission, strategy registry, and development boundary are checked before
	proposals run;
- proposals and fold artifacts are stored in SQLite and `artifacts-v2/`;
- `propose`, `evaluate`, `review`, `status`, and bounded `daemon` commands work;
- `generated_spec` lets the model compose new causal rule trees without editing
	C++ or pretending that a parameter mutation is a new strategy;
- `feature_request` lets the model record ideas requiring a local dataset
	contract without fetching remote data or fabricating results;
- feature requests are kept as a side queue and do not consume executable
  strategy search slots;
- the calibrated random-control null and incumbent comparison now separate
  development survivors from `frontier` candidates;
- local `qwen3-coder:latest` generated and reviewed development candidates;
- the active generator is `gpt-oss:20b`; `qwen3-coder:latest` is retained for
	evidence review;
- no holdout or deployment command exists in the supervisor.

The current candidates are research evidence only. Promotion is now blocked by
the incumbent-relative and null-calibrated gates unless a candidate clears both.

The null calibration is stored under `state/` (gitignored; rebuild it with
`calibrate-null`). It runs 200 `control_random` seeds through the same folds
and records the 99th-percentile gates a candidate has to clear. Read the
current values from that file rather than from prose: the numbers previously
quoted here had drifted from the ones the loop was actually enforcing.

A candidate must also beat the incumbent `ensemble_vote` on mean and worst-fold
Sharpe, with no worse worst-fold drawdown, before it can be marked `frontier`.
`frontier` still means research frontier only; it is not permission to use the
holdout or deploy.

The first implementation must use existing registered strategy families and
fixed development folds from `../cli_trader`. Source-generating candidates,
holdout validation, and deployment integration are explicitly out of scope
until the proposal-only and deterministic evaluation stages have been proven.

## Local runtime boundary

Inference is local Ollama only:

```text
http://127.0.0.1:11434
```

The supervisor should fail closed if the endpoint is not loopback, if Ollama is
unavailable, or if a proposal attempts to emit executable commands or paths.

## Run It

From this directory, install the local open-weight model tags and verify the
runtime:

```sh
./scripts/install_models.sh
python3 supervisor.py doctor
```

Generate one structured proposal without running a backtest:

```sh
python3 supervisor.py propose
```

Evaluate a recorded candidate on the fixed development folds, then ask the
local reviewer to annotate its deterministic result:

```sh
python3 supervisor.py evaluate CANDIDATE_ID
python3 supervisor.py review CANDIDATE_ID
python3 supervisor.py status
```

Show the durable current-best record:

```sh
python3 supervisor.py best
```

Build the 200-seed development-only random-control null before relying on
frontier labels:

```sh
python3 supervisor.py calibrate-null
```

Reclassify older development survivors after a new calibration:

```sh
python3 supervisor.py reclassify
```

Watch the daemon while it runs:

```sh
python3 supervisor.py status --watch --interval 5
```

The heartbeat retains the last candidate, current iteration, model, and latest
error even after a bounded daemon exits, so the same command is useful for
checking a stopped or crashed run.

Status meanings:

- `survives_development`: passes the basket-relative development screen;
- `frontier`: also beats the incumbent on mean and worst-fold Sharpe,
	stays within its worst drawdown, and clears the null's 99th-percentile gates;
- `blocked_missing_feature`: a creative idea is waiting for a human-supplied
	local feature manifest.

The scheduler supplies the generator with the least-tested family. It starts
with `generated_spec`, then rotates through `calendar_rule`, moving-average,
momentum, ensemble, breakout, and control families. This makes “try and try” an
observable search process rather than an unbounded conversation that can repeat
one idea.

The first continuous mode is bounded during development with
`--max-iterations`; omit it only after the loop has passed its restart and
resource tests:

```sh
python3 supervisor.py daemon --interval 30 --max-iterations 3
```

The daemon is development-only. It cannot emit a holdout command or edit the
live deployment, and every candidate is stored under `state/` and `artifacts-v2/`.

## License

Copyright (c) 2026 Ulises Merlan

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
