"""Combustion Inc BLE protocol: pure Python, no Home Assistant imports."""

from .advertising import (
    Advertisement,
    EngineAdvertisement,
    GaugeAdvertisement,
    NodeAdvertisement,
    ProbeAdvertisement,
    parse_advertisement,
)
from .const import VENDOR_ID, MessageType, ProbeMode, ProductType
from .fields import AlarmStatus, VirtualSensors, format_probe_serial, overheating_sensors
from .status import EngineStatus, GaugeStatus, PredictionStatus, ProbeStatus

__all__ = [
    "VENDOR_ID",
    "Advertisement",
    "AlarmStatus",
    "EngineAdvertisement",
    "EngineStatus",
    "GaugeAdvertisement",
    "GaugeStatus",
    "MessageType",
    "NodeAdvertisement",
    "PredictionStatus",
    "ProbeAdvertisement",
    "ProbeMode",
    "ProbeStatus",
    "ProductType",
    "VirtualSensors",
    "format_probe_serial",
    "overheating_sensors",
    "parse_advertisement",
]
