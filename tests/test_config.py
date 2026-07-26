from __future__ import annotations

import pytest

from poeflip.config import MIN_FETCH_INTERVAL_FLOOR_MINUTES, load_config
from poeflip.errors import ConfigError


def test_shipped_config_loads(write_config):
    cfg = load_config(write_config())
    assert cfg.bankroll.total_chaos == 200
    assert cfg.bankroll.max_position_chaos == pytest.approx(50.0)
    assert cfg.bankroll.max_corrupt_cost_chaos == pytest.approx(8.0)


def test_placeholder_user_agent_fails_startup(write_config):
    """Spec 12.7: startup must fail while the placeholder is unedited."""
    path = write_config(app={"user_agent": "poe-flip/0.1 (contact: CHANGEME@example.com)"})
    with pytest.raises(ConfigError) as excinfo:
        load_config(path)
    message = str(excinfo.value)
    assert "placeholder" in message
    assert "config.yaml" in message


def test_example_dot_com_also_counts_as_a_placeholder(write_config):
    path = write_config(app={"user_agent": "poe-flip/0.1 (contact: me@example.com)"})
    with pytest.raises(ConfigError):
        load_config(path)


def test_fetch_interval_floor_is_enforced_in_code(write_config):
    """Spec 2.3: the 5-minute floor is not configurable downward."""
    path = write_config(app={"user_agent": "ok/0.1 (contact: a@b.tld)",
                             "min_fetch_interval_minutes": 1})
    with pytest.raises(ConfigError) as excinfo:
        load_config(path)
    assert str(MIN_FETCH_INTERVAL_FLOOR_MINUTES) in str(excinfo.value)


def test_outcome_probabilities_must_sum_to_one(write_config, config_dict):
    tables = config_dict["corrupt"]["outcome_tables"]
    tables["with_vaal_variant"][0]["probability"] = 0.5
    path = write_config(corrupt={**config_dict["corrupt"], "outcome_tables": tables})
    with pytest.raises(ConfigError) as excinfo:
        load_config(path)
    assert "sum to" in str(excinfo.value)


def test_without_vaal_table_may_not_contain_a_transform(write_config, config_dict):
    tables = config_dict["corrupt"]["outcome_tables"]
    tables["without_vaal_variant"] = [
        {"outcome": "vaal_transform", "level_delta": 0, "quality_delta": 0, "probability": 0.5},
        {"outcome": "no_change", "level_delta": 0, "quality_delta": 0, "probability": 0.5},
    ]
    path = write_config(corrupt={**config_dict["corrupt"], "outcome_tables": tables})
    with pytest.raises(ConfigError) as excinfo:
        load_config(path)
    assert "cannot transform" in str(excinfo.value)


def test_missing_section_is_named(write_config, config_dict):
    del config_dict["bankroll"]
    path = write_config()
    import yaml
    path.write_text(yaml.safe_dump(config_dict), encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        load_config(path)
    assert "bankroll" in str(excinfo.value)


def test_missing_config_file_names_the_path(tmp_path):
    with pytest.raises(ConfigError) as excinfo:
        load_config(tmp_path / "nope.yaml")
    assert "nope.yaml" in str(excinfo.value)
