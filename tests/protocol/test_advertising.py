"""Advertisement decoding.

Vectors marked SDK come from the official combustion-android-ble tests
(src/test/.../ble/scanning/*AdvertisingDataTest.kt), with byte 0 set to the
product type the SDK passes alongside.
"""

import pytest

from custom_components.combustion_inc.protocol import (
    AlarmStatus,
    EngineAdvertisement,
    GaugeAdvertisement,
    NodeAdvertisement,
    ProbeAdvertisement,
    ProbeMode,
    ProductType,
    overheating_sensors,
    parse_advertisement,
)

HOT_TEMPS = "8900DA00D70D073305D518741A"
COLD_TEMPS = "00" * 13


def probe(product: int, temps: str = COLD_TEMPS, tail: str = "B9BB800004") -> bytes:
    return bytes.fromhex(f"{product:02X}78563412{temps}{tail}")


def test_probe_direct_full() -> None:
    adv = parse_advertisement(probe(1))
    assert isinstance(adv, ProbeAdvertisement)
    assert adv.product_type is ProductType.PROBE
    assert adv.serial == 0x12345678
    assert adv.serial_str == "12345678"
    assert adv.mode_id_color is not None
    assert adv.mode_id_color.mode is ProbeMode.INSTANT_READ
    assert adv.mode_id_color.color == 6  # COLOR7
    assert adv.mode_id_color.probe_id == 5  # ID6
    assert adv.battery_low is True
    assert adv.virtual_sensors is not None
    assert adv.virtual_sensors.core == 5  # T6
    assert adv.virtual_sensors.surface == 6  # T7
    assert adv.virtual_sensors.ambient == 6  # T7
    # A direct advertisement carries no route, whatever the network byte says.
    assert adv.hop_count is None
    assert adv.power_mode == 0
    assert adv.high_radio_power is True


def test_probe_repeated_hop_count_from_high_bits() -> None:
    adv = parse_advertisement(probe(2, tail="B9BB8000"))
    assert isinstance(adv, ProbeAdvertisement)
    assert adv.is_repeated
    assert adv.hop_count == 3  # 0x80 -> HOP3 in both SDKs
    assert adv.network_info_raw == 0x80
    # Repeated packets end at the overheat mask: no preferences byte.
    assert adv.power_mode is None
    assert adv.high_radio_power is None


@pytest.mark.parametrize(("network", "hops"), [(0x00, 1), (0x40, 2), (0x80, 3), (0xC0, 4)])
def test_hop_count_values(network: int, hops: int) -> None:
    adv = parse_advertisement(probe(2, tail=f"B9BB{network:02X}00"))
    assert isinstance(adv, ProbeAdvertisement)
    assert adv.hop_count == hops


def test_probe_temperatures_sdk_vector() -> None:
    adv = parse_advertisement(probe(1, HOT_TEMPS))
    assert isinstance(adv, ProbeAdvertisement)
    assert adv.temperatures == (-13.15, 67.2, 258.4, 160.55, 225.6, 114.5, 189.75, 22.3)


def test_probe_temperatures_each_slot_independent() -> None:
    # Distinct raw value per slot exercises every cross-byte boundary.
    raws = [1, 8191, 2, 4096, 3, 2048, 5, 1024]
    packed = sum(raw << (13 * i) for i, raw in enumerate(raws))
    data = bytes([1]) + bytes(4) + packed.to_bytes(13, "little") + bytes(5)
    adv = parse_advertisement(data)
    assert isinstance(adv, ProbeAdvertisement)
    assert adv.temperatures == tuple(round(r * 0.05 - 20, 2) for r in raws)


