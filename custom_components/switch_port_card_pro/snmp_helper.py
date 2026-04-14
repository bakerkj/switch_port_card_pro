"""Async SNMP helper built on puresnmp 2.x.

Replaces pysnmp to avoid its MIB-loader blocking calls that trigger
Home Assistant's blocking-call detector. Function signatures are preserved
so sensor.py / config_flow.py / __init__.py do not need changes:
``hass`` is still accepted (used for the one-time plugin pre-warm) and
``mp_model`` (0=v1, 1=v2c) is still accepted and mapped internally.
"""

from __future__ import annotations
import logging
import re
import asyncio
from functools import partial
from typing import Dict, Any

from puresnmp import Client, V1, V2C
from puresnmp.transport import send_udp
from x690.types import ObjectIdentifier, OctetString

from .const import (
    CONF_OID_IDESCR,
    CONF_OID_IFTYPE,
    CONF_OID_IFSPEED,
    CONF_OID_IFHIGHSPEED,
    CONF_OID_SYSDESCR,
    CONF_OID_IFMAUTYPE,
    HP_MANUFACTURER_KEYWORDS,
)

_LOGGER = logging.getLogger(__name__)

# MAU-MIB RFC 3636 (updated by RFC 4836): ifMauType values that indicate fiber/SFP media.
# Values are OID identities under dot3MauType (1.3.6.1.2.1.26.4).
# Suffix assignments verified against RFC 3636 §4 (entries 1-40) and RFC 4836 §4 (entries 41+).
_FIBER_MAU_TYPES: frozenset[str] = frozenset(
    {
        # 10BASE-FL HD/FD — RFC 3636 .12/.13
        "1.3.6.1.2.1.26.4.12",
        "1.3.6.1.2.1.26.4.13",
        # 100BASE-FX HD/FD — RFC 3636 .17/.18
        "1.3.6.1.2.1.26.4.17",
        "1.3.6.1.2.1.26.4.18",
        # 1000BASE-X generic HD/FD — RFC 3636 .21/.22
        "1.3.6.1.2.1.26.4.21",
        "1.3.6.1.2.1.26.4.22",
        # 1000BASE-LX HD/FD — RFC 3636 .23/.24
        "1.3.6.1.2.1.26.4.23",
        "1.3.6.1.2.1.26.4.24",
        # 1000BASE-SX HD/FD — RFC 3636 .25/.26
        "1.3.6.1.2.1.26.4.25",
        "1.3.6.1.2.1.26.4.26",
        # 1000BASE-CX HD/FD (twinax) — RFC 3636 .27/.28
        "1.3.6.1.2.1.26.4.27",
        "1.3.6.1.2.1.26.4.28",
        # 10GBASE fiber variants — RFC 3636 .32-.40
        # .31 (10GBASE-X) excluded: generic placeholder, covers both copper and fiber subtypes
        "1.3.6.1.2.1.26.4.32",
        "1.3.6.1.2.1.26.4.33",
        "1.3.6.1.2.1.26.4.34",
        "1.3.6.1.2.1.26.4.35",
        "1.3.6.1.2.1.26.4.36",
        "1.3.6.1.2.1.26.4.37",
        "1.3.6.1.2.1.26.4.38",
        "1.3.6.1.2.1.26.4.39",
        "1.3.6.1.2.1.26.4.40",
        # 100BASE-BX10 single-fiber — RFC 4836 .44/.45
        "1.3.6.1.2.1.26.4.44",
        "1.3.6.1.2.1.26.4.45",
        # 100BASE-LX10 — RFC 4836 .46
        "1.3.6.1.2.1.26.4.46",
        # 1000BASE-BX10 single-fiber — RFC 4836 .47/.48
        "1.3.6.1.2.1.26.4.47",
        "1.3.6.1.2.1.26.4.48",
        # 1000BASE-LX10 — RFC 4836 .49
        "1.3.6.1.2.1.26.4.49",
        # 1000BASE-PX PON — RFC 4836 .50-.53
        "1.3.6.1.2.1.26.4.50",
        "1.3.6.1.2.1.26.4.51",
        "1.3.6.1.2.1.26.4.52",
        "1.3.6.1.2.1.26.4.53",
        # 10GBASE-LRM — RFC 4836 .55
        "1.3.6.1.2.1.26.4.55",
        # 40GBASE fiber — RFC 4836 .72/.73/.74
        "1.3.6.1.2.1.26.4.72",
        "1.3.6.1.2.1.26.4.73",
        "1.3.6.1.2.1.26.4.74",
        # 100GBASE fiber — RFC 4836 .76/.77/.78
        "1.3.6.1.2.1.26.4.76",
        "1.3.6.1.2.1.26.4.77",
        "1.3.6.1.2.1.26.4.78",
    }
)

