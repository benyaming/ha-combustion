"""Select: the probe's prediction mode."""

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN, DeviceKind
from .entity import CombustionEntity, CombustionEntityDescription, async_setup_platform, kinds
from .hub import CombustionConfigEntry
from .models import CombustionDevice, ProbeDevice
from .protocol.const import PredictionMode

PARALLEL_UPDATES = 1

OPTIONS = {
    "none": PredictionMode.NONE,
    "time_to_removal": PredictionMode.TIME_TO_REMOVAL,
    "removal_and_resting": PredictionMode.REMOVAL_AND_RESTING,
}


def _probe_status(device: CombustionDevice, now: float) -> bool:
    return (
        isinstance(device, ProbeDevice) and device.has_status(now) and device.prediction is not None
    )


SELECTS = (
    CombustionEntityDescription(
        key="prediction_mode",
        translation_key="prediction_mode",
        kinds=kinds(DeviceKind.PROBE),
        requires_connection=True,
        available_fn=_probe_status,
    ),
)


class PredictionModeSelect(CombustionEntity, SelectEntity):
    _attr_options = list(OPTIONS)

    @property
    def current_option(self) -> str | None:
        assert isinstance(self.device, ProbeDevice)
        prediction = self.device.prediction
        if prediction is None:
            return None
        return next((k for k, v in OPTIONS.items() if v is prediction.mode), None)

    async def async_select_option(self, option: str) -> None:
        assert isinstance(self.device, ProbeDevice)
        mode = OPTIONS[option]
        set_point = self.device.prediction.set_point if self.device.prediction else 0.0
        if mode is PredictionMode.NONE:
            set_point = 0.0
        elif set_point <= 0:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="removal_temperature_required"
            )
        await self.hub.async_set_prediction(self.device, mode, set_point)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CombustionConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_setup_platform(hass, entry, async_add_entities, SELECTS, PredictionModeSelect)
