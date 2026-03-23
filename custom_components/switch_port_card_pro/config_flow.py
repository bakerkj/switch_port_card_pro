"""Config Flow for Switch Port Card Pro."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
# import homeassistant.helpers.selector as selector
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import config_validation as cv
from homeassistant.core import callback

from .snmp_helper import async_snmp_get 
from .const import (
    DOMAIN,
    CONF_COMMUNITY,
    CONF_PORTS,
    CONF_HOST,
    DEFAULT_PORTS,
    DEFAULT_BASE_OIDS,
    DEFAULT_SYSTEM_OIDS,
    CONF_SNMP_PORT,
    DEFAULT_SNMP_PORT,
    CONF_OID_SYSNAME,
    CONF_INCLUDE_VLANS,
)

_LOGGER = logging.getLogger(__name__)

# --- Initial setup schema ---
STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_COMMUNITY, default="public"): str,
        vol.Required(CONF_SNMP_PORT, default=DEFAULT_SNMP_PORT): vol.All(vol.Coerce(int), vol.Range(min=1, max=10000)),
    }
)


class SwitchPortCardProConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for Switch Port Card Pro."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Initial setup step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_HOST].lower())
            self._abort_if_unique_id_configured()

            # Connection Test: Crucial for network integrations
            try:
                await self._test_connection(
                    self.hass,
                    user_input[CONF_HOST],
                    user_input[CONF_COMMUNITY],
                    user_input[CONF_SNMP_PORT],
                )
            except ConnectionError:
                errors["base"] = "cannot_connect"
            except ValueError:
                errors["base"] = "invalid_community"
            except Exception:
                _LOGGER.exception("Unexpected error during connection test")
                errors["base"] = "unknown"

            if not errors:
                # Create entry with data and initial default options
                return self.async_create_entry(
                    title=user_input[CONF_HOST],
                    data={
                        CONF_HOST: user_input[CONF_HOST],
                        CONF_COMMUNITY: user_input[CONF_COMMUNITY],
                        CONF_SNMP_PORT: user_input[CONF_SNMP_PORT],
                    },
                    options={
    #                    CONF_PORTS: DEFAULT_PORTS, # removed for auto port detection
                        CONF_INCLUDE_VLANS: True,
                        "snmp_version": "v2c",
                        "oid_rx": DEFAULT_BASE_OIDS["rx"],
                        "oid_tx": DEFAULT_BASE_OIDS["tx"],
                        "oid_status": DEFAULT_BASE_OIDS["status"],
                        "oid_speed": DEFAULT_BASE_OIDS["speed"],
                        "oid_name": DEFAULT_BASE_OIDS.get("name", ""),
                        "oid_vlan": DEFAULT_BASE_OIDS.get("vlan", ""),
                        "oid_cpu": DEFAULT_SYSTEM_OIDS.get("cpu", ""),
                        "oid_firmware": DEFAULT_SYSTEM_OIDS.get("firmware", ""),
                        "oid_memory": DEFAULT_SYSTEM_OIDS.get("memory", ""),
                        "oid_memory_total": DEFAULT_SYSTEM_OIDS.get("memory_total", ""),
                        "oid_hostname": DEFAULT_SYSTEM_OIDS.get("hostname", ""),
                        "oid_uptime": DEFAULT_SYSTEM_OIDS.get("uptime", ""),
                        "oid_poe_power": DEFAULT_SYSTEM_OIDS.get("poe_power", ""),
                        "oid_poe_status": DEFAULT_SYSTEM_OIDS.get("poe_status", ""),
                        "oid_poe_class": DEFAULT_BASE_OIDS.get("poe_class", ""),
                        "oid_custom": DEFAULT_SYSTEM_OIDS.get("custom", ""),
                        "oid_port_custom": DEFAULT_SYSTEM_OIDS.get("port_custom", ""),
                        "update_interval": 20,
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_SCHEMA,
            errors=errors,
        )

    async def _test_connection(self, hass: HomeAssistant, host: str, community: str, snmp_port: int) -> None:
        """Test SNMP connectivity by fetching sysName. Raises ConnectionError if unreachable."""
        result = await async_snmp_get(
            hass,
            host,
            community,
            snmp_port,
            CONF_OID_SYSNAME,
            timeout=12,
            retries=3,
            mp_model=1,  # v2c
        )
        if result is None:
            raise ConnectionError(f"No SNMP response from {host}:{snmp_port}")

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Allow changing host, community string, or SNMP port without re-adding."""
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()

        if user_input is not None:
            try:
                await self._test_connection(
                    self.hass,
                    user_input[CONF_HOST],
                    user_input[CONF_COMMUNITY],
                    user_input[CONF_SNMP_PORT],
                )
            except ConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error during reconfigure")
                errors["base"] = "unknown"

            if not errors:
                return self.async_update_reload_and_abort(
                    entry,
                    title=user_input[CONF_HOST],
                    data_updates={
                        CONF_HOST: user_input[CONF_HOST],
                        CONF_COMMUNITY: user_input[CONF_COMMUNITY],
                        CONF_SNMP_PORT: user_input[CONF_SNMP_PORT],
                    },
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema({
                vol.Required(CONF_HOST, default=entry.data.get(CONF_HOST, "")): str,
                vol.Required(CONF_COMMUNITY, default=entry.data.get(CONF_COMMUNITY, "public")): str,
                vol.Required(CONF_SNMP_PORT, default=entry.data.get(CONF_SNMP_PORT, DEFAULT_SNMP_PORT)): vol.All(vol.Coerce(int), vol.Range(min=1, max=10000)),
            }),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry):
        """Return options flow."""
        return SwitchPortCardProOptionsFlow(config_entry)