MAU_TYPE_NAMES: dict[str, str] = {
    # Copper — RFC 3636
    "1.3.6.1.2.1.26.4.10": "10BASE-T HD",
    "1.3.6.1.2.1.26.4.11": "10BASE-T FD",
    "1.3.6.1.2.1.26.4.15": "100BASE-TX HD",
    "1.3.6.1.2.1.26.4.16": "100BASE-TX FD",
    "1.3.6.1.2.1.26.4.19": "100BASE-T2 HD",
    "1.3.6.1.2.1.26.4.20": "100BASE-T2 FD",
    "1.3.6.1.2.1.26.4.29": "1000BASE-T HD",
    "1.3.6.1.2.1.26.4.30": "1000BASE-T FD",
    "1.3.6.1.2.1.26.4.41": "10GBASE-CX4",
    # Copper — RFC 4836
    "1.3.6.1.2.1.26.4.54": "10GBASE-T",
    "1.3.6.1.2.1.26.4.56": "1000BASE-KX",
    "1.3.6.1.2.1.26.4.57": "10GBASE-KX4",
    "1.3.6.1.2.1.26.4.58": "10GBASE-KR",
    "1.3.6.1.2.1.26.4.70": "40GBASE-KR4",
    "1.3.6.1.2.1.26.4.71": "40GBASE-CR4",
    "1.3.6.1.2.1.26.4.75": "100GBASE-CR10",
    # Fiber — RFC 3636
    "1.3.6.1.2.1.26.4.12": "10BASE-FL HD",
    "1.3.6.1.2.1.26.4.13": "10BASE-FL FD",
    "1.3.6.1.2.1.26.4.17": "100BASE-FX HD",
    "1.3.6.1.2.1.26.4.18": "100BASE-FX FD",
    "1.3.6.1.2.1.26.4.21": "1000BASE-X HD",
    "1.3.6.1.2.1.26.4.22": "1000BASE-X FD",
    "1.3.6.1.2.1.26.4.23": "1000BASE-LX HD",
    "1.3.6.1.2.1.26.4.24": "1000BASE-LX FD",
    "1.3.6.1.2.1.26.4.25": "1000BASE-SX HD",
    "1.3.6.1.2.1.26.4.26": "1000BASE-SX FD",
    "1.3.6.1.2.1.26.4.27": "1000BASE-CX HD",
    "1.3.6.1.2.1.26.4.28": "1000BASE-CX FD",
    "1.3.6.1.2.1.26.4.31": "10GBASE-X",  # generic — copper or fiber subtype
    "1.3.6.1.2.1.26.4.32": "10GBASE-LX4",
    "1.3.6.1.2.1.26.4.33": "10GBASE-R",
    "1.3.6.1.2.1.26.4.34": "10GBASE-ER",
    "1.3.6.1.2.1.26.4.35": "10GBASE-LR",
    "1.3.6.1.2.1.26.4.36": "10GBASE-SR",
    "1.3.6.1.2.1.26.4.37": "10GBASE-W",
    "1.3.6.1.2.1.26.4.38": "10GBASE-EW",
    "1.3.6.1.2.1.26.4.39": "10GBASE-LW",
    "1.3.6.1.2.1.26.4.40": "10GBASE-SW",
    # Fiber — RFC 4836
    "1.3.6.1.2.1.26.4.44": "100BASE-BX10D",
    "1.3.6.1.2.1.26.4.45": "100BASE-BX10U",
    "1.3.6.1.2.1.26.4.46": "100BASE-LX10",
    "1.3.6.1.2.1.26.4.47": "1000BASE-BX10D",
    "1.3.6.1.2.1.26.4.48": "1000BASE-BX10U",
    "1.3.6.1.2.1.26.4.49": "1000BASE-LX10",
    "1.3.6.1.2.1.26.4.50": "1000BASE-PX10D",
    "1.3.6.1.2.1.26.4.51": "1000BASE-PX10U",
    "1.3.6.1.2.1.26.4.52": "1000BASE-PX20D",
    "1.3.6.1.2.1.26.4.53": "1000BASE-PX20U",
    "1.3.6.1.2.1.26.4.55": "10GBASE-LRM",
    "1.3.6.1.2.1.26.4.72": "40GBASE-SR4",
    "1.3.6.1.2.1.26.4.73": "40GBASE-FR",
    "1.3.6.1.2.1.26.4.74": "40GBASE-LR4",
    "1.3.6.1.2.1.26.4.76": "100GBASE-SR10",
    "1.3.6.1.2.1.26.4.77": "100GBASE-LR4",
    "1.3.6.1.2.1.26.4.78": "100GBASE-ER4",
}

