"""Tests for the gateway management actions."""

import asyncio
from itertools import count
import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from pytest_homeassistant.common import MockConfigEntry, load_fixture
from pytradfri.const import ATTR_NAME, ROOT_DEVICES
from pytradfri.device import Device
from pytradfri.gateway import Gateway
import voluptuous as vol

from custom_components.tradfri.const import DOMAIN
from homeassistant.auth.models import User
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import (
    HomeAssistantError,
    ServiceValidationError,
    Unauthorized,
)
from homeassistant.helpers import device_registry as dr

from . import GATEWAY_ID
from .common import CommandStore, setup_integration
from .fake_gateway import SUPERGROUP_ID, FakeGateway

OUTLET_ID = 65548
BULB_ID = 65537
LIVING_ID = 131075


@pytest.fixture(scope="module")
def outlet() -> str:
    """Return an outlet response."""
    return load_fixture("outlet.json", DOMAIN)


@pytest.fixture(name="fake_gateway")
def fake_gateway_fixture(command_store: CommandStore) -> FakeGateway:
    """Return a gateway with its SuperGroup and one room."""
    gateway = FakeGateway(command_store)
    gateway.add_group(SUPERGROUP_ID, "SuperGroup", [OUTLET_ID])
    gateway.add_group(LIVING_ID, "Living")
    return gateway


@pytest.fixture(name="mock_api")
def mock_api_fixture(fake_gateway: FakeGateway) -> Any:
    """Answer the integration's commands with the fake gateway."""
    return fake_gateway.api


def _device_id(hass: HomeAssistant, gateway_device_id: int = OUTLET_ID) -> str:
    (device,) = dr.async_get(hass).async_get_devices(
        identifiers={(DOMAIN, f"{GATEWAY_ID}-{gateway_device_id}")}
    )
    return device.id


async def _call(
    hass: HomeAssistant,
    service: str,
    data: dict[str, Any],
    *,
    user: User | None = None,
    response: bool = False,
) -> Any:
    return await hass.services.async_call(
        DOMAIN,
        service,
        data,
        blocking=True,
        context=Context(user_id=user.id) if user else None,
        return_response=response,
    )


@pytest.mark.parametrize("device", ["outlet"], indirect=True)
@pytest.mark.parametrize(
    "service",
    [
        "pair_devices",
        "list_groups",
        "rename_device",
        "rename_group",
        "create_group",
        "delete_group",
        "add_to_group",
        "remove_from_group",
        "remove_device",
    ],
)
async def test_actions_require_admin(
    hass: HomeAssistant,
    hass_read_only_user: User,
    fake_gateway: FakeGateway,
    device: Device,
    service: str,
) -> None:
    """Non-admin users cannot run the management actions."""
    await setup_integration(hass)
    device_id = _device_id(hass)
    data = {
        "pair_devices": {},
        "list_groups": {},
        "rename_device": {"device_id": device_id, "name": "New"},
        "rename_group": {"group": "Living", "name": "New"},
        "create_group": {"name": "New"},
        "delete_group": {"group": "Living"},
        "add_to_group": {"group": "Living", "device_ids": [device_id]},
        "remove_from_group": {"group": "Living", "device_ids": [device_id]},
        "remove_device": {"device_id": device_id},
    }[service]
    groups_before = json.dumps(fake_gateway.groups, sort_keys=True)

    with pytest.raises(Unauthorized):
        await _call(
            hass,
            service,
            data,
            user=hass_read_only_user,
            response=service == "list_groups",
        )

    assert json.dumps(fake_gateway.groups, sort_keys=True) == groups_before
    assert not fake_gateway.removed_devices
    assert not fake_gateway.commissioning


@pytest.mark.parametrize("device", ["outlet"], indirect=True)
async def test_list_groups(hass: HomeAssistant, device: Device) -> None:
    """Groups are listed by name with their members."""
    await setup_integration(hass)

    result = await _call(hass, "list_groups", {}, response=True)

    assert result == {
        "groups": [
            {"id": LIVING_ID, "name": "Living", "members": []},
            {"id": SUPERGROUP_ID, "name": "SuperGroup", "members": ["Test"]},
        ]
    }


