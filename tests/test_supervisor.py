import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from supervisor import (
    DEFAULT_MISSION,
    MIN_TREND_WINDOW,
    is_saturated,
    normalized_parameters,
    parameter_distance,
    parse_family_meta,
    parse_sparams,
    spec_signature,
    LEAF_SPEC,
    generator_prompts,
    parse_family_catalogue,
    proposal_schema,
    OllamaClient,
    Registry,
    TraceLogger,
    classify_results,
    parse_family_specs,
    proposal_schema,
    validate_mission,
    validate_proposal,
)


CLI_SAMPLE = """
Registry strategies (searchable by 'tournament'):
  donchian            Donchian rule
                      entryWindow=55 [5..250], exitWindow=20 [3..120], atrWindow
=20 [5..60], atrStopMult=2.5 [0..8]
  conviction_breakout causal rule
                      entryWindow=55 [20..250], exitWindow=20 [5..120], trendWindow=200 [50..400]
  control_always_long CONTROL
                      (no tunable parameters)

Available strategies:
  tsmom - legacy entry point
"""


NEW_FORMAT_SAMPLE = """
Registry strategies (searchable by 'tournament', tunable with --sparams):
  ensemble_vote       majority vote
                      enterVotes=2 [1..3], exitVotes=1 [0..2]
  faber_ma            Faber (2007)
                      window=200 [20..400], bandPct=0.0 [0.0..5.0]

Available strategies:
"""


class FakeStreamingResponse:
    def __init__(self, lines):
        self.lines = [json.dumps(line).encode() + b"\n" for line in lines]

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def readline(self):
        return self.lines.pop(0) if self.lines else b""


class FakeStreamingOpener:
    def __init__(self, response):
        self.response = response

    def open(self, request, timeout):
        return self.response


class SupervisorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mission = validate_mission(DEFAULT_MISSION)
        cls.families = parse_family_specs(CLI_SAMPLE)

    def proposal(self, strategy="donchian", sparams="entryWindow=50,exitWindow=10,atrWindow=20,atrStopMult=2"):
        return {
            "proposal_type": "family",
            "hypothesis": "A slower breakout should reduce false entries while preserving persistent trends.",
            "strategy": strategy,
            "sparams": sparams,
            "vol_target": 0.2,
            "weights": "equal",
            "vol_lookback": 120,
            "rebalance": 30,
            "mechanism": "A longer range requires directional persistence before entry and should reduce chop exposure.",
            "reasoning": "The mechanism is distinct from simply changing portfolio leverage or the benchmark.",
            "expected_failure_mode": "The slower trigger may enter after the profitable part of a move.",
            "novelty_key": "donchian:slower-entry",
        }

    def test_wrapped_registry_parameters_stay_in_their_family(self):
        self.assertEqual(set(self.families["donchian"]), {"entryWindow", "exitWindow", "atrWindow", "atrStopMult"})
        self.assertEqual(set(self.families["conviction_breakout"]), {"entryWindow", "exitWindow", "trendWindow"})
        self.assertEqual(self.families["control_always_long"], {})

    def test_valid_proposal_is_canonicalized(self):
        candidate = validate_proposal(self.proposal(), self.mission, self.families)
        self.assertEqual(candidate["candidate_id"][:5], "cand-")
        self.assertEqual(candidate["proposal"]["sparams"], "atrStopMult=2,atrWindow=20,entryWindow=50,exitWindow=10")

    def test_cross_family_parameter_leak_is_rejected(self):
        with self.assertRaisesRegex(Exception, "unknown parameter"):
            validate_proposal(self.proposal(sparams="breakoutAtr=1.0"), self.mission, self.families)

    def test_parameterless_family_schema_requires_empty_sparams(self):
        schema = proposal_schema(
            family="control_always_long",
            family_parameters=self.families["control_always_long"],
        )
        self.assertEqual(schema["properties"]["sparams"], {"type": "string", "enum": [""]})

    def test_parameterless_family_rejects_invented_parameters(self):
        with self.assertRaisesRegex(Exception, "unknown parameter"):
            validate_proposal(
                self.proposal(strategy="control_always_long", sparams="enterBand=1"),
                self.mission,
                self.families,
            )

    def test_duplicate_proposals_penalize_their_strategy_focus(self):
        with TemporaryDirectory() as directory:
            registry = Registry(Path(directory) / "research.sqlite3")
            try:
                registry.event(
                    "duplicate_proposal",
                    {"config_hash": "hash", "strategy": "donchian", "sparams": "entryWindow=50"},
                    "cand-duplicate",
                )
                failures = registry.strategy_duplicate_failures()
            finally:
                registry.close()
        self.assertGreater(failures["donchian"], 0.0)

    def test_semantic_duplicates_penalize_focus_without_invalid_streak(self):
        with TemporaryDirectory() as directory:
            registry = Registry(Path(directory) / "research.sqlite3")
            try:
                registry.event(
                    "semantic_duplicate",
                    {"strategy": "donchian", "reason": "same mechanism"},
                )
                failures = registry.strategy_duplicate_failures()
                self.assertGreater(failures["donchian"], 0.0)
                self.assertEqual(registry.consecutive_event_streak("invalid_proposal"), 0)
                registry.event("doctor_pass", {"mission_id": "test"})
                registry.event("duplicate_proposal", {"strategy": "donchian"})
                self.assertEqual(registry.consecutive_event_streak("duplicate_proposal"), 1)
            finally:
                registry.close()

    def test_non_loopback_ollama_is_rejected(self):
        with self.assertRaisesRegex(Exception, "loopback"):
            OllamaClient("http://example.com:11434", "qwen3-coder:latest")

    def test_streamed_ollama_dialog_is_written_to_trace(self):
        with TemporaryDirectory() as directory:
            trace_path = Path(directory) / "trace.jsonl"
            trace = TraceLogger(trace_path)
            client = OllamaClient(
                "http://127.0.0.1:11434",
                "gpt-oss:20b",
                min_interval=0,
                trace=trace,
            )
            client.opener = FakeStreamingOpener(FakeStreamingResponse([
                {"message": {"thinking": "reasoning ", "content": ""}, "done": False},
                {"message": {"thinking": "briefly", "content": "{\"ok\":"}, "done": False},
                {"message": {"content": "true}"}, "done": True, "eval_count": 2},
            ]))
            content, _ = client.chat("system", "user", {"type": "object"})
            self.assertEqual(content, '{"ok":true}')
            events = [json.loads(line) for line in trace_path.read_text().splitlines()]
        names = [event["event"] for event in events]
        self.assertIn("llm.request_start", names)
        self.assertIn("llm.chunk", names)
        self.assertIn("llm.request_end", names)
        self.assertEqual("reasoning briefly", "".join(event["thinking"] for event in events if event["event"] == "llm.chunk"))

    def test_cloud_model_and_nonstandard_endpoint_are_rejected(self):
        with self.assertRaisesRegex(Exception, "cloud"):
            OllamaClient("http://127.0.0.1:11434", "glm-5:cloud")
        with self.assertRaisesRegex(Exception, "exactly"):
            OllamaClient("http://127.0.0.1:11435", "qwen3-coder:latest")

    def test_generated_spec_is_validated_as_a_new_rule(self):
        proposal = self.proposal(strategy="generated_spec", sparams="")
        proposal["proposal_type"] = "generated_spec"
        proposal["spec"] = {
            "entry": {"all": [
                {"type": "close_above_sma", "window": 50, "threshold": 0.0},
                {"type": "return_above", "window": 20, "threshold": 0.03},
            ]},
            "exit": {"any": [{"type": "close_below_sma", "window": 50, "threshold": 0.0}]},
            "max_hold_bars": 0,
        }
        candidate = validate_proposal(proposal, self.mission, self.families)
        self.assertEqual(candidate["proposal"]["proposal_type"], "generated_spec")
        self.assertEqual(candidate["proposal"]["strategy"], "generated_spec")

    def test_generated_spec_rejects_unknown_leaf(self):
        proposal = self.proposal(strategy="generated_spec", sparams="")
        proposal["proposal_type"] = "generated_spec"
        proposal["spec"] = {
            "entry": {"type": "pizza_price_up", "window": 20, "threshold": 0.0},
            "exit": {"type": "red_candle"},
        }
        with self.assertRaisesRegex(Exception, "unknown generated spec leaf"):
            validate_proposal(proposal, self.mission, self.families)

    def test_generated_spec_accepts_safe_model_aliases(self):
        proposal = self.proposal(strategy="generated_spec", sparams="")
        proposal["proposal_type"] = "generated_spec"
        proposal["spec"] = {
            "entry": {"and": [
                {"close_above_sma": {"sma_window": 50, "threshold": 0.0}},
                {"rsi_above": {"window": 14, "level": 50}},
            ]},
            "exit": {"or": [{"breakdown_below": {"window": 20, "threshold": 0.0, "method": "low"}}]},
        }
        candidate = validate_proposal(proposal, self.mission, self.families)
        self.assertEqual(candidate["proposal"]["spec"]["entry"]["all"][0]["type"], "close_above_sma")

    def test_feature_request_is_validated_without_a_remote_source(self):
        proposal = self.proposal(strategy="feature_request", sparams="")
        proposal["proposal_type"] = "feature_request"
        proposal["feature"] = {
            "name": "ny_pizza_price",
            "description": "A local historical New York pizza price index.",
            "transformation": "Use the percentage change over 30 calendar days.",
            "publication_lag_days": 7,
        }
        candidate = validate_proposal(proposal, self.mission, self.families)
        self.assertEqual(candidate["proposal"]["proposal_type"], "feature_request")

    def test_feature_request_rejects_remote_urls(self):
        proposal = self.proposal(strategy="feature_request", sparams="")
        proposal["proposal_type"] = "feature_request"
        proposal["feature"] = {
            "name": "ny_pizza_price",
            "description": "Read the feature from https://example.com.",
            "transformation": "Use its monthly change.",
            "publication_lag_days": 7,
        }
        with self.assertRaisesRegex(Exception, "remote URLs"):
            validate_proposal(proposal, self.mission, self.families)

    def test_generated_strategy_does_not_become_feature_request_from_annotation(self):
        proposal = self.proposal(strategy="generated_spec", sparams="")
        proposal["proposal_type"] = "generated_spec"
        proposal["feature"] = {
            "name": "extra_annotation",
            "description": "A harmless annotation that is not a data provider.",
            "transformation": "Describe the proposed transformation only.",
            "publication_lag_days": 1,
        }
        with self.assertRaisesRegex(Exception, "generated spec"):
            validate_proposal(proposal, self.mission, self.families)

    def test_sparams_tolerates_spaces_after_commas(self):
        candidate = validate_proposal(
            self.proposal(sparams="entryWindow=50, exitWindow=10, atrWindow=20, atrStopMult=2"),
            self.mission, self.families)
        self.assertEqual(candidate["proposal"]["sparams"], "atrStopMult=2,atrWindow=20,entryWindow=50,exitWindow=10")

    def test_frontier_tolerates_one_noise_level_fold_loss(self):
        candidate = [
            {"sharpe": 1.55, "excess_sharpe_vs_basket": 1.2, "max_drawdown_pct": 4.3, "trade_count": 124},
            {"sharpe": 2.11, "excess_sharpe_vs_basket": 0.5, "max_drawdown_pct": 6.4, "trade_count": 167},
            {"sharpe": 0.58, "excess_sharpe_vs_basket": 0.45, "max_drawdown_pct": 7.1, "trade_count": 139},
        ]
        incumbent = [
            {"sharpe": 0.09, "max_drawdown_pct": 19.0},
            {"sharpe": 2.22, "max_drawdown_pct": 13.4},
            {"sharpe": 0.29, "max_drawdown_pct": 15.0},
        ]
        calibration = {"quantiles": {"mean_excess_sharpe_vs_basket_q99": 0.42, "worst_excess_sharpe_vs_basket_q99": 0.01}}
        summary = classify_results(candidate, incumbent, calibration)
        self.assertEqual(summary["status"], "frontier")          # loses fold 2 by 0.11, inside tolerance
        candidate[1]["sharpe"] = 1.80                            # loses fold 2 by 0.42, outside tolerance
        summary = classify_results(candidate, incumbent, calibration)
        self.assertFalse(summary["beats_incumbent"])
        self.assertEqual(summary["classification"], "risk_reducer")   # mean still > -0.10, drawdown < half

    def test_new_leaves_validate_with_secondary_windows(self):
        proposal = self.proposal(strategy="generated_spec", sparams="")
        proposal["proposal_type"] = "generated_spec"
        proposal["spec"] = {
            "entry": {"all": [
                {"type": "zscore_return_above", "window": 90, "vol_window": 30, "threshold": 0.5},
                {"type": "vol_rank_below", "window": 30, "rank_window": 250, "threshold": 0.8},
                {"type": "market_zscore_above", "window": 90, "threshold": 0.0},
            ]},
            "exit": {"any": [
                {"type": "atr_trailing_stop", "window": 20, "threshold": 3.0},
                {"type": "zscore_return_below", "window": 90, "threshold": -0.5},
            ]},
        }
        candidate = validate_proposal(proposal, self.mission, self.families)
        self.assertEqual(candidate["proposal"]["spec"]["entry"]["all"][0]["vol_window"], 30)

    def test_secondary_window_on_wrong_leaf_is_rejected(self):
        proposal = self.proposal(strategy="generated_spec", sparams="")
        proposal["proposal_type"] = "generated_spec"
        proposal["spec"] = {"entry": {"type": "rsi_above", "window": 14, "threshold": 70, "vol_window": 30},
                            "exit": {"type": "red_candle"}}
        with self.assertRaisesRegex(Exception, "unknown fields"):
            validate_proposal(proposal, self.mission, self.families)
        proposal["spec"] = {"entry": {"type": "atr_trailing_stop", "window": 20, "threshold": 0.1}, "exit": {"type": "red_candle"}}
        with self.assertRaisesRegex(Exception, "ATR multiple"):
            validate_proposal(proposal, self.mission, self.families)

    def test_spec_schema_describes_every_leaf_exactly(self):
        schema = proposal_schema("generated_spec")
        leaves = schema["$defs"]["leaf"]["anyOf"]
        self.assertEqual({leaf["properties"]["type"]["enum"][0] for leaf in leaves}, set(LEAF_SPEC))
        stop = next(leaf for leaf in leaves if leaf["properties"]["type"]["enum"] == ["atr_trailing_stop"])
        self.assertEqual(stop["required"], ["type", "window", "threshold"])
        self.assertEqual(stop["properties"]["threshold"]["minimum"], 0.5)
        self.assertEqual(schema["properties"]["spec"]["properties"]["entry"], {"$ref": "#/$defs/node"})

    def test_generator_prompt_states_units_incumbent_and_tested_configs(self):
        incumbent = [{"sharpe": 0.09, "max_drawdown_pct": 19.0}, {"sharpe": 2.22, "max_drawdown_pct": 13.4}, {"sharpe": 0.29, "max_drawdown_pct": 15.0}]
        specs = [{"entry": {"type": "close_above_sma", "window": 50, "threshold": 0}, "exit": {"type": "red_candle"},
                  "outcome": "killed", "sharpe_vs_incumbent": -0.7}]
        system, user = generator_prompts("generated_spec", 2, "window out of range", self.mission, self.families,
                                         incumbent, specs, [], {"close_above_sma": 13, "weekday": 14}, {"killed": 3},
                                         {"quantiles": {"mean_excess_sharpe_vs_basket_q99": 0.42}})
        self.assertIn("generated_spec", system)
        for needle in ("fold1 Sharpe 0.09", "0.25", "sigma units", "percentile 0-1", "ATR multiple",
                       "OVER-USED", "ALREADY TESTED", "close_above_sma", "ATTEMPT 2 of 3", "window out of range", "Sunday=0"):
            self.assertIn(needle, user, needle)
        catalogue = parse_family_catalogue(CLI_SAMPLE)
        self.assertEqual(catalogue["donchian"], "Donchian rule")
        self.assertEqual(set(catalogue), {"donchian", "conviction_breakout", "control_always_long"})
        system, user = generator_prompts("donchian", 1, "", self.mission, self.families, incumbent, [],
                                         [{"sparams": "entryWindow=50", "outcome": "killed", "sharpe_vs_incumbent": -0.1}], {}, {}, None,
                                         catalogue, {"donchian": {"tested": 5, "best_vs_incumbent": 0.21}})
        self.assertIn("strategy=donchian", user)
        self.assertIn("entryWindow=50", user)
        self.assertIn("STRATEGY ZOO", user)
        self.assertIn("conviction_breakout: causal rule [untested here]", user)
        self.assertIn("donchian: Donchian rule [tested 5x, best +0.21 vs incumbent]", user)
        self.assertNotIn("REGISTERED FAMILIES", user)          # other families' bounds stay out
        self.assertNotIn("trendWindow", user)                  # conviction_breakout's bound never appears

    def test_mechanism_must_be_prose_not_leaf_names(self):
        proposal = self.proposal()
        proposal["mechanism"] = "market_zscore_above + vol_rank_above + breakout_above filtering"
        with self.assertRaisesRegex(Exception, "eight words of prose"):
            validate_proposal(proposal, self.mission, self.families)

    def test_family_meta_reads_integer_marker_and_rounds(self):
        meta = parse_family_meta(NEW_FORMAT_SAMPLE)
        self.assertTrue(meta["ensemble_vote"]["enterVotes"]["is_int"])
        self.assertFalse(meta["faber_ma"]["bandPct"]["is_int"])
        self.assertFalse(meta["faber_ma"]["window"]["is_int"] is False and False)  # window is int
        self.assertTrue(meta["faber_ma"]["window"]["is_int"])
        family = {"enterVotes": (1.0, 3.0), "exitVotes": (0.0, 2.0)}
        self.assertEqual(parse_sparams("enterVotes=2.5,exitVotes=0.75", family, meta["ensemble_vote"]), "enterVotes=3,exitVotes=1")
        self.assertEqual(parse_sparams("enterVotes=2.5,exitVotes=0.75", family, None), "enterVotes=2.5,exitVotes=0.75")

    def test_near_duplicate_distance(self):
        meta = parse_family_meta(NEW_FORMAT_SAMPLE)["faber_ma"]
        a = normalized_parameters("window=200,bandPct=1.0", meta)
        b = normalized_parameters("window=210,bandPct=1.2", meta)     # 10/380 and 0.2/5 -> within 10%
        c = normalized_parameters("window=300,bandPct=1.0", meta)     # 100/380 -> 26%
        self.assertLess(parameter_distance(a, b), 0.10)
        self.assertGreater(parameter_distance(a, c), 0.10)
        self.assertEqual(normalized_parameters("", meta)["window"], (200 - 20) / 380)   # defaults filled in

    def test_saturation_rule(self):
        flat = [0.1] * 40
        self.assertTrue(is_saturated(flat))
        self.assertFalse(is_saturated(flat[:25]))                    # too few evaluations
        improving = [0.1] * 30 + [0.1] * 9 + [0.5]                    # best moved by 0.4 inside the last 20
        self.assertFalse(is_saturated(improving))

    def test_spec_signature_ignores_numbers(self):
        a = {"entry": {"all": [{"type": "close_above_sma", "window": 50}, {"type": "rsi_above", "window": 14, "threshold": 70}]},
             "exit": {"type": "close_below_sma", "window": 20}}
        b = {"entry": {"all": [{"type": "rsi_above", "window": 7, "threshold": 60}, {"type": "close_above_sma", "window": 120}]},
             "exit": {"type": "close_below_sma", "window": 50}}
        self.assertEqual(spec_signature(a), spec_signature(b))

    def test_trend_window_floor(self):
        proposal = self.proposal(strategy="generated_spec", sparams="")
        proposal["proposal_type"] = "generated_spec"
        proposal["spec"] = {"entry": {"type": "close_above_sma", "window": 6, "threshold": 0}, "exit": {"type": "red_candle"}}
        with self.assertRaisesRegex(Exception, "fee-dead"):
            validate_proposal(proposal, self.mission, self.families)
        proposal["spec"] = {"entry": {"type": "rsi_above", "window": 5, "threshold": 70}, "exit": {"type": "red_candle"}}
        validate_proposal(proposal, self.mission, self.families)      # RSI is not a trend leaf
        schema = proposal_schema("generated_spec")
        sma = next(l for l in schema["$defs"]["leaf"]["anyOf"] if l["properties"]["type"]["enum"] == ["close_above_sma"])
        self.assertEqual(sma["properties"]["window"]["minimum"], MIN_TREND_WINDOW)

    def test_prompt_states_plateau_and_novelty_rules(self):
        incumbent = [{"sharpe": 0.09, "max_drawdown_pct": 19.0}, {"sharpe": 2.22, "max_drawdown_pct": 13.4}, {"sharpe": 0.29, "max_drawdown_pct": 15.0}]
        system, user = generator_prompts("donchian", 1, "", self.mission, self.families, incumbent, [], [], {}, {}, None,
                                         {"donchian": "Donchian rule"}, {"donchian": {"tested": 40, "best_vs_incumbent": 0.2}},
                                         family_deltas=[0.1] * 40, saturated=frozenset({"donchian"}))
        self.assertIn("THIS FAMILY'S RECORD: 40 evaluated", user)
        self.assertIn("SATURATED", user)
        self.assertIn("near-duplicate", user)
        system, user = generator_prompts("generated_spec", 1, "", self.mission, self.families, incumbent, [], [], {"weekday": 5}, {}, None,
                                         novelty_policy={"leaf_cap_share": 0.4})
        self.assertIn("NOVELTY RULES", user)
        self.assertIn(f"below {MIN_TREND_WINDOW} bars", user)

    def test_zero_trade_report_parses_and_is_screened(self):
        from supervisor import parse_metrics, classify_results
        report = """===== Portfolio Report =====
Total return:            0.00%       -74.72%       74.72%
CAGR:                    0.00%       -49.79%       49.79%
Sharpe (ann.):           0.00         -0.36         0.36
  +/-                    0.71
Sortino (ann.):          0.00         -0.51
Max drawdown:            0.00%        88.94%   (close-to-close, see note)

Avg pairwise sleeve correlation: nan
--- per sleeve (each traded standalone, for reference) ---
  BTC_USDT                   0.00     -0.02      0.02      0.0        0      25.0
"""
        metrics = parse_metrics(report)
        self.assertEqual(metrics["trade_count"], 0)
        self.assertIsNone(metrics["sleeve_correlation"])
        summary = classify_results([metrics, metrics, metrics], None, None)
        self.assertEqual(summary["classification"], "zero_trade_fold")

    def test_frontier_requires_incumbent_and_null_gates(self):
        candidate = [
            {"sharpe": 1.2, "excess_sharpe_vs_basket": 0.6, "max_drawdown_pct": 10.0, "trade_count": 20},
            {"sharpe": 1.1, "excess_sharpe_vs_basket": 0.5, "max_drawdown_pct": 11.0, "trade_count": 20},
            {"sharpe": 0.9, "excess_sharpe_vs_basket": 0.45, "max_drawdown_pct": 12.0, "trade_count": 20},
        ]
        incumbent = [
            {"sharpe": 1.0, "max_drawdown_pct": 15.0},
            {"sharpe": 0.8, "max_drawdown_pct": 14.0},
            {"sharpe": 0.7, "max_drawdown_pct": 13.0},
        ]
        calibration = {"quantiles": {
            "mean_excess_sharpe_vs_basket_q99": 0.4,
            "worst_excess_sharpe_vs_basket_q99": 0.01,
        }}
        summary = classify_results(candidate, incumbent, calibration)
        self.assertEqual(summary["status"], "frontier")
        self.assertTrue(summary["beats_incumbent"])
        self.assertTrue(summary["beats_null"])


if __name__ == "__main__":
    unittest.main()