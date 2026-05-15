"""Auto-manage per-port entities for Switch Port Card Pro.

Goal: keep the registry small on switches with many unused ports. Each port
keeps its `status` indicator always; every *other* per-port entity that is
currently enabled is a disable candidate when the port is down.

Policy (hybrid):
  * Auto re-enable (no prompt): a port we disabled, seen "on" for
    `up_restore_cycles` consecutive polls, gets *exactly* the entities we
    disabled re-enabled — nothing more.
  * Suggest disable (never automatic): a port down continuously past
    `down_grace_hours` raises a fixable Repair issue. The user chooses to
    disable that port, disable all flagged ports, or ignore the port.

Note: entities with `entity_registry_enabled_default=False` are registered by
HA with `disabled_by=INTEGRATION` — the same marker we would use. So we cannot
infer "we disabled this" from `disabled_by` alone; we persist the exact set of
unique_ids we disabled and only ever re-enable those. Anything the user
disabled/enabled by hand is left alone, and `status` is never touched.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store

from .const import (
    DOMAIN,
    PORT_ANCHOR_KEY,
    CONF_AUTO_MANAGE_ENTITIES,
    CONF_DOWN_GRACE_HOURS,
    CONF_UP_RESTORE_CYCLES,
    DEFAULT_AUTO_MANAGE_ENTITIES,
    DEFAULT_DOWN_GRACE_HOURS,
    DEFAULT_UP_RESTORE_CYCLES,
)

_LOGGER = logging.getLogger(__name__)

_STORE_VERSION = 1
_UID_PORT_RE = re.compile(r"_port_(\d+)_(.+)$")


def _issue_id(entry_id: str, port: int | str) -> str:
    return f"port_down_{entry_id}_{port}"


def _default_port_state() -> dict[str, Any]:
    return {
        "down_since": None,  # epoch secs the current down episode began
        "ignored": False,  # user chose "ignore" — suppress until next up→down
        "disabled_uids": [],  # unique_ids WE disabled (precise restore set)
        "issue_open": False,  # a Repair issue is currently raised
    }


class PortEntityManager:
    """Reconciles per-port entity enablement against live link state."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, coordinator: Any
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.coordinator = coordinator
        self._store: Store = Store(
            hass, _STORE_VERSION, f"{DOMAIN}.{entry.entry_id}.entity_manager"
        )
        self._state: dict[str, dict[str, Any]] = {}  # port(str) -> state
        self._on_streak: dict[str, int] = {}  # port(str) -> consecutive ups
        self._lock = asyncio.Lock()
        self._unsub: Any | None = None
        self._stopped = False

    # --- lifecycle ---------------------------------------------------------

    async def async_load(self) -> None:
        data = await self._store.async_load()
        if isinstance(data, dict):
            loaded = data.get("ports", {}) or {}
            # Merge onto defaults so older stores gain new keys.
            for port, st in loaded.items():
                merged = _default_port_state()
                merged.update(st)
                self._state[str(port)] = merged

    @callback
    def async_start(self) -> None:
        """Begin reconciling after every coordinator update (and once now)."""
        self._unsub = self.coordinator.async_add_listener(self._schedule_reconcile)
        self._schedule_reconcile()

    @callback
    def async_stop(self) -> None:
        self._stopped = True
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    # --- options -----------------------------------------------------------

    def _opts(self) -> tuple[bool, float, int]:
        o = self.entry.options
        enabled = bool(o.get(CONF_AUTO_MANAGE_ENTITIES, DEFAULT_AUTO_MANAGE_ENTITIES))
        try:
            grace_h = float(o.get(CONF_DOWN_GRACE_HOURS, DEFAULT_DOWN_GRACE_HOURS))
        except (TypeError, ValueError):
            grace_h = DEFAULT_DOWN_GRACE_HOURS
        try:
            cycles = int(o.get(CONF_UP_RESTORE_CYCLES, DEFAULT_UP_RESTORE_CYCLES))
        except (TypeError, ValueError):
            cycles = DEFAULT_UP_RESTORE_CYCLES
        return enabled, max(0.0, grace_h) * 3600.0, max(1, cycles)

    # --- registry helpers --------------------------------------------------

    def _port_managed_entities(self, port: str) -> list[er.RegistryEntry]:
        """Per-port registry entries for `port`, excluding the status anchor."""
        reg = er.async_get(self.hass)
        out: list[er.RegistryEntry] = []
        for ent in er.async_entries_for_config_entry(reg, self.entry.entry_id):
            if ent.platform != DOMAIN:
                continue
            m = _UID_PORT_RE.search(ent.unique_id or "")
            if not m or m.group(1) != str(port):
                continue
            if m.group(2) == PORT_ANCHOR_KEY:
                continue
            out.append(ent)
        return out

    def _disable(self, entities: list[er.RegistryEntry]) -> list[str]:
        """Disable currently-enabled entities. Returns unique_ids disabled."""
        reg = er.async_get(self.hass)
        done: list[str] = []
        for ent in entities:
            if ent.disabled_by is not None:
                continue  # already disabled (by us, the user, or default-off)
            reg.async_update_entity(
                ent.entity_id, disabled_by=er.RegistryEntryDisabler.INTEGRATION
            )
            done.append(ent.unique_id)
        return done

    def _reenable(self, port: str, uids: list[str]) -> int:
        """Re-enable exactly the listed unique_ids (if we still own them)."""
        reg = er.async_get(self.hass)
        want = set(uids)
        n = 0
        for ent in self._port_managed_entities(port):
            if ent.unique_id not in want:
                continue
            if ent.disabled_by != er.RegistryEntryDisabler.INTEGRATION:
                continue  # user re-enabled it, or it changed — don't fight
            reg.async_update_entity(ent.entity_id, disabled_by=None)
            n += 1
        return n

    # --- reconcile ---------------------------------------------------------

    @callback
    def _schedule_reconcile(self) -> None:
        if self._stopped or self._lock.locked():
            return  # stopped, or a run is in flight (next poll catches up)
        self.hass.async_create_task(self._async_reconcile())

    async def _async_reconcile(self) -> None:
        async with self._lock:
            if self._stopped:
                return
            enabled, grace_s, up_cycles = self._opts()
            if not enabled:
                # Issues are persistent; if the feature is turned off they
                # would otherwise linger forever. Clear ours.
                await self._async_clear_all_issues()
                return
            if not self.coordinator.data:
                return
            now = time.time()
            dirty = False
            for port_int in self.coordinator.ports:
                port = str(port_int)
                pdata = self.coordinator.data.ports.get(port) or {}
                status_on = pdata.get("status") == "on"
                st = self._state.setdefault(port, _default_port_state())
                recorded = list(st.get("disabled_uids") or [])

                if status_on:
                    if st["down_since"] is not None:
                        st["down_since"] = None
                        dirty = True
                    if st["ignored"]:
                        st["ignored"] = False
                        dirty = True
                    if st["issue_open"]:
                        ir.async_delete_issue(
                            self.hass, DOMAIN, _issue_id(self.entry.entry_id, port)
                        )
                        st["issue_open"] = False
                        dirty = True
                    if recorded:
                        self._on_streak[port] = self._on_streak.get(port, 0) + 1
                        if self._on_streak[port] >= up_cycles:
                            n = self._reenable(port, recorded)
                            self._on_streak[port] = 0
                            st["disabled_uids"] = []
                            dirty = True
                            if n:
                                _LOGGER.info(
                                    "Port %s on %s back up — re-enabled " "%d entities",
                                    port,
                                    self.coordinator.host,
                                    n,
                                )
                    else:
                        self._on_streak[port] = 0
                    continue

                # --- port is down ---
                self._on_streak[port] = 0
                if recorded or st["ignored"]:
                    if st["down_since"] is not None:
                        st["down_since"] = None
                        dirty = True
                    continue
                managed = self._port_managed_entities(port)
                enabled_now = [e for e in managed if e.disabled_by is None]
                if not enabled_now:
                    if st["down_since"] is not None:
                        st["down_since"] = None
                        dirty = True
                    continue
                if st["down_since"] is None:
                    # Seed the clock. If the switch reports how long the port
                    # has held its current (down) state via ifLastChange,
                    # back-date so a long-dead port is flagged on this poll
                    # instead of waiting a fresh grace period from first
                    # observation. ifLastChange resets on a switch reboot, so
                    # this only ever accelerates — never flags early beyond
                    # what the switch itself reports.
                    lc = pdata.get("last_change_seconds")
                    if isinstance(lc, (int, float)) and lc > 0:
                        st["down_since"] = now - float(lc)
                    else:
                        st["down_since"] = now
                    dirty = True
                if (
                    not st["issue_open"]
                    and st["down_since"] is not None
                    and now - st["down_since"] >= grace_s
                ):
                    self._raise_issue(port)
                    st["issue_open"] = True
                    dirty = True

            if dirty:
                await self._async_save()

    def _raise_issue(self, port: str) -> None:
        _enabled, grace_s, _cycles = self._opts()
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            _issue_id(self.entry.entry_id, port),
            is_fixable=True,
            is_persistent=True,  # survive HA restarts (matches persisted issue_open)
            severity=ir.IssueSeverity.WARNING,
            translation_key="port_down",
            translation_placeholders={
                "host": self.coordinator.host,
                "port": port,
                "hours": str(int(grace_s // 3600)),
            },
            data={"entry_id": self.entry.entry_id, "port": port},
        )

    async def _async_save(self) -> None:
        await self._store.async_save({"ports": self._state})

    async def _async_clear_all_issues(self) -> None:
        """Delete every Repair issue we raised (e.g. feature turned off)."""
        dirty = False
        for port, st in self._state.items():
            if st.get("issue_open"):
                ir.async_delete_issue(
                    self.hass, DOMAIN, _issue_id(self.entry.entry_id, port)
                )
                st["issue_open"] = False
                dirty = True
        if dirty:
            await self._async_save()

    # --- actions invoked by the Repairs fix flow ---------------------------

    async def async_disable_port(self, port: str | int) -> int:
        """Disable every currently-enabled per-port extra for `port`."""
        port = str(port)
        managed = self._port_managed_entities(port)
        disabled = self._disable(managed)
        st = self._state.setdefault(port, _default_port_state())
        st["disabled_uids"] = sorted(set(st.get("disabled_uids") or []) | set(disabled))
        st["down_since"] = None
        if st["issue_open"]:
            ir.async_delete_issue(
                self.hass, DOMAIN, _issue_id(self.entry.entry_id, port)
            )
            st["issue_open"] = False
        await self._async_save()
        _LOGGER.info(
            "Disabled %d entities for down port %s on %s (user-approved)",
            len(disabled),
            port,
            self.coordinator.host,
        )
        return len(disabled)

    async def async_disable_all_flagged(self) -> int:
        """Disable extras for every port with an open Repair issue."""
        total = 0
        for port in self.flagged_ports():
            total += await self.async_disable_port(port)
        return total

    async def async_ignore_port(self, port: str | int) -> None:
        """Keep `port`'s entities; suppress until it goes up then down again."""
        port = str(port)
        st = self._state.setdefault(port, _default_port_state())
        st["ignored"] = True
        st["down_since"] = None
        if st["issue_open"]:
            ir.async_delete_issue(
                self.hass, DOMAIN, _issue_id(self.entry.entry_id, port)
            )
            st["issue_open"] = False
        await self._async_save()

    def flagged_ports(self) -> list[int]:
        return sorted(int(p) for p, st in self._state.items() if st.get("issue_open"))
