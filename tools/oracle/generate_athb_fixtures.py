#!/usr/bin/env python3
"""Generate reviewed ATHB numerical fixtures in an isolated oracle environment.

This module deliberately imports the pinned upstream package and never imports the
production implementation. Normal tests must only read the checked-in JSON output.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import platform
import random
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from pythermalcomfort.models import pmv_athb
from pythermalcomfort.models._pmv_ppd_optimized import _pmv_ppd_optimized
from pythermalcomfort.utilities import v_relative

FORMULATION_ID = "athb_2022_ptc_4_4_2"
NUMERICAL_CONTRACT_VERSION = 1
ORACLE_VERSION = "4.4.2"
ORACLE_COMMIT = "2597e88fed10fec2f40d49759ed9b74e10ce9f89"
RESEARCH_COMMIT = "8cb4e7eabe25bc6dbbafa554a045ceacd56a865b"
PMV_CORE_SHA256 = "dd3c1f3d7ffacea65978cb080a1b676dbadc143eeb8121196dfa6b9b8a9d2518"
ATHB_SOURCE_SHA256 = "6ea3044b9ab0a229e3f47603d64c3070b707fb31271a9a92ff4fff0c92ec5c9d"
FORWARD_FILENAME = "athb_forward_v1.json"
ROOTS_FILENAME = "athb_roots_v1.json"
PROVENANCE_FILENAME = "provenance.json"
ROOT_NAMES = (
    "lower_comfort",
    "heating_control",
    "thermal_neutral",
    "cooling_control",
    "upper_comfort",
)
QUANTIZATION_BOUNDARY_CASE_IDS = (
    "relative-air-speed-numpy-tie-even",
    "public-vote-numpy-tie-even",
    "public-vote-upstream-binary64-straddle",
)

JsonObject = dict[str, Any]


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _json_bytes(value: JsonObject) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()


def _clothing_value(specification: JsonObject, adapted_met: float, running_mean_c: float) -> float:
    if specification["kind"] == "fixed":
        return float(specification["clo"])
    return float(
        10.0
        ** (
            -0.17168
            - 0.000485 * running_mean_c
            + 0.08176 * adapted_met
            - 0.00527 * running_mean_c * adapted_met
        )
    )


def _relative_speed(speed: JsonObject, original_met: float) -> float:
    value = float(speed["value_m_s"])
    if speed["kind"] == "relative" or original_met <= 1.0:
        return value
    return float(v_relative(value, original_met))


def _oracle_forward(inputs: JsonObject, *, include_public: bool = True) -> JsonObject:
    tdb_c = float(inputs["tdb_c"])
    tr_c = float(inputs["tr_c"])
    rh_pct = float(inputs["rh_pct"])
    original_met = float(inputs["met"])
    running_mean_c = float(inputs["running_mean_c"])
    relative_speed = _relative_speed(inputs["speed"], original_met)
    adapted_met = original_met - (0.234 * running_mean_c) / 58.2
    effective_clo = _clothing_value(inputs["clothing"], adapted_met, running_mean_c)

    pmv = float(
        _pmv_ppd_optimized(
            tdb_c,
            tr_c,
            relative_speed,
            rh_pct,
            adapted_met,
            effective_clo,
            0.0,
        )
    )
    transfer_coefficient = 0.303 * math.exp(-0.036 * adapted_met * 58.15) + 0.028
    thermal_load = pmv / transfer_coefficient
    unrounded_vote = (
        1.484
        + 0.0276 * thermal_load
        - 0.9602 * adapted_met
        - 0.0342 * running_mean_c
        + 0.0002264 * thermal_load * running_mean_c
        + 0.018696 * adapted_met * running_mean_c
        - 0.0002909 * thermal_load * adapted_met * running_mean_c
    )

    expected: JsonObject = {
        "adapted_met": adapted_met,
        "effective_clo": effective_clo,
        "relative_air_speed_m_s": relative_speed,
        "thermal_load_w_m2": thermal_load,
        "unrounded_vote": unrounded_vote,
    }
    if include_public:
        clothing_argument: bool | float = (
            False if inputs["clothing"]["kind"] == "automatic" else effective_clo
        )
        public = pmv_athb(
            tdb=tdb_c,
            tr=tr_c,
            vr=relative_speed,
            rh=rh_pct,
            met=original_met,
            t_running_mean=running_mean_c,
            clo=clothing_argument,
        )
        expected["public_vote"] = float(public.athb_pmv)
    return expected


def _case(case_id: str, inputs: JsonObject) -> JsonObject:
    return {"case_id": case_id, "expected": _oracle_forward(inputs), "inputs": inputs}


def _automatic() -> JsonObject:
    return {"kind": "automatic"}


def _fixed(clo: float) -> JsonObject:
    return {"clo": clo, "kind": "fixed"}


def _ambient(value: float) -> JsonObject:
    return {"kind": "ambient", "value_m_s": value}


def _relative(value: float) -> JsonObject:
    return {"kind": "relative", "value_m_s": value}


def _inputs(
    *,
    tdb_c: float,
    tr_c: float,
    speed: JsonObject,
    rh_pct: float,
    met: float,
    running_mean_c: float,
    clothing: JsonObject,
) -> JsonObject:
    return {
        "clothing": clothing,
        "met": met,
        "rh_pct": rh_pct,
        "running_mean_c": running_mean_c,
        "speed": speed,
        "tdb_c": tdb_c,
        "tr_c": tr_c,
    }


def _named_forward_cases() -> list[JsonObject]:
    specifications: list[tuple[str, JsonObject]] = [
        (
            "published-forward-anchor",
            _inputs(
                tdb_c=25.0,
                tr_c=25.0,
                speed=_relative(0.1),
                rh_pct=50.0,
                met=1.2,
                running_mean_c=20.0,
                clothing=_automatic(),
            ),
        ),
        (
            "baseline-current-sensation",
            _inputs(
                tdb_c=20.0,
                tr_c=20.0,
                speed=_ambient(0.1),
                rh_pct=50.0,
                met=1.1,
                running_mean_c=5.0,
                clothing=_automatic(),
            ),
        ),
        (
            "near-balanced-target",
            _inputs(
                tdb_c=19.5,
                tr_c=19.5,
                speed=_ambient(0.1),
                rh_pct=51.5759274364,
                met=1.1,
                running_mean_c=5.0,
                clothing=_automatic(),
            ),
        ),
        (
            "below-comfort-band",
            _inputs(
                tdb_c=16.0,
                tr_c=16.0,
                speed=_ambient(0.1),
                rh_pct=64.3079856685,
                met=1.1,
                running_mean_c=5.0,
                clothing=_automatic(),
            ),
        ),
        (
            "winter-current-sensation",
            _inputs(
                tdb_c=20.0,
                tr_c=20.0,
                speed=_ambient(0.1),
                rh_pct=50.0,
                met=1.1,
                running_mean_c=-10.0,
                clothing=_automatic(),
            ),
        ),
        (
            "humid-current-sensation",
            _inputs(
                tdb_c=28.0,
                tr_c=28.0,
                speed=_ambient(0.1),
                rh_pct=80.0,
                met=1.1,
                running_mean_c=25.0,
                clothing=_automatic(),
            ),
        ),
        (
            "fixed-clothing-low",
            _inputs(
                tdb_c=24.0,
                tr_c=22.0,
                speed=_relative(0.2),
                rh_pct=35.0,
                met=1.3,
                running_mean_c=18.0,
                clothing=_fixed(0.1),
            ),
        ),
        (
            "fixed-clothing-high",
            _inputs(
                tdb_c=18.0,
                tr_c=20.0,
                speed=_relative(0.05),
                rh_pct=65.0,
                met=0.9,
                running_mean_c=0.0,
                clothing=_fixed(2.0),
            ),
        ),
        (
            "ambient-speed-stationary-branch",
            _inputs(
                tdb_c=22.0,
                tr_c=22.0,
                speed=_ambient(0.4),
                rh_pct=45.0,
                met=1.0,
                running_mean_c=12.0,
                clothing=_automatic(),
            ),
        ),
        (
            "ambient-speed-moving-branch",
            _inputs(
                tdb_c=22.0,
                tr_c=22.0,
                speed=_ambient(0.4),
                rh_pct=45.0,
                met=1.7,
                running_mean_c=12.0,
                clothing=_automatic(),
            ),
        ),
        (
            "relative-air-speed-numpy-tie-even",
            _inputs(
                tdb_c=22.0,
                tr_c=22.0,
                speed=_ambient(0.4665),
                rh_pct=45.0,
                met=1.5,
                running_mean_c=12.0,
                clothing=_automatic(),
            ),
        ),
        (
            "public-vote-numpy-tie-even",
            _inputs(
                tdb_c=15.0,
                tr_c=15.0,
                speed=_relative(0.1),
                rh_pct=29.499923881562744,
                met=1.2,
                running_mean_c=20.0,
                clothing=_automatic(),
            ),
        ),
        (
            "public-vote-upstream-binary64-straddle",
            _inputs(
                tdb_c=14.005708558508,
                tr_c=14.005708558508,
                speed=_relative(0.1),
                rh_pct=50.0,
                met=1.2,
                running_mean_c=20.0,
                clothing=_automatic(),
            ),
        ),
        (
            "zero-relative-air-speed",
            _inputs(
                tdb_c=20.0,
                tr_c=20.0,
                speed=_relative(0.0),
                rh_pct=50.0,
                met=1.1,
                running_mean_c=5.0,
                clothing=_automatic(),
            ),
        ),
        (
            "relative-humidity-zero",
            _inputs(
                tdb_c=21.0,
                tr_c=21.0,
                speed=_relative(0.1),
                rh_pct=0.0,
                met=1.1,
                running_mean_c=16.0,
                clothing=_automatic(),
            ),
        ),
        (
            "relative-humidity-hundred",
            _inputs(
                tdb_c=21.0,
                tr_c=21.0,
                speed=_relative(0.1),
                rh_pct=100.0,
                met=1.1,
                running_mean_c=16.0,
                clothing=_automatic(),
            ),
        ),
        (
            "engineering-lower-temperature-boundaries",
            _inputs(
                tdb_c=5.0,
                tr_c=0.0,
                speed=_relative(0.0),
                rh_pct=0.0,
                met=0.8,
                running_mean_c=-30.0,
                clothing=_fixed(0.1),
            ),
        ),
        (
            "engineering-upper-temperature-boundaries",
            _inputs(
                tdb_c=40.0,
                tr_c=50.0,
                speed=_relative(2.0),
                rh_pct=100.0,
                met=2.0,
                running_mean_c=45.0,
                clothing=_fixed(2.0),
            ),
        ),
        (
            "divergent-cold-mrt",
            _inputs(
                tdb_c=26.0,
                tr_c=16.8,
                speed=_relative(0.15),
                rh_pct=55.0,
                met=1.2,
                running_mean_c=10.0,
                clothing=_automatic(),
            ),
        ),
        (
            "divergent-warm-mrt",
            _inputs(
                tdb_c=14.0,
                tr_c=23.2,
                speed=_relative(0.15),
                rh_pct=55.0,
                met=1.2,
                running_mean_c=10.0,
                clothing=_automatic(),
            ),
        ),
    ]
    return [_case(case_id, inputs) for case_id, inputs in specifications]


def _dense_forward_cases() -> tuple[JsonObject, list[JsonObject]]:
    axes: JsonObject = {
        "rh_pct": [0.0, 16.9, 50.0, 87.7, 100.0],
        "running_mean_c": [-30.0, -2.7, 16.0, 30.0, 41.3, 45.0],
        "tdb_c": [5.0, 12.6, 20.0, 27.0, 38.5, 40.0],
    }
    tr_cycle = [0.0, 12.6, 20.0, 27.0, 38.5, 50.0]
    speed_cycle = [_relative(0.0), _ambient(0.1), _ambient(0.7), _relative(1.9), _relative(2.0)]
    met_cycle = [0.8, 1.0, 1.1, 1.5, 2.0]
    clothing_cycle = [_automatic(), _fixed(0.1), _fixed(0.7), _fixed(2.0)]
    cases: list[JsonObject] = []
    index = 0
    for tdb_c in axes["tdb_c"]:
        for rh_pct in axes["rh_pct"]:
            for running_mean_c in axes["running_mean_c"]:
                inputs = _inputs(
                    tdb_c=tdb_c,
                    tr_c=tr_cycle[index % len(tr_cycle)],
                    speed=speed_cycle[index % len(speed_cycle)],
                    rh_pct=rh_pct,
                    met=met_cycle[index % len(met_cycle)],
                    running_mean_c=running_mean_c,
                    clothing=clothing_cycle[index % len(clothing_cycle)],
                )
                cases.append(_case(f"dense-{index:03d}", inputs))
                index += 1
    axes["secondary_cycles"] = {
        "clothing": clothing_cycle,
        "met": met_cycle,
        "speed": speed_cycle,
        "tr_c": tr_cycle,
    }
    return axes, cases


def _random_forward_cases(seed: int = 20260911, count: int = 48) -> list[JsonObject]:
    generator = random.Random(seed)
    cases: list[JsonObject] = []
    for index in range(count):
        clothing = _automatic() if index % 2 == 0 else _fixed(generator.uniform(0.1, 2.0))
        speed_kind = "ambient" if index % 3 else "relative"
        speed = {"kind": speed_kind, "value_m_s": generator.uniform(0.0, 2.0)}
        inputs = _inputs(
            tdb_c=generator.uniform(5.0, 40.0),
            tr_c=generator.uniform(0.0, 50.0),
            speed=speed,
            rh_pct=generator.uniform(0.0, 100.0),
            met=generator.uniform(0.8, 2.0),
            running_mean_c=generator.uniform(-30.0, 45.0),
            clothing=clothing,
        )
        cases.append(_case(f"seeded-{index:03d}", inputs))
    return cases


def _saturation_pressure_pa(temperature_c: float) -> float:
    kelvin = temperature_c + 273.15
    if temperature_c > 0.01:
        logarithm = (
            -5800.2206 / kelvin
            + 1.3914993
            - 0.048640239 * kelvin
            + 4.1764768e-5 * kelvin**2
            - 1.4452093e-8 * kelvin**3
            + 6.5459673 * math.log(kelvin)
        )
    else:
        logarithm = (
            -5674.5359 / kelvin
            + 6.3925247
            - 0.009677843 * kelvin
            + 6.2215701e-7 * kelvin**2
            + 2.0747825e-9 * kelvin**3
            - 9.484024e-13 * kelvin**4
            + 4.1635019 * math.log(kelvin)
        )
    return math.exp(logarithm)


def _dew_point_c(vapor_pressure_pa: float, upper_c: float) -> float | None:
    if vapor_pressure_pa == 0.0:
        return None
    low = -100.0
    high = upper_c
    for _ in range(100):
        midpoint = (low + high) / 2.0
        if _saturation_pressure_pa(midpoint) < vapor_pressure_pa:
            low = midpoint
        else:
            high = midpoint
    return (low + high) / 2.0


def _candidate_environment(
    scenario: JsonObject,
    room_temperature_c: float,
    vapor_pressure_pa: float,
) -> tuple[float, float, float]:
    local_temperature_c = room_temperature_c - float(scenario.get("local_delta_c", 0.0))
    saturation = _saturation_pressure_pa(local_temperature_c)
    rh_pct = 0.0 if vapor_pressure_pa == 0.0 else 100.0 * vapor_pressure_pa / saturation
    radiant = scenario["radiant"]
    if radiant["kind"] == "moving_uniform":
        tr_c = local_temperature_c
    elif radiant["kind"] == "fixed":
        tr_c = float(radiant["tr_c"])
    elif radiant["kind"] == "surface_composite":
        view_factor = float(radiant["view_factor"])
        surface_kelvin = float(radiant["surface_temperature_c"]) + 273.15
        background_kelvin = room_temperature_c + 273.15
        tr_c = (
            view_factor * surface_kelvin**4 + (1.0 - view_factor) * background_kelvin**4
        ) ** 0.25 - 273.15
    else:  # pragma: no cover - generator input is internal and fixed
        raise ValueError(f"unsupported radiant kind: {radiant['kind']}")
    return local_temperature_c, tr_c, rh_pct


def _root_inputs(
    scenario: JsonObject,
    room_temperature_c: float,
    vapor_pressure_pa: float,
) -> JsonObject:
    local_temperature_c, tr_c, rh_pct = _candidate_environment(
        scenario, room_temperature_c, vapor_pressure_pa
    )
    return _inputs(
        tdb_c=local_temperature_c,
        tr_c=tr_c,
        speed=scenario["speed"],
        rh_pct=rh_pct,
        met=float(scenario["met"]),
        running_mean_c=float(scenario["running_mean_c"]),
        clothing=scenario["clothing"],
    )


def _solve_root(scenario: JsonObject, requested_vote: float) -> JsonObject:
    current = scenario["current"]
    vapor_pressure_pa = (
        float(current["rh_pct"]) / 100.0 * _saturation_pressure_pa(float(current["tdb_c"]))
    )
    dew_point = _dew_point_c(vapor_pressure_pa, float(current["tdb_c"]))
    lower = 5.0
    if dew_point is not None:
        lower = max(lower, dew_point + float(scenario.get("local_delta_c", 0.0)))
    upper = 40.0
    evaluations = 0

    def residual(room_temperature_c: float) -> float:
        nonlocal evaluations
        evaluations += 1
        inputs = _root_inputs(scenario, room_temperature_c, vapor_pressure_pa)
        return (
            float(_oracle_forward(inputs, include_public=False)["unrounded_vote"]) - requested_vote
        )

    sample_count = 257
    points = [lower + (upper - lower) * index / (sample_count - 1) for index in range(sample_count)]
    values = [residual(point) for point in points]
    brackets: list[tuple[float, float, float, float]] = []
    for left, right, left_value, right_value in zip(
        points[:-1], points[1:], values[:-1], values[1:], strict=True
    ):
        if left_value == 0.0:
            brackets.append((left, left, left_value, left_value))
        elif left_value * right_value < 0.0:
            brackets.append((left, right, left_value, right_value))
    if values[-1] == 0.0:
        brackets.append((upper, upper, values[-1], values[-1]))

    if not brackets:
        if lower > 5.0 and values[0] > 0.0:
            return {
                "evaluation_count": evaluations,
                "failure": "moisture_limited_no_solution",
                "requested_vote": requested_vote,
            }
        failure = "below_search_domain" if values[0] > 0.0 else "above_search_domain"
        return {
            "evaluation_count": evaluations,
            "failure": failure,
            "requested_vote": requested_vote,
        }
    if len(brackets) != 1:
        return {
            "evaluation_count": evaluations,
            "failure": "multiple_brackets",
            "requested_vote": requested_vote,
        }

    left, right, left_value, _right_value = brackets[0]
    initial_bracket = [left, right]
    for _ in range(80):
        if right - left <= 1e-10:
            break
        midpoint = (left + right) / 2.0
        midpoint_value = residual(midpoint)
        if midpoint_value == 0.0:
            left = midpoint
            right = midpoint
            break
        if left_value * midpoint_value <= 0.0:
            right = midpoint
        else:
            left = midpoint
            left_value = midpoint_value

    room_temperature_c = (left + right) / 2.0
    inputs = _root_inputs(scenario, room_temperature_c, vapor_pressure_pa)
    expected = _oracle_forward(inputs)
    evaluations += 1
    return {
        "bracket_width_c": right - left,
        "candidate_inputs": inputs,
        "evaluation_count": evaluations,
        "initial_bracket_c": initial_bracket,
        "local_candidate_temperature_c": inputs["tdb_c"],
        "requested_vote": requested_vote,
        "residual_vote": expected["unrounded_vote"] - requested_vote,
        "room_temperature_c": room_temperature_c,
        "unrounded_vote": expected["unrounded_vote"],
    }


def _root_scenario(
    scenario_id: str,
    *,
    current_tdb_c: float = 20.0,
    current_rh_pct: float = 50.0,
    running_mean_c: float = 5.0,
    requested_votes: Sequence[float] = (-0.5, -0.25, 0.0, 0.25, 0.5),
    radiant: JsonObject | None = None,
    local_delta_c: float = 0.0,
) -> JsonObject:
    scenario: JsonObject = {
        "clothing": _automatic(),
        "current": {"rh_pct": current_rh_pct, "tdb_c": current_tdb_c},
        "local_delta_c": local_delta_c,
        "met": 1.1,
        "moisture_model": "constant_vapor_pressure",
        "radiant": radiant or {"kind": "moving_uniform"},
        "running_mean_c": running_mean_c,
        "scenario_id": scenario_id,
        "search_domain_room_c": [5.0, 40.0],
        "speed": _ambient(0.1),
    }
    current_room = current_tdb_c
    vapor_pressure_pa = current_rh_pct / 100.0 * _saturation_pressure_pa(current_tdb_c)
    current_inputs = _root_inputs(scenario, current_room, vapor_pressure_pa)
    roots = {
        name: _solve_root(scenario, float(vote))
        for name, vote in zip(ROOT_NAMES, requested_votes, strict=True)
    }
    scenario["current_expected"] = _oracle_forward(current_inputs)
    scenario["current_inputs"] = current_inputs
    scenario["requested_votes"] = dict(zip(ROOT_NAMES, requested_votes, strict=True))
    scenario["roots"] = roots
    return scenario


def _root_scenarios() -> list[JsonObject]:
    scenarios = [
        _root_scenario("G_BASE_B"),
        _root_scenario("G_BASE_E", requested_votes=(-0.5, -0.35, 0.0, 0.35, 0.5)),
        _root_scenario("G_BASE_C", requested_votes=(-0.5, -0.15, 0.0, 0.15, 0.5)),
        _root_scenario("G_NEAR_B", current_tdb_c=19.5, current_rh_pct=51.5759274364),
        _root_scenario("G_BELOW_B", current_tdb_c=16.0, current_rh_pct=64.3079856685),
        _root_scenario(
            "G_SURFACE16_B",
            radiant={
                "kind": "surface_composite",
                "surface_temperature_c": 16.0,
                "view_factor": 0.25,
            },
        ),
        _root_scenario(
            "G_SURFACE14_B",
            radiant={
                "kind": "surface_composite",
                "surface_temperature_c": 14.0,
                "view_factor": 0.25,
            },
        ),
        _root_scenario("G_WINTER_B", running_mean_c=-10.0),
        _root_scenario(
            "G_HUMID_B",
            current_tdb_c=28.0,
            current_rh_pct=80.0,
            running_mean_c=25.0,
        ),
        _root_scenario("G_WIDE_B", requested_votes=(-1.0, -0.5, 0.0, 0.5, 1.0)),
        _root_scenario("FIXED_MRT16_B", radiant={"kind": "fixed", "tr_c": 16.0}),
        _root_scenario("CRITICAL_DELTA_1_5_B", local_delta_c=1.5),
        _root_scenario("CRITICAL_DELTA_2_B", local_delta_c=2.0),
        _root_scenario("CRITICAL_DELTA_3_B", local_delta_c=3.0),
    ]
    for running_mean_c in (-20.0, -10.0, 0.0, 5.0, 10.0, 20.0, 30.0):
        scenarios.append(
            _root_scenario(
                f"OUTDOOR_MATRIX_{running_mean_c:+05.1f}",
                running_mean_c=running_mean_c,
            )
        )
    return scenarios


def _package_versions(names: Iterable[str]) -> JsonObject:
    return {name: importlib.metadata.version(name) for name in names}


def _write_outputs(output_dir: Path, generated_at: str) -> None:
    root = Path(__file__).resolve().parents[2]
    generator_path = Path(__file__).resolve()
    lock_path = root / "tools/oracle/requirements.lock"
    dense_axes, dense_cases = _dense_forward_cases()
    random_seed = 20260911
    random_cases = _random_forward_cases(seed=random_seed)
    named_cases = _named_forward_cases()

    forward: JsonObject = {
        "dense_grid": {"axes": dense_axes, "cases": dense_cases},
        "fixture_kind": "athb_forward",
        "formulation_id": FORMULATION_ID,
        "named_cases": named_cases,
        "numerical_contract_version": NUMERICAL_CONTRACT_VERSION,
        "oracle": {
            "package": f"pythermalcomfort=={ORACLE_VERSION}",
            "repository_commit": ORACLE_COMMIT,
            "transformation": "remove only pmv_athb final three-decimal rounding",
        },
        "quantization_boundary_case_ids": list(QUANTIZATION_BOUNDARY_CASE_IDS),
        "seeded_random": {
            "cases": random_cases,
            "count": len(random_cases),
            "seed": random_seed,
        },
        "tolerances": {
            "public_vote_absolute": 0.00051,
            "unrounded_vote_absolute": 1e-7,
        },
        "vector_count": len(named_cases) + len(dense_cases) + len(random_cases),
    }
    roots: JsonObject = {
        "fixture_kind": "athb_roots",
        "formulation_id": FORMULATION_ID,
        "future_production_solver": False,
        "generator_solver": {
            "bracket_samples": 257,
            "method": "independent constant-vapor-pressure bracket scan and bisection",
            "temperature_width_c": 1e-10,
        },
        "numerical_contract_version": NUMERICAL_CONTRACT_VERSION,
        "oracle": {
            "package": f"pythermalcomfort=={ORACLE_VERSION}",
            "repository_commit": ORACLE_COMMIT,
            "transformation": "remove only pmv_athb final three-decimal rounding",
        },
        "scenarios": _root_scenarios(),
        "tolerances": {
            "displayed_current_vote_absolute": 1e-6,
            "root_temperature_absolute_c": 0.01,
            "unrounded_vote_absolute": 1e-7,
        },
    }

    forward_bytes = _json_bytes(forward)
    roots_bytes = _json_bytes(roots)
    provenance: JsonObject = {
        "artifact_hashes": {
            FORWARD_FILENAME: _sha256_bytes(forward_bytes),
            ROOTS_FILENAME: _sha256_bytes(roots_bytes),
        },
        "fixture_counts": {
            "dense_forward": len(dense_cases),
            "named_forward": len(named_cases),
            "quantization_boundary_forward": len(QUANTIZATION_BOUNDARY_CASE_IDS),
            "root_scenarios": len(roots["scenarios"]),
            "seeded_forward": len(random_cases),
            "total_forward": forward["vector_count"],
        },
        "formulation_id": FORMULATION_ID,
        "generated_at_utc": generated_at,
        "generation_command": (
            "python tools/oracle/generate_athb_fixtures.py "
            f"--output-dir tests/fixtures/numerical --generated-at {generated_at}"
        ),
        "generator_sha256": _sha256_file(generator_path),
        "isolation": {
            "imports_production_core": False,
            "normal_tests_regenerate": False,
            "oracle_only_dependencies": ["numpy", "scipy", "numba", "llvmlite"],
            "python": platform.python_version(),
            "runtime": platform.platform(),
        },
        "numerical_contract_version": NUMERICAL_CONTRACT_VERSION,
        "package_versions": _package_versions(
            ("pythermalcomfort", "numpy", "scipy", "numba", "llvmlite", "setuptools")
        ),
        "repository_revisions": {
            "pythermalcomfort": ORACLE_COMMIT,
            "research_analysis": RESEARCH_COMMIT,
        },
        "requirements_lock_sha256": _sha256_file(lock_path),
        "schema_version": 1,
        "source_hashes": {
            "_pmv_ppd_optimized.py": PMV_CORE_SHA256,
            "pmv_athb.py": ATHB_SOURCE_SHA256,
        },
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / FORWARD_FILENAME).write_bytes(forward_bytes)
    (output_dir / ROOTS_FILENAME).write_bytes(roots_bytes)
    (output_dir / PROVENANCE_FILENAME).write_bytes(_json_bytes(provenance))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument(
        "--generated-at",
        required=True,
        help="Reviewed UTC timestamp in YYYY-MM-DDTHH:MM:SSZ form.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if not args.generated_at.endswith("Z"):
        raise SystemExit("--generated-at must be an explicit UTC timestamp ending in Z")
    installed = importlib.metadata.version("pythermalcomfort")
    if installed != ORACLE_VERSION:
        raise SystemExit(f"expected pythermalcomfort {ORACLE_VERSION}, found {installed}")
    _write_outputs(args.output_dir, args.generated_at)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
