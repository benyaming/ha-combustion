"""Integration behaviour: advertisements in, entity states out."""

from unittest.mock import patch

import pytest
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.combustion_inc.const import CONF_ACTIVE_CONNECTION, DOMAIN
from custom_components.combustion_inc.diagnostics import async_get_config_entry_diagnostics
from custom_components.combustion_inc.hub import CombustionHub
from custom_components.combustion_inc.protocol import ProbeStatus

from .conftest import DISPLAY_ADDRESS, GAUGE_ADDRESS, PROBE_ADDRESS, inject, refresh, state
from .protocol.test_advertising import REAL_GAUGE
from .protocol.test_status import GOLDEN_0X45_FRAME

GAUGE = bytes.fromhex("0341423132434433344546A4060100E347C1120100")
GAUGE_NO_SENSOR = bytes.fromhex("0341423132434433344546A4060600E347C1120100")
# Normal mode, ID 1, yellow; core T1, surface T4, ambient T5. T1 = 50 °C (raw 1400).
PROBE_SERIAL = "12345678"


def probe_packet(product: int, t1_raw: int = 1400, mode: int = 0, network: int = 0) -> bytes:
    temps = [t1_raw, 900, 900, 1000, 1100, 1200, 1300, 1400]
    packed = sum(raw << (13 * i) for i, raw in enumerate(temps))
    tail = bytes([mode, 0x00, network, 0x00]) + (bytes([0x04]) if product == 1 else b"")
    return bytes([product]) + bytes.fromhex("78563412") + packed.to_bytes(13, "little") + tail


async def test_gauge_from_advertisement(
    hass: HomeAssistant, hub: CombustionHub, config_entry: MockConfigEntry
) -> None:
    inject(hass, GAUGE_ADDRESS, GAUGE)
    await hass.async_block_till_done()
    await refresh(hass, hub)

    prefix = "combustion_gauge_ab12cd34ef"
    assert state(hass, f"sensor.{prefix}_temperature") == "150.0"
    assert state(hass, f"binary_sensor.{prefix}_sensor_connected") == "on"
    assert state(hass, f"binary_sensor.{prefix}_battery") == "off"
    assert state(hass, f"binary_sensor.{prefix}_high_alarm") == "on"  # tripped
    assert state(hass, f"binary_sensor.{prefix}_low_alarm") == "off"
    # Connections are on by default, so the alarm limits are controls.
    assert state(hass, f"number.{prefix}_high_alarm_temperature") == "210.0"
    assert state(hass, f"switch.{prefix}_high_alarm") == "on"
    assert hass.states.get(f"sensor.{prefix}_high_alarm_temperature") is None

    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, "gauge_AB12CD34EF"), config_entry.entry_id
    )
    assert device is not None
    assert device.model == "Giant Grill Gauge"
    assert device.serial_number == "AB12CD34EF"

    inject(hass, GAUGE_ADDRESS, GAUGE_NO_SENSOR)
    await hass.async_block_till_done()
    await refresh(hass, hub)
    assert state(hass, f"sensor.{prefix}_temperature") == STATE_UNKNOWN
    assert state(hass, f"binary_sensor.{prefix}_sensor_connected") == "off"


async def test_probe_direct_wins_over_repeated(hass: HomeAssistant, hub: CombustionHub) -> None:
    core = f"sensor.combustion_probe_{PROBE_SERIAL.lower()}_core_temperature"
    hops = f"sensor.combustion_probe_{PROBE_SERIAL.lower()}_meatnet_hops"

    inject(hass, PROBE_ADDRESS, probe_packet(1, t1_raw=1400))
    await hass.async_block_till_done()
    await refresh(hass, hub)
    assert state(hass, core) == "50.0"
    assert state(hass, hops) == "0"

    # A node repeats an older reading: ignored while direct data is fresh.
    hub.now.now += 2
    inject(hass, DISPLAY_ADDRESS, probe_packet(2, t1_raw=1390, network=0x40))
    await hass.async_block_till_done()
    await refresh(hass, hub)
    assert state(hass, core) == "50.0"

    # Once the direct path goes quiet, the repeated data takes over.
    hub.now.now += 6
    inject(hass, DISPLAY_ADDRESS, probe_packet(2, t1_raw=1500, network=0x40))
    await hass.async_block_till_done()
    await refresh(hass, hub)
    assert state(hass, core) == "55.0"
    assert state(hass, hops) == "2"


