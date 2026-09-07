#!/usr/bin/env python3
"""Local-only Ollama research supervisor.

The model proposes one structured candidate. This process validates it,
constructs allowlisted development-only commands, runs the deterministic
backtester, and stores every state transition in SQLite. It never accepts a
date or command from model output and never has a holdout or deployment mode.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import math
import os
import re
import signal
import sqlite3
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Iterator


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_MISSION = PROJECT_ROOT / "missions" / "local-trend-discovery-v4.json"
DEFAULT_DB = PROJECT_ROOT / "state" / "research-v2.sqlite3"
DEFAULT_ARTIFACTS = PROJECT_ROOT / "artifacts-v2"
DEFAULT_HEARTBEAT = PROJECT_ROOT / "state" / "heartbeat-v2.json"
DEFAULT_CALIBRATION = PROJECT_ROOT / "state" / "null-calibration-v4.json"
DEFAULT_TRACE = PROJECT_ROOT / "logs" / "trace-v4.jsonl"
DEFAULT_MODEL = "gpt-oss:20b"
# Context window for every Ollama call. The generator prompt runs 6.9k tokens
# at the median and 7.9k at the maximum (measured 2026-09-05 on 44 recorded
# proposals); at 8192 that left nothing for the model's reasoning channel and
# 8 of 21 requests came back with empty content. 16k costs a few hundred MB
# of KV cache on a 13 GB model and removes the ceiling.
DEFAULT_NUM_CTX = 16384
# Reasoning effort for the GENERATOR only. gpt-oss at its default "medium"
# spends 1.7k-3.4k characters thinking per proposal; "low" measured 160-370
# with identical validity and 2-4x lower latency. The reviewer model is not a
# thinking model and must not receive the field at all (Ollama rejects it).
DEFAULT_GENERATOR_THINK = "low"
DEFAULT_REVIEWER_MODEL = "qwen3-coder:latest"
DEFAULT_ENDPOINT = "http://127.0.0.1:11434"
DEFAULT_OLLAMA_MIN_INTERVAL = 10.0
MAX_RESPONSE_BYTES = 2_000_000
RESEARCH_PRIORS = {
    "dead_regions": [
        "sub-4h trend and mean-reversion variants are fee-dead under repository costs",
        "daily variants of the same rules have been weaker than 4h",
        "exposure-reducing Hurst/order/Fibonacci confirmation filters failed prior tests",
        "conviction sizing, asymmetric exits, and conviction reallocation failed prior tests",
        "adding coins beyond the four-coin protocol universe weakened the comparable book",
        "cross-sectional momentum, chart patterns, and Fibonacci entries failed prior tests",
    ],
    "calendar_weekday_convention": "UTC tm_wday: Sunday=0, Monday=1, Tuesday=2, Wednesday=3, Thursday=4, Friday=5, Saturday=6",
    "instruction": "A proposal in a dead region needs a materially different mechanism; otherwise explore a different region.",
}
SAFE_NAME = re.compile(r"^[a-z][a-z0-9_-]{2,63}$")
SAFE_SPARAM = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*=[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$"
)
FAMILY_LINE = re.compile(r"^  ([a-z][a-z0-9_]*)\s+")
PARAM = re.compile(
    r"([A-Za-z_][A-Za-z0-9_]*)=([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*\["
    r"([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\.\."
    r"([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\]"
)


class SupervisorError(RuntimeError):
    """A policy, runtime, or protocol error that should be shown to the user."""


class ScheduledFocusSkipped(SupervisorError):
    """The model could not produce a valid proposal for one scheduled focus."""


class DuplicateProposal(SupervisorError):
    """A normalized candidate configuration already exists."""

    def __init__(self, message: str, candidate_id: str | None = None, strategy: str | None = None) -> None:
        super().__init__(message)
        self.candidate_id = candidate_id
        self.strategy = strategy


class CircuitBreakerOpen(SupervisorError):
    """The mission has paused after too many consecutive proposal failures."""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    os.replace(temporary, path)


class TraceLogger:
    """Append-only local trace for model dialogue and evaluator boundaries."""

    def __init__(self, path: Path | None = DEFAULT_TRACE, console: bool = False) -> None:
        self.path = None if path is None else path.resolve()
        self.console = console

    def emit(self, event: str, **fields: Any) -> None:
        record = {"timestamp": utc_now(), "event": event, **fields}
        try:
            line = json.dumps(record, sort_keys=True, ensure_ascii=True, allow_nan=False, default=str)
        except (TypeError, ValueError) as error:
            record = {
                "timestamp": utc_now(),
                "event": "trace.serialization_error",
                "original_event": event,
                "error": str(error),
            }
            line = json.dumps(record, sort_keys=True, ensure_ascii=True)
        if self.path is not None:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as stream:
                    stream.write(line + "\n")
                    stream.flush()
            except OSError:
                # Tracing must never change the research result or stop the daemon.
                pass
        if self.console:
            self._print_console(event, fields, line)

    @staticmethod
    def _print_console(event: str, fields: dict[str, Any], line: str) -> None:
        if event == "llm.chunk":
            text = fields.get("thinking") or fields.get("content") or ""
            if text:
                channel = "thinking" if fields.get("thinking") else "content"
                print(f"[trace:{channel}] {text}", end="", file=sys.stderr, flush=True)
            if fields.get("done"):
                print(file=sys.stderr, flush=True)
            return
        if event == "llm.request_start":
            print(
                f"\n[trace] {fields.get('role', 'model')} request "
                f"{fields.get('request_id', '-')}: prompt={fields.get('user_chars', 0)} chars",
                file=sys.stderr,
                flush=True,
            )
            return
        print(f"[trace] {line}", file=sys.stderr, flush=True)


def percentile(values: list[float], probability: float) -> float:
    if not values:
        raise SupervisorError("cannot calculate a percentile from no values")
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def parse_date(value: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError as error:
        raise SupervisorError(f"invalid ISO date: {value}") from error


def resolve_inside(path: Path, root: Path, label: str) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as error:
        raise SupervisorError(f"{label} escapes its allowed root: {resolved}") from error
    return resolved


def safe_child_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for key in list(environment):
        upper = key.upper()
        if (
            upper.startswith("POLONIEX_") or
            any(token in upper for token in ("API_KEY", "API_SECRET", "PASSWORD", "TOKEN")) or
            upper.lower() in {"http_proxy", "https_proxy", "all_proxy", "ftp_proxy"}
        ):
            environment.pop(key, None)
    environment["NO_PROXY"] = "*"
    environment["no_proxy"] = "*"
    return environment


def git_revision(repo: Path) -> str:
    git = "/usr/bin/git" if Path("/usr/bin/git").is_file() else "git"
    try:
        completed = subprocess.run(
            [git, "rev-parse", "HEAD"], cwd=repo, text=True,
            capture_output=True, timeout=10, check=False, env=safe_child_environment(),
        )
    except OSError:
        return "unavailable"
    return completed.stdout.strip() if completed.returncode == 0 else "unavailable"


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise SupervisorError(f"could not read JSON {path}: {error}") from error
    if not isinstance(value, dict):
        raise SupervisorError(f"JSON root must be an object: {path}")
    return value


def validate_mission(path: Path) -> dict[str, Any]:
    mission = load_json(path)
    required = {"mission_id", "source_repo", "data_policy", "allowed_commands", "universe", "evaluation", "incumbent", "limits", "permissions"}
    missing = required - set(mission)
    if missing:
        raise SupervisorError(f"mission is missing fields: {', '.join(sorted(missing))}")
    if not SAFE_NAME.fullmatch(str(mission["mission_id"])):
        raise SupervisorError("mission_id is not a safe identifier")

    data_policy = mission["data_policy"]
    if data_policy.get("development_end") != "2023-12-31":
        raise SupervisorError("v1 requires development_end=2023-12-31")
    if data_policy.get("holdout_start") != "2024-01-01":
        raise SupervisorError("v1 requires holdout_start=2024-01-01")
    if data_policy.get("allow_data_fetch"):
        raise SupervisorError("autonomous data fetching is forbidden")

    repo = resolve_inside(PROJECT_ROOT / mission["source_repo"], PROJECT_ROOT.parent, "source_repo")
    if repo.name != "cli_trader":
        raise SupervisorError(f"source_repo must resolve to cli_trader, got {repo}")
    mission["_source_repo"] = str(repo)

    allowed_dirs = data_policy.get("allowed_data_dirs", [])
    if not allowed_dirs:
        raise SupervisorError("mission has no allowed data directories")
    resolved_dirs = []
    for raw_dir in allowed_dirs:
        resolved_dirs.append(str(resolve_inside(PROJECT_ROOT / raw_dir, repo, "data directory")))
    mission["_allowed_data_dirs"] = resolved_dirs

    forbidden = data_policy.get("forbidden_paths", [])
    allowed_resolved = [Path(value).resolve() for value in mission["_allowed_data_dirs"]]
    for raw_path in forbidden:
        candidate = (PROJECT_ROOT / raw_path).resolve()
        if any(candidate == allowed or allowed in candidate.parents for allowed in allowed_resolved):
            raise SupervisorError(f"forbidden path overlaps allowed data: {candidate}")

    allowed_commands = set(mission["allowed_commands"])
    if not allowed_commands <= {"portfolio", "backtest", "list-strategies", "causality_check"}:
        raise SupervisorError("mission contains a command outside the autonomous allowlist")
    if "portfolio" not in allowed_commands:
        raise SupervisorError("portfolio is required for the v1 evaluator")

    evaluation = mission["evaluation"]
    folds = evaluation.get("folds", [])
    if not folds:
        raise SupervisorError("mission has no evaluation folds")
    development_end = parse_date(data_policy["development_end"])
    for fold in folds:
        if not isinstance(fold, list) or len(fold) != 2:
            raise SupervisorError("each fold must be [start, end]")
        start, end = parse_date(fold[0]), parse_date(fold[1])
        if start >= end or end > development_end:
            raise SupervisorError(f"fold is outside the development boundary: {fold}")
    if evaluation.get("primary_metric") != "mean_excess_sharpe_vs_incumbent":
        raise SupervisorError("v1 primary metric must be incumbent-relative")
    if evaluation.get("weights") != "equal":
        raise SupervisorError("active mission fixes portfolio weights to equal")
    if float(evaluation.get("vol_target", 0)) != 0.20:
        raise SupervisorError("active mission fixes vol_target to 0.20")
    if int(evaluation.get("vol_window", 0)) != 30:
        raise SupervisorError("active mission requires vol_window=30 to match the repository protocol")
    if int(evaluation.get("warmup_bars", 0)) < 600:
        raise SupervisorError("active mission requires at least 600 warm-up bars for generated specs")
    if int(evaluation.get("min_trades_per_fold", 0)) < 1:
        raise SupervisorError("active mission requires min_trades_per_fold >= 1")
    if int(evaluation.get("max_trades_per_fold", 0)) <= int(evaluation.get("min_trades_per_fold", 0)):
        raise SupervisorError("active mission requires max_trades_per_fold above min_trades_per_fold")
    if not mission["permissions"].get("allow_holdout") is False:
        raise SupervisorError("v1 cannot allow holdout access")
    if not mission["permissions"].get("allow_deployment") is False:
        raise SupervisorError("v1 cannot allow deployment")
    if not mission["permissions"].get("allow_source_edits") is False:
        raise SupervisorError("v1 cannot allow source edits")
    return mission


def parse_family_specs(output: str) -> dict[str, dict[str, tuple[float, float]]]:
    families: dict[str, dict[str, tuple[float, float]]] = {}
    current: str | None = None
    block: list[str] = []

    def flush() -> None:
        if current is None:
            return
        text = "".join(block)
        families[current] = {
            parameter.group(1): (float(parameter.group(3)), float(parameter.group(4)))
            for parameter in PARAM.finditer(text)
        }

    for line in output.splitlines():
        if line.startswith("Available strategies:"):
            flush()
            break
        match = FAMILY_LINE.match(line)
        if match:
            flush()
            current = match.group(1)
            block = [line]
            continue
        if current is None:
            continue
        block.append(line)
    flush()
    if not families:
        raise SupervisorError("could not discover strategy families from cli_trader")
    return families


def parse_family_meta(output: str) -> dict[str, dict[str, dict[str, Any]]]:
    """name -> {param: {lo, hi, default, is_int}} from `list-strategies`.

    Integer-ness is read from the text: the registry prints integer parameters
    without a decimal point and real ones always with one (main.cpp,
    cmdListStrategies). The registry ROUNDS integer parameters before running a
    strategy, so enterVotes=2.5 executes as enterVotes=3; canonicalising with
    the same rounding makes the two hash alike and stops a fractional vote count
    from passing the duplicate detector as a "new" configuration.
    """
    meta: dict[str, dict[str, dict[str, Any]]] = {}
    current: str | None = None
    block: list[str] = []

    def flush() -> None:
        if current is None:
            return
        text = "".join(block)
        meta[current] = {}
        for m in PARAM.finditer(text):
            tokens = (m.group(2), m.group(3), m.group(4))
            is_int = all("." not in t and "e" not in t.lower() for t in tokens)
            meta[current][m.group(1)] = {
                "lo": float(m.group(3)), "hi": float(m.group(4)),
                "default": float(m.group(2)), "is_int": is_int,
            }

    for line in output.splitlines():
        if line.startswith("Available strategies:"):
            flush()
            break
        match = FAMILY_LINE.match(line)
        if match:
            flush()
            current = match.group(1)
            block = [line]
            continue
        if current is None:
            continue
        block.append(line)
    flush()
    return meta


def normalized_parameters(sparams: str, meta: dict[str, dict[str, Any]]) -> dict[str, float]:
    """Every parameter of the family scaled to [0,1] over its legal range, defaults filled in."""
    values = {name: float(m["default"]) for name, m in meta.items()}
    for piece in sparams.split(","):
        piece = piece.strip()
        if "=" in piece:
            name, text = piece.split("=", 1)
            if name in values:
                values[name] = float(text)
    out = {}
    for name, m in meta.items():
        span = float(m["hi"]) - float(m["lo"])
        out[name] = (values[name] - float(m["lo"])) / span if span > 0 else 0.0
    return out


def enumerable_space_size(meta: dict[str, dict[str, Any]], cap: int = 10_000) -> int | None:
    """How many distinct configurations a family can express, or None if effectively unbounded.

    A family whose parameters are all integers spans a finite grid; one with no
    parameters at all spans exactly one configuration. Once the ledger holds
    that many distinct parameter sets the family cannot produce anything new,
    and scheduling it again can only ever yield a duplicate.

    That is not hypothetical: `control_always_long` takes no parameters, so its
    single configuration was tested on 2026-09-05 and every later proposal for
    it was an exact duplicate. Because a duplicate records no candidate, and the
    quota deficit is measured in RECORDED candidates, the control mode stayed
    starved, the scheduler re-picked the same family, and twenty of those opened
    the circuit breaker (three times, 2026-09-06 09:07 to 09:35). Four more
    parameterless families - pure_ichimoku, pencil_extrap, patterns and
    fib_ichimoku - were one duplicate each away from the same trap.

    Returns None when any parameter is continuous or the grid exceeds `cap`,
    which is the honest answer for `control_random` (a continuous entry
    probability and a seed spanning 100,000 values): a grid the loop could not
    cover in weeks is governed by saturation instead, not by exhaustion.
    """
    size = 1
    for spec in meta.values():
        if not spec.get("is_int"):
            return None
        low, high = int(round(float(spec["lo"]))), int(round(float(spec["hi"])))
        count = high - low + 1
        if count <= 0:
            return None
        size *= count
        if size > cap:
            return None
    return size


def parameter_distance(a: dict[str, float], b: dict[str, float]) -> float:
    """L-infinity distance between two normalised parameter vectors."""
    return max((abs(a[k] - b.get(k, 0.0)) for k in a), default=0.0)


def parse_family_catalogue(output: str) -> dict[str, str]:
    """name -> one-line provenance, from the same `list-strategies` text parse_family_specs reads.

    The provenance strings are the registry's own literature map ("Faber (2007)",
    "Donchian's 4-week rule; the Turtle system", ...). Handed to the generator
    they say what already exists as a named, tested family, so a generated rule
    can be asked to cover what they do not, instead of re-deriving one of them.
    """
    catalogue: dict[str, str] = {}
    for line in output.splitlines():
        if line.startswith("Available strategies:"):
            break
        match = FAMILY_LINE.match(line)
        if match:
            catalogue[match.group(1)] = line[match.end():].strip()
    return catalogue


def parse_sparams(raw: str, family: dict[str, tuple[float, float]],
                  meta: dict[str, dict[str, Any]] | None = None) -> str:
    if not isinstance(raw, str):
        raise SupervisorError("sparams must be a string")
    if not raw:
        return ""
    pieces = [piece.strip() for piece in raw.split(",") if piece.strip()]
    parsed: dict[str, float] = {}
    for piece in pieces:
        if not SAFE_SPARAM.fullmatch(piece):
            raise SupervisorError(f"invalid sparams entry: {piece!r}")
        name, value_text = piece.split("=", 1)
        if name in parsed:
            raise SupervisorError(f"duplicate sparams parameter: {name}")
        if name not in family:
            raise SupervisorError(f"unknown parameter for strategy: {name}")
        value = float(value_text)
        if not math.isfinite(value):
            raise SupervisorError(f"non-finite sparams value: {piece}")
        lo, hi = family[name]
        if value < lo or value > hi:
            raise SupervisorError(f"{name}={value_text} outside [{lo}, {hi}]")
        if meta and meta.get(name, {}).get("is_int"):
            # Mirror std::lround in registry.h (half away from zero); Python's
            # round() is half-to-even and would turn 2.5 into 2 where the
            # executor runs 3.
            value = math.copysign(math.floor(abs(value) + 0.5), value)
        parsed[name] = value
    def sort_key(item: tuple[str, float]) -> str:
        return item[0]
    return ",".join(f"{name}={value:g}" for name, value in sorted(parsed.items(), key=sort_key))


def proposal_schema(
    mode: str | None = None,
    family: str | None = None,
    family_parameters: dict[str, tuple[float, float]] | None = None,
) -> dict[str, Any]:
    number = {"type": "number"}
    integer = {"type": "integer", "minimum": 10}
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "proposal_type", "hypothesis", "strategy", "sparams", "vol_target", "weights",
            "vol_lookback", "rebalance", "mechanism", "reasoning",
            "expected_failure_mode", "novelty_key",
        ],
        "properties": {
            "proposal_type": {"type": "string", "enum": ["family"]},
            "hypothesis": {"type": "string", "minLength": 20},
            "strategy": {"type": "string"},
            "sparams": {"type": "string"},
            "vol_target": number,
            "weights": {"type": "string", "enum": ["equal"]},
            "vol_lookback": integer,
            "rebalance": {"type": "integer", "minimum": 1},
            "mechanism": {"type": "string", "minLength": 80},
            "reasoning": {"type": "string", "minLength": 40},
            "expected_failure_mode": {"type": "string", "minLength": 10},
            "novelty_key": {"type": "string", "minLength": 3, "maxLength": 128},
        },
    }
    if mode == "generated_spec":
        schema["properties"]["proposal_type"] = {"type": "string", "enum": ["generated_spec"]}
        schema["properties"]["strategy"] = {"type": "string", "enum": ["generated_spec"]}
        schema["properties"]["sparams"] = {"type": "string", "enum": [""]}
        schema["required"].append("spec")
        # Ollama's constrained decoding honours $defs/$ref and anyOf (verified
        # 2026-09-05 against gpt-oss:20b), so the rule tree is described
        # exactly: one object shape per leaf with its exact required fields,
        # and all/any/not nodes that recurse. An invalid leaf can then not be
        # generated at all, instead of being rejected after a 30-second call.
        schema["$defs"] = spec_rule_defs()
        schema["properties"]["spec"] = {
            "type": "object",
            "additionalProperties": False,
            "required": ["entry", "exit"],
            "properties": {
                "entry": {"$ref": "#/$defs/node"},
                "exit": {"$ref": "#/$defs/node"},
                "max_hold_bars": {"type": "integer", "minimum": 0, "maximum": 2000},
            },
        }
    elif mode == "feature_request":
        schema["properties"]["proposal_type"] = {"type": "string", "enum": ["feature_request"]}
        schema["properties"]["strategy"] = {"type": "string", "enum": ["feature_request"]}
        schema["properties"]["sparams"] = {"type": "string", "enum": [""]}
        schema["required"].append("feature")
        schema["properties"]["feature"] = {
            "type": "object",
            "additionalProperties": False,
            "required": ["name", "description", "transformation", "publication_lag_days"],
            "properties": {
                "name": {"type": "string"},
                "description": {"type": "string"},
                "transformation": {"type": "string"},
                "publication_lag_days": {"type": "integer", "minimum": 0, "maximum": 3650},
            },
        }
    elif family:
        schema["properties"]["strategy"] = {"type": "string", "enum": [family]}
        if family_parameters is not None and not family_parameters:
            schema["properties"]["sparams"] = {"type": "string", "enum": [""]}
    return schema


def spec_rule_defs() -> dict[str, Any]:
    """JSON-schema $defs for the generated_spec rule tree, derived from LEAF_SPEC."""
    leaves = []
    for name, spec in LEAF_SPEC.items():
        props: dict[str, Any] = {"type": {"type": "string", "enum": [name]}}
        for field in spec["fields"]:
            if field == "window":
                props["window"] = {"type": "integer", "minimum": MIN_TREND_WINDOW if name in TREND_LEAVES else 2, "maximum": 600}
            elif field == "threshold":
                lo, hi = spec["range"]
                props["threshold"] = {"type": "number", "minimum": lo, "maximum": hi}
            elif field == "day":
                props["day"] = {"type": "integer", "minimum": 0, "maximum": 6}
        for extra in spec["extra"]:
            lo, hi = SECONDARY_WINDOW_RANGE[extra]
            props[extra] = {"type": "integer", "minimum": lo, "maximum": hi}
        leaves.append({"type": "object", "additionalProperties": False,
                       "required": ["type", *spec["fields"]], "properties": props})
    children = {"type": "array", "minItems": 1, "maxItems": 6, "items": {"$ref": "#/$defs/node"}}
    node = {"anyOf": [
        {"$ref": "#/$defs/leaf"},
        {"type": "object", "additionalProperties": False, "required": ["all"], "properties": {"all": children}},
        {"type": "object", "additionalProperties": False, "required": ["any"], "properties": {"any": children}},
        {"type": "object", "additionalProperties": False, "required": ["not"], "properties": {"not": {"$ref": "#/$defs/node"}}},
    ]}
    return {"leaf": {"anyOf": leaves}, "node": node}


def leaf_reference_lines() -> list[str]:
    """The leaf table as prompt text: name, fields and the threshold's unit."""
    lines = []
    for name, spec in LEAF_SPEC.items():
        fields = ", ".join(spec["fields"] + tuple(f"{e}?" for e in spec["extra"])) or "no fields"
        lines.append(f"- {name} ({fields}): {spec['unit']}")
    return lines


