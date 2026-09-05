import unittest

from supervisor import (
    DEFAULT_MISSION,
    OllamaClient,
    classify_results,
    parse_family_specs,
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

    def test_non_loopback_ollama_is_rejected(self):
        with self.assertRaisesRegex(Exception, "loopback"):
            OllamaClient("http://example.com:11434", "qwen3-coder:latest")

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