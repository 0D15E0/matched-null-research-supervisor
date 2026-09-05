# Local Ollama Research Supervisor Plan

## Purpose

Build a continuously running, local-only research supervisor that uses Ollama
to propose and review trading hypotheses while the repository's deterministic
backtester performs the actual experiments.

The supervisor should not stop after finding one attractive result. It should
maintain a durable record of the current best candidates, continue exploring
new hypotheses, learn from killed candidates, and periodically re-evaluate the
frontier under the existing research protocol.

The LLM is the research assistant. It is not the source of truth for metrics,
causality, dates, fills, or deployment decisions.

## Implementation Status

Phase 0 and the first Phase 1 slice are now operational in
`supervisor.py`. The local `qwen3-coder:latest` model is installed and has
successfully generated structured proposals through Ollama on
`127.0.0.1:11434`. A Donchian candidate has completed all three fixed
development folds, and the local reviewer classified it as weak with a human
review recommendation. A bounded daemon iteration also completed and wrote a
checkpoint.

The generator is no longer limited to registry parameter mutations. The
`generated_spec` path lets it compose a bounded causal rule tree from moving
averages, returns, breakouts, RSI, relative volume, candle direction, weekdays,
and boolean logic. The `feature_request` path records ideas that need a local,
versioned data provider, such as a pizza-price signal, without granting the
supervisor web access or allowing invented feature values.

The implementation deliberately stops short of promotion: incumbent-relative
comparison and null calibration are implemented as research-frontier gates, and
a human-controlled forward test remains a separate gate. No 2024+ data was
used by the supervisor, and no deployment file or live state was touched.

## Current machine and repository context

Observed on the development Mac:

- Apple model: `Mac16,8`
- CPU: 12 cores
- Memory: 51.2 GB
- Ollama client: `0.32.14`
- Ollama server: installed but not currently running
- Models already pulled (`~/.ollama`, 65 GB): `glm-5`, `qwen3-coder`,
  `qwen3-coder-next` - benchmark these before pulling anything else
- Repository binary: `build/cli_trader`
- Existing registry: `experiments/hypotheses.json`
- Existing protocol: `experiments/research_protocol.py`
- Existing fixed folds: 2018-2019, 2020-2021, and 2022-2023
- Frozen development boundary: `2023-12-31`
- Holdout/forward data: `2024-01-01` onward - **already contaminated** for
  every registry family and for tsmom portfolio variations (weights, vol
  targets, universe size); see `experiments/holdout.json`, `contamination`.
  Results on it are descriptive, not out-of-sample. The only clean data for a
  candidate is data that arrives after that candidate is registered.
- Existing search machinery: `cli_trader tournament` already ran ~53,000
  parameter genomes across 24 families and 12 environment sets
  (`docs/TOURNAMENT.md`); `research_protocol.py` already holds 12 registered
  hypotheses; `experiments/factor_trend/` is the most recent study

The worktree contains unrelated current changes. The implementation phase must
inspect and preserve them rather than assuming a clean branch.

## Repository findings this plan must build on

An autonomous proposer that does not know what this repository has already
measured will spend its budget rediscovering it. These are not opinions; each
has a numbered addendum in `PROFITABILITY_PLAN.md` or a section in the README.

1. **The parameter space of the registered families is already searched.**
   The tournament tuned every family by genetic search and found that
   in-sample ranking among the *plausible* candidates carries no
   out-of-sample information (rank correlation over the better half: never
   significant in 12 runs); coin-flip controls ranked first on the holdout in
   4 of 12 runs. A loop that proposes `sparams` variations of registered
   families is a slower, noisier genetic algorithm. What an LLM can add that
   the GA cannot is a **mechanism**: a new information source, a new rule
   shape, a pre-measurement of the effect before the rule is built. Version 1
   below cannot do that (no source edits), so its honest purpose is
   infrastructure proving plus a calibrated null - not discovery.
