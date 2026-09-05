# Local Ollama Research Runbook

This runbook describes how to operate the local research supervisor on the
Mac. It is for the sibling project at:

```text
/Users/ulimerlan/trader/local_ollama_research
```

The source repository is:

```text
/Users/ulimerlan/trader/cli_trader
```

On this Mac, Homebrew Python/CMake may be unavailable if the terminal PATH has
been customized. The known-good absolute paths are
`/opt/homebrew/bin/python3` and `/opt/homebrew/bin/cmake`; use those paths when
`python3` or `cmake` reports “command not found”.

The search is development-only. It may read and evaluate data ending at
`2023-12-31`. The 2024+ period is contaminated for the registered families and
must not be used by the autonomous loop.

## Safety Boundary

The supervisor is allowed to:

- call Ollama on `http://127.0.0.1:11434`;
- read the fixed local development candle stores;
- run the allowlisted `cli_trader portfolio` commands;
- write its own SQLite state, logs, and candidate artifacts;
- generate proposals, reviews, and feature requests.

The supervisor is not allowed to:

- call a cloud model;
- fetch market or feature data;
- read the holdout protocol as an evaluation input;
- run a 2024+ evaluation;
- edit deployment files or live state;
- place orders;
- promote a research candidate to production.

A `frontier` label means “research frontier only.” It is not a deployment
recommendation.

## Pre-Upload Safety Check

Before the first commit or remote upload, run:

```sh
cd /Users/ulimerlan/trader/local_ollama_research
/usr/bin/git status --short --untracked-files=all
/usr/bin/git add -n .
```

The dry-run add should contain source, tests, documentation, mission specs, and
small examples only. It should not contain `state/`, `artifacts/`,
`artifacts-v2/`, `logs/`, databases, model outputs, feature datasets, or
Python caches. The repository `.gitignore` is designed to enforce that.

The runbook examples currently contain this machine's absolute workspace paths
for copy/paste convenience. Before publishing to a public repository, replace
`/Users/ulimerlan/trader/local_ollama_research` and
`/Users/ulimerlan/trader/cli_trader` with generic project-root variables if
revealing the local username or directory layout is undesirable. This is a
privacy concern, not a credential leak.

## Directory Map

| Path | Purpose |
|---|---|
| `supervisor.py` | Local Ollama supervisor and evaluator |
| `missions/local-trend-discovery-v2.json` | Active immutable development policy |
| `state/research-v2.sqlite3` | Active durable candidates, runs, events, and reviews |
| `state/null-calibration-v2.json` | Active 200-seed random-control null distribution |
| `state/heartbeat-v2.json` | Active daemon heartbeat |
| `state/research-v2.lock` | Single-instance lock while running |
| `artifacts-v2/<candidate-id>/` | Active proposal, spec, fold output, result, and review files |
| `artifacts-v2/invalid-*.json` | Rejected v2 model proposals |
| `features/` | Local feature-provider contracts; no remote fetches |
| `scripts/install_models.sh` | Pull the approved local model tags |
| `scripts/start_ollama.sh` | Verify or start loopback Ollama |
| `logs/` | Reserved for supervisor output redirection |

The repository may also contain legacy v1 databases and artifacts. They are
historical evidence only. Do not mix them into v2 status or reclassification.
Do not delete them until their provenance has been archived.

## Prerequisites

Check the native evaluator and local Ollama before starting:

```sh
PROJECT=/Users/ulimerlan/trader/local_ollama_research
CLI=/Users/ulimerlan/trader/cli_trader

ollama --version
ollama list
curl -fsS http://127.0.0.1:11434/api/tags >/dev/null
"$CLI/build/cli_trader" list-strategies >/dev/null
```

The approved initial model is the local, non-cloud tag:

```text
generator: gpt-oss:20b
reviewer: qwen3-coder:latest
```

Do not select `:cloud` model tags. The supervisor rejects cloud names and
non-loopback endpoints.

## Start Ollama

The Ollama application is often already serving the endpoint. Verify first:

```sh
curl -fsS http://127.0.0.1:11434/api/tags
```

If it is not running, use:

```sh
cd /Users/ulimerlan/trader/local_ollama_research
./scripts/start_ollama.sh
```

