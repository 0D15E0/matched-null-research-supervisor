# Supervisor Contract

## Role

Own the research loop and enforce the immutable mission. The supervisor is the
only component allowed to translate a validated proposal into an executable
backtest command.

## Responsibilities

1. Verify the local Ollama endpoint is loopback-only and healthy.
2. Load and hash the mission before any proposal is requested.
3. Discover legal strategy families from `../cli_trader` rather than trusting
   model-supplied names or stale lists.
4. Give the generator compact state: incumbent, frontier, failures, controls,
   and unexplored regions.
5. Validate and canonicalize exactly one JSON proposal.
6. Reject duplicates, dead-region proposals without a mechanism, invalid
   parameters, shell content, and holdout references.
7. Build only allowlisted commands with explicit development dates.
8. Run the deterministic build, causality check, and fixed development folds.
9. Parse metrics from evaluator output and apply protocol kill rules.
10. Store atomic manifests, logs, metrics, and state transitions.
11. Ask the reviewer only for eligible evidence; never let the reviewer change
    deterministic classification.
12. Continue after a result, preserving the incumbent and Pareto frontier.

## Forbidden actions

The supervisor must never:

- call a non-loopback model endpoint;
- emit a command without an explicit development `--end`;
- use `tournament`, `evolve`, `fetch`, `balances`, `order-spectrum`, or live
  commands in the autonomous loop;
- read or write `2024+`, holdout, live-state, exchange, or deployment data;
- pass arbitrary model text to a shell;
- use `shell=True`;
- modify `../cli_trader/deploy`, `../cli_trader/state_live`, or credentials;
- promote a candidate because the LLM described it as good;
- delete failed artifacts before they are durably recorded.

## Recovery

A candidate is `running` only while it has an active lease. On restart, stale
leases become `interrupted` and are never treated as successful. The supervisor
must checkpoint after every proposal, rejection, run, classification, review,
and frontier update.

Shutdown is graceful: finish or terminate the current bounded process, commit
its result, release the lock, and leave enough state for a later resume.
