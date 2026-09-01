from __future__ import annotations

import pytest

from minicode.cost_tracker import calculate_cost


def test_calculate_cost_returns_float_for_deepseek_usage() -> None:
    cost = calculate_cost(
        "deepseek-v4-pro",
        input_tokens=1_000_000,
        output_tokens=500_000,
        cache_read_tokens=250_000,
        cache_creation_tokens=100_000,
    )

    assert isinstance(cost, float)
    assert cost == pytest.approx(3.443)


def test_calculate_cost_uses_default_pricing_without_decimal_type_error() -> None:
    cost = calculate_cost(
        "future-provider-model",
        input_tokens=1_000,
        output_tokens=500,
    )

    assert cost == pytest.approx(0.0105)
