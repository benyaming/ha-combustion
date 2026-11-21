"""Sensors for Combustion devices."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    UnitOfElectricPotential,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DeviceKind
from .entity import (
    ALL_KINDS,
    CombustionEntity,
    CombustionEntityDescription,
    async_setup_platform,
    kinds,
)
from .hub import CombustionConfigEntry
from .models import CombustionDevice, EngineDevice, ProbeDevice
from .protocol.const import PredictionMode, PredictionType, ProbeMode

PARALLEL_UPDATES = 0

COLORS = ("yellow", "grey", "color_3", "color_4", "color_5", "color_6", "color_7", "color_8")
PROBE = kinds(DeviceKind.PROBE)
GAUGE = kinds(DeviceKind.GAUGE)
ENGINE = kinds(DeviceKind.ENGINE)


@dataclass(frozen=True, kw_only=True)
class CombustionSensorDescription(CombustionEntityDescription, SensorEntityDescription):
    value_fn: Callable[[Any, float], Any]


def _probe_status(device: CombustionDevice, now: float) -> bool:
    return (
        isinstance(device, ProbeDevice) and device.has_status(now) and device.prediction is not None
    )


def _engine_status(device: CombustionDevice, now: float) -> bool:
    return isinstance(device, EngineDevice) and device.has_status(now)


def _temperature(key: str, **kwargs: Any) -> CombustionSensorDescription:
    return CombustionSensorDescription(
        key=key,
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        **kwargs,
    )


def _thermistor(index: int) -> CombustionSensorDescription:
    return _temperature(
        f"t{index + 1}_temperature",
        translation_key="thermistor",
        translation_placeholders={"number": str(index + 1)},
        kinds=PROBE,
        entity_registry_enabled_default=False,
        value_fn=lambda d, _now: d.temperatures[index] if d.temperatures else None,
    )


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 1)


SENSORS: tuple[CombustionSensorDescription, ...] = (
    # Probe
    _temperature(
        "core_temperature",
        translation_key="core_temperature",
        kinds=PROBE,
        value_fn=lambda d, _now: _round(d.virtual_temperature("core")),
    ),
    _temperature(
        "surface_temperature",
        translation_key="surface_temperature",
        kinds=PROBE,
        value_fn=lambda d, _now: _round(d.virtual_temperature("surface")),
    ),
    _temperature(
        "ambient_temperature",
        translation_key="ambient_temperature",
        kinds=PROBE,
        value_fn=lambda d, _now: _round(d.virtual_temperature("ambient")),
    ),
    *(_thermistor(i) for i in range(8)),
    _temperature(
        "instant_read",
        translation_key="instant_read",
        kinds=PROBE,
        available_fn=lambda d, now: d.instant_read_value(now) is not None,
        value_fn=lambda d, now: _round(d.instant_read_value(now)),
    ),
    CombustionSensorDescription(
        key="mode",
        translation_key="mode",
        kinds=PROBE,
        device_class=SensorDeviceClass.ENUM,
        options=[mode.name.lower() for mode in ProbeMode],
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d, _now: d.mode.name.lower() if d.mode is not None else None,
    ),
    CombustionSensorDescription(
        key="probe_id",
        translation_key="probe_id",
        kinds=PROBE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d, _now: d.probe_id + 1 if d.probe_id is not None else None,
    ),
    CombustionSensorDescription(
        key="color",
        translation_key="color",
        kinds=PROBE,
        device_class=SensorDeviceClass.ENUM,
        options=list(COLORS),
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d, _now: COLORS[d.color] if d.color is not None else None,
    ),
    CombustionSensorDescription(
        key="hop_count",
        translation_key="hop_count",
        kinds=PROBE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d, _now: d.hop_count,
    ),
    # Probe, from a connection
    CombustionSensorDescription(
        key="prediction_state",
        translation_key="prediction_state",
        kinds=PROBE,
        requires_connection=True,
        device_class=SensorDeviceClass.ENUM,
        options=[
            "probe_not_inserted",
            "probe_inserted",
            "warming",
            "predicting",
            "removal_prediction_done",
            "unknown",
        ],
        available_fn=_probe_status,
        value_fn=lambda d, _now: (
            d.prediction.state.name.lower() if d.prediction.state is not None else "unknown"
        ),
    ),
    CombustionSensorDescription(
        key="prediction_type",
        translation_key="prediction_type",
        kinds=PROBE,
        requires_connection=True,
        device_class=SensorDeviceClass.ENUM,
        options=["none", "removal", "resting", "reserved"],
        available_fn=_probe_status,
        value_fn=lambda d, _now: d.prediction.type.name.lower(),
    ),
    CombustionSensorDescription(
        key="prediction_remaining",
        translation_key="prediction_remaining",
        kinds=PROBE,
        requires_connection=True,
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_unit_of_measurement=UnitOfTime.MINUTES,
        suggested_display_precision=0,
        available_fn=_probe_status,
        value_fn=lambda d, _now: (
            d.prediction.seconds if d.prediction.type is not PredictionType.NONE else None
        ),
    ),
    CombustionSensorDescription(
        key="prediction_progress",
        translation_key="prediction_progress",
        kinds=PROBE,
        requires_connection=True,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        available_fn=_probe_status,
        value_fn=lambda d, _now: d.prediction.percent_to_removal,
    ),
    _temperature(
        "estimated_core_temperature",
        translation_key="estimated_core_temperature",
        kinds=PROBE,
        requires_connection=True,
        available_fn=_probe_status,
        value_fn=lambda d, _now: d.prediction.estimated_core,
    ),
    _temperature(
        "heat_start_temperature",
        translation_key="heat_start_temperature",
        kinds=PROBE,
        requires_connection=True,
        entity_category=EntityCategory.DIAGNOSTIC,
        available_fn=_probe_status,
        value_fn=lambda d, _now: (
            d.prediction.heat_start if d.prediction.mode is not PredictionMode.NONE else None
        ),
    ),
    CombustionSensorDescription(
        key="food_safe_state",
        translation_key="food_safe_state",
        kinds=PROBE,
        requires_connection=True,
        device_class=SensorDeviceClass.ENUM,
        options=["not_safe", "safe", "safety_impossible"],
        available_fn=lambda d, now: _probe_status(d, now) and d.food_safe is not None,
        value_fn=lambda d, _now: (
            d.food_safe.state.name.lower() if d.food_safe.state is not None else None
        ),
    ),
    CombustionSensorDescription(
        key="food_safe_log_reduction",
        translation_key="food_safe_log_reduction",
        kinds=PROBE,
        requires_connection=True,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        state_class=SensorStateClass.MEASUREMENT,
        available_fn=lambda d, now: _probe_status(d, now) and d.food_safe is not None,
        value_fn=lambda d, _now: d.food_safe.log_reduction,
    ),
    # Gauge
    _temperature(
        "temperature",
        kinds=GAUGE,
        value_fn=lambda d, _now: d.temperature,
    ),
    _temperature(
        "high_alarm_temperature",
        translation_key="high_alarm_temperature",
        kinds=GAUGE,
        replaced_by_control=True,
        value_fn=lambda d, _now: (
            d.high_alarm.temperature if d.high_alarm and d.high_alarm.set else None
        ),
    ),
    _temperature(
        "low_alarm_temperature",
        translation_key="low_alarm_temperature",
        kinds=GAUGE,
        replaced_by_control=True,
        value_fn=lambda d, _now: (
            d.low_alarm.temperature if d.low_alarm and d.low_alarm.set else None
        ),
    ),
    CombustionSensorDescription(
        key="gauge_id",
        translation_key="gauge_id",
        kinds=GAUGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d, _now: d.gauge_id + 1 if d.gauge_id is not None else None,
    ),
    # Engine
    _temperature(
        "set_point",
        translation_key="set_point",
        kinds=ENGINE,
        replaced_by_control=True,
        value_fn=lambda d, _now: d.set_point,
    ),
    _temperature(
        "control_temperature",
        translation_key="control_temperature",
        kinds=ENGINE,
        requires_connection=True,
        available_fn=_engine_status,
        value_fn=lambda d, _now: d.status.control_temperature,
    ),
    CombustionSensorDescription(
        key="fan_state",
        translation_key="fan_state",
        kinds=ENGINE,
        requires_connection=True,
        device_class=SensorDeviceClass.ENUM,
        options=["power_down", "paused", "lid_open", "fan_off", "fan_on"],
        available_fn=_engine_status,
        value_fn=lambda d, _now: (
            d.status.fan.state.name.lower() if d.status.fan.state is not None else None
        ),
    ),
    CombustionSensorDescription(
        key="fan_duty_cycle",
        translation_key="fan_duty_cycle",
        kinds=ENGINE,
        requires_connection=True,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        available_fn=_engine_status,
        value_fn=lambda d, _now: d.status.fan.duty_cycle,
    ),
    CombustionSensorDescription(
        key="fan_speed",
        translation_key="fan_speed",
        kinds=ENGINE,
        requires_connection=True,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        available_fn=_engine_status,
        value_fn=lambda d, _now: d.status.fan.measured_speed,
    ),
    CombustionSensorDescription(
        key="controller_state",
        translation_key="controller_state",
        kinds=ENGINE,
        requires_connection=True,
        device_class=SensorDeviceClass.ENUM,
        options=["idle", "startup", "probe", "observe", "rest"],
        entity_category=EntityCategory.DIAGNOSTIC,
        available_fn=_engine_status,
        value_fn=lambda d, _now: (
            d.status.controller.state.name.lower()
            if d.status.controller.state is not None
            else None
        ),
    ),
    CombustionSensorDescription(
        key="battery_voltage",
        translation_key="battery_voltage",
        kinds=ENGINE,
        requires_connection=True,
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        available_fn=_engine_status,
        value_fn=lambda d, _now: d.status.battery_voltage,
    ),
    CombustionSensorDescription(
        key="charging_state",
        translation_key="charging_state",
        kinds=ENGINE,
        requires_connection=True,
        device_class=SensorDeviceClass.ENUM,
        options=["not_charging", "charging", "fully_charged"],
        entity_category=EntityCategory.DIAGNOSTIC,
        available_fn=_engine_status,
        value_fn=lambda d, _now: (
            d.status.charging_state.name.lower() if d.status.charging_state is not None else None
        ),
    ),
    # Every device
    CombustionSensorDescription(
        key="rssi",
        kinds=ALL_KINDS,
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda d, _now: d.rssi,
    ),
)


class CombustionSensor(CombustionEntity, SensorEntity):
    entity_description: CombustionSensorDescription

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.device, self.hub.now())


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CombustionConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_setup_platform(hass, entry, async_add_entities, SENSORS, CombustionSensor)