# ── Plugin pre-warm ──────────────────────────────────────────────────────────
# puresnmp creates a NEW Loader on every mpm.create() / security.create() call
# and each Loader runs pkgutil.iter_modules (which calls os.listdir) during its
# first discover_plugins() call. Because those functions are called inside
# Client() and ultimately inside every SNMP walk, the listdir fires on every
# single Client() instantiation, triggering HA's blocking-call detector.
#
# _prewarm_plugins runs the discovery once in an executor thread, then replaces
# the module-level create callables with versions that reuse a single
# pre-populated Loader instance, so no further filesystem I/O occurs.
_PLUGINS_PREWARMED = False
_PREWARM_LOCK = asyncio.Lock()


def _prewarm_plugins() -> None:
    """Pre-discover puresnmp plugins and patch module-level create functions."""
    from puresnmp.exc import UnknownMessageProcessingModel, UnknownSecurityModel
    from puresnmp.plugins import mpm as _mpm, security as _sec
    from puresnmp.plugins.pluginbase import Loader, discover_plugins

    _mpm_loader = Loader("puresnmp_plugins.mpm", _mpm.is_valid_mpm_plugin)
    _mpm_loader.discovered_plugins = discover_plugins(
        "puresnmp_plugins.mpm", _mpm.is_valid_mpm_plugin
    )

    def _cached_mpm_create(identifier, transport_handler, lcd):
        result = _mpm_loader.create(identifier)
        if result is None:
            raise UnknownMessageProcessingModel(
                "puresnmp_plugins.mpm",
                identifier,
                sorted(_mpm_loader.discovered_plugins.keys()),
            )
        return result.create(transport_handler, lcd)  # type: ignore

    _mpm.create = _cached_mpm_create  # type: ignore[assignment]

    _sec_loader = Loader("puresnmp_plugins.security", _sec.is_valid_sec_plugin)
    _sec_loader.discovered_plugins = discover_plugins(
        "puresnmp_plugins.security", _sec.is_valid_sec_plugin
    )

    def _cached_sec_create(identifier):
        result = _sec_loader.create(identifier)
        if result is None:
            raise UnknownSecurityModel(
                "puresnmp_plugins.security",
                identifier,
                sorted(_sec_loader.discovered_plugins.keys()),
            )
        return result.create()  # type: ignore

    _sec.create = _cached_sec_create  # type: ignore[assignment]

    # The mpm plugin modules captured security.create at import time via
    # ``from puresnmp.plugins.security import create as create_sm``. Patching
    # _sec.create doesn't reach those; patch each mpm plugin module directly.
    import puresnmp_plugins.mpm.v1 as _v1
    import puresnmp_plugins.mpm.v2c as _v2c

    _v1.create_sm = _cached_sec_create  # type: ignore[attr-defined]
    _v2c.create_sm = _cached_sec_create  # type: ignore[attr-defined]


async def async_prewarm_plugins(hass) -> None:
    """Run plugin pre-warm exactly once per process lifetime."""
    global _PLUGINS_PREWARMED
    if _PLUGINS_PREWARMED:
        return
    async with _PREWARM_LOCK:
        if _PLUGINS_PREWARMED:
            return
        await hass.async_add_executor_job(_prewarm_plugins)
        _PLUGINS_PREWARMED = True
        _LOGGER.debug("puresnmp plugins pre-loaded")


