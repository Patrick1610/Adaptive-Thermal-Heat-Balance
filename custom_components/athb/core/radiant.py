"""Pure radiant models with explicit current and inverse-candidate behavior."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

from .contracts import (
    DerivedMeanRadiantTemperature,
    MeasuredGlobeTemperature,
    MeasuredMeanRadiantTemperature,
    MeasuredSurfaceTemperature,
    ModelledSurfaceTemperature,
    Provenance,
    RadiantFailureCode,
)

ABSOLUTE_ZERO_OFFSET = 273.15
STEFAN_BOLTZMANN = 5.67e-8
MAX_SURFACE_CONTRIBUTIONS = 8


class RadiantMode(StrEnum):
    """Supported physical radiant modes."""

    UNIFORM = "uniform"
    DIRECT = "direct_mrt"
    GLOBE = "globe_derived_mrt"
    SURFACE_COMPOSITE = "surface_composite"


@dataclass(frozen=True, slots=True)
class RadiantFailure:
    """Typed radiant failure with no invented MRT value."""

    code: RadiantFailureCode
    field: str | None
    detail: str


@dataclass(frozen=True, slots=True)
class RadiantValue:
    """Effective MRT with its physical mode, provenance, and assumptions."""

    mrt_c: float
    mode: RadiantMode
    provenance: Provenance
    reasons: tuple[str, ...]


type RadiantResult = RadiantValue | RadiantFailure
type SurfaceTemperature = MeasuredSurfaceTemperature | ModelledSurfaceTemperature


@dataclass(frozen=True, slots=True)
class SurfaceContribution:
    """One fixed surface contribution for the occupant's radiant exposure."""

    source: SurfaceTemperature
    view_factor: float


def _number(value: object, field: str) -> float | RadiantFailure:
    if isinstance(value, bool):
        return RadiantFailure(RadiantFailureCode.BOOLEAN_INPUT, field, f"{field} is Boolean")
    if not isinstance(value, (int, float)):
        return RadiantFailure(RadiantFailureCode.NON_NUMERIC, field, f"{field} must be numeric")
    try:
        numeric = float(value)
    except OverflowError:
        return RadiantFailure(RadiantFailureCode.NON_FINITE, field, f"{field} must be finite")
    if not math.isfinite(numeric):
        return RadiantFailure(RadiantFailureCode.NON_FINITE, field, f"{field} must be finite")
    return numeric


def _temperature(value: object, field: str) -> float | RadiantFailure:
    temperature = _number(value, field)
    if isinstance(temperature, RadiantFailure):
        return temperature
    if not -273.15 < temperature <= 200.0:
        return RadiantFailure(
            RadiantFailureCode.OUTSIDE_SOURCE_DOMAIN,
            field,
            f"{field} must be above absolute zero and at most 200 degrees Celsius",
        )
    return temperature


@dataclass(frozen=True, slots=True)
class UniformRadiantModel:
    """Default estimate: MRT follows current or candidate local air."""

    mode: RadiantMode = RadiantMode.UNIFORM

    def current_mrt(self, air_temperature_c: float) -> RadiantResult:
        return self.candidate_mrt(air_temperature_c)

    def candidate_mrt(self, candidate_air_temperature_c: float) -> RadiantResult:
        temperature = _temperature(candidate_air_temperature_c, "air_temperature_c")
        if isinstance(temperature, RadiantFailure):
            return temperature
        return RadiantValue(
            temperature,
            self.mode,
            Provenance.ESTIMATED,
            ("estimated_uniform_radiant_environment",),
        )


@dataclass(frozen=True, slots=True)
class DirectRadiantModel:
    """Direct measured MRT held constant for one coherent snapshot."""

    source: MeasuredMeanRadiantTemperature
    mode: RadiantMode = RadiantMode.DIRECT

    def current_mrt(self, air_temperature_c: float) -> RadiantResult:
        del air_temperature_c
        temperature = _temperature(self.source.value_c, "direct_mrt_c")
        if isinstance(temperature, RadiantFailure):
            return temperature
        return RadiantValue(temperature, self.mode, self.source.provenance, ())

    def candidate_mrt(self, candidate_air_temperature_c: float) -> RadiantResult:
        del candidate_air_temperature_c
        return self.current_mrt(0.0)


