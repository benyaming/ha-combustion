"""Fixtures for Combustion Inc integration tests."""

import time
from collections.abc import Generator

import pytest
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_get_advertisement_callback,
)
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.combustion_inc.const import DOMAIN
from custom_components.combustion_inc.hub import CombustionHub

VENDOR_ID = 0x09C7
GAUGE_ADDRESS = "AA:BB:CC:00:00:03"
PROBE_ADDRESS = "AA:BB:CC:00:00:01"
DISPLAY_ADDRESS = "AA:BB:CC:00:00:04"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> Generator[None]:
    yield


def service_info(
    address: str, data: bytes, rssi: int = -60, connectable: bool = False
) -> BluetoothServiceInfoBleak:
    adv = AdvertisementData(
        local_name=None,
        manufacturer_data={VENDOR_ID: data},
        service_data={},
        service_uuids=[],
        tx_power=None,
        rssi=rssi,
        platform_data=(),
    )
    return BluetoothServiceInfoBleak(
        name=address,
        address=address,
        rssi=rssi,
        manufacturer_data={VENDOR_ID: data},
        service_data={},
        service_uuids=[],
        source="local",
        device=BLEDevice(address, None, {}),
        advertisement=adv,
        connectable=connectable,
        time=time.monotonic(),
        tx_power=None,
    )


def inject(hass: HomeAssistant, address: str, data: bytes, **kwargs: object) -> None:
    async_get_advertisement_callback(hass)(service_info(address, data, **kwargs))  # type: ignore[arg-type]


@pytest.fixture
def config_entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, title="Combustion Inc")
    entry.add_to_hass(hass)
    return entry


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
async def hub(
    hass: HomeAssistant, enable_bluetooth: None, config_entry: MockConfigEntry
) -> CombustionHub:
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    hub = config_entry.runtime_data
    hub.now = Clock()
    return hub


async def refresh(hass: HomeAssistant, hub: CombustionHub) -> None:
    hub._async_check_availability()
    hub._async_flush()
    await hass.async_block_till_done()


def state(hass: HomeAssistant, entity_id: str) -> str:
    current = hass.states.get(entity_id)
    assert current is not None, f"{entity_id} does not exist"
    return current.state
