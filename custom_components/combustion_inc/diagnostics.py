"""Diagnostics: decoded state plus raw packets, to check the decoder on real hardware."""

from dataclasses import asdict
from typing import Any

from homeassistant.core import HomeAssistant

from .hub import CombustionConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: CombustionConfigEntry
) -> dict[str, Any]:
    hub = entry.runtime_data
    return {
        "options": dict(entry.options),
        "connections": hub.connections.diagnostics() if hub.connections else None,
        "node_addresses": dict(hub.index.node_addresses),
        "devices": {key: asdict(device) for key, device in hub.devices.items()},
    }
