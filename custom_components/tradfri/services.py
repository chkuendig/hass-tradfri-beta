"""Gateway management actions: what otherwise needs the IKEA Home smart app."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
import contextlib
from dataclasses import dataclass, field
import functools
import time
from typing import Any

from pytradfri import RequestError
from pytradfri.device import Device
from pytradfri.group import Group
import voluptuous as vol

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.util.hass_dict import HassKey

from .admin import (
    PAIRING_RENEW_SECONDS,
    SUPERGROUP_ID,
    AmbiguousGroupError,
    GatewayError,
    GroupNotFoundError,
    TradfriAdmin,
)
from .const import CONF_GATEWAY_ID, DOMAIN, LOGGER
from .coordinator import TradfriConfigEntry

CONF_CONFIG_ENTRY_ID = "config_entry_id"
CONF_DEVICE_ID = "device_id"
CONF_DEVICE_IDS = "device_ids"
CONF_GROUP = "group"
CONF_NAME = "name"
CONF_SECONDS = "seconds"

_NAME = vol.All(cv.string, vol.Strip, vol.Length(min=1))
_ENTRY = {vol.Optional(CONF_CONFIG_ENTRY_ID): cv.string}
_SOME_DEVICES = vol.All(cv.ensure_list, [cv.string], vol.Length(min=1))

SCHEMAS: dict[str, vol.Schema] = {
    "pair_devices": vol.Schema(
        {
            **_ENTRY,
            vol.Optional(CONF_SECONDS, default=120): vol.All(
                vol.Coerce(int), vol.Range(30, 600)
            ),
            vol.Optional(CONF_GROUP): _NAME,
        }
    ),
    "list_groups": vol.Schema(_ENTRY),
    "rename_device": vol.Schema(
        {vol.Required(CONF_DEVICE_ID): cv.string, vol.Required(CONF_NAME): _NAME}
    ),
    "rename_group": vol.Schema(
        {**_ENTRY, vol.Required(CONF_GROUP): _NAME, vol.Required(CONF_NAME): _NAME}
    ),
    "create_group": vol.Schema(
        {
            **_ENTRY,
            vol.Required(CONF_NAME): _NAME,
            vol.Optional(CONF_DEVICE_IDS, default=[]): vol.All(
                cv.ensure_list, [cv.string]
            ),
        }
    ),
    "delete_group": vol.Schema({**_ENTRY, vol.Required(CONF_GROUP): _NAME}),
    "add_to_group": vol.Schema(
        {vol.Required(CONF_GROUP): _NAME, vol.Required(CONF_DEVICE_IDS): _SOME_DEVICES}
    ),
    "remove_from_group": vol.Schema(
        {vol.Required(CONF_GROUP): _NAME, vol.Required(CONF_DEVICE_IDS): _SOME_DEVICES}
    ),
    "remove_device": vol.Schema({vol.Required(CONF_DEVICE_ID): cv.string}),
}


@dataclass
class _GatewayState:
    """Per-gateway coordination of management actions."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pairing: asyncio.Task[list[Device]] | None = None
    unloading: bool = False


DATA_STATE: HassKey[dict[str, _GatewayState]] = HassKey(f"{DOMAIN}_management")


class GatewayNotLoadedError(HomeAssistantError):
    """The gateway's config entry is not loaded (any more)."""

    def __init__(self) -> None:
        """Initialize with the message."""
        super().__init__("The IKEA gateway is not connected")


def _state(hass: HomeAssistant, entry: TradfriConfigEntry) -> _GatewayState:
    if entry.state is not ConfigEntryState.LOADED:
        raise GatewayNotLoadedError
    return hass.data.setdefault(DATA_STATE, {}).setdefault(
        entry.entry_id, _GatewayState()
    )


@contextlib.asynccontextmanager
async def gateway_operation(
    hass: HomeAssistant, entry: TradfriConfigEntry
) -> AsyncIterator[TradfriAdmin]:
    """Talk to one gateway for a management change.

    Changes on a gateway run one at a time, and none starts once its entry
    is unloading: a change that waited for the lock is refused then. Never
    await the entry's unload or reload inside it; unloading waits for it.
    """
    state = _state(hass, entry)
    async with state.lock:
        if state.unloading:
            raise GatewayNotLoadedError
        yield _admin(entry)


