"""Binary sensors for Combustion devices."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DeviceKind
from .entity import (
    ALL_KINDS,
    CombustionEntity,
    CombustionEntityDescription,
    async_setup_platform,
    kinds,
)
from .hub import CombustionConfigEntry
from .models import CombustionDevice, ProbeDevice

PARALLEL_UPDATES = 0

PROBE = kinds(DeviceKind.PROBE)
GAUGE = kinds(DeviceKind.GAUGE)
ENGINE = kinds(DeviceKind.ENGINE)


@dataclass(frozen=True, kw_only=True)
class CombustionBinarySensorDescription(CombustionEntityDescription, BinarySensorEntityDescription):
    is_on_fn: Callable[[Any], bool | None]
    attributes_fn: Callable[[Any], dict[str, Any] | None] | None = None


def _probe_alarming(device: ProbeDevice) -> bool | None:
    if device.high_alarms is None or device.low_alarms is None:
        return None
    return any(a.alarming for a in (*device.high_alarms, *device.low_alarms))


def _probe_alarm_known(device: CombustionDevice, now: float) -> bool:
    return (
        isinstance(device, ProbeDevice)
        and device.has_status(now)
        and device.high_alarms is not None
    )


BINARY_SENSORS: tuple[CombustionBinarySensorDescription, ...] = (
    # Probe
    CombustionBinarySensorDescription(
        key="battery_low",
        kinds=PROBE | GAUGE,
        device_class=BinarySensorDeviceClass.BATTERY,
        is_on_fn=lambda d: d.battery_low if isinstance(d, ProbeDevice) else d.low_battery,
    ),
    CombustionBinarySensorDescription(
        key="overheating",
        translation_key="overheating",
        kinds=PROBE,
        device_class=BinarySensorDeviceClass.HEAT,
        is_on_fn=lambda d: bool(d.overheating) if d.overheating is not None else None,
        attributes_fn=lambda d: {
            "sensors": [f"T{i + 1}" for i in d.overheating or ()],
            "reported_mask": d.overheat_raw,
        },
    ),
    CombustionBinarySensorDescription(
        key="alarm",
        translation_key="alarm",
        kinds=PROBE,
        requires_connection=True,
        device_class=BinarySensorDeviceClass.PROBLEM,
        available_fn=_probe_alarm_known,
        is_on_fn=_probe_alarming,
    ),
    # Gauge
    CombustionBinarySensorDescription(
        key="sensor_present",
        translation_key="sensor_present",
        kinds=GAUGE,
        device_class=BinarySensorDeviceClass.PLUG,
        is_on_fn=lambda d: d.sensor_present,
    ),
    CombustionBinarySensorDescription(
        key="gauge_overheating",
        translation_key="overheating",
        kinds=GAUGE,
        device_class=BinarySensorDeviceClass.HEAT,
        is_on_fn=lambda d: d.overheating,
    ),
    CombustionBinarySensorDescription(
        key="high_alarm",
        translation_key="high_alarm",
        kinds=GAUGE,
        device_class=BinarySensorDeviceClass.PROBLEM,
        is_on_fn=lambda d: d.high_alarm.tripped if d.high_alarm else None,
        attributes_fn=lambda d: {"alarming": d.high_alarm.alarming} if d.high_alarm else None,
    ),
    CombustionBinarySensorDescription(
        key="low_alarm",
        translation_key="low_alarm",
        kinds=GAUGE,
        device_class=BinarySensorDeviceClass.PROBLEM,
        is_on_fn=lambda d: d.low_alarm.tripped if d.low_alarm else None,
        attributes_fn=lambda d: {"alarming": d.low_alarm.alarming} if d.low_alarm else None,
    ),
    # Engine
    CombustionBinarySensorDescription(
        key="app_mode",
        translation_key="app_mode",
        kinds=ENGINE,
        is_on_fn=lambda d: d.app_mode,
    ),
    CombustionBinarySensorDescription(
        key="control_device_connected",
        translation_key="control_device_connected",
        kinds=ENGINE,
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        is_on_fn=lambda d: d.control_device_connected,
    ),
    CombustionBinarySensorDescription(
        key="lid_open",
        translation_key="lid_open",
        kinds=ENGINE,
        device_class=BinarySensorDeviceClass.OPENING,
        is_on_fn=lambda d: d.lid_open,
    ),
    CombustionBinarySensorDescription(
        key="fixed_speed",
        translation_key="fixed_speed",
        kinds=ENGINE,
        entity_category=EntityCategory.DIAGNOSTIC,
        is_on_fn=lambda d: d.fixed_speed,
    ),
    # Every device
    CombustionBinarySensorDescription(
        key="connected",
        translation_key="connected",
        kinds=ALL_KINDS,
        requires_connection=True,
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        is_on_fn=lambda d: d.connected,
    ),
)


class CombustionBinarySensor(CombustionEntity, BinarySensorEntity):
    entity_description: CombustionBinarySensorDescription

    @property
    def available(self) -> bool:
        # The connection indicator stays meaningful while the device is out of range.
        if self.entity_description.key == "connected":
            return True
        return super().available

    @property
    def is_on(self) -> bool | None:
        return self.entity_description.is_on_fn(self.device)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attributes_fn is None:
            return None
        return self.entity_description.attributes_fn(self.device)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CombustionConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_setup_platform(hass, entry, async_add_entities, BINARY_SENSORS, CombustionBinarySensor)
