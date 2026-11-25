"""Support for IKEA Tradfri."""

import asyncio
from datetime import datetime, timedelta

from pytradfri import Gateway, RequestError
from pytradfri.api.aiocoap_api import APIFactory, APIRequestProtocol
from pytradfri.command import Command
from pytradfri.device import Device
from pytradfri.error import ClientError
from pytradfri.group import Group

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.typing import ConfigType

from .admin import SUPERGROUP_ID
from .const import CONF_GATEWAY_ID, CONF_IDENTITY, CONF_KEY, DOMAIN, LOGGER
from .coordinator import (
    TradfriConfigEntry,
    TradfriData,
    TradfriDeviceDataUpdateCoordinator,
    TradfriGroupDataUpdateCoordinator,
)
from .services import async_setup_services, async_unload_management, gateway_operation

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

PLATFORMS = [
    Platform.BUTTON,
    Platform.COVER,
    Platform.FAN,
    Platform.LIGHT,
    Platform.SENSOR,
    Platform.SWITCH,
]
SIGNAL_GW = "tradfri.gw_status"
TIMEOUT_API = 30


async def _fetch_each[T](api: APIRequestProtocol, commands: list[Command[T]]) -> list[T]:
    """Fetch each item, skipping ones deleted on the gateway meanwhile."""
    results = await asyncio.gather(
        *(api(command, timeout=TIMEOUT_API) for command in commands),
        return_exceptions=True,
    )
    items: list[T] = []
    for result in results:
        if isinstance(result, ClientError) and "4.04" in str(result):
            continue
        if isinstance(result, BaseException):
            raise result
        items.append(result)
    return items


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the gateway management actions."""
    async_setup_services(hass)
    return True


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TradfriConfigEntry,
) -> None:
    """Create a gateway."""
    factory = await APIFactory.init(
        entry.data[CONF_HOST],
        psk_id=entry.data[CONF_IDENTITY],
        psk=entry.data[CONF_KEY],
    )

    async def on_hass_stop(event: Event) -> None:
        """Close connection when hass stops."""
        await factory.shutdown()

    # Setup listeners
    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, on_hass_stop)
    )

    api: APIRequestProtocol = factory.request
    gateway = Gateway()

    try:
        gateway_info = await api(gateway.get_gateway_info(), timeout=TIMEOUT_API)
        devices_commands: list[Command[Device]] = await api(
            gateway.get_devices(), timeout=TIMEOUT_API
        )
        devices: list[Device] = await _fetch_each(api, devices_commands)
        group_commands: list[Command[Group]] = await api(
            gateway.get_groups(), timeout=TIMEOUT_API
        )
        groups: list[Group] = await _fetch_each(api, group_commands)

    except RequestError as exc:
        await factory.shutdown()
        raise ConfigEntryNotReady from exc

    # Only expose groups that actually contain a light; the gateway also keeps
    # controller-only groups (one per paired remote) which have nothing to
    # show, and the SuperGroup of all devices is not a room.
    light_ids = {device.id for device in devices if device.has_light_control}
    group_lights = [
        group
        for group in groups
        if group.id != SUPERGROUP_ID and light_ids.intersection(group.member_ids)
    ]

    dev_reg = dr.async_get(hass)
    dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        connections=set(),
        identifiers={(DOMAIN, entry.data[CONF_GATEWAY_ID])},
        manufacturer="IKEA of Sweden",
        name="Gateway",
        # They just have 1 gateway model. Type is not exposed yet.
        model="E1526",
        sw_version=gateway_info.firmware_version,
    )

    remove_stale_devices(hass, entry, devices, group_lights)

    # The factory holds the connection and every observation started below;
    # if setup fails or is cancelled from here on, nothing else shuts it down.
    try:
        # Setup the device coordinators
        tradfri_data = TradfriData(factory=factory, gateway=gateway, api=api)

        for device in devices:
            coordinator = TradfriDeviceDataUpdateCoordinator(
                hass=hass, config_entry=entry, api=api, device=device
            )
            await coordinator.async_config_entry_first_refresh()

            entry.async_on_unload(
                async_dispatcher_connect(
                    hass, SIGNAL_GW, coordinator.set_hub_available
                )
            )
            tradfri_data.coordinator_list.append(coordinator)

        for group in group_lights:
            group_coordinator = TradfriGroupDataUpdateCoordinator(
                hass=hass, config_entry=entry, api=api, group=group
            )
            await group_coordinator.async_config_entry_first_refresh()

            entry.async_on_unload(
                async_dispatcher_connect(
                    hass, SIGNAL_GW, group_coordinator.set_hub_available
                )
            )
            tradfri_data.group_coordinator_list.append(group_coordinator)

        entry.runtime_data = tradfri_data

        async def async_keep_alive(now: datetime) -> None:
            if hass.is_stopping:
                return

            gw_status = True
            try:
                await api(gateway.get_gateway_info())
            except RequestError:
                LOGGER.error("Keep-alive failed")
                gw_status = False

            async_dispatcher_send(hass, SIGNAL_GW, gw_status)

        entry.async_on_unload(
            async_track_time_interval(hass, async_keep_alive, timedelta(seconds=60))
        )

        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        await factory.shutdown()
        raise


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: TradfriConfigEntry, device_entry: dr.DeviceEntry
) -> bool:
    """Allow deleting a device in the UI; it is also unpaired on the gateway.

    Only devices the gateway no longer lists or cannot reach may be deleted
    this way. Reachable devices need the tradfri.remove_device action,
    because an unpaired device must be reset before it can be paired again.
    """
    prefix = f"{entry.data[CONF_GATEWAY_ID]}-"
    device_ids = []
    for domain, identifier in device_entry.identifiers:
        if domain != DOMAIN or not identifier.startswith(prefix):
            continue
        if identifier.removeprefix(prefix).startswith("group-"):
            raise HomeAssistantError(
                f"{device_entry.name} is a group; delete it with the"
                " tradfri.delete_group action"
            )
        device_ids.append(int(identifier.removeprefix(prefix)))
    if not device_ids:
        return False  # The gateway itself.
    try:
        async with gateway_operation(hass, entry) as admin:
            device = next(
                (d for d in await admin.devices() if d.id == device_ids[0]), None
            )
            if device is None:
                return True
            if device.reachable:
                raise HomeAssistantError(
                    f"{device.name} is reachable; use the tradfri.remove_device "
                    "action to unpair it"
                )
            await admin.remove_device(device.id)
    except RequestError as err:
        raise HomeAssistantError(f"The IKEA gateway refused: {err}") from err
    hass.config_entries.async_schedule_reload(entry.entry_id)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: TradfriConfigEntry) -> None:
    """Unload a config entry."""
    await async_unload_management(hass, entry)
    await hass.config_entries.async_unload_platforms_or_raise(entry, PLATFORMS)
    await entry.runtime_data.factory.shutdown()


@callback
def remove_stale_devices(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    devices: list[Device],
    groups: list[Group],
) -> None:
    """Remove stale devices from device registry."""
    device_registry = dr.async_get(hass)
    device_entries = dr.async_entries_for_config_entry(
        device_registry, config_entry.entry_id
    )
    all_device_ids = {str(device.id) for device in devices}
    # Group lights register their own device entry, keyed "group-<id>".
    all_device_ids |= {f"group-{group.id}" for group in groups}

    for device_entry in device_entries:
        device_id: str | None = None
        gateway_id: str | None = None

        for identifier in device_entry.identifiers:
            if identifier[0] != DOMAIN:
                continue

            _id = identifier[1]

            # Identify gateway device.
            if _id == config_entry.data[CONF_GATEWAY_ID]:
                gateway_id = _id
                break

            device_id = _id.replace(f"{config_entry.data[CONF_GATEWAY_ID]}-", "")
            break

        if gateway_id is not None:
            # Do not remove gateway device entry.
            continue

        if device_id is None or device_id not in all_device_ids:
            # If device_id is None an invalid device entry
            # was found for this config entry.
            # If the device_id is not in existing device ids it's a stale device entry.
            # Remove the device entry in either case.
            device_registry.async_remove_device(device_entry.id)


async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> None:
    """Migrate old entry."""
    LOGGER.debug(
        "Migrating Tradfri configuration from version %s.%s",
        config_entry.version,
        config_entry.minor_version,
    )

    if config_entry.version == 1:
        # Migrate to version 2
        migrate_config_entry_and_identifiers(hass, config_entry)

        hass.config_entries.async_update_entry(config_entry, version=2)

    LOGGER.debug(
        "Migration to Tradfri configuration version %s.%s successful",
        config_entry.version,
        config_entry.minor_version,
    )


def migrate_config_entry_and_identifiers(
    hass: HomeAssistant, config_entry: ConfigEntry
) -> None:
    """Migrate old non-unique identifiers to new unique identifiers."""

    related_device_flag: bool
    device_id: str

    device_reg = dr.async_get(hass)
    # Get all devices associated to contextual gateway config_entry
    # and loop through list of devices.
    for device in dr.async_entries_for_config_entry(device_reg, config_entry.entry_id):
        related_device_flag = False
        for identifier in device.identifiers:
            if identifier[0] != DOMAIN:
                continue

            related_device_flag = True

            _id = identifier[1]

            # Identify gateway device.
            if _id == config_entry.data[CONF_GATEWAY_ID]:
                # Using this to avoid updating gateway's own device registry entry
                related_device_flag = False
                break

            device_id = str(_id)
            break

        # Check that device is related to tradfri domain (and is not the gateway itself)
        if not related_device_flag:
            continue

        if config_entry.data[CONF_GATEWAY_ID] in device_id:
            continue

        device_reg.async_update_device(
            device.id,
            new_identifiers={
                (DOMAIN, f"{config_entry.data[CONF_GATEWAY_ID]}-{device_id}")
            },
        )
