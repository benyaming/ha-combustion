"""Switches: turn the gauge's high and low alarms on and off."""

from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DeviceKind
from .entity import CombustionEntity, CombustionEntityDescription, async_setup_platform, kinds
from .hub import CombustionConfigEntry
from .models import GaugeDevice

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class GaugeAlarmSwitchDescription(CombustionEntityDescription):
    high: bool


SWITCHES = (
    GaugeAlarmSwitchDescription(
        key="high_alarm",
        translation_key="high_alarm",
        kinds=kinds(DeviceKind.GAUGE),
        requires_connection=True,
        high=True,
    ),
    GaugeAlarmSwitchDescription(
        key="low_alarm",
        translation_key="low_alarm",
        kinds=kinds(DeviceKind.GAUGE),
        requires_connection=True,
        high=False,
    ),
)


class GaugeAlarmSwitch(CombustionEntity, SwitchEntity):
    entity_description: GaugeAlarmSwitchDescription

    @property
    def is_on(self) -> bool | None:
        assert isinstance(self.device, GaugeDevice)
        alarm = self.device.high_alarm if self.entity_description.high else self.device.low_alarm
        return alarm.set if alarm is not None else None

    async def _async_set(self, enabled: bool) -> None:
        assert isinstance(self.device, GaugeDevice)
        await self.hub.async_set_gauge_alarm(
            self.device, high=self.entity_description.high, enabled=enabled
        )
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_set(False)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CombustionConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_setup_platform(hass, entry, async_add_entities, SWITCHES, GaugeAlarmSwitch)
