"""Native, progressively disclosed configuration wizards for ATHB."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, override
from uuid import uuid4

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry, ConfigFlowResult
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector

from .config_schema import validate_environment, validate_options, validate_targets
from .const import (
    CONF_COMFORT_STRATEGY,
    CONF_CONTROL_ENABLED,
    CONF_OUTDOOR_SOURCE,
    CONF_PRIMARY_TEMPERATURE,
    CONF_PROFILE,
    CONF_RH_DECLARED,
    CONF_RH_ENTITY,
    CONF_RH_MODE,
    CONF_TARGETS,
    CONF_ZONE_UUID,
    DEFAULT_PROFILE,
    DEFAULT_STRATEGY,
    DOMAIN,
)

ENTITY = selector.EntitySelector(selector.EntitySelectorConfig())
CLIMATES = selector.EntitySelector(selector.EntitySelectorConfig(domain="climate", multiple=True))


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
    CONF_PROFILE: DEFAULT_PROFILE,
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
    "boost_delta_c": 1.0,
    "boost_duration_minutes": 60.0,
    "manual_override_minutes": 120.0,
    "minimum_control_temperature": 18.0,
    "maximum_control_temperature": 26.0,
    "minimum_range_gap": 1.0,
    "minimum_meaningful_change": 0.1,
    "feedback_resolution": 0.01,
    "reject_extrapolation": False,
    "auto_mapping": "unmapped",
    "fallback_mode": "fixed",
    "fallback_heating_c": 18.0,
    "fallback_cooling_c": 26.0,
    "critical_locations": (),
}


def _required_entity(key: str, value: object | None) -> vol.Marker:
    return vol.Required(key, default=value) if value else vol.Required(key)


class _OptionsWizardMixin:
    """Shared option steps used by setup, reconfigure, and Options."""

    async_show_form: Callable[..., ConfigFlowResult]
    _pending_options: dict[str, Any]
    _wizard_targets: list[dict[str, str]]
    _advanced: bool
    _critical_existing: list[dict[str, str]]
    _critical_locations: list[dict[str, str]]
    _critical_count: int
    _critical_index: int
    _calibration_index: int

    def _initialize_options_wizard(
        self,
        *,
        existing_options: Mapping[str, Any],
        targets: list[dict[str, str]],
    ) -> None:
        self._wizard_targets = targets
        self._pending_options = {**OPTION_DEFAULTS, **existing_options}
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
            self._pending_options.update(submitted)
            if "occupancy_entity" not in submitted:
                self._pending_options.pop("occupancy_entity", None)
            self._clean_radiant_options(str(self._pending_options["radiant_model"]))
            if self._pending_options["radiant_model"] != "uniform":
                return await self.async_step_radiant()
            if self._advanced:
                return await self.async_step_advanced_model()
            return await self._async_complete_wizard()
        defaults = self._pending_options
        fields: dict[vol.Marker, object] = {
            vol.Required(CONF_COMFORT_STRATEGY, default=defaults[CONF_COMFORT_STRATEGY]): _select(
                ("efficient", "balanced", "comfort"), "comfort_strategy"
            ),
            vol.Required(CONF_PROFILE, default=defaults[CONF_PROFILE]): _select(
                ("auto", "comfort", "eco", "boost"), "profile"
            ),
            vol.Required("radiant_model", default=defaults["radiant_model"]): _select(
                ("uniform", "direct_mrt", "globe", "surface"), "radiant_model"
            ),
            vol.Optional("advanced_settings", default=False): bool,
        }
        occupancy = defaults.get("occupancy_entity")
        fields[
            vol.Optional("occupancy_entity", default=occupancy)
            if occupancy
            else vol.Optional("occupancy_entity")
        ] = ENTITY
        return self.async_show_form(step_id=step_id, data_schema=vol.Schema(fields))

    def _clean_radiant_options(self, model: str) -> None:
        keys_by_model = {
            "uniform": set(),
            "direct_mrt": {"mrt_entity"},
            "globe": {"globe_temperature_entity", "globe_diameter_m", "globe_emissivity"},
            "surface": {
                "surface_modelled",
                "surface_temperature_entity",
                "surface_f_rsi",
                "surface_view_factor",
                "surface_rh_threshold_pct",
            },
        }
        all_keys = set().union(*keys_by_model.values())
        for key in all_keys - keys_by_model.get(model, set()):
            self._pending_options.pop(key, None)

    async def async_step_radiant(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        model = str(self._pending_options["radiant_model"])
        if user_input is not None:
            submitted = dict(user_input)
            if model == "surface":
                self._pending_options["surface_modelled"] = (
                    submitted["surface_source"] == "modelled"
                )
                return await self.async_step_surface_details()
            self._pending_options.update(submitted)
            return await self._after_radiant()
        defaults = self._pending_options
        if model == "direct_mrt":
            schema = vol.Schema(
                {_required_entity("mrt_entity", defaults.get("mrt_entity")): ENTITY}
            )
        elif model == "globe":
            schema = vol.Schema(
                {
                    _required_entity(
                        "globe_temperature_entity", defaults.get("globe_temperature_entity")
                    ): ENTITY,
                    vol.Required(
                        "globe_diameter_m", default=defaults.get("globe_diameter_m", 0.15)
                    ): _number(0.01, 1.0, 0.01, "m"),
                    vol.Required(
                        "globe_emissivity", default=defaults.get("globe_emissivity", 0.95)
                    ): _number(0.01, 1.0, 0.01),
                }
            )
        else:
            source = "modelled" if defaults.get("surface_modelled", False) else "measured"
            schema = vol.Schema(
                {
                    vol.Required("surface_source", default=source): _select(
                        ("measured", "modelled"), "surface_source"
                    )
                }
            )
        return self.async_show_form(step_id="radiant", data_schema=schema)

    async def _after_radiant(self) -> ConfigFlowResult:
        return (
            await self.async_step_advanced_model()
            if self._advanced
            else await self._async_complete_wizard()
        )

    async def async_step_surface_details(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._pending_options.update(user_input)
            if self._pending_options["surface_modelled"]:
                self._pending_options.pop("surface_temperature_entity", None)
            else:
                self._pending_options.pop("surface_f_rsi", None)
            return await self._after_radiant()
        defaults = self._pending_options
        fields: dict[vol.Marker, object] = {
            vol.Required(
                "surface_view_factor", default=defaults.get("surface_view_factor", 0.25)
            ): _number(0.0, 1.0, 0.01),
            vol.Required(
                "surface_rh_threshold_pct",
                default=defaults.get("surface_rh_threshold_pct", 80.0),
            ): _number(1.0, 100.0, 1.0, "%"),
        }
        if defaults.get("surface_modelled", False):
            fields[
                vol.Required("surface_f_rsi", default=defaults["surface_f_rsi"])
                if "surface_f_rsi" in defaults
                else vol.Required("surface_f_rsi")
            ] = _number(0.01, 1.0, 0.01)
        else:
            fields[
                _required_entity(
                    "surface_temperature_entity", defaults.get("surface_temperature_entity")
                )
            ] = ENTITY
        return self.async_show_form(step_id="surface_details", data_schema=vol.Schema(fields))

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
                return await self.async_step_profile_parameters()
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

    async def async_step_profile_parameters(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._pending_options.update(user_input)
            return await self.async_step_control_limits()
        defaults = self._pending_options
        return self.async_show_form(
            step_id="profile_parameters",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "eco_heating_setback_c",
                        default=defaults.get("eco_heating_setback_c", 2.0),
                    ): _number(0.0, 5.0, 0.1, "°C"),
                    vol.Required(
                        "eco_cooling_setback_c",
                        default=defaults.get("eco_cooling_setback_c", 2.0),
                    ): _number(0.0, 5.0, 0.1, "°C"),
                    vol.Required(
                        "boost_delta_c", default=defaults.get("boost_delta_c", 1.0)
                    ): _number(0.0, 3.0, 0.1, "°C"),
                    vol.Required(
                        "boost_duration_minutes",
                        default=defaults.get("boost_duration_minutes", 60.0),
                    ): _number(5.0, 180.0, 5.0, "min"),
                }
            ),
        )

    async def async_step_control_limits(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            pending = {**self._pending_options, **user_input}
            errors = validate_options(pending)
            if "control_bounds" not in errors:
                self._pending_options = pending
                return await self.async_step_command_behavior()
            return self.async_show_form(
                step_id="control_limits",
                data_schema=self._control_limits_schema(),
                errors={"base": "invalid_control_bounds"},
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
                    default=defaults.get("minimum_control_temperature", 18.0),
                ): _number(5.0, 35.0, 0.5, "°C"),
                vol.Required(
                    "maximum_control_temperature",
                    default=defaults.get("maximum_control_temperature", 26.0),
                ): _number(5.0, 35.0, 0.5, "°C"),
                vol.Required(
                    "manual_override_minutes",
                    default=defaults.get("manual_override_minutes", 120.0),
                ): _number(15.0, 1440.0, 15.0, "min"),
                vol.Required("auto_mapping", default=defaults.get("auto_mapping", "unmapped")): (
                    _select(
                        ("unmapped", "heating", "cooling", "range", "bidirectional_scalar"),
                        "auto_mapping",
                    )
                ),
            }
        )

    async def async_step_command_behavior(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._pending_options.update(user_input)
            if self._pending_options["fallback_mode"] == "fixed":
                return await self.async_step_fallback_temperatures()
            self._pending_options.pop("fallback_heating_c", None)
            self._pending_options.pop("fallback_cooling_c", None)
            return await self.async_step_critical_locations()
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
                    "fallback_heating_c", default=defaults.get("fallback_heating_c", 18.0)
                ): _number(5.0, 35.0, 0.5, "°C"),
                vol.Required(
                    "fallback_cooling_c", default=defaults.get("fallback_cooling_c", 26.0)
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
            return await self._begin_target_calibrations()
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
            return await self._begin_target_calibrations()
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

    VERSION = 1

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
            self._data.update(user_input)
            stale_key = (
                CONF_RH_DECLARED if self._data[CONF_RH_MODE] == "measured" else CONF_RH_ENTITY
            )
            self._data.pop(stale_key, None)
            return await self.async_step_humidity()
        return self.async_show_form(step_id="environment", data_schema=self._environment_schema())

    def _environment_schema(self) -> vol.Schema:
        defaults = self._data
        return vol.Schema(
            {
                _required_entity(
                    CONF_PRIMARY_TEMPERATURE, defaults.get(CONF_PRIMARY_TEMPERATURE)
                ): ENTITY,
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
                self._initialize_options_wizard(existing_options=existing, targets=targets)
                return await self.async_step_preferences()
        defaults = [target["entity_id"] for target in self._data.get(CONF_TARGETS, ())]
        marker = (
            vol.Required(CONF_TARGETS, default=defaults) if defaults else vol.Required(CONF_TARGETS)
        )
        return self.async_show_form(
            step_id="targets", data_schema=vol.Schema({marker: CLIMATES}), errors=errors
        )

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
            return self.async_show_form(
                step_id="review",
                data_schema=vol.Schema({}),
                description_placeholders={
                    "zone": str(self._data[CONF_NAME]),
                    "primary": str(self._data[CONF_PRIMARY_TEMPERATURE]),
                    "outdoor": str(self._data[CONF_OUTDOOR_SOURCE]),
                    "target_count": str(len(self._data[CONF_TARGETS])),
                    "strategy": str(self._pending_options[CONF_COMFORT_STRATEGY]),
                },
            )
        return await self._async_commit_config()

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
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema({vol.Required(CONF_NAME, default=self._data[CONF_NAME]): str}),
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
    """Edit the same option set exposed during setup and reconfigure."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._entry = config_entry
        self._pending_options = {}
        self._wizard_targets = list(config_entry.data.get(CONF_TARGETS, ()))
        self._advanced = False
        self._critical_existing = []
        self._critical_locations = []
        self._critical_count = 0
        self._critical_index = 0
        self._calibration_index = 0
        self._initialize_options_wizard(
            existing_options=config_entry.options, targets=self._wizard_targets
        )

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._async_preferences("init", user_input)

    async def _async_complete_wizard(self) -> ConfigFlowResult:
        if validate_options(self._pending_options):
            return self.async_show_form(
                step_id="init", data_schema=vol.Schema({}), errors={"base": "invalid_option"}
            )
        return self.async_create_entry(data=self._pending_options)