GENERATOR_SYSTEM = (
    "You are the generator for a local quantitative research supervisor that studies long/flat "
    "rules on 4-hour crypto candles. Return exactly one JSON object matching the supplied schema; "
    "the schema is the contract, the prose below is the context. Never emit commands, file paths, "
    "source code, holdout references or deployment advice; the deterministic evaluator computes "
    "every metric and you must not claim one. Scheduled mode is {focus}: propose that and nothing "
    "else. A good proposal states a MECHANISM - why prices should behave this way, in terms of who "
    "is forced to trade, what information arrives slowly, or what risk is being paid for - and one "
    "concrete way it fails. Restating the parameters is not a mechanism. Do not re-propose a "
    "configuration listed under ALREADY TESTED; change the mechanism, not a digit. The novelty_key "
    "is a short slug such as donchian:wide-entry:atr-stop and belongs in no other field."
)


def generator_prompts(focus: str, attempt: int, feedback: str, mission: dict[str, Any],
                      families: dict[str, dict[str, tuple[float, float]]],
                      incumbent_rows: list[dict[str, Any]] | None,
                      recent_specs: list[dict[str, Any]],
                      recent_sparams: list[dict[str, Any]],
                      leaf_usage: dict[str, int],
                      counts: dict[str, int],
                      calibration: dict[str, Any] | None,
                      catalogue: dict[str, str] | None = None,
                      family_outcomes: dict[str, dict[str, Any]] | None = None,
                      family_deltas: list[float] | None = None,
                      saturated: set[str] | frozenset[str] | None = None,
                      novelty_policy: dict[str, Any] | None = None) -> tuple[str, str]:
    """Build (system, user) for one generator call. Pure: everything it says comes from its arguments.

    What the model is told, and why:
      * the evaluation in one paragraph (universe, folds, sizing, costs) - so
        window choices are made in bars of the right size;
      * the incumbent's fold Sharpes and what 'beats it' means - the target,
        not just 'do well';
      * what has ALREADY been tested in this mode, with outcomes - the model
        cannot avoid duplicates it cannot see, and 32 of today's 74 rejected
        proposals were duplicates;
      * for specs: every leaf with its threshold UNIT (percent vs fraction vs
        sigma vs percentile), plus which leaves are over-used so the search
        moves rather than re-deriving moving-average rules with weekday gates;
      * the repository's dead regions and the weekday convention;
      * the STRATEGY ZOO: every registered family with its one-line provenance
        and its outcome in this ledger - names only, never the other families'
        parameter bounds, which the model cannot act on once the schema pins
        the strategy and which cost 2.5k tokens per call. In spec mode the
        catalogue is what the generated rule must NOT re-derive.
    """
    evaluation = mission["evaluation"]
    folds = ", ".join(f"{a}..{b}" for a, b in evaluation["folds"])
    symbols = ", ".join(mission["universe"]["protocol_symbols"])
    header = [
        f"EVALUATION. Universe {symbols} at 4h bars (6 bars per day, 2190 per year); folds {folds}; "
        f"{evaluation.get('warmup_bars', 80)} warm-up bars; positions sized to {float(evaluation['vol_target']):.0%} "
        f"annualized volatility, equal sleeve weights; next-bar-open fills, 0.15% fee and 0.05% slippage per side. "
        f"Development data ends {mission['data_policy']['development_end']}; nothing later exists for you.",
    ]
    if incumbent_rows:
        cells = ", ".join(f"fold{i + 1} Sharpe {float(r['sharpe']):.2f} (drawdown {float(r['max_drawdown_pct']):.1f}%)"
                          for i, r in enumerate(incumbent_rows))
        header.append(
            f"INCUMBENT. {mission['incumbent']['strategy']} {mission['incumbent']['sparams']}: {cells}. "
            "A candidate reaches the frontier when its mean fold Sharpe exceeds the incumbent's, no fold "
            "trails by more than 0.25, its worst drawdown is not worse, and it clears the random-control null"
            + (f" (99th percentile mean excess Sharpe vs the basket {calibration['quantiles']['mean_excess_sharpe_vs_basket_q99']:.2f})" if calibration else "")
            + ". A candidate with half the incumbent's drawdown and similar Sharpe is also recorded as a risk reducer."
        )
    priors = RESEARCH_PRIORS
    header.append("DEAD REGIONS (measured; a proposal here needs a materially different mechanism): "
                  + "; ".join(priors["dead_regions"]) + ".")
    header.append(f"LEDGER so far: {json.dumps(counts, sort_keys=True)}.")
    if catalogue:
        lines = []
        for name, provenance in catalogue.items():
            outcome = (family_outcomes or {}).get(name)
            if outcome and outcome.get("tested"):
                best = outcome.get("best_vs_incumbent")
                tag = f" [tested {outcome['tested']}x" + (f", best {best:+.2f} vs incumbent" if best is not None else "") \
                      + (", SATURATED: its best stopped moving" if saturated and name in saturated else "") + "]"
            else:
                tag = " [untested here]"
            lines.append(f"- {name}: {provenance[:110]}{tag}")
        header.append(
            ("STRATEGY ZOO (every family the evaluator already has, with its source and its record in this ledger). "
             + ("In generated_spec mode: do not rebuild any of these out of leaves; compose a rule that covers a mechanism they do not."
                if focus == "generated_spec" else
                f"The scheduled family is {focus}; the rest is orientation, their parameters are not available in this call.")
             + "\n" + "\n".join(lines))
        )

    if focus == "generated_spec":
        used = sorted(leaf_usage.items(), key=lambda kv: -kv[1])
        over = [name for name, _ in used[:4]]
        under = [name for name in LEAF_SPEC if leaf_usage.get(name, 0) == 0]
        body = [
            "MODE: generated_spec. Set proposal_type=generated_spec, strategy=generated_spec, sparams=\"\".",
            "spec = {entry, exit, max_hold_bars}. entry and exit are boolean rule trees: a leaf, or {\"all\": [...]}, "
            "{\"any\": [...]} with 1-6 children, or {\"not\": node}; depth at most 4 and at most "
            f"{int(mission['limits']['max_parameters'])} leaves in total. max_hold_bars 0 means no time stop.",
            "LEAVES (name (fields): what the threshold means). Windows are in 4h bars: 6 = one day, 42 = one week, 180 = one month.",
            *leaf_reference_lines(),
            "Rules: atr_trailing_stop only makes sense in exit; market_zscore_* is the only leaf that sees another market; "
            f"weekday uses {priors['calendar_weekday_convention']}.",
            "Every leaf is a prediction about the next bars. Combine leaves that carry DIFFERENT information "
            "(trend, volatility regime, participation, the market factor, a risk stop) rather than three views of the same average.",
        ]
        if over:
            body.append(f"OVER-USED so far (avoid building yet another rule around these): {', '.join(over)}."
                        + (f" NEVER USED yet: {', '.join(under)}." if under else ""))
        cap = (novelty_policy or {}).get("leaf_cap_share", 0.4)
        body.append(
            "NOVELTY RULES, enforced before any backtest: a spec whose entry and exit use exactly the same SET of leaf "
            "types as a tested spec is rejected as a repeat, whatever its numbers; an entry leaf that already appears in "
            f"more than {cap:.0%} of recent specs is rejected; trend windows below {MIN_TREND_WINDOW} bars (two days) are "
            "rejected as fee-dead. A new spec must combine leaves that have not been combined before."
        )
        if recent_specs:
            body.append("ALREADY TESTED (entry -> exit; outcome; mean Sharpe vs incumbent):")
            for item in recent_specs:
                delta = item.get("sharpe_vs_incumbent")
                body.append(f"  {json.dumps(item['entry'], separators=(',', ':'))} -> {json.dumps(item['exit'], separators=(',', ':'))}; "
                            f"{item['outcome']}; {'n/a' if delta is None else f'{delta:+.2f}'}")
    elif focus == "feature_request":
        body = [
            "MODE: feature_request. Set strategy=feature_request and sparams=\"\". Provide "
            "feature={name,description,transformation,publication_lag_days} for a LOCAL data series a human "
            "could supply; never a URL. This is a note for a human, not a runnable rule.",
        ]
    else:
        bounds = families.get(focus, {})
        body = [f"MODE: family. Set proposal_type=family and strategy={focus}."]
        if bounds:
            body.append("PARAMETERS and legal ranges for this family (sparams is a comma-separated name=value list using "
                        "only these names; an empty string means the published defaults): "
                        + json.dumps({k: [lo, hi] for k, (lo, hi) in bounds.items()}))
            body.append("The repository measured flat plateaus in these parameters: a small tweak is noise. Propose a "
                        "setting that expresses a different regime or horizon and say why.")
        else:
            body.append("This family has no tunable parameters: sparams must be the empty string exactly.")
        if family_deltas:
            best = max(family_deltas)
            improved = best - max(family_deltas[:-20]) if len(family_deltas) > 20 else None
            body.append(
                f"THIS FAMILY'S RECORD: {len(family_deltas)} evaluated, best {best:+.2f}, median "
                f"{statistics.median(family_deltas):+.2f} vs the incumbent"
                + (f"; the last 20 evaluations moved the best by {improved:+.2f}" if improved is not None else "")
                + (". This family is SATURATED: only a setting that expresses a different horizon or regime is worth running."
                   if saturated and focus in saturated else ".")
            )
            body.append("A proposal whose every parameter lies within 10% of its legal range from a tested configuration is "
                        "rejected as a near-duplicate before any backtest.")
        if recent_sparams:
            body.append("ALREADY TESTED for this family (sparams; outcome; mean Sharpe vs incumbent):")
            for item in recent_sparams:
                delta = item.get("sharpe_vs_incumbent")
                body.append(f"  {item['sparams'] or '(defaults)'}; {item['outcome']}; {'n/a' if delta is None else f'{delta:+.2f}'}")
    tail = [f"ATTEMPT {attempt} of 3." + (f" PREVIOUS ATTEMPT REJECTED: {feedback}" if feedback else ""),
            "Return only the proposal JSON object."]
    return GENERATOR_SYSTEM.format(focus=focus), "\n".join(header + [""] + body + [""] + tail)