def globe_mean_radiant_temperature(
    *,
    globe_temperature_c: float,
    air_temperature_c: float,
    ambient_air_speed_m_s: float,
    diameter_m: float = 0.15,
    emissivity: float = 0.95,
) -> DerivedMeanRadiantTemperature | RadiantFailure:
    """Derive current MRT using the explicit ISO 7726-style estimate."""

    globe = _temperature(globe_temperature_c, "globe_temperature_c")
    if isinstance(globe, RadiantFailure):
        return globe
    air = _temperature(air_temperature_c, "air_temperature_c")
    if isinstance(air, RadiantFailure):
        return air
    speed = _number(ambient_air_speed_m_s, "ambient_air_speed_m_s")
    if isinstance(speed, RadiantFailure):
        return speed
    diameter = _number(diameter_m, "diameter_m")
    if isinstance(diameter, RadiantFailure):
        return diameter
    emissivity_value = _number(emissivity, "emissivity")
    if isinstance(emissivity_value, RadiantFailure):
        return emissivity_value
    if not 0.0 <= speed <= 2.0:
        return RadiantFailure(
            RadiantFailureCode.OUTSIDE_SOURCE_DOMAIN,
            "ambient_air_speed_m_s",
            "ambient air speed must be within [0, 2] metres per second",
        )
    if not 0.04 <= diameter <= 0.15:
        return RadiantFailure(
            RadiantFailureCode.INVALID_GLOBE_CONFIGURATION,
            "diameter_m",
            "globe diameter must be within [0.04, 0.15] metres",
        )
    if not 0.8 <= emissivity_value <= 1.0:
        return RadiantFailure(
            RadiantFailureCode.INVALID_GLOBE_CONFIGURATION,
            "emissivity",
            "globe emissivity must be within [0.8, 1.0]",
        )

    natural = 1.4 * (abs(globe - air) / diameter) ** 0.25
    forced = 6.3 * speed**0.6 / diameter**0.4
    heat_transfer = max(natural, forced)
    radicand = (globe + ABSOLUTE_ZERO_OFFSET) ** 4 + heat_transfer * (globe - air) / (
        emissivity_value * STEFAN_BOLTZMANN
    )
    if not math.isfinite(radicand) or radicand <= 0.0:
        return RadiantFailure(
            RadiantFailureCode.NONPOSITIVE_RADICAND,
            None,
            "globe MRT fourth-root radicand must be finite and positive",
        )
    mrt = radicand**0.25 - ABSOLUTE_ZERO_OFFSET
    reasons = ["derived_globe_mrt", "assumes_no_direct_solar_exposure"]
    if diameter < 0.15:
        reasons.append("small_globe_measurement_quality")
    return DerivedMeanRadiantTemperature(mrt, tuple(reasons))


@dataclass(frozen=True, slots=True)
class GlobeRadiantModel:
    """Snapshot globe-derived MRT, held constant for candidate evaluation."""

    derived: DerivedMeanRadiantTemperature
    mode: RadiantMode = RadiantMode.GLOBE

    @classmethod
    def from_observations(
        cls,
        *,
        globe: MeasuredGlobeTemperature,
        air_temperature_c: float,
        ambient_air_speed_m_s: float,
        diameter_m: float = 0.15,
        emissivity: float = 0.95,
    ) -> GlobeRadiantModel | RadiantFailure:
        derived = globe_mean_radiant_temperature(
            globe_temperature_c=globe.value_c,
            air_temperature_c=air_temperature_c,
            ambient_air_speed_m_s=ambient_air_speed_m_s,
            diameter_m=diameter_m,
            emissivity=emissivity,
        )
        if isinstance(derived, RadiantFailure):
            return derived
        return cls(derived)

    def current_mrt(self, air_temperature_c: float) -> RadiantResult:
        del air_temperature_c
        return RadiantValue(
            self.derived.value_c, self.mode, self.derived.provenance, self.derived.reasons
        )

    def candidate_mrt(self, candidate_air_temperature_c: float) -> RadiantResult:
        del candidate_air_temperature_c
        return self.current_mrt(0.0)


