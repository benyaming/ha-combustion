"""Constants for the Combustion Inc integration."""

from enum import StrEnum

DOMAIN = "combustion_inc"
MANUFACTURER = "Combustion Inc"

CONF_ACTIVE_CONNECTION = "active_connection"
DEFAULT_ACTIVE_CONNECTION = True

# How often queued state changes are written to entities. Probes advertise every
# 250 ms (100 ms in Instant Read), far more often than anyone needs to record.
FLUSH_INTERVAL = 1.0
AVAILABILITY_INTERVAL = 5.0

# A probe is unavailable after this long without data from any path.
PROBE_TIMEOUT = 30.0
# Nodes also count as present while Home Assistant still tracks their address.
NODE_TIMEOUT = 60.0
# The Instant Read value is dropped this long after the last Instant Read packet.
INSTANT_READ_TIMEOUT = 5.0
# Prediction and other connection-only values expire this long after the last status.
STATUS_TIMEOUT = 60.0
# Direct probe data wins over repeated data for this long, then any path is fine.
DIRECT_PREFERENCE = 5.0


class DeviceKind(StrEnum):
    PROBE = "probe"
    GAUGE = "gauge"
    DISPLAY = "display"
    BOOSTER = "booster"
    ENGINE = "engine"


MODELS = {
    DeviceKind.PROBE: "Predictive Thermometer",
    DeviceKind.GAUGE: "Giant Grill Gauge",
    DeviceKind.DISPLAY: "Display",
    DeviceKind.BOOSTER: "Booster",
    DeviceKind.ENGINE: "Engine",
}

NAMES = {
    DeviceKind.PROBE: "Combustion Probe",
    DeviceKind.GAUGE: "Combustion Gauge",
    DeviceKind.DISPLAY: "Combustion Display",
    DeviceKind.BOOSTER: "Combustion Booster",
    DeviceKind.ENGINE: "Combustion Engine",
}
