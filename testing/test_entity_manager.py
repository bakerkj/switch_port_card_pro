"""Standalone workflow test for the per-port entity manager + Repairs flow.

No Home Assistant or pytest required. Stubs the HA helpers the component
imports, loads the *real* entity_manager.py / repairs.py, and drives the
disable / ignore / restore / persistence workflows with assertions.

Run:  python3 testing/test_entity_manager.py
"""

from __future__ import annotations

import asyncio
import dataclasses
import enum
import importlib.util
import os
import sys
import types

COMP = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "custom_components",
    "switch_port_card_pro",
)

# --------------------------------------------------------------------------
# Stub homeassistant.*
# --------------------------------------------------------------------------
_UNSET = object()


def _callback(fn):
    return fn


class _Disabler(enum.Enum):
    USER = "user"
    INTEGRATION = "integration"
    CONFIG_ENTRY = "config_entry"
    DEVICE = "device"
    HASS = "hass"


@dataclasses.dataclass
class _RegEntry:
    entity_id: str
    unique_id: str
    platform: str
    config_entry_id: str
    disabled_by: object = None


class _Registry:
    def __init__(self):
        self.entries: list[_RegEntry] = []

    def async_update_entity(self, entity_id, disabled_by=_UNSET):
        for e in self.entries:
            if e.entity_id == entity_id:
                if disabled_by is not _UNSET:
                    e.disabled_by = disabled_by
                return e
        raise AssertionError(f"unknown entity_id {entity_id}")


_REG = _Registry()


def _er_async_get(_hass):
    return _REG


def _er_entries_for_entry(reg, cid):
    return [e for e in reg.entries if e.config_entry_id == cid]


class _IssueSeverity(enum.Enum):
    WARNING = "warning"
    ERROR = "error"


_ISSUES: dict[str, dict] = {}


def _ir_create(hass, domain, issue_id, **kw):
    _ISSUES[issue_id] = {"domain": domain, **kw}


def _ir_delete(hass, domain, issue_id):
    _ISSUES.pop(issue_id, None)


def _simulate_ha_restart():
    """HA clears non-persistent issues on restart; persistent ones survive."""
    for iid in [k for k, v in _ISSUES.items() if not v.get("is_persistent")]:
        _ISSUES.pop(iid)


_STORE_BACKING: dict[str, dict] = {}


class _Store:
    def __init__(self, hass, version, key):
        self._key = key

    async def async_load(self):
        v = _STORE_BACKING.get(self._key)
        return None if v is None else __import__("copy").deepcopy(v)

    async def async_save(self, data):
        _STORE_BACKING[self._key] = __import__("copy").deepcopy(data)


class _RepairsFlow:
    def async_show_menu(self, *, step_id, menu_options):
        return {"type": "menu", "step_id": step_id, "menu_options": menu_options}

    def async_create_entry(self, *, title, data):
        return {"type": "create_entry", "title": title, "data": data}