# ── Credentials / transport helpers ──────────────────────────────────────────
def _credentials(mp_model: int, community: str):
    """Return puresnmp credentials for the given SNMP mpModel (0=v1, 1=v2c)."""
    return V1(community) if mp_model == 0 else V2C(community)


def _make_sender(timeout: int, retries: int):
    """Return a puresnmp sender with custom timeout and retry settings."""
    return partial(send_udp, timeout=timeout, retries=retries)


def _value_to_str(value) -> str:
    """Convert a puresnmp value to a string.

    Matches the format pysnmp prettyPrint() produced so existing parsers
    (sensor.py, discover_physical_ports) continue to work unchanged:
    - OctetString of printable ASCII  → decoded text (e.g. "Port 1")
    - OctetString of binary bytes     → "0x<hex>" (e.g. "0x1c28afc34624")
    - IpAddress                       → "192.168.1.1"
    - Integer / Counter / TimeTicks   → plain decimal string
    - ObjectIdentifier                → "1.3.6.1.2.1.26.4.35"
    """
    if value is None:
        return ""
    if isinstance(value, OctetString):
        raw = value.value
        if not raw:
            return ""
        if all(0x20 <= b < 0x7F for b in raw):
            return raw.decode("ascii")
        return "0x" + raw.hex()
    if isinstance(value, ObjectIdentifier):
        return str(value)
    inner = getattr(value, "value", value)
    return str(inner)


async def async_snmp_get(
    hass,
    host: str,
    community: str,
    snmp_port: int,
    oid: str,
    timeout: int = 10,
    retries: int = 3,
    mp_model: int = 1,
) -> str | None:
    """Async SNMP GET for a single OID. Returns None on any failure."""
    if not oid or not oid.strip():
        return None

    await async_prewarm_plugins(hass)

    try:
        client = Client(
            host,
            _credentials(mp_model, community),
            port=snmp_port,
            sender=_make_sender(timeout, retries),
        )
        value = await client.get(ObjectIdentifier(oid.strip()))
        return _value_to_str(value)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _LOGGER.debug("SNMP GET failed on %s (oid=%s): %s", host, oid, exc)
        return None


async def async_snmp_walk(
    hass,
    host: str,
    community: str,
    snmp_port: int,
    base_oid: str,
    timeout: int = 10,
    retries: int = 3,
    mp_model: int = 1,
) -> dict[str, str]:
    """Async SNMP walk. Returns {full_oid_str: value_str} for all OIDs under base_oid.

    Uses GETBULK for SNMPv2c, falls back to GETNEXT for SNMPv1.
    """
    if not base_oid or not base_oid.strip():
        return {}

    await async_prewarm_plugins(hass)

    base = base_oid.strip()
    results: dict[str, str] = {}
    try:
        client = Client(
            host,
            _credentials(mp_model, community),
            port=snmp_port,
            sender=_make_sender(timeout, retries),
        )
        oid_obj = ObjectIdentifier(base)
        walker = (
            client.walk([oid_obj])
            if mp_model == 0
            else client.bulkwalk([oid_obj], bulk_size=25)
        )
        async for vb in walker:
            oid_str = str(vb.oid)
            if not oid_str.startswith(base):
                break
            results[oid_str] = _value_to_str(vb.value)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _LOGGER.debug("SNMP WALK failed on %s (%s): %s", host, base, exc)
    return results


async def async_snmp_bulk(
    hass,
    host: str,
    community: str,
    snmp_port: int,
    oid_list: list[str],
    timeout: int = 8,
    retries: int = 2,
    mp_model: int = 1,
) -> Dict[str, str | None]:
    """Fast parallel GET for system OIDs. Skips empty/blank OIDs."""
    if not oid_list:
        return {}

    # Filter out empty or whitespace-only OIDs
    filtered_oids = []
    results_template = {}
    for oid in oid_list:
        stripped = oid.strip() if oid else ""
        if stripped:
            filtered_oids.append(stripped)
            results_template[stripped] = None
        else:
            results_template[oid] = None

    if not filtered_oids:
        return results_template

    # Perform parallel GET only on valid OIDs
    async def _get_one(oid: str):
        return await async_snmp_get(
            hass,
            host,
            community,
            snmp_port,
            oid,
            timeout=timeout,
            retries=retries,
            mp_model=mp_model,
        )

    valid_results = await asyncio.gather(*[_get_one(oid) for oid in filtered_oids])

    # Combine results back
    for oid, result in zip(filtered_oids, valid_results):
        results_template[oid] = result

    # Preserve original oid_list order
    final_results = {}
    for oid in oid_list:
        stripped = oid.strip() if oid else ""
        final_results[oid] = results_template.get(stripped)

    return final_results


