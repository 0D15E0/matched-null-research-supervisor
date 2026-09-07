import json
import datetime as dt
import tempfile
import unittest
from pathlib import Path
from pathlib import Path
from tempfile import TemporaryDirectory

from supervisor import (
    DEFAULT_MISSION,
    Registry,
    Supervisor,
    enumerable_space_size,
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

    # ------------------------------------------------------------------
    # Exhaustion: a family that cannot produce a new configuration must leave
    # the rotation. Regression for 2026-09-06, when control_always_long (no
    # parameters, one configuration, already tested) was scheduled 40 times in
    # a row and opened the duplicate circuit breaker three times.
    # ------------------------------------------------------------------

    def test_enumerable_space_size(self):
        self.assertEqual(enumerable_space_size({}), 1)                       # no parameters: one configuration
        grid = {"enterVotes": {"lo": 1, "hi": 3, "is_int": True}, "exitVotes": {"lo": 0, "hi": 2, "is_int": True}}
        self.assertEqual(enumerable_space_size(grid), 9)
        continuous = {"bandPct": {"lo": 0.0, "hi": 5.0, "is_int": False}}
        self.assertIsNone(enumerable_space_size(continuous))
        huge = {"seed": {"lo": 1, "hi": 100000, "is_int": True}}
        self.assertIsNone(enumerable_space_size(huge))

    def _supervisor(self, families, family_meta, tmp):
        """A Supervisor with only the attributes the scheduler touches."""
        supervisor = Supervisor.__new__(Supervisor)
        supervisor.mission = self.mission
        supervisor.families = families
        supervisor.family_meta = family_meta
        supervisor.db = Registry(Path(tmp) / "t.sqlite3")
        supervisor.calibration_path = Path(tmp) / "missing.json"
        return supervisor

    def _record(self, supervisor, strategy, sparams="", ident=None):
        ident = ident or f"cand-{strategy}-{sparams or 'default'}"
        supervisor.db.insert_candidate({
            "candidate_id": ident, "proposal_hash": ident, "config_hash": ident,
            "mission_id": self.mission["mission_id"],
            "proposal": {"strategy": strategy, "sparams": sparams, "proposal_type": "family"},
        })
        supervisor.db.event("proposal_recorded", {"strategy": strategy}, ident)

    def test_parameterless_family_is_exhausted_by_its_only_configuration(self):
        families = {"control_always_long": {}, "faber_ma": {"window": (20.0, 400.0)}}
        meta = {"control_always_long": {}, "faber_ma": {"window": {"lo": 20, "hi": 400, "is_int": True}}}
        with tempfile.TemporaryDirectory() as tmp:
            supervisor = self._supervisor(families, meta, tmp)
            self.assertEqual(supervisor.exhausted(), {})            # untested: still worth one run
            self._record(supervisor, "control_always_long")
            exhausted = supervisor.exhausted()
            self.assertIn("control_always_long", exhausted)
            self.assertIn("takes no parameters", exhausted["control_always_long"])
            self.assertNotIn("faber_ma", exhausted)                 # 381 windows, one tested
            supervisor.db.close()

    def test_one_duplicate_exhausts_until_a_new_candidate_appears(self):
        families = {"faber_ma": {"window": (20.0, 400.0)}}
        meta = {"faber_ma": {"window": {"lo": 20, "hi": 400, "is_int": True}}}
        with tempfile.TemporaryDirectory() as tmp:
            supervisor = self._supervisor(families, meta, tmp)
            self._record(supervisor, "faber_ma", "window=200")
            self.assertEqual(supervisor.exhausted(), {})
            supervisor.db.event("duplicate_proposal", {"strategy": "faber_ma"})
            self.assertIn("faber_ma", supervisor.exhausted())       # routed away on the FIRST duplicate
            self._record(supervisor, "faber_ma", "window=120")      # a new configuration clears it
            self.assertEqual(supervisor.exhausted(), {})
            supervisor.db.close()

    def test_duplicate_exhaustion_expires_so_a_family_is_not_retired_forever(self):
        """An excluded family cannot record the candidate that would clear it, so the
        duplicate cooldown must expire on its own."""
        families = {"faber_ma": {"window": (20.0, 400.0)}}
        meta = {"faber_ma": {"window": {"lo": 20, "hi": 400, "is_int": True}}}
        with tempfile.TemporaryDirectory() as tmp:
            supervisor = self._supervisor(families, meta, tmp)
            self._record(supervisor, "faber_ma", "window=200")
            supervisor.db.event("duplicate_proposal", {"strategy": "faber_ma"})
            self.assertIn("faber_ma", supervisor.exhausted())
            stale = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=48)).replace(microsecond=0).isoformat()
            supervisor.db.db.execute("UPDATE events SET created_at=? WHERE event_type='duplicate_proposal'", (stale,))
            supervisor.db.db.commit()
            self.assertEqual(supervisor.exhausted(), {})            # cooldown expired: back in rotation
            supervisor.db.close()

    def test_scheduler_leaves_a_starved_mode_instead_of_repeating_it(self):
        """The exact dead end: the only eligible control cannot produce a new configuration.

        A duplicate records no candidate, so the control quota deficit never
        shrinks and the mode keeps winning. The scheduler must drop the mode.
        """
        families = {"control_always_long": {}, "faber_ma": {"window": (20.0, 400.0)}}
        meta = {"control_always_long": {}, "faber_ma": {"window": {"lo": 20, "hi": 400, "is_int": True}}}
        with tempfile.TemporaryDirectory() as tmp:
            supervisor = self._supervisor(families, meta, tmp)
            self._record(supervisor, "control_always_long")
            for window in (60, 90, 120, 150):                        # families are over-represented
                self._record(supervisor, "faber_ma", f"window={window}")
            focus = supervisor.state_context()["next_search_focus"]
            self.assertNotEqual(focus["strategy"], "control_always_long")
            self.assertIn("control_always_long", focus["exhausted"])
            supervisor.db.close()

    def test_generated_spec_is_never_saturated_or_exhausted(self):
        families = {"faber_ma": {"window": (20.0, 400.0)}}
        meta = {"faber_ma": {"window": {"lo": 20, "hi": 400, "is_int": True}}}
        with tempfile.TemporaryDirectory() as tmp:
            supervisor = self._supervisor(families, meta, tmp)
            for index in range(40):                                  # a long flat run of specs
                supervisor.db.insert_candidate({
                    "candidate_id": f"cand-spec-{index}", "proposal_hash": f"h{index}", "config_hash": f"c{index}",
                    "mission_id": self.mission["mission_id"],
                    "proposal": {"strategy": "generated_spec", "sparams": "", "proposal_type": "generated_spec"},
                })
                supervisor.db.update_candidate(f"cand-spec-{index}", "killed", "basket_screen_failed",
                                               {"mean_excess_sharpe_vs_incumbent": 0.1})
            supervisor.db.event("duplicate_proposal", {"strategy": "generated_spec"})
            self.assertNotIn("generated_spec", supervisor.saturation())
            self.assertNotIn("generated_spec", supervisor.exhausted())
            supervisor.db.close()

    def test_run_iteration_reroutes_past_a_duplicate_instead_of_burning_the_iteration(self):
        """One duplicate must cost one model call, not a whole iteration.

        Before the fix a duplicate ended the iteration, the daemon slept its
        interval, and the scheduler re-picked the same family, because a
        duplicate records no candidate and so cannot reduce the quota deficit
        that selected it. Forty of those ran in a row on 2026-09-06.

        `faber_ma` here has a large parameter space, so it is NOT statically
        exhausted: it becomes ineligible only once it has actually duplicated,
        which is the path the reroute has to handle.
        """
        from supervisor import DuplicateProposal, TraceLogger
        families = {"faber_ma": {"window": (20.0, 400.0)}, "kama_trend": {"erWindow": (5.0, 100.0)}}
        meta = {"faber_ma": {"window": {"lo": 20, "hi": 400, "is_int": True}},
                "kama_trend": {"erWindow": {"lo": 5, "hi": 100, "is_int": True}}}
        with tempfile.TemporaryDirectory() as tmp:
            supervisor = self._supervisor(families, meta, tmp)
            supervisor.trace = TraceLogger(None)
            supervisor.heartbeat_path = Path(tmp) / "hb.json"
            supervisor.model, supervisor.reviewer_model, supervisor.mission_hash = "m", "r", "h"
            self._record(supervisor, "faber_ma", "window=200")
            self._record(supervisor, "kama_trend", "erWindow=20")     # kama is better tested...
            self._record(supervisor, "kama_trend", "erWindow=40")     # ...so faber_ma is picked first
            calls = []

            def fake_run_once(evaluate: bool):
                focus = supervisor.state_context()["next_search_focus"]["strategy"]
                calls.append(focus)
                if focus == "faber_ma":
                    supervisor.db.event("duplicate_proposal", {"strategy": focus}, "cand-faber_ma-window=200")
                    raise DuplicateProposal("duplicate candidate configuration", "cand-faber_ma-window=200", focus)
                return {"candidate_id": "cand-new", "status": "survives_development"}

            supervisor.run_once = fake_run_once
            supervisor.search_policy = lambda: {
                "quotas": {"family": 1.0}, "quota_window": 100, "exclude": [],
                "saturation": {"min_evaluations": 30, "window": 20, "min_improvement": 0.10, "drift_every": 0},
                "near_duplicate_distance": 0.10, "spec_novelty": {"leaf_cap_share": 0.4, "leaf_cap_window": 30},
                "exhaust_after_duplicates": 1, "exhaust_duplicate_hours": 6.0, "preferred_sequence": [],
            }
            result = supervisor.run_iteration(iteration=1)
            self.assertEqual(result["candidate_id"], "cand-new")      # the iteration still produced a candidate
            self.assertEqual(calls[0], "faber_ma")                    # it duplicated once...
            self.assertEqual(calls[1], "kama_trend")                  # ...and the next call went elsewhere
            self.assertEqual(len(calls), 2)
            self.assertIn("faber_ma", supervisor.exhausted())
            rerouted = supervisor.db.db.execute(
                "SELECT COUNT(*) FROM events WHERE event_type='focus_rerouted'").fetchone()[0]
            self.assertEqual(rerouted, 1)
            supervisor.db.close()

    def test_run_iteration_gives_up_when_the_scheduler_cannot_move(self):
        """If the reroute would land on the same focus, raise rather than loop."""
        from supervisor import DuplicateProposal, TraceLogger
        families = {"faber_ma": {"window": (20.0, 400.0)}}
        meta = {"faber_ma": {"window": {"lo": 20, "hi": 400, "is_int": True}}}
        with tempfile.TemporaryDirectory() as tmp:
            supervisor = self._supervisor(families, meta, tmp)
            supervisor.trace = TraceLogger(None)
            supervisor.heartbeat_path = Path(tmp) / "hb.json"
            supervisor.model, supervisor.reviewer_model, supervisor.mission_hash = "m", "r", "h"
            calls = []

            def always_duplicate(evaluate: bool):
                calls.append(supervisor.state_context()["next_search_focus"]["strategy"])
                raise DuplicateProposal("duplicate candidate configuration", "cand-x", "faber_ma")

            supervisor.run_once = always_duplicate
            with self.assertRaises(DuplicateProposal):
                supervisor.run_iteration(iteration=1, max_reroutes=3)
            self.assertLessEqual(len(calls), 4)                        # bounded, never an inner loop
            supervisor.db.close()

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