# Local Search TODO

This is the active maintenance backlog for the local Ollama research
supervisor. Items are intentionally separate from the immutable mission: a
backlog change must not silently change an evaluation protocol.

## Completed

- [x] Loopback-only Ollama client with cloud-model rejection.
- [x] Strict `http://127.0.0.1:11434` endpoint, proxy bypass, and redirect refusal.
- [x] Structured JSON generator and reviewer contracts.
- [x] Durable SQLite candidates, runs, reviews, and events.
- [x] Atomic proposal/result artifacts.
- [x] Fixed 2018-2019, 2020-2021, and 2022-2023 development folds.
- [x] Explicit `2023-12-31` command-builder boundary.
- [x] Causal `generated_spec` rule trees.
- [x] Local-only `feature_request` queue for ideas needing external data.
- [x] Calendar-rule control family for weekday hypotheses.
- [x] Invalid/duplicate proposal recording.
- [x] Heartbeat and `status --watch` monitoring.
- [x] 200-seed `control_random` null calibration.
- [x] Incumbent-relative fold comparison against deployed `ensemble_vote`.
- [x] Frontier gate requiring incumbent and null superiority.
- [x] Automatic reviewer pass for surviving daemon candidates.
- [x] Mission hash guard with a clean v2 ledger after v1 evidence was archived.
- [x] Mission-owned `vol_window=30` and 600-bar warm-up; no hidden 90-bar setting.
- [x] Sanitized evaluator environment with exchange credentials/proxies removed.
- [x] Non-destructive run insertion and recovery of candidates left `running`.
- [x] Circuit breakers for consecutive invalid and duplicate proposals.
- [x] Explicit zero-trade and excessive-turnover screens.
- [x] Source revision and binary SHA-256 in candidate/run artifacts.
- [x] Read-only status database access.

## P0: Operations and Reliability

- [ ] Add a macOS `launchd` agent or an operator-approved process manager so
      the daemon can restart after login/reboot. Keep it disabled until the
      24-hour soak test passes.
- [ ] Run a documented 24-hour soak test and record proposal rate, invalid
      rate, duplicate rate, memory, Ollama failures, and checkpoint recovery.
- [ ] Add bounded stdout/stderr log rotation under `logs/` for long runs.
- [x] Recover in-flight candidates as `interrupted` on supervisor startup.
- [ ] Add a `pause` file or command that lets an operator stop after the current
      candidate without sending a signal to the terminal process.
- [x] Add a mission hash and binary/source provenance to candidate/result
      manifests.

## P1: Research Quality

- [x] Store source Git revision and `cli_trader` binary SHA-256 in every run.
- [ ] Add portfolio equity-curve export so candidate/incumbent correlation can
      become a real frontier axis rather than an unimplemented plan item.
- [ ] Add explicit parameter-neighbour robustness checks before frontier status.
- [ ] Add search quotas across generated specs, feature requests, family
      exploration, mutations, diversification, and controls. Current scheduling
      chooses the least-tested family but does not enforce percentage quotas.
- [ ] Add dead-region metadata and require a mechanism justification before
      scheduling a known-dead family/timeframe.
- [ ] Add a frontier report that ranks candidates by mean/worst incumbent delta,
      drawdown, complexity, and correlation instead of relying on recent status.
- [ ] Add a control drift check that periodically re-runs a fixed seed set and
      detects evaluator/cost changes.
- [ ] Add a second local reviewer-model benchmark using an already-installed
- [x] Benchmark installed local models; use `gpt-oss:20b` for generation and
      `qwen3-coder:latest` for review because the local benchmark showed higher
      generator proposal diversity for gpt-oss.

## P1: Feature Providers

- [ ] Define and validate `features/manifest.json` with source, license,
      checksum, observation timestamp, publication timestamp, and lag policy.
- [ ] Add a local feature importer that accepts only manifest-approved CSV and
      never performs network access.
- [ ] Add feature alignment tests proving no value published after a trading bar
      is used by a generated rule.
- [ ] Add a feature-spec executor only after one local feature has passed the
      alignment and missing-data tests.
- [ ] Keep pizza-price ideas blocked until a licensed, versioned local series is
      supplied and registered before evaluation.

## P2: Strategy Generation

- [ ] Expand the generated-spec DSL only through causal, unit-tested primitives.
- [ ] Add generated-spec causality checks to the supervisor preflight.
- [ ] Add a source-generation phase in isolated worktrees only after the
      declarative DSL has a stable null and frontier process.
- [ ] Add compiler/test/artifact capture for source-generated candidates.
- [ ] Add a complexity budget that penalizes deep boolean trees and many leaves.

## P2: State and Tooling

- [ ] Add SQLite schema migrations and a schema version instead of relying on
      `CREATE TABLE IF NOT EXISTS` alone.
- [ ] Export a compact JSON/CSV frontier report for inspection and review.
- [ ] Add a read-only local dashboard only after the CLI status output is stable.
- [ ] Add automated backup verification and restore tests.
- [ ] Reconcile or retire the legacy root `supervisor.db` and archived v1
      ledger after checking their provenance; the active database is
      `state/research-v2.sqlite3`.

## Promotion Boundary

- [ ] Never give the autonomous loop holdout access.
- [ ] For a candidate that survives the frontier gate, freeze its ID, spec,
      binary, sizing, and mission in a human-authored forward-test registration
      before any 2024+ or later paper/live observation.
- [ ] Require human review before any descriptive 2024+ comparison.
- [ ] Require a separate human-controlled decision before deployment.