def test_overheating_from_temperatures_and_trusted_mask() -> None:
    # Hot temperatures overheat T3 and T4 whatever the mask says...
    hot = parse_advertisement(probe(1, HOT_TEMPS, "B9BB800004"))
    assert isinstance(hot, ProbeAdvertisement)
    assert overheating_sensors(hot.temperatures, hot.overheat_raw) == (2, 3)
    # ...the mask adds sensors only while something really is over a threshold...
    hot = parse_advertisement(probe(1, HOT_TEMPS, "B9BB808104"))
    assert isinstance(hot, ProbeAdvertisement)
    assert hot.overheat_raw == 0x81
    assert overheating_sensors(hot.temperatures, hot.overheat_raw) == (0, 2, 3, 7)
    # ...and a mask of 0xFF on cold temperatures means nothing (repeater firmware bug).
    cold = parse_advertisement(probe(1, COLD_TEMPS, "B9BB80FF04"))
    assert isinstance(cold, ProbeAdvertisement)
    assert overheating_sensors(cold.temperatures, cold.overheat_raw) == ()


def test_probe_short_packets_leave_fields_unknown() -> None:
    adv = parse_advertisement(probe(1, tail=""))
    assert isinstance(adv, ProbeAdvertisement)
    assert adv.mode_id_color is None
    assert adv.battery_low is None
    assert adv.virtual_sensors is None
    assert adv.power_mode is None
    adv = parse_advertisement(probe(1, tail="B9"))
    assert isinstance(adv, ProbeAdvertisement)
    assert adv.mode_id_color is not None
    assert adv.battery_low is None


def test_probe_undefined_core_selector_is_unknown() -> None:
    # core code 6 is not defined by the spec (0-5 = T1-T6)
    adv = parse_advertisement(probe(1, tail="000C"))
    assert isinstance(adv, ProbeAdvertisement)
    assert adv.virtual_sensors is not None
    assert adv.virtual_sensors.core is None


def test_probe_too_short() -> None:
    assert parse_advertisement(bytes.fromhex("0178563412")) is None


GAUGE = "0341423132434433344546"  # type 3 + "AB12CD34EF"


def test_gauge_sdk_default() -> None:
    adv = parse_advertisement(bytes.fromhex(GAUGE + "A40601000000000001"))
    assert isinstance(adv, GaugeAdvertisement)
    assert adv.serial == "AB12CD34EF"
    assert adv.temperature == 150.0
    assert adv.sensor_present is True
    assert adv.overheating is False
    assert adv.low_battery is False
    assert adv.high_radio_power is True
    assert adv.gauge_id is None
    assert adv.high_alarm.set is False


def test_gauge_flags() -> None:
    adv = parse_advertisement(bytes.fromhex(GAUGE + "A40606000000000001"))
    assert isinstance(adv, GaugeAdvertisement)
    assert adv.sensor_present is False
    assert adv.temperature is None
    assert adv.overheating is True
    assert adv.low_battery is True


def test_gauge_alarms_sdk_vector() -> None:
    adv = parse_advertisement(bytes.fromhex(GAUGE + "A4060100E347C11201"))
    assert isinstance(adv, GaugeAdvertisement)
    assert adv.high_alarm.set and adv.high_alarm.tripped and not adv.high_alarm.alarming
    assert adv.high_alarm.temperature == 210.0
    assert adv.low_alarm.set and not adv.low_alarm.tripped
    assert adv.low_alarm.temperature == 40.0


def test_alarm_configured() -> None:
    assert not AlarmStatus.decode(0).configured  # as shipped: off, at -20 °C
    assert AlarmStatus.decode(1).configured  # on at -20 °C was chosen
    assert AlarmStatus.decode(1200 << 3).configured  # off, but keeps 100 °C


def test_gauge_reserved_temperature_bits_are_masked() -> None:
    # 0x24B0: low 13 bits = 1200 -> 100.0 °C; the top three bits are padding.
    adv = parse_advertisement(bytes.fromhex(GAUGE + "B02401000000000001"))
    assert isinstance(adv, GaugeAdvertisement)
    assert adv.temperature == 100.0


