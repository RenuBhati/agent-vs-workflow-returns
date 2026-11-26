"""Scenario files load, and mistakes in them fail before any run."""

import pytest
import yaml
from pydantic import ValidationError

from evals.scenario import SCENARIO_DIR, Scenario, _check_against_fixture, load_split

EXAMPLE = SCENARIO_DIR / "dev" / "exch-blue-medium.yaml"


def example() -> dict:
    return yaml.safe_load(EXAMPLE.read_text())


def test_all_dev_scenarios_load():
    scenarios = load_split("dev")
    assert scenarios and all(s.split == "dev" for s in scenarios)
    assert len({s.id for s in scenarios}) == len(scenarios)   # ids are unique


def test_typo_in_a_field_name_is_rejected():
    data = example()
    data["expected"]["stok_delta"] = data["expected"].pop("stock_delta")
    with pytest.raises(ValidationError, match="stok_delta"):
        Scenario.model_validate(data)


def test_exchange_without_a_size_is_rejected():
    data = example()
    del data["expected"]["replacement_size"]
    with pytest.raises(ValidationError, match="replacement_size"):
        Scenario.model_validate(data)


def test_no_action_outcome_cannot_change_stock():
    data = example()
    data["expected"] = {"outcome": "no_action", "stock_delta": {"oxford_blue_M": -1}}
    with pytest.raises(ValidationError, match="cannot change"):
        Scenario.model_validate(data)


@pytest.mark.parametrize("path, value, message", [
    (("expected", "acceptable_units"), ["u_amira_bleu"], "unknown unit"),
    (("expected", "stock_delta"), {"oxford_blu_M": -1}, "unknown sku"),
    (("persona",), "amyra", "persona"),
])
def test_ids_are_checked_against_the_fixture(path, value, message):
    data = example()
    target = data
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValueError, match=message):
        _check_against_fixture(Scenario.model_validate(data))