async def async_unload_management(
    hass: HomeAssistant, entry: TradfriConfigEntry
) -> None:
    """Stop management work before the entry unloads and its API shuts down.

    Pairing is cancelled. A running change is left to finish, as stopping it
    halfway can leave the gateway half changed (a group emptied but not
    deleted); its requests time out, so this is bounded. Waiting changes are
    refused.
    """
    if (state := hass.data.get(DATA_STATE, {}).get(entry.entry_id)) is None:
        return
    state.unloading = True
    if state.pairing and not state.pairing.done():
        state.pairing.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await state.pairing
    async with state.lock:
        hass.data[DATA_STATE].pop(entry.entry_id, None)


async def _reload(hass: HomeAssistant, entry: TradfriConfigEntry) -> None:
    """Reload the entry so entities follow the change, and wait for it.

    Waiting means the action returns with the gateway loaded again, so a
    script can run the next action straight away. Only call it after leaving
    gateway_operation, as unloading waits for that.

    The reload runs in its own task: a stopped script cancels the action, and
    that must not cancel the unload halfway, which would leave the entry
    stuck unloading.
    """
    reload = hass.async_create_task(
        hass.config_entries.async_reload(entry.entry_id),
        f"tradfri reload {entry.title}",
    )
    try:
        loaded = await asyncio.shield(reload)
    except asyncio.CancelledError:
        # Nobody awaits the reload any more; report how it ends.
        reload.add_done_callback(functools.partial(_log_reload_result, entry))
        raise
    if not loaded or entry.state is not ConfigEntryState.LOADED:
        raise HomeAssistantError(
            "The gateway change was made, but the integration could not"
            " reconnect to the IKEA gateway afterwards"
        )


def _log_reload_result(entry: TradfriConfigEntry, reload: asyncio.Task[bool]) -> None:
    """Log a reload that failed after the action waiting for it was cancelled."""
    if reload.cancelled():
        return
    if (err := reload.exception()) is not None:
        LOGGER.error("Reloading %s failed", entry.title, exc_info=err)
    elif not reload.result():
        LOGGER.warning("Reloading %s failed; the gateway is not loaded", entry.title)


def _loaded_entries(hass: HomeAssistant) -> list[TradfriConfigEntry]:
    return [
        e
        for e in hass.config_entries.async_entries(DOMAIN)
        if e.state is ConfigEntryState.LOADED
    ]


def _entry(hass: HomeAssistant, entry_id: str | None) -> TradfriConfigEntry:
    entries = [
        e for e in _loaded_entries(hass) if entry_id is None or e.entry_id == entry_id
    ]
    if entry_id is not None and not entries:
        raise ServiceValidationError(f"No loaded IKEA gateway with entry id {entry_id}")
    if not entries:
        raise ServiceValidationError("No IKEA gateway is loaded")
    if len(entries) > 1:
        raise ServiceValidationError(
            "Several IKEA gateways are set up; choose one with config_entry_id"
        )
    return entries[0]


def _gateway_device(
    hass: HomeAssistant, ha_device_id: str
) -> tuple[TradfriConfigEntry, int]:
    """Map a Home Assistant device to its gateway entry and gateway device id."""
    if (device := dr.async_get(hass).async_get(ha_device_id)) is None:
        raise ServiceValidationError(f"Unknown device {ha_device_id}")
    for entry in _loaded_entries(hass):
        prefix = f"{entry.data[CONF_GATEWAY_ID]}-"
        for domain, identifier in device.identifiers:
            if domain == DOMAIN and identifier.startswith(prefix):
                if identifier.removeprefix(prefix).startswith("group-"):
                    raise ServiceValidationError(
                        f"{device.name} is a group, not a device"
                    )
                return entry, int(identifier.removeprefix(prefix))
    raise ServiceValidationError(
        f"{device.name} is not a device of a loaded IKEA gateway"
    )


def _gateway_devices(
    hass: HomeAssistant, ha_device_ids: list[str], entry_id: str | None = None
) -> tuple[TradfriConfigEntry, list[int]]:
    """Map Home Assistant devices that must all belong to one gateway."""
    pairs = [_gateway_device(hass, d) for d in ha_device_ids]
    entries = {entry.entry_id for entry, _ in pairs}
    if len(entries) > 1:
        raise ServiceValidationError("The devices belong to different IKEA gateways")
    if entry_id is not None and entries != {entry_id}:
        raise ServiceValidationError(
            "The devices do not belong to the selected IKEA gateway"
        )
    return pairs[0][0], [gateway_id for _, gateway_id in pairs]


