"""Decoders for status messages that need a GATT connection.

- Probe Status: the probe's 0x101 characteristic, or MeatNet UART message 0x45
- Gauge Status: MeatNet UART message 0x60
- Engine Status: MeatNet UART message 0x70
"""

from dataclasses import dataclass

from .const import (
    BatteryLevel,
    ChargingState,
    EngineControllerState,
    EngineFanState,
    FoodSafeMode,
    FoodSafeState,
    PredictionMode,
    PredictionState,
    PredictionType,
    ProductType,
)
from .fields import (
    AlarmStatus,
    ModeIdColor,
    VirtualSensors,
    bits,
    decode_ascii_serial,
    decode_battery_and_virtual_sensors,
    decode_hop_count,
    decode_probe_temperatures,
    decode_temperature_point,
    format_probe_serial,
    uint_le,
)


def _enum[E](enum: type[E], value: int) -> E | None:
    try:
        return enum(value)  # type: ignore[call-arg]
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class PredictionStatus:
    state: PredictionState | None
    mode: PredictionMode
    type: PredictionType
    set_point: float  # °C
    heat_start: float  # °C
    seconds: int  # time until removal or resting completes, per `type`
    estimated_core: float  # °C

    @classmethod
    def decode(cls, data: bytes) -> PredictionStatus:
        v = uint_le(data, 0, 7)
        return cls(
            state=_enum(PredictionState, bits(v, 0, 4)),
            mode=PredictionMode(bits(v, 4, 2)),
            type=PredictionType(bits(v, 6, 2)),
            set_point=round(bits(v, 8, 10) * 0.1, 1),
            heat_start=round(bits(v, 18, 10) * 0.1, 1),
            seconds=bits(v, 28, 17),
            estimated_core=round(bits(v, 45, 11) * 0.1 - 20.0, 1),
        )

    @property
    def percent_to_removal(self) -> float | None:
        span = self.set_point - self.heat_start
        if self.mode is PredictionMode.NONE or span <= 0:
            return None
        done = (self.estimated_core - self.heat_start) / span * 100
        return round(max(0.0, min(100.0, done)), 1)


@dataclass(frozen=True, slots=True)
class FoodSafeConfig:
    mode: FoodSafeMode | None
    product: int
    serving: int

    @classmethod
    def decode(cls, data: bytes) -> FoodSafeConfig:
        v = uint_le(data, 0, 10)
        return cls(_enum(FoodSafeMode, bits(v, 0, 3)), bits(v, 3, 10), bits(v, 13, 3))


@dataclass(frozen=True, slots=True)
class FoodSafeStatus:
    state: FoodSafeState | None
    log_reduction: float
    seconds_above_threshold: int
    sequence: int

    @classmethod
    def decode(cls, data: bytes) -> FoodSafeStatus:
        v = uint_le(data, 0, 8)
        return cls(
            state=_enum(FoodSafeState, bits(v, 0, 3)),
            log_reduction=round(bits(v, 3, 8) * 0.1, 1),
            seconds_above_threshold=bits(v, 11, 16),
            sequence=bits(v, 27, 32),
        )