async def discover_physical_ports(
    hass,
    host: str,
    community: str,
    snmp_port,
    mp_model: int = 1,
) -> dict[int, dict[str, Any]]:
    """
    Auto-discover real physical ports and perfectly classify copper vs SFP/SFP+.
    Works on: Zyxel, TP-Link, QNAP, Ubiquiti, Cisco, ASUS, MikroTik, Netgear, D-Link, etc.
    """
    mapping: dict[int, dict[str, Any]] = {}
    logical_port = 1

    try:
        # Step 1: Get interface descriptions
        descr_data = await async_snmp_walk(
            hass, host, community, snmp_port, CONF_OID_IDESCR, mp_model=mp_model
        )
        if not descr_data:
            _LOGGER.debug("discover_physical_ports: no ifDescr data from %s", host)
            return {}

        _LOGGER.debug(
            "ifDescr data from %s: %d interfaces found", host, len(descr_data)
        )

        # Step 2: Get speed, ifType, and MAU type (parallel)
        speed_data, high_speed_data, type_data, mau_data = await asyncio.gather(
            async_snmp_walk(
                hass, host, community, snmp_port, CONF_OID_IFSPEED, mp_model=mp_model
            ),
            async_snmp_walk(
                hass,
                host,
                community,
                snmp_port,
                CONF_OID_IFHIGHSPEED,
                mp_model=mp_model,
            ),
            async_snmp_walk(
                hass, host, community, snmp_port, CONF_OID_IFTYPE, mp_model=mp_model
            ),
            async_snmp_walk(
                hass, host, community, snmp_port, CONF_OID_IFMAUTYPE, mp_model=mp_model
            ),
        )
        _LOGGER.debug("MAU-MIB data from %s: %d entries", host, len(mau_data))

        # Step 4: Get sysDescr for manufacturer info (scalar OID — use GET not WALK)
        sys_descr = (
            await async_snmp_get(
                hass, host, community, snmp_port, CONF_OID_SYSDESCR, mp_model=mp_model
            )
            or "Unknown"
        )
        _LOGGER.debug("sysDescr from %s: %s", host, sys_descr)

        # Extract manufacturer from sysDescr
        manufacturer = _extract_manufacturer(sys_descr)

        sorted_oids = sorted(descr_data.keys(), key=lambda x: int(x.split(".")[-1]))
        for oid_str in sorted_oids:
            descr_raw = descr_data[oid_str]
            try:
                # Extract ifIndex from the end of the OID
                if_index = int(oid_str.split(".")[-1])
                descr_clean = descr_raw.strip()
                descr_lower = descr_clean.lower()
            except (ValueError, IndexError, AttributeError):
                continue

            # === STEP 1: Reject obvious virtual/junk interfaces ===
            if _is_virtual_interface(descr_lower):
                continue

            # === STEP 2: Accept anything that looks like a real port ===
            is_likely_physical = _is_physical_interface(
                descr_lower, descr_clean, if_index
            )

            if not is_likely_physical:
                continue

            # === STEP 3: Fiber vs Copper detection ===
            # Primary: MAU-MIB (RFC 3636/4836) — authoritative when available
            # Fallback: ifType + description heuristics
            mau_key_prefix = f"{CONF_OID_IFMAUTYPE}.{if_index}."
            mau_type_oid = next(
                (v for k, v in mau_data.items() if k.startswith(mau_key_prefix)), None
            )
            if mau_type_oid is not None:
                # Normalize: pysnmp may return "iso.3.6..." instead of "1.3.6..."
                if mau_type_oid.startswith("iso."):
                    mau_type_oid = "1." + mau_type_oid[4:]
                is_sfp = mau_type_oid in _FIBER_MAU_TYPES
                is_copper = not is_sfp
                port_type = MAU_TYPE_NAMES.get(mau_type_oid, "unknown")
                detection = "mau_mib"
            else:
                if_type = _get_interface_type(type_data, if_index)
                is_sfp, detection = _detect_sfp_port(if_type, descr_lower, manufacturer)
                is_copper = not is_sfp
                port_type = "unknown"

            # === STEP 4: Port speed ===
            speed_mbps = _get_port_speed(speed_data, high_speed_data, if_index)

            # === STEP 5: Friendly name generation ===
            name = _generate_port_name(descr_clean, descr_lower, logical_port)

            mapping[logical_port] = {
                "if_index": if_index,
                "name": name,
                "if_descr": descr_clean,
                "is_sfp": is_sfp,
                "is_copper": is_copper,
                "detection": detection,
                "speed_mbps": speed_mbps,
                "manufacturer": manufacturer,
                "port_type": port_type,
            }
            logical_port += 1

        copper_count = sum(1 for p in mapping.values() if p["is_copper"])
        sfp_count = len(mapping) - copper_count
        _LOGGER.info(
            "Auto-discovered %d physical ports on %s → %d copper, %d SFP/SFP+ | Manufacturer: %s",
            len(mapping),
            host,
            copper_count,
            sfp_count,
            manufacturer,
        )
        return mapping

    except asyncio.CancelledError:
        raise
    except Exception:
        _LOGGER.exception("Failed to auto-discover ports on %s", host)
        return {}