If `ollama serve` prints `address already in use`, another Ollama server is
already listening. Treat that as confirmation and verify `/api/tags`; do not
start a second server.

Install the local model tags when the model store is new or incomplete:

```sh
cd /Users/ulimerlan/trader/local_ollama_research
./scripts/install_models.sh
```

## First-Time Checks

Run the supervisor doctor before the first daemon start:

```sh
cd /Users/ulimerlan/trader/local_ollama_research
/opt/homebrew/bin/python3 supervisor.py \
  --model gpt-oss:20b --reviewer-model qwen3-coder:latest doctor
```

Doctor must report:

- `ok: true`;
- endpoint `http://127.0.0.1:11434`;
- generator model `gpt-oss:20b`;
- reviewer model `qwen3-coder:latest`;
- `holdout_allowed: false`;
- the expected strategy count;
- development end `2023-12-31`.

Run the local tests after changing supervisor code:

```sh
python3 -m py_compile supervisor.py tests/test_supervisor.py
python3 -m unittest discover -s tests -v
```

If the C++ strategy registry or generated-spec executor changed, rebuild the
source project before starting research:

```sh
cd /Users/ulimerlan/trader/cli_trader
cmake --build build -j2
ctest --test-dir build --output-on-failure
```

## Null Calibration

The null calibration is required before a candidate can become `frontier`.
It runs 200 `control_random` seeds over the three fixed development folds and
writes `state/null-calibration-v2.json`.

Run it once:

```sh
cd /Users/ulimerlan/trader/local_ollama_research
/opt/homebrew/bin/python3 supervisor.py \
  --model gpt-oss:20b --reviewer-model qwen3-coder:latest calibrate-null
```

The command is idempotent. To deliberately replace the calibration after a
mission, binary, or cost-model change:

```sh
/opt/homebrew/bin/python3 supervisor.py calibrate-null --force
/opt/homebrew/bin/python3 supervisor.py reclassify
```

A recalibration invalidates the meaning of previous frontier decisions. Keep
the old artifact before forcing a new one:

```sh
/bin/cp state/null-calibration-v2.json \
  state/null-calibration-v2.$(/bin/date +%Y%m%d-%H%M%S).json
```

## Start the Continuous Search

Use an absolute supervisor path. This avoids terminal working-directory
normalization problems:

```sh
/opt/homebrew/bin/python3 \
  /Users/ulimerlan/trader/local_ollama_research/supervisor.py \
  --model gpt-oss:20b --reviewer-model qwen3-coder:latest daemon --interval 30
```

The daemon:

1. obtains a single-instance lock;
2. verifies the mission and strategy registry;
3. asks local Ollama for one proposal;
4. validates the proposal or records it as invalid;
5. evaluates valid candidates on the fixed development folds;
6. compares candidates with the deployed incumbent;
7. applies the calibrated null gate;
8. reviews survivors locally;
9. writes SQLite/artifact checkpoints;
10. continues to the next search focus.

Ollama requests are rate-limited locally: the default minimum interval is
10 seconds between HTTP requests, including retries, and transient failures
add exponential backoff up to 60 seconds. This protects the Mac GPU/model
server from retry storms. The daemon's `--interval 30` is an additional delay
between completed iterations; it does not replace request pacing.

To choose a slower request rate explicitly:

```sh
/opt/homebrew/bin/python3 /Users/ulimerlan/trader/local_ollama_research/supervisor.py \
  --llm-min-interval 20 daemon --interval 60
```

For a controlled smoke run:

```sh
/opt/homebrew/bin/python3 \
  /Users/ulimerlan/trader/local_ollama_research/supervisor.py \
  --model gpt-oss:20b --reviewer-model qwen3-coder:latest daemon \
  --interval 30 --max-iterations 3
```

Stop gracefully with `Ctrl-C` in the daemon terminal. The current candidate is
bounded by a per-fold timeout, and the heartbeat is written as `stopped`.

## Monitor the Search

Single status snapshot:

```sh
cd /Users/ulimerlan/trader/local_ollama_research
/opt/homebrew/bin/python3 supervisor.py status
```

Continuous status view:

```sh
/opt/homebrew/bin/python3 supervisor.py status --watch --interval 5
```

Watch mode intentionally prints one compact live line per refresh. It shows
the current heartbeat error only; it does not repeat historical
`iteration_error` events. Use one-shot `status` or the SQLite query below when
you need the full error history.

Process and Ollama checks:

```sh
ps -axo pid,command | grep '[s]upervisor.py'
ollama ps
curl -fsS http://127.0.0.1:11434/api/tags >/dev/null && echo 'Ollama API OK'
```

The heartbeat fields mean:

| Field | Meaning |
|---|---|
| `status=proposing` | Ollama is generating/validating a proposal |
| `status=idle` | Last iteration completed and the daemon is waiting |
| `status=error` | The last iteration failed; inspect `error` and recent events |
| `status=stopped` | The daemon exited cleanly or was stopped |
| `candidate_id` | Last completed candidate, when available |
| `iteration` | Number of daemon iterations attempted |
| `focus` | Strategy family or proposal mode currently being requested |

While `status=proposing`, the model request is in flight. The raw prompt and
raw response are not persisted until the response is accepted or rejected. The
heartbeat `focus` tells you what mode is being requested; after completion,
inspect `proposal.json` or `artifacts-v2/invalid-*.json` for the exact result.

Candidate statuses mean:

- `blocked_missing_feature`: feature idea recorded but no local data manifest;
- `proposed`: validated and waiting for evaluation;
- `killed`: failed execution or the basket-relative screen;
- `survives_development`: passed the basket screen but failed or has not yet
  cleared incumbent/null frontier gates;
- `frontier`: cleared both incumbent-relative and null-calibrated gates.

The durable current-best record is separate from the latest-candidate list:

```sh
/opt/homebrew/bin/python3 /Users/ulimerlan/trader/local_ollama_research/supervisor.py best
```

It is stored in `state/best-v2.json` and backed by the SQLite `best_history`
table. Feature requests remain visible in the ledger but are a side queue; they
do not consume executable strategy search focus unless mission policy enables
feature scheduling.

A healthy daemon can have many `invalid` and `killed` candidates. That is part
of the search record, not necessarily an operational failure.

## Find The Current Best

The best log is not the latest candidate and not every development survivor.
It records the highest mean incumbent-relative Sharpe seen by the active v2
ledger. A candidate can be the current best and still fail the frontier gate.

Use the CLI view:

```sh
/opt/homebrew/bin/python3 \
  /Users/ulimerlan/trader/local_ollama_research/supervisor.py best
```

The same record is stored in `state/best-v2.json` and backed by the SQLite
`best_history` table. Inspect the exact rule and deterministic result:

```sh
/usr/bin/sqlite3 state/research-v2.sqlite3 \
  "select candidate_id from best_history order by score desc, record_id desc limit 1")
/bin/cat "artifacts-v2/$BEST_ID/proposal.json"
/bin/cat "artifacts-v2/$BEST_ID/result.json"
/bin/cat "artifacts-v2/$BEST_ID/review.json" 2>/dev/null || true
```

Interpret the result this way:

- `frontier`: cleared basket, incumbent, drawdown, and null gates;
- `survives_development` plus `incumbent_or_null_gate_failed`: best evidence
  so far, but not a winner;
- `killed`: failed execution, turnover, or the basket-relative screen;
- `blocked_missing_feature`: an idea waiting for a human-supplied local dataset.

If `frontier` is zero, the system has no validated winner. The deployed
strategy remains the operational incumbent.

## List Every Tested Request

Accepted proposals and their status:

```sh
/usr/bin/sqlite3 -header -column state/research-v2.sqlite3 \
"select candidate_id,
        status,
        classification,
        json_extract(proposal_json,'$.proposal_type') as type,
        json_extract(proposal_json,'$.strategy') as strategy,
        created_at
 from candidates
 order by created_at desc;"
```

Rank completed candidates by incumbent-relative evidence:

```sh
/usr/bin/sqlite3 -header -column state/research-v2.sqlite3 \
"select candidate_id,
        status,
        classification,
        json_extract(proposal_json,'$.strategy') as strategy,
        json_extract(summary_json,'$.mean_excess_sharpe_vs_incumbent') as mean_vs_incumbent,
        json_extract(summary_json,'$.worst_excess_sharpe_vs_incumbent') as worst_vs_incumbent,
        json_extract(summary_json,'$.mean_excess_sharpe_vs_basket') as mean_vs_basket
 from candidates
 where summary_json is not null
 order by coalesce(json_extract(summary_json,'$.mean_excess_sharpe_vs_incumbent'), -999) desc;"
```

Feature requests are a separate side queue:

```sh
/usr/bin/sqlite3 -header -column state/research-v2.sqlite3 \
"select candidate_id,
        json_extract(proposal_json,'$.feature.name') as feature,
        json_extract(proposal_json,'$.feature.description') as description,
        created_at
 from candidates
 where status='blocked_missing_feature'
 order by created_at desc;"
```

## Inspect a Candidate

For candidate `CANDIDATE_ID`:

```sh
cd /Users/ulimerlan/trader/local_ollama_research
/usr/bin/find "artifacts-v2/CANDIDATE_ID" -maxdepth 2 -type f -print
/bin/cat "artifacts-v2/CANDIDATE_ID/proposal.json"
/bin/cat "artifacts-v2/CANDIDATE_ID/result.json"
/bin/cat "artifacts-v2/CANDIDATE_ID/review.json" 2>/dev/null || true
```

The fold outputs contain the exact evaluator stdout/stderr and command context.
Do not judge a candidate from the model's prose alone. Read the fold metrics and
`excess_sharpe_vs_incumbent` values.

Re-run the local reviewer after a completed result:

```sh
/opt/homebrew/bin/python3 supervisor.py review CANDIDATE_ID
```

Reclassify existing survivors after changing calibration:

```sh
/opt/homebrew/bin/python3 supervisor.py reclassify
```

## Read Logs And Events

There are three levels of evidence.

### Live heartbeat

Use `status --watch` for current health. It reports the current heartbeat error
only; it does not repeat historical errors:

```sh
/opt/homebrew/bin/python3 supervisor.py status --watch --interval 5
```

Important states:

- `proposing`: the local generator is talking to Ollama;
- `idle`: the last iteration completed or was skipped cleanly;
- `error`: the current iteration hit an operational exception;
- `paused`: a circuit breaker stopped the loop;
- `stopped`: no daemon is running.

### SQLite event history

Read proposal, skip, classification, review, and circuit-breaker events:

```sh
sqlite3 -header -column state/research-v2.sqlite3 \
"select event_id,
        event_type,
        candidate_id,
        created_at,
        payload_json
 from events
 order by event_id desc
 limit 30;"
```

Useful event types:

| Event | Meaning |
|---|---|
| `proposal_recorded` | A validated request became a candidate |
| `candidate_classified` | Fold evaluation finished |
| `candidate_reclassified` | Gates were recalculated |
| `invalid_proposal` | Raw model output failed validation |
| `duplicate_proposal` | Normalized configuration already exists |
| `iteration_skipped` | A focus failed without an operational crash |
| `iteration_error` | The current iteration hit an operational error |
| `circuit_breaker_open` | Consecutive invalid/duplicate limit was reached |
| `doctor_pass` | Startup safety checks passed |

### Candidate artifacts

Rejected raw model responses are stored under:

```text
artifacts-v2/invalid-*.json
```

Accepted candidates are stored under:

```text
artifacts-v2/<candidate-id>/
```

Read these files in order:

1. `proposal.json`: normalized proposal, raw model output, and provenance;
2. `spec.json`: canonical generated rule tree, when applicable;
3. `fold-1/`, `fold-2/`, `fold-3/`: evaluator stdout/stderr;
4. `result.json`: parsed fold metrics and gate decisions;
5. `review.json`: advisory reviewer assessment.

The model's prose is not evidence. Fold metrics, exact commands, mission hash,
binary hash, and source revision are evidence.

## Recover from an Error

### Ollama unavailable

Symptoms:

```text
local Ollama request failed
```

Recovery:

```sh
/usr/bin/curl -fsS http://127.0.0.1:11434/api/tags
/Applications/Ollama.app/Contents/Resources/ollama ps
```