2. **The kill rules in the registry are basket-relative and lenient.** A
   candidate can clear "+0.10 mean excess Sharpe over the buy-and-hold
   basket" while losing to the live book on every fold - this happened on
   2026-09-05 (`factor-trend-w050-vt020`: survives_development, and worse
   than the incumbent on all three folds). For a loop generating hundreds of
   candidates the frontier must be **incumbent-relative** and
   **risk-matched**, or it will fill with survivors nobody should trade.
3. **Known-dead regions.** Timeframes below 4h are fee-dead for trend
   (addendum 12) and for mean reversion (23, 24); daily is worse than 4h for
   the same rules (25); eleven exposure-reducing regime filters plus the
   factor confirmation gate have all failed (14, 26); conviction sizing,
   asymmetric exits and conviction reallocation fail (20, 21); adding coins
   makes the book worse (19); cross-sectional momentum died before 2020 (6);
   chart patterns and Fibonacci entries are negative (7, 10). Seed the
   generator with this list. A proposal inside a dead region needs a stated
   reason why this time is different, or it is rejected without a run.
4. **The vol target is a leverage dial** (13): Sharpe is flat across 0.20-0.40
   while CAGR and drawdown scale together. CAGR is therefore not a frontier
   axis, and any comparison across sizing settings must be risk-matched
   (standing requirement since addendum 22).
5. **Diversification arithmetic is exact here, every time it has been
   checked.** Portfolio Sharpe = mean sleeve Sharpe x sqrt(N/(1+(N-1)rho)). A
   candidate that raises sleeve correlation is spending the one thing the
   book lives on; the frontier must record rho, not only Sharpe.
6. **The protocol universe is four coins at 4h** (BTC, ETH, XRP, LTC). The
   eight-coin universe cannot run the 2018-19 and 2020-21 folds: ADA and SOL
   start in 2022. Eight coins are a secondary, descriptive window
   (2021-01-01..2023-12-31), as in addendum 19.

## Non-negotiable rules

### Local-only execution

All model inference must use the local Ollama HTTP service:

```text
http://127.0.0.1:11434
```

The supervisor must not call OpenAI, Anthropic, Google, hosted inference,
remote embeddings, or any other cloud model. It should fail closed if the
configured Ollama endpoint is not loopback or if an unexpected network client
is introduced.

The backtester may use local data only. Data fetching is a separate explicit
operation and is not part of the autonomous research loop.

### Holdout isolation

The autonomous loop may use only data ending at `2023-12-31`. It must not be
given a command-line date, data directory, or arbitrary shell access that could
reach 2024+ data.

The guard is the **command builder**, not filename patterns: the stores are
single append-only files holding the whole history, so nothing about a path
says which dates a run touched. The builder emits only `portfolio` and
`backtest` with an explicit `--end` no later than `2023-12-31` and an explicit
`--start`, and it never emits `tournament`, `evolve`, `evolve-strategy`,
`xsmom` or `order-spectrum` without those bounds - the 2026-08-24
contamination event was twelve `tournament` runs whose internal train/holdout
split reached into 2024+. `fetch`, `run`, `parity`, `order` and `balances` are
never emitted at all.

The 2024+ period is **not** a clean validation window for any registered
family (see the contamination record). It may be used only as a labelled
descriptive check of a frozen candidate, by a human, once, and the result must
be written down as descriptive. Clean evidence for a new candidate comes from
a **forward test registered in `holdout.json` before it starts**, on paper or
live, judged no earlier than its stated read date - the pattern already in
use for the live book (registered 2026-08-24, readable 2027-08-25). The local
supervisor must have no command that performs either.

### Deterministic evaluation

The LLM must never calculate or invent performance metrics. Every reported
metric must come from a completed deterministic command, with:

- causal warm-up bars;
- next-open fills;
- configured fees and slippage;
- the fixed chronological development folds;
- the equal-weight buy-and-hold benchmark;
- an exact command record;
- a captured binary version and source revision;
- a registry record that can be replayed.

