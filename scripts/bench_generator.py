#!/usr/bin/env python3
"""Phase-0 generator benchmark: which local model, and which schema, actually
produce VALID proposals for this supervisor?

Runs the supervisor's real generator prompt against one or more local Ollama
models under two schema regimes and scores every answer with the supervisor's
own validate_proposal(), so "valid" means exactly what the daemon means by it.

  current : supervisor.proposal_schema(mode) + the daemon's system/user prompt
            (three proposal types offered, strategy not constrained by schema)
  tight   : one mode per call - proposal_type is a single-value enum, strategy
            is a single-value enum, no spec/feature fields outside their mode,
            spec leaves are a closed enum - and a prompt that mentions only the
            scheduled mode

Read-only with respect to the research state: it never touches the SQLite
registry or artifacts/, and it runs nothing but Ollama and `list-strategies`.

    python3 scripts/bench_generator.py --models qwen3-coder:latest gpt-oss:20b \
        --reps 3 --out /tmp/bench
"""
import argparse, json, statistics, subprocess, sys, time, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import supervisor as sv  # noqa: E402

LEAVES = sorted(sv.SPEC_LEAVES)
FOCI = ["donchian", "tsmom", "faber_ma", "ensemble_vote", "macd_norm", "squeeze_breakout", "control_random", "generated_spec"]

SYSTEM_CURRENT = (
    "You are the generator agent for a local quantitative research supervisor. "
    "Return exactly one JSON object matching the supplied schema. Propose one "
    "existing registered strategy family OR a creative generated_spec rule tree. Do not emit commands, "
    "paths, source code, holdout references, or deployment advice. State a real causal "
    "mechanism and an expected failure mode. The deterministic evaluator decides metrics. "
    "Use sparams only from the selected strategy family. Set novelty_key to a short "
    "slug such as donchian:wide-entry:atr-stop, never a sentence. "
    "This iteration is scheduled for strategy={focus}; the strategy field MUST be exactly "
    "{focus}. If the focus is generated_spec, set proposal_type=generated_spec, "
    "sparams to an empty string, and provide entry/exit boolean rules using only these "
    "leaves: close_above_sma, close_below_sma, close_above_ema, close_below_ema, "
    "return_above, return_below, breakout_above, breakdown_below, rsi_above, rsi_below, "
    "relative_volume_above, weekday, green_candle, red_candle."
    " Use all/any/not or their aliases and/or/not; indicator-key objects such as "
    "{{close_above_sma:{{window:50}}}} are accepted and normalized. Do not emit "
    "entry_rule strings, function calls, source code, or unlisted indicators. "
    "If the focus is feature_request, set strategy=feature_request, leave sparams empty, "
    "and provide feature={{name,description,transformation,publication_lag_days}}. "
    "A feature request is queued until a human supplies a local manifest; never provide a URL."
)

SYSTEM_TIGHT_FAMILY = (
    "You are the generator for a local quantitative research supervisor. Propose ONE hypothesis "
    "for the strategy family '{focus}' by choosing its parameters. Return exactly one JSON object "
    "matching the schema. 'sparams' is a comma-separated list of name=value pairs using ONLY the "
    "parameter names listed for '{focus}', each value inside its [lo..hi] range; an empty string means "
    "published defaults. 'mechanism' must say why prices should behave that way, not restate the "
    "parameters. 'novelty_key' is a short slug like donchian:wide-entry:atr-stop. The deterministic "
    "evaluator decides all metrics; do not claim any."
)
SYSTEM_TIGHT_SPEC = (
    "You are the generator for a local quantitative research supervisor. Compose ONE new long/flat "
    "trading rule as a boolean rule tree and return exactly one JSON object matching the schema. "
    "'entry' and 'exit' are each either a single leaf or an object with 'all' or 'any' holding 1-6 "
    "leaves. Leaf types: {leaves}. Leaves with a window need an integer 'window' (2-600 bars of 4h) "
    "and a numeric 'threshold' (percent above/below the average or channel for sma/ema/breakout "
    "leaves, a plain return for return_* leaves, 0-100 for rsi leaves, a ratio for relative_volume_above); "
    "'weekday' needs 'day' 0-6 (UTC, Sunday=0); green_candle/red_candle need nothing. 'mechanism' must "
    "say why prices should behave that way. The evaluator decides all metrics."
)