@pytest.mark.parametrize("device", ["outlet"], indirect=True)
async def test_create_group(
    hass: HomeAssistant, fake_gateway: FakeGateway, device: Device
) -> None:
    """A group is created and its members are added afterwards."""
    await setup_integration(hass)

    result = await _call(
        hass,
        "create_group",
        {"name": " Hallway ", "device_ids": [_device_id(hass)]},
        response=True,
    )

    assert result == {"group_id": 131100}
    assert fake_gateway.name(131100) == "Hallway"
    assert fake_gateway.members(131100) == [OUTLET_ID]


@pytest.mark.parametrize("device", ["outlet"], indirect=True)
async def test_create_group_existing_name(
    hass: HomeAssistant, fake_gateway: FakeGateway, device: Device
) -> None:
    """Group names are unique, compared without case and surrounding spaces."""
    await setup_integration(hass)

    with pytest.raises(ServiceValidationError):
        await _call(hass, "create_group", {"name": " living"})

    assert set(fake_gateway.groups) == {SUPERGROUP_ID, LIVING_ID}


@pytest.mark.parametrize("device", ["outlet"], indirect=True)
async def test_create_group_devices_of_other_gateway(
    hass: HomeAssistant, fake_gateway: FakeGateway, device: Device
) -> None:
    """The devices must belong to the selected gateway."""
    await setup_integration(hass)
    other = MockConfigEntry(domain=DOMAIN, data={"gateway_id": "other"})
    other.add_to_hass(hass)

    with pytest.raises(ServiceValidationError):
        await _call(
            hass,
            "create_group",
            {
                "name": "Hallway",
                "device_ids": [_device_id(hass)],
                "config_entry_id": other.entry_id,
            },
        )

    assert set(fake_gateway.groups) == {SUPERGROUP_ID, LIVING_ID}


@pytest.mark.parametrize("device", ["outlet"], indirect=True)
@pytest.mark.parametrize(
    ("service", "data"),
    [
        ("create_group", {"name": "   "}),
        ("rename_group", {"group": "Living", "name": ""}),
        ("add_to_group", {"group": "Living", "device_ids": []}),
        ("remove_from_group", {"group": "Living", "device_ids": []}),
    ],
)
async def test_invalid_input(
    hass: HomeAssistant,
    fake_gateway: FakeGateway,
    device: Device,
    service: str,
    data: dict[str, Any],
) -> None:
    """Blank names and empty device lists are rejected."""
    await setup_integration(hass)

    with pytest.raises((vol.Invalid, ServiceValidationError)):
        await _call(hass, service, data)

    assert fake_gateway.name(LIVING_ID) == "Living"


@pytest.mark.parametrize("device", ["outlet"], indirect=True)
async def test_delete_group_with_members(
    hass: HomeAssistant, fake_gateway: FakeGateway, device: Device
) -> None:
    """A group is emptied before it is deleted; the gateway refuses otherwise."""
    fake_gateway.add_group(131076, "Hallway", [OUTLET_ID])
    await setup_integration(hass)

    await _call(hass, "delete_group", {"group": "hallway"})

    assert 131076 not in fake_gateway.groups
    assert fake_gateway.members(SUPERGROUP_ID) == [OUTLET_ID]


@pytest.mark.parametrize("device", ["outlet"], indirect=True)
async def test_group_actions_right_after_delete(
    hass: HomeAssistant, fake_gateway: FakeGateway, device: Device
) -> None:
    """The gateway still lists a deleted group once; listings skip it."""
    await setup_integration(hass)
    await _call(hass, "delete_group", {"group": "Living"})
    assert fake_gateway.stale == {LIVING_ID}

    result = await _call(hass, "list_groups", {}, response=True)

    assert [group["name"] for group in result["groups"]] == ["SuperGroup"]


