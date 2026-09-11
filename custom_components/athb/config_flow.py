"""Native UI config and options flows for ATHB."""

from __future__ import annotations

import json
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


class AthbConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._options: dict[str, Any] = {}
        self._reconfigure_data: dict[str, Any] = {}

    @override
    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(user_input)
            self._data[CONF_ZONE_UUID] = str(uuid4())
            return await self.async_step_environment()
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_NAME): str}),
        )

    async def async_step_environment(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_humidity()
        return self.async_show_form(
            step_id="environment",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PRIMARY_TEMPERATURE): ENTITY,
                    vol.Required(CONF_RH_MODE, default="measured"): vol.In(
                        ("measured", "declared")
                    ),
                    vol.Required(CONF_OUTDOOR_SOURCE): ENTITY,
                }
            ),
        )

    async def async_step_humidity(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        mode = str(self._data[CONF_RH_MODE])
        errors: dict[str, str] = {}
        if user_input is not None:
            environment = {
                CONF_PRIMARY_TEMPERATURE: self._data[CONF_PRIMARY_TEMPERATURE],
                CONF_OUTDOOR_SOURCE: self._data[CONF_OUTDOOR_SOURCE],
                CONF_RH_MODE: mode,
                **user_input,
            }
            errors = validate_environment(environment)
            if not errors:
                self._data.update(user_input)
                return await self.async_step_targets()
        schema = (
            vol.Schema({vol.Required(CONF_RH_ENTITY): ENTITY})
            if mode == "measured"
            else vol.Schema({vol.Required(CONF_RH_DECLARED): vol.Coerce(float)})
        )
        return self.async_show_form(step_id="humidity", data_schema=schema, errors=errors)

    async def async_step_targets(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                target_entities = validate_targets(user_input[CONF_TARGETS])
            except ValueError:
                errors[CONF_TARGETS] = "invalid_targets"
            else:
                registry = er.async_get(self.hass)
                targets: list[dict[str, str]] = []
                for entity_id in target_entities:
                    registry_entry = registry.async_get(entity_id)
                    if registry_entry is None:
                        errors[CONF_TARGETS] = "target_not_registered"
                        break
                    targets.append(
                        {
                            "target_uuid": str(uuid4()),
                            "entity_id": entity_id,
                            "registry_identity": registry_entry.id,
                        }
                    )
                    if self._target_is_claimed(registry_entry.id):
                        errors[CONF_TARGETS] = "target_already_controlled"
                        break
                if errors:
                    return self.async_show_form(
                        step_id="targets",
                        data_schema=vol.Schema({vol.Required(CONF_TARGETS): CLIMATES}),
                        errors=errors,
                    )
                self._data[CONF_TARGETS] = targets
                return await self.async_step_comfort()
        return self.async_show_form(
            step_id="targets",
            data_schema=vol.Schema({vol.Required(CONF_TARGETS): CLIMATES}),
            errors=errors,
        )

    async def async_step_comfort(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._options.update(user_input)
            return await self.async_step_control()
        return self.async_show_form(
            step_id="comfort",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_COMFORT_STRATEGY, default=DEFAULT_STRATEGY): vol.In(
                        ("efficient", "balanced", "comfort")
                    )
                }
            ),
        )

    async def async_step_control(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            advanced = bool(user_input.pop("advanced_settings", False))
            self._options.update(user_input)
            if advanced:
                return await self.async_step_advanced_control()
            self._set_initial_defaults()
            return await self.async_step_review()
        return self.async_show_form(step_id="control", data_schema=self._control_schema())

    @staticmethod
    def _control_schema() -> vol.Schema:
        return vol.Schema(
            {
                vol.Optional("occupancy_entity"): ENTITY,
                vol.Optional("advanced_settings", default=False): bool,
            }
        )

    async def async_step_advanced_control(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            if validate_options(user_input):
                return self.async_show_form(
                    step_id="advanced_control",
                    data_schema=self._advanced_control_schema(),
                    errors={"base": "invalid_control_bounds"},
                )
            self._options.update(user_input)
            self._set_initial_defaults()
            return await self.async_step_review()
        return self.async_show_form(
            step_id="advanced_control", data_schema=self._advanced_control_schema()
        )

    @staticmethod
    def _advanced_control_schema() -> vol.Schema:
        return vol.Schema(
            {
                vol.Required("met", default=1.1): vol.All(
                    vol.Coerce(float), vol.Range(min=0.8, max=2.0)
                ),
                vol.Required("air_speed_m_s", default=0.1): vol.All(
                    vol.Coerce(float), vol.Range(min=0.0, max=2.0)
                ),
                vol.Required("minimum_control_temperature", default=18.0): vol.All(
                    vol.Coerce(float), vol.Range(min=5.0, max=35.0)
                ),
                vol.Required("maximum_control_temperature", default=26.0): vol.All(
                    vol.Coerce(float), vol.Range(min=5.0, max=35.0)
                ),
                vol.Required("fallback_mode", default="fixed"): vol.In(("fixed", "no_write")),
                vol.Required("fallback_heating_c", default=18.0): vol.Coerce(float),
                vol.Required("fallback_cooling_c", default=26.0): vol.Coerce(float),
                vol.Required("manual_override_minutes", default=120.0): vol.All(
                    vol.Coerce(float), vol.Range(min=15.0, max=1440.0)
                ),
            }
        )

    def _set_initial_defaults(self) -> None:
        defaults: dict[str, object] = {
            "met": 1.1,
            "air_speed_m_s": 0.1,
            "minimum_control_temperature": 18.0,
            "maximum_control_temperature": 26.0,
            "fallback_mode": "fixed",
            "fallback_heating_c": 18.0,
            "fallback_cooling_c": 26.0,
            "manual_override_minutes": 120.0,
            "radiant_model": "uniform",
            CONF_PROFILE: DEFAULT_PROFILE,
            CONF_CONTROL_ENABLED: False,
        }
        for key, value in defaults.items():
            self._options.setdefault(key, value)

    async def async_step_review(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            title = str(self._data.pop(CONF_NAME))
            await self.async_set_unique_id(self._data[CONF_ZONE_UUID])
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title=title, data=self._data, options=self._options)
        return self.async_show_form(
            step_id="review",
            data_schema=vol.Schema({}),
            description_placeholders={
                "zone": str(self._data[CONF_NAME]),
                "primary": str(self._data[CONF_PRIMARY_TEMPERATURE]),
                "outdoor": str(self._data[CONF_OUTDOOR_SOURCE]),
                "target_count": str(len(self._data[CONF_TARGETS])),
                "strategy": str(self._options[CONF_COMFORT_STRATEGY]),
            },
        )

    @staticmethod
    @callback
    @override
    def async_get_options_flow(config_entry: ConfigEntry) -> config_entries.OptionsFlow:
        return AthbOptionsFlow(config_entry)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        if user_input is not None:
            self._reconfigure_data.update(user_input)
            return await self.async_step_reconfigure_humidity()
        defaults = entry.data
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_PRIMARY_TEMPERATURE,
                        default=defaults[CONF_PRIMARY_TEMPERATURE],
                    ): ENTITY,
                    vol.Required(CONF_RH_MODE, default=defaults[CONF_RH_MODE]): vol.In(
                        ("measured", "declared")
                    ),
                    vol.Required(
                        CONF_OUTDOOR_SOURCE, default=defaults[CONF_OUTDOOR_SOURCE]
                    ): ENTITY,
                }
            ),
        )

    async def async_step_reconfigure_humidity(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        mode = str(self._reconfigure_data[CONF_RH_MODE])
        errors: dict[str, str] = {}
        if user_input is not None:
            environment = {**self._reconfigure_data, **user_input}
            errors = validate_environment(environment)
            if not errors:
                self._reconfigure_data.update(user_input)
                return await self.async_step_reconfigure_targets()
        if mode == "measured":
            prior = entry.data.get(CONF_RH_ENTITY)
            marker = (
                vol.Required(CONF_RH_ENTITY, default=prior)
                if prior is not None
                else vol.Required(CONF_RH_ENTITY)
            )
            schema = vol.Schema({marker: ENTITY})
        else:
            schema = vol.Schema(
                {
                    vol.Required(
                        CONF_RH_DECLARED,
                        default=entry.data.get(CONF_RH_DECLARED, 50.0),
                    ): vol.Coerce(float)
                }
            )
        return self.async_show_form(
            step_id="reconfigure_humidity", data_schema=schema, errors=errors
        )

    async def async_step_reconfigure_targets(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                target_entities = validate_targets(user_input[CONF_TARGETS])
            except ValueError:
                errors[CONF_TARGETS] = "invalid_targets"
            else:
                registry = er.async_get(self.hass)
                prior = {
                    target["registry_identity"]: target["target_uuid"]
                    for target in entry.data[CONF_TARGETS]
                }
                targets: list[dict[str, str]] = []
                for entity_id in target_entities:
                    registry_entry = registry.async_get(entity_id)
                    if registry_entry is None:
                        errors[CONF_TARGETS] = "target_not_registered"
                        break
                    targets.append(
                        {
                            "target_uuid": prior.get(registry_entry.id, str(uuid4())),
                            "entity_id": entity_id,
                            "registry_identity": registry_entry.id,
                        }
                    )
                    if self._target_is_claimed(registry_entry.id, excluding=entry.entry_id):
                        errors[CONF_TARGETS] = "target_already_controlled"
                        break
                if not errors:
                    updated = {**entry.data, **self._reconfigure_data, CONF_TARGETS: targets}
                    stale_rh_key = (
                        CONF_RH_DECLARED
                        if self._reconfigure_data[CONF_RH_MODE] == "measured"
                        else CONF_RH_ENTITY
                    )
                    updated.pop(stale_rh_key, None)
                    await self.async_set_unique_id(str(entry.data[CONF_ZONE_UUID]))
                    self._abort_if_unique_id_mismatch()
                    return self.async_update_reload_and_abort(entry, data=updated)
        return self.async_show_form(
            step_id="reconfigure_targets",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_TARGETS,
                        default=[target["entity_id"] for target in entry.data[CONF_TARGETS]],
                    ): CLIMATES
                }
            ),
            errors=errors,
        )

    def _target_is_claimed(self, registry_identity: str, *, excluding: str | None = None) -> bool:
        """Reject a target already owned by another enabled ATHB config entry."""

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


