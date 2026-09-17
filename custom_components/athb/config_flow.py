"""Native, progressively disclosed configuration wizards for ATHB."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, override
from uuid import uuid4

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry, ConfigFlowResult
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector
from homeassistant.util import dt as dt_util

from .adapters.climate import capability_from_state
from .adapters.sources import (
    configured_freshness,
    snapshot_primary_temperature,
    snapshot_state,
    valid_value,
    validate_state_value,
)
from .config_schema import validate_environment, validate_options, validate_targets
from .const import (
    CONF_BOOST_MODE,
    CONF_COMFORT_STRATEGY,
    CONF_CONTROL_ENABLED,
    CONF_ECO_INTENSITY,
    CONF_MOLD_INDICATOR_ENTITY,
    CONF_OUTDOOR_SOURCE,
    CONF_PRIMARY_TEMPERATURE,
    CONF_RH_DECLARED,
    CONF_RH_ENTITY,
    CONF_RH_MODE,
    CONF_TARGETS,
    CONF_ZONE_UUID,
    DEFAULT_BOOST_DELTA_C,
    DEFAULT_BOOST_MODE,
    DEFAULT_ECO_INTENSITY,
    DEFAULT_FALLBACK_COOLING_C,
    DEFAULT_FALLBACK_HEATING_C,
    DEFAULT_MAXIMUM_CONTROL_TEMPERATURE,
    DEFAULT_MINIMUM_CONTROL_TEMPERATURE,
    DEFAULT_STRATEGY,
    DOMAIN,
)
from .core.climate import CapabilityMapping, ClimateFailure, resolve_capability
from .core.sources import SourceKind

ENTITY = selector.EntitySelector(selector.EntitySelectorConfig())
PRIMARY_TEMPERATURE = selector.EntitySelector(
    selector.EntitySelectorConfig(
        filter=[
            {"domain": "sensor", "device_class": "temperature"},
            {"domain": "sensor", "unit_of_measurement": ["°C", "°F", "K"]},
            {"domain": "climate"},
        ]
    )
)
CLIMATES = selector.EntitySelector(selector.EntitySelectorConfig(domain="climate", multiple=True))
MOLD_INDICATORS = selector.EntitySelector(
    selector.EntitySelectorConfig(domain="sensor", integration="mold_indicator")
)


def _select(options: tuple[str, ...], translation_key: str) -> selector.SelectSelector:
    return selector.SelectSelector(
        selector.SelectSelectorConfig(options=list(options), translation_key=translation_key)
    )


def _number(
    minimum: float,
    maximum: float,
    step: float,
    unit: str | None = None,
) -> selector.NumberSelector:
    config: selector.NumberSelectorConfig = {
        "min": minimum,
        "max": maximum,
        "step": step,
        "mode": selector.NumberSelectorMode.BOX,
    }
    if unit is not None:
        config["unit_of_measurement"] = unit
    return selector.NumberSelector(config)


OPTION_DEFAULTS: dict[str, object] = {
    CONF_COMFORT_STRATEGY: DEFAULT_STRATEGY,
    CONF_BOOST_MODE: DEFAULT_BOOST_MODE,
    CONF_ECO_INTENSITY: DEFAULT_ECO_INTENSITY,
    "radiant_model": "uniform",
    "met": 1.1,
    "clothing_mode": "automatic",
    "air_speed_mode": "fixed",
    "air_speed_m_s": 0.1,
    "lower_comfort_vote": -0.5,
    "upper_comfort_vote": 0.5,
    "running_mean_alpha": 0.8,
    "eco_heating_setback_c": 2.0,
    "eco_cooling_setback_c": 2.0,
    "boost_delta_c": DEFAULT_BOOST_DELTA_C,
    "boost_duration_minutes": 60.0,
    "manual_override_minutes": 120.0,
    "minimum_control_temperature": DEFAULT_MINIMUM_CONTROL_TEMPERATURE,
    "maximum_control_temperature": DEFAULT_MAXIMUM_CONTROL_TEMPERATURE,
    "minimum_range_gap": 1.0,
    "minimum_meaningful_change": 0.1,
    "feedback_resolution": 0.01,
    "primary_temperature_freshness_minutes": 30.0,
    "stale_heat_demand_margin_c": 0.5,
    "stale_heat_active_minutes": 30.0,
    "stale_heat_start_minutes": 60.0,
    "stale_heat_ramp_minutes_per_c": 10.0,
    "stale_heat_ramp_max_minutes": 30.0,
    "relative_humidity_freshness_minutes": 30.0,
    "local_temperature_freshness_minutes": 30.0,
    "radiant_freshness_minutes": 30.0,
    "air_speed_freshness_minutes": 30.0,
    "reject_extrapolation": False,
    "fallback_mode": "fixed",
    "fallback_heating_c": DEFAULT_FALLBACK_HEATING_C,
    "fallback_cooling_c": DEFAULT_FALLBACK_COOLING_C,
    "critical_locations": (),
}


def _required_entity(key: str, value: object | None) -> vol.Marker:
    return vol.Required(key, default=value) if value else vol.Required(key)


def _primary_source_error(hass: HomeAssistant, entity_id: str) -> str | None:
    """Reject available malformed sources without treating staleness as configuration failure."""

    if not entity_id.startswith(("sensor.", "climate.")):
        return "invalid_primary_source"
    state = hass.states.get(entity_id)
    if state is None or state.state in {"unknown", "unavailable"}:
        return None
    captured = snapshot_primary_temperature(
        state, climate_unit=str(hass.config.units.temperature_unit)
    )
    observation, _source_state = validate_state_value(
        captured,
        kind=SourceKind.PRIMARY_AIR,
        now=(captured.observed_at if captured is not None else None) or dt_util.utcnow(),
        freshness=None,
    )
    return None if valid_value(observation) is not None else "invalid_primary_source"


class _OptionsWizardMixin:
    """Shared option steps used by setup, reconfigure, and Options."""

    async_show_form: Callable[..., ConfigFlowResult]
    add_suggested_values_to_schema: Callable[[vol.Schema, Mapping[str, Any] | None], vol.Schema]
    _pending_options: dict[str, Any]
    _wizard_targets: list[dict[str, str]]
    _advanced: bool
    _critical_existing: list[dict[str, str]]
    _critical_locations: list[dict[str, str]]
    _critical_count: int
    _critical_index: int
    _calibration_index: int
    _wizard_environment: dict[str, Any]

    def _initialize_options_wizard(
        self,
        *,
        existing_options: Mapping[str, Any],
        targets: list[dict[str, str]],
        environment_data: Mapping[str, Any],
    ) -> None:
        self._wizard_targets = targets
        self._wizard_environment = dict(environment_data)
        compatible_existing = dict(existing_options)
        compatible_existing.pop("auto_mapping", None)
        compatible_existing.pop("inactive_heating_temperature", None)
        compatible_existing.pop("inactive_cooling_temperature", None)
        compatible_existing.pop("profile", None)
        if compatible_existing and CONF_ECO_INTENSITY not in compatible_existing:
            compatible_existing[CONF_ECO_INTENSITY] = "custom"
        if compatible_existing.get("radiant_model", "uniform") not in {
            "uniform",
            "mold_indicator",
        }:
            compatible_existing["radiant_model"] = "uniform"
        self._pending_options = {**OPTION_DEFAULTS, **compatible_existing}
        self._advanced = False
        self._critical_existing = [
            dict(item)
            for item in self._pending_options.get("critical_locations", ())
            if isinstance(item, dict)
        ]
        self._critical_locations = []
        self._critical_count = len(self._critical_existing)
        self._critical_index = 0
        self._calibration_index = 0

    async def _async_preferences(
        self, step_id: str, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        if user_input is not None:
            submitted = dict(user_input)
            self._advanced = bool(submitted.pop("advanced_settings", False))
            occupancy = submitted.pop("occupancy_entity", None)
            self._pending_options.update(submitted)
            self._pending_options.pop("inactive_heating_temperature", None)
            self._pending_options.pop("inactive_cooling_temperature", None)
            self._pending_options.pop("auto_mapping", None)
            self._pending_options.pop("occupancy_entity", None)
            if isinstance(occupancy, str) and (occupancy := occupancy.strip()):
                self._pending_options["occupancy_entity"] = occupancy
            else:
                for key in (
                    CONF_ECO_INTENSITY,
                    "eco_heating_setback_c",
                    "eco_cooling_setback_c",
                ):
                    self._pending_options.pop(key, None)
            self._clean_radiant_options(str(self._pending_options["radiant_model"]))
            if self._pending_options.get("occupancy_entity"):
                return await self.async_step_setback()
            return await self._after_setback()
        defaults = self._pending_options
        fields: dict[vol.Marker, object] = {
            vol.Required(CONF_COMFORT_STRATEGY, default=defaults[CONF_COMFORT_STRATEGY]): _select(
                ("eco", "efficient", "balanced", "comfort", "near_neutral"),
                "comfort_strategy",
            ),
            vol.Required("radiant_model", default=defaults["radiant_model"]): _select(
                ("uniform", "mold_indicator"), "radiant_model"
            ),
            vol.Optional("advanced_settings", default=False): bool,
        }
        occupancy = defaults.get("occupancy_entity")
        fields[
            vol.Optional("occupancy_entity", description={"suggested_value": occupancy})
            if occupancy
            else vol.Optional("occupancy_entity")
        ] = ENTITY
        return self.async_show_form(step_id=step_id, data_schema=vol.Schema(fields))

    async def async_step_setback(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure the setback that is applied only while unoccupied."""

        if user_input is not None:
            self._pending_options.update(user_input)
            if self._pending_options[CONF_ECO_INTENSITY] == "custom":
                return await self.async_step_setback_parameters()
            self._pending_options.pop("eco_heating_setback_c", None)
            self._pending_options.pop("eco_cooling_setback_c", None)
            return await self._after_setback()
        return self.async_show_form(
            step_id="setback",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_ECO_INTENSITY,
                        default=self._pending_options.get(
                            CONF_ECO_INTENSITY, DEFAULT_ECO_INTENSITY
                        ),
                    ): _select(("deep", "workday", "mild", "custom"), "eco_intensity")
                }
            ),
        )

    async def async_step_setback_parameters(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure custom heating and cooling setback offsets."""

        if user_input is not None:
            self._pending_options.update(user_input)
            return await self._after_setback()
        return self.async_show_form(
            step_id="setback_parameters",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "eco_heating_setback_c",
                        default=self._pending_options.get("eco_heating_setback_c", 2.0),
                    ): _number(0.0, 5.0, 0.1, "°C"),
                    vol.Required(
                        "eco_cooling_setback_c",
                        default=self._pending_options.get("eco_cooling_setback_c", 2.0),
                    ): _number(0.0, 5.0, 0.1, "°C"),
                }
            ),
        )

    async def _after_setback(self) -> ConfigFlowResult:
        if self._pending_options["radiant_model"] != "uniform":
            return await self.async_step_radiant()
        return await self.async_step_control_limits()

    def _clean_radiant_options(self, model: str) -> None:
        old_and_current_keys = {
            "mrt_entity",
            "globe_temperature_entity",
            "globe_diameter_m",
            "globe_emissivity",
            "surface_modelled",
            "surface_temperature_entity",
            "surface_f_rsi",
            "surface_view_factor",
            "surface_rh_threshold_pct",
            CONF_MOLD_INDICATOR_ENTITY,
        }
        retained = {CONF_MOLD_INDICATOR_ENTITY} if model == "mold_indicator" else set()
        for key in old_and_current_keys - retained:
            self._pending_options.pop(key, None)

    async def async_step_radiant(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._pending_options.update(user_input)
            return await self._after_radiant()
        defaults = self._pending_options
        schema = vol.Schema(
            {
                _required_entity(
                    CONF_MOLD_INDICATOR_ENTITY,
                    defaults.get(CONF_MOLD_INDICATOR_ENTITY),
                ): MOLD_INDICATORS
            }
        )
        return self.async_show_form(step_id="radiant", data_schema=schema)

    async def _after_radiant(self) -> ConfigFlowResult:
        return await self.async_step_control_limits()

    async def async_step_advanced_model(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._pending_options.update(user_input)
            if self._pending_options["clothing_mode"] == "automatic":
                self._pending_options.pop("fixed_clothing_clo", None)
            if self._pending_options["air_speed_mode"] == "fixed":
                self._pending_options.pop("air_speed_entity", None)
            else:
                self._pending_options.pop("air_speed_m_s", None)
            if self._pending_options["clothing_mode"] == "fixed":
                return await self.async_step_clothing()
            return await self.async_step_air_speed()
        defaults = self._pending_options
        return self.async_show_form(
            step_id="advanced_model",
            data_schema=vol.Schema(
                {
                    vol.Required("met", default=defaults.get("met", 1.1)): _number(
                        0.8, 2.0, 0.05, "met"
                    ),
                    vol.Required(
                        "clothing_mode", default=defaults.get("clothing_mode", "automatic")
                    ): _select(("automatic", "fixed"), "clothing_mode"),
                    vol.Required(
                        "air_speed_mode", default=defaults.get("air_speed_mode", "fixed")
                    ): _select(("fixed", "measured"), "air_speed_mode"),
                }
            ),
        )

    async def async_step_clothing(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._pending_options.update(user_input)
            return await self.async_step_air_speed()
        return self.async_show_form(
            step_id="clothing",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "fixed_clothing_clo",
                        default=self._pending_options.get("fixed_clothing_clo", 0.7),
                    ): _number(0.1, 2.0, 0.05, "clo")
                }
            ),
        )

    async def async_step_air_speed(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._pending_options.update(user_input)
            return await self.async_step_comfort_parameters()
        if self._pending_options["air_speed_mode"] == "measured":
            schema = vol.Schema(
                {
                    _required_entity(
                        "air_speed_entity", self._pending_options.get("air_speed_entity")
                    ): ENTITY
                }
            )
        else:
            schema = vol.Schema(
                {
                    vol.Required(
                        "air_speed_m_s", default=self._pending_options.get("air_speed_m_s", 0.1)
                    ): _number(0.0, 2.0, 0.05, "m/s")
                }
            )
        return self.async_show_form(step_id="air_speed", data_schema=schema)

    async def async_step_comfort_parameters(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            pending = {**self._pending_options, **user_input}
            errors = validate_options(pending)
            relevant = {
                key: value
                for key, value in errors.items()
                if key in {"lower_comfort_vote", "upper_comfort_vote", "running_mean_alpha"}
            }
            if not relevant:
                self._pending_options = pending
                return await self.async_step_command_behavior()
            return self.async_show_form(
                step_id="comfort_parameters",
                data_schema=self._comfort_parameters_schema(),
                errors=relevant,
            )
        return self.async_show_form(
            step_id="comfort_parameters", data_schema=self._comfort_parameters_schema()
        )

    def _comfort_parameters_schema(self) -> vol.Schema:
        defaults = self._pending_options
        return vol.Schema(
            {
                vol.Required(
                    "lower_comfort_vote", default=defaults.get("lower_comfort_vote", -0.5)
                ): _number(-1.0, -0.05, 0.05),
                vol.Required(
                    "upper_comfort_vote", default=defaults.get("upper_comfort_vote", 0.5)
                ): _number(0.05, 1.0, 0.05),
                vol.Required(
                    "running_mean_alpha", default=defaults.get("running_mean_alpha", 0.8)
                ): _number(0.6, 0.9, 0.01),
                vol.Required(
                    "reject_extrapolation", default=defaults.get("reject_extrapolation", False)
                ): bool,
            }
        )

    async def async_step_control_limits(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            pending = {**self._pending_options, **user_input}
            errors = validate_options(pending)
            control_errors = {
                "control_bounds",
                "manual_override_minutes",
                "boost_delta_c",
                "boost_duration_minutes",
            } & errors.keys()
            if not control_errors:
                minimum = float(pending["minimum_control_temperature"])
                maximum = float(pending["maximum_control_temperature"])
                if pending.get("fallback_mode", "fixed") == "fixed":
                    fallback_heating = float(pending.get("fallback_heating_c", minimum))
                    fallback_cooling = float(pending.get("fallback_cooling_c", maximum))
                    if not minimum <= fallback_heating < maximum:
                        pending["fallback_heating_c"] = minimum
                    if not minimum < fallback_cooling <= maximum:
                        pending["fallback_cooling_c"] = maximum
                self._pending_options = pending
                return (
                    await self.async_step_advanced_model()
                    if self._advanced
                    else await self._async_complete_wizard()
                )
            return self.async_show_form(
                step_id="control_limits",
                data_schema=self._control_limits_schema(),
                errors={
                    "base": (
                        "invalid_control_bounds"
                        if "control_bounds" in control_errors
                        else "invalid_option"
                    )
                },
            )
        return self.async_show_form(
            step_id="control_limits", data_schema=self._control_limits_schema()
        )

    def _control_limits_schema(self) -> vol.Schema:
        defaults = self._pending_options
        return vol.Schema(
            {
                vol.Required(
                    "minimum_control_temperature",
                    default=defaults.get(
                        "minimum_control_temperature", DEFAULT_MINIMUM_CONTROL_TEMPERATURE
                    ),
                ): _number(5.0, 35.0, 0.5, "°C"),
                vol.Required(
                    "maximum_control_temperature",
                    default=defaults.get(
                        "maximum_control_temperature", DEFAULT_MAXIMUM_CONTROL_TEMPERATURE
                    ),
                ): _number(5.0, 35.0, 0.5, "°C"),
                vol.Required(
                    "manual_override_minutes",
                    default=defaults.get("manual_override_minutes", 120.0),
                ): _number(15.0, 1440.0, 15.0, "min"),
                vol.Required(
                    "boost_delta_c", default=defaults.get("boost_delta_c", DEFAULT_BOOST_DELTA_C)
                ): _number(0.0, 3.0, 0.1, "°C"),
                vol.Required(
                    "boost_duration_minutes",
                    default=defaults.get("boost_duration_minutes", 60.0),
                ): _number(5.0, 180.0, 5.0, "min"),
            }
        )

    async def async_step_command_behavior(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._pending_options.update(user_input)
            return await self.async_step_fallback_temperatures()
        defaults = self._pending_options
        return self.async_show_form(
            step_id="command_behavior",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "minimum_range_gap", default=defaults.get("minimum_range_gap", 1.0)
                    ): _number(1.0, 10.0, 0.1, "°C"),
                    vol.Required(
                        "minimum_meaningful_change",
                        default=defaults.get("minimum_meaningful_change", 0.1),
                    ): _number(0.0, 5.0, 0.05, "°C"),
                    vol.Required(
                        "feedback_resolution", default=defaults.get("feedback_resolution", 0.01)
                    ): _number(0.0, 5.0, 0.01, "°C"),
                    vol.Required(
                        "fallback_mode", default=defaults.get("fallback_mode", "fixed")
                    ): _select(("fixed", "no_write"), "fallback_mode"),
                }
            ),
        )

    async def async_step_fallback_temperatures(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            pending = {**self._pending_options, **user_input}
            errors = validate_options(pending)
            relevant = {
                key: value
                for key, value in errors.items()
                if key in {"fallback_heating_c", "fallback_cooling_c"}
            }
            if not relevant:
                self._pending_options = pending
                return await self.async_step_critical_locations()
            return self.async_show_form(
                step_id="fallback_temperatures",
                data_schema=self._fallback_schema(),
                errors=relevant,
            )
        return self.async_show_form(
            step_id="fallback_temperatures", data_schema=self._fallback_schema()
        )

    def _fallback_schema(self) -> vol.Schema:
        defaults = self._pending_options
        return vol.Schema(
            {
                vol.Required(
                    "fallback_heating_c",
                    default=defaults.get("fallback_heating_c", DEFAULT_FALLBACK_HEATING_C),
                ): _number(5.0, 35.0, 0.5, "°C"),
                vol.Required(
                    "fallback_cooling_c",
                    default=defaults.get("fallback_cooling_c", DEFAULT_FALLBACK_COOLING_C),
                ): _number(5.0, 35.0, 0.5, "°C"),
            }
        )

    async def async_step_critical_locations(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._critical_count = int(user_input["critical_location_count"])
            self._critical_locations = []
            self._critical_index = 0
            if self._critical_count:
                return await self.async_step_critical_location()
            self._pending_options["critical_locations"] = []
            return await self.async_step_source_freshness()
        return self.async_show_form(
            step_id="critical_locations",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "critical_location_count", default=len(self._critical_existing)
                    ): _number(0.0, 8.0, 1.0)
                }
            ),
        )

    async def async_step_critical_location(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            item = {
                "location_id": str(user_input["location_id"]).strip(),
                "entity_id": str(user_input["location_entity"]),
                "mode": str(user_input["location_mode"]),
            }
            candidate = (*self._critical_locations, item)
            duplicate = len({entry["location_id"] for entry in candidate}) != len(candidate)
            if not item["location_id"] or duplicate:
                return self.async_show_form(
                    step_id="critical_location",
                    data_schema=self._critical_location_schema(),
                    errors={"location_id": "duplicate_location"},
                    description_placeholders=self._critical_placeholders(),
                )
            self._critical_locations.append(item)
            self._critical_index += 1
            if self._critical_index < self._critical_count:
                return await self.async_step_critical_location()
            self._pending_options["critical_locations"] = self._critical_locations
            return await self.async_step_source_freshness()
        return self.async_show_form(
            step_id="critical_location",
            data_schema=self._critical_location_schema(),
            description_placeholders=self._critical_placeholders(),
        )

    def _critical_location_schema(self) -> vol.Schema:
        existing = (
            self._critical_existing[self._critical_index]
            if self._critical_index < len(self._critical_existing)
            else {}
        )
        fields: dict[vol.Marker, object] = {
            vol.Required("location_id", default=existing.get("location_id", "")): str,
            vol.Required("location_mode", default=existing.get("mode", "monitoring")): _select(
                ("monitoring", "heating", "cooling", "both"), "critical_location_mode"
            ),
            _required_entity("location_entity", existing.get("entity_id")): ENTITY,
        }
        return vol.Schema(fields)

    def _critical_placeholders(self) -> dict[str, str]:
        return {"number": str(self._critical_index + 1), "total": str(self._critical_count)}

    async def async_step_source_freshness(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure only freshness windows used by selected measured sources."""

        if user_input is not None:
            pending = {**self._pending_options, **user_input}
            errors = validate_options(pending)
            relevant = {key: value for key, value in errors.items() if key in user_input}
            if not relevant:
                self._pending_options = pending
                return (
                    await self.async_step_stale_heat_safety()
                    if self._advanced
                    else await self._begin_target_calibrations()
                )
            return self.async_show_form(
                step_id="source_freshness",
                data_schema=self._source_freshness_schema(),
                errors=relevant,
            )
        return self.async_show_form(
            step_id="source_freshness", data_schema=self._source_freshness_schema()
        )

    def _source_freshness_schema(self) -> vol.Schema:
        defaults = self._pending_options
        fields: dict[vol.Marker, object] = {
            vol.Required(
                "primary_temperature_freshness_minutes",
                default=defaults.get("primary_temperature_freshness_minutes", 30.0),
            ): _number(5.0, 360.0, 5.0, "min")
        }
        if self._wizard_environment.get(CONF_RH_MODE) == "measured":
            fields[
                vol.Required(
                    "relative_humidity_freshness_minutes",
                    default=defaults.get("relative_humidity_freshness_minutes", 30.0),
                )
            ] = _number(5.0, 360.0, 5.0, "min")
        if self._critical_count:
            fields[
                vol.Required(
                    "local_temperature_freshness_minutes",
                    default=defaults.get("local_temperature_freshness_minutes", 30.0),
                )
            ] = _number(5.0, 360.0, 5.0, "min")
        radiant_model = str(defaults.get("radiant_model", "uniform"))
        measured_radiant = radiant_model in {"direct_mrt", "globe", "mold_indicator"} or (
            radiant_model == "surface" and not defaults.get("surface_modelled", False)
        )
        if measured_radiant:
            fields[
                vol.Required(
                    "radiant_freshness_minutes",
                    default=defaults.get("radiant_freshness_minutes", 30.0),
                )
            ] = _number(5.0, 360.0, 5.0, "min")
        if defaults.get("air_speed_mode", "fixed") == "measured":
            fields[
                vol.Required(
                    "air_speed_freshness_minutes",
                    default=defaults.get("air_speed_freshness_minutes", 30.0),
                )
            ] = _number(5.0, 360.0, 5.0, "min")
        return vol.Schema(fields)

    async def async_step_stale_heat_safety(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Expose the heat feedback guard only in advanced settings."""

        names = (
            "stale_heat_demand_margin_c",
            "stale_heat_active_minutes",
            "stale_heat_start_minutes",
            "stale_heat_ramp_minutes_per_c",
            "stale_heat_ramp_max_minutes",
        )
        if user_input is not None:
            pending = {**self._pending_options, **user_input}
            errors = validate_options(pending)
            relevant = {key: value for key, value in errors.items() if key in names}
            if not relevant:
                self._pending_options = pending
                return await self._begin_target_calibrations()
            return self.async_show_form(
                step_id="stale_heat_safety",
                data_schema=self._stale_heat_safety_schema(),
                errors=relevant,
            )
        return self.async_show_form(
            step_id="stale_heat_safety", data_schema=self._stale_heat_safety_schema()
        )

    def _stale_heat_safety_schema(self) -> vol.Schema:
        defaults = self._pending_options
        fields = {
            "stale_heat_demand_margin_c": (0.1, 2.0, 0.1, "°C"),
            "stale_heat_active_minutes": (15.0, 60.0, 1.0, "min"),
            "stale_heat_start_minutes": (30.0, 60.0, 1.0, "min"),
            "stale_heat_ramp_minutes_per_c": (5.0, 15.0, 1.0, "min/°C"),
            "stale_heat_ramp_max_minutes": (15.0, 60.0, 1.0, "min"),
        }
        return vol.Schema(
            {
                vol.Required(name, default=defaults.get(name, OPTION_DEFAULTS[name])): _number(
                    minimum, maximum, step, unit
                )
                for name, (minimum, maximum, step, unit) in fields.items()
            }
        )

    async def _begin_target_calibrations(self) -> ConfigFlowResult:
        self._calibration_index = 0
        valid_keys = {f"calibration_{target['target_uuid']}" for target in self._wizard_targets}
        for key in tuple(self._pending_options):
            if key.startswith("calibration_") and key not in valid_keys:
                self._pending_options.pop(key)
        if not self._wizard_targets:
            return await self._async_complete_wizard()
        return await self.async_step_target_calibration()

    async def async_step_target_calibration(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        target = self._wizard_targets[self._calibration_index]
        key = f"calibration_{target['target_uuid']}"
        if user_input is not None:
            self._pending_options[key] = user_input["calibration_offset_c"]
            self._calibration_index += 1
            if self._calibration_index >= len(self._wizard_targets):
                return await self._async_complete_wizard()
            return await self.async_step_target_calibration()
        return self.async_show_form(
            step_id="target_calibration",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "calibration_offset_c", default=self._pending_options.get(key, 0.0)
                    ): _number(-3.0, 3.0, 0.1, "°C")
                }
            ),
            description_placeholders={
                "target": target["entity_id"],
                "number": str(self._calibration_index + 1),
                "total": str(len(self._wizard_targets)),
            },
        )

    async def _async_complete_wizard(self) -> ConfigFlowResult:
        raise NotImplementedError


class AthbConfigFlow(_OptionsWizardMixin, config_entries.ConfigFlow, domain=DOMAIN):
    """Create and fully reconfigure one thermal-zone device."""

    VERSION = 2

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._is_reconfigure = False
        self._pending_options = {}
        self._wizard_targets = []
        self._advanced = False
        self._critical_existing = []
        self._critical_locations = []
        self._critical_count = 0
        self._critical_index = 0
        self._calibration_index = 0
        self._wizard_environment = {}

    @override
    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._data = {CONF_NAME: user_input[CONF_NAME], CONF_ZONE_UUID: str(uuid4())}
            return await self.async_step_environment()
        return self.async_show_form(
            step_id="user", data_schema=vol.Schema({vol.Required(CONF_NAME): str})
        )

    async def async_step_environment(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            if error := _primary_source_error(
                self.hass, str(user_input.get(CONF_PRIMARY_TEMPERATURE, ""))
            ):
                return self.async_show_form(
                    step_id="environment",
                    data_schema=self._environment_schema(),
                    errors={CONF_PRIMARY_TEMPERATURE: error},
                )
            self._data.update(user_input)
            stale_key = (
                CONF_RH_DECLARED if self._data[CONF_RH_MODE] == "measured" else CONF_RH_ENTITY
            )
            self._data.pop(stale_key, None)
            return await self.async_step_humidity()
        schema = self._environment_schema()
        if self._is_reconfigure:
            schema = self.add_suggested_values_to_schema(schema, self._data)
        return self.async_show_form(step_id="environment", data_schema=schema)

    def _environment_schema(self) -> vol.Schema:
        defaults = self._data
        return vol.Schema(
            {
                _required_entity(
                    CONF_PRIMARY_TEMPERATURE, defaults.get(CONF_PRIMARY_TEMPERATURE)
                ): PRIMARY_TEMPERATURE,
                vol.Required(CONF_RH_MODE, default=defaults.get(CONF_RH_MODE, "measured")): _select(
                    ("measured", "declared"), "rh_mode"
                ),
                _required_entity(CONF_OUTDOOR_SOURCE, defaults.get(CONF_OUTDOOR_SOURCE)): ENTITY,
            }
        )

    async def async_step_humidity(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        mode = str(self._data[CONF_RH_MODE])
        errors: dict[str, str] = {}
        if user_input is not None:
            environment = {**self._data, **user_input}
            errors = validate_environment(environment)
            if not errors:
                self._data.update(user_input)
                stale_key = CONF_RH_DECLARED if mode == "measured" else CONF_RH_ENTITY
                self._data.pop(stale_key, None)
                return await self.async_step_targets()
        if mode == "measured":
            schema = vol.Schema(
                {_required_entity(CONF_RH_ENTITY, self._data.get(CONF_RH_ENTITY)): ENTITY}
            )
        else:
            schema = vol.Schema(
                {
                    vol.Required(
                        CONF_RH_DECLARED, default=self._data.get(CONF_RH_DECLARED, 50.0)
                    ): _number(0.0, 100.0, 1.0, "%")
                }
            )
        if self._is_reconfigure:
            schema = self.add_suggested_values_to_schema(schema, self._data)
        return self.async_show_form(step_id="humidity", data_schema=schema, errors=errors)

    async def async_step_targets(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors, targets = self._resolve_targets(user_input.get(CONF_TARGETS))
            if not errors:
                self._data[CONF_TARGETS] = targets
                existing = self._get_reconfigure_entry().options if self._is_reconfigure else {}
                self._initialize_options_wizard(
                    existing_options=existing,
                    targets=targets,
                    environment_data=self._data,
                )
                return await self.async_step_preferences()
        defaults = [target["entity_id"] for target in self._data.get(CONF_TARGETS, ())]
        marker = (
            vol.Required(CONF_TARGETS, default=defaults) if defaults else vol.Required(CONF_TARGETS)
        )
        schema = vol.Schema({marker: CLIMATES})
        if self._is_reconfigure:
            schema = self.add_suggested_values_to_schema(schema, {CONF_TARGETS: defaults})
        return self.async_show_form(step_id="targets", data_schema=schema, errors=errors)

    def _resolve_targets(self, raw_targets: object) -> tuple[dict[str, str], list[dict[str, str]]]:
        try:
            entity_ids = validate_targets(raw_targets)
        except ValueError:
            return {CONF_TARGETS: "invalid_targets"}, []
        registry = er.async_get(self.hass)
        prior = {
            target["registry_identity"]: target["target_uuid"]
            for target in self._data.get(CONF_TARGETS, ())
            if isinstance(target, dict)
        }
        targets: list[dict[str, str]] = []
        excluding = self._get_reconfigure_entry().entry_id if self._is_reconfigure else None
        for entity_id in entity_ids:
            registry_entry = registry.async_get(entity_id)
            if registry_entry is None:
                return {CONF_TARGETS: "target_not_registered"}, []
            if self._target_is_claimed(registry_entry.id, excluding=excluding):
                return {CONF_TARGETS: "target_already_controlled"}, []
            targets.append(
                {
                    "target_uuid": prior.get(registry_entry.id, str(uuid4())),
                    "entity_id": entity_id,
                    "registry_identity": registry_entry.id,
                }
            )
        return {}, targets

    async def async_step_preferences(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self._async_preferences("preferences", user_input)

    async def _async_complete_wizard(self) -> ConfigFlowResult:
        if validate_options(self._pending_options):
            return self.async_show_form(
                step_id="preferences",
                data_schema=vol.Schema({}),
                errors={"base": "invalid_option"},
            )
        return await self.async_step_review()

    async def async_step_review(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is None:
            primary_value, primary_unit, primary_age, primary_status = self._primary_preview()
            rh_preview = self._relative_humidity_preview()
            outdoor_preview = self._source_preview(
                str(self._data.get(CONF_OUTDOOR_SOURCE, "")), SourceKind.OUTDOOR
            )
            blocking_sources = (
                ", ".join(
                    name
                    for name, status in (
                        ("primary", primary_status),
                        ("humidity", rh_preview[3]),
                        ("outdoor", outdoor_preview[3]),
                    )
                    if status != "valid"
                )
                or "none"
            )
            return self.async_show_form(
                step_id="review",
                data_schema=vol.Schema({}),
                description_placeholders={
                    "zone": str(self._data[CONF_NAME]),
                    "primary": str(self._data[CONF_PRIMARY_TEMPERATURE]),
                    "outdoor": str(self._data[CONF_OUTDOOR_SOURCE]),
                    "target_count": str(len(self._data[CONF_TARGETS])),
                    "strategy": str(self._pending_options[CONF_COMFORT_STRATEGY]),
                    "primary_value": primary_value,
                    "primary_unit": primary_unit,
                    "primary_age": primary_age,
                    "primary_status": primary_status,
                    "humidity_preview": self._format_preview(rh_preview),
                    "outdoor_preview": self._format_preview(outdoor_preview),
                    "blocking_sources": blocking_sources,
                    "occupancy": self._occupancy_preview(),
                    "target_capabilities": self._target_capability_preview(),
                },
            )
        return await self._async_commit_config()

    def _primary_preview(self) -> tuple[str, str, str, str]:
        entity_id = str(self._data.get(CONF_PRIMARY_TEMPERATURE, ""))
        state = self.hass.states.get(entity_id)
        registry_entry = er.async_get(self.hass).async_get(entity_id)
        captured = snapshot_primary_temperature(
            state,
            climate_unit=str(self.hass.config.units.temperature_unit),
            registry_identity=(registry_entry.id if registry_entry is not None else None),
        )
        now = dt_util.utcnow()
        observation, _source_state = validate_state_value(
            captured,
            kind=SourceKind.PRIMARY_AIR,
            now=now,
            freshness=configured_freshness(self._pending_options, SourceKind.PRIMARY_AIR),
        )
        value = valid_value(observation)
        age = (
            max(0.0, (now - observation.observed_at).total_seconds() / 60.0)
            if observation.observed_at is not None
            else None
        )
        return (
            "—" if value is None else f"{value:.2f}",
            observation.unit or "—",
            "—" if age is None else f"{age:.1f} min",
            observation.validity.value,
        )

    def _source_preview(self, entity_id: str, kind: SourceKind) -> tuple[str, str, str, str]:
        state = self.hass.states.get(entity_id)
        registry_entry = er.async_get(self.hass).async_get(entity_id)
        captured = snapshot_state(
            state,
            registry_identity=(registry_entry.id if registry_entry is not None else None),
        )
        now = dt_util.utcnow()
        observation, _source_state = validate_state_value(
            captured,
            kind=kind,
            now=now,
            freshness=configured_freshness(self._pending_options, kind),
        )
        value = valid_value(observation)
        age = (
            max(0.0, (now - observation.observed_at).total_seconds() / 60.0)
            if observation.observed_at is not None
            else None
        )
        return (
            "—" if value is None else f"{value:.2f}",
            observation.unit or "—",
            "—" if age is None else f"{age:.1f} min",
            observation.validity.value,
        )

    def _relative_humidity_preview(self) -> tuple[str, str, str, str]:
        if self._data.get(CONF_RH_MODE) == "declared":
            return (f"{float(self._data[CONF_RH_DECLARED]):.2f}", "%", "declared", "valid")
        return self._source_preview(
            str(self._data.get(CONF_RH_ENTITY, "")), SourceKind.RELATIVE_HUMIDITY
        )

    @staticmethod
    def _format_preview(preview: tuple[str, str, str, str]) -> str:
        value, unit, age, status = preview
        return f"{value} {unit}, age {age}, status {status}"

    def _occupancy_preview(self) -> str:
        entity_id = str(self._pending_options.get("occupancy_entity", ""))
        state = self.hass.states.get(entity_id) if entity_id else None
        return (
            "not configured" if not entity_id else state.state if state is not None else "missing"
        )

    def _target_capability_preview(self) -> str:
        previews = []
        for target in self._data.get(CONF_TARGETS, ()):
            entity_id = str(target["entity_id"])
            capability = capability_from_state(self.hass.states.get(entity_id))
            mapping = resolve_capability(capability)
            if isinstance(mapping, CapabilityMapping):
                outcome = f"{mapping.direction.value}/{mapping.shape.value}"
            else:
                assert isinstance(mapping, ClimateFailure)
                outcome = mapping.reason
            previews.append(f"{entity_id}: {outcome}")
        return "; ".join(previews) or "none"

    async def _async_commit_config(self) -> ConfigFlowResult:
        if self._is_reconfigure:
            entry = self._get_reconfigure_entry()
            title = str(self._data.pop(CONF_NAME))
            updated = {**entry.data, **self._data}
            stale_rh_key = (
                CONF_RH_DECLARED if updated[CONF_RH_MODE] == "measured" else CONF_RH_ENTITY
            )
            updated.pop(stale_rh_key, None)
            await self.async_set_unique_id(str(entry.data[CONF_ZONE_UUID]))
            self._abort_if_unique_id_mismatch()
            return self.async_update_reload_and_abort(
                entry, title=title, data=updated, options=self._pending_options
            )
        self._pending_options[CONF_CONTROL_ENABLED] = False
        title = str(self._data.pop(CONF_NAME))
        await self.async_set_unique_id(str(self._data[CONF_ZONE_UUID]))
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=title, data=self._data, options=self._pending_options)

    @staticmethod
    @callback
    @override
    def async_get_options_flow(config_entry: ConfigEntry) -> config_entries.OptionsFlow:
        return AthbOptionsFlow(config_entry)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        self._is_reconfigure = True
        if not self._data:
            self._data = {**entry.data, CONF_NAME: entry.title}
        if user_input is not None:
            self._data[CONF_NAME] = user_input[CONF_NAME]
            return await self.async_step_environment()
        schema = vol.Schema({vol.Required(CONF_NAME, default=self._data[CONF_NAME]): str})
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(schema, self._data),
        )

    def _target_is_claimed(self, registry_identity: str, *, excluding: str | None = None) -> bool:
        for existing in self._async_current_entries():
            if existing.entry_id == excluding or not existing.options.get(
                CONF_CONTROL_ENABLED, False
            ):
                continue
            if any(
                target.get("registry_identity") == registry_identity
                for target in existing.data.get(CONF_TARGETS, ())
            ):
                return True
        return False


class AthbOptionsFlow(_OptionsWizardMixin, config_entries.OptionsFlowWithReload):
    """Present all zone settings behind the standard Configure action."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._entry = config_entry
        self._pending_data = {**config_entry.data, CONF_NAME: config_entry.title}
        self._pending_options = {}
        self._wizard_targets = list(config_entry.data.get(CONF_TARGETS, ()))
        self._advanced = False
        self._critical_existing = []
        self._critical_locations = []
        self._critical_count = 0
        self._critical_index = 0
        self._calibration_index = 0
        self._wizard_environment = {}
        self._initialize_options_wizard(
            existing_options=config_entry.options,
            targets=self._wizard_targets,
            environment_data=config_entry.data,
        )

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show a short settings menu instead of one long technical form."""

        return self.async_show_menu(
            step_id="init",
            menu_options=("sources", "target_entities", "comfort"),
        )

    async def async_step_sources(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit the zone name and environmental source identities."""

        if user_input is not None:
            if error := _primary_source_error(
                self.hass, str(user_input.get(CONF_PRIMARY_TEMPERATURE, ""))
            ):
                return self.async_show_form(
                    step_id="sources",
                    data_schema=self._sources_schema(),
                    errors={CONF_PRIMARY_TEMPERATURE: error},
                )
            self._pending_data.update(user_input)
            stale_key = (
                CONF_RH_DECLARED
                if self._pending_data[CONF_RH_MODE] == "measured"
                else CONF_RH_ENTITY
            )
            self._pending_data.pop(stale_key, None)
            return await self.async_step_source_humidity()
        return self.async_show_form(step_id="sources", data_schema=self._sources_schema())

    def _sources_schema(self) -> vol.Schema:
        defaults = self._pending_data
        return vol.Schema(
            {
                vol.Required(CONF_NAME, default=defaults[CONF_NAME]): str,
                _required_entity(
                    CONF_PRIMARY_TEMPERATURE,
                    defaults.get(CONF_PRIMARY_TEMPERATURE),
                ): PRIMARY_TEMPERATURE,
                vol.Required(
                    CONF_RH_MODE,
                    default=defaults.get(CONF_RH_MODE, "measured"),
                ): _select(("measured", "declared"), "rh_mode"),
                _required_entity(
                    CONF_OUTDOOR_SOURCE,
                    defaults.get(CONF_OUTDOOR_SOURCE),
                ): ENTITY,
            }
        )

    async def async_step_source_humidity(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect only the humidity input selected on the source page."""

        mode = str(self._pending_data[CONF_RH_MODE])
        errors: dict[str, str] = {}
        if user_input is not None:
            environment = {**self._pending_data, **user_input}
            errors = validate_environment(environment)
            if not errors:
                self._pending_data.update(user_input)
                stale_key = CONF_RH_DECLARED if mode == "measured" else CONF_RH_ENTITY
                self._pending_data.pop(stale_key, None)
                return self._save_data_settings()
        if mode == "measured":
            schema = vol.Schema(
                {
                    _required_entity(
                        CONF_RH_ENTITY,
                        self._pending_data.get(CONF_RH_ENTITY),
                    ): ENTITY
                }
            )
        else:
            schema = vol.Schema(
                {
                    vol.Required(
                        CONF_RH_DECLARED,
                        default=self._pending_data.get(CONF_RH_DECLARED, 50.0),
                    ): _number(0.0, 100.0, 1.0, "%")
                }
            )
        return self.async_show_form(
            step_id="source_humidity",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_target_entities(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit climate targets while preserving their stable identities."""

        errors: dict[str, str] = {}
        if user_input is not None:
            errors, targets = self._resolve_targets(user_input.get(CONF_TARGETS))
            if not errors:
                self._pending_data[CONF_TARGETS] = targets
                self._wizard_targets = targets
                valid_calibrations = {f"calibration_{target['target_uuid']}" for target in targets}
                for key in tuple(self._pending_options):
                    if key.startswith("calibration_") and key not in valid_calibrations:
                        self._pending_options.pop(key)
                return self._save_data_settings(options=self._pending_options)
        defaults = [
            target["entity_id"]
            for target in self._pending_data.get(CONF_TARGETS, ())
            if isinstance(target, dict) and isinstance(target.get("entity_id"), str)
        ]
        marker = vol.Required(CONF_TARGETS, default=defaults)
        return self.async_show_form(
            step_id="target_entities",
            data_schema=vol.Schema({marker: CLIMATES}),
            errors=errors,
        )

    def _resolve_targets(self, raw_targets: object) -> tuple[dict[str, str], list[dict[str, str]]]:
        try:
            entity_ids = validate_targets(raw_targets)
        except ValueError:
            return {CONF_TARGETS: "invalid_targets"}, []
        registry = er.async_get(self.hass)
        prior = {
            target["registry_identity"]: target["target_uuid"]
            for target in self._pending_data.get(CONF_TARGETS, ())
            if isinstance(target, dict)
        }
        targets: list[dict[str, str]] = []
        for entity_id in entity_ids:
            registry_entry = registry.async_get(entity_id)
            if registry_entry is None:
                return {CONF_TARGETS: "target_not_registered"}, []
            if self._target_is_claimed(registry_entry.id):
                return {CONF_TARGETS: "target_already_controlled"}, []
            targets.append(
                {
                    "target_uuid": prior.get(registry_entry.id, str(uuid4())),
                    "entity_id": entity_id,
                    "registry_identity": registry_entry.id,
                }
            )
        return {}, targets

    def _target_is_claimed(self, registry_identity: str) -> bool:
        for existing in self.hass.config_entries.async_entries(DOMAIN):
            if existing.entry_id == self._entry.entry_id or not existing.options.get(
                CONF_CONTROL_ENABLED, False
            ):
                continue
            if any(
                target.get("registry_identity") == registry_identity
                for target in existing.data.get(CONF_TARGETS, ())
            ):
                return True
        return False

    async def async_step_comfort(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit comfort, occupancy, limits, and optionally expert settings."""

        return await self._async_preferences("comfort", user_input)

    def _save_data_settings(self, *, options: Mapping[str, Any] | None = None) -> ConfigFlowResult:
        """Persist data edited from Options and reload once after completion."""

        updated = dict(self._pending_data)
        title = str(updated.pop(CONF_NAME))
        changed = self.hass.config_entries.async_update_entry(
            self._entry,
            title=title,
            data=updated,
        )
        if changed:
            self.hass.config_entries.async_schedule_reload(self._entry.entry_id)
        return self.async_create_entry(
            data=dict(self._entry.options if options is None else options)
        )

    async def _async_complete_wizard(self) -> ConfigFlowResult:
        if validate_options(self._pending_options):
            return self.async_show_form(
                step_id="comfort", data_schema=vol.Schema({}), errors={"base": "invalid_option"}
            )
        return self.async_create_entry(data=self._pending_options)