### No direct deployment

The supervisor must not edit `deploy/pi/bot.env`, live state, systemd files,
or exchange credentials. It must not place orders. A human must explicitly
review and authorize any holdout test or deployment.

### Endless does not mean uncontrolled

The process may run indefinitely, but every iteration must have a timeout,
checkpoint, resource limit, and durable result. A crash or restart must resume
from the registry without losing the incumbent or re-running an experiment
under a different interpretation.

## Target architecture

```text
                    localhost only

  Ollama generator model  <---->  Research supervisor
          |                              |
          | structured proposal          | validates and schedules
          v                              v
      proposal.json              isolated candidate workspace
                                         |
                                         v
                              C++ build + causality check
                                         |
                                         v
                              fixed development evaluator
                                         |
                                         v
                         result.json + stdout/stderr + manifest
                                         |
                                         v
                              registry and Pareto frontier
                                         |
                                         v
                             Ollama reviewer model
```

The supervisor should be a small Python process using the standard library
where practical. It should call Ollama's local `/api/chat` endpoint over HTTP,
invoke only allowlisted repository commands, and write atomic JSON artifacts.
The research engine remains the existing C++ executable and Python protocol.

## Ollama setup

### Installation and service

Ollama is already installed on this Mac, but the server is not running. The
initial setup should be performed manually and verified before automation:

```sh
ollama serve
```

Keep the server bound to loopback. Do not expose port `11434` to the LAN or
the public internet. The supervisor should verify `/api/tags` before starting
and record the selected model name and Ollama version in every run manifest.

### Model strategy

Use two local model roles rather than one oversized model for every iteration:

1. **Generator:** a smaller coding/reasoning model that proposes one
   structured hypothesis per iteration. It should be fast enough to keep the
   experiment queue occupied without exhausting memory.
2. **Reviewer:** a larger local model used only for survivors, repeated
   failures, novelty checks, and frontier reviews.

Do not hard-code a model name into the protocol until it has been benchmarked
on this Mac. Test installed or locally pullable open-weight models in the
roughly 7B-14B range first, then compare a larger quantized model for review.
The useful measurement is proposals per hour, valid-schema rate, duplicate
rate, and reviewer agreement with deterministic classification, not model
benchmark scores.

Model licenses must be checked separately. “Runs through Ollama” does not by
itself mean that every model has the same redistribution or commercial-use
terms.

### Resource policy

The 51.2 GB machine can support a local model and native backtests, but memory
should not be treated as unlimited:

- Keep one generation model loaded during normal operation.
- Load the reviewer only for review batches, or use a separate bounded queue.
- Start with one LLM request and one backtest worker at a time.
- Increase backtest concurrency only after measuring memory pressure and
  thermal throttling.
- Set Ollama keep-alive deliberately so an idle loop does not consume memory
  forever.
- Record process duration and peak resident memory where available.
- Do not run simultaneous large-model inference and a large tournament until a
  measured resource budget supports it.

The first objective is reliable continuous research, not maximum parallelism.

## Immutable research mission

Create a versioned mission document or JSON file that the supervisor loads at
startup and refuses to modify. It should contain:

```yaml
mission_id: local-trend-discovery-v1
data_policy:
  development_end: 2023-12-31          # every emitted --end must be <= this
  holdout_start: 2024-01-01            # contaminated; never emitted, never read
  allowed_data_dirs: [data]            # data/derived only when periods != 14400 (v2)
  allowed_commands: [portfolio, backtest, list-strategies, causality_check]
universe:
  protocol_symbols: [BTC_USDT, ETH_USDT, XRP_USDT, LTC_USDT]   # the folds run here
  descriptive_symbols: [BTC_USDT, ETH_USDT, XRP_USDT, LTC_USDT, DOGE_USDT, TRX_USDT, ADA_USDT, SOL_USDT]
  descriptive_window: [2021-01-01, 2023-12-31]   # 8 coins share history only from here
  periods: [14400]                     # <4h is fee-dead, 1d is worse: addenda 12/23/24/25
evaluation:
  folds:
    - [2018-01-01, 2019-12-31]
    - [2020-01-01, 2021-12-31]
    - [2022-01-01, 2023-12-31]
  warmup_bars: 80
  vol_target: 0.20                     # fixed: the vol target is a leverage dial
  costs: protocol defaults (0.15% fee, 0.05% slippage; the venue charges 0.125%)
  primary_metric: mean_excess_sharpe_vs_incumbent   # not vs the basket
  required_metrics: [sharpe, excess_sharpe_vs_basket, excess_sharpe_vs_incumbent,
                     worst_fold_excess, max_drawdown, sleeve_correlation, trades]
incumbent:
  strategy: ensemble_vote
  sparams: "enterVotes=2,exitVotes=0"
  note: the live rule; re-run through the identical folds at mission start,
        and its fold results stored in the mission record
null_calibration:
  strategy: control_random
  seeds: 200                           # same folds, same sizing; gives the
  note: distribution of mean/worst excess Sharpe under no skill - the
        promotion threshold is a quantile of THIS, not a round number
limits:
  max_parameters: 12
  max_candidate_runtime_seconds: 900
  max_invalid_proposals_in_a_row: 20
  max_duplicate_proposals_in_a_row: 20
  dead_regions: see "Repository findings" - a proposal inside one needs a
                stated reason or is rejected unrun
permissions:
  allow_holdout: false
  allow_deployment: false
  allow_source_edits: false
```

The actual format may be JSON or YAML, but the supervisor must validate it at
startup and include its content hash in every candidate manifest. Any mission
change creates a new mission ID and a new experiment lineage.

## Candidate contract

The LLM should produce exactly one JSON proposal per iteration. It must not
produce shell commands, Python source, arbitrary file paths, or prose in place
of required fields.

Example:

```json
{
  "proposal_id": "generated-by-supervisor",
  "hypothesis": "A slower breakout with a volatility-ranked entry gate improves worst-fold excess Sharpe without increasing drawdown.",
  "strategy": "donchian",
  "sparams": "entryWindow=80,exitWindow=25,atrWindow=20,atrStopMult=3.0",
  "vol_target": 0.20,
  "weights": "equal",
  "vol_lookback": 120,
  "rebalance": 30,
  "mechanism": "A slower channel needs a longer-lived move to trigger, so fewer entries happen inside the chop that the 55-bar channel trades; the wider stop keeps the survivors through pullbacks the 20-bar exit would have sold.",
  "reasoning": "The candidate changes one structural idea from the current frontier and remains within the Donchian family bounds.",
  "expected_failure_mode": "It may enter too late and miss fast reversals.",
  "novelty_key": "donchian:slower-entry:wider-stop"
}
```

`mechanism` is required and is what the reviewer grades: a sentence about
*why prices should behave that way*, not a restatement of the parameters. The
repository's own history is that mechanism-first studies (addenda 20, 23, 26
measured the conditional effect before building the rule) produced knowledge
even when the rule failed, while parameter searches produced noise. A
`timeframes` field is deliberately absent from v1: the protocol runs 4h, and
the other timeframes are dead regions (see above).

Family names and parameter bounds must be read from `cli_trader
list-strategies` at mission start, not from a list typed into the supervisor:
`research_protocol.py` keeps its own `REGISTERED_STRATEGIES` set and a family
added to the registry (`factor_trend`, 2026-09-05) had to be added there by
hand.

Validation must reject:

- unknown strategy families;
- unknown or out-of-range parameters;
- more than the mission's parameter limit;
- dates or data directories from the holdout;
- shell commands or paths in model output;
- missing hypothesis, failure mode, or novelty key;
- duplicate candidates or semantically equivalent parameter sets;
- bespoke flags mixed with registry `sparams`;
- candidates that change the benchmark or fill model;
- strategies that cannot pass the causality tool.

