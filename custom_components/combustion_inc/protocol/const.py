"""Constants of the Combustion Inc BLE protocol.

Source: https://github.com/combustion-inc/combustion-documentation
"""

from enum import IntEnum

VENDOR_ID = 0x09C7

UART_SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
UART_RX_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
UART_TX_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"
PROBE_STATUS_SERVICE_UUID = "00000100-caab-3792-3d44-97ae51c1407a"
PROBE_STATUS_CHAR_UUID = "00000101-caab-3792-3d44-97ae51c1407a"


class ProductType(IntEnum):
    """Product type byte that leads every Combustion advertisement."""

    UNKNOWN = 0
    PROBE = 1
    # Probe data repeated by a MeatNet node. The serial in it is the probe's.
    REPEATER = 2
    GAUGE = 3
    DISPLAY = 4
    BOOSTER = 5
    ENGINE = 6


class ProbeMode(IntEnum):
    NORMAL = 0
    INSTANT_READ = 1
    RESERVED = 2
    ERROR = 3


class PredictionState(IntEnum):
    PROBE_NOT_INSERTED = 0
    PROBE_INSERTED = 1
    WARMING = 2
    PREDICTING = 3
    REMOVAL_PREDICTION_DONE = 4
    UNKNOWN = 15


class PredictionMode(IntEnum):
    NONE = 0
    TIME_TO_REMOVAL = 1
    REMOVAL_AND_RESTING = 2
    RESERVED = 3


class PredictionType(IntEnum):
    NONE = 0
    REMOVAL = 1
    RESTING = 2
    RESERVED = 3


class FoodSafeState(IntEnum):
    NOT_SAFE = 0
    SAFE = 1
    SAFETY_IMPOSSIBLE = 2


class FoodSafeMode(IntEnum):
    SIMPLIFIED = 0
    INTEGRATED = 1


class PowerMode(IntEnum):
    NORMAL = 0
    ALWAYS_ON = 1


class EngineFanState(IntEnum):
    POWER_DOWN = 0
    PAUSED = 1
    LID_OPEN = 2
    FAN_OFF = 3
    FAN_ON = 4


class EngineControllerState(IntEnum):
    IDLE = 0
    STARTUP = 1
    PROBE = 2
    OBSERVE = 3
    REST = 4


class BatteryLevel(IntEnum):
    OK = 0
    LOW = 1
    CRITICAL = 2


class ChargingState(IntEnum):
    NOT_CHARGING = 0
    CHARGING = 1
    FULLY_CHARGED = 2


class MessageType(IntEnum):
    """UART message types (direct probe and MeatNet node)."""

    SET_PROBE_ID = 0x01
    SET_PROBE_COLOR = 0x02
    READ_SESSION_INFO = 0x03
    READ_LOGS = 0x04
    SET_PREDICTION = 0x05
    READ_OVER_TEMPERATURE = 0x06
    CONFIGURE_FOOD_SAFE = 0x07
    RESET_FOOD_SAFE = 0x08
    SET_POWER_MODE = 0x09
    RESET_PROBE = 0x0A
    SET_PROBE_HIGH_LOW_ALARM = 0x0B
    SILENCE_ALARMS = 0x0C
    PROBE_STATUS = 0x45
    HEARTBEAT = 0x49
    GAUGE_STATUS = 0x60
    SET_GAUGE_HIGH_LOW_ALARM = 0x61
    READ_GAUGE_LOGS = 0x62
    SET_GAUGE_ID = 0x63
    ENGINE_STATUS = 0x70
    SET_ENGINE_TEMPERATURE_SET_POINT = 0x71
    SET_ENGINE_CONTROL_DEVICE = 0x72


# Probe sensors T1..T8 count as overheating at these temperatures (°C). The probe
# reports an overheat mask too, but repeater firmware <= 2.2.0 sets it wrongly, so
# the official SDK derives overheating from these thresholds and so do we.
OVERHEAT_THRESHOLDS_C = (105.0, 105.0, 115.0, 125.0, 315.56, 315.56, 315.56, 315.56)

# Probe alarm slots, in wire order.
ALARM_SLOTS = ("t1", "t2", "t3", "t4", "t5", "t6", "t7", "t8", "core", "surface", "ambient")