def _extract_manufacturer(sys_descr: str) -> str:
    """Extract manufacturer name from sysDescr string."""
    if not sys_descr or sys_descr == "Unknown":
        return "Unknown"

    # Common patterns: "H3C S3100-26C, Software Version..." → "H3C"
    first_word = sys_descr.split(" ")[0]

    # Reject common non-manufacturer words
    if first_word.lower() in ("version", "software", "hardware", "release", "build"):
        return "Unknown"

    return first_word


def _is_virtual_interface(descr_lower: str) -> bool:
    """Check if interface description indicates a virtual interface."""
    # Quick rejections first
    if any(
        x in descr_lower for x in ["cpu interface", "link aggregate", "logical-int"]
    ):
        return True

    # Patterns that should match at word start
    word_start_bad = [
        r"\bvlan",
        r"\btun",
        r"\bgre",
        r"\bimq",
        r"\bifb",
        r"\berspan",
        r"\bip_vti",
        r"\bip6_vti",
        r"\bip6tnl",
        r"\bip6gre",
        r"\bwds",
        r"\bloopback",
        r"\bpo\d+",
    ]
    if any(re.search(pattern, descr_lower) for pattern in word_start_bad):
        return True

    # Patterns that need exact word match
    exact_word_bad = [
        r"\blo\b",
        r"\bbr\b",
        r"\bdummy\b",
        r"\bwlan\b",
        r"\bath\b",
        r"\bwifi\b",
        r"\bwl\b",
        r"\bbond\b",
        r"\bveth\b",
        r"\bbridge\b",
        r"\bvirtual\b",
        r"\bnull\b",
        r"\bsit\b",
        r"\bipip\b",
        r"\bbcmsw\b",
        r"\bspu\b",
    ]
    return any(re.search(pattern, descr_lower) for pattern in exact_word_bad)


def _is_physical_interface(descr_lower: str, descr_clean: str, if_index: int) -> bool:
    """Check if interface description indicates a physical interface."""
    # Universal exclusion of management/console ports
    if any(k in descr_lower for k in ["mgmt", "management", "console"]):
        return False

    # Specifically catch Cisco/Standard management ports like GigabitEthernet0/0
    if re.search(r"ethernet0/0$", descr_lower):
        return False

    # Check for common physical port indicators
    is_likely_physical = (
        any(
            k in descr_lower
            for k in [
                "port",
                "eth",
                "ge.",
                "swp",
                "xe.",
                "lan",
                "wan",
                "sfp",
                "gigabit",
                "fasteth",
                "10g",
                "slot:",
                "level",
            ]
        )
        or re.match(r"^gigabithethernet\d+", descr_lower)
        or re.match(r"^[pg]\d+$", descr_lower)
        or re.match(
            r"^[a-z]\d+$", descr_lower
        )  # e.g. A1, A2, A3, A4 (uplink/SFP ports)
        or (descr_lower.startswith("slot:") and "port:" in descr_lower)
    )

    # Special case: single-digit descriptions
    if descr_clean.isdigit():
        if if_index >= 1000:
            return False
        return True

    return is_likely_physical