The supervisor, not the model, constructs the executable command from the
validated fields.

## Prompt design

Every generator prompt should contain compact machine-produced context:

- immutable mission ID and rules;
- current incumbent and its complete fold metrics;
- Pareto frontier members;
- recently killed candidates and exact kill reasons;
- strategy families and legal parameter bounds;
- recent unexplored family/timeframe combinations;
- resource and runtime limits;
- the instruction to propose exactly one JSON object.

Do not send the entire repository or the entire conversation history on every
iteration. Retrieve focused source snippets or family metadata when needed.
Long histories encourage the model to repeat its own assumptions and increase
local inference cost.

Use a second prompt for review. The reviewer receives the candidate manifest,
deterministic fold outputs, complexity information, and comparable frontier
members. It may annotate the result, but it cannot override the protocol's
classification.

## Durable state

The registry should eventually move from a mutable, human-oriented JSON file to
a small SQLite database, while preserving JSON export for inspection. The
database should have at least these tables:

### `missions`

- mission ID and content hash;
- development boundary and fold definition;
- allowed data directories;
- created timestamp;
- model and supervisor versions.

### `candidates`

- candidate ID;
- normalized proposal JSON and hash;
- hypothesis text;
- strategy and parameters;
- mission ID;
- status: `proposed`, `running`, `killed`, `survives_development`,
  `frontier`, `review_pending`, or `invalid`;
- parent candidate IDs, if any;
- created and completed timestamps.

### `runs`

- candidate ID and fold;
- exact argv array;
- source revision and binary hash;
- stdout/stderr artifact paths;
- exit code and timeout;
- parsed metrics;
- data-store manifest;
- start/end timestamps.

### `frontier`

- candidate ID;
- mean excess Sharpe;
- worst-fold excess Sharpe;
- worst drawdown;
- CAGR;
- complexity;
- correlation to the incumbent, when measured;
- reason for entering or leaving the frontier.

### `events`

Append-only supervisor events such as proposal rejection, process restart,
Ollama failure, candidate timeout, registry checkpoint, and human pause.

Every state transition should be atomic. A candidate marked `running` without a
completed result must be recoverable as `interrupted` after a restart, never
silently treated as successful.

## Endless search loop

The supervisor loop should follow this lifecycle:

1. Acquire a single-instance lock.
2. Verify the mission hash and local Ollama health.
3. Recover interrupted candidates and stale leases.
4. Summarize the incumbent, frontier, failures, and unexplored search space.
5. Ask the generator for one structured proposal.
6. Validate and canonicalize the proposal.
7. Reject invalid or duplicate proposals without running them.
8. Register the candidate as `running` with a lease and exact manifest.
9. Run build, causality, and fixed development folds in an isolated workspace.
10. Parse metrics only from known output formats.
11. Apply deterministic kill rules.
12. Ask the reviewer only for candidates that survive or reveal a repeated
    failure pattern.
13. Update the incumbent and Pareto frontier.
14. Checkpoint all state and append an event.
15. Continue to the next proposal.

Pseudocode:

```python
while not shutdown_requested:
    recover_interrupted_runs()
    verify_mission_and_ollama()

    context = registry.compact_context(
        incumbent=True,
        frontier=True,
        recent_failures=50,
        unexplored_regions=True,
    )
    proposal = ollama_generate_json(mission, context)

    candidate = validate_and_canonicalize(proposal, mission)
    if candidate.invalid:
        registry.record_invalid(candidate)
        continue
    if registry.is_duplicate(candidate):
        registry.record_duplicate(candidate)
        continue

    run = evaluator.run_development_only(candidate, mission)
    classification = protocol.classify(run)
    registry.commit(candidate, run, classification)

    if classification in {"survives_development", "frontier_candidate"}:
        review = ollama_review_json(candidate, run, frontier)
        registry.record_review(review)

    checkpoint()
```

The loop should support graceful shutdown after the current candidate and a
resume command after restart. It should not require an active chat session.