@pytest.mark.parametrize("device", ["outlet"], indirect=True)
@pytest.mark.parametrize(
    ("service", "data"),
    [
        ("delete_group", {"group": "All devices"}),
        ("rename_group", {"group": "All devices", "name": "Everything"}),
        ("add_to_group", {"group": "All devices", "device_ids": ["DEVICE"]}),
        ("remove_from_group", {"group": "All devices", "device_ids": ["DEVICE"]}),
    ],
)
async def test_supergroup_protected_by_id(
    hass: HomeAssistant,
    fake_gateway: FakeGateway,
    device: Device,
    service: str,
    data: dict[str, Any],
) -> None:
    """The gateway's own group stays protected under another name."""
    fake_gateway.groups[SUPERGROUP_ID]["9001"] = "All devices"
    await setup_integration(hass)
    if "device_ids" in data:
        data = {**data, "device_ids": [_device_id(hass)]}

    with pytest.raises(ServiceValidationError):
        await _call(hass, service, data)

    assert fake_gateway.name(SUPERGROUP_ID) == "All devices"
    assert fake_gateway.members(SUPERGROUP_ID) == [OUTLET_ID]


@pytest.mark.parametrize("device", ["outlet"], indirect=True)
async def test_ambiguous_group_name(
    hass: HomeAssistant, fake_gateway: FakeGateway, device: Device
) -> None:
    """With two groups of the same name, nothing is changed."""
    fake_gateway.add_group(131076, "living ")
    await setup_integration(hass)

    with pytest.raises(ServiceValidationError):
        await _call(hass, "delete_group", {"group": "Living"})

    assert {LIVING_ID, 131076} <= set(fake_gateway.groups)


@pytest.mark.parametrize("device", ["outlet"], indirect=True)
async def test_rename_group_to_existing_name(
    hass: HomeAssistant, fake_gateway: FakeGateway, device: Device
) -> None:
    """A group cannot take another group's name."""
    fake_gateway.add_group(131076, "Kitchen")
    await setup_integration(hass)

    with pytest.raises(ServiceValidationError):
        await _call(hass, "rename_group", {"group": "Living", "name": "kitchen"})

    await _call(hass, "rename_group", {"group": "Living", "name": "Living Room"})
    assert fake_gateway.name(LIVING_ID) == "Living Room"


@pytest.mark.parametrize("device", ["outlet"], indirect=True)
async def test_rename_device(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    command_store: CommandStore,
    device: Device,
) -> None:
    """The gateway name changes; a name given in Home Assistant is kept."""
    await setup_integration(hass)
    device_id = _device_id(hass)
    device_registry.async_update_device(device_id, name_by_user="My plug")

    await _call(hass, "rename_device", {"device_id": device_id, "name": "Desk"})

    rename = command_store.sent_commands[-1]
    assert rename.method == "put"
    assert rename.path_str == f"{ROOT_DEVICES}/{OUTLET_ID}"
    assert rename.data == {ATTR_NAME: "Desk"}
    entry = device_registry.async_get(device_id)
    assert entry
    assert entry.name == "Desk"
    assert entry.name_by_user == "My plug"


@pytest.mark.parametrize("device", ["outlet"], indirect=True)
async def test_remove_device(
    hass: HomeAssistant, fake_gateway: FakeGateway, device: Device
) -> None:
    """A device is unpaired and the entry reloaded."""
    entry = await setup_integration(hass)

    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        await _call(hass, "remove_device", {"device_id": _device_id(hass)})

    assert fake_gateway.removed_devices == [OUTLET_ID]
    reload.assert_called_once_with(entry.entry_id)


@pytest.mark.parametrize("device", ["outlet"], indirect=True)
async def test_pair_devices(
    hass: HomeAssistant,
    command_store: CommandStore,
    fake_gateway: FakeGateway,
    mock_gateway: Gateway,
    device: Device,
) -> None:
    """The window is kept open and closed, and devices that joined are returned."""
    entry = await setup_integration(hass)
    bulb = json.loads(load_fixture("bulb_w.json", DOMAIN))

    def join(timeout: int) -> None:
        if timeout:
            command_store.register_response(
                mock_gateway.get_devices(), [OUTLET_ID, BULB_ID]
            )
            command_store.register_response(mock_gateway.get_device(BULB_ID), bulb)

    fake_gateway.on_commissioning = join
    clock = MagicMock()
    clock.monotonic.side_effect = count(0, 10)

    with (
        patch("custom_components.tradfri.services.PAIRING_RENEW_SECONDS", 0),
        patch("custom_components.tradfri.services.time", clock),
        patch.object(hass.config_entries, "async_schedule_reload") as reload,
    ):
        result = await _call(hass, "pair_devices", {"seconds": 30}, response=True)

    assert result == {
        "new_devices": [
            {"id": BULB_ID, "name": "Test W", "model": "TRADFRI bulb E27 W opal 1000lm"}
        ]
    }
    assert fake_gateway.commissioning == [60, 60, 0]
    reload.assert_called_once_with(entry.entry_id)


