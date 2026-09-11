"""Radiant model semantics and physical distinction tests."""

from __future__ import annotations

import math

import pytest

from custom_components.athb.core.contracts import (
    DerivedMeanRadiantTemperature,
    MeasuredGlobeTemperature,
    MeasuredMeanRadiantTemperature,
    MeasuredSurfaceTemperature,
    ModelledSurfaceTemperature,
    Provenance,
    RadiantFailureCode,
)
from custom_components.athb.core.radiant import (
    DirectRadiantModel,
    GlobeRadiantModel,
    RadiantFailure,
    RadiantMode,
    RadiantValue,
    SurfaceCompositeRadiantModel,
    SurfaceContribution,
    UniformRadiantModel,
    globe_mean_radiant_temperature,
)


def test_uniform_default_moves_with_candidate_air_and_is_estimated() -> None:
    model = UniformRadiantModel()
    current = model.current_mrt(20.0)
    candidate = model.candidate_mrt(24.5)
    assert current == RadiantValue(
        20.0,
        RadiantMode.UNIFORM,
        Provenance.ESTIMATED,
        ("estimated_uniform_radiant_environment",),
    )
    assert isinstance(candidate, RadiantValue)
    assert candidate.mrt_c == 24.5


def test_direct_mrt_is_held_constant_for_candidate() -> None:
    model = DirectRadiantModel(MeasuredMeanRadiantTemperature(16.0))
    assert model.current_mrt(20.0) == model.candidate_mrt(35.0)
    result = model.candidate_mrt(35.0)
    assert isinstance(result, RadiantValue)
    assert result.mrt_c == 16.0
    assert result.provenance is Provenance.MEASURED


def test_globe_iso_reference_formula_and_snapshot_hold() -> None:
    derived = globe_mean_radiant_temperature(
        globe_temperature_c=25.0,
        air_temperature_c=20.0,
        ambient_air_speed_m_s=0.1,
        diameter_m=0.15,
        emissivity=0.95,
    )
    assert isinstance(derived, DerivedMeanRadiantTemperature)
    assert derived.value_c == pytest.approx(27.9163, abs=0.001)
    model = GlobeRadiantModel.from_observations(
        globe=MeasuredGlobeTemperature(25.0),
        air_temperature_c=20.0,
        ambient_air_speed_m_s=0.1,
    )
    assert isinstance(model, GlobeRadiantModel)
    assert model.current_mrt(20.0) == model.candidate_mrt(30.0)


def test_small_globe_warning_is_explicit() -> None:
    result = globe_mean_radiant_temperature(
        globe_temperature_c=22.0,
        air_temperature_c=20.0,
        ambient_air_speed_m_s=0.0,
        diameter_m=0.04,
    )
    assert isinstance(result, DerivedMeanRadiantTemperature)
    assert "small_globe_measurement_quality" in result.reasons


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"diameter_m": 0.03}, RadiantFailureCode.INVALID_GLOBE_CONFIGURATION),
        ({"diameter_m": 0.16}, RadiantFailureCode.INVALID_GLOBE_CONFIGURATION),
        ({"emissivity": 0.79}, RadiantFailureCode.INVALID_GLOBE_CONFIGURATION),
        ({"ambient_air_speed_m_s": 2.1}, RadiantFailureCode.OUTSIDE_SOURCE_DOMAIN),
        ({"globe_temperature_c": math.nan}, RadiantFailureCode.NON_FINITE),
        ({"air_temperature_c": True}, RadiantFailureCode.BOOLEAN_INPUT),
    ],
)
def test_invalid_globe_inputs_are_typed(
    kwargs: dict[str, float | bool], code: RadiantFailureCode
) -> None:
    inputs: dict[str, float | bool] = {
        "globe_temperature_c": 22.0,
        "air_temperature_c": 20.0,
        "ambient_air_speed_m_s": 0.1,
    }
    inputs.update(kwargs)
    result = globe_mean_radiant_temperature(**inputs)  # type: ignore[arg-type]
    assert isinstance(result, RadiantFailure)
    assert result.code is code


def test_surface_composite_uses_fourth_power_and_moving_background() -> None:
    model = SurfaceCompositeRadiantModel(
        (
            SurfaceContribution(MeasuredSurfaceTemperature(10.0), 0.25),
            SurfaceContribution(MeasuredSurfaceTemperature(30.0), 0.25),
        )
    )
    current = model.current_mrt(20.0)
    candidate = model.candidate_mrt(25.0)
    assert isinstance(current, RadiantValue)
    assert isinstance(candidate, RadiantValue)
    expected = (
        0.25 * (10.0 + 273.15) ** 4 + 0.25 * (30.0 + 273.15) ** 4 + 0.5 * (20.0 + 273.15) ** 4
    ) ** 0.25 - 273.15
    assert current.mrt_c == pytest.approx(expected, abs=1e-12)
    assert candidate.mrt_c > current.mrt_c
    assert candidate.provenance is Provenance.ESTIMATED