def _admin(entry: TradfriConfigEntry) -> TradfriAdmin:
    return TradfriAdmin(entry.runtime_data.api, entry.runtime_data.gateway)


def _not_supergroup(group: Group, action: str) -> None:
    if group.id == SUPERGROUP_ID:
        raise ServiceValidationError(
            f"{group.name} is the gateway's own group of all devices and cannot be {action}"
        )


def _translate_errors[**P, R](
    func: Callable[P, Awaitable[R]],
) -> Callable[P, Awaitable[R]]:
    """Report gateway problems as Home Assistant errors."""

    @functools.wraps(func)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return await func(*args, **kwargs)
        except GroupNotFoundError as err:
            raise ServiceValidationError(
                f"No group named {err} on the gateway"
            ) from err
        except AmbiguousGroupError as err:
            raise ServiceValidationError(
                f"Several groups are named {err}; rename one of them first"
            ) from err
        except (GatewayError, RequestError) as err:
            raise HomeAssistantError(f"The IKEA gateway refused: {err}") from err

    return wrapper


async def _pair(
    admin: TradfriAdmin, seconds: int, group_id: int | None = None
) -> list[Device]:
    """Keep the pairing window open for this long; return the devices that joined.

    With group_id, a joining controller is placed in that group, like the
    IKEA app's add-accessory flow.
    """
    before = {d.id for d in await admin.devices()}
    end = time.monotonic() + seconds
    LOGGER.info("Pairing window open for %s s", seconds)
    try:
        while (left := end - time.monotonic()) > 0:
            await admin.open_pairing(group_id=group_id)
            await asyncio.sleep(min(PAIRING_RENEW_SECONDS, left))
    finally:
        try:
            await admin.close_pairing()
        except RequestError as err:
            LOGGER.warning("Could not close the pairing window: %s", err)
    return [d for d in await admin.devices() if d.id not in before]