def test_gauge_id_and_short_variants() -> None:
    adv = parse_advertisement(bytes.fromhex(GAUGE + "A4060100000000000104"))
    assert isinstance(adv, GaugeAdvertisement)
    assert adv.gauge_id == 4
    adv = parse_advertisement(bytes.fromhex(GAUGE + "A406010000000000"))
    assert isinstance(adv, GaugeAdvertisement)
    assert adv.high_radio_power is None


def test_gauge_full_spec_layout() -> None:
    # 22 bytes: ..., battery 100, alarms, prefs, id 0, reserved
    adv = parse_advertisement(bytes.fromhex(GAUGE + "A40601640000000001" + "0000"))
    assert isinstance(adv, GaugeAdvertisement)
    assert adv.battery_percentage == 100
    assert adv.gauge_id == 0


def test_gauge_nul_padded_serial() -> None:
    data = bytes([3]) + b"GGG198A54\x00" + bytes.fromhex("A40601000000000001")
    adv = parse_advertisement(data)
    assert isinstance(adv, GaugeAdvertisement)
    assert adv.serial == "GGG198A54"


def test_engine_sdk_vectors() -> None:
    adv = parse_advertisement(bytes.fromhex("0641423132434433344546A4060301"))
    assert isinstance(adv, EngineAdvertisement)
    assert adv.serial == "AB12CD34EF"
    assert adv.set_point == 150.0
    assert adv.app_mode and adv.control_device_connected
    assert not adv.lid_open and not adv.fixed_speed
    assert adv.high_radio_power is True
    adv = parse_advertisement(bytes.fromhex("064142313243443334454698080401"))
    assert isinstance(adv, EngineAdvertisement)
    assert adv.set_point == 200.0
    assert adv.lid_open and not adv.app_mode
    adv = parse_advertisement(bytes.fromhex("0641423132434433344546A4060300" + "00" * 8))
    assert isinstance(adv, EngineAdvertisement)
    assert adv.high_radio_power is False


@pytest.mark.parametrize("product", [ProductType.DISPLAY, ProductType.BOOSTER])
def test_display_and_booster(product: ProductType) -> None:
    data = bytes([product]) + b"DSP1234567" + bytes([1]) + bytes(11)
    adv = parse_advertisement(data)
    assert isinstance(adv, NodeAdvertisement)
    assert adv.product_type is product
    assert adv.serial == "DSP1234567"
    assert adv.high_radio_power is True


@pytest.mark.parametrize("data", [b"", b"\x00" * 23, b"\x07" * 23, b"\x03" + bytes(21)])
def test_unknown_or_blank(data: bytes) -> None:
    assert parse_advertisement(data) is None


# Captured 2026-10-29 from a real Giant Grill Gauge (firmware as shipped), probe
# plugged in, room temperature. Both packets are 23 bytes: one byte longer than
# the field tables in the spec.
REAL_GAUGE = "0347303030303031373131cb0101640000000001000000"
REAL_GAUGE_NO_PROBES = "0200000000000000000000000000000000000000000000"


def test_real_gauge_capture() -> None:
    adv = parse_advertisement(bytes.fromhex(REAL_GAUGE))
    assert isinstance(adv, GaugeAdvertisement)
    assert adv.serial == "G000001711"
    assert adv.temperature == 25.9
    assert adv.sensor_present and not adv.overheating and not adv.low_battery
    assert adv.battery_percentage == 100
    assert not adv.high_alarm.set and not adv.low_alarm.set
    assert adv.high_radio_power is True
    assert adv.gauge_id == 0


def test_real_gauge_no_probes_packet() -> None:
    # The gauge as a MeatNet node, repeating "no probes" (serial 0).
    adv = parse_advertisement(bytes.fromhex(REAL_GAUGE_NO_PROBES))
    assert isinstance(adv, ProbeAdvertisement)
    assert adv.is_repeated
    assert adv.serial == 0
