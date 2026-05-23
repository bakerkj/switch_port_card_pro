"""Auto-manage per-port entities for Switch Port Card Pro.

Goal: keep the registry small on switches with many unused ports. Each port
keeps its `status` indicator always; every *other* per-port entity that is
currently enabled is a disable candidate when the port is down.

A port is "up" whenever it has a link OR is sourcing PoE — a powered device
with no negotiated link (a camera mid-boot, say) still means the port is in
use, so it is never flagged down nor has its entities disabled.

Policy (hybrid):
  * Auto re-enable (no prompt): a port we disabled, seen "up" for
    `up_restore_cycles` consecutive polls, gets *exactly* the entities we
    disabled re-enabled — nothing more.
  * Suggest disable (never automatic): a port down (no link and no PoE)
    continuously past `down_grace_hours` raises a fixable Repair issue. The
    user chooses to disable that port, disable all flagged ports, or ignore it.
  * Flapping ports are not "long down" ports — they are unstable links, and
    nagging about them (or disabling their entities) is the wrong action. A
    port with `FLAP_MIN_TRANSITIONS`+ up<->down transitions inside the trailing
    `down_grace_hours / 2` is treated as flapping: the Repair is not raised
    (and an already-open one is cleared). This self-corrects once the link
    goes quiet for that half-grace window. The user can also explicitly mute
    a port via the Repair flow ("allow flapping"); that mute persists until
    the port is cleanly up for `up_restore_cycles` consecutive polls.

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
    PORT_UNMANAGED_KEYS,
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

# A port counts as "flapping" once it records this many up<->down transitions
# inside the trailing `down_grace_hours / 2`. 2 means a single bounce —
# `down->up->down` or `up->down->up` — within the half-grace window is enough
# to treat the link as unstable rather than a stale dead port. A genuinely
# dead/unused port only ever goes `up->down` once (1 transition) and stays
# down, so it is never misclassified. The window is derived from the existing
# grace option rather than adding another knob.
FLAP_MIN_TRANSITIONS = 2


def _issue_id(entry_id: str, port: int | str) -> str:
    return f"port_down_{entry_id}_{port}"


def _default_port_state() -> dict[str, Any]:
    return {
        "down_since": None,  # epoch secs the current down episode began
        "ignored": False,  # user chose "ignore" — suppress until next up→down
        "disabled_uids": [],  # unique_ids WE disabled (precise restore set)
        "issue_open": False,  # a Repair issue is currently raised
        "transitions": [],  # epoch secs of recent up<->down changes (pruned)
        "last_status": None,  # last observed "on"/"off" (transition detection)
        "flap_muted": False,  # user chose "allow flapping" — mute until stable
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
            if m.group(2) in PORT_UNMANAGED_KEYS:
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

    # --- liveness ----------------------------------------------------------

    @staticmethod
    def _poe_active(pdata: dict[str, Any]) -> bool:
        """True if the port is sourcing PoE, regardless of link state.

        ``poe_status`` 3 is the standard ``deliveringPower`` enum value; a
        positive measured ``poe_power`` is the same conclusion from the power
        side. Either means a powered device is attached and drawing, so the
        port is in use even with no link. Mirrors the ``poe_enabled`` logic in
        the per-port sensor.
        """
        if pdata.get("poe_status") == 3:
            return True
        try:
            return float(pdata.get("poe_power") or 0) > 0
        except (TypeError, ValueError):
            return False

    # --- flap detection ----------------------------------------------------

    @staticmethod
    def _note_transition(
        st: dict[str, Any], status_on: bool | None, now: float, grace_s: float
    ) -> bool:
        """Record an up<->down change and prune to the half-grace window.

        Returns True if `st` was mutated (so the caller can mark dirty).
        `status_on` is None when the port has no usable data this poll — we
        neither record a transition nor lose the prior state.
        """
        if status_on is None:
            return False
        cur = "on" if status_on else "off"
        prev = st.get("last_status")
        window = grace_s / 2.0
        trans: list[float] = list(st.get("transitions") or [])
        changed = False
        if prev is not None and cur != prev:
            trans.append(now)
            changed = True
        kept = [t for t in trans if now - t <= window]
        if kept != (st.get("transitions") or []):
            st["transitions"] = kept
            changed = True
        if prev != cur:
            st["last_status"] = cur
            changed = True
        return changed

    @staticmethod
    def _is_flapping(st: dict[str, Any], now: float, grace_s: float) -> bool:
        """True if the port changed state often enough inside grace/2."""
        window = grace_s / 2.0
        recent = [t for t in (st.get("transitions") or []) if now - t <= window]
        return len(recent) >= FLAP_MIN_TRANSITIONS

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
                raw_status = pdata.get("status")
                link_on = raw_status == "on"
                link_known = raw_status in ("on", "off")
                poe_active = self._poe_active(pdata)
                # A port counts as "up" when it has a link OR is sourcing PoE.
                # A powered device with no negotiated link (e.g. a camera mid-
                # boot, or one that simply never links) still means the port is
                # in use, so it must not be flagged down or have its entities
                # disabled. PoE delivering is itself a definite "up" signal, so
                # the port's state is known even when the link OID is unusable.
                active = link_on or poe_active
                active_known = link_known or poe_active
                st = self._state.setdefault(port, _default_port_state())
                recorded = list(st.get("disabled_uids") or [])

                # Track up<->down transitions for flap detection. Done first so
                # every branch (incl. the early continues below) keeps the
                # window current. Unknown state records nothing.
                if self._note_transition(
                    st, active if active_known else None, now, grace_s
                ):
                    dirty = True

                if active:
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
                    self._on_streak[port] = self._on_streak.get(port, 0) + 1
                    if self._on_streak[port] >= up_cycles:
                        if recorded:
                            n = self._reenable(port, recorded)
                            st["disabled_uids"] = []
                            dirty = True
                            if n:
                                _LOGGER.info(
                                    "Port %s on %s back up — re-enabled %d entities",
                                    port,
                                    self.coordinator.host,
                                    n,
                                )
                        if st.get("flap_muted"):
                            st["flap_muted"] = False
                            dirty = True
                            _LOGGER.info(
                                "Port %s on %s stable for %d cycles — "
                                "flap mute cleared",
                                port,
                                self.coordinator.host,
                                up_cycles,
                            )
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
                # Flapping / user-muted ports are unstable links, not stale
                # dead ports: never nag, and retract a warning raised before
                # the flapping became apparent.
                suppressed = bool(st.get("flap_muted")) or self._is_flapping(
                    st, now, grace_s
                )
                if st["issue_open"]:
                    if suppressed:
                        ir.async_delete_issue(
                            self.hass, DOMAIN, _issue_id(self.entry.entry_id, port)
                        )
                        st["issue_open"] = False
                        dirty = True
                elif (
                    st["down_since"] is not None
                    and now - st["down_since"] >= grace_s
                    and not suppressed
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

    async def async_allow_flap(self, port: str | int) -> None:
        """Mute `port`: keep its entities, never warn while it flaps.

        Unlike ``ignore`` (which re-arms on the next up→down), this stays
        muted until the port is cleanly up for ``up_restore_cycles``
        consecutive polls — i.e. the link genuinely stabilised.
        """
        port = str(port)
        st = self._state.setdefault(port, _default_port_state())
        st["flap_muted"] = True
        st["down_since"] = None
        if st["issue_open"]:
            ir.async_delete_issue(
                self.hass, DOMAIN, _issue_id(self.entry.entry_id, port)
            )
            st["issue_open"] = False
        await self._async_save()
        _LOGGER.info(
            "Port %s on %s muted as flapping (user-approved)",
            port,
            self.coordinator.host,
        )

    def flagged_ports(self) -> list[int]:
        return sorted(int(p) for p, st in self._state.items() if st.get("issue_open"))