## Search strategy for the LLM

The generator should not always optimize the current best score. Use a staged
mixture of search modes, recorded in the registry:

- **Family exploration:** try registered families not recently tested.
- **Local mutation:** change one or two parameters around a surviving
  candidate, creating a new hypothesis ID.
- **Structural alternatives:** compare equal versus inverse-volatility
  construction, timeframes, and regime gates under predeclared rules.
- **Robustness search:** seek simpler candidates with similar performance.
- **Diversification search:** seek candidates whose returns are weakly
  correlated with the incumbent, not merely candidates with a higher Sharpe.
- **Failure-directed search:** use recurring kill reasons to avoid known dead
  regions or test a clearly different mechanism.
- **Control proposals:** periodically run always-long, random-entry, and
  buy-and-hold controls to detect evaluation drift.

The supervisor should enforce quotas, for example 40% family exploration, 25%
local mutation, 20% robustness/diversification, and 15% controls. The LLM may
choose within a quota, but it should not spend forever mutating one apparent
winner.

Expect the honest outcome of v1 to be negative: the tournament has already
searched this space, and the repository's standing finding is that the
plausible region of it is flat. That is not a reason to skip v1 - a loop that
reproduces the tournament's null with full provenance, a calibrated control
distribution and an incumbent-relative frontier is the infrastructure the
later, mechanism-generating phases need - but the plan should not promise
discovery from parameter mutation, and the 24-hour review should ask whether
the frontier beat the control frontier, not whether it is non-empty.

## Incumbent and Pareto frontier

Do not keep only one “best strategy.” Maintain a frontier across at least:

- mean excess Sharpe **versus the incumbent** on the same folds;
- worst-fold excess Sharpe versus the incumbent;
- maximum drawdown (only comparable at the fixed mission vol target);
- average pairwise sleeve correlation (printed by `portfolio`);
- complexity (parameter count, and whether the rule is one mechanism);
- correlation of the candidate's portfolio curve to the incumbent's.

CAGR is not an axis: at a fixed vol target it is implied by Sharpe, and across
vol targets it is a leverage choice (addendum 13).

The last axis needs plumbing that does not exist yet: only `backtest` has
`--dump-equity`; `portfolio` prints statistics and discards its curve. Add a
`--dump-equity` to `portfolio` (about twenty lines; the single-instrument one
is at `main.cpp` `cmdBacktest`) before the frontier claims to measure it.

A candidate can be valuable because it has lower drawdown, simpler rules, or
low correlation to the current strategy even if its raw Sharpe is lower - and
low correlation is the axis most worth having, because effective breadth is
the binding constraint on this book (addenda 19, 21, 26). It is also the
axis this repository has never found a positive example of.

The incumbent is a research incumbent, not a live deployment recommendation.
The live strategy remains unchanged until a separate human-controlled process
reviews development evidence and authorizes a frozen forward test.

## Kill and promotion rules

The deterministic protocol remains the authority. A baseline first version can
use the existing registry rules:

- minimum positive-fold count;
- minimum mean excess Sharpe;
- minimum worst-fold excess Sharpe;
- maximum worst-fold drawdown;
- successful build and causality check.

Add these autonomous-loop rules:

- invalid schema: kill immediately;
- duplicate normalized candidate: do not run;
- timeout or crash: kill the run and retain the failure artifact;
- no improvement after a predeclared number of mutations: return to family
  exploration;
- too many repeated invalid proposals: pause and require operator review;
- any holdout access attempt: stop the supervisor and create a security event.

Add these evidence rules, which the existing registry does not have and an
autonomous loop cannot do without:

- **Incumbent-relative promotion.** A candidate enters the frontier only if it
  beats the incumbent's stored fold results on mean *and* worst-fold excess
  Sharpe at the same vol target, with drawdown no worse - the risk-matched
  comparison that has been standing practice since addendum 22. Clearing the
  basket-relative gate alone is recorded, not promoted.