@dataclass(frozen=True, slots=True)
class ProbeStatus:
    serial: int | None  # set for MeatNet 0x45, None for the direct characteristic
    min_sequence: int
    max_sequence: int
    temperatures: tuple[float, ...]
    mode_id_color: ModeIdColor
    battery_low: bool
    virtual_sensors: VirtualSensors
    prediction: PredictionStatus
    food_safe_config: FoodSafeConfig | None
    food_safe_status: FoodSafeStatus | None
    overheat_raw: int | None
    power_mode: int | None
    high_radio_power: bool | None
    high_alarms: tuple[AlarmStatus, ...] | None  # T1-T8, core, surface, ambient
    low_alarms: tuple[AlarmStatus, ...] | None
    session_id: int | None  # 0 on the wire means "not supplied"
    sample_period_ms: int | None
    hop_count: int | None  # None for a direct connection

    @property
    def serial_str(self) -> str | None:
        return format_probe_serial(self.serial) if self.serial is not None else None

    @classmethod
    def decode_characteristic(cls, data: bytes) -> ProbeStatus | None:
        """Decode a direct Probe Status (0x101) notification."""
        return cls._decode(data, serial=None, hop_count=None)

    @classmethod
    def decode_node_payload(cls, payload: bytes) -> ProbeStatus | None:
        """Decode the payload of a MeatNet Probe Status (0x45) message.

        It is the 0x101 layout behind a 4-byte probe serial, with the network
        information byte inserted after Food Safe Status. Very old nodes send a
        35-byte payload: serial, the first 30 status bytes, then network info.
        """
        if len(payload) == 35:
            block, network = payload[4:34], payload[34]
        elif len(payload) >= 4 + 49:
            block, network = payload[4:52] + payload[53:], payload[52]
        else:
            return None
        return cls._decode(
            block, serial=uint_le(payload, 0, 4), hop_count=decode_hop_count(network)
        )

    @classmethod
    def _decode(cls, d: bytes, serial: int | None, hop_count: int | None) -> ProbeStatus | None:
        if len(d) < 30:
            return None
        battery_low, virtual = decode_battery_and_virtual_sensors(d[22])
        has_food_safe = len(d) >= 48
        prefs = d[49] if len(d) > 49 else None
        has_alarms = len(d) >= 94
        session_id = uint_le(d, 94, 4) if len(d) >= 100 else 0
        period = uint_le(d, 98, 2) if len(d) >= 100 else 0
        return cls(
            serial=serial,
            min_sequence=uint_le(d, 0, 4),
            max_sequence=uint_le(d, 4, 4),
            temperatures=decode_probe_temperatures(d[8:21]),
            mode_id_color=ModeIdColor.decode(d[21]),
            battery_low=battery_low,
            virtual_sensors=virtual,
            prediction=PredictionStatus.decode(d[23:30]),
            food_safe_config=FoodSafeConfig.decode(d[30:40]) if has_food_safe else None,
            food_safe_status=FoodSafeStatus.decode(d[40:48]) if has_food_safe else None,
            overheat_raw=d[48] if len(d) > 48 else None,
            power_mode=prefs & 0x03 if prefs is not None else None,
            high_radio_power=bool(prefs & 0x04) if prefs is not None else None,
            high_alarms=_alarm_array(d, 50) if has_alarms else None,
            low_alarms=_alarm_array(d, 72) if has_alarms else None,
            session_id=session_id or None,
            sample_period_ms=period or None,
            hop_count=hop_count,
        )


def _alarm_array(data: bytes, start: int) -> tuple[AlarmStatus, ...]:
    return tuple(AlarmStatus.decode_bytes(data, start + 2 * i) for i in range(11))


def encode_alarm_arrays(high: tuple[AlarmStatus, ...], low: tuple[AlarmStatus, ...]) -> bytes:
    """The 44-byte payload of Set Probe High Low Alarm: all highs, then all lows."""
    if len(high) != 11 or len(low) != 11:
        raise ValueError("a probe has exactly 11 high and 11 low alarms")
    return b"".join(alarm.encode() for alarm in (*high, *low))


@dataclass(frozen=True, slots=True)
class GaugeStatus:
    serial: str
    session_id: int | None
    sample_period_ms: int
    temperature: float | None
    sensor_present: bool
    overheating: bool
    low_battery: bool
    min_sequence: int
    max_sequence: int
    high_alarm: AlarmStatus
    low_alarm: AlarmStatus
    new_record: bool
    hop_count: int
    gauge_id: int | None

    @classmethod
    def decode_node_payload(cls, payload: bytes) -> GaugeStatus | None:
        """Decode the payload of a MeatNet Gauge Status (0x60) message."""
        if len(payload) < 34 or (serial := decode_ascii_serial(payload[0:10])) is None:
            return None
        flags = payload[18]
        present = bool(flags & 1)
        return cls(
            serial=serial,
            session_id=uint_le(payload, 10, 4) or None,
            sample_period_ms=uint_le(payload, 14, 2),
            temperature=decode_temperature_point(uint_le(payload, 16, 2)) if present else None,
            sensor_present=present,
            overheating=bool(flags & 2),
            low_battery=bool(flags & 4),
            min_sequence=uint_le(payload, 19, 4),
            max_sequence=uint_le(payload, 23, 4),
            high_alarm=AlarmStatus.decode_bytes(payload, 28),
            low_alarm=AlarmStatus.decode_bytes(payload, 30),
            new_record=payload[32] == 1,
            hop_count=decode_hop_count(payload[33]),
            gauge_id=payload[34] if len(payload) > 34 else None,
        )