def async_setup_services(hass: HomeAssistant) -> None:
    """Register the gateway management actions (administrators only)."""

    @_translate_errors
    async def pair_devices(call: ServiceCall) -> ServiceResponse:
        entry = _entry(hass, call.data.get(CONF_CONFIG_ENTRY_ID))
        group_id: int | None = None
        if (group_name := call.data.get(CONF_GROUP)) is not None:
            async with gateway_operation(hass, entry) as admin:
                group = await admin.get_group(group_name)
                _not_supergroup(group, "used as a pairing target")
                group_id = group.id
        # Re-read the state after the (awaited) lookup: the entry may have
        # been unloaded, or another session started meanwhile. Nothing awaits
        # between the check and the task registration, so it holds.
        state = _state(hass, entry)
        if state.pairing and not state.pairing.done():
            raise ServiceValidationError("Pairing is already open on this gateway")
        state.pairing = entry.async_create_background_task(
            hass,
            _pair(_admin(entry), call.data[CONF_SECONDS], group_id=group_id),
            "tradfri pairing",
        )
        try:
            new = await state.pairing
        except asyncio.CancelledError:
            if (task := asyncio.current_task()) and task.cancelling():
                raise  # The action itself was cancelled.
            raise HomeAssistantError(
                "Pairing stopped because the IKEA gateway integration was unloaded"
            ) from None
        if new:
            await _reload(hass, entry)
        return {
            "new_devices": [
                {"id": d.id, "name": d.name, "model": d.device_info.model_number}
                for d in new
            ]
        }

    @_translate_errors
    async def list_groups(call: ServiceCall) -> ServiceResponse:
        entry = _entry(hass, call.data.get(CONF_CONFIG_ENTRY_ID))
        async with gateway_operation(hass, entry) as admin:
            names = {d.id: d.name for d in await admin.devices()}
            groups = await admin.group_infos()
        return {
            "groups": [
                {
                    "id": g.id,
                    "name": g.name,
                    "members": [names.get(m, str(m)) for m in g.member_ids],
                }
                for g in groups
            ]
        }

    @_translate_errors
    async def rename_device(call: ServiceCall) -> None:
        entry, gateway_id = _gateway_device(hass, call.data[CONF_DEVICE_ID])
        async with gateway_operation(hass, entry) as admin:
            await admin.rename_device(gateway_id, call.data[CONF_NAME])
        # Only the integration's name; a name given in Home Assistant stays.
        dr.async_get(hass).async_update_device(
            call.data[CONF_DEVICE_ID], name=call.data[CONF_NAME]
        )

    @_translate_errors
    async def rename_group(call: ServiceCall) -> None:
        entry = _entry(hass, call.data.get(CONF_CONFIG_ENTRY_ID))
        async with gateway_operation(hass, entry) as admin:
            group = await admin.get_group(call.data[CONF_GROUP])
            _not_supergroup(group, "renamed")
            other = await admin.find_group(call.data[CONF_NAME])
            if other is not None and other.id != group.id:
                raise ServiceValidationError(
                    f"A group named {call.data[CONF_NAME]} already exists"
                )
            await admin.rename_group(group, call.data[CONF_NAME])
        await _reload(hass, entry)

    @_translate_errors
    async def create_group(call: ServiceCall) -> ServiceResponse:
        entry_id = call.data.get(CONF_CONFIG_ENTRY_ID)
        if call.data[CONF_DEVICE_IDS]:
            entry, members = _gateway_devices(
                hass, call.data[CONF_DEVICE_IDS], entry_id
            )
        else:
            entry, members = _entry(hass, entry_id), []
        async with gateway_operation(hass, entry) as admin:
            if await admin.find_group(call.data[CONF_NAME]) is not None:
                raise ServiceValidationError(
                    f"A group named {call.data[CONF_NAME]} already exists"
                )
            group_id = await admin.create_group(call.data[CONF_NAME], members)
        await _reload(hass, entry)
        return {"group_id": group_id}

    @_translate_errors
    async def delete_group(call: ServiceCall) -> None:
        entry = _entry(hass, call.data.get(CONF_CONFIG_ENTRY_ID))
        async with gateway_operation(hass, entry) as admin:
            group = await admin.get_group(call.data[CONF_GROUP])
            _not_supergroup(group, "deleted")
            await admin.delete_group(group)
        await _reload(hass, entry)

    @_translate_errors
    async def add_to_group(call: ServiceCall) -> None:
        entry, members = _gateway_devices(hass, call.data[CONF_DEVICE_IDS])
        async with gateway_operation(hass, entry) as admin:
            group = await admin.get_group(call.data[CONF_GROUP])
            _not_supergroup(group, "changed")
            await admin.add_members(group.id, members)
        await _reload(hass, entry)

    @_translate_errors
    async def remove_from_group(call: ServiceCall) -> None:
        entry, members = _gateway_devices(hass, call.data[CONF_DEVICE_IDS])
        async with gateway_operation(hass, entry) as admin:
            group = await admin.get_group(call.data[CONF_GROUP])
            _not_supergroup(group, "changed")
            await admin.remove_members(group.id, members)
        await _reload(hass, entry)

    @_translate_errors
    async def remove_device(call: ServiceCall) -> None:
        entry, gateway_id = _gateway_device(hass, call.data[CONF_DEVICE_ID])
        async with gateway_operation(hass, entry) as admin:
            await admin.remove_device(gateway_id)
        await _reload(hass, entry)

    handlers: dict[
        str, tuple[Callable[[ServiceCall], Awaitable[Any]], SupportsResponse]
    ] = {
        "pair_devices": (pair_devices, SupportsResponse.OPTIONAL),
        "list_groups": (list_groups, SupportsResponse.ONLY),
        "rename_device": (rename_device, SupportsResponse.NONE),
        "rename_group": (rename_group, SupportsResponse.NONE),
        "create_group": (create_group, SupportsResponse.OPTIONAL),
        "delete_group": (delete_group, SupportsResponse.NONE),
        "add_to_group": (add_to_group, SupportsResponse.NONE),
        "remove_from_group": (remove_from_group, SupportsResponse.NONE),
        "remove_device": (remove_device, SupportsResponse.NONE),
    }
    for name, (handler, response) in handlers.items():
        async_register_admin_service(
            hass,
            DOMAIN,
            name,
            handler,
            schema=SCHEMAS[name],
            supports_response=response,
        )