async def test_probe_instant_read_keeps_normal_readings(
    hass: HomeAssistant, hub: CombustionHub
) -> None:
    base = f"sensor.combustion_probe_{PROBE_SERIAL.lower()}"
    inject(hass, PROBE_ADDRESS, probe_packet(1, t1_raw=1400))
    await hass.async_block_till_done()
    await refresh(hass, hub)
    assert state(hass, f"{base}_instant_read") == STATE_UNAVAILABLE

    inject(hass, PROBE_ADDRESS, probe_packet(1, t1_raw=1800, mode=1))
    await hass.async_block_till_done()
    await refresh(hass, hub)
    assert state(hass, f"{base}_instant_read") == "70.0"
    assert state(hass, f"{base}_core_temperature") == "50.0"
    assert state(hass, f"{base}_mode") == "instant_read"

    hub.now.now += 6
    await refresh(hass, hub)
    assert state(hass, f"{base}_instant_read") == STATE_UNAVAILABLE


async def repeat(
    hass: HomeAssistant,
    hub: CombustionHub,
    address: str,
    data: bytes,
    key: str | None,
    after: float,
) -> None:
    """The same packet again, `after` seconds on: heard, but no change callback."""
    hub.now.now += after
    seen = hub.devices[key].last_seen if key else None
    inject(hass, address, data)
    await hass.async_block_till_done()
    if key:
        assert hub.devices[key].last_seen == seen


async def test_probe_goes_unavailable(hass: HomeAssistant, hub: CombustionHub) -> None:
    core = f"sensor.combustion_probe_{PROBE_SERIAL.lower()}_core_temperature"
    inject(hass, PROBE_ADDRESS, probe_packet(1))
    await hass.async_block_till_done()
    await refresh(hass, hub)
    assert state(hass, core) == "50.0"
    await repeat(hass, hub, PROBE_ADDRESS, probe_packet(1), "probe_12345678", after=29)
    hub.now.now += 2
    await refresh(hass, hub)
    assert state(hass, core) == "50.0"
    # Back in its charger, it stops advertising.
    hub.now.now += 30
    await refresh(hass, hub)
    assert state(hass, core) == STATE_UNAVAILABLE


async def test_gauge_goes_unavailable_before_home_assistant_drops_it(
    hass: HomeAssistant, hub: CombustionHub
) -> None:
    # Seen live: out of range, the gauge stayed available for minutes because Home
    # Assistant keeps a silent address listed until its next sweep.
    temperature = "sensor.combustion_gauge_ab12cd34ef_temperature"
    inject(hass, GAUGE_ADDRESS, GAUGE)
    await hass.async_block_till_done()
    await repeat(hass, hub, GAUGE_ADDRESS, GAUGE, "gauge_AB12CD34EF", after=59)
    hub.now.now += 2
    await refresh(hass, hub)
    assert state(hass, temperature) == "150.0"
    hub.now.now += 60
    await refresh(hass, hub)
    assert state(hass, temperature) == STATE_UNAVAILABLE


async def test_relayed_probe_ignores_node_presence(hass: HomeAssistant, hub: CombustionHub) -> None:
    core = f"sensor.combustion_probe_{PROBE_SERIAL.lower()}_core_temperature"
    inject(hass, DISPLAY_ADDRESS, probe_packet(2, network=0x40))
    await hass.async_block_till_done()
    await refresh(hass, hub)
    assert state(hass, core) == "50.0"
    # The node is still heard; the probe behind it is not.
    await repeat(hass, hub, DISPLAY_ADDRESS, probe_packet(2, network=0x40), None, after=31)
    await refresh(hass, hub)
    assert state(hass, core) == STATE_UNAVAILABLE


