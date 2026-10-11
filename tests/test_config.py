from pathlib import Path

import pytest

from qfii.config import ConfigError, load_config


def write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "local.toml"
    p.write_text(text)
    return p


def test_defaults_load():
    cfg = load_config()
    assert cfg.execution_mode == "paper"
    assert set(cfg.agents) == {"research", "assessment", "risk",
                               "aggressive", "conservative", "neutral"}
    assert cfg.agents["research"].tier == "mid"
    # neutral thinks hardest; the two biased members share the high tier
    assert cfg.agents["neutral"].tier == "strongest"
    assert cfg.agents["aggressive"].tier == cfg.agents["conservative"].tier == "strong"
    assert cfg.model_for("risk") == cfg.models["strong"]
    c = cfg.committee
    assert (c.max_rounds, c.invest_rule, c.procedural_rule) == (5, "unanimous", "majority")
    assert cfg.portfolio.benchmark == "SPY"
    assert cfg.data_source == "synthetic" and cfg.mcp_url == ""


def test_openai_compat_model(tmp_path):
    cfg = load_config(write(tmp_path, '[models.mid]\nprovider = "openai_compat"\n'
                                      'model = "meta/llama-3.1-8b-instruct"\n'
                                      'base_url = "http://localhost:8000/v1"\n'))
    assert cfg.model_for("research").model == "meta/llama-3.1-8b-instruct"


def test_override_merges(tmp_path):
    cfg = load_config(write(tmp_path, '[limits]\nmax_order_usd = 2500\n[universe]\nsymbols = ["aapl"]\n'))
    assert cfg.limits.max_order_usd == 2500
    assert cfg.limits.max_position_pct == 10.0  # untouched default
    assert cfg.symbols == ("AAPL",)


@pytest.mark.parametrize("text, msg", [
    ("[limits]\nmax_order = 5\n", "unknown config key: limits.max_order"),
    ("[limits]\nmax_position_pct = 150\n", "max_position_pct"),
    ("[limits]\ndaily_order_cap = 2.5\n", "integer"),
    ('[limits]\nmax_order_usd = "lots"\n', "number"),
    ('[agents.research]\ntier = "genius"\n', "tier"),
    ('[execution]\nmode = "live"\n', "execution.mode"),
    ("[universe]\nsymbols = []\n", "universe.symbols"),
    ('[universe]\nsymbols = ["A", "A"]\n', "duplicates"),
    ("[committee]\nmax_rounds = 0\n", "max_rounds"),
    ('[committee]\ninvest_rule = "chair_decides"\n', "committee.invest_rule"),
    ('[committee]\nno_consensus = "buy"\n', "unknown config key: committee.no_consensus"),
    ("[portfolio]\nshort_term_tax_pct = 30\n", "unknown config key: portfolio.short_term"),
    ("[agents.chair]\ntier = \"strongest\"\n", "unknown config key: agents.chair"),
    ('[models.mid]\nprovider = "magic"\n', "provider"),
    ('[models.mid]\nprovider = "openai_compat"\n', "base_url"),
    ('[data]\nsource = "bloomberg"\n', "data.source"),
])
def test_bad_config_rejected(tmp_path, text, msg):
    with pytest.raises(ConfigError, match=msg):
        load_config(write(tmp_path, text))
