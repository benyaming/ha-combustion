"""UART framing and request encoding.

Frames marked SDK were rendered from the official SDK's request builders and
checked against the CRC of the firmware's golden 0x45 frame.
"""

import pytest

from custom_components.combustion_inc.protocol.const import MessageType, PredictionMode
from custom_components.combustion_inc.protocol.fields import AlarmStatus
from custom_components.combustion_inc.protocol.status import ProbeStatus
from custom_components.combustion_inc.protocol.uart import (
    FrameDecoder,
    Framing,
    crc16_ccitt,
    encode_node_request,
    encode_node_response,
    encode_probe_request,
    encode_probe_response,
    node_set_engine_set_point_payload,
    node_set_gauge_alarms_payload,
    node_set_prediction_payload,
    node_set_probe_alarms_payload,
    node_silence_all_alarms_payload,
    set_prediction_payload,
)

from .test_status import GOLDEN_0X45_FRAME

REQ_ID = 0x12345678
PROBE_SERIAL = 0x10005A4C


def test_crc_check_value() -> None:
    assert crc16_ccitt(b"123456789") == 0x29B1


@pytest.mark.parametrize(
    ("frame", "expected"),
    [
        (
            encode_probe_request(
                0x05, set_prediction_payload(65.0, PredictionMode.TIME_TO_REMOVAL)
            ),
            "CAFE70C205028A06",
        ),
        (
            encode_probe_request(0x05, set_prediction_payload(0, PredictionMode.NONE)),
            "CAFEE556050200 00",
        ),
        (encode_probe_request(0x0C), "CAFE62580C00"),
        (encode_probe_request(0x0A), "CAFEC4F20A00"),
        (encode_probe_request(0x01, bytes([2])), "CAFEDFE8010102"),
        (
            encode_node_request(
                0x05,
                REQ_ID,
                node_set_prediction_payload(PROBE_SERIAL, 65.0, PredictionMode.TIME_TO_REMOVAL),
            ),
            "CAFE349805785634120 64C5A00108A06",
        ),
        (
            encode_node_request(0x0C, REQ_ID, node_silence_all_alarms_payload()),
            "CAFED5F60C7856341210" + "0100" + "00" * 14,
        ),
        (
            encode_node_request(
                0x61,
                REQ_ID,
                node_set_gauge_alarms_payload(
                    "GGG198A54", AlarmStatus(set=True, temperature=250.0), AlarmStatus()
                ),
            ),
            "CAFE4F2A61785634120E4747473139384135340061540000",
        ),
        (
            encode_node_request(
                0x71, REQ_ID, node_set_engine_set_point_payload("ABCDEF1234", 120.0)
            ),
            "CAFE1628717856341 20C414243444546313233347805",
        ),
        (
            encode_node_request(0x0A, REQ_ID, PROBE_SERIAL.to_bytes(4, "little")),
            "CAFE6A960A78563412044C5A0010",
        ),
    ],
)
def test_sdk_request_frames(frame: bytes, expected: str) -> None:
    assert frame.hex().upper() == expected.replace(" ", "")


def test_probe_alarm_payload_keeps_every_slot() -> None:
    high = [AlarmStatus()] * 11
    high[8] = AlarmStatus(set=True, temperature=70.0)
    high[10] = AlarmStatus(set=True, temperature=300.0)
    payload = node_set_probe_alarms_payload(PROBE_SERIAL, tuple(high), (AlarmStatus(),) * 11)
    frame = encode_node_request(0x0B, REQ_ID, payload)
    expected_array = (
        "00000000000000000000000000000000211C0000016400000000000000000000000000000000000000000000"
    )
    assert frame.hex().upper() == "CAFEB6C00B78563412304C5A0010" + expected_array


def test_set_prediction_rounds_instead_of_truncating() -> None:
    assert set_prediction_payload(54.4, PredictionMode.TIME_TO_REMOVAL) == (544 | 1 << 10).to_bytes(
        2, "little"
    )


def test_decoder_splits_packed_and_reassembles_split_frames() -> None:
    ack = encode_node_response(0x05, REQ_ID, 0xCAFEBABE, True)
    assert ack.hex().upper() == "CAFE68F58578563412BEBAFECA0100"
    stream = b"\x00garbage" + GOLDEN_0X45_FRAME + ack + GOLDEN_0X45_FRAME
    decoder = FrameDecoder(Framing.NODE)
    frames = []
    for i in range(0, len(stream), 20):  # 20-byte notifications, as with a default MTU
        frames += decoder.feed(stream[i : i + 20])
    assert [f.message_type for f in frames] == [0x45, 0x05, 0x45]
    assert frames[1].is_response and frames[1].success
    assert frames[1].request_id == REQ_ID and frames[1].response_id == 0xCAFEBABE
    assert ProbeStatus.decode_node_payload(frames[2].payload) is not None


def test_decoder_skips_corrupt_frame() -> None:
    bad = bytearray(GOLDEN_0X45_FRAME)
    bad[40] ^= 0xFF
    frames = FrameDecoder(Framing.NODE).feed(bytes(bad) + GOLDEN_0X45_FRAME)
    assert len(frames) == 1
    assert frames[0].message_type == MessageType.PROBE_STATUS


def test_probe_decoder_reads_responses() -> None:
    frames = FrameDecoder(Framing.PROBE).feed(
        encode_probe_response(0x09, True) + encode_probe_response(0x09, False)
    )
    assert encode_probe_response(0x09, True).hex().upper() == "CAFE3C61090100"
    assert [(f.message_type, f.success) for f in frames] == [(0x09, True), (0x09, False)]


def test_decoder_does_not_wait_on_an_impossible_response_reading() -> None:
    # A corrupt request (bit 7 clear) must not hold back the complete ACK after it.
    bad = bytearray(encode_node_request(0x45, 1, b"\xff"))
    bad[2] ^= 0xFF
    ack = encode_node_response(0x61, 10, 11, True)
    frames = FrameDecoder(Framing.NODE).feed(bytes(bad) + ack)
    assert [(f.message_type, f.is_response, f.request_id) for f in frames] == [(0x61, True, 10)]


def test_decoder_does_not_wait_on_an_impossible_request_reading() -> None:
    # The converse: a corrupt response (bit 7 set) is no request either, even though
    # its response ID would make a long request length.
    bad = bytearray(encode_node_response(0x61, 1, 0xFF, True))
    bad[2] ^= 1
    ack = encode_node_response(0x61, 2, 3, True)
    frames = FrameDecoder(Framing.NODE).feed(bytes(bad) + ack)
    assert [(f.message_type, f.request_id) for f in frames] == [(0x61, 2)]


def test_decoder_keeps_display_status_as_a_request() -> None:
    # Display Status is numbered 0x80, the only request with bit 7 set.
    frames = FrameDecoder(Framing.NODE).feed(encode_node_request(0x80, 5, bytes(25)))
    assert [(f.message_type, f.is_response) for f in frames] == [(0x80, False)]
