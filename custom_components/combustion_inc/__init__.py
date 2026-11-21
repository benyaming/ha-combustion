"""The Combustion Inc integration."""

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .hub import CombustionConfigEntry, CombustionHub

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]


async def async_setup_entry(hass: HomeAssistant, entry: CombustionConfigEntry) -> bool:
    hub = CombustionHub(hass, entry)
    hub.async_restore_devices()
    entry.runtime_data = hub
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # Listen only once the platforms can take new devices.
    hub.async_start()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: CombustionConfigEntry) -> bool:
    await entry.runtime_data.async_stop()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
