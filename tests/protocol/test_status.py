"""GATT status decoding.

Vectors come from the official SDK tests: the firmware golden Probe Status
notification and 0x45 frame (ProbeStatusTest.kt, NodeProbeStatusRequestTest.kt),
GaugeStatusTest.kt and EngineStatusTest.kt.
"""

from custom_components.combustion_inc.protocol import (
    EngineStatus,
    GaugeStatus,
    ProbeMode,
    ProbeStatus,
)
from custom_components.combustion_inc.protocol.const import (
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
from custom_components.combustion_inc.protocol.uart import FrameDecoder, Framing

GOLDEN_NOTIFICATION = bytes.fromhex(
    "040302010807060523C18A241E5EF5DE6A8291B81A89A553714A03E1A0550820B0C412E0150A6004"
    "09A291786F5E4D04A50505080B0815081B0825082B0835083B0845084B08550803100E1013101E10"
    "23102E1033103E1043104E105310EFBEADDE8813"
)
GOLDEN_0X45_FRAME = bytes.fromhex(
    "CAFEC38245D4C3B2A16978563412040302010807060523C18A241E5EF5DE6A8291B81A89A553714A"
    "03E1A0550820B0C412E0150A600409A291786F5E4D0400A50505080B0815081B0825082B0835083B"
    "0845084B08550803100E1013101E1023102E1033103E1043104E105310EFBEADDE8813"
)


def assert_golden(status: ProbeStatus) -> None:
    assert status.min_sequence == 0x01020304
    assert status.max_sequence == 0x05060708
    assert status.temperatures == (-5.45, 35.5, 76.45, 117.4, 158.35, -4.55, 9.1, 22.75)
    assert status.mode_id_color.mode is ProbeMode.INSTANT_READ
    assert status.mode_id_color.color == 2
    assert status.mode_id_color.probe_id == 4
    assert status.battery_low is True
    assert (status.virtual_sensors.core, status.virtual_sensors.surface) == (2, 5)
    assert status.virtual_sensors.ambient == 6
    pred = status.prediction
    assert pred.state is PredictionState.PREDICTING
    assert pred.mode is PredictionMode.TIME_TO_REMOVAL
    assert pred.type is PredictionType.REMOVAL
    assert (pred.set_point, pred.heat_start) == (62.5, 21.0)
    assert pred.seconds == 3600
    assert pred.estimated_core == 48.5
    assert pred.percent_to_removal == 66.3
    assert status.food_safe_config is not None
    assert status.food_safe_config.mode is FoodSafeMode.SIMPLIFIED
    assert (status.food_safe_config.product, status.food_safe_config.serving) == (1, 1)
    assert status.food_safe_status is not None
    assert status.food_safe_status.state is FoodSafeState.SAFE
    assert status.food_safe_status.log_reduction == 6.5
    assert status.food_safe_status.seconds_above_threshold == 4660
    assert status.food_safe_status.sequence == 0x89ABCDEF
    assert status.overheat_raw == 0xA5
    assert status.power_mode == 1
    assert status.high_radio_power is True
    assert status.high_alarms is not None and status.low_alarms is not None
    t1_high, core_high = status.high_alarms[0], status.high_alarms[8]
    assert (t1_high.set, t1_high.tripped, t1_high.alarming, t1_high.temperature) == (
        True,
        False,
        True,
        5.6,
    )
    assert core_high.temperature == 6.4
    ambient_low = status.low_alarms[10]
    assert (ambient_low.set, ambient_low.tripped, ambient_low.temperature) == (True, True, 32.2)
    assert status.session_id == 0xDEADBEEF
    assert status.sample_period_ms == 5000


def test_direct_characteristic_golden() -> None:
    status = ProbeStatus.decode_characteristic(GOLDEN_NOTIFICATION)
    assert status is not None
    assert status.serial is None
    assert status.hop_count is None
    assert_golden(status)


def test_direct_characteristic_old_firmware_has_no_session() -> None:
    status = ProbeStatus.decode_characteristic(GOLDEN_NOTIFICATION[:94])
    assert status is not None
    assert status.high_alarms is not None
    assert status.session_id is None


def test_direct_characteristic_zero_session_means_unknown() -> None:
    data = bytearray(GOLDEN_NOTIFICATION)
    data[94:98] = bytes(4)
    status = ProbeStatus.decode_characteristic(bytes(data))
    assert status is not None
    assert status.session_id is None


def test_direct_characteristic_minimal() -> None:
    status = ProbeStatus.decode_characteristic(GOLDEN_NOTIFICATION[:30])
    assert status is not None
    assert status.food_safe_status is None
    assert status.overheat_raw is None
    assert status.high_alarms is None
    assert ProbeStatus.decode_characteristic(GOLDEN_NOTIFICATION[:29]) is None


def test_sdk_prediction_field_vector() -> None:
    data = bytearray(b"\xff" * 48)
    data[23:30] = bytes(
        [0b10_01_0011, 0xA9, 0b101010_10, 0b1101_1001, 0b1101_1011, 0b101_10101, 0xF9]
    )
    status = ProbeStatus.decode_characteristic(bytes(data))
    assert status is not None
    pred = status.prediction
    assert pred.state is PredictionState.PREDICTING
    assert pred.mode is PredictionMode.TIME_TO_REMOVAL
    assert pred.type is PredictionType.RESTING
    assert pred.set_point == 68.1
    assert pred.heat_start == 61.8
    assert pred.seconds == 89533
    assert pred.estimated_core == round(1997 * 0.1 - 20, 1)


def test_sdk_food_safe_status_vector() -> None:
    data = bytearray(b"\xff" * 48)
    data[40:48] = bytes(
        [0b11011_010, 0b01011_101, 0xE6, 0b01110_101, 0xD6, 0x7F, 0xF7, 0b11111_101]
    )
    status = ProbeStatus.decode_characteristic(bytes(data))
    assert status is not None and status.food_safe_status is not None
    assert status.food_safe_status.state is FoodSafeState.SAFETY_IMPOSSIBLE
    assert status.food_safe_status.log_reduction == 18.7
    assert status.food_safe_status.seconds_above_threshold == 48331
    assert status.food_safe_status.sequence == 0xBEEFFACE


def test_node_probe_status_golden_frame() -> None:
    frames = FrameDecoder(Framing.NODE).feed(GOLDEN_0X45_FRAME)
    assert len(frames) == 1
    frame = frames[0]
    assert frame.message_type == 0x45
    assert frame.request_id == 0xA1B2C3D4
    assert not frame.is_response
    status = ProbeStatus.decode_node_payload(frame.payload)
    assert status is not None
    assert status.serial == 0x12345678
    assert status.serial_str == "12345678"
    assert status.hop_count == 1
    assert_golden(status)


def test_node_probe_status_from_older_node_has_no_session() -> None:
    payload = GOLDEN_0X45_FRAME[10:][:99]
    status = ProbeStatus.decode_node_payload(payload)
    assert status is not None
    assert status.high_alarms is not None
    assert status.session_id is None


def test_node_probe_status_pre_food_safe_layout() -> None:
    payload = GOLDEN_0X45_FRAME[10:44] + bytes([0x40])
    status = ProbeStatus.decode_node_payload(payload)
    assert status is not None
    assert status.hop_count == 2
    assert status.food_safe_status is None
    assert status.temperatures[1] == 35.5


def test_node_probe_status_ambiguous_length_is_rejected() -> None:
    assert ProbeStatus.decode_node_payload(GOLDEN_0X45_FRAME[10:50]) is None


def test_gauge_status() -> None:
    serial = b"GGG198A54\x00"
    status_bytes = bytearray(25)
    status_bytes[0:4] = (0xCAFEBABE).to_bytes(4, "little")
    status_bytes[4:6] = (1000).to_bytes(2, "little")
    status_bytes[6:8] = (1700).to_bytes(2, "little")  # 150.0 °C
    status_bytes[8] = 0b101  # present, low battery
    status_bytes[9:13] = (7).to_bytes(4, "little")
    status_bytes[13:17] = (99).to_bytes(4, "little")
    status_bytes[17] = 100
    status_bytes[18:22] = bytes.fromhex("E347C112")
    status_bytes[22] = 1
    status_bytes[23] = 0xC0
    status_bytes[24] = 5
    status = GaugeStatus.decode_node_payload(serial + bytes(status_bytes))
    assert status is not None
    assert status.serial == "GGG198A54"
    assert status.session_id == 0xCAFEBABE
    assert status.sample_period_ms == 1000
    assert status.temperature == 150.0
    assert status.low_battery and not status.overheating
    assert (status.min_sequence, status.max_sequence) == (7, 99)
    assert status.high_alarm.temperature == 210.0
    assert status.low_alarm.temperature == 40.0
    assert status.new_record is True
    assert status.hop_count == 4
    assert status.gauge_id == 5
    assert GaugeStatus.decode_node_payload(serial + bytes(status_bytes[:24])).gauge_id is None
    assert GaugeStatus.decode_node_payload(serial + bytes(status_bytes[:23])) is None


ENGINE_STATUS = bytes.fromhex(
    "EFBEADDE881364000000C8000000010178B004BC02014CDA001000000000000000000704504B46E803"
    "0000D007000003FA0301DC053CFF40E40C080703"
)
ENGINE_STATUS_GAUGE_CONTROL = bytes.fromhex(
    "EFBEADDE881364000000C8000000010178B004BC02034142313243443334454600000704504B46E803"
    "0000D007000003FA0301DC053CFF40E40C080703"
)
ENGINE_STATUS_NO_CONTROL = bytes.fromhex(
    "EFBEADDE881364000000C8000000010178B004BC02000000000000000000000000000504504B46E803"
    "0000D007000003FA0301DC053CFF40E40C080703"
)


def test_engine_status_sdk_vector() -> None:
    status = EngineStatus.decode_node_payload(b"ABCDEF1234" + ENGINE_STATUS)
    assert status is not None
    assert status.serial == "ABCDEF1234"
    assert status.session_id == 0xDEADBEEF
    assert status.sample_period_ms == 5000
    assert (status.min_sequence, status.max_sequence) == (100, 200)
    assert status.battery_level is BatteryLevel.LOW
    assert status.charging_state is ChargingState.CHARGING
    assert status.battery_voltage == 12.0
    assert status.set_point == 100.0
    assert status.control_device_type is ProductType.PROBE
    assert status.control_device_serial == "1000DA4C"
    assert status.control_temperature == 50.0
    assert status.app_mode and status.control_device_connected and status.lid_open
    assert not status.fixed_speed
    assert status.fan.state is EngineFanState.FAN_ON
    assert (status.fan.duty_cycle, status.fan.commanded_speed, status.fan.measured_speed) == (
        80,
        75,
        70,
    )
    assert (status.fan.off_time_ms, status.fan.on_time_ms) == (1000, 2000)
    ctrl = status.controller
    assert ctrl.state is EngineControllerState.OBSERVE
    assert ctrl.response_coefficient == 0.5
    assert ctrl.cycles_completed == 3
    assert ctrl.reached_set_point and not ctrl.maintenance_mode
    assert ctrl.smoothed_temperature == 150.0
    assert ctrl.time_to_peak_s == 60
    assert ctrl.drift_rate == -0.001
    assert status.hop_count == 2
    assert status.knob_voltage == 3.3
    assert status.knob_angle == 180.0
    assert status.charging_fault == 1


def test_engine_status_control_device_variants() -> None:
    gauge = EngineStatus.decode_node_payload(b"ABCDEF1234" + ENGINE_STATUS_GAUGE_CONTROL)
    assert gauge is not None
    assert gauge.control_device_type is ProductType.GAUGE
    assert gauge.control_device_serial == "AB12CD34EF"
    none = EngineStatus.decode_node_payload(b"ABCDEF1234" + ENGINE_STATUS_NO_CONTROL)
    assert none is not None
    assert none.control_device_type is None
    assert none.control_device_serial is None
    assert none.control_temperature is None
    assert EngineStatus.decode_node_payload(b"ABCDEF1234" + ENGINE_STATUS[:60]) is None
