"""The hub: one Bluetooth listener for every Combustion device in range."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval

from .connection import ConnectionManager
from .const import (
    AVAILABILITY_INTERVAL,
    CONF_ACTIVE_CONNECTION,
    DEFAULT_ACTIVE_CONNECTION,
    DOMAIN,
    FLUSH_INTERVAL,
    DeviceKind,
)
from .models import (
    CombustionDevice,
    DeviceIndex,
    EngineDevice,
    GaugeDevice,
    NodeDevice,
    Outcome,
    ProbeDevice,
    make_device,
)
from .protocol import (
    VENDOR_ID,
    AlarmStatus,
    EngineAdvertisement,
    EngineStatus,
    GaugeAdvertisement,
    GaugeStatus,
    NodeAdvertisement,
    ProbeAdvertisement,
    ProbeStatus,
    ProductType,
    format_probe_serial,
    parse_advertisement,
)
from .protocol.const import PredictionMode

_LOGGER = logging.getLogger(__name__)

NODE_KINDS = {
    ProductType.GAUGE: DeviceKind.GAUGE,
    ProductType.DISPLAY: DeviceKind.DISPLAY,
    ProductType.BOOSTER: DeviceKind.BOOSTER,
    ProductType.ENGINE: DeviceKind.ENGINE,
}

type CombustionConfigEntry = ConfigEntry[CombustionHub]


class CombustionHub:
    """Merges advertisements and GATT status into per-device state."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: CombustionConfigEntry,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.now = clock
        self.index = DeviceIndex()
        self._dirty: set[str] = set()
        self._heard: dict[str, float] = {}  # address -> last packet, any adapter
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._unsubs: list[Callable[[], None]] = []
        self.connections: ConnectionManager | None = None  # only when enabled
        self.signal_new_device = f"{DOMAIN}_{entry.entry_id}_new_device"

    @property
    def active_enabled(self) -> bool:
        return self.entry.options.get(CONF_ACTIVE_CONNECTION, DEFAULT_ACTIVE_CONNECTION)

    @property
    def devices(self) -> dict[str, CombustionDevice]:
        return self.index.devices

    def signal_update(self, key: str) -> str:
        return f"{DOMAIN}_{self.entry.entry_id}_{key}_update"

    # --- lifecycle ----------------------------------------------------------

    @callback
    def async_restore_devices(self) -> None:
        """Recreate devices seen before, so their entities exist from the start."""
        registry = dr.async_get(self.hass)
        for entry in dr.async_entries_for_config_entry(registry, self.entry.entry_id):
            for domain, identifier in entry.identifiers:
                if domain != DOMAIN or "_" not in identifier:
                    continue
                kind, serial = identifier.split("_", 1)
                try:
                    device = make_device(DeviceKind(kind), serial)
                except ValueError:
                    continue
                self.index.add(device)

    @callback
    def async_start(self) -> None:
        self._unsubs.append(
            bluetooth.async_register_callback(
                self.hass,
                self._async_on_advertisement,
                bluetooth.BluetoothCallbackMatcher(manufacturer_id=VENDOR_ID, connectable=False),
                bluetooth.BluetoothScanningMode.PASSIVE,
            )
        )
        self._unsubs.append(
            async_track_time_interval(
                self.hass, self._async_flush, timedelta(seconds=FLUSH_INTERVAL)
            )
        )
        self._unsubs.append(
            async_track_time_interval(
                self.hass,
                self._async_check_availability,
                timedelta(seconds=AVAILABILITY_INTERVAL),
            )
        )
        if self.active_enabled:
            self.connections = ConnectionManager(self)
            self.connections.async_start()

    async def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self.connections is not None:
            await self.connections.async_stop()

    # --- device bookkeeping -------------------------------------------------

    def _get_or_create(self, kind: DeviceKind, serial: str) -> CombustionDevice:
        key = f"{kind}_{serial}"
        if (device := self.index.devices.get(key)) is None:
            device = make_device(kind, serial)
            self.index.add(device)
            _LOGGER.debug("Discovered %s %s", kind, serial)
            async_dispatcher_send(self.hass, self.signal_new_device, device)
        return device

    def probe(self, serial: int) -> ProbeDevice:
        device = self._get_or_create(DeviceKind.PROBE, format_probe_serial(serial))
        assert isinstance(device, ProbeDevice)
        return device

    def mark_dirty(self, device: CombustionDevice) -> None:
        if not device.available:
            device.available = True
        self._dirty.add(device.key)

    @callback
    def _async_flush(self, _now: object = None) -> None:
        dirty, self._dirty = self._dirty, set()
        for key in dirty:
            async_dispatcher_send(self.hass, self.signal_update(key))

    @callback
    def _async_check_availability(self, _now: object = None) -> None:
        now = self.now()
        for device in self.index.devices.values():
            last_heard = self._heard.get(device.address) if device.address else None
            available = device.compute_available(now, last_heard)
            # Instant Read and connection-only values expire on their own clocks.
            if available != device.available or isinstance(device, ProbeDevice | EngineDevice):
                device.available = available
                self._dirty.add(device.key)

    def _watch(self, address: str) -> None:
        """Note every packet from a Combustion address, whichever adapter heard it.

        Home Assistant skips callbacks for unchanged advertisements, lists a silent
        address for minutes, and with several adapters keeps the stronger one's last
        packet in its history after that one lost the device. None of those says when
        the device was last heard.
        """
        if address in self._heard:
            return
        self._unsubs.append(
            bluetooth.async_register_advertisement_callback(
                self.hass, self._async_on_packet, address
            )
        )

    @callback
    def _async_on_packet(self, service_info: bluetooth.BluetoothServiceInfoBleak) -> None:
        self._heard[service_info.address] = self.now()

    # --- advertisements -----------------------------------------------------

    @callback
    def _async_on_advertisement(
        self, service_info: bluetooth.BluetoothServiceInfoBleak, change: bluetooth.BluetoothChange
    ) -> None:
        data = service_info.manufacturer_data.get(VENDOR_ID)
        if not data:
            return
        try:
            adv = parse_advertisement(data)
        except Exception:  # a malformed packet must never break the listener
            _LOGGER.debug("Unparseable advertisement from %s: %s", service_info.address, data.hex())
            return
        if adv is None:
            return
        now = self.now()
        address, rssi = service_info.address, service_info.rssi
        self._watch(address)
        self._heard[address] = now
        match adv:
            case ProbeAdvertisement():
                self._handle_probe_advertisement(adv, now, address, rssi, data)
            case GaugeAdvertisement():
                gauge = self._node(DeviceKind.GAUGE, adv.serial, now, address, rssi, data)
                assert isinstance(gauge, GaugeDevice)
                gauge.apply_advertisement(adv)
                self.mark_dirty(gauge)
            case EngineAdvertisement():
                engine = self._node(DeviceKind.ENGINE, adv.serial, now, address, rssi, data)
                assert isinstance(engine, EngineDevice)
                engine.apply_advertisement(adv)
                self.mark_dirty(engine)
            case NodeAdvertisement():
                node = self._node(
                    NODE_KINDS[adv.product_type], adv.serial, now, address, rssi, data
                )
                assert isinstance(node, NodeDevice)
                node.apply_advertisement(adv)
                self.mark_dirty(node)

    def _node(
        self, kind: DeviceKind, serial: str, now: float, address: str, rssi: int, raw: bytes
    ) -> CombustionDevice:
        device = self._get_or_create(kind, serial)
        device.note_own_advertisement(now, address, rssi, raw)
        self.index.node_addresses[address] = device.key
        return device

    def _handle_probe_advertisement(
        self, adv: ProbeAdvertisement, now: float, address: str, rssi: int, raw: bytes
    ) -> None:
        if adv.serial == 0:
            return  # a node advertising that it has no probes
        probe = self.probe(adv.serial)
        if not adv.is_repeated:
            probe.last_raw = raw.hex()
        if probe.apply_advertisement(adv, now, address, rssi):
            self.mark_dirty(probe)

    # --- GATT status (from connection.py) -----------------------------------

    def handle_probe_status(self, status: ProbeStatus, serial: int, via_node: bool = False) -> None:
        probe = self.probe(serial)
        now = self.now()
        outcome = probe.apply_status(status, now)
        if via_node and outcome in (Outcome.APPLIED, Outcome.CURRENT):
            # Only data of the current cook proves MeatNet delivers this probe.
            probe.last_node_status = now
        if outcome is Outcome.APPLIED:
            self.mark_dirty(probe)

    def handle_gauge_status(self, status: GaugeStatus) -> None:
        gauge = self._get_or_create(DeviceKind.GAUGE, status.serial)
        assert isinstance(gauge, GaugeDevice)
        if gauge.apply_status(status, self.now()) is Outcome.APPLIED:
            self.mark_dirty(gauge)

    def handle_engine_status(self, status: EngineStatus) -> None:
        engine = self._get_or_create(DeviceKind.ENGINE, status.serial)
        assert isinstance(engine, EngineDevice)
        if engine.apply_status(status, self.now()) is Outcome.APPLIED:
            self.mark_dirty(engine)

    def set_connected(self, address: str, connected: bool, probe_serial: int | None = None) -> None:
        if probe_serial is not None:
            device: CombustionDevice | None = self.index.probes.get(probe_serial)
        else:
            key = self.index.node_addresses.get(address)
            device = self.index.devices.get(key) if key else None
        if device is not None and device.connected != connected:
            device.connected = connected
            self._dirty.add(device.key)

    # --- commands -----------------------------------------------------------

    def _require_connections(self) -> ConnectionManager:
        if self.connections is None:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="active_connection_disabled"
            )
        return self.connections

    async def async_set_prediction(
        self, probe: ProbeDevice, mode: PredictionMode, set_point: float
    ) -> None:
        await self._require_connections().async_set_prediction(probe.serial_number, mode, set_point)

    async def async_silence_alarms(self) -> None:
        await self._require_connections().async_silence_alarms()

    async def async_set_gauge_alarm(
        self,
        gauge: GaugeDevice,
        *,
        high: bool,
        enabled: bool | None = None,
        temperature: float | None = None,
    ) -> None:
        """Change one gauge alarm and send both, keeping the other as reported.

        Setting a temperature without saying otherwise also turns the alarm on. The
        whole read, send and update runs under a per-gauge lock, so two edits made at
        once cannot each send a stale copy of the other's alarm.
        """
        connections = self._require_connections()
        async with self._locks[gauge.key]:
            current_high = gauge.high_alarm or AlarmStatus()
            current_low = gauge.low_alarm or AlarmStatus()
            current = current_high if high else current_low
            if enabled is None:
                enabled = True if temperature is not None else current.set
            if enabled and temperature is None and not current.configured:
                # Arming it as is would put a high alarm at -20 °C, tripped at once.
                raise ServiceValidationError(
                    translation_domain=DOMAIN, translation_key="alarm_temperature_required"
                )
            updated = replace(
                current,
                set=enabled,
                temperature=current.temperature if temperature is None else temperature,
            )
            new_high, new_low = (updated, current_low) if high else (current_high, updated)
            await connections.async_set_gauge_alarms(gauge.serial, new_high, new_low)
            gauge.high_alarm, gauge.low_alarm = new_high, new_low
            self.mark_dirty(gauge)

    async def async_set_engine_set_point(self, engine: EngineDevice, celsius: float) -> None:
        if engine.app_mode is False:
            # The Engine only takes a set point from an app while in app mode.
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="engine_not_in_app_mode"
            )
        await self._require_connections().async_set_engine_set_point(engine.serial, celsius)
        engine.set_point = celsius
        self.mark_dirty(engine)
