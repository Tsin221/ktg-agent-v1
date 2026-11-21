from decimal import Decimal

import pytest

from ktg_agent.exploration.query import aggregate_rows, matches, order_rows, validate_filter
from ktg_agent.core.messages import JsonObject
from ktg_agent.ktg.client import KTGError


FIELDS = {"id": "integer", "code": "string", "quantity": "decimal", "date": "date"}


def test_filters_preserve_prefix_and_decimal_semantics() -> None:
    node = {"all": [{"field": "code", "op": "starts_with", "value": "Ｒ"},
                    {"field": "quantity", "op": "gte", "value": "9.5"}]}
    validate_filter(node, FIELDS)
    assert matches({"code": "r-01", "quantity": "10.25"}, node, FIELDS)
    assert not matches({"code": "X-R01", "quantity": "10.25"}, node, FIELDS)
    assert not matches({"code": "R01", "quantity": "9"}, node, FIELDS)


def test_date_intervals_are_half_open_and_order_is_numeric_stable() -> None:
    node = {"field": "date", "op": "between", "value": ["2026-11-01", "2026-12-01"]}
    validate_filter(node, FIELDS)
    assert matches({"date": "2026-11-30"}, node, FIELDS)
    assert not matches({"date": "2026-12-01"}, node, FIELDS)
    assert order_rows([{"id": 10, "quantity": "2"}, {"id": 2, "quantity": "2"}],
                      [{"field": "quantity", "direction": "desc"}], FIELDS) == [
                          {"id": 2, "quantity": "2"}, {"id": 10, "quantity": "2"}]


@pytest.mark.parametrize("node", [
    {"field": "password", "op": "eq", "value": "x"},
    {"field": "code", "op": "in", "value": []},
    {"field": "quantity", "op": "gte", "value": True},
    {"field": "code", "op": "eq", "value": None},
    {"all": []},
    {"field": "code", "op": "eq", "value": "x", "sql": "1=1"},
    {"field": "code", "op": [], "value": "x"},
])
def test_unsupported_or_ambiguous_filters_fail_closed(node: dict[str, object]) -> None:
    with pytest.raises(KTGError):
        validate_filter(node, FIELDS)


def test_aggregation_counts_root_records_and_sums_without_float_loss() -> None:
    rows: list[JsonObject] = [{"id": 1, "quantity": "0.1"}, {"id": 1, "quantity": "0.1"},
                              {"id": 2, "quantity": "0.2"}]
    result = aggregate_rows(rows, [], [{"op": "count_records", "as": "n"},
                                     {"op": "sum", "field": "quantity", "as": "q"}], FIELDS)
    assert result == [{"n": 2, "q": "0.3"}]
    assert Decimal(str(result[0]["q"])) == Decimal("0.3")


def test_conflicting_duplicate_roots_are_not_silently_deduplicated() -> None:
    with pytest.raises(KTGError, match="粒度"):
        aggregate_rows([{"id": 1, "quantity": "1"}, {"id": 1, "quantity": "2"}], [],
                       [{"op": "sum", "field": "quantity", "as": "q"}], FIELDS)