def test_surface_composite_fixed_measured_background_is_held_constant() -> None:
    model = SurfaceCompositeRadiantModel(
        (SurfaceContribution(MeasuredSurfaceTemperature(10.0), 0.25),),
        fixed_background=MeasuredMeanRadiantTemperature(22.0),
    )
    assert model.current_mrt(18.0) == model.candidate_mrt(35.0)
    result = model.current_mrt(18.0)
    assert isinstance(result, RadiantValue)
    assert result.provenance is Provenance.MEASURED


def test_modelled_surface_remains_estimated_and_explicit() -> None:
    model = SurfaceCompositeRadiantModel(
        (SurfaceContribution(ModelledSurfaceTemperature(12.0), 0.2),)
    )
    result = model.current_mrt(20.0)
    assert isinstance(result, RadiantValue)
    assert result.provenance is Provenance.ESTIMATED
    assert "modelled_surface" in result.reasons


def test_missing_surface_falls_back_without_redistributing_factor() -> None:
    model = SurfaceCompositeRadiantModel(
        (SurfaceContribution(MeasuredSurfaceTemperature(None), 0.5),)
    )
    result = model.candidate_mrt(23.0)
    assert isinstance(result, RadiantValue)
    assert result.mrt_c == 23.0
    assert result.mode is RadiantMode.UNIFORM
    assert result.reasons == (
        "estimated_uniform_radiant_environment",
        "radiant_fallback_missing_surface_data",
    )


@pytest.mark.parametrize(
    ("model", "code"),
    [
        (
            SurfaceCompositeRadiantModel(
                (SurfaceContribution(MeasuredSurfaceTemperature(10.0), 0.0),)
            ),
            RadiantFailureCode.INVALID_VIEW_FACTOR,
        ),
        (
            SurfaceCompositeRadiantModel(
                (
                    SurfaceContribution(MeasuredSurfaceTemperature(10.0), 0.6),
                    SurfaceContribution(MeasuredSurfaceTemperature(20.0), 0.5),
                )
            ),
            RadiantFailureCode.INVALID_VIEW_FACTOR,
        ),
        (
            SurfaceCompositeRadiantModel(
                tuple(SurfaceContribution(MeasuredSurfaceTemperature(20.0), 0.1) for _ in range(9))
            ),
            RadiantFailureCode.TOO_MANY_SURFACES,
        ),
    ],
)
def test_invalid_surface_configuration_is_typed(
    model: SurfaceCompositeRadiantModel, code: RadiantFailureCode
) -> None:
    result = model.current_mrt(20.0)
    assert isinstance(result, RadiantFailure)
    assert result.code is code


def test_globe_model_rejects_invalid_observation() -> None:
    model = GlobeRadiantModel.from_observations(
        globe=MeasuredGlobeTemperature(300.0),
        air_temperature_c=20.0,
        ambient_air_speed_m_s=0.1,
    )
    assert isinstance(model, RadiantFailure)


def test_radiant_models_propagate_invalid_direct_and_background_values() -> None:
    uniform = UniformRadiantModel().candidate_mrt(math.nan)
    direct = DirectRadiantModel(MeasuredMeanRadiantTemperature(math.inf)).current_mrt(20.0)
    composite = SurfaceCompositeRadiantModel(
        (), fixed_background=MeasuredMeanRadiantTemperature(math.nan)
    ).current_mrt(20.0)
    assert isinstance(uniform, RadiantFailure)
    assert isinstance(direct, RadiantFailure)
    assert isinstance(composite, RadiantFailure)


def test_surface_composite_rejects_nonnumeric_factor_and_temperature() -> None:
    bad_factor = SurfaceCompositeRadiantModel(
        (SurfaceContribution(MeasuredSurfaceTemperature(20.0), "half"),)  # type: ignore[arg-type]
    ).current_mrt(20.0)
    bad_temperature = SurfaceCompositeRadiantModel(
        (SurfaceContribution(MeasuredSurfaceTemperature(300.0), 0.5),)
    ).current_mrt(20.0)
    assert isinstance(bad_factor, RadiantFailure)
    assert bad_factor.code is RadiantFailureCode.NON_NUMERIC
    assert isinstance(bad_temperature, RadiantFailure)
    assert bad_temperature.code is RadiantFailureCode.OUTSIDE_SOURCE_DOMAIN


def test_globe_rejects_nonnumeric_configuration_fields() -> None:
    for field in ("ambient_air_speed_m_s", "diameter_m", "emissivity"):
        arguments: dict[str, object] = {
            "globe_temperature_c": 22.0,
            "air_temperature_c": 20.0,
            "ambient_air_speed_m_s": 0.1,
            "diameter_m": 0.15,
            "emissivity": 0.95,
        }
        arguments[field] = "invalid"
        result = globe_mean_radiant_temperature(**arguments)  # type: ignore[arg-type]
        assert isinstance(result, RadiantFailure)
        assert result.code is RadiantFailureCode.NON_NUMERIC


def test_globe_rejects_nonpositive_fourth_root_radicand() -> None:
    result = globe_mean_radiant_temperature(
        globe_temperature_c=-273.14,
        air_temperature_c=200.0,
        ambient_air_speed_m_s=2.0,
        diameter_m=0.04,
        emissivity=0.8,
    )
    assert isinstance(result, RadiantFailure)
    assert result.code is RadiantFailureCode.NONPOSITIVE_RADICAND