class AthbOptionsFlow(config_entries.OptionsFlowWithReload):
    def __init__(self, config_entry: ConfigEntry) -> None:
        self._entry = config_entry
        self._pending: dict[str, Any] = {}
        self._advanced = False

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._advanced = bool(user_input.pop("advanced_settings", False))
            self._pending = {**self._entry.options, **user_input}
            if user_input["radiant_model"] == "uniform":
                return (
                    await self.async_step_advanced() if self._advanced else self._finish_options()
                )
            return await self.async_step_radiant()
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_COMFORT_STRATEGY,
                        default=self._entry.options.get(CONF_COMFORT_STRATEGY, DEFAULT_STRATEGY),
                    ): vol.In(("efficient", "balanced", "comfort")),
                    vol.Required(
                        CONF_PROFILE,
                        default=self._entry.options.get(CONF_PROFILE, DEFAULT_PROFILE),
                    ): vol.In(("auto", "comfort", "eco", "boost")),
                    vol.Required(
                        "radiant_model", default=self._entry.options.get("radiant_model", "uniform")
                    ): vol.In(("uniform", "direct_mrt", "globe", "surface")),
                    vol.Optional("advanced_settings", default=False): bool,
                }
            ),
        )

    async def async_step_radiant(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        model = str(self._pending["radiant_model"])
        if user_input is not None:
            self._pending.update(user_input)
            if model == "surface":
                stale_surface_key = (
                    "surface_temperature_entity"
                    if self._pending["surface_modelled"]
                    else "surface_f_rsi"
                )
                self._pending.pop(stale_surface_key, None)
                return await self.async_step_surface_details()
            return await self.async_step_advanced() if self._advanced else self._finish_options()
        return self.async_show_form(step_id="radiant", data_schema=self._radiant_schema(model))

    def _radiant_schema(self, model: str) -> vol.Schema:
        if model == "direct_mrt":
            return vol.Schema({vol.Required("mrt_entity"): ENTITY})
        if model == "globe":
            return vol.Schema(
                {
                    vol.Required("globe_temperature_entity"): ENTITY,
                    vol.Required("globe_diameter_m", default=0.15): vol.Coerce(float),
                    vol.Required("globe_emissivity", default=0.95): vol.Coerce(float),
                }
            )
        return vol.Schema({vol.Required("surface_modelled", default=False): bool})

    async def async_step_surface_details(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._pending.update(user_input)
            errors = validate_options(self._pending)
            if not errors:
                return (
                    await self.async_step_advanced() if self._advanced else self._finish_options()
                )
            return self.async_show_form(
                step_id="surface_details",
                data_schema=self._surface_details_schema(),
                errors={key: value for key, value in errors.items() if key in user_input},
            )
        return self.async_show_form(
            step_id="surface_details", data_schema=self._surface_details_schema()
        )

    def _surface_details_schema(self) -> vol.Schema:
        fields: dict[vol.Marker, object] = {
            vol.Required(
                "surface_view_factor",
                default=self._pending.get("surface_view_factor", 0.25),
            ): vol.Coerce(float),
            vol.Required(
                "surface_rh_threshold_pct",
                default=self._pending.get("surface_rh_threshold_pct", 80.0),
            ): vol.Coerce(float),
        }
        if self._pending.get("surface_modelled", False):
            f_rsi = self._pending.get("surface_f_rsi")
            marker = (
                vol.Required("surface_f_rsi", default=f_rsi)
                if f_rsi is not None
                else vol.Required("surface_f_rsi")
            )
            fields[marker] = vol.Coerce(float)
        else:
            surface_entity = self._pending.get("surface_temperature_entity")
            marker = (
                vol.Required("surface_temperature_entity", default=surface_entity)
                if surface_entity is not None
                else vol.Required("surface_temperature_entity")
            )
            fields[marker] = ENTITY
        return vol.Schema(fields)

    async def async_step_advanced(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            pending = {**self._pending, **user_input}
            try:
                critical = json.loads(str(user_input.get("critical_locations_json", "[]")))
            except json.JSONDecodeError:
                errors["critical_locations_json"] = "invalid_option"
            else:
                pending["critical_locations"] = critical
                pending.pop("critical_locations_json", None)
                errors = validate_options(pending)
                if not errors:
                    self._pending = pending
                    return self.async_create_entry(data=self._pending)
        return self.async_show_form(
            step_id="advanced",
            data_schema=self._advanced_schema(),
            errors=errors,
        )

    def _advanced_schema(self) -> vol.Schema:
        fields: dict[vol.Marker, object] = {
            vol.Required("met", default=self._entry.options.get("met", 1.1)): vol.Coerce(float),
            vol.Required(
                "clothing_mode",
                default=self._entry.options.get("clothing_mode", "automatic"),
            ): vol.In(("automatic", "fixed")),
            vol.Required(
                "fixed_clothing_clo",
                default=self._entry.options.get("fixed_clothing_clo", 0.7),
            ): vol.Coerce(float),
            vol.Required(
                "air_speed_mode",
                default=self._entry.options.get("air_speed_mode", "fixed"),
            ): vol.In(("fixed", "measured")),
            vol.Required(
                "air_speed_m_s",
                default=self._entry.options.get("air_speed_m_s", 0.1),
            ): vol.Coerce(float),
            vol.Required("lower_comfort_vote", default=-0.5): vol.Coerce(float),
            vol.Required("upper_comfort_vote", default=0.5): vol.Coerce(float),
            vol.Required("running_mean_alpha", default=0.8): vol.Coerce(float),
            vol.Required("eco_heating_setback_c", default=2.0): vol.Coerce(float),
            vol.Required("eco_cooling_setback_c", default=2.0): vol.Coerce(float),
            vol.Required("boost_delta_c", default=1.0): vol.Coerce(float),
            vol.Required("boost_duration_minutes", default=60.0): vol.Coerce(float),
            vol.Required("manual_override_minutes", default=120.0): vol.Coerce(float),
            vol.Required("minimum_range_gap", default=1.0): vol.Coerce(float),
            vol.Required("minimum_meaningful_change", default=0.1): vol.Coerce(float),
            vol.Required("feedback_resolution", default=0.01): vol.Coerce(float),
            vol.Required("reject_extrapolation", default=False): bool,
            vol.Required("auto_mapping", default="unmapped"): vol.In(
                ("unmapped", "heating", "cooling", "range", "bidirectional_scalar")
            ),
            vol.Optional(
                "critical_locations_json",
                default=json.dumps(self._entry.options.get("critical_locations", [])),
            ): str,
        }
        for target in self._entry.data.get(CONF_TARGETS, ()):
            key = f"calibration_{target['target_uuid']}"
            fields[vol.Required(key, default=self._entry.options.get(key, 0.0))] = vol.Coerce(float)
        if air_speed_entity := self._entry.options.get("air_speed_entity"):
            fields[vol.Optional("air_speed_entity", default=air_speed_entity)] = ENTITY
        else:
            fields[vol.Optional("air_speed_entity")] = ENTITY
        return vol.Schema(fields)

    def _finish_options(self) -> ConfigFlowResult:
        errors = validate_options(self._pending)
        if errors:
            return self.async_show_form(
                step_id="init",
                data_schema=vol.Schema({}),
                errors={"base": "invalid_option"},
            )
        return self.async_create_entry(data=self._pending)
