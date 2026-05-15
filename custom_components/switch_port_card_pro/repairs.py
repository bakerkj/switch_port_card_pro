"""Repairs flow for Switch Port Card Pro.

Raised when a port stays down past the grace period. The user can disable
that port's extra entities, disable every flagged port at once, or ignore
the port (keep its entities; re-arm only on the next up→down cycle).
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult

from .const import DOMAIN
from .entity_manager import PortEntityManager


class PortDownRepairFlow(RepairsFlow):
    """Menu-driven fix flow for a long-down port."""

    def __init__(
        self, hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None
    ) -> None:
        self._hass = hass
        self._issue_id = issue_id
        self._data = data or {}

    def _manager(self) -> PortEntityManager | None:
        coordinator = self._hass.data.get(DOMAIN, {}).get(self._data.get("entry_id"))
        return getattr(coordinator, "entity_manager", None)

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        return self.async_show_menu(
            step_id="init",
            menu_options=["disable_this", "disable_all", "ignore"],
        )

    async def async_step_disable_this(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        mgr = self._manager()
        if mgr is not None:
            await mgr.async_disable_port(self._data.get("port"))
        return self.async_create_entry(title="", data={})

    async def async_step_disable_all(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        mgr = self._manager()
        if mgr is not None:
            await mgr.async_disable_all_flagged()
        return self.async_create_entry(title="", data={})

    async def async_step_ignore(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        mgr = self._manager()
        if mgr is not None:
            await mgr.async_ignore_port(self._data.get("port"))
        return self.async_create_entry(title="", data={})


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None
) -> RepairsFlow:
    return PortDownRepairFlow(hass, issue_id, data)