def _get_interface_type(type_data: dict, if_index: int) -> int:
    """Extract interface type from SNMP ifType data (IANAifType)."""
    for t_oid, t_val in type_data.items():
        if t_oid.endswith(f".{if_index}"):
            try:
                # Handle types like "ethernetCsmacd(6)"
                if "(" in str(t_val):
                    match_type = re.search(r"\((\d+)\)", str(t_val))
                    return int(match_type.group(1)) if match_type else 0
                return int(t_val)
            except (ValueError, TypeError):
                return 0
    return 0


def _detect_sfp_port(
    if_type: int, descr_lower: str, manufacturer: str = ""
) -> tuple[bool, str]:
    """Detect if port is fiber based on ifType and description keywords.
    Only used when MAU-MIB data is unavailable.
    """
    # HP/Aruba: uplink ports named A1-A4 are SFP slots
    if any(m in manufacturer.lower() for m in HP_MANUFACTURER_KEYWORDS):
        if re.match(r"^[a-z]\d+$", descr_lower):
            return True, "hp_uplink_name"

    # Netgear 10G special case
    if "10g - level" in descr_lower:
        return True, "netgear_10g_sfp"

    # Cisco stack/modular slot: GigabitEthernetX/Y/Z where Y > 0 is a module slot
    cisco_slot_match = re.search(r"gigabithethernet(\d+)/(\d+)/(\d+)", descr_lower)
    if cisco_slot_match:
        module_slot = int(cisco_slot_match.group(2))
        return (
            (True, "cisco_module_sfp")
            if module_slot > 0
            else (False, "cisco_fixed_copper")
        )

    # IANA ifType: 56=fibreChannel, 171=pos (Packet over SONET/SDH)
    # Verified against IANA ifType registry — 161=LAG and 172=DVB excluded
    if if_type in (56, 171):
        return True, "iftype_fiber"

    # Description keyword matching
    # "10gbase-t" is copper — only match known fiber 10G variants explicitly
    is_fiber_by_name = any(
        k in descr_lower
        for k in [
            "sfp",
            "fiber",
            "fibre",
            "optical",
            "1000base-x",
            "10gbase-sr",
            "10gbase-lr",
            "10gbase-er",
            "10gbase-lrm",
            "10gbase-zr",
            "mini-gbic",
            "sfp+",
            "sfp28",
            "25g",
            "40g",
            "100g",
            "qsfp",
            "fortygigabit",
        ]
    )
    if is_fiber_by_name:
        return True, "name_keyword"

    return False, "default_copper"


def _get_port_speed(speed_data: dict, high_speed_data: dict, if_index: int) -> int:
    """Get port speed in Mbps."""
    # Try high-speed first (ifHighSpeed)
    raw_high = high_speed_data.get(f"{CONF_OID_IFHIGHSPEED}.{if_index}")
    if raw_high:
        try:
            return int(raw_high)
        except (ValueError, TypeError):
            pass

    # Fall back to regular speed (ifSpeed)
    raw_speed = speed_data.get(f"{CONF_OID_IFSPEED}.{if_index}")
    if raw_speed:
        try:
            return int(raw_speed) // 1_000_000
        except (ValueError, TypeError):
            pass

    return 0


def _generate_port_name(descr_clean: str, descr_lower: str, logical_port: int) -> str:
    """Generate a friendly port name."""
    # Slot:X Port:Y format
    if "slot:" in descr_lower and "port:" in descr_lower:
        match = re.search(r"port:\s*(\d+)", descr_lower, re.IGNORECASE)
        if match:
            return f"Port {match.group(1)}"

    # Pure numeric description
    if descr_clean.isdigit():
        return f"Port {descr_clean}"

    # Cisco GigabitEthernet format
    if "gigabithethernet" in descr_lower:
        match = re.search(r"(\d+)$", descr_lower)
        if match:
            return f"Port {match.group(1)}"
        return descr_clean

    # Already has "port" in name
    if "port " in descr_lower:
        return descr_clean

    # Standard interface names
    if descr_lower.startswith(("eth", "ge.", "swp", "xe.")):
        return descr_clean

    # Fallback
    return f"Port {logical_port}"