class OllamaClient:
    def __init__(self, endpoint: str, model: str, timeout: float = 180.0,
                 min_interval: float = DEFAULT_OLLAMA_MIN_INTERVAL,
                 num_ctx: int = DEFAULT_NUM_CTX, think: str | None = None,
                 trace: TraceLogger | None = None, role: str = "generator") -> None:
        parsed = urllib.parse.urlparse(endpoint)
        try:
            port = parsed.port
        except ValueError as error:
            raise SupervisorError("Ollama endpoint has an invalid port") from error
        if (
            parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or
            port != 11434 or parsed.username or parsed.password or
            parsed.path not in ("", "/") or parsed.query or parsed.fragment
        ):
            raise SupervisorError("Ollama endpoint must be exactly the loopback URL http://127.0.0.1:11434")
        if ":cloud" in model:
            raise SupervisorError("cloud Ollama models are forbidden")
        self.endpoint = "http://127.0.0.1:11434"
        self.model = model
        self.timeout = timeout
        self.num_ctx = int(num_ctx)
        self.think = think          # None = leave the model's default; "low"/"medium"/"high" for thinking models
        self.trace = trace
        self.role = role
        self.min_interval = max(0.0, float(min_interval))
        self.next_request_at = 0.0
        self.failure_streak = 0
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _NoRedirectHandler(),
        )

    def _request(self, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        wait = self.next_request_at - time.monotonic()
        if wait > 0.0:
            time.sleep(wait)
        self.next_request_at = time.monotonic() + self.min_interval
        data = None if payload is None else json.dumps(payload).encode()
        request = urllib.request.Request(
            self.endpoint + path,
            data=data,
            headers={"Content-Type": "application/json"} if data else {},
            method="POST" if data else "GET",
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
        except (urllib.error.URLError, TimeoutError) as error:
            self.failure_streak += 1
            backoff = min(60.0, float(2 ** min(self.failure_streak, 6)))
            self.next_request_at = max(self.next_request_at, time.monotonic() + backoff)
            raise SupervisorError(f"local Ollama request failed: {error}") from error
        self.failure_streak = 0
        if len(body) > MAX_RESPONSE_BYTES:
            raise SupervisorError("Ollama response exceeded the local response limit")
        try:
            value = json.loads(body)
        except json.JSONDecodeError as error:
            raise SupervisorError("Ollama returned invalid JSON") from error
        if not isinstance(value, dict):
            raise SupervisorError("Ollama response root was not an object")
        if value.get("error"):
            raise SupervisorError(f"Ollama error: {value['error']}")
        return value

    def _stream_request(self, path: str, payload: dict[str, Any], request_id: str) -> dict[str, Any]:
        wait = self.next_request_at - time.monotonic()
        if wait > 0.0:
            time.sleep(wait)
        self.next_request_at = time.monotonic() + self.min_interval
        data = json.dumps(payload).encode()
        request = urllib.request.Request(
            self.endpoint + path,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        content_chunks: list[str] = []
        thinking_chunks: list[str] = []
        final_response: dict[str, Any] = {}
        total_bytes = 0
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                while True:
                    raw_line = response.readline()
                    if not raw_line:
                        break
                    total_bytes += len(raw_line)
                    if total_bytes > MAX_RESPONSE_BYTES:
                        raise SupervisorError("Ollama response exceeded the local response limit")
                    if isinstance(raw_line, bytes):
                        raw_line = raw_line.decode("utf-8")
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        value = json.loads(line)
                    except json.JSONDecodeError as error:
                        raise SupervisorError("Ollama returned invalid streaming JSON") from error
                    if not isinstance(value, dict):
                        raise SupervisorError("Ollama streaming response was not an object")
                    if value.get("error"):
                        raise SupervisorError(f"Ollama error: {value['error']}")
                    final_response = value
                    message = value.get("message", {})
                    content = message.get("content") if isinstance(message, dict) else None
                    thinking = message.get("thinking") if isinstance(message, dict) else None
                    if isinstance(content, str) and content:
                        content_chunks.append(content)
                    if isinstance(thinking, str) and thinking:
                        thinking_chunks.append(thinking)
                    if self.trace is not None:
                        self.trace.emit(
                            "llm.chunk",
                            request_id=request_id,
                            role=self.role,
                            content=content or "",
                            thinking=thinking or "",
                            done=bool(value.get("done")),
                        )
        except (urllib.error.URLError, TimeoutError) as error:
            self.failure_streak += 1
            backoff = min(60.0, float(2 ** min(self.failure_streak, 6)))
            self.next_request_at = max(self.next_request_at, time.monotonic() + backoff)
            raise SupervisorError(f"local Ollama request failed: {error}") from error
        self.failure_streak = 0
        final_response["message"] = {
            "role": "assistant",
            "content": "".join(content_chunks),
        }
        if thinking_chunks:
            final_response["message"]["thinking"] = "".join(thinking_chunks)
        return final_response

    def tags(self) -> list[str]:
        value = self._request("/api/tags")
        models = value.get("models", [])
        return [str(item.get("name")) for item in models if isinstance(item, dict) and item.get("name")]

    def chat(self, system: str, user: str, format_schema: dict[str, Any] | None = None,
             trace_context: dict[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
        request_id = uuid.uuid4().hex
        schema = format_schema or proposal_schema()
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "stream": True,
            "format": schema,
            "options": {"temperature": 0.7, "num_ctx": self.num_ctx},
            "keep_alive": "10m",
        }
        if self.think is not None:
            payload["think"] = self.think
        context = trace_context or {}
        if self.trace is not None:
            self.trace.emit(
                "llm.request_start",
                request_id=request_id,
                role=self.role,
                model=self.model,
                endpoint=self.endpoint,
                system=system,
                user=user,
                schema=schema,
                **context,
            )
        started = time.monotonic()
        try:
            response = self._stream_request("/api/chat", payload, request_id)
        except SupervisorError as error:
            if self.trace is not None:
                self.trace.emit(
                    "llm.request_error",
                    request_id=request_id,
                    role=self.role,
                    model=self.model,
                    error=str(error),
                    duration_seconds=time.monotonic() - started,
                    **context,
                )
            raise
        message = response.get("message", {})
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content.strip():
            if self.trace is not None:
                self.trace.emit(
                    "llm.request_error",
                    request_id=request_id,
                    role=self.role,
                    model=self.model,
                    error="Ollama returned no proposal content",
                    duration_seconds=time.monotonic() - started,
                    **context,
                )
            raise SupervisorError("Ollama returned no proposal content")
        if self.trace is not None:
            self.trace.emit(
                "llm.request_end",
                request_id=request_id,
                role=self.role,
                model=self.model,
                content_chars=len(content),
                thinking_chars=len(message.get("thinking", "")) if isinstance(message, dict) else 0,
                duration_seconds=time.monotonic() - started,
                metadata={key: value for key, value in response.items() if key != "message"},
                **context,
            )
        return content, response


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise SupervisorError(f"Ollama redirect refused: {newurl}")


def review_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "assessment", "mechanism_status", "robustness_concerns",
            "selection_bias_concerns", "next_experiment_type",
            "recommended_action", "summary",
        ],
        "properties": {
            "assessment": {"type": "string", "enum": ["promising", "weak", "overfit_suspect", "duplicate", "insufficient_evidence"]},
            "mechanism_status": {"type": "string"},
            "robustness_concerns": {"type": "array", "items": {"type": "string"}},
            "selection_bias_concerns": {"type": "array", "items": {"type": "string"}},
            "next_experiment_type": {"type": "string"},
            "recommended_action": {"type": "string", "enum": ["keep_frontier", "keep_as_diversifier", "kill", "request_human_review"]},
            "summary": {"type": "string"},
        },
    }


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS missions (
    mission_id TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    model TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS candidates (
    candidate_id TEXT PRIMARY KEY,
    proposal_hash TEXT NOT NULL,
    config_hash TEXT NOT NULL UNIQUE,
    mission_id TEXT NOT NULL,
    status TEXT NOT NULL,
    proposal_json TEXT NOT NULL,
    classification TEXT,
    summary_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id TEXT NOT NULL,
    fold_index INTEGER NOT NULL,
    fold_start TEXT NOT NULL,
    fold_end TEXT NOT NULL,
    argv_json TEXT NOT NULL,
    status TEXT NOT NULL,
    returncode INTEGER,
    stdout_path TEXT,
    stderr_path TEXT,
    metrics_json TEXT,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE(candidate_id, fold_index)
);
CREATE TABLE IF NOT EXISTS reviews (
    review_id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id TEXT NOT NULL,
    review_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id TEXT,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS best_history (
    record_id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id TEXT NOT NULL,
    score REAL NOT NULL,
    status TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
"""


class Registry:
    def __init__(self, path: Path, read_only: bool = False) -> None:
        if not read_only:
            path.parent.mkdir(parents=True, exist_ok=True)
        if read_only and not path.is_file():
            raise SupervisorError(f"registry does not exist: {path}")
        self.path = path
        self.read_only = read_only
        self.db = sqlite3.connect(
            f"file:{path}?mode=ro", uri=True
        ) if read_only else sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        if not read_only:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.executescript(SCHEMA_SQL)
            self.db.commit()

    def close(self) -> None:
        self.db.close()

    def mission(self, mission: dict[str, Any], mission_path: Path, model: str) -> None:
        content_hash = sha256_file(mission_path)
        existing = self.db.execute(
            "SELECT content_hash FROM missions WHERE mission_id=?", (mission["mission_id"],)
        ).fetchone()
        if existing is not None and existing["content_hash"] != content_hash:
            raise SupervisorError(
                f"mission hash mismatch for {mission['mission_id']}; create a new mission id"
            )
        if existing is None:
            self.db.execute(
                "INSERT INTO missions VALUES (?, ?, ?, ?, ?)",
                (mission["mission_id"], content_hash, str(mission_path), utc_now(), model),
            )
        self.db.commit()

    def event(self, event_type: str, payload: dict[str, Any], candidate_id: str | None = None) -> None:
        self.db.execute(
            "INSERT INTO events(candidate_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?)",
            (candidate_id, event_type, canonical_json(payload), utc_now()),
        )
        self.db.commit()

    def has_config(self, config_hash: str) -> bool:
        return self.db.execute("SELECT 1 FROM candidates WHERE config_hash=?", (config_hash,)).fetchone() is not None

    def insert_candidate(self, candidate: dict[str, Any], status: str = "proposed") -> None:
        now = utc_now()
        self.db.execute(
            "INSERT INTO candidates VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)",
            (
                candidate["candidate_id"], candidate["proposal_hash"], candidate["config_hash"],
                candidate["mission_id"], status, canonical_json(candidate["proposal"]), now, now,
            ),
        )
        self.db.commit()

    def update_candidate(self, candidate_id: str, status: str, classification: str | None = None,
                         summary: dict[str, Any] | None = None) -> None:
        self.db.execute(
            "UPDATE candidates SET status=?, classification=?, summary_json=?, updated_at=? WHERE candidate_id=?",
            (status, classification, None if summary is None else canonical_json(summary), utc_now(), candidate_id),
        )
        self.db.commit()

    def candidate(self, candidate_id: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM candidates WHERE candidate_id=?", (candidate_id,)).fetchone()

    def candidate_runs(self, candidate_id: str) -> list[sqlite3.Row]:
        return list(self.db.execute("SELECT * FROM runs WHERE candidate_id=? ORDER BY fold_index", (candidate_id,)))

    def start_run(self, candidate_id: str, fold_index: int, start: str, end: str, argv: list[str]) -> int:
        try:
            cursor = self.db.execute(
            "INSERT INTO runs(candidate_id, fold_index, fold_start, fold_end, argv_json, status, started_at) VALUES (?, ?, ?, ?, ?, 'running', ?)",
            (candidate_id, fold_index, start, end, canonical_json(argv), utc_now()),
            )
        except sqlite3.IntegrityError as error:
            raise SupervisorError(
                f"run already exists for {candidate_id} fold {fold_index}; history is immutable"
            ) from error
        self.db.commit()
        return int(cursor.lastrowid)

    def finish_run(self, run_id: int, status: str, returncode: int | None,
                   stdout_path: Path, stderr_path: Path, metrics: dict[str, Any]) -> None:
        self.db.execute(
            "UPDATE runs SET status=?, returncode=?, stdout_path=?, stderr_path=?, metrics_json=?, completed_at=? WHERE run_id=?",
            (status, returncode, str(stdout_path), str(stderr_path), canonical_json(metrics), utc_now(), run_id),
        )
        self.db.commit()

    def add_review(self, candidate_id: str, review: dict[str, Any]) -> None:
        self.db.execute(
            "INSERT INTO reviews(candidate_id, review_json, created_at) VALUES (?, ?, ?)",
            (candidate_id, canonical_json(review), utc_now()),
        )
        self.db.commit()

    def counts(self) -> dict[str, int]:
        rows = self.db.execute("SELECT status, COUNT(*) AS n FROM candidates GROUP BY status")
        return {row["status"]: int(row["n"]) for row in rows}

    def latest_candidates(self, limit: int = 10) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT candidate_id, status, classification, summary_json, created_at FROM candidates ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        return [dict(row) for row in rows]

    def latest_events(self, limit: int = 10) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT event_type, candidate_id, payload_json, created_at FROM events ORDER BY event_id DESC LIMIT ?",
            (limit,),
        )
        return [dict(row) for row in rows]

    def strategy_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in self.db.execute("SELECT proposal_json FROM candidates"):
            try:
                strategy = json.loads(row["proposal_json"]).get("strategy")
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(strategy, str):
                counts[strategy] = counts.get(strategy, 0) + 1
        return counts

    def strategy_focus_failures(self) -> dict[str, float]:
        failures: dict[str, float] = {}
        now = dt.datetime.now(dt.timezone.utc)
        for row in self.db.execute(
            "SELECT payload_json, created_at FROM events "
            "WHERE event_type='scheduled_strategy_failed' ORDER BY event_id DESC LIMIT 200"
        ):
            try:
                strategy = json.loads(row["payload_json"]).get("strategy")
                created = dt.datetime.fromisoformat(row["created_at"])
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if isinstance(strategy, str):
                age_hours = max(0.0, (now - created).total_seconds() / 3600.0)
                failures[strategy] = failures.get(strategy, 0.0) + math.exp(-age_hours / 24.0)
        return failures

    def strategy_duplicate_failures(self) -> dict[str, float]:
        failures: dict[str, float] = {}
        now = dt.datetime.now(dt.timezone.utc)
        for row in self.db.execute(
            "SELECT candidate_id, payload_json, created_at FROM events "
            "WHERE event_type IN ('duplicate_proposal', 'semantic_duplicate') "
            "ORDER BY event_id DESC LIMIT 200"
        ):
            try:
                payload = json.loads(row["payload_json"])
                strategy = payload.get("strategy")
                created = dt.datetime.fromisoformat(row["created_at"])
            except (TypeError, ValueError, AttributeError, json.JSONDecodeError):
                continue
            if not isinstance(strategy, str) and row["candidate_id"]:
                candidate = self.db.execute(
                    "SELECT proposal_json FROM candidates WHERE candidate_id=?",
                    (row["candidate_id"],),
                ).fetchone()
                if candidate is not None:
                    try:
                        strategy = json.loads(candidate["proposal_json"]).get("strategy")
                    except (TypeError, json.JSONDecodeError):
                        strategy = None
            if isinstance(strategy, str):
                age_hours = max(0.0, (now - created).total_seconds() / 3600.0)
                failures[strategy] = failures.get(strategy, 0.0) + math.exp(-age_hours / 24.0)
        return failures

    def recent_spec_configs(self, limit: int = 8) -> list[dict[str, Any]]:
        """entry/exit trees of the most recent generated_spec candidates, with outcome."""
        out = []
        for row in self.db.execute(
            "SELECT proposal_json, status, classification, summary_json FROM candidates ORDER BY created_at DESC LIMIT 200"
        ):
            try:
                proposal = json.loads(row["proposal_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            if proposal.get("proposal_type") != "generated_spec" or not proposal.get("spec"):
                continue
            outcome = row["classification"] or row["status"]
            delta = None
            if row["summary_json"]:
                try:
                    delta = json.loads(row["summary_json"]).get("mean_excess_sharpe_vs_incumbent")
                except (TypeError, json.JSONDecodeError):
                    delta = None
            out.append({"entry": proposal["spec"].get("entry"), "exit": proposal["spec"].get("exit"),
                        "outcome": outcome, "sharpe_vs_incumbent": delta})
            if len(out) >= limit:
                break
        return out

    def recent_family_sparams(self, strategy: str, limit: int = 12) -> list[dict[str, Any]]:
        """sparams already tried for one family, with outcome, newest first."""
        out = []
        for row in self.db.execute(
            "SELECT proposal_json, status, classification, summary_json FROM candidates ORDER BY created_at DESC LIMIT 400"
        ):
            try:
                proposal = json.loads(row["proposal_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            if proposal.get("strategy") != strategy:
                continue
            delta = None
            if row["summary_json"]:
                try:
                    delta = json.loads(row["summary_json"]).get("mean_excess_sharpe_vs_incumbent")
                except (TypeError, json.JSONDecodeError):
                    delta = None
            out.append({"sparams": proposal.get("sparams", ""), "outcome": row["classification"] or row["status"],
                        "sharpe_vs_incumbent": delta})
            if len(out) >= limit:
                break
        return out

    def family_deltas(self) -> dict[str, list[float]]:
        """Per strategy, mean Sharpe vs incumbent of every scored candidate, in evaluation order."""
        out: dict[str, list[float]] = {}
        for row in self.db.execute("SELECT proposal_json, summary_json FROM candidates WHERE summary_json IS NOT NULL ORDER BY created_at"):
            try:
                strategy = json.loads(row["proposal_json"]).get("strategy")
                delta = json.loads(row["summary_json"]).get("mean_excess_sharpe_vs_incumbent")
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(strategy, str) and isinstance(delta, (int, float)):
                out.setdefault(strategy, []).append(float(delta))
        return out

    def distinct_config_counts(self) -> dict[str, int]:
        """strategy -> number of distinct parameter sets recorded, in one scan."""
        seen: dict[str, set[str]] = {}
        for (proposal_json,) in self.db.execute("SELECT proposal_json FROM candidates"):
            try:
                proposal = json.loads(proposal_json)
            except (TypeError, json.JSONDecodeError):
                continue
            strategy = proposal.get("strategy")
            if isinstance(strategy, str):
                seen.setdefault(strategy, set()).add(proposal.get("sparams", "") or "")
        return {name: len(values) for name, values in seen.items()}

    def duplicates_since_last_record(self, limit: int = 1000,
                                     within_hours: float | None = None) -> dict[str, int]:
        """strategy -> duplicate rejections since that family last produced a NEW candidate.

        Counted from the last recorded candidate so the number self-clears: a
        family that manages a fresh configuration starts again from zero.

        `within_hours` additionally ignores older duplicates, which is what keeps
        this from becoming a life sentence. A family excluded on a duplicate can
        never record the new candidate that would clear it, because it is no
        longer scheduled; without an expiry, one repeated proposal would retire a
        family whose parameter space is barely explored.
        """
        cutoff = None
        if within_hours is not None:
            cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=float(within_hours))
        strategy_of: dict[str, str] = {}
        for row in self.db.execute("SELECT candidate_id, proposal_json FROM candidates"):
            try:
                strategy = json.loads(row["proposal_json"]).get("strategy")
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(strategy, str):
                strategy_of[row["candidate_id"]] = strategy

        # Walk backwards by event_id, which is strictly monotonic. Timestamps
        # are NOT usable here: utc_now() truncates to whole seconds, so a
        # duplicate landing in the same second as the family's last recorded
        # candidate compared equal and was dropped - exactly the case a reroute
        # creates, since it records the duplicate and re-schedules at once.
        counts: dict[str, int] = {}
        settled: set[str] = set()
        for row in self.db.execute(
            "SELECT candidate_id, event_type, payload_json, created_at FROM events "
            "WHERE event_type IN ('duplicate_proposal', 'semantic_duplicate', 'proposal_recorded') "
            "ORDER BY event_id DESC LIMIT ?", (limit,),
        ):
            try:
                strategy = json.loads(row["payload_json"]).get("strategy")
            except (TypeError, json.JSONDecodeError):
                strategy = None
            if not isinstance(strategy, str) and row["candidate_id"]:
                strategy = strategy_of.get(row["candidate_id"])
            if not isinstance(strategy, str) or strategy in settled:
                continue
            if row["event_type"] == "proposal_recorded":
                settled.add(strategy)          # older events precede its last new candidate
                continue
            if cutoff is not None:
                try:
                    created = dt.datetime.fromisoformat(row["created_at"])
                except (TypeError, ValueError):
                    created = None
                if created is not None and created < cutoff:
                    continue
            counts[strategy] = counts.get(strategy, 0) + 1
        return counts

    def tested_family_configs(self, strategy: str) -> list[tuple[str, str]]:
        """(candidate_id, sparams) of every recorded candidate of one family."""
        out = []
        for row in self.db.execute("SELECT candidate_id, proposal_json FROM candidates"):
            try:
                proposal = json.loads(row["proposal_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            if proposal.get("strategy") == strategy:
                out.append((row["candidate_id"], proposal.get("sparams", "") or ""))
        return out

    def recent_strategies(self, limit: int = 100) -> list[str]:
        """Strategy names of the most recent recorded candidates, newest first."""
        out = []
        for (proposal_json,) in self.db.execute("SELECT proposal_json FROM candidates ORDER BY created_at DESC LIMIT ?", (limit,)):
            try:
                strategy = json.loads(proposal_json).get("strategy")
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(strategy, str):
                out.append(strategy)
        return out

    def recent_specs(self, limit: int = 30) -> list[dict[str, Any]]:
        """The rule trees of the most recent generated specs, newest first."""
        out = []
        for (proposal_json,) in self.db.execute("SELECT proposal_json FROM candidates ORDER BY created_at DESC LIMIT 2000"):
            try:
                proposal = json.loads(proposal_json)
            except (TypeError, json.JSONDecodeError):
                continue
            if proposal.get("spec"):
                out.append(proposal["spec"])
                if len(out) >= limit:
                    break
        return out

    def family_outcomes(self) -> dict[str, dict[str, Any]]:
        """Per strategy: how many candidates were evaluated and the best mean Sharpe vs the incumbent."""
        out: dict[str, dict[str, Any]] = {}
        for row in self.db.execute("SELECT proposal_json, summary_json FROM candidates"):
            try:
                strategy = json.loads(row["proposal_json"]).get("strategy")
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(strategy, str):
                continue
            entry = out.setdefault(strategy, {"tested": 0, "best_vs_incumbent": None})
            entry["tested"] += 1
            if row["summary_json"]:
                try:
                    delta = json.loads(row["summary_json"]).get("mean_excess_sharpe_vs_incumbent")
                except (TypeError, json.JSONDecodeError):
                    delta = None
                if isinstance(delta, (int, float)) and (entry["best_vs_incumbent"] is None or delta > entry["best_vs_incumbent"]):
                    entry["best_vs_incumbent"] = float(delta)
        return out

    def spec_leaf_usage(self) -> dict[str, int]:
        """How often each leaf type appears across every recorded generated spec."""
        counts: dict[str, int] = {}

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                if isinstance(node.get("type"), str):
                    counts[node["type"]] = counts.get(node["type"], 0) + 1
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        for (proposal_json,) in self.db.execute("SELECT proposal_json FROM candidates"):
            try:
                proposal = json.loads(proposal_json)
            except (TypeError, json.JSONDecodeError):
                continue
            if proposal.get("spec"):
                walk(proposal["spec"])
        return counts

    def proposed_candidates(self) -> list[sqlite3.Row]:
        return list(self.db.execute("SELECT * FROM candidates WHERE status='proposed' ORDER BY created_at"))

    def candidates_with_status(self, status: str) -> list[sqlite3.Row]:
        return list(self.db.execute("SELECT * FROM candidates WHERE status=? ORDER BY created_at", (status,)))

    def current_best(self) -> dict[str, Any] | None:
        try:
            row = self.db.execute(
                "SELECT candidate_id, score, status, summary_json, recorded_at "
                "FROM best_history ORDER BY score DESC, record_id DESC LIMIT 1"
            ).fetchone()
        except sqlite3.OperationalError:
            return None
        return None if row is None else dict(row)

    def record_best(self, candidate_id: str, score: float, status: str,
                    summary: dict[str, Any]) -> bool:
        best = self.current_best()
        if best is not None and float(best["score"]) >= score:
            return False
        self.db.execute(
            "INSERT INTO best_history(candidate_id, score, status, summary_json, recorded_at) VALUES (?, ?, ?, ?, ?)",
            (candidate_id, score, status, canonical_json(summary), utc_now()),
        )
        self.db.commit()
        return True

    def consecutive_event_streak(
        self,
        event_type: str,
        reset_types: tuple[str, ...] = ("proposal_recorded", "doctor_pass"),
    ) -> int:
        rows = self.db.execute(
            "SELECT event_type FROM events ORDER BY event_id DESC LIMIT 10000"
        )
        streak = 0
        for row in rows:
            if row["event_type"] in reset_types:
                break
            if row["event_type"] == event_type:
                streak += 1
        return streak


@contextlib.contextmanager
def single_instance(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise SupervisorError(f"another supervisor holds {path}") from error
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        yield
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def extract_json(content: str) -> dict[str, Any]:
    cleaned = content.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned).strip()
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            raise SupervisorError("model response did not contain a JSON object")
        try:
            value = json.loads(cleaned[start:end + 1])
        except json.JSONDecodeError as error:
            raise SupervisorError(f"model response JSON was invalid: {error}") from error
    if not isinstance(value, dict):
        raise SupervisorError("model proposal must be a JSON object")
    return value


def numeric(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise SupervisorError(f"{field} must be finite numeric")
    return float(value)


# One table drives the validator, the JSON schema handed to Ollama, and the
# prompt, so the model is told exactly what the C++ executor
# (cli_trader/src/strategy/zoo/spec_strategy.h) will accept and in which unit.
#   fields:  required leaf fields beyond "type"
#   extra:   optional secondary windows the leaf accepts
#   range:   legal threshold range (None = no threshold)
#   unit:    what the threshold means, in the model's words
LEAF_SPEC: dict[str, dict[str, Any]] = {
    "close_above_sma": {"fields": ("window", "threshold"), "extra": (), "range": (-100.0, 100.0), "unit": "percent band around the SMA; 0 = the average itself"},
    "close_below_sma": {"fields": ("window", "threshold"), "extra": (), "range": (-100.0, 100.0), "unit": "percent band around the SMA"},
    "close_above_ema": {"fields": ("window", "threshold"), "extra": (), "range": (-100.0, 100.0), "unit": "percent band around the EMA"},
    "close_below_ema": {"fields": ("window", "threshold"), "extra": (), "range": (-100.0, 100.0), "unit": "percent band around the EMA"},
    "breakout_above": {"fields": ("window", "threshold"), "extra": (), "range": (-100.0, 100.0), "unit": "percent above the prior-window high (excludes the current bar)"},
    "breakdown_below": {"fields": ("window", "threshold"), "extra": (), "range": (-100.0, 100.0), "unit": "percent below the prior-window low"},
    "return_above": {"fields": ("window", "threshold"), "extra": (), "range": (-2.0, 2.0), "unit": "fraction: 0.03 means +3% over the window"},
    "return_below": {"fields": ("window", "threshold"), "extra": (), "range": (-2.0, 2.0), "unit": "fraction: -0.05 means -5% over the window"},
    "zscore_return_above": {"fields": ("window", "threshold"), "extra": ("vol_window",), "range": (-5.0, 5.0), "unit": "sigma units: trailing return / (per-bar vol * sqrt(window)); tsmom's own statistic, 0.5 is the live default"},
    "zscore_return_below": {"fields": ("window", "threshold"), "extra": ("vol_window",), "range": (-5.0, 5.0), "unit": "sigma units, negative for a downtrend"},
    "rsi_above": {"fields": ("window", "threshold"), "extra": (), "range": (0.0, 100.0), "unit": "RSI level 0-100"},
    "rsi_below": {"fields": ("window", "threshold"), "extra": (), "range": (0.0, 100.0), "unit": "RSI level 0-100"},
    "relative_volume_above": {"fields": ("window", "threshold"), "extra": (), "range": (0.0, 20.0), "unit": "ratio of this bar's volume to the prior-window mean; 2 = twice normal"},
    "vol_rank_above": {"fields": ("window", "threshold"), "extra": ("rank_window",), "range": (0.0, 1.0), "unit": "percentile 0-1 of realized volatility within its own trailing history (rank_window bars, default 250)"},
    "vol_rank_below": {"fields": ("window", "threshold"), "extra": ("rank_window",), "range": (0.0, 1.0), "unit": "percentile 0-1; 0.3 means calmer than 70% of recent history"},
    "market_zscore_above": {"fields": ("window", "threshold"), "extra": ("vol_window",), "range": (-5.0, 5.0), "unit": "the same z-score read on BTC_USDT, the market factor, one bar late; the ONLY leaf that uses information outside the coin itself"},
    "market_zscore_below": {"fields": ("window", "threshold"), "extra": ("vol_window",), "range": (-5.0, 5.0), "unit": "BTC z-score below the level, one bar late"},
    "atr_trailing_stop": {"fields": ("window", "threshold"), "extra": (), "range": (0.5, 10.0), "unit": "ATR multiple k: true while a position is open and close < highest close since entry - k*ATR(window); EXIT trees only, always false in an entry tree"},
    "weekday": {"fields": ("day",), "extra": (), "range": None, "unit": "day 0-6 in UTC, Sunday=0, Monday=1, ... Saturday=6"},
    "green_candle": {"fields": (), "extra": (), "range": None, "unit": "close above open on this bar"},
    "red_candle": {"fields": (), "extra": (), "range": None, "unit": "close below open on this bar"},
}
SPEC_LEAVES = set(LEAF_SPEC)
SECONDARY_WINDOW_RANGE = {"vol_window": (2, 600), "rank_window": (20, 2000)}
# Leaves that measure a trend or a channel. Below two days of 4h bars they
# describe noise and the fee model kills them: 87% of the first day's generated
# specs died, and the losers were dominated by 6-12 bar windows. The floor is a
# design choice, enforced in the schema (the model cannot emit it) and here.
TREND_LEAVES = {
    "close_above_sma", "close_below_sma", "close_above_ema", "close_below_ema",
    "breakout_above", "breakdown_below", "return_above", "return_below",
    "zscore_return_above", "zscore_return_below", "market_zscore_above", "market_zscore_below",
}
MIN_TREND_WINDOW = 12


def spec_leaf_types(node: Any) -> list[str]:
    out: list[str] = []
    if isinstance(node, dict):
        if isinstance(node.get("type"), str):
            out.append(node["type"])
        for value in node.values():
            out.extend(spec_leaf_types(value))
    elif isinstance(node, list):
        for value in node:
            out.extend(spec_leaf_types(value))
    return out


def spec_signature(spec: dict[str, Any]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(sorted entry leaf types, sorted exit leaf types): the rule's structure, ignoring numbers.

    Two specs with the same signature test the same mechanism with different
    digits; the second is a parameter sweep, not a new hypothesis.
    """
    return (tuple(sorted(spec_leaf_types(spec.get("entry")))), tuple(sorted(spec_leaf_types(spec.get("exit")))))


def is_saturated(deltas: list[float], min_evaluations: int = 30, window: int = 20,
                 min_improvement: float = 0.10) -> bool:
    """A family is saturated when many evaluations have stopped moving its best result.

    deltas are mean Sharpe vs the incumbent in evaluation order. Saturated means
    at least `min_evaluations`, and the best over all of them exceeds the best
    before the last `window` by less than `min_improvement`. tsmom reached this
    at about its 30th evaluation on 2026-09-05 and then ran 120 more.
    """
    if len(deltas) < max(min_evaluations, window + 1):
        return False
    return max(deltas) - max(deltas[:-window]) < min_improvement


def normalize_spec_node(value: Any, depth: int = 0) -> dict[str, Any]:
    if not isinstance(value, dict) or depth > 4:
        raise SupervisorError("generated spec rule depth must be <= 4")
    if "and" in value or "or" in value:
        if len(value) != 1:
            raise SupervisorError("generated spec logical node must contain only and or or")
        key = "all" if "and" in value else "any"
        children = value["and"] if "and" in value else value["or"]
        if not isinstance(children, list):
            raise SupervisorError(f"generated spec {key} must be an array")
        return {key: [normalize_spec_node(child, depth + 1) for child in children]}
    if "all" in value or "any" in value:
        if len(value) != 1:
            raise SupervisorError("generated spec logical node must contain only all or any")
        key = "all" if "all" in value else "any"
        children = value[key]
        if not isinstance(children, list):
            raise SupervisorError(f"generated spec {key} must be an array")
        return {key: [normalize_spec_node(child, depth + 1) for child in children]}
    if "not" in value:
        if len(value) != 1:
            raise SupervisorError("generated spec not node must contain only not")
        return {"not": normalize_spec_node(value["not"], depth + 1)}
    if "type" in value:
        return dict(value)
    leaf_keys = [key for key in value if key in SPEC_LEAVES]
    if len(leaf_keys) != 1 or len(value) != 1:
        raise SupervisorError("generated spec leaf must use type or one indicator-key object")
    leaf = leaf_keys[0]
    payload = value[leaf]
    if not isinstance(payload, dict):
        raise SupervisorError("generated spec indicator-key leaf must contain an object")
    aliases = {"sma_window": "window", "level": "threshold"}
    normalized = {"type": leaf}
    for key, item in payload.items():
        if key in {"window", "threshold", "day", "vol_window", "rank_window"}:
            normalized[key] = item
        elif key in aliases:
            normalized[aliases[key]] = item
        elif key in {"source", "method"}:
            if item not in {"close", "high", "low"}:
                raise SupervisorError(f"unsupported generated spec {key}: {item!r}")
        else:
            raise SupervisorError(f"unknown generated spec leaf field: {key}")
    return normalized


def validate_spec_node(value: Any, depth: int = 0) -> int:
    if not isinstance(value, dict) or depth > 4:
        raise SupervisorError("generated spec rule depth must be <= 4")
    logical = [key for key in ("all", "any", "not") if key in value]
    if logical:
        if len(logical) != 1:
            raise SupervisorError("generated spec logical node must have one of all, any, or not")
        key = logical[0]
        if key == "not":
            return validate_spec_node(value[key], depth + 1)
        children = value[key]
        if not isinstance(children, list) or not 1 <= len(children) <= 8:
            raise SupervisorError(f"generated spec {key} must contain 1..8 rules")
        return sum(validate_spec_node(child, depth + 1) for child in children)
    leaf = value.get("type")
    if leaf not in LEAF_SPEC:
        raise SupervisorError(f"unknown generated spec leaf: {leaf!r}")
    spec = LEAF_SPEC[leaf]
    allowed = {"type", *spec["fields"], *spec["extra"]}
    if set(value) - allowed:
        raise SupervisorError(f"generated spec leaf {leaf} has unknown fields: {sorted(set(value) - allowed)}")
    for field in spec["fields"]:
        if field not in value:
            raise SupervisorError(f"generated spec {leaf} requires {field}")
    if "day" in spec["fields"]:
        day = value.get("day")
        if isinstance(day, bool) or not isinstance(day, int) or not 0 <= day <= 6:
            raise SupervisorError("generated spec weekday day must be an integer from 0 to 6")
    if "window" in spec["fields"]:
        window = value.get("window")
        floor = MIN_TREND_WINDOW if leaf in TREND_LEAVES else 2
        if isinstance(window, bool) or not isinstance(window, int) or not floor <= window <= 600:
            raise SupervisorError(f"generated spec {leaf} window must be an integer from {floor} to 600"
                                  + (" (two days of 4h bars; shorter trend windows are fee-dead)" if floor > 2 else ""))
    if spec["range"] is not None:
        threshold = numeric(value["threshold"], "generated spec threshold")
        lo, hi = spec["range"]
        if not lo <= threshold <= hi:
            raise SupervisorError(f"{leaf} threshold must be in [{lo:g},{hi:g}] ({spec['unit']})")
    for extra in spec["extra"]:
        if extra in value:
            lo, hi = SECONDARY_WINDOW_RANGE[extra]
            item = value[extra]
            if isinstance(item, bool) or not isinstance(item, int) or not lo <= item <= hi:
                raise SupervisorError(f"generated spec {extra} must be an integer from {lo} to {hi}")
    return 1


def validate_generated_spec(value: Any, max_parameters: int) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - {"name", "entry", "exit", "entry_rule", "exit_rule", "max_hold_bars"}:
        raise SupervisorError("generated spec must contain only name, entry, exit, max_hold_bars")
    entry_value = value.get("entry", value.get("entry_rule"))
    exit_value = value.get("exit", value.get("exit_rule"))
    if entry_value is None or exit_value is None:
        raise SupervisorError("generated spec requires entry and exit")
    entry = normalize_spec_node(entry_value)
    exit_rule = normalize_spec_node(exit_value)
    leaves = validate_spec_node(entry) + validate_spec_node(exit_rule)
    if leaves > max_parameters:
        raise SupervisorError("generated spec exceeds the mission rule limit")
    max_hold = value.get("max_hold_bars", 0)
    if isinstance(max_hold, bool) or not isinstance(max_hold, int) or not 0 <= max_hold <= 2000:
        raise SupervisorError("generated spec max_hold_bars must be an integer from 0 to 2000")
    normalized = {"entry": entry, "exit": exit_rule, "max_hold_bars": max_hold}
    return json.loads(canonical_json(normalized))


def validate_feature_request(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SupervisorError("feature_request requires a feature object")
    required = {"name", "description", "transformation", "publication_lag_days"}
    if not required <= set(value) or set(value) - required:
        raise SupervisorError("feature request requires name, description, transformation, publication_lag_days")
    name = value["name"]
    if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]{2,63}", name):
        raise SupervisorError("feature name must be a safe local identifier")
    for field in ("description", "transformation"):
        if not isinstance(value[field], str) or len(value[field].strip()) < 10:
            raise SupervisorError(f"feature {field} is too short")
    lag = value["publication_lag_days"]
    if isinstance(lag, bool) or not isinstance(lag, int) or not 0 <= lag <= 3650:
        raise SupervisorError("feature publication_lag_days must be an integer from 0 to 3650")
    text = (value["description"] + value["transformation"]).lower()
    if any(token in text for token in ("http://", "https://", "api key", "credential")):
        raise SupervisorError("feature requests cannot contain remote URLs or credentials")
    return json.loads(canonical_json({
        "name": name,
        "description": value["description"].strip(),
        "transformation": value["transformation"].strip(),
        "publication_lag_days": lag,
    }))


def validate_proposal(raw: dict[str, Any], mission: dict[str, Any], families: dict[str, dict[str, tuple[float, float]]],
                      family_meta: dict[str, dict[str, dict[str, Any]]] | None = None) -> dict[str, Any]:
    required = {
        "hypothesis", "strategy", "sparams", "vol_target", "weights", "vol_lookback",
        "rebalance", "mechanism", "reasoning", "expected_failure_mode", "novelty_key",
    }
    missing = required - set(raw)
    if missing:
        raise SupervisorError(f"proposal missing fields: {', '.join(sorted(missing))}")
    proposal_type = raw.get("proposal_type", "family")
    if proposal_type not in {"family", "generated_spec", "feature_request"}:
        raise SupervisorError("proposal_type must be family, generated_spec, or feature_request")
    if not isinstance(raw["hypothesis"], str) or len(raw["hypothesis"].strip()) < 20:
        raise SupervisorError("hypothesis must be at least 20 characters")
    for field, minimum in (("mechanism", 20), ("reasoning", 20), ("expected_failure_mode", 10), ("novelty_key", 3)):
        if not isinstance(raw[field], str) or len(raw[field].strip()) < minimum:
            raise SupervisorError(f"{field} is too short")
    # A mechanism that is only leaf or parameter names ("market_zscore_above +
    # vol_rank_above") is a restatement, not a mechanism; the reviewer cannot
    # grade it and the ledger cannot learn from it. Eight words with at least
    # half of them ordinary prose is the cheapest test that rejects that.
    words = [w for w in re.split(r"\s+", raw["mechanism"].strip()) if w]
    prose = [w for w in words if re.fullmatch(r"[A-Za-z][a-z]*[,.;:]?", w)]
    if len(words) < 8 or len(prose) * 2 < len(words):
        raise SupervisorError("mechanism must be at least eight words of prose explaining why prices should behave that way, not a list of leaf or parameter names")
    strategy = raw["strategy"]
    spec = None
    feature = None
    if proposal_type == "generated_spec":
        if strategy != "generated_spec":
            raise SupervisorError("generated_spec proposals must use strategy=generated_spec")
        if raw["sparams"]:
            raise SupervisorError("generated_spec proposals cannot use sparams")
        spec = validate_generated_spec(raw.get("spec"), int(mission["limits"]["max_parameters"]))
        sparams = ""
        parameter_count = 0
    elif proposal_type == "feature_request":
        if strategy != "feature_request":
            raise SupervisorError("feature_request proposals must use strategy=feature_request")
        if raw["sparams"]:
            raise SupervisorError("feature_request proposals cannot use sparams")
        feature = validate_feature_request(raw.get("feature"))
        sparams = ""
        parameter_count = 0
    else:
        if not isinstance(strategy, str) or strategy not in families:
            raise SupervisorError(f"strategy is not in the live CLI registry: {strategy!r}")
        sparams = parse_sparams(raw["sparams"], families[strategy], (family_meta or {}).get(strategy))
        parameter_count = len(sparams.split(",")) if sparams else 0
    if parameter_count > int(mission["limits"]["max_parameters"]):
        raise SupervisorError("proposal exceeds the mission parameter limit")
    if numeric(raw["vol_target"], "vol_target") != float(mission["evaluation"]["vol_target"]):
        raise SupervisorError("vol_target is fixed by the mission")
    if raw["weights"] != mission["evaluation"]["weights"]:
        raise SupervisorError("portfolio weights are fixed by the mission")
    vol_lookback = int(numeric(raw["vol_lookback"], "vol_lookback"))
    rebalance = int(numeric(raw["rebalance"], "rebalance"))
    if vol_lookback < 10 or rebalance < 1:
        raise SupervisorError("vol_lookback/rebalance are outside safe bounds")
    novelty_key = re.sub(r"[^a-z0-9:_-]+", "-", raw["novelty_key"].strip().lower()).strip("-:_")
    novelty_key = novelty_key[:128].rstrip("-:_")
    if len(novelty_key) < 3:
        raise SupervisorError("novelty_key cannot be canonicalized to a safe identifier")
    forbidden_text = (raw["hypothesis"] + raw["mechanism"] + raw["reasoning"] + raw["expected_failure_mode"]).lower()
    if any(token in forbidden_text for token in ("holdout.json", "state_live", "deploy/pi", "2024-", "2025-", "2026-")):
        raise SupervisorError("proposal references a forbidden forward/deployment resource")
    normalized = {
        "hypothesis": raw["hypothesis"].strip(),
        "proposal_type": proposal_type,
        "strategy": strategy,
        "sparams": sparams,
        "vol_target": float(mission["evaluation"]["vol_target"]),
        "weights": mission["evaluation"]["weights"],
        "vol_lookback": vol_lookback,
        "rebalance": rebalance,
        "mechanism": raw["mechanism"].strip(),
        "reasoning": raw["reasoning"].strip(),
        "expected_failure_mode": raw["expected_failure_mode"].strip(),
        "novelty_key": novelty_key,
    }
    if spec is not None:
        normalized["spec"] = spec
    if feature is not None:
        normalized["feature"] = feature
    config = {key: normalized[key] for key in ("proposal_type", "strategy", "sparams", "vol_target", "weights", "vol_lookback", "rebalance")}
    if spec is not None:
        config["spec"] = spec
    if feature is not None:
        config["feature"] = feature
    proposal_hash = sha256_bytes(canonical_json(raw).encode())
    config_hash = sha256_bytes(canonical_json(config).encode())
    candidate_id = "cand-" + config_hash[:16]
    return {
        "candidate_id": candidate_id,
        "proposal_hash": proposal_hash,
        "config_hash": config_hash,
        "mission_id": mission["mission_id"],
        "proposal": normalized,
    }


def parse_metrics(output: str) -> dict[str, Any]:
    def find(pattern: str, field: str) -> float | None:
        match = re.search(pattern, output, re.MULTILINE)
        if not match:
            raise SupervisorError(f"portfolio output missing {field}")
        return None if match.group(1) == "nan" else float(match.group(1))
    metrics = {
        "total_return_pct": find(r"^Total return:\s+([-+0-9.]+)%", "total return"),
        "cagr_pct": find(r"^CAGR:\s+([-+0-9.]+)%", "CAGR"),
        "sharpe": find(r"^Sharpe \(ann\.\):\s+([-+0-9.]+)", "Sharpe"),
        "excess_sharpe_vs_basket": find(r"^Sharpe \(ann\.\):\s+[-+0-9.]+\s+[-+0-9.]+\s+([-+0-9.]+)", "excess Sharpe"),
        "sortino": find(r"^Sortino \(ann\.\):\s+([-+0-9.]+)", "Sortino"),
        "max_drawdown_pct": find(r"^Max drawdown:\s+([-+0-9.]+)%", "max drawdown"),
        # A spec that never trades prints `nan` here (zero-variance sleeve returns).
        # Read it as NaN so the zero-trade screen can label the candidate, instead
        # of failing the parse and mislabelling it as an execution failure.
        "sleeve_correlation": find(r"^Avg pairwise sleeve correlation:\s+([-+0-9.]+|nan)", "sleeve correlation"),
    }
    trade_values = re.findall(r"^\s+[A-Z0-9_]+\s+[-+0-9.]+\s+[-+0-9.]+\s+[-+0-9.]+\s+[-+0-9.]+\s+(\d+)\s+[-+0-9.]+\s*$", output, re.MULTILINE)
    metrics["trade_count"] = sum(int(value) for value in trade_values)
    return metrics


class Supervisor:
    def __init__(self, mission_path: Path, model: str, endpoint: str, timeout: float,
                 db_path: Path = DEFAULT_DB, artifacts: Path = DEFAULT_ARTIFACTS,
                 heartbeat_path: Path = DEFAULT_HEARTBEAT,
                 calibration_path: Path = DEFAULT_CALIBRATION,
                 ollama_min_interval: float = DEFAULT_OLLAMA_MIN_INTERVAL,
                 reviewer_model: str = DEFAULT_REVIEWER_MODEL,
                 num_ctx: int = DEFAULT_NUM_CTX,
                 generator_think: str | None = DEFAULT_GENERATOR_THINK,
                 trace_path: Path | None = DEFAULT_TRACE,
                 trace_console: bool = False) -> None:
        self.mission_path = mission_path.resolve()
        self.mission = validate_mission(self.mission_path)
        self.mission_hash = sha256_file(self.mission_path)
        self.repo = Path(self.mission["_source_repo"])
        self.binary = self.repo / "build" / "cli_trader"
        if not self.binary.is_file() or not os.access(self.binary, os.X_OK):
            raise SupervisorError(f"cli_trader binary is not executable: {self.binary}")
        self.binary_hash = sha256_file(self.binary)
        self.source_revision = git_revision(self.repo)
        self.model = model
        self.reviewer_model = reviewer_model
        self.trace = TraceLogger(trace_path, trace_console)
        self.client = OllamaClient(endpoint, model, timeout, ollama_min_interval,
                       num_ctx=num_ctx, think=generator_think,
                       trace=self.trace, role="generator")
        self.reviewer_client = OllamaClient(endpoint, reviewer_model, timeout, ollama_min_interval,
                            num_ctx=num_ctx, think=None,
                            trace=self.trace, role="reviewer")
        self.db = Registry(db_path)
        self.artifacts = artifacts
        self.heartbeat_path = heartbeat_path
        self.calibration_path = calibration_path
        self.best_path = db_path.parent / "best-v2.json"
        self.artifacts.mkdir(parents=True, exist_ok=True)
        self.db.mission(self.mission, self.mission_path, model)
        self.recover_inflight_candidates()
        self.families = self.discover_families()
        self.catalogue = self.discover_catalogue()
        self.family_meta = self.discover_family_meta()
        self.reconcile_proposals()
        self.seed_best_history()

    def close(self) -> None:
        self.db.close()

    def recover_inflight_candidates(self) -> None:
        for row in self.db.candidates_with_status("running"):
            summary = {
                "status": "interrupted",
                "classification": "recovered_after_process_exit",
                "error": "candidate was running when the supervisor restarted",
            }
            self.db.update_candidate(row["candidate_id"], "interrupted", summary["classification"], summary)
            self.db.event("candidate_recovered_interrupted", summary, row["candidate_id"])

    def seed_best_history(self) -> None:
        if self.db.current_best() is not None:
            return
        best_row = None
        best_score = None
        for status in ("survives_development", "frontier"):
            for row in self.db.candidates_with_status(status):
                if not row["summary_json"]:
                    continue
                summary = json.loads(row["summary_json"])
                score = summary.get("mean_excess_sharpe_vs_incumbent")
                if score is None:
                    continue
                if best_score is None or float(score) > best_score:
                    best_row, best_score = row, float(score)
        if best_row is None or best_score is None:
            return
        summary = json.loads(best_row["summary_json"])
        if self.db.record_best(best_row["candidate_id"], best_score, best_row["status"], summary):
            write_json(self.best_path, {
                "mission_id": self.mission["mission_id"],
                "mission_hash": self.mission_hash,
                "binary_hash": self.binary_hash,
                "source_revision": self.source_revision,
                "candidate_id": best_row["candidate_id"],
                "score": best_score,
                "status": best_row["status"],
                "proposal": json.loads(best_row["proposal_json"]),
                "summary": summary,
                "updated_at": utc_now(),
                "backfilled": True,
            })

    def write_heartbeat(self, status: str, iteration: int = 0,
                        candidate_id: str | None = None, error: str | None = None,
                        focus: str | None = None) -> None:
        write_json(self.heartbeat_path, {
            "pid": os.getpid(),
            "status": status,
            "iteration": iteration,
            "candidate_id": candidate_id,
            "error": error,
            "focus": focus,
            "mission_id": self.mission["mission_id"],
            "model": self.model,
            "reviewer_model": self.reviewer_model,
            "updated_at": utc_now(),
        })
        self.trace.emit(
            "heartbeat",
            status=status,
            iteration=iteration,
            candidate_id=candidate_id,
            error=error,
            focus=focus,
        )

    def discover_family_meta(self) -> dict[str, dict[str, dict[str, Any]]]:
        completed = subprocess.run(
            [str(self.binary), "list-strategies"], cwd=self.repo, text=True,
            capture_output=True, timeout=30, check=False, env=safe_child_environment(),
        )
        return parse_family_meta(completed.stdout) if completed.returncode == 0 else {}

    def discover_catalogue(self) -> dict[str, str]:
        completed = subprocess.run(
            [str(self.binary), "list-strategies"], cwd=self.repo, text=True,
            capture_output=True, timeout=30, check=False, env=safe_child_environment(),
        )
        return parse_family_catalogue(completed.stdout) if completed.returncode == 0 else {}

    def discover_families(self) -> dict[str, dict[str, tuple[float, float]]]:
        completed = subprocess.run(
            [str(self.binary), "list-strategies"], cwd=self.repo, text=True,
            capture_output=True, timeout=30, check=False, env=safe_child_environment(),
        )
        if completed.returncode != 0:
            raise SupervisorError(completed.stderr.strip() or "list-strategies failed")
        return parse_family_specs(completed.stdout)

    def reconcile_proposals(self) -> None:
        for row in self.db.proposed_candidates():
            try:
                raw = json.loads(row["proposal_json"])
                raw.setdefault("proposal_type", "family")
                validate_proposal(raw, self.mission, self.families, self.family_meta)
            except SupervisorError as error:
                self.db.update_candidate(row["candidate_id"], "invalid", "registry_revalidation_failed", {"error": str(error)})
                self.db.event("proposal_revalidated_invalid", {"error": str(error)}, row["candidate_id"])

    def doctor(self) -> dict[str, Any]:
        tags = self.client.tags()
        for role, model in (("generator", self.model), ("reviewer", self.reviewer_model)):
            if model not in tags:
                raise SupervisorError(f"selected local {role} model is not installed: {model}; available: {tags}")
            if ":cloud" in model or any(name == model and ":cloud" in name for name in tags):
                raise SupervisorError(f"cloud Ollama {role} models are forbidden")
        data_dir = Path(self.mission["_allowed_data_dirs"][0])
        missing = []
        for symbol in self.mission["universe"]["protocol_symbols"]:
            path = data_dir / f"{symbol}_14400.ctc"
            if not path.is_file():
                missing.append(str(path))
        if missing:
            raise SupervisorError("missing protocol data: " + ", ".join(missing))
        result = {
            "ok": True,
            "mission_id": self.mission["mission_id"],
            "model": self.model,
            "reviewer_model": self.reviewer_model,
            "ollama_endpoint": self.client.endpoint,
            "strategy_count": len(self.families),
            "strategies": sorted(self.families),
            "development_end": self.mission["data_policy"]["development_end"],
            "holdout_allowed": False,
            "mission_hash": self.mission_hash,
            "binary_hash": self.binary_hash,
            "source_revision": self.source_revision,
        }
        self.db.event("doctor_pass", result)
        return result

    @staticmethod
    def mode_of(name: str) -> str:
        if name == "generated_spec":
            return "spec"
        if name.startswith("control_"):
            return "control"
        if name == "calendar_rule":
            return "calendar"
        return "family"

    def search_policy(self) -> dict[str, Any]:
        policy = dict(self.mission.get("search_policy", {}))
        policy.setdefault("quotas", {"spec": 0.5, "family": 0.4, "calendar": 0.05, "control": 0.05})
        policy.setdefault("quota_window", 100)
        policy.setdefault("exclude", [])
        policy.setdefault("saturation", {"min_evaluations": 30, "window": 20, "min_improvement": 0.10, "drift_every": 20})
        policy.setdefault("near_duplicate_distance", 0.10)
        # A family is routed out of the rotation after this many duplicate
        # rejections since its last new candidate. 1 means the first duplicate
        # is enough, which is the only safe setting for a family whose
        # configuration grid is already covered.
        policy.setdefault("exhaust_after_duplicates", 1)
        # ...and for how long that holds. Exhaustion by a covered grid is
        # permanent and provable; exhaustion by duplicates is a cooldown, or a
        # family the model happened to repeat once would be retired for good
        # while most of its parameter space was still unexplored.
        policy.setdefault("exhaust_duplicate_hours", 6.0)
        policy.setdefault("spec_novelty", {"leaf_cap_share": 0.40, "leaf_cap_window": 30})
        return policy

    def saturation(self) -> dict[str, bool]:
        """Which PARAMETER families have stopped improving.

        `generated_spec` is deliberately absent: saturation is a statement about
        a parameter plateau, and a rule language has no parameter grid to
        exhaust. Its repetition is governed by the structural-signature and
        leaf-cap gates instead. It is excluded explicitly so that adding it to
        the registry one day cannot silently remove the scheduler's fallback.
        """
        sat = self.search_policy()["saturation"]
        deltas = self.db.family_deltas()
        return {
            name: is_saturated(deltas.get(name, []), int(sat["min_evaluations"]), int(sat["window"]), float(sat["min_improvement"]))
            for name in self.families if name != "generated_spec"
        }

    def exhausted(self) -> dict[str, str]:
        """Families that cannot produce a new configuration right now, and why.

        Two ways to get here, one permanent and one self-clearing:
          * the family's configuration grid is finite and every point in it has
            been recorded (`enumerable_space_size`), which is permanent;
          * it has produced `exhaust_after_duplicates` duplicate rejections
            since it last recorded a new candidate and within the last
            `exhaust_duplicate_hours`, which clears when either the cooldown
            expires or it manages a fresh configuration.
        Either way the scheduler must not pick it, because a duplicate records
        nothing and therefore cannot reduce the quota deficit that selected it.
        """
        policy = self.search_policy()
        threshold = int(policy["exhaust_after_duplicates"])
        cooldown = float(policy["exhaust_duplicate_hours"])
        distinct = self.db.distinct_config_counts()
        duplicates = self.db.duplicates_since_last_record(within_hours=cooldown)
        out: dict[str, str] = {}
        for name in self.families:
            if name == "generated_spec":
                continue
            size = enumerable_space_size(self.family_meta.get(name) or {})
            tested = distinct.get(name, 0)
            if size is not None and tested >= size:
                out[name] = (f"all {size} configuration{'' if size == 1 else 's'} of this family have been tested"
                             + (" (it takes no parameters)" if size == 1 and not (self.family_meta.get(name) or {}) else ""))
                continue
            recent_duplicates = duplicates.get(name, 0)
            if threshold > 0 and recent_duplicates >= threshold:
                out[name] = (f"{recent_duplicates} duplicate proposal{'' if recent_duplicates == 1 else 's'} "
                             f"since it last produced a new candidate (clears {cooldown:g}h after the last one)")
        return out

    def state_context(self) -> dict[str, Any]:
        strategy_counts = self.db.strategy_counts()
        focus_failures = self.db.strategy_focus_failures()
        duplicate_failures = self.db.strategy_duplicate_failures()
        policy = self.search_policy()
        schedule_features = bool(policy.get("schedule_feature_requests", False))
        exclude = set(policy["exclude"]) | ({"feature_request"} if not schedule_features else set())
        preferred = list(policy.get("preferred_sequence", []))
        # THE WHOLE REGISTRY is in rotation, not a hand-typed list: the v2
        # mission named ten modes and 25 of 35 families were never scheduled in
        # 705 evaluations while tsmom was scheduled 151 times.
        exhausted = self.exhausted()
        # An exhausted family is removed from the pool outright rather than
        # merely penalised. A penalty is worthless when the family is the only
        # eligible member of its mode: `control_always_long` was picked 40 times
        # in a row on 2026-09-06 while carrying the maximum duplicate penalty,
        # because nothing else in the control mode was eligible.
        pool = [name for name in list(self.families) + ["generated_spec"]
                if name not in exclude and name not in exhausted]
        saturated = self.saturation()
        FAILURE_WEIGHT = 10.0

        def score(name: str) -> tuple[float, int]:
            return (
                strategy_counts.get(name, 0)
                + FAILURE_WEIGHT * focus_failures.get(name, 0)
                + FAILURE_WEIGHT * duplicate_failures.get(name, 0),
                preferred.index(name) if name in preferred else len(preferred),
            )

        # QUOTAS by mode, measured over the recent window. Least-tested alone
        # is uniform allocation: it gave coin-flip controls 15% of the first
        # night's budget after the null had already been calibrated.
        recent = self.db.recent_strategies(int(policy["quota_window"]))
        realized: dict[str, int] = {}
        for name in recent:
            realized[self.mode_of(name)] = realized.get(self.mode_of(name), 0) + 1
        total_recent = max(1, len(recent))
        quotas = policy["quotas"]
        deficit = {mode: float(share) - realized.get(mode, 0) / total_recent for mode, share in quotas.items()}

        # SATURATION: a family whose best has stopped moving leaves the rotation
        # except for one drift check every `drift_every` recorded candidates.
        total_recorded = sum(strategy_counts.values())
        drift_every = int(policy["saturation"]["drift_every"])
        saturated_pool = [name for name in pool if saturated.get(name)]
        target = None
        reason = ""
        if saturated_pool and drift_every > 0 and total_recorded % drift_every == 0:
            target = min(saturated_pool, key=score)
            reason = "drift check of a saturated family"
        else:
            for mode in sorted(deficit, key=deficit.get, reverse=True):
                members = [name for name in pool if self.mode_of(name) == mode and not saturated.get(name)]
                if members:
                    target = min(members, key=score)
                    reason = f"mode {mode} is {deficit[mode]:+.0%} below its quota"
                    break
        if target is None:
            # generated_spec is never saturated and never exhausted, so this is
            # a real fallback and not a hope.
            target = "generated_spec"
            reason = "every parameter family is saturated or exhausted"
        calibration = self.load_calibration()
        return {
            "counts": self.db.counts(),
            "strategy_counts": strategy_counts,
            "strategy_focus_failures": focus_failures,
            "strategy_duplicate_failures": duplicate_failures,
            "null_calibration": {
                "ready": calibration is not None,
                "mean_q99": None if calibration is None else calibration["quantiles"]["mean_excess_sharpe_vs_basket_q99"],
                "worst_q99": None if calibration is None else calibration["quantiles"]["worst_excess_sharpe_vs_basket_q99"],
            },
            "next_search_focus": {
                "strategy": target,
                "reason": reason,
                "saturated": sorted(name for name, flag in saturated.items() if flag),
                "exhausted": exhausted,
                "quota_deficit": deficit,
                "instruction": f"Try {target} next; do not repeat a better-tested family unless the mechanism is materially different.",
            },
            "recent_candidates": self.db.latest_candidates(12),
            "mission_id": self.mission["mission_id"],
            "development_end": self.mission["data_policy"]["development_end"],
        }

    def model_context(self) -> dict[str, Any]:
        state = self.state_context()
        compact_candidates = []
        for candidate in state["recent_candidates"]:
            summary = {}
            if candidate.get("summary_json"):
                try:
                    raw_summary = json.loads(candidate["summary_json"])
                    for key in (
                        "mean_excess_sharpe_vs_basket", "mean_excess_sharpe_vs_incumbent",
                        "worst_excess_sharpe_vs_incumbent", "worst_drawdown_pct",
                    ):
                        if key in raw_summary:
                            summary[key] = raw_summary[key]
                except (TypeError, json.JSONDecodeError):
                    summary = {}
            compact_candidates.append({
                "candidate_id": candidate["candidate_id"],
                "status": candidate["status"],
                "classification": candidate["classification"],
                "summary": summary,
            })
        state["recent_candidates"] = compact_candidates
        state["recent_events"] = self.db.latest_events(12)
        state["research_priors"] = RESEARCH_PRIORS
        return state

    def enforce_circuit_breakers(self) -> None:
        limits = self.mission["limits"]
        invalid = self.db.consecutive_event_streak("invalid_proposal")
        duplicate = self.db.consecutive_event_streak("duplicate_proposal")
        if invalid >= int(limits["max_invalid_proposals_in_a_row"]):
            self.db.event("circuit_breaker_open", {
                "kind": "invalid_proposals",
                "streak": invalid,
                "limit": limits["max_invalid_proposals_in_a_row"],
            })
            raise CircuitBreakerOpen(f"paused after {invalid} consecutive invalid proposals")
        if duplicate >= int(limits["max_duplicate_proposals_in_a_row"]):
            self.db.event("circuit_breaker_open", {
                "kind": "duplicate_proposals",
                "streak": duplicate,
                "limit": limits["max_duplicate_proposals_in_a_row"],
            })
            raise CircuitBreakerOpen(f"paused after {duplicate} consecutive duplicate proposals")

    def load_calibration(self) -> dict[str, Any] | None:
        if not self.calibration_path.is_file():
            return None
        try:
            calibration = load_json(self.calibration_path)
        except SupervisorError:
            return None
        if calibration.get("mission_id") != self.mission["mission_id"]:
            return None
        if calibration.get("seed_count") != int(self.mission["null_calibration"]["seeds"]):
            return None
        return calibration

    def portfolio_command(self, strategy: str, sparams: str, start: str, end: str,
                          vol_lookback: int, rebalance: int,
                          candidate: dict[str, Any] | None = None) -> list[str]:
        development_end = parse_date(self.mission["data_policy"]["development_end"])
        if parse_date(start) >= parse_date(end) or parse_date(end) > development_end:
            raise SupervisorError(f"refusing non-development fold: {start}..{end}")
        envs = ",".join(f"{symbol}:14400" for symbol in self.mission["universe"]["protocol_symbols"])
        command = [
            str(self.binary), "portfolio", "--data-dir", self.mission["_allowed_data_dirs"][0],
            "--envs", envs, "--start", start, "--end", end,
            "--warmup-bars", str(self.mission["evaluation"]["warmup_bars"]),
            "--strategy", strategy, "--vol-target", str(self.mission["evaluation"]["vol_target"]),
            "--vol-window", str(self.mission["evaluation"]["vol_window"]), "--weights", "equal",
            "--vol-lookback", str(vol_lookback), "--rebalance", str(rebalance),
        ]
        if candidate is not None and candidate["proposal"]["proposal_type"] == "generated_spec":
            spec_path = self.artifacts / candidate["candidate_id"] / "spec.json"
            if not spec_path.is_file() or load_json(spec_path) != candidate["proposal"].get("spec"):
                raise SupervisorError("generated spec artifact does not match the stored proposal")
            command.extend(["--spec-file", str(spec_path)])
        elif sparams:
            command.extend(["--sparams", sparams])
        return command

    def run_portfolio(self, command: list[str]) -> dict[str, Any]:
        started = time.monotonic()
        self.trace.emit("evaluator.start", mode="portfolio", argv=command)
        completed = subprocess.run(
            command, cwd=self.repo, text=True, capture_output=True,
            timeout=int(self.mission["limits"]["max_candidate_runtime_seconds"]),
            check=False, env=safe_child_environment(),
        )
        self.trace.emit(
            "evaluator.end",
            mode="portfolio",
            argv=command,
            returncode=completed.returncode,
            duration_seconds=time.monotonic() - started,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
        if completed.returncode != 0:
            raise SupervisorError(completed.stderr.strip() or completed.stdout.strip() or "portfolio failed")
        metrics = parse_metrics(completed.stdout)
        metrics["command"] = command
        metrics["mission_hash"] = self.mission_hash
        metrics["binary_hash"] = self.binary_hash
        metrics["source_revision"] = self.source_revision
        return metrics

    def incumbent_folds(self) -> list[dict[str, Any]]:
        incumbent = self.mission["incumbent"]
        portfolio = self.mission["evaluation"]
        output = []
        for start, end in self.mission["evaluation"]["folds"]:
            command = self.portfolio_command(
                incumbent["strategy"], incumbent["sparams"], start, end,
                int(portfolio.get("vol_lookback", 120)), int(portfolio.get("rebalance", 30)),
            )
            output.append(self.run_portfolio(command))
        return output

    def calibrate_null(self, force: bool = False) -> dict[str, Any]:
        if self.load_calibration() is not None and not force:
            return load_json(self.calibration_path)
        null = self.mission["null_calibration"]
        seed_count = int(null["seeds"])
        rows = []
        portfolio = self.mission["evaluation"]
        for seed in range(1, seed_count + 1):
            folds = []
            for start, end in self.mission["evaluation"]["folds"]:
                sparams = f"entryProb=0.02,holdBars=20,seed={seed}"
                command = self.portfolio_command(
                    "control_random", sparams, start, end,
                    int(portfolio.get("vol_lookback", 120)), int(portfolio.get("rebalance", 30)),
                )
                folds.append(self.run_portfolio(command))
            excess = [float(fold["excess_sharpe_vs_basket"]) for fold in folds]
            rows.append({
                "seed": seed,
                "mean_excess_sharpe_vs_basket": sum(excess) / len(excess),
                "worst_excess_sharpe_vs_basket": min(excess),
                "folds": folds,
            })
            if seed % 25 == 0:
                print(f"null calibration: {seed}/{seed_count}", file=sys.stderr, flush=True)
        calibration = {
            "mission_id": self.mission["mission_id"],
            "strategy": "control_random",
            "seed_count": seed_count,
            "created_at": utc_now(),
            "quantiles": {
                "mean_excess_sharpe_vs_basket_q99": percentile(
                    [row["mean_excess_sharpe_vs_basket"] for row in rows], 0.99
                ),
                "worst_excess_sharpe_vs_basket_q99": percentile(
                    [row["worst_excess_sharpe_vs_basket"] for row in rows], 0.99
                ),
            },
            "seeds": rows,
        }
        write_json(self.calibration_path, calibration)
        self.db.event("null_calibration_complete", {
            "seed_count": seed_count,
            "quantiles": calibration["quantiles"],
        })
        return calibration

    def novelty_rejection(self, raw: dict[str, Any]) -> str | None:
        """Why a VALID proposal should still not run: it is a near-duplicate or a structural repeat.

        Returns a reason (recorded as a duplicate_proposal event so the
        scheduler's penalties and the circuit breaker see it) or None.
        """
        policy = self.search_policy()
        if raw.get("proposal_type", "family") == "family":
            strategy = raw.get("strategy")
            meta = self.family_meta.get(strategy) or {}
            if not meta:
                return None
            here = normalized_parameters(raw.get("sparams", "") or "", meta)
            threshold = float(policy["near_duplicate_distance"])
            for candidate_id, tested in self.db.tested_family_configs(strategy):
                distance = parameter_distance(here, normalized_parameters(tested, meta))
                if distance < threshold:
                    return (f"near-duplicate of {candidate_id} (every parameter within {distance:.0%} of its range; "
                            f"the limit is {threshold:.0%}): change the mechanism or the horizon, not a digit")
            return None
        if raw.get("proposal_type") == "generated_spec" and isinstance(raw.get("spec"), dict):
            spec = raw["spec"]
            signature = spec_signature(spec)
            recent = self.db.recent_specs(2000)
            for tested in recent:
                if spec_signature(tested) == signature:
                    return ("a spec with exactly this leaf structure was already tested (entry: "
                            + ", ".join(signature[0]) + "; exit: " + ", ".join(signature[1])
                            + "); change which leaves are combined, not their numbers")
            novelty = policy["spec_novelty"]
            window = recent[: int(novelty["leaf_cap_window"])]
            if len(window) >= 10:
                cap = float(novelty["leaf_cap_share"])
                for leaf in set(spec_leaf_types(spec.get("entry"))):
                    share = sum(1 for tested in window if leaf in spec_leaf_types(tested.get("entry"))) / len(window)
                    if share > cap:
                        return (f"entry leaf {leaf} appears in {share:.0%} of the last {len(window)} specs "
                                f"(cap {cap:.0%}); build the entry around a different mechanism")
        return None

    def record_semantic_duplicate(self, raw_content: str, strategy: str, reason: str) -> None:
        proposal_hash = sha256_bytes(raw_content.encode())[:16]
        artifact = self.artifacts / f"duplicate-{proposal_hash}.json"
        write_json(artifact, {
            "raw_model_content": raw_content,
            "strategy": strategy,
            "reason": reason,
            "created_at": utc_now(),
        })
        self.db.event("semantic_duplicate", {
            "artifact": str(artifact),
            "strategy": strategy,
            "reason": reason,
        })
        self.trace.emit(
            "proposal.semantic_duplicate",
            strategy=strategy,
            reason=reason,
            raw_model_content=raw_content,
        )

    def incumbent_summary(self) -> list[dict[str, Any]] | None:
        """The incumbent's fold results, computed once per process (three 0.05 s backtests)."""
        if not hasattr(self, "_incumbent_cache"):
            try:
                self._incumbent_cache = self.incumbent_folds()
            except SupervisorError as error:
                self.db.event("incumbent_summary_failed", {"error": str(error)})
                self._incumbent_cache = None
        return self._incumbent_cache

    def generate_proposal(self) -> tuple[dict[str, Any], str, dict[str, Any]]:
        focus = self.state_context()["next_search_focus"]["strategy"]
        feedback = ""
        for attempt in range(1, 4):
            system, user = generator_prompts(
                focus, attempt, feedback, self.mission, self.families,
                self.incumbent_summary(),
                self.db.recent_spec_configs(8) if focus == "generated_spec" else [],
                self.db.recent_family_sparams(focus, 12) if focus not in ("generated_spec", "feature_request") else [],
                self.db.spec_leaf_usage() if focus == "generated_spec" else {},
                self.db.counts(), self.load_calibration(),
                self.catalogue, self.db.family_outcomes(),
                self.db.family_deltas().get(focus, []) if focus not in ("generated_spec", "feature_request") else None,
                frozenset(name for name, flag in self.saturation().items() if flag),
                self.search_policy()["spec_novelty"],
            )
            schema_mode = "generated_spec" if focus == "generated_spec" else "feature_request" if focus == "feature_request" else None
            try:
                content, response = self.client.chat(
                    system,
                    user,
                    proposal_schema(
                        schema_mode,
                        None if schema_mode else focus,
                        None if schema_mode else self.families.get(focus),
                    ),
                    trace_context={
                        "focus": focus,
                        "attempt": attempt,
                        "mode": schema_mode or "family",
                    },
                )
            except SupervisorError as error:
                self.db.event("proposal_request_error", {
                    "strategy": focus,
                    "attempt": attempt,
                    "error": str(error),
                })
                feedback = (
                    "The previous Ollama request failed before producing a proposal: "
                    + str(error)
                    + ". Retry with one complete JSON object only."
                )
                if attempt == 3:
                    raise
                continue
            try:
                raw = extract_json(content)
            except SupervisorError as error:
                self.record_invalid_proposal(content, str(error))
                feedback = "Your previous response was invalid: " + str(error) + ". Return one JSON object only."
                continue
            proposal_type = raw.get("proposal_type", "family")

            # Risk construction is fixed by the mission, not chosen by the
            # model. Preserve the hypothesis while canonicalizing these
            # required fields before validation.
            raw["vol_target"] = self.mission["evaluation"]["vol_target"]
            raw["weights"] = self.mission["evaluation"]["weights"]
            raw["vol_lookback"] = self.mission["evaluation"].get("vol_lookback", 120)
            raw["rebalance"] = self.mission["evaluation"].get("rebalance", 30)

            # The model's most valuable contribution is a new mechanism. It
            # may answer a named-family prompt with a valid creative spec or
            # feature request; accept that safely instead of burning the
            # iteration on an artificial family mismatch.
            if proposal_type == "feature_request" or raw.get("strategy") == "feature_request":
                raw["proposal_type"] = "feature_request"
                raw["strategy"] = "feature_request"
                raw["sparams"] = ""
                raw.pop("spec", None)
                if focus != "feature_request":
                    self.record_invalid_proposal(content, "feature_request is only accepted during the scheduled feature phase")
                    feedback = "Do not return a feature_request now. Return an executable generated_spec rule tree with entry and exit, or the exact scheduled family proposal."
                    continue
                try:
                    validate_proposal(raw, self.mission, self.families)
                except SupervisorError as error:
                    self.record_invalid_proposal(content, str(error))
                    feedback = "Your previous feature request was invalid: " + str(error) + ". Include a complete feature object with name, description, transformation, and publication_lag_days."
                    continue
                else:
                    self.db.event("scheduled_feature_substitution", {
                        "scheduled_strategy": focus,
                        "accepted_strategy": "feature_request",
                    })
                    return raw, content, response

            if proposal_type == "generated_spec" or raw.get("strategy") == "generated_spec" or "spec" in raw:
                if focus != "generated_spec":
                    self.record_invalid_proposal(content, f"generated_spec is only accepted during the scheduled generated_spec phase, not {focus}")
                    feedback = "Return proposal_type=family with the exact scheduled strategy and its legal sparams. Do not emit a generated spec in this phase."
                    continue
                raw["proposal_type"] = "generated_spec"
                raw["strategy"] = "generated_spec"
                raw["sparams"] = ""
                try:
                    validate_proposal(raw, self.mission, self.families, self.family_meta)
                    raw["spec"] = validate_generated_spec(raw.get("spec"), int(self.mission["limits"]["max_parameters"]))
                except SupervisorError as error:
                    self.record_invalid_proposal(content, str(error))
                    feedback = "Your previous generated_spec was invalid: " + str(error) + ". For generated_spec, sparams must be empty and spec must contain entry and exit rule trees."
                    continue
                rejection = self.novelty_rejection(raw)
                if rejection:
                    self.record_semantic_duplicate(content, "generated_spec", rejection)
                    feedback = "Your previous spec was rejected as a repeat: " + rejection
                    continue
                self.db.event("scheduled_creative_substitution", {
                    "scheduled_strategy": focus,
                    "accepted_strategy": "generated_spec",
                })
                return raw, content, response

            if proposal_type != ("generated_spec" if focus == "generated_spec" else
                                 "feature_request" if focus == "feature_request" else "family"):
                error = f"model used proposal_type={proposal_type} outside scheduled mode {focus}"
                self.record_invalid_proposal(content, error)
                feedback = "Your previous response was invalid: " + error + ". Return the scheduled proposal type only."
                continue
            if raw.get("proposal_type") == "generated_spec" and focus == "generated_spec":
                raw["strategy"] = "generated_spec"
            if raw.get("strategy") == focus:
                try:
                    validate_proposal(raw, self.mission, self.families, self.family_meta)
                except SupervisorError as error:
                    self.record_invalid_proposal(content, str(error))
                    feedback = (
                        "Your previous family proposal was invalid: " + str(error)
                        + f". Return strategy={focus!r} and use only the legal sparams shown for that family."
                    )
                    continue
                rejection = self.novelty_rejection(raw)
                if rejection:
                    self.record_semantic_duplicate(content, focus, rejection)
                    feedback = "Your previous proposal was rejected as a repeat: " + rejection
                    continue
                return raw, content, response
            error = f"model ignored scheduled strategy {focus}"
            self.record_invalid_proposal(content, error)
            feedback = "Your previous response was invalid: " + error + ". Return the exact scheduled strategy only."
        self.db.event("scheduled_strategy_failed", {"strategy": focus, "attempts": 3})
        raise ScheduledFocusSkipped(f"model failed to follow scheduled strategy {focus} after 3 attempts")

    def record_proposal(self, raw: dict[str, Any], raw_content: str, response: dict[str, Any]) -> dict[str, Any]:
        candidate = validate_proposal(raw, self.mission, self.families, self.family_meta)
        if self.db.has_config(candidate["config_hash"]):
            self.db.event(
                "duplicate_proposal",
                {
                    "config_hash": candidate["config_hash"],
                    "strategy": candidate["proposal"]["strategy"],
                    "sparams": candidate["proposal"]["sparams"],
                },
                candidate["candidate_id"],
            )
            raise DuplicateProposal(
                f"duplicate candidate configuration: {candidate['candidate_id']}",
                candidate["candidate_id"],
                candidate["proposal"]["strategy"],
            )
        initial_status = "blocked_missing_feature" if candidate["proposal"]["proposal_type"] == "feature_request" else "proposed"
        if candidate["proposal"]["proposal_type"] == "generated_spec":
            write_json(self.artifacts / candidate["candidate_id"] / "spec.json", candidate["proposal"]["spec"])
        if candidate["proposal"]["proposal_type"] == "feature_request":
            write_json(self.artifacts / candidate["candidate_id"] / "feature_request.json", candidate["proposal"]["feature"])
        self.db.insert_candidate(candidate, initial_status)
        artifact = {
            "candidate": candidate,
            "provenance": {
                "mission_hash": self.mission_hash,
                "binary_hash": self.binary_hash,
                "source_revision": self.source_revision,
            },
            "raw_model_content": raw_content,
            "ollama_response_metadata": {key: value for key, value in response.items() if key != "message"},
            "created_at": utc_now(),
        }
        write_json(self.artifacts / candidate["candidate_id"] / "proposal.json", artifact)
        self.db.event("proposal_recorded", {"proposal_hash": candidate["proposal_hash"]}, candidate["candidate_id"])
        self.trace.emit(
            "proposal.accepted",
            candidate_id=candidate["candidate_id"],
            proposal=candidate["proposal"],
        )
        return candidate

    def record_invalid_proposal(self, raw_content: str, error: str) -> None:
        proposal_hash = sha256_bytes(raw_content.encode())[:16]
        artifact = self.artifacts / f"invalid-{proposal_hash}.json"
        write_json(artifact, {"raw_model_content": raw_content, "error": error, "created_at": utc_now()})
        self.db.event("invalid_proposal", {"artifact": str(artifact), "error": error})
        self.trace.emit("proposal.rejected", raw_model_content=raw_content, error=error)

    def command_for_fold(self, candidate: dict[str, Any], start: str, end: str) -> list[str]:
        proposal = candidate["proposal"]
        if proposal["proposal_type"] == "feature_request":
            raise SupervisorError("feature request is blocked until a local feature manifest exists")
        return self.portfolio_command(
            proposal["strategy"], proposal["sparams"], start, end,
            proposal["vol_lookback"], proposal["rebalance"], candidate,
        )

    def evaluate_candidate(self, candidate_id: str) -> dict[str, Any]:
        try:
            return self._evaluate_candidate(candidate_id)
        except KeyboardInterrupt:
            self.db.update_candidate(candidate_id, "interrupted", "operator_interrupt", {"message": "evaluation interrupted"})
            self.db.event("candidate_interrupted", {"message": "evaluation interrupted"}, candidate_id)
            raise
        except Exception as error:
            summary = {
                "status": "killed",
                "classification": "evaluation_exception",
                "error": f"{type(error).__name__}: {error}",
                "promotion": "blocked_until_successful_evaluation",
            }
            self.db.update_candidate(candidate_id, summary["status"], summary["classification"], summary)
            self.db.event("candidate_evaluation_exception", summary, candidate_id)
            return summary

    def _evaluate_candidate(self, candidate_id: str) -> dict[str, Any]:
        row = self.db.candidate(candidate_id)
        if row is None:
            raise SupervisorError(f"unknown candidate: {candidate_id}")
        proposal = json.loads(row["proposal_json"])
        proposal.setdefault("proposal_type", "family")
        candidate = {
            "candidate_id": row["candidate_id"],
            "proposal_hash": row["proposal_hash"],
            "config_hash": row["config_hash"],
            "mission_id": row["mission_id"],
            "proposal": proposal,
        }
        self.db.update_candidate(candidate_id, "running")
        results = []
        for index, fold in enumerate(self.mission["evaluation"]["folds"]):
            start, end = fold
            command = self.command_for_fold(candidate, start, end)
            run_id = self.db.start_run(candidate_id, index, start, end, command)
            folder = self.artifacts / candidate_id / f"fold-{index + 1}"
            folder.mkdir(parents=True, exist_ok=True)
            stdout_path, stderr_path = folder / "stdout.txt", folder / "stderr.txt"
            started = time.monotonic()
            self.trace.emit(
                "evaluator.start",
                mode="candidate_fold",
                candidate_id=candidate_id,
                fold=index + 1,
                argv=command,
            )
            try:
                completed = subprocess.run(
                    command, cwd=self.repo, text=True, capture_output=True,
                    timeout=int(self.mission["limits"]["max_candidate_runtime_seconds"]),
                    check=False, env=safe_child_environment(),
                )
                stdout_path.write_text(completed.stdout)
                stderr_path.write_text(completed.stderr)
                self.trace.emit(
                    "evaluator.end",
                    mode="candidate_fold",
                    candidate_id=candidate_id,
                    fold=index + 1,
                    argv=command,
                    returncode=completed.returncode,
                    duration_seconds=time.monotonic() - started,
                    stdout=completed.stdout,
                    stderr=completed.stderr,
                )
                if completed.returncode != 0:
                    metrics = {"error": completed.stderr.strip() or completed.stdout.strip(), "duration_seconds": time.monotonic() - started}
                    self.db.finish_run(run_id, "failed", completed.returncode, stdout_path, stderr_path, metrics)
                    results.append(metrics)
                    continue
                metrics = parse_metrics(completed.stdout)
                metrics["duration_seconds"] = time.monotonic() - started
                self.db.finish_run(run_id, "completed", completed.returncode, stdout_path, stderr_path, metrics)
                results.append(metrics)
            except subprocess.TimeoutExpired as error:
                stdout_path.write_text(error.stdout or "")
                stderr_path.write_text(error.stderr or "")
                self.trace.emit(
                    "evaluator.end",
                    mode="candidate_fold",
                    candidate_id=candidate_id,
                    fold=index + 1,
                    argv=command,
                    status="timeout",
                    duration_seconds=time.monotonic() - started,
                    stdout=error.stdout or "",
                    stderr=error.stderr or "",
                )
                metrics = {"error": "candidate timeout", "duration_seconds": time.monotonic() - started}
                self.db.finish_run(run_id, "timeout", None, stdout_path, stderr_path, metrics)
                results.append(metrics)
            except KeyboardInterrupt:
                self.trace.emit(
                    "evaluator.end",
                    mode="candidate_fold",
                    candidate_id=candidate_id,
                    fold=index + 1,
                    argv=command,
                    status="interrupted",
                    duration_seconds=time.monotonic() - started,
                )
                metrics = {"error": "operator interrupt", "duration_seconds": time.monotonic() - started}
                self.db.finish_run(run_id, "interrupted", None, stdout_path, stderr_path, metrics)
                raise
            except Exception as error:
                self.trace.emit(
                    "evaluator.end",
                    mode="candidate_fold",
                    candidate_id=candidate_id,
                    fold=index + 1,
                    argv=command,
                    status="exception",
                    error=f"{type(error).__name__}: {error}",
                    duration_seconds=time.monotonic() - started,
                )
                metrics = {
                    "error": f"{type(error).__name__}: {error}",
                    "duration_seconds": time.monotonic() - started,
                }
                self.db.finish_run(run_id, "failed", None, stdout_path, stderr_path, metrics)
                results.append(metrics)
                break
        incumbent_folds = []
        calibration = self.load_calibration()
        if not any("error" in result for result in results):
            try:
                incumbent_folds = self.incumbent_folds()
            except Exception as error:
                summary = {
                    "status": "killed",
                    "classification": "incumbent_comparison_failed",
                    "folds": results,
                    "error": str(error),
                    "promotion": "blocked_until_incumbent_comparison",
                }
            else:
                summary = classify_results(
                    results, incumbent_folds, calibration,
                    int(self.mission["evaluation"]["min_trades_per_fold"]),
                    int(self.mission["evaluation"]["max_trades_per_fold"]),
                )
        else:
            summary = classify_results(
                results, incumbent_folds, calibration,
                int(self.mission["evaluation"]["min_trades_per_fold"]),
                int(self.mission["evaluation"]["max_trades_per_fold"]),
            )
        self.db.update_candidate(candidate_id, summary["status"], summary["classification"], summary)
        self.db.event("candidate_classified", summary, candidate_id)
        score = summary.get("mean_excess_sharpe_vs_incumbent")
        if score is not None and summary.get("status") in {"survives_development", "frontier"}:
            if self.db.record_best(candidate_id, float(score), summary["status"], summary):
                write_json(self.best_path, {
                    "mission_id": self.mission["mission_id"],
                    "mission_hash": self.mission_hash,
                    "binary_hash": self.binary_hash,
                    "source_revision": self.source_revision,
                    "candidate_id": candidate_id,
                    "score": float(score),
                    "status": summary["status"],
                    "proposal": candidate["proposal"],
                    "summary": summary,
                    "updated_at": utc_now(),
                })
        write_json(self.artifacts / candidate_id / "result.json", {"candidate": candidate, "folds": results, "summary": summary})
        return summary

    def reclassify_survivors(self) -> dict[str, Any]:
        calibration = self.load_calibration()
        if calibration is None:
            raise SupervisorError("run calibrate-null before reclassifying survivors")
        incumbent = self.incumbent_folds()
        changed = []
        for row in self.db.candidates_with_status("survives_development"):
            if not row["summary_json"]:
                continue
            prior = json.loads(row["summary_json"])
            folds = prior.get("fold_metrics") or prior.get("folds")
            if not isinstance(folds, list) or len(folds) != len(incumbent):
                continue
            summary = classify_results(
                folds, incumbent, calibration,
                int(self.mission["evaluation"]["min_trades_per_fold"]),
                int(self.mission["evaluation"]["max_trades_per_fold"]),
            )
            self.db.update_candidate(row["candidate_id"], summary["status"], summary["classification"], summary)
            self.db.event("candidate_reclassified", {
                "status": summary["status"],
                "classification": summary["classification"],
            }, row["candidate_id"])
            changed.append({
                "candidate_id": row["candidate_id"],
                "status": summary["status"],
                "classification": summary["classification"],
                "mean_excess_sharpe_vs_incumbent": summary.get("mean_excess_sharpe_vs_incumbent"),
                "beats_null": summary.get("beats_null"),
            })
        return {"reclassified": len(changed), "candidates": changed}

    def run_once(self, evaluate: bool) -> dict[str, Any]:
        raw, content, response = self.generate_proposal()
        try:
            candidate = self.record_proposal(raw, content, response)
        except DuplicateProposal:
            raise
        except SupervisorError as error:
            self.record_invalid_proposal(content, str(error))
            raise
        if not evaluate:
            return {"candidate_id": candidate["candidate_id"], "status": "proposed"}
        if candidate["proposal"]["proposal_type"] == "feature_request":
            return {
                "candidate_id": candidate["candidate_id"],
                "status": "blocked_missing_feature",
                "classification": "awaiting_local_feature_manifest",
            }
        summary = self.evaluate_candidate(candidate["candidate_id"])
        return {"candidate_id": candidate["candidate_id"], **summary}

    def run_iteration(self, iteration: int, max_reroutes: int = 3) -> dict[str, Any]:
        """One iteration, rerouting past a family that cannot produce a new configuration.

        A duplicate used to end the iteration: the daemon slept its interval,
        the scheduler re-picked the same family (nothing had changed, because a
        duplicate records no candidate and the quota deficit is measured in
        recorded candidates), and twenty of those opened the circuit breaker.

        Now the duplicate is recorded, which marks the family exhausted, the
        scheduler picks a different one, and the iteration carries on. The cost
        of a duplicate is one model call instead of one iteration. If the
        scheduler cannot move - the same focus comes back - the duplicate is
        raised to the caller rather than looping.
        """
        for attempt in range(max_reroutes + 1):
            focus = self.state_context()["next_search_focus"]["strategy"]
            self.write_heartbeat("proposing", iteration, focus=focus)
            self.trace.emit(
                "iteration.start" if attempt == 0 else "iteration.reroute",
                iteration=iteration, focus=focus, attempt=attempt,
            )
            try:
                return self.run_once(evaluate=True)
            except DuplicateProposal as error:
                rerouted = self.state_context()["next_search_focus"]
                exhausted = self.exhausted().get(focus, "no new configuration available")
                self.db.event("focus_rerouted", {
                    "iteration": iteration,
                    "attempt": attempt + 1,
                    "from_focus": focus,
                    "to_focus": rerouted["strategy"],
                    "why_exhausted": exhausted,
                    "reason": str(error),
                }, error.candidate_id)
                self.trace.emit(
                    "iteration.duplicate", iteration=iteration, attempt=attempt + 1,
                    from_focus=focus, to_focus=rerouted["strategy"], why_exhausted=exhausted,
                )
                if attempt >= max_reroutes or rerouted["strategy"] == focus:
                    raise
        raise SupervisorError("run_iteration exhausted its reroutes without a result")

    def review_candidate(self, candidate_id: str) -> dict[str, Any]:
        row = self.db.candidate(candidate_id)
        if row is None:
            raise SupervisorError(f"unknown candidate: {candidate_id}")
        if row["summary_json"] is None:
            raise SupervisorError("candidate has no completed evaluation")
        summary = json.loads(row["summary_json"])
        system = (
            "You are the reviewer agent for a local quantitative research supervisor. "
            "Return exactly one JSON object with assessment, mechanism_status, robustness_concerns, "
            "selection_bias_concerns, next_experiment_type, recommended_action, and summary. "
            "Use only the deterministic evidence supplied. Do not invent metrics, request holdout "
            "data, emit commands, or override the protocol classification."
        )
        user = json.dumps({
            "mission_id": self.mission["mission_id"],
            "candidate": json.loads(row["proposal_json"]),
            "classification": row["classification"],
            "summary": summary,
            "instruction": "Review this development-only result. Keep the recommendation advisory.",
        }, indent=2)
        content, response = self.reviewer_client.chat(
            system,
            user,
            review_schema(),
            trace_context={"candidate_id": candidate_id},
        )
        try:
            review = extract_json(content)
        except SupervisorError as error:
            self.db.event("invalid_review", {"candidate_id": candidate_id, "error": str(error)}, candidate_id)
            raise
        required = {"assessment", "mechanism_status", "robustness_concerns", "selection_bias_concerns", "next_experiment_type", "recommended_action", "summary"}
        missing = required - set(review)
        if missing:
            raise SupervisorError(f"review missing fields: {', '.join(sorted(missing))}")
        self.db.add_review(candidate_id, review)
        artifact = {
            "candidate_id": candidate_id,
            "review": review,
            "raw_model_content": content,
            "ollama_response_metadata": {key: value for key, value in response.items() if key != "message"},
            "created_at": utc_now(),
        }
        write_json(self.artifacts / candidate_id / "review.json", artifact)
        self.db.event("review_recorded", {"assessment": review.get("assessment")}, candidate_id)
        return review

    def daemon(self, interval: float, max_iterations: int) -> None:
        stop = False
        def request_stop(_signum: int, _frame: Any) -> None:
            nonlocal stop
            stop = True
        signal.signal(signal.SIGINT, request_stop)
        signal.signal(signal.SIGTERM, request_stop)
        iteration = 0
        last_candidate_id: str | None = None
        self.write_heartbeat("running")
        try:
            while not stop and (max_iterations <= 0 or iteration < max_iterations):
                iteration += 1
                try:
                    self.enforce_circuit_breakers()
                except CircuitBreakerOpen as error:
                    self.db.event("daemon_paused", {"reason": str(error), "iteration": iteration})
                    self.write_heartbeat("paused", iteration, last_candidate_id, str(error))
                    print(json.dumps({"iteration": iteration, "status": "paused", "reason": str(error)}), flush=True)
                    break
                try:
                    result = self.run_iteration(iteration)
                    if result.get("status") == "survives_development":
                        try:
                            review = self.review_candidate(result["candidate_id"])
                            result["review_assessment"] = review.get("assessment")
                            result["review_action"] = review.get("recommended_action")
                        except SupervisorError as error:
                            result["review_error"] = str(error)
                    last_candidate_id = result.get("candidate_id")
                    self.write_heartbeat("idle", iteration, last_candidate_id)
                    self.trace.emit("iteration.end", iteration=iteration, result=result)
                    print(json.dumps({"iteration": iteration, **result}), flush=True)
                except ScheduledFocusSkipped as error:
                    self.db.event("iteration_skipped", {"reason": str(error), "iteration": iteration})
                    self.write_heartbeat("idle", iteration, last_candidate_id)
                    print(json.dumps({"iteration": iteration, "status": "skipped", "reason": str(error)}), flush=True)
                except DuplicateProposal as error:
                    # Every reroute this iteration also came back a duplicate.
                    self.db.event(
                        "iteration_skipped",
                        {
                            "reason": str(error),
                            "iteration": iteration,
                            "focus": error.strategy,
                            "candidate_id": error.candidate_id,
                        },
                        error.candidate_id,
                    )
                    self.write_heartbeat("idle", iteration, last_candidate_id)
                    print(json.dumps({
                        "iteration": iteration,
                        "status": "duplicate_skipped",
                        "candidate_id": error.candidate_id,
                        "strategy": error.strategy,
                        "reason": str(error),
                    }), flush=True)
                except CircuitBreakerOpen as error:
                    self.db.event("daemon_paused", {"reason": str(error), "iteration": iteration})
                    self.write_heartbeat("paused", iteration, last_candidate_id, str(error))
                    print(json.dumps({"iteration": iteration, "status": "paused", "reason": str(error)}), flush=True)
                    break
                except SupervisorError as error:
                    self.db.event("iteration_error", {"error": str(error), "iteration": iteration})
                    self.write_heartbeat("error", iteration, error=str(error))
                    print(json.dumps({"iteration": iteration, "error": str(error)}), file=sys.stderr, flush=True)
                except Exception as error:
                    message = f"{type(error).__name__}: {error}"
                    self.db.event("iteration_exception", {"error": message, "iteration": iteration})
                    self.write_heartbeat("error", iteration, error=message)
                    print(json.dumps({"iteration": iteration, "error": message}), file=sys.stderr, flush=True)
                if not stop and interval > 0:
                    time.sleep(interval)
        finally:
            self.write_heartbeat("stopped", iteration, last_candidate_id)


def classify_results(results: list[dict[str, Any]], incumbent: list[dict[str, Any]] | None = None,
                     calibration: dict[str, Any] | None = None,
                     min_trades_per_fold: int = 1,
                     max_trades_per_fold: int = 1000) -> dict[str, Any]:
    failures = [result for result in results if "error" in result]
    if failures:
        return {"status": "killed", "classification": "execution_failed", "failed_folds": len(failures), "folds": results}
    excess = [float(result["excess_sharpe_vs_basket"]) for result in results]
    trade_counts = [int(result.get("trade_count", 0)) for result in results]
    if any(count < min_trades_per_fold for count in trade_counts):
        return {
            "status": "killed",
            "classification": "zero_trade_fold",
            "folds": results,
            "trade_counts": trade_counts,
            "promotion": "blocked_until_non_degenerate",
        }
    if any(count > max_trades_per_fold for count in trade_counts):
        return {
            "status": "killed",
            "classification": "turnover_screen_failed",
            "folds": results,
            "trade_counts": trade_counts,
            "promotion": "blocked_until_non_degenerate",
        }
    screen_pass = sum(value > 0 for value in excess) >= 2
    summary = {
        "status": "survives_development" if screen_pass else "killed",
        "classification": "development_screen_only" if screen_pass else "basket_screen_failed",
        "folds": len(results),
        "mean_excess_sharpe_vs_basket": sum(excess) / len(excess),
        "worst_excess_sharpe_vs_basket": min(excess),
        "positive_folds": sum(value > 0 for value in excess),
        "worst_drawdown_pct": max(float(result["max_drawdown_pct"]) for result in results),
        "fold_metrics": results,
        "promotion": "blocked_until_incumbent_and_null_calibration",
    }
    if incumbent and len(incumbent) == len(results):
        deltas = []
        incumbent_drawdowns = []
        for candidate_fold, incumbent_fold in zip(results, incumbent):
            delta = float(candidate_fold["sharpe"]) - float(incumbent_fold["sharpe"])
            candidate_fold["incumbent_sharpe"] = float(incumbent_fold["sharpe"])
            candidate_fold["excess_sharpe_vs_incumbent"] = delta
            deltas.append(delta)
            incumbent_drawdowns.append(float(incumbent_fold["max_drawdown_pct"]))
        summary["incumbent_fold_metrics"] = incumbent
        summary["mean_excess_sharpe_vs_incumbent"] = sum(deltas) / len(deltas)
        summary["worst_excess_sharpe_vs_incumbent"] = min(deltas)
        summary["incumbent_worst_drawdown_pct"] = max(incumbent_drawdowns)
        # A two-year fold carries a Sharpe standard error of about 0.71, so
        # demanding a strictly positive delta on every fold turned a -0.02 on
        # one fold into a veto over candidates that won the other two by more
        # than a full unit and cut drawdown by two thirds. The worst fold may
        # lose by up to a third of one standard error; the mean must still win
        # and the drawdown must not be worse.
        WORST_FOLD_TOLERANCE = 0.25
        summary["worst_fold_tolerance"] = WORST_FOLD_TOLERANCE
        summary["beats_incumbent"] = (
            screen_pass and summary["mean_excess_sharpe_vs_incumbent"] > 0.0 and
            summary["worst_excess_sharpe_vs_incumbent"] > -WORST_FOLD_TOLERANCE and
            summary["worst_drawdown_pct"] <= summary["incumbent_worst_drawdown_pct"]
        )
        # Risk reducer: not a Sharpe improvement, but the same or nearly the
        # same return per unit of risk at half the drawdown. The reference rule's
        # own justification is drawdown, not alpha, so this deserves a label
        # rather than "gate failed".
        summary["risk_reducer"] = (
            screen_pass and not summary["beats_incumbent"] and
            summary["mean_excess_sharpe_vs_incumbent"] >= -0.10 and
            summary["worst_drawdown_pct"] <= 0.5 * summary["incumbent_worst_drawdown_pct"]
        )
    else:
        summary["beats_incumbent"] = False
        summary["risk_reducer"] = False

    if calibration is None:
        summary["null_calibration_ready"] = False
        summary["beats_null"] = False
    else:
        quantiles = calibration["quantiles"]
        summary["null_calibration_ready"] = True
        summary["null_q99_mean_excess_sharpe"] = quantiles["mean_excess_sharpe_vs_basket_q99"]
        summary["null_q99_worst_excess_sharpe"] = quantiles["worst_excess_sharpe_vs_basket_q99"]
        summary["beats_null"] = (
            summary["mean_excess_sharpe_vs_basket"] > quantiles["mean_excess_sharpe_vs_basket_q99"] and
            summary["worst_excess_sharpe_vs_basket"] > quantiles["worst_excess_sharpe_vs_basket_q99"]
        )

    if screen_pass and summary.get("beats_incumbent") and summary.get("beats_null"):
        summary["status"] = "frontier"
        summary["classification"] = "frontier_candidate"
        summary["promotion"] = "research_frontier_only"
    elif screen_pass and summary.get("risk_reducer"):
        summary["classification"] = "risk_reducer"
        summary["promotion"] = "research_note_only"
    elif screen_pass:
        summary["classification"] = "incumbent_or_null_gate_failed"
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mission", type=Path, default=DEFAULT_MISSION)
    parser.add_argument("--model", default=os.environ.get("OLLAMA_MODEL", DEFAULT_MODEL))
    parser.add_argument("--reviewer-model", default=os.environ.get("OLLAMA_REVIEWER_MODEL", DEFAULT_REVIEWER_MODEL))
    parser.add_argument("--num-ctx", type=int, default=DEFAULT_NUM_CTX,
                        help="Ollama context window for every call (default %(default)s)")
    parser.add_argument("--generator-think", choices=["low", "medium", "high", "off"], default=DEFAULT_GENERATOR_THINK,
                        help="reasoning effort for the generator model; 'off' sends no think field (default %(default)s)")
    parser.add_argument("--endpoint", default=os.environ.get("OLLAMA_HOST", DEFAULT_ENDPOINT))
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--llm-min-interval", type=float, default=DEFAULT_OLLAMA_MIN_INTERVAL)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--artifacts", type=Path, default=DEFAULT_ARTIFACTS)
    parser.add_argument("--heartbeat", type=Path, default=DEFAULT_HEARTBEAT)
    parser.add_argument("--calibration", type=Path, default=DEFAULT_CALIBRATION)
    parser.add_argument("--trace-file", type=Path, default=DEFAULT_TRACE,
                        help="append structured dialogue/evaluator events to this JSONL file")
    parser.add_argument("--trace-console", action="store_true",
                        help="mirror trace events and streamed model text to stderr")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("doctor")
    subparsers.add_parser("propose")
    subparsers.add_parser("run-once")
    evaluate = subparsers.add_parser("evaluate")
    evaluate.add_argument("candidate_id")
    review = subparsers.add_parser("review")
    review.add_argument("candidate_id")
    subparsers.add_parser("reclassify")
    daemon = subparsers.add_parser("daemon")
    daemon.add_argument("--interval", type=float, default=30.0)
    daemon.add_argument("--max-iterations", type=int, default=0)
    calibration = subparsers.add_parser("calibrate-null")
    calibration.add_argument("--force", action="store_true")
    status = subparsers.add_parser("status")
    status.add_argument("--watch", action="store_true")
    status.add_argument("--interval", type=float, default=5.0)
    subparsers.add_parser("best")
    return parser


def status_snapshot(db_path: Path, heartbeat_path: Path, calibration_path: Path) -> dict[str, Any]:
    try:
        registry = Registry(db_path, read_only=True)
    except (SupervisorError, sqlite3.Error) as error:
        return {
            "heartbeat": load_json(heartbeat_path) if heartbeat_path.is_file() else {},
            "status": "unavailable",
            "error": str(error),
            "registry": str(db_path),
        }
    try:
        heartbeat = {}
        if heartbeat_path.is_file():
            try:
                heartbeat = load_json(heartbeat_path)
            except SupervisorError as error:
                heartbeat = {"status": "invalid", "error": str(error)}
        calibration = None
        if calibration_path.is_file():
            try:
                calibration = load_json(calibration_path)
            except SupervisorError:
                calibration = {"status": "invalid"}
        return {
            "heartbeat": heartbeat,
            "counts": registry.counts(),
            "best": registry.current_best(),
            "null_calibration": {
                "ready": calibration is not None and calibration.get("status") != "invalid",
                "seed_count": None if calibration is None else calibration.get("seed_count"),
                "quantiles": None if calibration is None else calibration.get("quantiles"),
            },
            "recent_candidates": registry.latest_candidates(),
            "recent_events": registry.latest_events(),
        }
    finally:
        registry.close()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    registry_path = args.db.resolve()
    lock_path = registry_path.with_suffix(".lock")
    if args.command == "status":
        while True:
            snapshot = status_snapshot(registry_path, args.heartbeat.resolve(), args.calibration.resolve())
            if args.watch:
                heartbeat = snapshot["heartbeat"]
                counts = snapshot["counts"]
                print(
                    f"{heartbeat.get('updated_at', utc_now())} "
                    f"status={heartbeat.get('status', 'unknown')} "
                    f"iteration={heartbeat.get('iteration', 0)} "
                    f"focus={heartbeat.get('focus') or '-'} "
                    f"candidate={heartbeat.get('candidate_id') or '-'} "
                    f"survivors={counts.get('survives_development', 0)} "
                    f"frontier={counts.get('frontier', 0)} "
                    f"killed={counts.get('killed', 0)} "
                    f"invalid={counts.get('invalid', 0)} "
                    f"blocked_features={counts.get('blocked_missing_feature', 0)} "
                    f"calibration={'ready' if snapshot['null_calibration']['ready'] else 'missing'} "
                    f"current_error={heartbeat.get('error') or '-'}",
                    flush=True,
                )
            else:
                print(json.dumps(snapshot, indent=2), flush=True)
            if not args.watch:
                break
            try:
                time.sleep(max(0.2, args.interval))
            except KeyboardInterrupt:
                break
        return 0
    if args.command == "best":
        registry = Registry(registry_path, read_only=True)
        try:
            print(json.dumps({"best": registry.current_best()}, indent=2))
        finally:
            registry.close()
        return 0
    with single_instance(lock_path):
        supervisor = Supervisor(
            args.mission.resolve(), args.model, args.endpoint, args.timeout,
            registry_path, args.artifacts.resolve(), args.heartbeat.resolve(),
            args.calibration.resolve(), args.llm_min_interval, args.reviewer_model,
            args.num_ctx, None if args.generator_think == "off" else args.generator_think,
            args.trace_file.resolve() if args.trace_file else None,
            args.trace_console,
        )
        try:
            if args.command in {"propose", "run-once", "review", "daemon", "calibrate-null"}:
                supervisor.doctor()
            if args.command == "doctor":
                print(json.dumps(supervisor.doctor(), indent=2))
            elif args.command == "propose":
                print(json.dumps(supervisor.run_once(evaluate=False), indent=2))
            elif args.command == "run-once":
                print(json.dumps(supervisor.run_once(evaluate=True), indent=2))
            elif args.command == "evaluate":
                print(json.dumps(supervisor.evaluate_candidate(args.candidate_id), indent=2))
            elif args.command == "review":
                print(json.dumps(supervisor.review_candidate(args.candidate_id), indent=2))
            elif args.command == "reclassify":
                print(json.dumps(supervisor.reclassify_survivors(), indent=2))
            elif args.command == "calibrate-null":
                print(json.dumps(supervisor.calibrate_null(args.force), indent=2))
            elif args.command == "daemon":
                supervisor.doctor()
                supervisor.daemon(args.interval, args.max_iterations)
            return 0
        finally:
            supervisor.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SupervisorError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2)