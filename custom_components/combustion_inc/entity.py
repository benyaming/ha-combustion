"""Base entity and platform setup shared by every Combustion platform."""

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity, EntityDescription
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN, MANUFACTURER, MODELS, NAMES, DeviceKind
from .hub import CombustionConfigEntry, CombustionHub
from .models import CombustionDevice


def _always(_device: CombustionDevice, _now: float) -> bool:
    return True


@dataclass(frozen=True, kw_only=True)
class CombustionEntityDescription(EntityDescription):
    kinds: frozenset[DeviceKind]
    # Needs data or control that only a GATT connection provides.
    requires_connection: bool = False
    # A read-only twin of a control that replaces it when connections are enabled.
    replaced_by_control: bool = False
    available_fn: Callable[[CombustionDevice, float], bool] = _always


def kinds(*values: DeviceKind) -> frozenset[DeviceKind]:
    return frozenset(values)


ALL_KINDS = frozenset(DeviceKind)
NODES = kinds(DeviceKind.GAUGE, DeviceKind.DISPLAY, DeviceKind.BOOSTER, DeviceKind.ENGINE)


class CombustionEntity(Entity):
    _attr_has_entity_name = True
    _attr_should_poll = False
    entity_description: CombustionEntityDescription

    def __init__(
        self, hub: CombustionHub, device: CombustionDevice, description: CombustionEntityDescription
    ) -> None:
        self.hub = hub
        self.device = device
        self.entity_description = description
        self._attr_unique_id = f"{device.key}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.key)},
            name=f"{NAMES[device.kind]} {device.serial}",
            manufacturer=MANUFACTURER,
            model=MODELS[device.kind],
            serial_number=device.serial,
        )

    @property
    def available(self) -> bool:
        return self.device.available and self.entity_description.available_fn(
            self.device, self.hub.now()
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, self.hub.signal_update(self.device.key), self._async_handle_update
            )
        )

    @callback
    def _async_handle_update(self) -> None:
        self.async_write_ha_state()


def _wanted(description: CombustionEntityDescription, connections_enabled: bool) -> bool:
    if connections_enabled:
        return not description.replaced_by_control
    return not description.requires_connection


def async_setup_platform(
    hass: HomeAssistant,
    entry: CombustionConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
    descriptions: Iterable[CombustionEntityDescription],
    factory: Callable[[CombustionHub, CombustionDevice, CombustionEntityDescription], Entity],
) -> None:
    """Add entities for known devices now and for new ones as they appear."""
    hub = entry.runtime_data
    descriptions = tuple(descriptions)

    @callback
    def _add(device: CombustionDevice) -> None:
        async_add_entities(
            factory(hub, device, description)
            for description in descriptions
            if device.kind in description.kinds and _wanted(description, hub.active_enabled)
        )

    for device in list(hub.devices.values()):
        _add(device)
    entry.async_on_unload(async_dispatcher_connect(hass, hub.signal_new_device, _add))
