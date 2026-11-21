"""Framing for the Nordic UART service: direct probe and MeatNet node variants.

Direct probe   request:  CA FE crc16 type len payload
               response: CA FE crc16 type success len payload
MeatNet node   request:  CA FE crc16 type req_id(4) len payload
               response: CA FE crc16 type|0x80 req_id(4) resp_id(4) success len payload

The CRC is CRC-16/CCITT-FALSE over everything after it, stored little-endian.
"""

from dataclasses import dataclass, field
from enum import Enum

from .const import MessageType, PredictionMode, ProductType
from .fields import AlarmStatus, encode_ascii_serial, encode_temperature_point
from .status import encode_alarm_arrays

SYNC = b"\xca\xfe"
RESPONSE_FLAG = 0x80
DISPLAY_STATUS = 0x80
NODE_REQUEST_HEADER = 10
NODE_RESPONSE_HEADER = 15
PROBE_REQUEST_HEADER = 6
PROBE_RESPONSE_HEADER = 7
# Longest frame the buffer waits for before giving up on a sync and rescanning.
MAX_BUFFER = 1024


def crc16_ccitt(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else crc << 1
            crc &= 0xFFFF
    return crc


class Framing(Enum):
    PROBE = "probe"
    NODE = "node"


@dataclass(frozen=True, slots=True)
class Frame:
    message_type: int  # without the response flag
    is_response: bool
    payload: bytes
    request_id: int | None = None  # MeatNet only
    response_id: int | None = None
    success: bool | None = None  # responses only


def _seal(body: bytes) -> bytes:
    return SYNC + crc16_ccitt(body).to_bytes(2, "little") + body


def encode_probe_request(message_type: int, payload: bytes = b"") -> bytes:
    return _seal(bytes((message_type, len(payload))) + payload)


def encode_node_request(message_type: int, request_id: int, payload: bytes = b"") -> bytes:
    return _seal(
        bytes((message_type,)) + request_id.to_bytes(4, "little") + bytes((len(payload),)) + payload
    )


def encode_probe_response(message_type: int, success: bool, payload: bytes = b"") -> bytes:
    return _seal(bytes((message_type, int(success), len(payload))) + payload)


def encode_node_response(
    message_type: int, request_id: int, response_id: int, success: bool, payload: bytes = b""
) -> bytes:
    return _seal(
        bytes((message_type | RESPONSE_FLAG,))
        + request_id.to_bytes(4, "little")
        + response_id.to_bytes(4, "little")
        + bytes((int(success), len(payload)))
        + payload
    )


def _candidate(buf: bytes, framing: Framing, as_response: bool) -> tuple[int, Frame | None] | None:
    """Try to read one frame at the start of buf.

    Returns None if more bytes are needed, (total, frame) if the CRC matches, and
    (total, None) if this interpretation is complete but invalid.
    """
    if framing is Framing.PROBE:
        header = PROBE_RESPONSE_HEADER if as_response else PROBE_REQUEST_HEADER
    else:
        header = NODE_RESPONSE_HEADER if as_response else NODE_REQUEST_HEADER
        # Rule out an impossible reading before waiting on its length, or a corrupt
        # frame could hold back complete frames behind it. Requests have bit 7 clear,
        # except Display Status (0x80), the one request the spec numbers that way.
        if len(buf) > 4:
            flagged = bool(buf[4] & RESPONSE_FLAG)
            request_ok = not flagged or buf[4] == DISPLAY_STATUS
            if not (flagged if as_response else request_ok):
                return 0, None
    if len(buf) < header:
        return None
    total = header + buf[header - 1]
    if len(buf) < total:
        return None
    if int.from_bytes(buf[2:4], "little") != crc16_ccitt(buf[4:total]):
        return total, None
    payload = bytes(buf[header:total])
    type_byte = buf[4]
    if framing is Framing.PROBE:
        frame = Frame(
            type_byte, as_response, payload, success=bool(buf[5]) if as_response else None
        )
    elif as_response:
        frame = Frame(
            type_byte & ~RESPONSE_FLAG,
            True,
            payload,
            request_id=int.from_bytes(buf[5:9], "little"),
            response_id=int.from_bytes(buf[9:13], "little"),
            success=bool(buf[13]),
        )
    else:
        frame = Frame(type_byte, False, payload, request_id=int.from_bytes(buf[5:9], "little"))
    return total, frame


@dataclass
class FrameDecoder:
    """Reassemble frames from notifications that may split or pack them."""

    framing: Framing
    _buffer: bytearray = field(default_factory=bytearray)

    def feed(self, data: bytes) -> list[Frame]:
        self._buffer += data
        frames: list[Frame] = []
        while True:
            start = self._buffer.find(SYNC)
            if start < 0:
                # Keep a trailing 0xCA, it may be the first half of the next sync.
                del self._buffer[: max(0, len(self._buffer) - 1)]
                return frames
            del self._buffer[:start]
            # A probe only ever sends responses; a node sends both.
            kinds = (True,) if self.framing is Framing.PROBE else (True, False)
            results = [_candidate(self._buffer, self.framing, kind) for kind in kinds]
            if found := next((r for r in results if r is not None and r[1] is not None), None):
                total, frame = found
                frames.append(frame)  # type: ignore[arg-type]
                del self._buffer[:total]
                continue
            if any(r is None for r in results) and len(self._buffer) < MAX_BUFFER:
                return frames  # wait for the rest of the frame
            del self._buffer[:2]  # not a frame after all; rescan past this sync


# --- Request payloads -------------------------------------------------------


def set_prediction_payload(set_point: float, mode: PredictionMode) -> bytes:
    raw = max(0, min(0x3FF, round(set_point * 10)))
    return (raw | (int(mode) & 0x03) << 10).to_bytes(2, "little")


def node_set_prediction_payload(probe_serial: int, set_point: float, mode: PredictionMode) -> bytes:
    return probe_serial.to_bytes(4, "little") + set_prediction_payload(set_point, mode)


def node_silence_all_alarms_payload() -> bytes:
    # global = 1, product type unknown, no serial: what the official app sends.
    return bytes((1, ProductType.UNKNOWN)) + bytes(14)


def node_set_probe_alarms_payload(
    probe_serial: int, high: tuple[AlarmStatus, ...], low: tuple[AlarmStatus, ...]
) -> bytes:
    return probe_serial.to_bytes(4, "little") + encode_alarm_arrays(high, low)


def node_set_gauge_alarms_payload(serial: str, high: AlarmStatus, low: AlarmStatus) -> bytes:
    return encode_ascii_serial(serial) + high.encode() + low.encode()


def node_set_engine_set_point_payload(serial: str, celsius: float) -> bytes:
    return encode_ascii_serial(serial) + encode_temperature_point(celsius).to_bytes(2, "little")


__all__ = [
    "Frame",
    "FrameDecoder",
    "Framing",
    "MessageType",
    "crc16_ccitt",
    "encode_node_request",
    "encode_node_response",
    "encode_probe_request",
    "encode_probe_response",
]
