# Glossary

## Research terms

| term | meaning here |
|---|---|
| **development data** | candles up to 2023-12-31, the only data the loop may evaluate on |
| **holdout** | 2024-01-01 onward. Contaminated for every registry family by earlier research (recorded in `cli_trader/experiments/holdout.json`); descriptive only, never evidence, never reachable by the loop |
| **fold** | one of three fixed two-year windows: 2018-19, 2020-21, 2022-23, each with a 600-bar causal warm-up |
| **basket** | equal-weight buy-and-hold of the four protocol coins over the same window, with the same costs; the benchmark every fold is measured against |
| **excess Sharpe vs basket** | portfolio Sharpe minus basket Sharpe on a fold; the repository's primary metric |
| **incumbent** | `ensemble_vote enterVotes=2,exitVotes=0`, the rule the live book runs, evaluated through the identical commands; the bar a candidate must clear |
| **excess Sharpe vs incumbent** | candidate fold Sharpe minus incumbent fold Sharpe; `mean_excess_sharpe_vs_incumbent` is the loop's ranking metric |
| **null calibration** | 200 seeds of `control_random` through the folds; its 99th-percentile mean and worst excess Sharpe vs basket are the null gate |
| **frontier** | beats the incumbent (mean delta > 0, worst fold delta > −0.25, drawdown not worse) and beats the null. Research frontier only; not a deployment signal |
| **risk reducer** | does not beat the incumbent on Sharpe but keeps within −0.10 of it at half the drawdown |
| **dead region** | a rule family or timeframe the repository has measured and killed; listed in `RESEARCH_PRIORS` and in every prompt |
| **mechanism** | the required sentence saying why prices should behave as the rule assumes; the validator rejects a list of leaf names |
| **config hash** | SHA-256 of the canonical configuration; the candidate id and the duplicate key |
| **mission** | the JSON policy file; hashed at registration, re-checked at every start; a change is a new mission and a new ledger |

## Candidate statuses (`candidates.status`)

| status | meaning |
|---|---|
| `proposed` | validated and recorded, not yet evaluated |
| `running` | evaluation in progress in a live process |
| `killed` | failed a screen or the basket gate, or failed to execute |
| `survives_development` | passed the basket screen; see classification for how far it got |
| `frontier` | passed incumbent and null gates |
| `blocked_missing_feature` | a `feature_request`; a note for a human, never runnable |
| `interrupted` | evaluation stopped by Ctrl-C or a process death; never counts as evaluated |
| `invalid` | a stored proposal that no longer validates against the current rules |

## Classifications (`candidates.classification`)

| classification | with status | meaning |
|---|---|---|
| `execution_failed` | killed | a fold returned non-zero, timed out, or printed an unparseable report |
| `evaluation_exception` | killed | an exception inside the evaluator; the message is in the summary |
| `zero_trade_fold` | killed | fewer than `min_trades_per_fold` trades in some fold |
| `turnover_screen_failed` | killed | more than `max_trades_per_fold` trades in some fold; a churn rule that fees would destroy |
| `basket_screen_failed` | killed | positive excess Sharpe vs basket in fewer than two folds |
| `development_screen_only` | survives_development | passed the basket screen before incumbent and null comparison |
| `incumbent_or_null_gate_failed` | survives_development | passed the basket screen, failed one or both gates |
| `risk_reducer` | survives_development | see glossary |
| `frontier_candidate` | frontier | passed everything |
| `incumbent_comparison_failed` | survives_development | the incumbent backtest itself failed; promotion blocked |
| `awaiting_local_feature_manifest` | blocked_missing_feature | feature request waiting for a human-supplied data series |
| `recovered_after_process_exit` | interrupted | found `running` at startup after a hard kill |
| `operator_interrupt` | interrupted | Ctrl-C during evaluation |
| `registry_revalidation_failed` | invalid | a stored `proposed` candidate no longer validates |

## Heartbeat statuses (`state/heartbeat-v2.json`)

`running` (starting), `proposing` (model call in flight, with `focus`), `idle`
(between iterations), `error` (last iteration failed; see `error`), `paused`
(circuit breaker open; restart to continue), `stopped` (clean exit).

## Event types (`events.event_type`)

| event | when |
|---|---|
| `doctor_pass` | startup checks passed (model installed and local, data present, hashes recorded) |
| `proposal_request_error` | an Ollama request failed or returned no content; retried |
| `invalid_proposal` | a model response was rejected; the artifact is `invalid-<hash>.json` |
| `duplicate_proposal` | a valid proposal matched an existing config hash |
| `proposal_recorded` | a new candidate was stored |
| `scheduled_creative_substitution`, `scheduled_feature_substitution` | the accepted proposal type differed from the scheduled focus in a permitted way |
| `scheduled_strategy_failed` | three attempts at one focus failed |
| `iteration_skipped` | the iteration produced no candidate (failed focus or duplicate) |
| `iteration_error`, `iteration_exception` | the iteration failed with a supervisor error or an unexpected exception |
| `candidate_classified` | evaluation finished; payload is the summary |
| `candidate_reclassified` | `reclassify` re-labelled a stored survivor |
| `candidate_evaluation_exception` | evaluator raised; candidate killed |
| `candidate_interrupted`, `candidate_recovered_interrupted` | see statuses |
| `incumbent_summary_failed` | the incumbent backtest failed while building a prompt |
| `review_recorded`, `invalid_review` | reviewer output stored, or rejected |
| `null_calibration_complete` | `calibrate-null` finished |
| `circuit_breaker_open`, `daemon_paused` | a streak limit was hit and the daemon exited |

## Subcommands

| command | needs the lock | what it does |
|---|---|---|
| `doctor` | yes | startup checks and provenance hashes; run before any daemon start |
| `propose` | yes | one proposal, recorded, not evaluated |
| `run-once` | yes | one full iteration |
| `evaluate <id>` | yes | evaluate a recorded candidate |
| `review <id>` | yes | reviewer annotation for an evaluated candidate |
| `reclassify` | yes | re-run the classification over stored survivors |
| `calibrate-null [--force]` | yes | build or rebuild the null calibration |
| `daemon [--interval] [--max-iterations]` | yes | the loop |
| `status [--watch]` | no | heartbeat, counts, recent candidates and events |
| `best` | no | the current best candidate |