def tight_schema(focus, families):
    common = {
        "hypothesis": {"type": "string", "minLength": 20},
        "mechanism": {"type": "string", "minLength": 20},
        "reasoning": {"type": "string", "minLength": 20},
        "expected_failure_mode": {"type": "string", "minLength": 10},
        "novelty_key": {"type": "string", "minLength": 3, "maxLength": 128},
    }
    if focus == "generated_spec":
        leaf = {"type": "object", "additionalProperties": False,
                "properties": {"type": {"type": "string", "enum": LEAVES}, "window": {"type": "integer", "minimum": 2, "maximum": 600},
                               "threshold": {"type": "number"}, "day": {"type": "integer", "minimum": 0, "maximum": 6}},
                "required": ["type"]}
        node = {"type": "object", "additionalProperties": False,
                "properties": {"all": {"type": "array", "items": leaf, "minItems": 1, "maxItems": 6},
                               "any": {"type": "array", "items": leaf, "minItems": 1, "maxItems": 6},
                               "type": leaf["properties"]["type"], "window": leaf["properties"]["window"],
                               "threshold": leaf["properties"]["threshold"], "day": leaf["properties"]["day"]}}
        return {"type": "object", "additionalProperties": False,
                "required": ["proposal_type", "strategy", "spec", *common],
                "properties": {"proposal_type": {"type": "string", "enum": ["generated_spec"]},
                               "strategy": {"type": "string", "enum": ["generated_spec"]},
                               "spec": {"type": "object", "additionalProperties": False, "required": ["entry", "exit"],
                                        "properties": {"entry": node, "exit": node, "max_hold_bars": {"type": "integer", "minimum": 0, "maximum": 2000}}},
                               **common}}
    return {"type": "object", "additionalProperties": False,
            "required": ["proposal_type", "strategy", "sparams", *common],
            "properties": {"proposal_type": {"type": "string", "enum": ["family"]},
                           "strategy": {"type": "string", "enum": [focus]},
                           "sparams": {"type": "string"}, **common}}

def user_prompt(regime, focus, mission, families):
    public = {k: v for k, v in mission.items() if not k.startswith("_")}
    if regime == "current":
        if focus == "generated_spec":
            ctx = ["MISSION: local causal development research only; development_end=2023-12-31.",
                   "Return exactly one proposal_type=generated_spec JSON object.", "Set strategy=generated_spec and sparams=\"\".",
                   "The spec MUST have exactly entry, exit, and optional max_hold_bars.",
                   "entry and exit are rule trees using only all, any, not and these leaf objects:",
                   "{type:close_above_sma|close_below_sma|close_above_ema|close_below_ema,window:INTEGER,threshold:NUMBER}",
                   "{type:return_above|return_below|breakout_above|breakdown_below,window:INTEGER,threshold:NUMBER}",
                   "{type:rsi_above|rsi_below|relative_volume_above,window:INTEGER,threshold:NUMBER}",
                   "{type:weekday,day:0..6} or {type:green_candle} or {type:red_candle}.",
                   "Do not include sparams, strategy parameters, ATR names, custom indicators, URLs, or extra spec fields.",
                   "Minimal valid spec example: {\"entry\":{\"all\":[{\"type\":\"close_above_sma\",\"window\":50,\"threshold\":0}]},\"exit\":{\"type\":\"close_below_sma\",\"window\":50,\"threshold\":0},\"max_hold_bars\":0}."]
        else:
            ctx = ["MISSION:", json.dumps(public, indent=2), "REGISTERED FAMILIES AND PARAMETER BOUNDS:", json.dumps(families, sort_keys=True),
                   f"SCHEDULED STRATEGY (attempt 1/3): {focus}"]
        return "\n".join(ctx + ["", "Return only the proposal JSON object."])
    # tight: compact, mode-specific context
    if focus == "generated_spec":
        return "\n".join(["Development research on 4h crypto candles, long/flat, evaluated 2018-2023 on BTC/ETH/XRP/LTC.",
                          "Known: trend following works at 4h; sub-4h rules are fee-dead; regime filters that only remove exposure have failed 12 times.",
                          "Compose one rule that is NOT a plain moving-average trend rule. Return only the JSON object."])
    bounds = families[focus]
    return "\n".join([f"Development research on 4h crypto candles, long/flat, evaluated 2018-2023 on BTC/ETH/XRP/LTC. Family: {focus}.",
                      "Parameters and legal ranges: " + json.dumps({k: list(v) for k, v in bounds.items()}),
                      "Known: parameter plateaus are flat; propose a structurally motivated setting, not a tweak. Return only the JSON object."])

