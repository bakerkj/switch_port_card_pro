"""Async sensor platform for Switch Port Card Pro."""

from __future__ import annotations
import logging
import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from homeassistant.helpers import device_registry
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    UnitOfDataRate,
    UnitOfTemperature,
    UnitOfTime,
    PERCENTAGE,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .const import (
    DOMAIN,
    SNMP_VERSION_TO_MP_MODEL,
    HP_OID_CPU_REALTIME,
    HP_OID_MEMORY_USED,
    HP_OID_MEMORY_TOTAL,
    HP_OID_POE_POWER,
    HP_OID_POE_POWER_LEGACY,
    HP_OID_POE_STATUS,
    HP_OID_POE_CLASS,
    HP_OID_FIRMWARE,
    HP_MANUFACTURER_KEYWORDS,
    CONF_OID_IFHCINOCTETS,
    CONF_OID_IFHCOUTOCTETS,
    CONF_OID_IFINERRORS,
    CONF_OID_IFOUTERRORS,
    CONF_OID_IFINDISCARDS,
    CONF_OID_IFOUTDISCARDS,
    CONF_OID_IFADMINSTATUS,
    CONF_OID_IFLASTCHANGE,
    CONF_OID_SYSUPTIME,
    CONF_OID_POE_BUDGET_TOTAL,
    CONF_OID_ENT_SENSOR_TYPE,
    CONF_OID_ENT_SENSOR_VALUE,
    CONF_OID_ENT_SENSOR_OPSTATUS,
    CONF_OID_ENT_PHYSICAL_NAME,
)
from .snmp_helper import (
    async_snmp_get,
    async_snmp_walk,
    async_snmp_bulk,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class SwitchPortData:
    ports: dict[str, dict[str, Any]]
    bandwidth_mbps: float
    system: dict[str, Any]


class SwitchPortCoordinator(DataUpdateCoordinator[SwitchPortData]):
    def __init__(
        self,
        hass: HomeAssistant,
        host: str,
        community: str,
        snmp_port,
        ports: list[int],
        base_oids: dict[str, str],
        system_oids: dict[str, str],
        snmp_version: str,
        include_vlans: bool,
        update_seconds: int,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{host}",
            update_interval=timedelta(seconds=update_seconds),
        )
        self.host = host
        self.community = community
        self.snmp_port = snmp_port
        self.ports = ports
        self.base_oids = base_oids
        self.system_oids = system_oids
        self.include_vlans = include_vlans
        self.mp_model = SNMP_VERSION_TO_MP_MODEL.get(snmp_version, 1)
        self.port_mapping = {}
        self.update_seconds = update_seconds
        self._last_total_bytes = 0
        # if_index → (rx_bytes, tx_bytes, monotonic_time) — used to compute live per-port rates centrally
        self._prev_port_counters: dict[int, tuple[int, int, float]] = {}

    async def _async_update_data(self) -> SwitchPortData:
        try:
            if not self.port_mapping:
                # Fallback if detection somehow failed in __init__
                self.port_mapping = {
                    p: {
                        "if_index": p,
                        "name": f"Port {p}",
                        "is_sfp": False,
                        "is_copper": True,
                    }
                    for p in self.ports
                }
            # === PORT WALKS ===
            oids_to_walk = [
                "rx",
                "tx",
                "status",
                "speed",
                "name",
                "poe_power",
                "poe_status",
                "poe_class",
                "port_custom",
            ]
            if self.include_vlans and self.base_oids.get("vlan"):
                oids_to_walk.append("vlan")

            tasks = [
                async_snmp_walk(
                    self.hass,
                    self.host,
                    self.community,
                    self.snmp_port,
                    self.base_oids[k],
                    mp_model=self.mp_model,
                )
                for k in oids_to_walk
                if self.base_oids.get(k)
            ]
            # Always walk standard IF-MIB OIDs — not user-configurable
            (
                hc_rx_raw,
                hc_tx_raw,
                in_errors_raw,
                out_errors_raw,
                in_discards_raw,
                out_discards_raw,
                admin_status_raw,
                last_change_raw,
                *results,
            ) = await asyncio.gather(
                async_snmp_walk(
                    self.hass,
                    self.host,
                    self.community,
                    self.snmp_port,
                    CONF_OID_IFHCINOCTETS,
                    mp_model=self.mp_model,
                ),
                async_snmp_walk(
                    self.hass,
                    self.host,
                    self.community,
                    self.snmp_port,
                    CONF_OID_IFHCOUTOCTETS,
                    mp_model=self.mp_model,
                ),
                async_snmp_walk(
                    self.hass,
                    self.host,
                    self.community,
                    self.snmp_port,
                    CONF_OID_IFINERRORS,
                    mp_model=self.mp_model,
                ),
                async_snmp_walk(
                    self.hass,
                    self.host,
                    self.community,
                    self.snmp_port,
                    CONF_OID_IFOUTERRORS,
                    mp_model=self.mp_model,
                ),
                async_snmp_walk(
                    self.hass,
                    self.host,
                    self.community,
                    self.snmp_port,
                    CONF_OID_IFINDISCARDS,
                    mp_model=self.mp_model,
                ),
                async_snmp_walk(
                    self.hass,
                    self.host,
                    self.community,
                    self.snmp_port,
                    CONF_OID_IFOUTDISCARDS,
                    mp_model=self.mp_model,
                ),
                async_snmp_walk(
                    self.hass,
                    self.host,
                    self.community,
                    self.snmp_port,
                    CONF_OID_IFADMINSTATUS,
                    mp_model=self.mp_model,
                ),
                async_snmp_walk(
                    self.hass,
                    self.host,
                    self.community,
                    self.snmp_port,
                    CONF_OID_IFLASTCHANGE,
                    mp_model=self.mp_model,
                ),
                *tasks,
                return_exceptions=True,
            )

            walk_map: dict[str, dict[str, str]] = {}
            for key, result in zip(
                [k for k in oids_to_walk if self.base_oids.get(k)], results
            ):
                if isinstance(result, Exception):
                    _LOGGER.error("SNMP walk failed for %s: %s", key, result)
                    walk_map[key] = {}
                elif not result:
                    #     _LOGGER.warning("SNMP walk empty for %s → using defaults", key) # surpress unneeded log
                    walk_map[key] = {}
                else:
                    walk_map[key] = result

            # HP PoE auto-detection: fill in missing PoE walk results when OIDs are not manually configured.
            manufacturer = getattr(self, "manufacturer", "").lower()
            is_hp = any(m in manufacturer for m in HP_MANUFACTURER_KEYWORDS)
            is_unknown_manufacturer = not manufacturer or manufacturer == "unknown"
            if is_hp or is_unknown_manufacturer:
                hp_poe_keys: list[str] = []
                hp_poe_tasks = []
                for key, oid in (
                    ("poe_power", HP_OID_POE_POWER),
                    ("poe_status", HP_OID_POE_STATUS),
                    ("poe_class", HP_OID_POE_CLASS),
                ):
                    if not self.base_oids.get(key, "").strip():
                        hp_poe_keys.append(key)
                        hp_poe_tasks.append(
                            async_snmp_walk(
                                self.hass,
                                self.host,
                                self.community,
                                self.snmp_port,
                                oid,
                                mp_model=self.mp_model,
                            )
                        )
                if hp_poe_tasks:
                    hp_poe_results = await asyncio.gather(
                        *hp_poe_tasks, return_exceptions=True
                    )
                    for key, result in zip(hp_poe_keys, hp_poe_results):
                        walk_map[key] = (
                            result
                            if not isinstance(result, Exception) and result
                            else {}
                        )
                # Older HP switches (e.g. 2520) use column 3 for mW instead of column 8 — fall back if primary returned empty.
                if (
                    not walk_map.get("poe_power")
                    and not self.base_oids.get("poe_power", "").strip()
                ):
                    legacy = await async_snmp_walk(
                        self.hass,
                        self.host,
                        self.community,
                        self.snmp_port,
                        HP_OID_POE_POWER_LEGACY,
                        mp_model=self.mp_model,
                    )
                    if legacy:
                        walk_map["poe_power"] = legacy

            def parse(raw: dict[str, str], int_val: bool = True) -> dict[int, Any]:
                out = {}
                for oid, val in raw.items():
                    try:
                        idx = int(oid.split(".")[-1])
                        out[idx] = int(val) if int_val else val
                    except (ValueError, IndexError):
                        continue
                return out

            rx = parse(walk_map.get("rx", {}))
            tx = parse(walk_map.get("tx", {}))
            hc_rx = parse(hc_rx_raw if not isinstance(hc_rx_raw, Exception) else {})
            hc_tx = parse(hc_tx_raw if not isinstance(hc_tx_raw, Exception) else {})
            in_errors = parse(
                in_errors_raw if not isinstance(in_errors_raw, Exception) else {}
            )
            out_errors = parse(
                out_errors_raw if not isinstance(out_errors_raw, Exception) else {}
            )
            in_discards = parse(
                in_discards_raw if not isinstance(in_discards_raw, Exception) else {}
            )
            out_discards = parse(
                out_discards_raw if not isinstance(out_discards_raw, Exception) else {}
            )
            admin_status = parse(
                admin_status_raw if not isinstance(admin_status_raw, Exception) else {}
            )
            last_change = parse(
                last_change_raw if not isinstance(last_change_raw, Exception) else {}
            )
            # sysUpTime for computing time-since-last-change
            sys_uptime_raw = await async_snmp_get(
                self.hass,
                self.host,
                self.community,
                self.snmp_port,
                CONF_OID_SYSUPTIME,
                mp_model=self.mp_model,
            )
            try:
                sys_uptime_ticks = (
                    int(sys_uptime_raw) if sys_uptime_raw is not None else None
                )
            except (ValueError, TypeError):
                sys_uptime_ticks = None
            status = parse(walk_map.get("status", {}))
            speed = parse(walk_map.get("speed", {}))
            name = parse(walk_map.get("name", {}), int_val=False)
            vlan = parse(walk_map.get("vlan", {}))
            poe_power = parse(walk_map.get("poe_power", {}))
            poe_status = parse(walk_map.get("poe_status", {}))
            poe_class_data = parse(walk_map.get("poe_class", {}))
            port_custom = parse(walk_map.get("port_custom", {}))

            def _port_in_bitmap(raw_value: str, bridge_port: int) -> bool:
                """Return True if bridge_port (1-indexed) bit is set in an SNMP OctetString bitmap."""
                try:
                    hex_str = raw_value.replace(" ", "")
                    if hex_str.startswith(("0x", "0X")):
                        hex_str = hex_str[2:]
                    bitmap_bytes = bytes.fromhex(hex_str)
                    byte_index = (bridge_port - 1) // 8
                    bit_index = 7 - ((bridge_port - 1) % 8)
                    return byte_index < len(bitmap_bytes) and bool(
                        (bitmap_bytes[byte_index] >> bit_index) & 1
                    )
                except Exception:
                    return False

            # Build if_index → bridge_port mapping and fetch per-VLAN egress bitmaps in parallel.
            # dot1qPvid is indexed by dot1dBasePort (RFC 4363), not ifIndex.
            ifindex_to_bridge_port: dict[int, int] = {}
            vlan_egress_bitmaps: dict[int, str] = {}
            if self.include_vlans and self.base_oids.get("vlan"):
                bridge_walk, egress_walk = await asyncio.gather(
                    async_snmp_walk(
                        self.hass,
                        self.host,
                        self.community,
                        self.snmp_port,
                        "1.3.6.1.2.1.17.1.4.1.2",  # dot1dBasePortIfIndex (RFC 4188)
                        mp_model=self.mp_model,
                    ),
                    async_snmp_walk(
                        self.hass,
                        self.host,
                        self.community,
                        self.snmp_port,
                        "1.3.6.1.2.1.17.7.1.4.2.1.4",  # dot1qVlanCurrentEgressPorts (RFC 4363)
                        mp_model=self.mp_model,
                    ),
                )
                for b_oid, b_val in bridge_walk.items():
                    try:
                        b_port = int(b_oid.split(".")[-1])
                        b_ifidx = int(b_val)
                        ifindex_to_bridge_port[b_ifidx] = b_port
                    except (ValueError, IndexError):
                        continue
                for e_oid, e_val in egress_walk.items():
                    try:
                        vlan_id = int(e_oid.split(".")[-1])
                        vlan_egress_bitmaps[vlan_id] = e_val
                    except (ValueError, IndexError):
                        continue

            ports_data: dict[str, dict[str, Any]] = {}
            total_rx = total_tx = total_poe_mw = 0
            now = time.monotonic()
            new_port_counters: dict[int, tuple[int, int, float]] = {}

            for port in self.ports:
                p = str(port)
                port_info = self.port_mapping.get(port) or {}
                if_index = port_info.get(
                    "if_index", port
                )  # fallback to port number if no mapping
                ports_data[p] = {
                    "status": "off",
                    "speed": 0,
                    "rx": 0,
                    "tx": 0,
                    "rx_bps_live": 0,
                    "tx_bps_live": 0,
                    "name": f"Port {port}",
                    "vlan": None,
                    "vlan_id_list": [],
                    "poe_power": 0,
                    "poe_status": 0,
                    "poe_class": None,
                    "port_custom": 0,
                    "admin_status": None,
                    "in_errors": 0,
                    "out_errors": 0,
                    "in_discards": 0,
                    "out_discards": 0,
                    "last_change_seconds": None,
                }

                # For VLAN: dot1qPvid is indexed by bridge port, not ifIndex (RFC 4363).
                vlan_bridge_port = ifindex_to_bridge_port.get(if_index, if_index)
                vlan_id_list = (
                    sorted(
                        vid
                        for vid, bitmap in vlan_egress_bitmaps.items()
                        if _port_in_bitmap(bitmap, vlan_bridge_port)
                    )
                    if vlan_egress_bitmaps
                    else []
                )

                if any(if_index in t for t in (status, speed, rx, tx, poe_power)):
                    HighLowSpeed = speed.get(if_index, 0)
                    if (
                        HighLowSpeed < 100000
                    ):  # check if we use the 32 or 64 bit variant
                        HighLowSpeed = HighLowSpeed * 1000000  # convert to bps
                    ports_data[p].update(
                        {
                            "status": "on" if status.get(if_index, 2) == 1 else "off",
                            "speed": HighLowSpeed,
                            "rx": (
                                hc_rx.get(if_index)
                                if if_index in hc_rx
                                else rx.get(if_index, 0)
                            ),
                            "tx": (
                                hc_tx.get(if_index)
                                if if_index in hc_tx
                                else tx.get(if_index, 0)
                            ),
                            "name": name.get(if_index, f"Port {port}"),
                            "vlan": vlan.get(vlan_bridge_port),
                            "vlan_id_list": vlan_id_list,
                            "poe_power": poe_power.get(if_index, 0),
                            "poe_status": poe_status.get(if_index, 0),
                            "poe_class": poe_class_data.get(if_index),
                            "port_custom": port_custom.get(if_index, 0),
                            "admin_status": {1: "up", 2: "down"}.get(
                                admin_status.get(if_index)
                            ),
                            "in_errors": in_errors.get(if_index, 0),
                            "out_errors": out_errors.get(if_index, 0),
                            "in_discards": in_discards.get(if_index, 0),
                            "out_discards": out_discards.get(if_index, 0),
                            "last_change_seconds": (
                                round(
                                    (sys_uptime_ticks - last_change.get(if_index)) / 100
                                )
                                if sys_uptime_ticks is not None
                                and if_index in last_change
                                else None
                            ),
                        }
                    )

                # Live per-port rate (centralized — replaces per-sensor delta tracking).
                # Uses HC counters when present, otherwise falls back to 32-bit and handles wrap.
                cur_rx = ports_data[p]["rx"]
                cur_tx = ports_data[p]["tx"]
                using_hc = if_index in hc_rx or if_index in hc_tx
                prev = self._prev_port_counters.get(if_index)
                if prev is not None:
                    prev_rx, prev_tx, prev_ts = prev
                    dt = now - prev_ts
                    if dt > 0:
                        delta_rx = cur_rx - prev_rx
                        delta_tx = cur_tx - prev_tx
                        # Wrap handling for 32-bit counters near the boundary
                        if delta_rx < 0 and not using_hc and prev_rx > 3_000_000_000:
                            delta_rx = (4_294_967_296 - prev_rx) + cur_rx
                        if delta_tx < 0 and not using_hc and prev_tx > 3_000_000_000:
                            delta_tx = (4_294_967_296 - prev_tx) + cur_tx
                        # Negative remaining => counter reset; drop the rate this tick
                        rx_bps_live = int(delta_rx * 8 / dt) if delta_rx >= 0 else 0
                        tx_bps_live = int(delta_tx * 8 / dt) if delta_tx >= 0 else 0
                        MAX_SAFE_BPS = 20_000_000_000
                        if rx_bps_live < 0 or rx_bps_live > MAX_SAFE_BPS:
                            rx_bps_live = 0
                        if tx_bps_live < 0 or tx_bps_live > MAX_SAFE_BPS:
                            tx_bps_live = 0
                        ports_data[p]["rx_bps_live"] = rx_bps_live
                        ports_data[p]["tx_bps_live"] = tx_bps_live
                new_port_counters[if_index] = (cur_rx, cur_tx, now)

                total_rx += cur_rx
                total_tx += cur_tx
                total_poe_mw += poe_power.get(if_index, 0)

            # Replace counters dict so removed ports stop accumulating stale state.
            self._prev_port_counters = new_port_counters

            # compute current totals (these are lifetime counters) in bytes
            current_total_bytes = total_rx + total_tx
            # compute delta from last poll
            delta_total = current_total_bytes - getattr(self, "_last_total_bytes", 0)
            # Handle counter wrap or reset
            if delta_total < 0:
                using_hc = bool(hc_rx or hc_tx)
                if using_hc:
                    # 64-bit: treat negative as reset
                    delta_total = 0
                else:
                    # 32-bit wrap at 2^32 bytes (~4GB)
                    MAX32 = 4_294_967_296
                    if getattr(self, "_last_total_bytes", 0) > 3_000_000_000:
                        delta_total = (
                            MAX32 - self._last_total_bytes
                        ) + current_total_bytes
                    else:
                        delta_total = 0
            # prefer using configured stable interval if available
            delta_time = getattr(self, "update_seconds", 20)
            if delta_time <= 0:
                delta_time = 20
            # Mbps: megabits per second
            bandwidth_mbps = round((delta_total * 8) / (1024 * 1024) / delta_time, 2)

            # store for next run
            self._last_total_bytes = current_total_bytes
            # === SYSTEM OIDs ===
            raw_system = await async_snmp_bulk(
                self.hass,
                self.host,
                self.community,
                self.snmp_port,
                [oid for oid in self.system_oids.values() if oid],
                mp_model=self.mp_model,
            )

            def get(oid_key: str) -> str | None:
                oid = self.system_oids.get(oid_key)
                return next(
                    (v for k, v in raw_system.items() if oid and k.startswith(oid)),
                    None,
                )

            memory_raw = get("memory")
            memory_total_raw = get("memory_total")
            if memory_raw is not None and memory_total_raw is not None:
                try:
                    memory_value = (
                        round(float(memory_raw) / float(memory_total_raw) * 100, 1)
                        if float(memory_total_raw) > 0
                        else None
                    )
                except (ValueError, TypeError):
                    memory_value = memory_raw
            else:
                memory_value = memory_raw

            system = {
                "cpu": get("cpu"),
                "memory": memory_value,
                "hostname": get("hostname"),
                "uptime": get("uptime"),
                "firmware": get("firmware"),
                "poe_total_watts": (
                    round(total_poe_mw / 1000.0, 2) if total_poe_mw > 0 else None
                ),
                "custom": get("custom"),
            }

            # HP/Aruba auto-detection: fill in cpu/memory/firmware if not manually configured.
            # Also attempt when manufacturer is unknown (handles entries created before
            # manufacturer detection was added — OIDs return None on non-HP switches).
            # Note: is_hp / is_unknown_manufacturer already computed above for PoE detection.
            if is_hp or is_unknown_manufacturer:
                if (
                    not self.system_oids.get("cpu", "").strip()
                    and system.get("cpu") is None
                ):
                    hp_cpu = await async_snmp_get(
                        self.hass,
                        self.host,
                        self.community,
                        self.snmp_port,
                        HP_OID_CPU_REALTIME,
                        mp_model=self.mp_model,
                    )
                    if hp_cpu is not None:
                        system["cpu"] = hp_cpu
                if (
                    not self.system_oids.get("memory", "").strip()
                    and system.get("memory") is None
                ):
                    hp_used, hp_total = await asyncio.gather(
                        async_snmp_get(
                            self.hass,
                            self.host,
                            self.community,
                            self.snmp_port,
                            HP_OID_MEMORY_USED,
                            mp_model=self.mp_model,
                        ),
                        async_snmp_get(
                            self.hass,
                            self.host,
                            self.community,
                            self.snmp_port,
                            HP_OID_MEMORY_TOTAL,
                            mp_model=self.mp_model,
                        ),
                    )
                    if hp_used is not None and hp_total is not None:
                        try:
                            system["memory"] = (
                                round(float(hp_used) / float(hp_total) * 100, 1)
                                if float(hp_total) > 0
                                else None
                            )
                        except (ValueError, TypeError):
                            pass
                if (
                    not self.system_oids.get("firmware", "").strip()
                    and system.get("firmware") is None
                ):
                    hp_fw = await async_snmp_get(
                        self.hass,
                        self.host,
                        self.community,
                        self.snmp_port,
                        HP_OID_FIRMWARE,
                        mp_model=self.mp_model,
                    )
                    if hp_fw is not None:
                        system["firmware"] = str(hp_fw).strip() or system["firmware"]

            # PoE budget (RFC 3621) + ENTITY-SENSOR-MIB (RFC 3433) — all in parallel
            (
                poe_budget_raw,
                ent_type_raw,
                ent_value_raw,
                ent_opstatus_raw,
                ent_name_raw,
            ) = await asyncio.gather(
                async_snmp_walk(
                    self.hass,
                    self.host,
                    self.community,
                    self.snmp_port,
                    CONF_OID_POE_BUDGET_TOTAL,
                    mp_model=self.mp_model,
                ),
                async_snmp_walk(
                    self.hass,
                    self.host,
                    self.community,
                    self.snmp_port,
                    CONF_OID_ENT_SENSOR_TYPE,
                    mp_model=self.mp_model,
                ),
                async_snmp_walk(
                    self.hass,
                    self.host,
                    self.community,
                    self.snmp_port,
                    CONF_OID_ENT_SENSOR_VALUE,
                    mp_model=self.mp_model,
                ),
                async_snmp_walk(
                    self.hass,
                    self.host,
                    self.community,
                    self.snmp_port,
                    CONF_OID_ENT_SENSOR_OPSTATUS,
                    mp_model=self.mp_model,
                ),
                async_snmp_walk(
                    self.hass,
                    self.host,
                    self.community,
                    self.snmp_port,
                    CONF_OID_ENT_PHYSICAL_NAME,
                    mp_model=self.mp_model,
                ),
                return_exceptions=True,
            )

            def _first_int(raw) -> int | None:
                if isinstance(raw, Exception) or not raw:
                    return None
                for v in raw.values():
                    try:
                        return int(v)
                    except (ValueError, TypeError):
                        pass
                return None

            system["poe_budget_watts"] = _first_int(poe_budget_raw)

            # Parse ENTITY-SENSOR-MIB walks: OID suffix is the entity index
            def _ent_parse(raw) -> dict[int, str]:
                if isinstance(raw, Exception) or not raw:
                    return {}
                out = {}
                for oid, val in raw.items():
                    try:
                        out[int(oid.split(".")[-1])] = val
                    except (ValueError, IndexError):
                        continue
                return out

            ent_types = _ent_parse(ent_type_raw)
            ent_values = _ent_parse(ent_value_raw)
            ent_opstatus = _ent_parse(ent_opstatus_raw)
            ent_names = _ent_parse(ent_name_raw)

            # Temperature: entPhySensorType == 8 (celsius, RFC 3433 §4).
            # Assumes scale=units and precision=0 — standard for switch thermal sensors.
            temperature_celsius = None
            for entity_id, sensor_type in ent_types.items():
                try:
                    if int(sensor_type) == 8:
                        raw_val = ent_values.get(entity_id)
                        if raw_val is not None:
                            temperature_celsius = float(raw_val)
                            break
                except (ValueError, TypeError):
                    continue

            # Fans: entPhySensorType == 1 (other — used by HP/Aruba fans) or 10 (rpm).
            # Use entPhySensorOperStatus: 1=ok, 2=unavailable.
            fans = []
            for entity_id, sensor_type in sorted(ent_types.items()):
                try:
                    if int(sensor_type) in (1, 10):
                        op_status = int(ent_opstatus.get(entity_id, 0))
                        fan_name = str(ent_names.get(entity_id, f"Fan {entity_id}"))
                        fans.append(
                            {
                                "entity_id": entity_id,
                                "name": fan_name,
                                "oper_status": op_status,
                                "ok": op_status == 1,
                            }
                        )
                except (ValueError, TypeError):
                    continue

            system["temperature_celsius"] = temperature_celsius
            system["fans"] = fans

            return SwitchPortData(
                ports=ports_data, bandwidth_mbps=bandwidth_mbps, system=system
            )

        except Exception as err:
            _LOGGER.exception("Update failed for %s", self.host)
            raise UpdateFailed(str(err)) from err


# =============================================================================
# Entities
# =============================================================================
class SwitchPortBaseEntity(SensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator: SwitchPortCoordinator, entry_id: str) -> None:
        self.coordinator = coordinator
        self.entry_id = entry_id

        # STATIC DEVICE INFO (never changes)
        sys_info = coordinator.data.system if coordinator.data else {}
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry_id}_{self.coordinator.host}")},
            connections=set(),
            name=f"Switch {self.coordinator.host}",  # temporary before SNMP poll
            manufacturer=sys_info.get("manufacturer") or "Generic SNMP",
            model=sys_info.get("model") or f"{entry_id}",  # updated dynamically later
            sw_version=sys_info.get("firmware"),  # updated dynamically later
        )

    @property
    def available(self) -> bool:
        """Return True only if we have data."""
        try:
            return (
                self.coordinator.last_update_success
                and self.coordinator.data is not None
            )
        except Exception:
            _LOGGER.error("Entity not available")

    async def async_will_remove_from_hass(self) -> None:
        if hasattr(self, "_unsub_coordinator") and self._unsub_coordinator:
            self._unsub_coordinator()
        if hasattr(self, "_unsub_devinfo") and self._unsub_devinfo:
            self._unsub_devinfo()
        await super().async_will_remove_from_hass()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._unsub_coordinator = self.coordinator.async_add_listener(
            self.async_write_ha_state
        )

        @callback
        def _update_device_info() -> None:
            """
            Update HA device registry with dynamic system info.
            """
            try:
                if not self.coordinator.data:
                    return

                system = self.coordinator.data.system

                raw_hostname = system.get("hostname") or ""
                device_name = raw_hostname.strip() or f"Switch {self.coordinator.host}"
                model = system.get("model") or ""
                firmware = system.get("firmware")

                # Update device registry entry
                dev_reg = device_registry.async_get(self.hass)
                device_entry = dev_reg.async_get_device(
                    identifiers={(DOMAIN, f"{self.entry_id}_{self.coordinator.host}")}
                )
                if device_entry:
                    dev_reg.async_update_device(
                        device_entry.id,
                        name=device_name,
                        model=model,
                        sw_version=firmware,
                    )
            except Exception as err:
                _LOGGER.error(
                    "Entity update failed for %s with error %s", self.host, err
                )

        # Run on each coordinator update
        self._unsub_devinfo = self.coordinator.async_add_listener(_update_device_info)

        # Also run immediately on entity creation (if we already have data)
        if self.coordinator.data:
            _update_device_info()


