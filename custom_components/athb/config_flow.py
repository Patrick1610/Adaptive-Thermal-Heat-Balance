"""Native UI config and options flows for ATHB."""

from __future__ import annotations

from typing import Any, override
from uuid import uuid4

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry, ConfigFlowResult
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector

from .config_schema import validate_environment, validate_targets
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
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = validate_environment(user_input)
            if not errors:
                self._data.update(user_input)
                return await self.async_step_targets()
        return self.async_show_form(
            step_id="environment",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PRIMARY_TEMPERATURE): ENTITY,
                    vol.Required(CONF_RH_MODE, default="measured"): vol.In(
                        ("measured", "declared")
                    ),
                    vol.Optional(CONF_RH_ENTITY): ENTITY,
                    vol.Optional(CONF_RH_DECLARED): vol.Coerce(float),
                    vol.Required(CONF_OUTDOOR_SOURCE): ENTITY,
                }
            ),
            errors=errors,
        )

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
            if (
                user_input["minimum_control_temperature"]
                >= user_input["maximum_control_temperature"]
            ):
                return self.async_show_form(
                    step_id="control",
                    data_schema=self._control_schema(),
                    errors={"base": "invalid_control_bounds"},
                )
            self._options.update(user_input)
            self._options[CONF_CONTROL_ENABLED] = False
            self._options.setdefault(CONF_PROFILE, DEFAULT_PROFILE)
            title = str(self._data.pop(CONF_NAME))
            await self.async_set_unique_id(self._data[CONF_ZONE_UUID])
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title=title, data=self._data, options=self._options)
        return self.async_show_form(step_id="control", data_schema=self._control_schema())

    @staticmethod
    def _control_schema() -> vol.Schema:
        return vol.Schema(
            {
                vol.Optional("occupancy_entity"): ENTITY,
                vol.Required("minimum_control_temperature", default=18.0): vol.All(
                    vol.Coerce(float), vol.Range(min=5.0, max=35.0)
                ),
                vol.Required("maximum_control_temperature", default=26.0): vol.All(
                    vol.Coerce(float), vol.Range(min=5.0, max=35.0)
                ),
            }
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
        errors: dict[str, str] = {}
        if user_input is not None:
            environment = {
                key: value
                for key, value in user_input.items()
                if key != CONF_TARGETS and value is not None
            }
            errors = validate_environment(environment)
            try:
                target_entities = validate_targets(user_input[CONF_TARGETS])
            except ValueError:
                errors[CONF_TARGETS] = "invalid_targets"
            if not errors:
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
                if not errors:
                    return self.async_update_reload_and_abort(
                        entry, data_updates={**environment, CONF_TARGETS: targets}
                    )
        defaults = entry.data
        schema: dict[vol.Marker, object] = {
            vol.Required(
                CONF_PRIMARY_TEMPERATURE, default=defaults[CONF_PRIMARY_TEMPERATURE]
            ): ENTITY,
            vol.Required(CONF_RH_MODE, default=defaults[CONF_RH_MODE]): vol.In(
                ("measured", "declared")
            ),
            vol.Optional(CONF_RH_DECLARED, default=defaults.get(CONF_RH_DECLARED)): vol.Coerce(
                float
            ),
            vol.Required(CONF_OUTDOOR_SOURCE, default=defaults[CONF_OUTDOOR_SOURCE]): ENTITY,
            vol.Required(
                CONF_TARGETS,
                default=[target["entity_id"] for target in defaults[CONF_TARGETS]],
            ): CLIMATES,
        }
        if CONF_RH_ENTITY in defaults:
            schema[vol.Optional(CONF_RH_ENTITY, default=defaults[CONF_RH_ENTITY])] = ENTITY
        else:
            schema[vol.Optional(CONF_RH_ENTITY)] = ENTITY
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(schema),
            errors=errors,
        )


class AthbOptionsFlow(config_entries.OptionsFlowWithReload):
    def __init__(self, config_entry: ConfigEntry) -> None:
        self._entry = config_entry
        self._pending: dict[str, Any] = {}

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._pending = {**self._entry.options, **user_input}
            if user_input["radiant_model"] == "uniform":
                return self.async_create_entry(data=self._pending)
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
                }
            ),
        )

    async def async_step_radiant(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data={**self._pending, **user_input})
        model = self._pending["radiant_model"]
        field = {
            "direct_mrt": "mrt_entity",
            "globe": "globe_temperature_entity",
            "surface": "surface_temperature_entity",
        }[model]
        return self.async_show_form(
            step_id="radiant", data_schema=vol.Schema({vol.Required(field): ENTITY})
        )
