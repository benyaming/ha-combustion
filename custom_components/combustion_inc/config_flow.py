"""Config flow: one entry covers every Combustion device in Bluetooth range."""

from typing import Any

import voluptuous as vol
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.core import callback

from .const import CONF_ACTIVE_CONNECTION, DEFAULT_ACTIVE_CONNECTION, DOMAIN, MANUFACTURER
from .protocol import VENDOR_ID, parse_advertisement


class CombustionConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        if parse_advertisement(discovery_info.manufacturer_data.get(VENDOR_ID, b"")) is None:
            return self.async_abort(reason="not_supported")
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        return await self.async_step_confirm()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title=MANUFACTURER, data={})
        self._set_confirm_only()
        return self.async_show_form(step_id="confirm")

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> CombustionOptionsFlow:
        return CombustionOptionsFlow()


class CombustionOptionsFlow(OptionsFlowWithReload):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        current = self.config_entry.options.get(CONF_ACTIVE_CONNECTION, DEFAULT_ACTIVE_CONNECTION)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({vol.Required(CONF_ACTIVE_CONNECTION, default=current): bool}),
        )