- **Null-calibrated thresholds.** Before the first LLM proposal, run
  `control_random` through the identical folds for 200 seeds and store the
  distribution of mean and worst excess Sharpe. A promotion needs to exceed a
  stated quantile of that distribution (99th is the natural choice), and the
  threshold is part of the mission hash.
- **Search accounting.** Every survivor's record carries the number of
  candidates evaluated in the mission so far, and the frontier report prints
  the expected maximum Sharpe under the null for that count
  (`metrics::expectedMaxSharpeUnderNull`, the deflated-Sharpe machinery the
  tournament already uses) next to the best candidate. A frontier whose best
  member sits at or below that line is reporting its own selection pressure.
- **Plateau, not spike.** A promoted candidate's immediate parameter
  neighbours (one step in each direction within bounds) are run before
  promotion; if the neighbours do not agree, the result is a spike and is
  recorded as such (addendum 16 and 24 are the precedents).

“Survives development” means eligible for review only. It does not permit
holdout access, deployment, or replacing the incumbent.

## Isolation and process controls

The supervisor should use:

- a dedicated local user or macOS launch agent where practical;
- a repository worktree or temporary candidate directory per run;
- an allowlisted executable command builder;
- no `shell=True` subprocess calls;
- fixed environment variables and explicit working directories;
- per-process timeouts;
- bounded stdout/stderr capture;
- atomic result writes;
- a lock file or SQLite lease to prevent duplicate supervisors;
- automatic cleanup only after artifacts are committed;
- a read-only copy of the mission and holdout policy.

The first implementation should not let the LLM edit C++ files. It should use
existing strategy families and parameters only. Source-generating candidates
can be added later in isolated worktrees after the structured-search loop is
proven reliable.

With no source edits there is nothing to build or isolate per candidate: build
once at mission start, record the binary hash and source revision in the
mission record, run `causality_check` once against that binary, and run each
candidate as a plain subprocess in the repository directory with a list argv.
A worktree and a rebuild per one-second backtest add failure modes and buy
nothing until candidates can change source.

## Observability

Each iteration should produce a human-readable event line and a machine-readable
manifest. Track:

- iteration number;
- candidate ID and proposal hash;
- Ollama model, version, and latency;
- prompt/schema validation outcome;
- command, source revision, binary hash, and data manifest;
- per-fold metrics and kill reason;
- memory/runtime information;
- frontier changes;
- restart and recovery events.

Provide a read-only status command that shows:

- current supervisor heartbeat;
- current candidate and lease expiry;
- incumbent and frontier;
- candidates completed, killed, invalid, and duplicated;
- last Ollama error;
- last checkpoint;
- holdout policy status.

The status command must not trigger experiments.

## Phased implementation

### Phase 0: manual local model check

Goal: prove Ollama works locally before writing a daemon.

Tasks:

1. Start `ollama serve` bound to loopback.
2. Start with the models already on disk (`qwen3-coder`, `qwen3-coder-next`,
   `glm-5`); pull nothing until one of them has been measured.
3. Call `/api/tags` and `/api/chat` with a tiny proposal task, passing the
   proposal JSON schema in the request's `format` field so the model is
   constrained to it at generation time rather than validated afterwards.
4. Measure response latency, valid JSON rate, and memory behavior.
5. Record the model name and license in a local operator note.

Exit condition: 20 consecutive valid structured responses with no network
request outside loopback.

### Phase 1: proposal-only supervisor

Goal: let the LLM propose candidates without executing them.

Tasks:

1. Define the immutable mission file.
2. Build proposal validation and canonicalization.
3. Add duplicate detection.
4. Store proposals and rejection reasons durably.
5. Generate a compact frontier context from the existing registry.

Exit condition: the supervisor can restart and continue without losing state.

### Phase 2: one-candidate evaluator

Goal: run one validated proposal deterministically.

Tasks:

1. Build commands only from validated fields.
2. Use the existing fixed development folds.
3. Capture exact artifacts and parse metrics.
4. Apply existing kill rules.
5. Add timeout, lease recovery, and atomic registry updates.

Exit condition: a manually selected candidate produces the same result through
the supervisor and through a direct command.

### Phase 3: bounded continuous loop

Goal: run unattended for hours or days.

Tasks:

1. Add one-candidate-at-a-time looping.
2. Add quotas across exploration modes.
3. Add heartbeat and status output.
4. Add graceful shutdown and restart recovery.
5. Keep the reviewer disabled or restricted to survivors.

Exit condition: a 24-hour local run has no lost candidates, duplicate state
corruption, holdout access, or uncontrolled memory growth.

### Phase 4: reviewer and frontier analysis

Goal: use a second local model to interpret deterministic evidence.

Tasks:

1. Ask for structured review, not a replacement classification.
2. Add complexity and novelty assessments.
3. Add correlation-to-incumbent analysis.
4. Test whether reviewer comments predict later deterministic failures.

Exit condition: reviewer annotations are useful metadata but never override
protocol decisions.

### Phase 5: human-controlled forward test

Goal: obtain clean evidence for a small number of frozen survivors.

2024+ is not that evidence: it is contaminated for every registry family
(`holdout.json`), and a candidate proposed by a model that was shown the
incumbent's development results is, in any case, a descendant of families
that have all been measured on it. The clean route is the one the repository
already uses: a human freezes the candidate ID, parameters, binary revision
and sizing, registers a forward test in `holdout.json` with its criteria and
earliest read date **before it starts**, and runs it on paper alongside the
live book. Results are imported as read-only evidence when the read date
arrives. A single descriptive 2024+ run of the frozen candidate may be taken
by the human and must be labelled descriptive; it does not promote anything.

This phase must be outside the autonomous loop. No model gets permission to
alter the candidate after registration.

## Practical first milestone

The first useful implementation should be deliberately modest:

```text
one local Ollama model
one generator prompt
one JSON proposal schema
existing registered strategies only
one candidate at a time
fixed 2018-2023 development folds
existing hypotheses registry plus append-only artifacts
no source edits
no holdout access
no deployment access
```

Before the first proposal, the supervisor runs the incumbent and 200
`control_random` seeds through the folds and stores both; the frontier is
empty until something beats both.

Run it for 24 hours, inspect its invalid/duplicate rate, and ask one question
of the frontier: did anything beat the incumbent on every fold *and* the
control distribution's 99th percentile? If not - the expected outcome - the
run has still delivered the calibrated null and the provenance machinery, and
the next step is the mechanism-generating phase, not more parameter search.

## Success criteria

The project is successful when the supervisor can run unattended and still
answer these questions exactly after a restart:

1. What mission and data boundary governed this candidate?
2. Which local model proposed it, and what did it output?
3. What exact executable command evaluated it?
4. Which source revision and binary were used?
5. Which folds passed or failed?
6. Why was it killed or added to the frontier?
7. What was the last best candidate at the time?
8. Did the loop ever access holdout data or deployment files?
9. How many candidates had been evaluated when this one was promoted, and
   what did the best control score by then?
10. How does it compare to the incumbent on the same folds, risk-matched?

If those answers cannot be reconstructed from local artifacts, the loop is not
ready to run forever.

## Revision notes

- 2026-09-05: reviewed against the repository's recorded findings. Corrected
  the holdout status (2024+ is contaminated, `holdout.json`), replaced the
  filename-pattern data guard with a command-builder guard, fixed the
  universe/timeframe mismatch with the protocol (four coins at 4h; eight coins
  only as a 2021-23 descriptive window), made the frontier incumbent-relative
  and null-calibrated, dropped CAGR as an axis, noted that `portfolio` cannot
  yet dump an equity curve, removed per-candidate builds and worktrees from
  v1, rewrote Phase 5 as a registered forward test, listed the models already
  pulled, and added the "Repository findings" section so the generator is
  seeded with what has already been measured.