@dataclass(frozen=True, slots=True)
class SurfaceCompositeRadiantModel:
    """Diffuse fourth-power combination with fixed surfaces and explicit background."""

    surfaces: tuple[SurfaceContribution, ...]
    fixed_background: MeasuredMeanRadiantTemperature | None = None
    mode: RadiantMode = RadiantMode.SURFACE_COMPOSITE

    def _calculate(self, background_air_temperature_c: float) -> RadiantResult:
        if len(self.surfaces) > MAX_SURFACE_CONTRIBUTIONS:
            return RadiantFailure(
                RadiantFailureCode.TOO_MANY_SURFACES,
                "surfaces",
                "at most eight surface contributions are supported",
            )
        factor_sum = 0.0
        weighted_fourth_power = 0.0
        reasons: list[str] = ["diffuse_surface_composite"]
        for index, contribution in enumerate(self.surfaces):
            factor = _number(contribution.view_factor, f"surfaces[{index}].view_factor")
            if isinstance(factor, RadiantFailure):
                return factor
            if not 0.0 < factor <= 1.0:
                return RadiantFailure(
                    RadiantFailureCode.INVALID_VIEW_FACTOR,
                    f"surfaces[{index}].view_factor",
                    "each effective view factor must be within (0, 1]",
                )
            if contribution.source.value_c is None:
                uniform = UniformRadiantModel().candidate_mrt(background_air_temperature_c)
                if isinstance(uniform, RadiantFailure):
                    return uniform
                return RadiantValue(
                    uniform.mrt_c,
                    RadiantMode.UNIFORM,
                    Provenance.ESTIMATED,
                    (
                        "estimated_uniform_radiant_environment",
                        "radiant_fallback_missing_surface_data",
                    ),
                )
            temperature = _temperature(
                contribution.source.value_c, f"surfaces[{index}].temperature_c"
            )
            if isinstance(temperature, RadiantFailure):
                return temperature
            factor_sum += factor
            if factor_sum > 1.0 + 1e-12:
                return RadiantFailure(
                    RadiantFailureCode.INVALID_VIEW_FACTOR,
                    "surfaces",
                    "sum of effective view factors must not exceed one",
                )
            weighted_fourth_power += factor * (temperature + ABSOLUTE_ZERO_OFFSET) ** 4
            if isinstance(contribution.source, ModelledSurfaceTemperature):
                reasons.append("modelled_surface")

        if self.fixed_background is None:
            background = _temperature(background_air_temperature_c, "background_air_temperature_c")
            background_provenance = Provenance.ESTIMATED
            reasons.append("candidate_air_background")
        else:
            background = _temperature(self.fixed_background.value_c, "background_mrt_c")
            background_provenance = self.fixed_background.provenance
            reasons.append("fixed_measured_background_mrt")
        if isinstance(background, RadiantFailure):
            return background
        weighted_fourth_power += (1.0 - factor_sum) * (background + ABSOLUTE_ZERO_OFFSET) ** 4
        if not math.isfinite(weighted_fourth_power) or weighted_fourth_power <= 0.0:
            return RadiantFailure(
                RadiantFailureCode.NONPOSITIVE_RADICAND,
                None,
                "surface-composite fourth-root radicand must be finite and positive",
            )
        mrt = weighted_fourth_power**0.25 - ABSOLUTE_ZERO_OFFSET
        provenance = (
            Provenance.MEASURED
            if background_provenance is Provenance.MEASURED
            and all(isinstance(item.source, MeasuredSurfaceTemperature) for item in self.surfaces)
            else Provenance.ESTIMATED
        )
        return RadiantValue(mrt, self.mode, provenance, tuple(dict.fromkeys(reasons)))

    def current_mrt(self, air_temperature_c: float) -> RadiantResult:
        return self._calculate(air_temperature_c)

    def candidate_mrt(self, candidate_air_temperature_c: float) -> RadiantResult:
        return self._calculate(candidate_air_temperature_c)
