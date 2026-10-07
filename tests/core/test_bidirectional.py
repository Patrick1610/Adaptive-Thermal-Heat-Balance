"""Continuous, stateless bidirectional scalar selection rules."""

import pytest

from custom_components.athb.core.bidirectional import select_scalar_target


@pytest.mark.parametrize(
    ("inside", "outside", "branch", "expected", "reason"),
    [
        (26, 10, "neutral", 23, "cold_outside_hot_inside"),
        (20, 30, "neutral", 23, "hot_outside_cold_inside"),
        (22, 10, "heating", 21, "outside_below_neutral"),
        (24, 30, "cooling", 25, "outside_above_neutral"),
        (24, 23, "neutral", 23, "outside_at_neutral"),
        (25, 10, "heating", 21, "outside_below_neutral"),
        (20, 25, "cooling", 25, "outside_above_neutral"),
        (26, 21, "heating", 21, "outside_below_neutral"),
        (21, 30, "cooling", 25, "outside_above_neutral"),
    ],
)
def test_selection_rules_and_strict_boundaries(inside, outside, branch, expected, reason):
    selection = select_scalar_target(
        indoor_c=inside, outdoor_c=outside, heating_c=21, neutral_c=23, cooling_c=25
    )
    assert selection is not None
    assert (selection.branch, selection.requested_c, selection.reason) == (branch, expected, reason)
    assert selection.indoor_c == inside
    assert selection.outdoor_c == outside


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), True])
def test_invalid_current_measurement_never_selects(invalid):
    assert (
        select_scalar_target(
            indoor_c=invalid, outdoor_c=10, heating_c=21, neutral_c=23, cooling_c=25
        )
        is None
    )


def test_invalid_root_order_never_selects():
    assert (
        select_scalar_target(indoor_c=22, outdoor_c=10, heating_c=24, neutral_c=23, cooling_c=25)
        is None
    )
