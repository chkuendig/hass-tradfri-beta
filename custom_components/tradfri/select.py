"""Power-on behavior of IKEA Tradfri lights and outlets."""

from typing import override

from pytradfri.api.aiocoap_api import APIRequestProtocol
from pytradfri.const import StartupOnOff
from pytradfri.error import RequestError

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_GATEWAY_ID
from .coordinator import TradfriConfigEntry, TradfriDeviceDataUpdateCoordinator
from .entity import TradfriBaseEntity

# The option names Home Assistant shows, by the wire values pytradfri defines.
# This is the only place they are paired; Tuya's 0x8002 attribute encodes the
# same choices differently (2 means "previous" there), so another write path
# must not reuse raw numbers from here.
POWER_ON_BEHAVIORS: dict[StartupOnOff, str] = {
    StartupOnOff.OFF: "off",
    StartupOnOff.ON: "on",
    StartupOnOff.TOGGLE: "toggle",
    StartupOnOff.PREVIOUS: "previous",
}
_BY_OPTION = {option: value for value, option in POWER_ON_BEHAVIORS.items()}


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: TradfriConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add a power-on behavior select for each light and outlet."""
    gateway_id = config_entry.data[CONF_GATEWAY_ID]
    tradfri_data = config_entry.runtime_data

    # The gateway includes the setting in every light and outlet resource,
    # with 0 while the device has not reported it, so it cannot tell which
    # devices support it; whether a device honors a write is up to it.
    async_add_entities(
        TradfriPowerOnBehavior(device_coordinator, tradfri_data.api, gateway_id)
        for device_coordinator in tradfri_data.coordinator_list
        if device_coordinator.device.has_light_control
        or device_coordinator.device.has_socket_control
    )


class TradfriPowerOnBehavior(TradfriBaseEntity, SelectEntity):
    """What a light or outlet does when its power comes back."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "power_on_behavior"
    _attr_options = list(POWER_ON_BEHAVIORS.values())

    def __init__(
        self,
        device_coordinator: TradfriDeviceDataUpdateCoordinator,
        api: APIRequestProtocol,
        gateway_id: str,
    ) -> None:
        """Initialize the select."""
        super().__init__(
            device_coordinator=device_coordinator, api=api, gateway_id=gateway_id
        )
        self._request = api
        self._attr_unique_id = f"{self._attr_unique_id}-power_on_behavior"
        self._refresh()

    @callback
    @override
    def _refresh(self) -> None:
        """Read the setting from the device data."""
        self._device = self.coordinator.data
        if self._device.has_light_control:
            control = self._device.light_control
            assert control is not None
            value = control.lights[0].startup_on_off
        else:
            socket_control = self._device.socket_control
            assert socket_control is not None
            value = socket_control.sockets[0].startup_on_off
        self._attr_current_option = (
            None if value is None else POWER_ON_BEHAVIORS[value]
        )

    @override
    async def async_select_option(self, option: str) -> None:
        """Write the setting to the device through the gateway."""
        value = _BY_OPTION[option]
        if self._device.has_light_control:
            control = self._device.light_control
            assert control is not None
            command = control.set_startup_on_off(value)
        else:
            socket_control = self._device.socket_control
            assert socket_control is not None
            command = socket_control.set_startup_on_off(value)
        try:
            await self._request(command)
        except RequestError as err:
            raise HomeAssistantError(
                f"The gateway did not accept the power-on behavior for"
                f" {self._device.name}: {err}"
            ) from err
