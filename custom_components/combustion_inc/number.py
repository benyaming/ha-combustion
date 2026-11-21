"""Numbers: probe removal target, gauge alarm limits and the Engine set point."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DeviceKind
from .entity import CombustionEntity, CombustionEntityDescription, async_setup_platform, kinds
from .hub import CombustionConfigEntry, CombustionHub
from .models import CombustionDevice, ProbeDevice
from .protocol import AlarmStatus
from .protocol.const import PredictionMode

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class CombustionNumberDescription(CombustionEntityDescription, NumberEntityDescription):
    value_fn: Callable[[Any], float | None]
    set_fn: Callable[[CombustionHub, Any, float], Awaitable[None]]


async def _set_removal_target(hub: CombustionHub, probe: ProbeDevice, value: float) -> None:
    mode = probe.prediction.mode if probe.prediction else PredictionMode.NONE
    if mode not in (PredictionMode.TIME_TO_REMOVAL, PredictionMode.REMOVAL_AND_RESTING):
        mode = PredictionMode.TIME_TO_REMOVAL
    await hub.async_set_prediction(probe, mode, value)


def _probe_status(device: CombustionDevice, now: float) -> bool:
    return (
        isinstance(device, ProbeDevice) and device.has_status(now) and device.prediction is not None
    )


def _alarm_temperature(alarm: AlarmStatus | None) -> float | None:
    # An alarm off and still at the zero point has never been set: show unknown, not -20 °C.
    return alarm.temperature if alarm is not None and alarm.configured else None


def _temperature(**kwargs: Any) -> CombustionNumberDescription:
    return CombustionNumberDescription(
        device_class=NumberDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        mode=NumberMode.BOX,
        requires_connection=True,
        **kwargs,
    )


NUMBERS: tuple[CombustionNumberDescription, ...] = (
    _temperature(
        key="removal_temperature",
        translation_key="removal_temperature",
        kinds=kinds(DeviceKind.PROBE),
        native_min_value=0,
        native_max_value=100,
        native_step=0.1,
        available_fn=_probe_status,
        value_fn=lambda d: (
            d.prediction.set_point
            if d.prediction and d.prediction.mode is not PredictionMode.NONE
            else None
        ),
        set_fn=_set_removal_target,
    ),
    _temperature(
        key="high_alarm_temperature",
        translation_key="high_alarm_temperature",
        kinds=kinds(DeviceKind.GAUGE),
        native_min_value=-20,
        native_max_value=799,
        native_step=1,
        value_fn=lambda d: _alarm_temperature(d.high_alarm),
        set_fn=lambda hub, d, v: hub.async_set_gauge_alarm(d, high=True, temperature=v),
    ),
    _temperature(
        key="low_alarm_temperature",
        translation_key="low_alarm_temperature",
        kinds=kinds(DeviceKind.GAUGE),
        native_min_value=-20,
        native_max_value=799,
        native_step=1,
        value_fn=lambda d: _alarm_temperature(d.low_alarm),
        set_fn=lambda hub, d, v: hub.async_set_gauge_alarm(d, high=False, temperature=v),
    ),
    _temperature(
        key="set_point",
        translation_key="set_point",
        kinds=kinds(DeviceKind.ENGINE),
        # The official app limits the Engine to 150-575 °F.
        native_min_value=65,
        native_max_value=302,
        native_step=1,
        value_fn=lambda d: d.set_point,
        set_fn=lambda hub, d, v: hub.async_set_engine_set_point(d, v),
    ),
)


class CombustionNumber(CombustionEntity, NumberEntity):
    entity_description: CombustionNumberDescription

    @property
    def native_value(self) -> float | None:
        return self.entity_description.value_fn(self.device)

    async def async_set_native_value(self, value: float) -> None:
        await self.entity_description.set_fn(self.hub, self.device, value)
        self.async_write_ha_state()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CombustionConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_setup_platform(hass, entry, async_add_entities, NUMBERS, CombustionNumber)