@dataclass(frozen=True, slots=True)
class EngineFanStatus:
    state: EngineFanState | None
    duty_cycle: int
    commanded_speed: int
    measured_speed: int
    off_time_ms: int
    on_time_ms: int


@dataclass(frozen=True, slots=True)
class EngineControllerStatus:
    state: EngineControllerState | None
    response_coefficient: float
    cycles_completed: int
    reached_set_point: bool
    maintenance_mode: bool
    smoothed_temperature: float  # °C, no offset
    time_to_peak_s: int
    drift_rate: float  # °C/s


@dataclass(frozen=True, slots=True)
class EngineStatus:
    serial: str
    session_id: int | None
    sample_period_ms: int
    min_sequence: int
    max_sequence: int
    battery_level: BatteryLevel | None
    charging_state: ChargingState | None
    battery_voltage: float
    set_point: float
    control_temperature: float | None
    control_device_type: ProductType | None
    control_device_serial: str | None
    app_mode: bool
    control_device_connected: bool
    lid_open: bool
    fixed_speed: bool
    fan: EngineFanStatus
    controller: EngineControllerStatus
    hop_count: int
    knob_voltage: float  # V
    knob_angle: float  # degrees clockwise from Off
    charging_fault: int  # 0 = none, see the Engine spec for the codes

    @classmethod
    def decode_node_payload(cls, payload: bytes) -> EngineStatus | None:
        """Decode the payload of a MeatNet Engine Status (0x70) message."""
        if len(payload) < 71 or (serial := decode_ascii_serial(payload[0:10])) is None:
            return None
        d = payload[10:]
        flags = d[34]
        control_type = _enum(ProductType, d[21]) if d[21] else None
        control_serial: str | None = None
        if control_type is ProductType.PROBE:
            if probe_serial := uint_le(d, 22, 4):
                control_serial = format_probe_serial(probe_serial)
        elif control_type is not None:
            control_serial = decode_ascii_serial(d[22:32])
        drift = d[54] - 256 if d[54] > 127 else d[54]
        return cls(
            serial=serial,
            session_id=uint_le(d, 0, 4) or None,
            sample_period_ms=uint_le(d, 4, 2),
            min_sequence=uint_le(d, 6, 4),
            max_sequence=uint_le(d, 10, 4),
            battery_level=_enum(BatteryLevel, d[14]),
            charging_state=_enum(ChargingState, d[15]),
            battery_voltage=round(d[16] / 10, 1),
            set_point=decode_temperature_point(uint_le(d, 17, 2)),
            control_temperature=(
                decode_temperature_point(uint_le(d, 19, 2)) if control_serial else None
            ),
            control_device_type=control_type if control_serial else None,
            control_device_serial=control_serial,
            app_mode=bool(flags & 1),
            control_device_connected=bool(flags & 2),
            lid_open=bool(flags & 4),
            fixed_speed=bool(flags & 8),
            fan=EngineFanStatus(
                state=_enum(EngineFanState, d[35]),
                duty_cycle=d[36],
                commanded_speed=d[37],
                measured_speed=d[38],
                off_time_ms=uint_le(d, 39, 4),
                on_time_ms=uint_le(d, 43, 4),
            ),
            controller=EngineControllerStatus(
                state=_enum(EngineControllerState, d[47]),
                response_coefficient=round(d[48] / 500, 3),
                cycles_completed=d[49],
                reached_set_point=bool(d[50] & 1),
                maintenance_mode=bool(d[50] & 2),
                smoothed_temperature=round(uint_le(d, 51, 2) / 10, 1),
                time_to_peak_s=d[53],
                drift_rate=round(drift / 1000, 3),
            ),
            hop_count=decode_hop_count(d[55]),
            knob_voltage=round(uint_le(d, 56, 2) / 1000, 3),
            knob_angle=round(uint_le(d, 58, 2) / 10, 1),
            charging_fault=d[60] >> 1,
        )
