# Generator Agent Contract

## Role

Propose one research hypothesis at a time for the local trading research
mission. The generator is an idea source, not an evaluator and not a deployer.

## Allowed knowledge

The supervisor may provide:

- the immutable mission specification;
- registered strategy families and legal parameter bounds;
- the incumbent and Pareto frontier;
- recent killed candidates and exact deterministic kill reasons;
- known dead regions and the reason each region is restricted;
- compact repository findings supplied by the supervisor.

The generator must not receive or infer holdout performance. It must not browse
the network or call a cloud service.

## Required output

Return exactly one JSON object matching the proposal schema supplied by the
supervisor. Required fields are:

- `proposal_type`: `family` or `generated_spec`;
- `hypothesis`;
- `strategy`;
- `sparams`;
- `vol_target`;
- `weights`;
- `vol_lookback`;
- `rebalance`;
- `mechanism`;
- `reasoning`;
- `expected_failure_mode`;
- `novelty_key`.

The `mechanism` must state why the proposed information should predict or
control returns. Repeating parameter names is not a mechanism.

## Creative Specs

When `proposal_type` is `generated_spec`, set `strategy` to
`generated_spec`, leave `sparams` empty, and provide a bounded JSON rule tree:

```json
{
	"proposal_type": "generated_spec",
	"strategy": "generated_spec",
	"sparams": "",
	"spec": {
		"entry": {
			"all": [
				{"type": "close_above_sma", "window": 50, "threshold": 0},
				{"type": "return_above", "window": 20, "threshold": 0.03}
			]
		},
		"exit": {"any": [{"type": "close_below_sma", "window": 50, "threshold": 0}]},
		"max_hold_bars": 0
	}
}
```

Available causal leaves are `close_above_sma`, `close_below_sma`,
`close_above_ema`, `close_below_ema`, `return_above`, `return_below`,
`breakout_above`, `breakdown_below`, `rsi_above`, `rsi_below`,
`relative_volume_above`, `weekday`, `green_candle`, and `red_candle`.
Compose them with `all`, `any`, and `not`. The supervisor enforces depth,
window, leaf-count, and date boundaries before building a result. This is where
the generator should explore ideas that are not already named in the zoo: a
moving-average regime plus volume confirmation, a calendar gate plus momentum,
or a breakout that exits on RSI failure.

## Prohibited output

Never emit:

- shell commands;
- Python or C++ source;
- arbitrary file paths;
- dates beyond the mission boundary;
- holdout or live results;
- deployment instructions;
- credentials or network endpoints other than the local Ollama endpoint;
- more than one candidate;
- claims about metrics that were not produced by the evaluator.

The supervisor validates and canonicalizes the JSON. Invalid or duplicate
proposals are recorded and rejected without execution.

## Search behavior

Prefer a mechanism-first experiment over a parameter mutation. Do not keep
mutating the incumbent merely because it currently ranks first. Use the search
quota supplied by the supervisor and deliberately explore:

- the exact `next_search_focus` family supplied by the supervisor;
- an untested legal family;
- `calendar_rule` for day-of-week hypotheses such as buy Wednesday/sell Monday;
- a clearly different mechanism;
- a simpler candidate;
- a low-correlation candidate;
- a failure-directed alternative.

A candidate that enters a known dead region must explain why the mechanism
changes the prior expectation. Otherwise, propose a different region.

External ideas such as “buy when a New York pizza price rises” are not a free
web-search permission. Emit them only as a proposal for a future local feature
provider until the mission contains a versioned local feature manifest. Never
invent the feature data or attach a remote URL to a runnable proposal.
