"""Configuration: packaged defaults deep-merged with an optional local TOML file.

Unknown keys and out-of-range values raise ConfigError so a typo in a hard
limit can never silently fall back to a default.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from .scheduler import SchedulerSettings

TIERS = ("mid", "strong", "strongest")
COMMITTEE = ("aggressive", "conservative", "neutral")
AGENTS = ("research", "assessment", "risk", *COMMITTEE)
CONSENSUS_RULES = ("unanimous", "majority")
EXECUTION_MODES = ("paper",)  # live trading is out of scope for v0.1
PROVIDERS = ("fake", "ollama", "openai_compat")
DATA_SOURCES = ("synthetic", "csv")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ModelSpec:
    provider: str
    model: str
    base_url: str = ""
    api_key_env: str = ""
    usd_per_m_input: float = 0.0
    usd_per_m_output: float = 0.0
    num_ctx: int = 16384
    think: bool = False
    temperature: float = 0.0
    merge_system_prompt: bool = False
    timeout_s: float = 600.0
    max_output_tokens: int = 4096
    json_schema: bool = True


@dataclass(frozen=True)
class AgentSpec:
    tier: str
    daily_budget_usd: float


@dataclass(frozen=True)
class Limits:
    starting_cash_usd: float
    max_position_pct: float
    max_order_usd: float
    min_cash_pct: float
    daily_order_cap: int


@dataclass(frozen=True)
class Committee:
    max_rounds: int
    invest_rule: str
    procedural_rule: str
    size_tolerance_pct: float
    max_send_backs: int
    memory_items: int


@dataclass(frozen=True)
class Portfolio:
    benchmark: str
    commission_bps: float
    slippage_bps: float
    long_term_after_days: int


@dataclass(frozen=True)
class Config:
    scheduler: SchedulerSettings
    db_path: Path
    models: dict[str, ModelSpec]
    agents: dict[str, AgentSpec]
    symbols: tuple[str, ...]
    data_source: str
    csv_dir: Path
    macro_dir: Path
    fundamentals_dir: Path
    macro_series: tuple[str, ...]
    sec_user_agent: str
    sec_cache_dir: Path
    news_lookback_days: int
    mcp_url: str
    batch_size: int
    queue_cap: int
    cache_ttl_hours: int
    min_avg_daily_volume_usd: float
    min_observations: int
    stale_after_days: int
    committee: Committee
    portfolio: Portfolio
    max_tool_turns: int
    limits: Limits
    execution_mode: str

    def model_for(self, agent: str) -> ModelSpec:
        return self.models[self.agents[agent].tier]

    @property
    def sec_identity(self) -> str:
        """data.sec_user_agent, resolving "env:NAME"; empty if unset."""
        import os

        value = self.sec_user_agent
        if value.startswith("env:"):
            return os.environ.get(value[4:], "")
        return value


def _defaults() -> dict[str, Any]:
    text = resources.files("qfii").joinpath("default_config.toml").read_text()
    return tomllib.loads(text)


def _merge(base: dict[str, Any], override: dict[str, Any], path: str = "") -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        where = f"{path}.{key}" if path else key
        if key not in base:
            raise ConfigError(f"unknown config key: {where}")
        if isinstance(base[key], dict):
            if not isinstance(value, dict):
                raise ConfigError(f"{where} must be a table")
            out[key] = _merge(base[key], value, where)
        else:
            out[key] = value
    return out


def _number(raw: dict[str, Any], key: str, where: str, *, lo: float, hi: float | None = None,
            integer: bool = False) -> float:
    value = raw[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{where}.{key} must be a number")
    if integer and not isinstance(value, int):
        raise ConfigError(f"{where}.{key} must be an integer")
    if value < lo or (hi is not None and value > hi):
        bound = f"[{lo}, {hi}]" if hi is not None else f">= {lo}"
        raise ConfigError(f"{where}.{key} must be {bound}, got {value}")
    return value


def _committee(raw: dict[str, Any]) -> Committee:
    for key in ("invest_rule", "procedural_rule"):
        if raw[key] not in CONSENSUS_RULES:
            raise ConfigError(f"committee.{key} must be one of {CONSENSUS_RULES}")
    return Committee(
        max_rounds=int(_number(raw, "max_rounds", "committee", lo=1, hi=10, integer=True)),
        invest_rule=raw["invest_rule"],
        procedural_rule=raw["procedural_rule"],
        size_tolerance_pct=float(_number(raw, "size_tolerance_pct", "committee", lo=0, hi=100)),
        max_send_backs=int(_number(raw, "max_send_backs", "committee", lo=0, hi=5, integer=True)),
        memory_items=int(_number(raw, "memory_items", "committee", lo=0, hi=200, integer=True)),
    )


def _portfolio(raw: dict[str, Any]) -> Portfolio:
    if not raw["benchmark"]:
        raise ConfigError("portfolio.benchmark is required")
    return Portfolio(
        benchmark=raw["benchmark"].upper(),
        commission_bps=float(_number(raw, "commission_bps", "portfolio", lo=0, hi=1000)),
        slippage_bps=float(_number(raw, "slippage_bps", "portfolio", lo=0, hi=1000)),
        long_term_after_days=int(
            _number(raw, "long_term_after_days", "portfolio", lo=0, integer=True)
        ),
    )


def _build(raw: dict[str, Any]) -> Config:
    models = {}
    for tier in TIERS:
        spec = raw["models"][tier]
        if spec["provider"] not in PROVIDERS:
            raise ConfigError(f"models.{tier}.provider must be one of {PROVIDERS}")
        if not spec["model"]:
            raise ConfigError(f"models.{tier}.model is required")
        if spec["provider"] == "openai_compat" and not spec["base_url"]:
            raise ConfigError(f"models.{tier}.base_url is required for openai_compat")
        where = f"models.{tier}"
        for flag in ("think", "merge_system_prompt", "json_schema"):
            if not isinstance(spec[flag], bool):
                raise ConfigError(f"{where}.{flag} must be true or false")
        models[tier] = ModelSpec(
            spec["provider"], spec["model"], spec["base_url"], spec["api_key_env"],
            float(_number(spec, "usd_per_m_input", where, lo=0)),
            float(_number(spec, "usd_per_m_output", where, lo=0)),
            int(_number(spec, "num_ctx", where, lo=512, hi=1_048_576, integer=True)),
            spec["think"],
            float(_number(spec, "temperature", where, lo=0, hi=2)),
            spec["merge_system_prompt"],
            float(_number(spec, "timeout_s", where, lo=1)),
            int(_number(spec, "max_output_tokens", where, lo=16, integer=True)),
            spec["json_schema"],
        )

    agents = {}
    for name in AGENTS:
        spec = raw["agents"][name]
        if spec["tier"] not in TIERS:
            raise ConfigError(f"agents.{name}.tier must be one of {TIERS}, got {spec['tier']!r}")
        budget = _number(spec, "daily_budget_usd", f"agents.{name}", lo=0)
        agents[name] = AgentSpec(spec["tier"], float(budget))

    symbols = raw["universe"]["symbols"]
    if not symbols or not all(isinstance(s, str) and s for s in symbols):
        raise ConfigError("universe.symbols must be a non-empty list of tickers")
    if len(set(symbols)) != len(symbols):
        raise ConfigError("universe.symbols contains duplicates")

    lim = raw["limits"]
    limits = Limits(
        starting_cash_usd=float(_number(lim, "starting_cash_usd", "limits", lo=0)),
        max_position_pct=float(_number(lim, "max_position_pct", "limits", lo=0, hi=100)),
        max_order_usd=float(_number(lim, "max_order_usd", "limits", lo=0)),
        min_cash_pct=float(_number(lim, "min_cash_pct", "limits", lo=0, hi=100)),
        daily_order_cap=int(_number(lim, "daily_order_cap", "limits", lo=0, integer=True)),
    )

    if raw["data"]["source"] not in DATA_SOURCES:
        raise ConfigError(f"data.source must be one of {DATA_SOURCES}")

    mode = raw["execution"]["mode"]
    if mode not in EXECUTION_MODES:
        raise ConfigError(f"execution.mode must be one of {EXECUTION_MODES}, got {mode!r}")

    rt = raw["runtime"]
    if not isinstance(rt["exclusive_models"], bool):
        raise ConfigError("runtime.exclusive_models must be true or false")
    return Config(
        scheduler=SchedulerSettings(
            exclusive=rt["exclusive_models"],
            max_concurrent=int(_number(rt, "max_concurrent_calls", "runtime", lo=1, hi=64,
                                       integer=True)),
            settle_s=float(_number(rt, "settle_ms", "runtime", lo=0, hi=10_000)) / 1000,
            max_batch=int(_number(rt, "max_batch", "runtime", lo=1, integer=True)),
        ),
        db_path=Path(raw["paths"]["db"]),
        models=models,
        agents=agents,
        symbols=tuple(s.upper() for s in symbols),
        data_source=raw["data"]["source"],
        csv_dir=Path(raw["data"]["csv_dir"]),
        macro_dir=Path(raw["data"]["macro_dir"]),
        fundamentals_dir=Path(raw["data"]["fundamentals_dir"]),
        macro_series=tuple(raw["data"]["macro_series"]),
        sec_user_agent=raw["data"]["sec_user_agent"],
        sec_cache_dir=Path(raw["data"]["sec_cache_dir"]),
        news_lookback_days=int(_number(raw["data"], "news_lookback_days", "data", lo=1,
                                       hi=3650, integer=True)),
        mcp_url=raw["tools"]["mcp_url"],
        batch_size=int(_number(raw["research"], "batch_size", "research", lo=1, integer=True)),
        queue_cap=int(_number(raw["research"], "queue_cap", "research", lo=1, integer=True)),
        min_avg_daily_volume_usd=float(
            _number(raw["research"], "min_avg_daily_volume_usd", "research", lo=0)
        ),
        cache_ttl_hours=int(
            _number(raw["research"], "cache_ttl_hours", "research", lo=0, integer=True)
        ),
        min_observations=int(
            _number(raw["research"], "min_observations", "research", lo=1, integer=True)
        ),
        stale_after_days=int(
            _number(raw["research"], "stale_after_days", "research", lo=0, integer=True)
        ),
        committee=_committee(raw["committee"]),
        max_tool_turns=int(_number(raw["teams"], "max_tool_turns", "teams", lo=1, hi=20,
                                   integer=True)),
        portfolio=_portfolio(raw["portfolio"]),
        limits=limits,
        execution_mode=mode,
    )


def load_config(path: str | Path | None = None) -> Config:
    raw = _defaults()
    if path is not None:
        with open(path, "rb") as fh:
            raw = _merge(raw, tomllib.load(fh))
    return _build(raw)
