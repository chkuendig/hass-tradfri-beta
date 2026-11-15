"""Gateway restart button."""

from __future__ import annotations

from typing import override

from pytradfri import RequestError

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_GATEWAY_ID, DOMAIN
from .coordinator import TradfriConfigEntry
from .services import gateway_operation


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: TradfriConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the gateway restart button."""
    async_add_entities([TradfriGatewayRestartButton(config_entry)])


class TradfriGatewayRestartButton(ButtonEntity):
    """Reboot the IKEA gateway (about 1-2 minutes offline)."""

    _attr_has_entity_name = True
    _attr_device_class = ButtonDeviceClass.RESTART
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry: TradfriConfigEntry) -> None:
        gateway_id = entry.data[CONF_GATEWAY_ID]
        self._entry = entry
        self._attr_unique_id = f"{gateway_id}-restart"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, gateway_id)})

    @override
    async def async_press(self) -> None:
        try:
            async with gateway_operation(self.hass, self._entry) as admin:
                await admin.reboot()
        except RequestError as err:
            raise HomeAssistantError(f"The IKEA gateway refused: {err}") from err