def chat(endpoint, model, system, user, schema, timeout, think=None):
    payload = {"model": model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
               "stream": False, "format": schema, "options": {"temperature": 0.7, "num_ctx": 8192}, "keep_alive": "10m"}
    if think is not None:
        payload["think"] = think   # thinking models (qwen3.x, gpt-oss): False = answer directly
    req = urllib.request.Request(endpoint + "/api/chat", data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST")
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = json.loads(r.read())
    return body, time.monotonic() - t0

def score(raw_content, focus, mission, families):
    """Mirror the daemon's acceptance logic on the CURRENT code, then the supervisor's validator."""
    try:
        raw = sv.extract_json(raw_content)
    except sv.SupervisorError as e:
        return False, "no-json: " + str(e)[:80], None
    raw["vol_target"] = mission["evaluation"]["vol_target"]; raw["weights"] = mission["evaluation"]["weights"]
    raw["vol_lookback"] = mission["evaluation"].get("vol_lookback", 120); raw["rebalance"] = mission["evaluation"].get("rebalance", 30)
    ptype = raw.get("proposal_type", "family")
    try:
        if ptype == "feature_request" or raw.get("strategy") == "feature_request":
            return False, "feature_request outside scheduled mode", raw
        if ptype == "generated_spec" or raw.get("strategy") == "generated_spec" or "spec" in raw:
            raw["proposal_type"] = "generated_spec"; raw["strategy"] = "generated_spec"; raw["sparams"] = ""
            c = sv.validate_proposal(raw, mission, families)
            return (focus == "generated_spec"), ("ok" if focus == "generated_spec" else "spec substituted for scheduled family (accepted by daemon, counts as off-focus here)"), c
        if raw.get("strategy") != focus:
            return False, f"ignored scheduled strategy (got {raw.get('strategy')!r})", raw
        raw.setdefault("sparams", "")
        c = sv.validate_proposal(raw, mission, families)
        return True, "ok", c
    except sv.SupervisorError as e:
        return False, str(e)[:90], raw

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="+", required=True); ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--regimes", nargs="+", default=["current", "tight"]); ap.add_argument("--foci", nargs="+", default=FOCI)
    ap.add_argument("--endpoint", default="http://127.0.0.1:11434"); ap.add_argument("--timeout", type=float, default=300)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--think", choices=["on", "off"], default=None, help="force thinking on/off for models that support it")
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    mission = sv.validate_mission(sv.DEFAULT_MISSION)
    binary = Path(mission["_source_repo"]) / "build" / "cli_trader"
    families = sv.parse_family_specs(subprocess.run([str(binary), "list-strategies"], text=True, capture_output=True, check=True).stdout)
    rows = []
    with (a.out / "calls.jsonl").open("a") as sink:
        for model in a.models:
            for regime in a.regimes:
                for focus in a.foci:
                    for rep in range(a.reps):
                        if regime == "current":
                            system = SYSTEM_CURRENT.format(focus=focus)
                            schema = sv.proposal_schema("generated_spec" if focus == "generated_spec" else None)
                        else:
                            system = (SYSTEM_TIGHT_SPEC.format(leaves=", ".join(LEAVES)) if focus == "generated_spec" else SYSTEM_TIGHT_FAMILY.format(focus=focus))
                            schema = tight_schema(focus, families)
                        user = user_prompt(regime, focus, mission, families)
                        try:
                            body, wall = chat(a.endpoint, model, system, user, schema, a.timeout,
                                              None if a.think is None else a.think == "on")
                            content = body.get("message", {}).get("content", "")
                            ok, reason, cand = score(content, focus, mission, families)
                            row = {"model": model, "regime": regime, "focus": focus, "rep": rep, "ok": ok, "reason": reason, "wall_s": round(wall, 1),
                                   "prompt_tokens": body.get("prompt_eval_count"), "output_tokens": body.get("eval_count"),
                                   "sparams": (cand or {}).get("proposal", {}).get("sparams") if isinstance(cand, dict) and "proposal" in cand else None,
                                   "content": content}
                        except Exception as e:  # network/timeout: record, continue
                            row = {"model": model, "regime": regime, "focus": focus, "rep": rep, "ok": False, "reason": "error: " + str(e)[:100], "wall_s": None}
                        rows.append(row); sink.write(json.dumps(row) + "\n"); sink.flush()
                        print(f"{model:22s} {regime:8s} {focus:18s} rep{rep} {'OK ' if row['ok'] else 'BAD'} {row['wall_s']}s {row['reason'][:70]}", flush=True)
    print("\n=== SUMMARY: valid-proposal rate (daemon acceptance semantics), median latency ===")
    print(f"{'model':22s} {'regime':8s} {'valid':>7s} {'n':>4s} {'lat_med':>8s} {'lat_max':>8s} {'distinct sparams':>17s}")
    for model in a.models:
        for regime in a.regimes:
            sub = [r for r in rows if r["model"] == model and r["regime"] == regime]
            lat = [r["wall_s"] for r in sub if r["wall_s"] is not None]
            distinct = len({r["sparams"] for r in sub if r.get("sparams")})
            print(f"{model:22s} {regime:8s} {sum(r['ok'] for r in sub)/max(1,len(sub)):7.0%} {len(sub):4d} {statistics.median(lat) if lat else float('nan'):8.1f} {max(lat) if lat else float('nan'):8.1f} {distinct:17d}")
    print("\n=== failure reasons by model/regime ===")
    for model in a.models:
        for regime in a.regimes:
            reasons = {}
            for r in rows:
                if r["model"] == model and r["regime"] == regime and not r["ok"]: reasons[r["reason"]] = reasons.get(r["reason"], 0) + 1
            for k, v in sorted(reasons.items(), key=lambda kv: -kv[1]): print(f"  {model} {regime}: {v} x {k}")
    (a.out / "summary_rows.json").write_text(json.dumps(rows, indent=1))

if __name__ == "__main__":
    main()
