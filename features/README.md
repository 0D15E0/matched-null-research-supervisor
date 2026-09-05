# Local Feature Providers

External ideas are data contracts, not web-search permissions.

A proposal such as “buy when New York pizza prices rise” becomes runnable only
when a human or a separately reviewed importer places a versioned feature file
here. The autonomous supervisor never fetches it from the internet.

## CSV contract

A feature file must contain:

```csv
timestamp,value
2018-01-01T00:00:00Z,12.50
2018-02-01T00:00:00Z,12.75
```

Rules:

- timestamps are UTC ISO-8601 values;
- values are finite numeric observations;
- timestamps are strictly increasing;
- the file has a declared source, license, checksum, and publication delay;
- the feature's observation time is not confused with its publication time;
- the evaluator uses only the last value published before the trading bar;
- a feature must cover the complete development folds or the missing periods
  are recorded rather than silently forward-filled;
- the manifest must declare the transformation, lag, and alignment policy.

`manifest.json` is the allowlist. A file is not usable merely because it exists
in this directory.

## Pizza example

A future `ny_pizza_price` provider would need a local, licensed, versioned
series plus a predeclared rule such as:

```text
feature: ny_pizza_price
change_window: 30 calendar days
entry: pizza change > 0
lag: publication delay supplied by the source
exit: fixed calendar or market rule specified before evaluation
```

The experiment should first test whether the feature predicts anything in a
chronological feature study. Only then should it be wired into a trading
strategy. No remote URL, live API, or unverified historical reconstruction is
allowed in the autonomous loop.