# --- Aggregate and Port Sensors ---
class TotalPoESensor(SwitchPortBaseEntity):
    _attr_name = "Total PoE Power"
    _attr_native_unit_of_measurement = "W"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: SwitchPortCoordinator, entry_id: str) -> None:
        super().__init__(coordinator, entry_id)
        self._attr_unique_id = f"{entry_id}_total_poe"

    @property
    def native_value(self) -> float | None:
        if not self.coordinator.data:
            return 0
        try:
            val = self.coordinator.data.system.get("poe_total_watts")
            return float(val) if val is not None else None
        except (ValueError, TypeError):
            return 0


class PoEBudgetTotalSensor(SwitchPortBaseEntity):
    """Total PoE power budget from pethMainPsePower (RFC 3621)."""

    _attr_name = "PoE Budget Total"
    _attr_native_unit_of_measurement = "W"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:lightning-bolt"

    def __init__(self, coordinator: SwitchPortCoordinator, entry_id: str) -> None:
        super().__init__(coordinator, entry_id)
        self._attr_unique_id = f"{entry_id}_poe_budget_total"

    @property
    def native_value(self) -> float | None:
        if not self.coordinator.data:
            return None
        return self.coordinator.data.system.get("poe_budget_watts")


class BandwidthSensor(SwitchPortBaseEntity):
    """Total bandwidth sensor."""

    _attr_name = "Total Bandwidth"
    _attr_native_unit_of_measurement = UnitOfDataRate.MEGABITS_PER_SECOND
    _attr_device_class = SensorDeviceClass.DATA_RATE
    _attr_icon = "mdi:speedometer"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_unique_id_suffix = "total_bandwidth_mbps"

    def __init__(self, coordinator: SwitchPortCoordinator, entry_id: str) -> None:
        super().__init__(coordinator, entry_id)
        self._attr_unique_id = f"{entry_id}_{self._attr_unique_id_suffix}"

    @property
    def native_value(self) -> float | None:
        """Return the state of the sensor."""
        if not self.coordinator.data:
            return 0
        try:
            val = self.coordinator.data.bandwidth_mbps
            return float(val) if val is not None else None
        except (ValueError, TypeError):
            return 0