async def test_meatnet_status_adds_predictions(hass: HomeAssistant, hub: CombustionHub) -> None:
    status = ProbeStatus.decode_node_payload(GOLDEN_0X45_FRAME[10:])
    assert status is not None and status.serial is not None
    hub.handle_probe_status(status, status.serial)
    await refresh(hass, hub)

    base = "sensor.combustion_probe_12345678"
    assert state(hass, f"{base}_prediction") == "predicting"
    assert state(hass, f"{base}_time_remaining") == "60.0"  # 3600 s, shown in minutes
    assert state(hass, f"{base}_estimated_core_temperature") == "48.5"
    assert state(hass, f"{base}_progress_to_removal") == "66.3"
    assert state(hass, f"{base}_food_safety") == "safe"
    assert state(hass, "number.combustion_probe_12345678_removal_temperature") == "62.5"
    assert state(hass, "select.combustion_probe_12345678_prediction_mode") == "time_to_removal"
    # The golden status is an Instant Read, so it fills the instant value only.
    assert state(hass, f"{base}_instant_read") == "-5.5"
    assert state(hass, "binary_sensor.combustion_probe_12345678_alarm") == "on"


async def test_devices_restored_after_restart(
    hass: HomeAssistant, hub: CombustionHub, config_entry: MockConfigEntry
) -> None:
    inject(hass, GAUGE_ADDRESS, GAUGE)
    await hass.async_block_till_done()
    await refresh(hass, hub)
    assert await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()
    # The entity exists right away, unavailable until the gauge is heard again.
    assert state(hass, "sensor.combustion_gauge_ab12cd34ef_temperature") == STATE_UNAVAILABLE


async def test_passive_only_option(hass: HomeAssistant, enable_bluetooth: None) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=DOMAIN, options={CONF_ACTIVE_CONNECTION: False}
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    hub = entry.runtime_data
    assert hub.connections is None
    inject(hass, GAUGE_ADDRESS, GAUGE)
    await hass.async_block_till_done()
    await refresh(hass, hub)
    prefix = "combustion_gauge_ab12cd34ef"
    assert state(hass, f"sensor.{prefix}_high_alarm_temperature") == "210.0"
    assert hass.states.get(f"number.{prefix}_high_alarm_temperature") is None
    assert hass.states.get(f"button.{prefix}_silence_alarms") is None


async def test_unset_gauge_alarm(hass: HomeAssistant, hub: CombustionHub) -> None:
    inject(hass, GAUGE_ADDRESS, bytes.fromhex(REAL_GAUGE))
    await hass.async_block_till_done()
    await refresh(hass, hub)
    prefix = "combustion_gauge_g000001711"
    # Never set: unknown rather than the -20 °C zero point...
    assert state(hass, f"number.{prefix}_high_alarm_temperature") == STATE_UNKNOWN
    assert state(hass, f"switch.{prefix}_high_alarm") == "off"
    # ...and it cannot be armed there.
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": f"switch.{prefix}_high_alarm"}, blocking=True
        )


async def test_command_without_connection_raises(hass: HomeAssistant, hub: CombustionHub) -> None:
    from homeassistant.exceptions import HomeAssistantError  # noqa: PLC0415

    inject(hass, GAUGE_ADDRESS, GAUGE)
    await hass.async_block_till_done()
    await refresh(hass, hub)
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "number",
            "set_value",
            {
                "entity_id": "number.combustion_gauge_ab12cd34ef_high_alarm_temperature",
                "value": 230,
            },
            blocking=True,
        )


async def test_garbage_advertisement_is_ignored(hass: HomeAssistant, hub: CombustionHub) -> None:
    with patch("custom_components.combustion_inc.hub._LOGGER") as logger:
        inject(hass, GAUGE_ADDRESS, b"\x03\xff")
        inject(hass, GAUGE_ADDRESS, b"\x09" * 30)
        await hass.async_block_till_done()
    assert not hub.devices
    assert not logger.exception.called


async def test_diagnostics(
    hass: HomeAssistant, hub: CombustionHub, config_entry: MockConfigEntry
) -> None:
    inject(hass, GAUGE_ADDRESS, GAUGE)
    await hass.async_block_till_done()
    diag = await async_get_config_entry_diagnostics(hass, config_entry)
    gauge = diag["devices"]["gauge_AB12CD34EF"]
    assert gauge["last_raw"] == GAUGE.hex()
    assert gauge["temperature"] == 150.0
    assert diag["node_addresses"] == {GAUGE_ADDRESS: "gauge_AB12CD34EF"}