class SwitchPortCardProOptionsFlow(config_entries.OptionsFlow):
    """Options flow for Switch Port Card Pro."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize."""
     #   self.config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options."""
        return await self.async_step_options(user_input)

    async def async_step_options(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle options."""
        current = self.config_entry.options

        if user_input is not None:
            import re
            errors: dict[str, str] = {}

            # Validate and strip all OID fields
            for key, value in user_input.items():
                if key.startswith("oid_"):
                    stripped = value.strip() if value else ""
                    if stripped and not re.fullmatch(r"\d+(\.\d+)*", stripped):
                        errors[key] = "invalid_oid"
                    else:
                        user_input[key] = stripped

            if errors:
                return self.async_show_form(
                    step_id="options",
                    data_schema=self._build_schema(current, user_input),
                    errors=errors,
                )

            # Convert port strings (from multi-select) to integers for saving
            if CONF_PORTS in user_input and isinstance(user_input[CONF_PORTS], list):
                user_input[CONF_PORTS] = [int(p) for p in user_input[CONF_PORTS]]

            try:
                new = {**current, **user_input}
                return self.async_create_entry(title="", data=new)
            except Exception as err:
                _LOGGER.exception("Error saving options: %s", err)
                return self.async_abort(reason="Error storing input")

        return self.async_show_form(
            step_id="options",
            data_schema=self._build_schema(current),
        )

    def _build_schema(self, current: dict, overrides: dict | None = None) -> vol.Schema:
        """Build the options schema, optionally pre-filling fields from overrides."""
        src = {**current, **(overrides or {})}
        ports_dict = {str(i): str(i) for i in range(1, 65)}
        current_ports = [str(p) for p in src.get(CONF_PORTS, DEFAULT_PORTS)]
        return vol.Schema(
            {
                vol.Optional(
                    "update_interval",
                    default=src.get("update_interval", 20)
                ): cv.positive_int,
                vol.Optional(
                    CONF_PORTS,
                    default=current_ports,
                ): cv.multi_select(ports_dict),
                vol.Optional(
                    "re_detect_ports",
                    default=False,
                ): cv.boolean,
                
                vol.Optional(
                    CONF_INCLUDE_VLANS,
                    default=src.get(CONF_INCLUDE_VLANS, True),
                ): cv.boolean,
                
                # --- Port OIDs ---
                vol.Optional(
                    "oid_rx",
                    default=src.get("oid_rx", DEFAULT_BASE_OIDS["rx"]),
                ): cv.string,
                vol.Optional(
                    "oid_tx",
                    default=src.get("oid_tx", DEFAULT_BASE_OIDS["tx"]),
                ): cv.string,
                vol.Optional(
                    "oid_status",
                    default=src.get("oid_status", DEFAULT_BASE_OIDS["status"]),
                ): cv.string,
                vol.Optional(
                    "oid_speed",
                    default=src.get("oid_speed", DEFAULT_BASE_OIDS["speed"]),
                ): cv.string,
                vol.Optional(
                    "oid_name",
                    default=src.get("oid_name", DEFAULT_BASE_OIDS.get("name", "")),
                ): cv.string,
                vol.Optional(
                    "oid_vlan",
                    default=src.get("oid_vlan", DEFAULT_BASE_OIDS.get("vlan", "")),
                ): cv.string,

                # --- System OIDs ---
                vol.Optional(
                    "oid_cpu",
                    default=src.get("oid_cpu", DEFAULT_SYSTEM_OIDS.get("cpu", "")),
                ): cv.string,
                vol.Optional(
                    "oid_firmware",
                    default=src.get("oid_firmware", DEFAULT_SYSTEM_OIDS.get("firmware", "")),
                ): cv.string,
                vol.Optional(
                    "oid_memory",
                    default=src.get("oid_memory", DEFAULT_SYSTEM_OIDS.get("memory", "")),
                ): cv.string,
                vol.Optional(
                    "oid_memory_total",
                    default=src.get("oid_memory_total", DEFAULT_SYSTEM_OIDS.get("memory_total", "")),
                ): cv.string,
                vol.Optional(
                    "oid_hostname",
                    default=src.get("oid_hostname", DEFAULT_SYSTEM_OIDS.get("hostname", "")),
                ): cv.string,
                vol.Optional(
                    "oid_uptime",
                    default=src.get("oid_uptime", DEFAULT_SYSTEM_OIDS.get("uptime", "")),
                ): cv.string,
                vol.Optional(
                    "oid_poe_power",
                    default=src.get("oid_poe_power", DEFAULT_SYSTEM_OIDS.get("poe_power", "")),
                ): cv.string,
                vol.Optional(
                    "oid_poe_status",
                    default=src.get("oid_poe_status", DEFAULT_SYSTEM_OIDS.get("poe_status", "")),
                ): cv.string,
                vol.Optional(
                    "oid_poe_class",
                    default=src.get("oid_poe_class", DEFAULT_BASE_OIDS.get("poe_class", "")),
                ): cv.string,
                vol.Optional(
                    "oid_custom",
                    default=src.get("oid_custom", DEFAULT_SYSTEM_OIDS.get("custom", "")),
                ): cv.string,
                vol.Optional(
                    "oid_port_custom",
                    default=src.get("oid_port_custom", DEFAULT_SYSTEM_OIDS.get("port_custom", "")),
                ): cv.string,
                vol.Optional(
                    "snmp_version",
                    default=src.get("snmp_version", "v2c"),
                ): vol.In({"v2c": "v2c", "v1": "v1"}),
     #           vol.Optional(
     #               CONF_SFP_PORTS_START, 
     #               default=25,
     #           ): vol.All(vol.Coerce(int), vol.Range(min=1, max=52)),
            }
        )
