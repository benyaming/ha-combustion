"""Field codecs shared by advertisements and GATT status messages.

Every packed field in the Combustion protocol is a little-endian integer whose
"bit 1" is the least significant bit. Two exceptions, both following the official
SDKs rather than the spec text: the overheating mask table is printed MSB first,
and the hop count sits in the top two bits of the network information byte.
"""

from dataclasses import dataclass

from .const import OVERHEAT_THRESHOLDS_C, ProbeMode

THIRTEEN_BITS = 0x1FFF


def bits(value: int, start: int, width: int) -> int:
    """Return `width` bits of `value`, starting at bit `start` (0 = LSB)."""
    return (value >> start) & ((1 << width) - 1)


def uint_le(data: bytes, start: int, length: int) -> int:
    return int.from_bytes(data[start : start + length], "little")


def decode_ascii_serial(data: bytes) -> str | None:
    """Decode a 10-byte alphanumeric node serial; None when it is blank."""
    text = data.split(b"\x00", 1)[0].decode("ascii", errors="replace").strip()
    return text.upper() or None


def encode_ascii_serial(serial: str) -> bytes:
    return serial.encode("ascii")[:10].ljust(10, b"\x00")


def format_probe_serial(serial: int) -> str:
    return f"{serial:08X}"


def decode_probe_temperatures(data: bytes) -> tuple[float, ...]:
    """Decode 13 bytes holding eight packed 13-bit thermistor readings, T1 first."""
    packed = int.from_bytes(data[:13], "little")
    return tuple(round(bits(packed, 13 * i, 13) * 0.05 - 20.0, 2) for i in range(8))


def decode_temperature_point(word: int) -> float:
    """13-bit value in 0.1 °C steps, offset by -20 °C (Gauge and Engine)."""
    return round((word & THIRTEEN_BITS) * 0.1 - 20.0, 1)


def encode_temperature_point(celsius: float) -> int:
    return max(0, min(THIRTEEN_BITS, round((celsius + 20.0) * 10)))


def decode_hop_count(network_info: int) -> int:
    """Hops between the probe and the node that sent this data (1-4)."""
    return bits(network_info, 6, 2) + 1


@dataclass(frozen=True, slots=True)
class ModeIdColor:
    mode: ProbeMode
    color: int  # 0 yellow, 1 grey, 2-7 not named by the spec
    probe_id: int  # 0-7, shown to people as ID 1-8

    @classmethod
    def decode(cls, value: int) -> ModeIdColor:
        return cls(ProbeMode(bits(value, 0, 2)), bits(value, 2, 3), bits(value, 5, 3))


@dataclass(frozen=True, slots=True)
class VirtualSensors:
    """Which thermistors the probe picked as core, surface and ambient (0 = T1)."""

    core: int | None
    surface: int
    ambient: int

    @classmethod
    def decode(cls, value: int) -> VirtualSensors:
        core = bits(value, 0, 3)
        return cls(
            core=core if core <= 5 else None,
            surface=bits(value, 3, 2) + 3,
            ambient=bits(value, 5, 2) + 4,
        )


def decode_battery_and_virtual_sensors(value: int) -> tuple[bool, VirtualSensors]:
    """Return (battery_low, virtual sensors) from the packed status byte."""
    return bool(value & 1), VirtualSensors.decode(value >> 1)


def overheating_sensors(
    temperatures: tuple[float, ...], reported_mask: int | None = None
) -> tuple[int, ...]:
    """Indices (0 = T1) of overheating sensors.

    A sensor at or above its threshold always counts. The probe's own mask (bit 0 =
    T1) adds to that only while some temperature really is over a threshold, because
    repeater firmware <= 2.2.0 sets it wrongly. This union is deliberately more
    cautious than either official SDK path: their advertisement parser uses only the
    thresholds, their status parser only the mask (and only when something is hot).
    """
    hot = {
        i
        for i, (temp, limit) in enumerate(zip(temperatures, OVERHEAT_THRESHOLDS_C, strict=True))
        if temp >= limit
    }
    if hot and reported_mask:
        hot |= {i for i in range(8) if reported_mask >> i & 1}
    return tuple(sorted(hot))


@dataclass(frozen=True, slots=True)
class AlarmStatus:
    """One high or low alarm: configuration plus its live state."""

    set: bool = False
    tripped: bool = False
    alarming: bool = False
    temperature: float = -20.0

    @classmethod
    def decode(cls, word: int) -> AlarmStatus:
        return cls(
            set=bool(word & 1),
            tripped=bool(word & 2),
            alarming=bool(word & 4),
            temperature=round(bits(word, 3, 13) * 0.1 - 20.0, 1),
        )

    @property
    def configured(self) -> bool:
        """False for an alarm never given a temperature: off, at the zero point (-20 °C)."""
        return self.set or self.temperature != -20.0

    @classmethod
    def decode_bytes(cls, data: bytes, start: int = 0) -> AlarmStatus:
        return cls.decode(uint_le(data, start, 2))

    def encode(self) -> bytes:
        # The device ignores the tripped bit on writes. The flags are passed through
        # as the official SDK does, so callers should start from the reported status.
        word = (
            (encode_temperature_point(self.temperature) << 3)
            | int(self.set)
            | int(self.tripped) << 1
            | int(self.alarming) << 2
        )
        return word.to_bytes(2, "little")