def _mod(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


_mod("homeassistant")
_mod("homeassistant.config_entries", ConfigEntry=object)
_mod("homeassistant.core", HomeAssistant=object, callback=_callback)
_mod("homeassistant.helpers")
_mod("homeassistant.components")
_mod("homeassistant.data_entry_flow", FlowResult=dict)
_mod("homeassistant.components.repairs", RepairsFlow=_RepairsFlow)
_mod(
    "homeassistant.helpers.entity_registry",
    async_get=_er_async_get,
    async_entries_for_config_entry=_er_entries_for_entry,
    RegistryEntryDisabler=_Disabler,
    RegistryEntry=_RegEntry,
)
_mod(
    "homeassistant.helpers.issue_registry",
    async_create_issue=_ir_create,
    async_delete_issue=_ir_delete,
    IssueSeverity=_IssueSeverity,
)
_mod("homeassistant.helpers.storage", Store=_Store)


# --------------------------------------------------------------------------
# Load the real component modules as package "spcp"
# --------------------------------------------------------------------------
def _load(modname, filename):
    spec = importlib.util.spec_from_file_location(modname, os.path.join(COMP, filename))
    module = importlib.util.module_from_spec(spec)
    sys.modules[modname] = module
    spec.loader.exec_module(module)
    return module


pkg = types.ModuleType("spcp")
pkg.__path__ = [COMP]
pkg.__package__ = "spcp"
sys.modules["spcp"] = pkg
_load("spcp.const", "const.py")
em = _load("spcp.entity_manager", "entity_manager.py")
rp = _load("spcp.repairs", "repairs.py")


# Controllable clock for grace-period math.
class _Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def time(self):
        return self.t


CLOCK = _Clock()
em.time = CLOCK


# --------------------------------------------------------------------------
# Fakes for coordinator / entry / hass
# --------------------------------------------------------------------------
EID = "ENTRY1"
HOST = "switch-x.example.org"
DOMAIN = sys.modules["spcp.const"].DOMAIN
DISABLER = _Disabler

DEFAULT_ON = ("link_speed", "rx_rate", "tx_rate", "poe_power")
DEFAULT_OFF = ("rx_bytes", "in_errors")  # HA marks these disabled_by=INTEGRATION


class FakeData:
    def __init__(self, ports):
        self.ports = ports  # {"1": {"status": "on"/"off"}}


class FakeCoordinator:
    def __init__(self, ports):
        self.ports = ports
        self.host = HOST
        self.data = FakeData({str(p): {"status": "off"} for p in ports})
        self._listeners = []

    def async_add_listener(self, cb):
        self._listeners.append(cb)
        return lambda: self._listeners.remove(cb)

    def set_status(self, port, on, last_change=None):
        d = {"status": "on" if on else "off"}
        if last_change is not None:
            d["last_change_seconds"] = last_change
        self.data.ports[str(port)] = d


class FakeEntry:
    def __init__(self, options):
        self.entry_id = EID
        self.options = options


class FakeHass:
    def __init__(self):
        self.data = {DOMAIN: {}}

    def async_create_task(self, coro):
        coro.close()  # tests call _async_reconcile directly


def seed_registry(ports):
    _REG.entries.clear()
    for p in ports:
        _REG.entries.append(
            _RegEntry(
                f"sensor.p{p}_status",
                f"{EID}_{HOST}_port_{p}_status",
                DOMAIN,
                EID,
                None,
            )
        )
        for k in DEFAULT_ON:
            _REG.entries.append(
                _RegEntry(
                    f"sensor.p{p}_{k}",
                    f"{EID}_{HOST}_port_{p}_{k}",
                    DOMAIN,
                    EID,
                    None,
                )
            )
        for k in DEFAULT_OFF:
            _REG.entries.append(
                _RegEntry(
                    f"sensor.p{p}_{k}",
                    f"{EID}_{HOST}_port_{p}_{k}",
                    DOMAIN,
                    EID,
                    DISABLER.INTEGRATION,
                )
            )
        # Telemetry carrier — must never be touched by the manager.
        _REG.entries.append(
            _RegEntry(
                f"sensor.p{p}_info",
                f"{EID}_{HOST}_port_{p}_info",
                DOMAIN,
                EID,
                None,
            )
        )
    # an unrelated system entity (must always be ignored)
    _REG.entries.append(
        _RegEntry("sensor.sys_cpu", f"{EID}_system_cpu", DOMAIN, EID, None)
    )


def disabled_by(port, key):
    uid = f"{EID}_{HOST}_port_{port}_{key}"
    for e in _REG.entries:
        if e.unique_id == uid:
            return e.disabled_by
    raise AssertionError(uid)


# --------------------------------------------------------------------------
# Assertion harness
# --------------------------------------------------------------------------
RESULTS = []


def check(name, cond):
    RESULTS.append((name, bool(cond)))
    print(("PASS  " if cond else "FAIL  ") + name)


def opts(enabled=True, grace_h=24, cycles=3):
    return {
        "auto_manage_entities": enabled,
        "down_grace_hours": grace_h,
        "up_restore_cycles": cycles,
    }


def new_manager(coord, entry, hass):
    return em.PortEntityManager(hass, entry, coord)


def reset_world():
    """Isolate a fresh manager lineage (distinct config entry in production)."""
    _STORE_BACKING.clear()
    _ISSUES.clear()


async def main():
    # ===== Scenario 1: feature OFF → no-op =====
    seed_registry([1])
    coord = FakeCoordinator([1])
    hass = FakeHass()
    entry = FakeEntry(opts(enabled=False))
    mgr = new_manager(coord, entry, hass)
    await mgr.async_load()
    hass.data[DOMAIN][EID] = coord
    coord.entity_manager = mgr
    await mgr._async_reconcile()
    check("feature off: no issue raised", not _ISSUES)
    check("feature off: extras stay enabled", disabled_by(1, "rx_rate") is None)

    # ===== Scenario 2: down → grace → issue =====
    entry.options = opts()  # enable
    coord.set_status(1, False)
    CLOCK.t = 1_000_000.0
    await mgr._async_reconcile()
    check("down<grace: no issue yet", not _ISSUES)
    CLOCK.t += 23 * 3600  # still < 24h
    await mgr._async_reconcile()
    check("down 23h: still no issue", not _ISSUES)
    CLOCK.t += 2 * 3600  # now > 24h total
    await mgr._async_reconcile()
    iid = f"port_down_{EID}_1"
    check("down>grace: issue raised", iid in _ISSUES)
    check(
        "issue placeholders correct",
        _ISSUES[iid]["translation_placeholders"]["port"] == "1"
        and _ISSUES[iid]["translation_placeholders"]["hours"] == "24",
    )

    # ===== Scenario 3: Repairs 'disable_this' =====
    flow = rp.PortDownRepairFlow(hass, iid, {"entry_id": EID, "port": "1"})
    menu = await flow.async_step_init()
    check(
        "fix flow shows 4-option menu",
        menu["type"] == "menu"
        and menu["menu_options"]
        == ["disable_this", "disable_all", "ignore", "allow_flap"],
    )
    res = await flow.async_step_disable_this()
    check("disable_this completes flow", res["type"] == "create_entry")
    check(
        "disable_this: all 4 default-on extras disabled",
        all(disabled_by(1, k) == DISABLER.INTEGRATION for k in DEFAULT_ON),
    )
    check("disable_this: status NOT touched", disabled_by(1, "status") is None)
    check(
        "disable_this: default-off extras untouched",
        all(disabled_by(1, k) == DISABLER.INTEGRATION for k in DEFAULT_OFF),
    )
    check("disable_this: issue cleared", iid not in _ISSUES)
    recorded = set(mgr._state["1"]["disabled_uids"])
    check(
        "recorded set == exactly the 4 we disabled",
        recorded == {f"{EID}_{HOST}_port_1_{k}" for k in DEFAULT_ON},
    )
    check(
        "info carrier never disabled (excluded like status)",
        disabled_by(1, "info") is None and f"{EID}_{HOST}_port_1_info" not in recorded,
    )

    # ===== Scenario 4: still down, already managed → no re-raise =====
    CLOCK.t += 100 * 3600
    await mgr._async_reconcile()
    check("disabled port: no re-raise while down", iid not in _ISSUES)

    # ===== Scenario 5: back up N cycles → restore EXACT set =====
    coord.set_status(1, True)
    await mgr._async_reconcile()  # streak 1
    await mgr._async_reconcile()  # streak 2
    check(
        "up but < cycles: not yet restored",
        disabled_by(1, "rx_rate") == DISABLER.INTEGRATION,
    )
    await mgr._async_reconcile()  # streak 3 == cycles → restore
    check(
        "restored: the 4 we disabled are re-enabled",
        all(disabled_by(1, k) is None for k in DEFAULT_ON),
    )
    check(
        "restored: default-off extras STILL disabled (regression)",
        all(disabled_by(1, k) == DISABLER.INTEGRATION for k in DEFAULT_OFF),
    )
    check("restored: disabled_uids cleared", not mgr._state["1"]["disabled_uids"])

    # ===== Scenario 6: ignore + re-arm =====
    reset_world()
    seed_registry([2])
    coord2 = FakeCoordinator([2])
    entry2 = FakeEntry(opts())
    hass2 = FakeHass()
    mgr2 = new_manager(coord2, entry2, hass2)
    await mgr2.async_load()
    hass2.data[DOMAIN][EID] = coord2
    coord2.entity_manager = mgr2
    coord2.set_status(2, False)
    CLOCK.t += 1_000
    await mgr2._async_reconcile()
    CLOCK.t += 25 * 3600
    await mgr2._async_reconcile()
    iid2 = f"port_down_{EID}_2"
    check("port2 down>grace: issue raised", iid2 in _ISSUES)
    flow2 = rp.PortDownRepairFlow(hass2, iid2, {"entry_id": EID, "port": "2"})
    await flow2.async_step_init()
    await flow2.async_step_ignore()
    check("ignore: issue cleared", iid2 not in _ISSUES)
    check(
        "ignore: extras NOT disabled",
        all(disabled_by(2, k) is None for k in DEFAULT_ON),
    )
    CLOCK.t += 100 * 3600
    await mgr2._async_reconcile()
    check("ignore: no re-raise while still down", iid2 not in _ISSUES)
    coord2.set_status(2, True)
    await mgr2._async_reconcile()  # clears ignored
    coord2.set_status(2, False)
    CLOCK.t += 1_000
    await mgr2._async_reconcile()
    CLOCK.t += 26 * 3600
    await mgr2._async_reconcile()
    check("ignore re-arms after up→down: issue raised again", iid2 in _ISSUES)

    # ===== Scenario 7: disable_all flagged =====
    reset_world()
    seed_registry([3, 4, 5])
    coord3 = FakeCoordinator([3, 4, 5])
    entry3 = FakeEntry(opts())
    hass3 = FakeHass()
    mgr3 = new_manager(coord3, entry3, hass3)
    await mgr3.async_load()
    hass3.data[DOMAIN][EID] = coord3
    coord3.entity_manager = mgr3
    for p in (3, 4):
        coord3.set_status(p, False)
    coord3.set_status(5, True)  # 5 stays up — must NOT be flagged
    CLOCK.t += 1_000
    await mgr3._async_reconcile()
    CLOCK.t += 25 * 3600
    await mgr3._async_reconcile()
    check(
        "ports 3&4 flagged, 5 not",
        f"port_down_{EID}_3" in _ISSUES
        and f"port_down_{EID}_4" in _ISSUES
        and f"port_down_{EID}_5" not in _ISSUES,
    )
    check("flagged_ports() == [3,4]", mgr3.flagged_ports() == [3, 4])
    flow3 = rp.PortDownRepairFlow(
        hass3, f"port_down_{EID}_3", {"entry_id": EID, "port": "3"}
    )
    await flow3.async_step_init()
    await flow3.async_step_disable_all()
    check(
        "disable_all: ports 3 & 4 disabled",
        all(disabled_by(3, k) == DISABLER.INTEGRATION for k in DEFAULT_ON)
        and all(disabled_by(4, k) == DISABLER.INTEGRATION for k in DEFAULT_ON),
    )
    check(
        "disable_all: port 5 (up) untouched",
        all(disabled_by(5, k) is None for k in DEFAULT_ON),
    )
    check(
        "disable_all: both issues cleared",
        f"port_down_{EID}_3" not in _ISSUES and f"port_down_{EID}_4" not in _ISSUES,
    )

    # ===== Scenario 8: persistence across reload =====
    saved = new_manager(coord3, FakeEntry(opts()), hass3)
    await saved.async_load()
    check(
        "store round-trips disabled_uids for port 3",
        set(saved._state.get("3", {}).get("disabled_uids", []))
        == {f"{EID}_{HOST}_port_3_{k}" for k in DEFAULT_ON},
    )

    # ===== Scenario 9: user-disabled entity is never our business =====
    reset_world()
    seed_registry([6])
    # user manually disabled one default-on extra
    for e in _REG.entries:
        if e.unique_id == f"{EID}_{HOST}_port_6_tx_rate":
            e.disabled_by = DISABLER.USER
    coord6 = FakeCoordinator([6])
    entry6 = FakeEntry(opts())
    hass6 = FakeHass()
    mgr6 = new_manager(coord6, entry6, hass6)
    await mgr6.async_load()
    hass6.data[DOMAIN][EID] = coord6
    coord6.entity_manager = mgr6
    coord6.set_status(6, False)
    CLOCK.t += 1_000
    await mgr6._async_reconcile()
    CLOCK.t += 25 * 3600
    await mgr6._async_reconcile()
    flow6 = rp.PortDownRepairFlow(
        hass6, f"port_down_{EID}_6", {"entry_id": EID, "port": "6"}
    )
    await flow6.async_step_init()
    await flow6.async_step_disable_this()
    check(
        "user-disabled entity left as USER (not claimed by us)",
        disabled_by(6, "tx_rate") == DISABLER.USER,
    )
    check(
        "user-disabled uid NOT in our recorded set",
        f"{EID}_{HOST}_port_6_tx_rate" not in set(mgr6._state["6"]["disabled_uids"]),
    )
    # bring 6 back up; USER-disabled entity must stay USER-disabled
    coord6.set_status(6, True)
    for _ in range(3):
        await mgr6._async_reconcile()
    check(
        "restore never re-enables USER-disabled entity",
        disabled_by(6, "tx_rate") == DISABLER.USER,
    )
    check(
        "restore re-enabled the 3 we owned",
        all(disabled_by(6, k) is None for k in DEFAULT_ON if k != "tx_rate"),
    )

    # ===== Scenario 10: ifLastChange accelerator =====
    reset_world()
    seed_registry([7, 8, 9])
    coord7 = FakeCoordinator([7, 8, 9])
    entry7 = FakeEntry(opts(grace_h=24))
    hass7 = FakeHass()
    mgr7 = new_manager(coord7, entry7, hass7)
    await mgr7.async_load()
    hass7.data[DOMAIN][EID] = coord7
    coord7.entity_manager = mgr7
    CLOCK.t += 1_000
    # port 7: switch says down 30h (> 24h grace) -> flag on FIRST poll
    coord7.set_status(7, False, last_change=30 * 3600)
    # port 8: switch says down 10 min (< grace) -> must still wait
    coord7.set_status(8, False, last_change=600)
    # port 9: no ifLastChange data -> fall back to observed-time wait
    coord7.set_status(9, False, last_change=None)
    await mgr7._async_reconcile()
    check(
        "accelerator: long-down port flagged on first poll",
        f"port_down_{EID}_7" in _ISSUES,
    )
    check(
        "accelerator: recently-down port NOT flagged early",
        f"port_down_{EID}_8" not in _ISSUES,
    )
    check(
        "accelerator: no-data port NOT flagged early (fallback)",
        f"port_down_{EID}_9" not in _ISSUES,
    )
    # port 8 only needs the remaining ~grace-600s; jump just past it
    CLOCK.t += 24 * 3600
    await mgr7._async_reconcile()
    check(
        "non-accelerated ports flag after real grace elapses",
        f"port_down_{EID}_8" in _ISSUES and f"port_down_{EID}_9" in _ISSUES,
    )

    # ===== Scenario 11: issue persistence across restart + feature-off =====
    reset_world()
    seed_registry([10])
    coordA = FakeCoordinator([10])
    entryA = FakeEntry(opts(grace_h=1))
    hassA = FakeHass()
    mgrA = new_manager(coordA, entryA, hassA)
    await mgrA.async_load()
    hassA.data[DOMAIN][EID] = coordA
    coordA.entity_manager = mgrA
    coordA.set_status(10, False)
    CLOCK.t += 100
    await mgrA._async_reconcile()
    CLOCK.t += 2 * 3600
    await mgrA._async_reconcile()
    iidA = f"port_down_{EID}_10"
    check("scenario11: port flagged", iidA in _ISSUES)
    check(
        "issue created with is_persistent=True",
        _ISSUES[iidA].get("is_persistent") is True,
    )

    # Simulate HA restart (drops only non-persistent issues) + fresh manager
    # loading the persisted Store.
    _simulate_ha_restart()
    check("persistent issue survives HA restart", iidA in _ISSUES)
    coordB = FakeCoordinator([10])
    coordB.set_status(10, False)
    entryB = FakeEntry(opts(grace_h=1))
    hassB = FakeHass()
    mgrB = new_manager(coordB, entryB, hassB)
    await mgrB.async_load()
    hassB.data[DOMAIN][EID] = coordB
    coordB.entity_manager = mgrB
    check("restart: issue_open restored from Store", mgrB._state["10"]["issue_open"])
    CLOCK.t += 100
    await mgrB._async_reconcile()
    check(
        "restart: issue still present (not lost), no duplicate churn",
        iidA in _ISSUES,
    )

    # Feature turned off must clear the now-persistent issue.
    entryB.options = opts(enabled=False, grace_h=1)
    await mgrB._async_reconcile()
    check("feature off clears persistent issue", iidA not in _ISSUES)
    check("feature off resets issue_open", not mgrB._state["10"]["issue_open"])

    # ===== Scenario 12: flapping auto-suppress + self-correction =====
    reset_world()
    seed_registry([11])
    coord11 = FakeCoordinator([11])
    entry11 = FakeEntry(opts(grace_h=24))  # grace/2 window = 12h
    hass11 = FakeHass()
    mgr11 = new_manager(coord11, entry11, hass11)
    await mgr11.async_load()
    hass11.data[DOMAIN][EID] = coord11
    coord11.entity_manager = mgr11
    iid11 = f"port_down_{EID}_11"

    def flapping11() -> bool:
        return em.PortEntityManager._is_flapping(mgr11._state["11"], CLOCK.t, 24 * 3600)

    CLOCK.t += 1_000
    coord11.set_status(11, False)
    await mgr11._async_reconcile()  # seeds last_status, 0 transitions
    check("first down (0 transitions): not flapping", not flapping11())
    CLOCK.t += 3600
    coord11.set_status(11, True)
    await mgr11._async_reconcile()  # transition #1 (down→up)
    check("1 transition (down→up): not flapping yet", not flapping11())
    CLOCK.t += 3600
    coord11.set_status(11, False)
    await mgr11._async_reconcile()  # transition #2 (up→down)
    check(
        "2 transitions (down→up→down) inside grace/2: flapping",
        flapping11(),
    )
    CLOCK.t += 3600
    coord11.set_status(11, True)
    await mgr11._async_reconcile()  # transition #3 (down→up)

    # Switch now reports the port down 30h (> grace) — the accelerator would
    # normally flag it immediately, but it is flapping, so suppress.
    CLOCK.t += 3600
    coord11.set_status(11, False, last_change=30 * 3600)
    await mgr11._async_reconcile()
    check(
        "flapping + down>grace (accelerator): issue suppressed",
        iid11 not in _ISSUES,
    )

    # Link goes quiet; once the flap window empties, long-down warns again.
    CLOCK.t += 13 * 3600  # > grace/2 (12h) since the last transition
    coord11.set_status(11, False)
    await mgr11._async_reconcile()
    check("flap window emptied: long-down warning resumes", iid11 in _ISSUES)

    # ===== Scenario 13: manual 'allow_flap' (persistent until stable) =====
    reset_world()
    seed_registry([12])
    coord12 = FakeCoordinator([12])
    entry12 = FakeEntry(opts(grace_h=24))
    hass12 = FakeHass()
    mgr12 = new_manager(coord12, entry12, hass12)
    await mgr12.async_load()
    hass12.data[DOMAIN][EID] = coord12
    coord12.entity_manager = mgr12
    iid12 = f"port_down_{EID}_12"

    coord12.set_status(12, False)
    CLOCK.t += 1_000
    await mgr12._async_reconcile()
    CLOCK.t += 25 * 3600
    await mgr12._async_reconcile()
    check("port12 down>grace: issue raised", iid12 in _ISSUES)

    flow12 = rp.PortDownRepairFlow(hass12, iid12, {"entry_id": EID, "port": "12"})
    m12 = await flow12.async_step_init()
    check("menu offers allow_flap", "allow_flap" in m12["menu_options"])
    res12 = await flow12.async_step_allow_flap()
    check("allow_flap completes flow", res12["type"] == "create_entry")
    check("allow_flap: issue cleared", iid12 not in _ISSUES)
    check(
        "allow_flap: extras NOT disabled",
        all(disabled_by(12, k) is None for k in DEFAULT_ON),
    )
    check("allow_flap: port marked flap_muted", mgr12._state["12"]["flap_muted"])

    CLOCK.t += 100 * 3600
    await mgr12._async_reconcile()
    check("allow_flap: no re-raise while muted + down", iid12 not in _ISSUES)

    # A single brief up must NOT clear the mute (this is the 'ignore' trap).
    coord12.set_status(12, True)
    await mgr12._async_reconcile()
    check(
        "allow_flap: single up keeps mute (unlike ignore)",
        mgr12._state["12"]["flap_muted"],
    )
    coord12.set_status(12, False)
    CLOCK.t += 26 * 3600
    await mgr12._async_reconcile()
    check("allow_flap: still muted after brief up→down", iid12 not in _ISSUES)

    # Sustained up (up_restore_cycles consecutive polls) clears the mute.
    coord12.set_status(12, True)
    for _ in range(3):
        await mgr12._async_reconcile()
    check(
        "allow_flap: mute cleared after sustained recovery",
        not mgr12._state["12"]["flap_muted"],
    )

    # Re-armed: a fresh long-down stretch warns again.
    coord12.set_status(12, False)
    CLOCK.t += 1_000
    await mgr12._async_reconcile()
    CLOCK.t += 25 * 3600  # also ages out incidental flap transitions
    await mgr12._async_reconcile()
    check("allow_flap: re-arms after stable recovery", iid12 in _ISSUES)

    # ===== Scenario 14: open issue retracted once suppressed; persistence ==
    reset_world()
    seed_registry([13])
    coord13 = FakeCoordinator([13])
    entry13 = FakeEntry(opts(grace_h=24))
    hass13 = FakeHass()
    mgr13 = new_manager(coord13, entry13, hass13)
    await mgr13.async_load()
    hass13.data[DOMAIN][EID] = coord13
    coord13.entity_manager = mgr13
    iid13 = f"port_down_{EID}_13"

    coord13.set_status(13, False)
    CLOCK.t += 1_000
    await mgr13._async_reconcile()
    CLOCK.t += 25 * 3600
    await mgr13._async_reconcile()
    check("port13 down>grace: issue raised", iid13 in _ISSUES)

    # Port still down, but now flagged suppressed → reconcile retracts it.
    mgr13._state["13"]["flap_muted"] = True
    await mgr13._async_reconcile()
    check(
        "open issue retracted once port becomes suppressed",
        iid13 not in _ISSUES and not mgr13._state["13"]["issue_open"],
    )

    saved13 = new_manager(coord13, FakeEntry(opts(grace_h=24)), hass13)
    await saved13.async_load()
    check(
        "flap_muted round-trips via Store",
        saved13._state.get("13", {}).get("flap_muted") is True,
    )

    # A legacy store entry (pre-flap keys) must load with safe defaults.
    _STORE_BACKING[f"{DOMAIN}.{EID}.entity_manager"] = {
        "ports": {
            "13": {
                "down_since": None,
                "ignored": False,
                "disabled_uids": [],
                "issue_open": False,
            }
        }
    }
    legacy13 = new_manager(coord13, FakeEntry(opts(grace_h=24)), hass13)
    await legacy13.async_load()
    ls = legacy13._state["13"]
    check(
        "legacy store gains new flap keys via default-merge",
        not ls.get("flap_muted")
        and ls.get("transitions") == []
        and ls.get("last_status") is None,
    )

    # ===== Summary =====
    total = len(RESULTS)
    passed = sum(1 for _, ok in RESULTS if ok)
    print(f"\n{passed}/{total} checks passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