class FirmwareSensor(SwitchPortBaseEntity):
    _attr_name = "Firmware"
    _attr_icon = "mdi:chip"

    def __init__(self, coordinator: SwitchPortCoordinator, entry_id: str) -> None:
        super().__init__(coordinator, entry_id)
        self._attr_unique_id = f"{entry_id}_firmware"

    @property
    def native_value(self) -> str | None:
        if not self.coordinator.data:
            return ""
        try:
            return self.coordinator.data.system.get("firmware")
        except (ValueError, TypeError):
            return ""


# =============================================================================
# Per-port device pattern — each port is a sub-device of the switch
# =============================================================================
class SwitchPortPerPortBaseEntity(SensorEntity):
    """Base for entities attached to a per-port sub-device.

    Each port becomes its own device in the registry, linked to the parent
    switch via `via_device`. Mirrors the AP/radio device pattern from
    ha-aruba-ap so per-port data can be browsed and consumed individually.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self, coordinator: SwitchPortCoordinator, entry_id: str, port: int
    ) -> None:
        self.coordinator = coordinator
        self.entry_id = entry_id
        self.port = str(port)
        self._port_int = int(port)
        self._unsub: Callable[[], None] | None = None
        self._unsub_devinfo: Callable[[], None] | None = None

        switch_identifier = (DOMAIN, f"{entry_id}_{coordinator.host}")
        port_identifier = (DOMAIN, f"{entry_id}_{coordinator.host}_port_{port}")
        port_info = coordinator.port_mapping.get(int(port), {}) or {}
        port_name = (
            self._port_data().get("name") if coordinator.data else port_info.get("name")
        )
        switch_label = self._switch_label()
        device_name = f"{switch_label} / Port {port}"
        if port_name and port_name != f"Port {port}":
            device_name = f"{switch_label} / Port {port} ({port_name})"

        manufacturer = getattr(coordinator, "manufacturer", None) or "Generic SNMP"
        model_bits = []
        if port_info.get("is_sfp"):
            model_bits.append("SFP")
        elif port_info.get("is_copper"):
            model_bits.append("Copper")
        if port_info.get("if_descr"):
            model_bits.append(port_info["if_descr"])
        model = " / ".join(model_bits) if model_bits else None

        self._attr_device_info = DeviceInfo(
            identifiers={port_identifier},
            name=device_name,
            manufacturer=manufacturer,
            model=model,
            via_device=switch_identifier,
        )

    def _switch_label(self) -> str:
        if self.coordinator.data:
            hostname = (self.coordinator.data.system.get("hostname") or "").strip()
            if hostname:
                return hostname
        return (
            getattr(self.coordinator, "device_name", None)
            or f"Switch {self.coordinator.host}"
        )

    def _port_data(self) -> dict[str, Any]:
        if not self.coordinator.data:
            return {}
        return self.coordinator.data.ports.get(self.port, {}) or {}

    @property
    def available(self) -> bool:
        return (
            self.coordinator.last_update_success and self.coordinator.data is not None
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._unsub = self.coordinator.async_add_listener(self.async_write_ha_state)
        self._unsub_devinfo = self.coordinator.async_add_listener(
            self._update_port_device_info
        )
        if self.coordinator.data:
            self._update_port_device_info()

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub is not None:
            self._unsub()
        if self._unsub_devinfo is not None:
            self._unsub_devinfo()
        await super().async_will_remove_from_hass()

    @callback
    def _update_port_device_info(self) -> None:
        try:
            if not self.coordinator.data:
                return
            dev_reg = device_registry.async_get(self.hass)
            device_entry = dev_reg.async_get_device(
                identifiers={
                    (
                        DOMAIN,
                        f"{self.entry_id}_{self.coordinator.host}_port_{self.port}",
                    )
                }
            )
            if not device_entry:
                return
            port_name = (self._port_data().get("name") or "").strip()
            switch_label = self._switch_label()
            new_name = f"{switch_label} / Port {self.port}"
            if port_name and port_name != f"Port {self.port}":
                new_name = f"{switch_label} / Port {self.port} ({port_name})"
            if device_entry.name != new_name:
                dev_reg.async_update_device(device_entry.id, name=new_name)
        except Exception as err:
            _LOGGER.debug(
                "Port device update failed for %s port %s: %s",
                self.coordinator.host,
                self.port,
                err,
            )


class PortStatusSensor(SwitchPortPerPortBaseEntity):
    """Port status (on/off) sensor — anchor entity for the per-port device.

    Carries the full attribute payload (port_name, live rates, PoE, errors,
    VLAN, etc.) so the existing frontend card continues to work. Newer
    per-attribute sensors expose the same values as first-class entities.
    """

    def __init__(
        self, coordinator: SwitchPortCoordinator, entry_id: str, port: int
    ) -> None:
        super().__init__(coordinator, entry_id, port)
        self._attr_name = "Status"
        self._attr_unique_id = f"{entry_id}_{coordinator.host}_port_{port}_status"
        self._attr_icon = "mdi:lan"

    @property
    def native_value(self) -> str | None:
        return self._port_data().get("status") or ""

    @property
    def icon(self) -> str | None:
        return "mdi:lan-connect" if self.native_value == "on" else "mdi:lan-disconnect"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        p = self._port_data()
        if not p:
            return {}
        raw_rx_bytes = p.get("rx", 0) or 0
        raw_tx_bytes = p.get("tx", 0) or 0
        port_info = self.coordinator.port_mapping.get(self._port_int, {}) or {}
        has_poe = (
            p.get("poe_power", 0) > 0
            or p.get("poe_status", 0) > 0
            or self.coordinator.base_oids.get("poe_power")
            or self.coordinator.base_oids.get("poe_status")
        )
        attrs = {
            "port_name": p.get("name"),
            "speed_bps": p.get("speed"),
            # Legacy — kept for old cards / backward compatibility
            "rx_bps": raw_rx_bytes * 8,
            "tx_bps": raw_tx_bytes * 8,
            "rx_bps_live": p.get("rx_bps_live", 0),
            "tx_bps_live": p.get("tx_bps_live", 0),
            "is_sfp": bool(port_info.get("is_sfp", False)),
            "is_copper": bool(port_info.get("is_copper", True)),
            "interface": port_info.get("if_descr"),
            "custom": p.get("port_custom"),
            "admin_status": p.get("admin_status"),
            "in_errors": p.get("in_errors", 0),
            "out_errors": p.get("out_errors", 0),
            "in_discards": p.get("in_discards", 0),
            "out_discards": p.get("out_discards", 0),
            "last_change_seconds": p.get("last_change_seconds"),
        }
        if self.coordinator.include_vlans:
            if p.get("vlan") is not None:
                attrs["vlan_id"] = p["vlan"]
            if p.get("vlan_id_list"):
                attrs["vlan_id_list"] = p["vlan_id_list"]
        if has_poe:
            attrs.update(
                {
                    "poe_power_watts": round((p.get("poe_power") or 0) / 1000.0, 2),
                    "poe_enabled": p.get("poe_status") == 3,
                    "poe_class": p.get("poe_class"),
                }
            )
        return attrs


# --- Per-port description-based sensors --------------------------------------


@dataclass(frozen=True)
class PortSensorDescription:
    """Describes a single per-port sensor attached to the port sub-device."""

    key: str
    name: str
    value_fn: Callable[["PortStatusSensor", dict[str, Any]], Any]
    unit: str | None = None
    device_class: SensorDeviceClass | None = None
    state_class: SensorStateClass | None = None
    icon: str = "mdi:ethernet"
    enabled_default: bool = True
    poe_only: bool = False
    vlan_only: bool = False


def _poe_power_watts(_e, p):
    raw = p.get("poe_power")
    return round(raw / 1000.0, 2) if raw else 0.0


def _admin_status_value(_e, p):
    return p.get("admin_status")


def _link_speed_mbps(_e, p):
    sp = p.get("speed") or 0
    # Coordinator already converts <100000 (treated as Mbps) to bps. Convert to Mbps for display.
    return int(sp / 1_000_000) if sp else 0


PORT_SENSOR_DESCRIPTIONS: tuple[PortSensorDescription, ...] = (
    PortSensorDescription(
        "link_speed",
        "Link Speed",
        _link_speed_mbps,
        unit=UnitOfDataRate.MEGABITS_PER_SECOND,
        device_class=SensorDeviceClass.DATA_RATE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:speedometer",
    ),
    PortSensorDescription(
        "rx_rate",
        "RX Rate",
        lambda _e, p: p.get("rx_bps_live") or 0,
        unit=UnitOfDataRate.BITS_PER_SECOND,
        device_class=SensorDeviceClass.DATA_RATE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:download-network",
    ),
    PortSensorDescription(
        "tx_rate",
        "TX Rate",
        lambda _e, p: p.get("tx_bps_live") or 0,
        unit=UnitOfDataRate.BITS_PER_SECOND,
        device_class=SensorDeviceClass.DATA_RATE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:upload-network",
    ),
    PortSensorDescription(
        "rx_bytes",
        "RX Bytes",
        lambda _e, p: p.get("rx") or 0,
        unit="B",
        device_class=SensorDeviceClass.DATA_SIZE,
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:download",
        enabled_default=False,
    ),
    PortSensorDescription(
        "tx_bytes",
        "TX Bytes",
        lambda _e, p: p.get("tx") or 0,
        unit="B",
        device_class=SensorDeviceClass.DATA_SIZE,
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:upload",
        enabled_default=False,
    ),
    PortSensorDescription(
        "in_errors",
        "RX Errors",
        lambda _e, p: p.get("in_errors") or 0,
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:alert-circle-outline",
        enabled_default=False,
    ),
    PortSensorDescription(
        "out_errors",
        "TX Errors",
        lambda _e, p: p.get("out_errors") or 0,
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:alert-circle-outline",
        enabled_default=False,
    ),
    PortSensorDescription(
        "in_discards",
        "RX Discards",
        lambda _e, p: p.get("in_discards") or 0,
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:trash-can-outline",
        enabled_default=False,
    ),
    PortSensorDescription(
        "out_discards",
        "TX Discards",
        lambda _e, p: p.get("out_discards") or 0,
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:trash-can-outline",
        enabled_default=False,
    ),
    PortSensorDescription(
        "admin_status",
        "Admin Status",
        _admin_status_value,
        icon="mdi:toggle-switch",
        enabled_default=False,
    ),
    PortSensorDescription(
        "last_change",
        "Last Change",
        lambda _e, p: p.get("last_change_seconds"),
        unit=UnitOfTime.SECONDS,
        device_class=SensorDeviceClass.DURATION,
        icon="mdi:clock-outline",
        enabled_default=False,
    ),
    PortSensorDescription(
        "port_name",
        "Port Name",
        lambda _e, p: p.get("name"),
        icon="mdi:tag-outline",
        enabled_default=False,
    ),
    PortSensorDescription(
        "port_custom",
        "Custom",
        lambda _e, p: p.get("port_custom"),
        icon="mdi:text-box-search",
        enabled_default=False,
    ),
    PortSensorDescription(
        "vlan_id",
        "VLAN",
        lambda _e, p: p.get("vlan"),
        icon="mdi:lan-pending",
        vlan_only=True,
        enabled_default=False,
    ),
    PortSensorDescription(
        "poe_power",
        "PoE Power",
        _poe_power_watts,
        unit="W",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:lightning-bolt",
        poe_only=True,
    ),
    PortSensorDescription(
        "poe_class",
        "PoE Class",
        lambda _e, p: p.get("poe_class"),
        icon="mdi:flash-outline",
        poe_only=True,
        enabled_default=False,
    ),
    PortSensorDescription(
        "poe_enabled",
        "PoE Status",
        lambda _e, p: "on" if p.get("poe_status") == 3 else "off",
        icon="mdi:power-plug",
        poe_only=True,
        enabled_default=False,
    ),
)


class PortAttributeSensor(SwitchPortPerPortBaseEntity):
    """Generic per-attribute sensor on a port sub-device."""

    def __init__(
        self,
        coordinator: SwitchPortCoordinator,
        entry_id: str,
        port: int,
        description: PortSensorDescription,
    ) -> None:
        super().__init__(coordinator, entry_id, port)
        self._description = description
        self._attr_name = description.name
        self._attr_unique_id = (
            f"{entry_id}_{coordinator.host}_port_{port}_{description.key}"
        )
        self._attr_native_unit_of_measurement = description.unit
        self._attr_device_class = description.device_class
        self._attr_state_class = description.state_class
        self._attr_icon = description.icon
        self._attr_entity_registry_enabled_default = description.enabled_default

    @property
    def native_value(self) -> Any:
        p = self._port_data()
        if not p:
            return None
        try:
            return self._description.value_fn(self, p)
        except Exception as err:
            _LOGGER.debug(
                "value_fn failed for port %s/%s: %s",
                self.port,
                self._description.key,
                err,
            )
            return None


# --- System Sensors ---


class SystemCpuSensor(SwitchPortBaseEntity):
    """CPU usage sensor."""

    _attr_name = "CPU Usage"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_device_class = None
    _attr_icon = "mdi:cpu-64-bit"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_unique_id_suffix = "system_cpu"

    def __init__(self, coordinator: SwitchPortCoordinator, entry_id: str) -> None:
        super().__init__(coordinator, entry_id)
        self._attr_unique_id = f"{entry_id}_{self._attr_unique_id_suffix}"

    @property
    def native_value(self) -> float | None:
        """Return the state of the sensor."""
        if not self.coordinator.data:
            return 0
        try:
            return float(self.coordinator.data.system.get("cpu") or 0)
        except (ValueError, TypeError):
            return 0


class CustomValueSensor(SwitchPortBaseEntity):
    _attr_name = "Custom Value"
    _attr_icon = "mdi:text-box-search"

    def __init__(self, coordinator, entry_id):
        super().__init__(coordinator, entry_id)
        self._attr_unique_id = f"{entry_id}_custom_value"

    @property
    def native_value(self):
        """Return the custom OID value safely."""
        if not self.coordinator.data:
            return ""
        try:
            return self.coordinator.data.system.get("custom")
        except (ValueError, TypeError):
            return ""


class SystemMemorySensor(SwitchPortBaseEntity):
    """Memory usage sensor."""

    _attr_name = "Memory Usage"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_device_class = None
    _attr_icon = "mdi:memory"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_unique_id_suffix = "system_memory"

    def __init__(self, coordinator: SwitchPortCoordinator, entry_id: str) -> None:
        super().__init__(coordinator, entry_id)
        self._attr_unique_id = f"{entry_id}_{self._attr_unique_id_suffix}"

    @property
    def native_value(self) -> float | None:
        """Return the state of the sensor."""
        if not self.coordinator.data:
            return 0
        try:
            return float(self.coordinator.data.system.get("memory") or 0)
        except (ValueError, TypeError):
            return 0


class SystemUptimeSensor(SwitchPortBaseEntity):
    """System Uptime sensor."""

    _attr_name = "Uptime"
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_unique_id_suffix = "system_uptime"

    def __init__(self, coordinator: SwitchPortCoordinator, entry_id: str) -> None:
        super().__init__(coordinator, entry_id)
        self._attr_unique_id = f"{entry_id}_{self._attr_unique_id_suffix}"

    @property
    def native_value(self) -> int | None:
        """Return the state of the sensor (in seconds)."""
        if not self.coordinator.data:
            return 0
        try:
            # Uptime OID typically returns hundredths of a second. Convert to seconds.
            uptime_hsec = int(self.coordinator.data.system.get("uptime") or 0)
            return int(uptime_hsec / 100)
        except (ValueError, TypeError):
            return 0


class SystemHostnameSensor(SwitchPortBaseEntity):
    """System Hostname sensor (for device name info)."""

    _attr_name = "Hostname"
    _attr_icon = "mdi:dns"
    _attr_unique_id_suffix = "system_hostname"

    def __init__(self, coordinator: SwitchPortCoordinator, entry_id: str) -> None:
        super().__init__(coordinator, entry_id)
        self._attr_unique_id = f"{entry_id}_{self._attr_unique_id_suffix}"

    @property
    def native_value(self) -> str | None:
        """Return the state of the sensor."""
        if not self.coordinator.data:
            return ""
        try:
            return self.coordinator.data.system.get("hostname")
        except (ValueError, TypeError):
            return ""


class TemperatureSensor(SwitchPortBaseEntity):
    """Switch temperature from ENTITY-SENSOR-MIB (RFC 3433), entPhySensorType=8 (celsius)."""

    _attr_name = "Temperature"
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:thermometer"

    def __init__(self, coordinator: SwitchPortCoordinator, entry_id: str) -> None:
        super().__init__(coordinator, entry_id)
        self._attr_unique_id = f"{entry_id}_temperature"

    @property
    def native_value(self) -> float | None:
        if not self.coordinator.data:
            return None
        return self.coordinator.data.system.get("temperature_celsius")


class FanStatusSensor(SwitchPortBaseEntity):
    """Aggregate fan status from ENTITY-SENSOR-MIB (RFC 3433), entPhySensorType=1/10.

    State: "ok" (all fans ok), "degraded" (some not ok), "unavailable" (none reporting ok).
    Per-fan detail exposed as attributes.
    """

    _attr_name = "Fan Status"
    _attr_icon = "mdi:fan"

    def __init__(self, coordinator: SwitchPortCoordinator, entry_id: str) -> None:
        super().__init__(coordinator, entry_id)
        self._attr_unique_id = f"{entry_id}_fan_status"

    @property
    def native_value(self) -> str | None:
        if not self.coordinator.data:
            return None
        fans = self.coordinator.data.system.get("fans", [])
        if not fans:
            return None
        if all(f["oper_status"] == 1 for f in fans):
            return "ok"
        if any(f["oper_status"] == 1 for f in fans):
            return "degraded"
        return "unavailable"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if not self.coordinator.data:
            return {}
        fans = self.coordinator.data.system.get("fans", [])
        return {f["name"]: "ok" if f["ok"] else "unavailable" for f in fans}


# =============================================================================
# Setup
# =============================================================================
async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the platform from config_entry. vlans override always to true"""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    # Create entities
    entities = [
        BandwidthSensor(coordinator, entry.entry_id),
        TotalPoESensor(coordinator, entry.entry_id),
        PoEBudgetTotalSensor(coordinator, entry.entry_id),
        SystemCpuSensor(coordinator, entry.entry_id),
        CustomValueSensor(coordinator, entry.entry_id),
        FirmwareSensor(coordinator, entry.entry_id),
        SystemMemorySensor(coordinator, entry.entry_id),
        SystemUptimeSensor(coordinator, entry.entry_id),
        SystemHostnameSensor(coordinator, entry.entry_id),
        TemperatureSensor(coordinator, entry.entry_id),
        FanStatusSensor(coordinator, entry.entry_id),
    ]

    # Per-port sensors: each port becomes its own sub-device (via_device → switch),
    # with the Status entity as anchor (carries legacy attributes used by the card)
    # plus individual sensors for rates, errors, PoE, VLAN, etc.
    manufacturer = (getattr(coordinator, "manufacturer", "") or "").lower()
    is_hp_like = any(m in manufacturer for m in HP_MANUFACTURER_KEYWORDS)
    # Coordinator may have polled PoE data on first refresh even without manual OIDs
    # (HP/Aruba auto-detection). Detect by scanning current per-port readings.
    has_poe_data = False
    if coordinator.data:
        for pdata in coordinator.data.ports.values():
            if (pdata.get("poe_power") or 0) > 0 or (pdata.get("poe_status") or 0) > 0:
                has_poe_data = True
                break
    poe_available = bool(
        coordinator.base_oids.get("poe_power")
        or coordinator.base_oids.get("poe_status")
        or is_hp_like
        or has_poe_data
    )
    include_vlans = bool(coordinator.include_vlans)
    for port in coordinator.ports:
        entities.append(PortStatusSensor(coordinator, entry.entry_id, port))
        for desc in PORT_SENSOR_DESCRIPTIONS:
            if desc.poe_only and not poe_available:
                continue
            if desc.vlan_only and not include_vlans:
                continue
            entities.append(
                PortAttributeSensor(coordinator, entry.entry_id, port, desc)
            )

    async_add_entities(entities)