@pytest.mark.parametrize("device", ["outlet"], indirect=True)
async def test_pair_devices_one_session_and_unload(
    hass: HomeAssistant, fake_gateway: FakeGateway, device: Device
) -> None:
    """A second session is refused, and unloading stops pairing."""
    entry = await setup_integration(hass)
    first = asyncio.ensure_future(_call(hass, "pair_devices", {"seconds": 600}))
    for _ in range(100):
        if fake_gateway.commissioning:
            break
        await asyncio.sleep(0)
    assert fake_gateway.commissioning == [60]

    with pytest.raises(ServiceValidationError):
        await _call(hass, "pair_devices", {"seconds": 60})

    await hass.config_entries.async_unload(entry.entry_id)

    with pytest.raises(HomeAssistantError, match="unloaded"):
        await first
    assert fake_gateway.commissioning[-1] == 0


@pytest.mark.parametrize("device", ["outlet"], indirect=True)
async def test_create_group_shows_up_late(
    hass: HomeAssistant, fake_gateway: FakeGateway, device: Device
) -> None:
    """A new group the gateway lists only after a while is still found."""
    await setup_integration(hass)
    fake_gateway.created_visible_after = 3

    with patch("custom_components.tradfri.admin._POLL_INTERVAL", 0):
        result = await _call(
            hass,
            "create_group",
            {"name": "Kitchen", "device_ids": [_device_id(hass)]},
            response=True,
        )

    assert fake_gateway.name(result["group_id"]) == "Kitchen"
    assert fake_gateway.members(result["group_id"]) == [OUTLET_ID]


@pytest.mark.parametrize("device", ["outlet"], indirect=True)
async def test_create_group_never_shows_up(
    hass: HomeAssistant, fake_gateway: FakeGateway, device: Device
) -> None:
    """A group the gateway does not create is reported, not returned."""
    await setup_integration(hass)
    fake_gateway.ignore_creates = True

    with (
        patch("custom_components.tradfri.admin._POLL_INTERVAL", 0),
        pytest.raises(HomeAssistantError, match="did not create"),
    ):
        await _call(hass, "create_group", {"name": "Kitchen"}, response=True)


@pytest.mark.parametrize("device", ["outlet"], indirect=True)
async def test_unload_lets_running_change_finish(
    hass: HomeAssistant, fake_gateway: FakeGateway, device: Device
) -> None:
    """Unloading waits for the running change and refuses the waiting ones."""
    entry = await setup_integration(hass)
    fake_gateway.hold = asyncio.Event()
    rename = asyncio.ensure_future(
        _call(hass, "rename_group", {"group": "Living", "name": "Lounge"})
    )
    await asyncio.wait_for(fake_gateway.held.wait(), 5)
    delete = asyncio.ensure_future(_call(hass, "delete_group", {"group": "Lounge"}))
    unload = asyncio.ensure_future(hass.config_entries.async_unload(entry.entry_id))
    for _ in range(50):
        await asyncio.sleep(0)
    assert not unload.done()

    fake_gateway.hold.set()
    await rename
    with pytest.raises(HomeAssistantError, match="not connected"):
        await delete
    assert await unload
    assert fake_gateway.name(LIVING_ID) == "Lounge"


@pytest.mark.parametrize("device", ["outlet"], indirect=True)
async def test_no_gateway_loaded(hass: HomeAssistant, device: Device) -> None:
    """Without a loaded gateway the actions say so."""
    entry = await setup_integration(hass)
    await hass.config_entries.async_unload(entry.entry_id)

    with pytest.raises(ServiceValidationError, match="No IKEA gateway is loaded"):
        await _call(hass, "list_groups", {}, response=True)
