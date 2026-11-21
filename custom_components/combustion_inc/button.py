"""Button: silence alarms, the way the Combustion app does it (all of them)."""

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DeviceKind
from .entity import CombustionEntity, CombustionEntityDescription, async_setup_platform, kinds
from .hub import CombustionConfigEntry

PARALLEL_UPDATES = 1

BUTTONS = (
    CombustionEntityDescription(
        key="silence_alarms",
        translation_key="silence_alarms",
        kinds=kinds(DeviceKind.PROBE, DeviceKind.GAUGE),
        requires_connection=True,
    ),
)


class SilenceAlarmsButton(CombustionEntity, ButtonEntity):
    async def async_press(self) -> None:
        await self.hub.async_silence_alarms()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CombustionConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_setup_platform(hass, entry, async_add_entities, BUTTONS, SilenceAlarmsButton)