Start the server with `./scripts/start_ollama.sh` if the endpoint is down. Do
not change the endpoint to a LAN or cloud URL.

### Model missing

Run:

```sh
/Applications/Ollama.app/Contents/Resources/ollama list
./scripts/install_models.sh
/opt/homebrew/bin/python3 supervisor.py doctor
```

### Supervisor lock exists

First verify that no daemon is running:

```sh
/bin/ps -axo pid,command | /usr/bin/grep '[s]upervisor.py'
```

Only when no supervisor process exists may the stale lock be removed:

```sh
/bin/rm -f state/research-v2.lock
```

Never remove the lock while a daemon is active. That can create concurrent
writers and corrupt the research history.

### Heartbeat says `error`

Read the error and recent events:

```sh
/opt/homebrew/bin/python3 supervisor.py status
/usr/bin/sqlite3 state/research-v2.sqlite3 \
  'select event_id,event_type,candidate_id,created_at,payload_json from events order by event_id desc limit 20;'
```

Common causes are an invalid model proposal, duplicate configuration, a stale
strategy family, or a missing local data file. Invalid proposals are preserved
under `artifacts-v2/invalid-*.json`; do not delete them before diagnosing the
pattern.

If the daemon is no longer running, correct the cause and restart it. The
SQLite registry and artifacts are the checkpoint; there is no need to recreate
the experiment history.

### C++ binary changed or fails

Stop the daemon before rebuilding:

```sh
cd /Users/ulimerlan/trader/cli_trader
cmake --build build -j2
ctest --test-dir build --output-on-failure
```

Then return to the supervisor project and run `doctor`. If the evaluator,
transaction costs, indicators, or strategy registry changed, create a new
mission ID and a new null calibration rather than mixing old and new evidence.

## Back Up and Maintain State

Before maintenance:

```sh
cd /Users/ulimerlan/trader/local_ollama_research
mkdir -p state/backups
/usr/bin/sqlite3 state/research-v2.sqlite3 \
  "backup 'state/backups/research-v2-$(/bin/date +%Y%m%d-%H%M%S).sqlite3'"
/bin/cp missions/local-trend-discovery-v2.json \
  "state/backups/mission-v2-$(/bin/date +%Y%m%d-%H%M%S).json"
```

Keep `state/research-v2.sqlite3`, the mission file, null calibration, and candidate
artifacts together. They form one provenance bundle. Do not delete individual
candidate artifacts while the database still references them.

Artifacts can grow quickly during an endless run. Archive old artifacts only
when the daemon is stopped and after a database backup. Keep all `result.json`,
`proposal.json`, and `review.json` files for candidates that reached
`survives_development` or `frontier`.

## Clean Restart Procedure

1. Stop the daemon with `Ctrl-C`.
2. Confirm no process remains with `ps`.
3. Back up SQLite and the mission if changing code or policy.
4. Run Python tests.
5. Run `doctor`.
6. Rebuild `cli_trader` if C++ changed.
7. Re-run `status` and confirm the null calibration is present.
8. Restart the daemon with the absolute supervisor path.
9. Watch the heartbeat for the first iteration.

## Do Not Do This

Do not run these through the autonomous supervisor:

```text
2024+ dates
--end 2024-01-01 or later
fetch
balances
run --mode live
parity
order-spectrum without explicit development bounds
tournament or evolve commands
```

Do not manually edit `state/research-v2.sqlite3`, candidate statuses, or result
metrics to make a strategy look better. Add a new event or create a new
mission when the protocol changes.

## Current Operational Snapshot

As of the latest maintenance pass:

- active mission: `local-trend-discovery-v2`;
- generator model: `gpt-oss:20b`; reviewer model: `qwen3-coder:latest`;
- endpoint: `http://127.0.0.1:11434`;
- development boundary: `2023-12-31`;
- vol window: `30` bars; warm-up: `600` bars;
- null calibration: v2 200 seeds, read from `state/null-calibration-v2.json`;
- incumbent: `ensemble_vote`, `enterVotes=2,exitVotes=0`;
- autonomous holdout access: disabled;
- autonomous deployment access: disabled.

Recheck this section with `doctor` and `status`; it is operational guidance,
not a substitute for the machine-readable mission.
