"""Decoders for Combustion manufacturer data in BLE advertisements.

`data` is the manufacturer data value for company 0x09C7 as Home Assistant
reports it: the two vendor ID bytes are already stripped, so data[0] is the
product type. Fields a shorter (older firmware) packet does not carry are None.
"""

from dataclasses import dataclass

from .const import ProductType
from .fields import (
    AlarmStatus,
    ModeIdColor,
    VirtualSensors,
    decode_ascii_serial,
    decode_battery_and_virtual_sensors,
    decode_hop_count,
    decode_probe_temperatures,
    decode_temperature_point,
    format_probe_serial,
    uint_le,
)


@dataclass(frozen=True, slots=True)
class ProbeAdvertisement:
    """Probe data, sent by the probe itself or repeated by a MeatNet node."""

    product_type: ProductType  # PROBE (direct) or REPEATER
    serial: int
    temperatures: tuple[float, ...]
    mode_id_color: ModeIdColor | None
    battery_low: bool | None
    virtual_sensors: VirtualSensors | None
    hop_count: int | None  # None for a direct advertisement
    network_info_raw: int | None
    overheat_raw: int | None
    power_mode: int | None
    high_radio_power: bool | None

    @property
    def serial_str(self) -> str:
        return format_probe_serial(self.serial)

    @property
    def is_repeated(self) -> bool:
        return self.product_type is ProductType.REPEATER


@dataclass(frozen=True, slots=True)
class GaugeAdvertisement:
    serial: str
    temperature: float | None  # None while no sensor is plugged in
    sensor_present: bool
    overheating: bool
    low_battery: bool
    battery_percentage: int  # firmware always reports 100; use low_battery
    high_alarm: AlarmStatus
    low_alarm: AlarmStatus
    high_radio_power: bool | None
    gauge_id: int | None  # 0-255, shown to people as ID 1-256


@dataclass(frozen=True, slots=True)
class EngineAdvertisement:
    serial: str
    set_point: float
    app_mode: bool
    control_device_connected: bool
    lid_open: bool
    fixed_speed: bool
    high_radio_power: bool | None


@dataclass(frozen=True, slots=True)
class NodeAdvertisement:
    """A Display (Timer) or Booster advertising its own identity."""

    product_type: ProductType
    serial: str
    high_radio_power: bool | None


type Advertisement = (
    ProbeAdvertisement | GaugeAdvertisement | EngineAdvertisement | NodeAdvertisement
)


def _optional(data: bytes, index: int) -> int | None:
    return data[index] if len(data) > index else None


def parse_probe(data: bytes) -> ProbeAdvertisement | None:
    if len(data) < 18:
        return None
    product_type = ProductType(data[0])
    mode_byte = _optional(data, 18)
    status_byte = _optional(data, 19)
    network = _optional(data, 20)
    battery_low = virtual = None
    if status_byte is not None:
        battery_low, virtual = decode_battery_and_virtual_sensors(status_byte)
    # Only direct advertisements carry preferences; a node's packet ends at the mask.
    prefs = _optional(data, 22) if product_type is ProductType.PROBE else None
    repeated = product_type is ProductType.REPEATER
    return ProbeAdvertisement(
        product_type=product_type,
        serial=uint_le(data, 1, 4),
        temperatures=decode_probe_temperatures(data[5:18]),
        mode_id_color=ModeIdColor.decode(mode_byte) if mode_byte is not None else None,
        battery_low=battery_low,
        virtual_sensors=virtual,
        hop_count=decode_hop_count(network) if repeated and network is not None else None,
        network_info_raw=network if repeated else None,
        overheat_raw=_optional(data, 21),
        power_mode=prefs & 0x03 if prefs is not None else None,
        high_radio_power=bool(prefs & 0x04) if prefs is not None else None,
    )


def parse_gauge(data: bytes) -> GaugeAdvertisement | None:
    if len(data) < 19 or (serial := decode_ascii_serial(data[1:11])) is None:
        return None
    flags = data[13]
    sensor_present = bool(flags & 1)
    prefs = _optional(data, 19)
    return GaugeAdvertisement(
        serial=serial,
        temperature=decode_temperature_point(uint_le(data, 11, 2)) if sensor_present else None,
        sensor_present=sensor_present,
        overheating=bool(flags & 2),
        low_battery=bool(flags & 4),
        battery_percentage=data[14],
        high_alarm=AlarmStatus.decode_bytes(data, 15),
        low_alarm=AlarmStatus.decode_bytes(data, 17),
        high_radio_power=bool(prefs & 1) if prefs is not None else None,
        gauge_id=_optional(data, 20),
    )


def parse_engine(data: bytes) -> EngineAdvertisement | None:
    if len(data) < 14 or (serial := decode_ascii_serial(data[1:11])) is None:
        return None
    flags = data[13]
    prefs = _optional(data, 14)
    return EngineAdvertisement(
        serial=serial,
        set_point=decode_temperature_point(uint_le(data, 11, 2)),
        app_mode=bool(flags & 1),
        control_device_connected=bool(flags & 2),
        lid_open=bool(flags & 4),
        fixed_speed=bool(flags & 8),
        high_radio_power=bool(prefs & 1) if prefs is not None else None,
    )


def parse_node(data: bytes) -> NodeAdvertisement | None:
    if len(data) < 11 or (serial := decode_ascii_serial(data[1:11])) is None:
        return None
    prefs = _optional(data, 11)
    return NodeAdvertisement(
        product_type=ProductType(data[0]),
        serial=serial,
        high_radio_power=bool(prefs & 1) if prefs is not None else None,
    )


_PARSERS = {
    ProductType.PROBE: parse_probe,
    ProductType.REPEATER: parse_probe,
    ProductType.GAUGE: parse_gauge,
    ProductType.DISPLAY: parse_node,
    ProductType.BOOSTER: parse_node,
    ProductType.ENGINE: parse_engine,
}


def parse_advertisement(data: bytes) -> Advertisement | None:
    """Decode Combustion manufacturer data; None if it is unknown or too short."""
    if not data:
        return None
    try:
        parser = _PARSERS[ProductType(data[0])]
    except ValueError, KeyError:
        return None
    return parser(data)
