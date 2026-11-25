"""Tests for gateway groups exposed as lights."""

import json
from typing import Any

import pytest
from pytest_homeassistant.common import load_fixture
from pytradfri.device import Device
from pytradfri.error import ClientError

from custom_components.tradfri import async_remove_config_entry_device
from custom_components.tradfri.const import DOMAIN
from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_SUPPORTED_COLOR_MODES,
    ATTR_TRANSITION,
    DOMAIN as LIGHT_DOMAIN,
    ColorMode,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er

from . import GATEWAY_ID
from .common import CommandStore, setup_integration
from .fake_gateway import SUPERGROUP_ID, FakeGateway

BULB_ID = 65537
REMOTE_ID = 65536
LIVING_ID = 131075
EMPTY_ID = 131076
REMOTE_GROUP_ID = 131100
LIVING = "light.living"


@pytest.fixture(scope="module")
def bulb_w() -> str:
    """Return a bulb W response."""
    return load_fixture("bulb_w.json", DOMAIN)


@pytest.fixture(name="fake_gateway")
def fake_gateway_fixture(command_store: CommandStore) -> FakeGateway:
    """A room with the bulb, an empty room and a remote's own group."""
    gateway = FakeGateway(command_store)
    gateway.add_group(SUPERGROUP_ID, "SuperGroup", [BULB_ID, REMOTE_ID])
    gateway.add_group(LIVING_ID, "Living", [BULB_ID])
    gateway.groups[LIVING_ID].update({"5850": 1, "5851": 200})
    gateway.add_group(EMPTY_ID, "Empty")
    gateway.add_group(REMOTE_GROUP_ID, "TRADFRI GROUP 8", [REMOTE_ID])
    return gateway


@pytest.fixture(name="mock_api")
def mock_api_fixture(fake_gateway: FakeGateway) -> Any:
    """Answer the integration's commands with the fake gateway."""
    return fake_gateway.api


def _group_writes(command_store: CommandStore, group_id: int) -> list[Any]:
    return [
        c.data
        for c in command_store.sent_commands
        if c.method == "put" and c.path == ["15004", str(group_id)]
    ]


async def _light(hass: HomeAssistant, service: str, **data: Any) -> None:
    await hass.services.async_call(
        LIGHT_DOMAIN, service, {ATTR_ENTITY_ID: LIVING, **data}, blocking=True
    )


@pytest.mark.parametrize("device", ["bulb_w"], indirect=True)
async def test_only_rooms_with_lights(
    hass: HomeAssistant, entity_registry: er.EntityRegistry, device: Device
) -> None:
    """Only groups holding a light become lights; not the SuperGroup."""
    await setup_integration(hass)

    group_entities = [
        e.entity_id
        for e in entity_registry.entities.values()
        if e.unique_id.startswith("group-")
    ]
    assert group_entities == [LIVING]
    state = hass.states.get(LIVING)
    assert state
    assert state.state == STATE_ON
    assert state.attributes[ATTR_BRIGHTNESS] == 200
    entry = entity_registry.async_get(LIVING)
    assert entry
    assert entry.unique_id == f"group-{GATEWAY_ID}-{LIVING_ID}"


@pytest.mark.parametrize("device", ["bulb_w"], indirect=True)
@pytest.mark.parametrize(
    ("service", "data", "expected"),
    [
        (SERVICE_TURN_ON, {}, {"5850": 1}),
        (SERVICE_TURN_ON, {ATTR_BRIGHTNESS: 128}, {"5851": 128}),
        (SERVICE_TURN_ON, {ATTR_BRIGHTNESS: 255}, {"5851": 254}),
        (
            SERVICE_TURN_ON,
            {ATTR_BRIGHTNESS: 100, ATTR_TRANSITION: 2},
            {"5851": 100, "5712": 20},
        ),
        # Fading in without a level keeps the group's level (200 here).
        (SERVICE_TURN_ON, {ATTR_TRANSITION: 2}, {"5851": 200, "5712": 20}),
        (SERVICE_TURN_OFF, {}, {"5850": 0}),
        (SERVICE_TURN_OFF, {ATTR_TRANSITION: 1}, {"5851": 0, "5712": 10}),
    ],
)
async def test_control(
    hass: HomeAssistant,
    command_store: CommandStore,
    device: Device,
    service: str,
    data: dict[str, Any],
    expected: dict[str, int],
) -> None:
    """Commands go to the group resource, not to each bulb."""
    await setup_integration(hass)

    await _light(hass, service, **data)

    assert _group_writes(command_store, LIVING_ID) == [expected]


@pytest.mark.parametrize("device", ["bulb_w"], indirect=True)
async def test_state_follows_observation(
    hass: HomeAssistant,
    command_store: CommandStore,
    fake_gateway: FakeGateway,
    device: Device,
) -> None:
    """The group light shows what the gateway reports for the group."""
    await setup_integration(hass)
    observe = next(
        c
        for c in command_store.sent_commands
        if c.observe and c.path == ["15004", str(LIVING_ID)]
    )

    observe.process_result({**fake_gateway.groups[LIVING_ID], "5850": 0})
    await hass.async_block_till_done()

    state = hass.states.get(LIVING)
    assert state
    assert state.state == STATE_OFF


@pytest.mark.parametrize("device", ["bulb_w"], indirect=True)
async def test_group_device_not_removable_from_page(
    hass: HomeAssistant, device_registry: dr.DeviceRegistry, device: Device
) -> None:
    """A group's device entry points to the delete_group action instead."""
    entry = await setup_integration(hass)
    (group_device,) = device_registry.async_get_devices(
        identifiers={(DOMAIN, f"{GATEWAY_ID}-group-{LIVING_ID}")}
    )

    with pytest.raises(HomeAssistantError, match="delete_group"):
        await async_remove_config_entry_device(hass, entry, group_device)


@pytest.mark.parametrize("device", ["bulb_w"], indirect=True)
async def test_group_light_follows_topology(
    hass: HomeAssistant, fake_gateway: FakeGateway, device: Device
) -> None:
    """Adding the bulb to a room makes that room a light once the action returns."""
    await setup_integration(hass)
    assert hass.states.get("light.empty") is None

    await hass.services.async_call(
        DOMAIN,
        "add_to_group",
        {
            "group": "Empty",
            "device_ids": [
                device.id
                for device in dr.async_get(hass).async_get_devices(
                    identifiers={(DOMAIN, f"{GATEWAY_ID}-{BULB_ID}")}
                )
            ],
        },
        blocking=True,
    )

    assert fake_gateway.members(EMPTY_ID) == [BULB_ID]
    assert hass.states.get("light.empty")


RELAY_ID = 65567


@pytest.fixture(scope="module")
def relay() -> str:
    """Return a third-party on/off relay that the gateway lists as a light."""
    return load_fixture("relay.json", DOMAIN)


def _add_device(command_store: CommandStore, raw: str) -> None:
    """Register one more device next to the parametrized one."""
    response = json.loads(raw)
    ids = command_store.mock_responses["15001"]
    command_store.mock_responses["15001"] = [*ids, response["9003"]]
    command_store.mock_responses[f"15001/{response['9003']}"] = response


@pytest.mark.parametrize("device", ["relay"], indirect=True)
async def test_relay_only_group_is_on_off(
    hass: HomeAssistant,
    command_store: CommandStore,
    fake_gateway: FakeGateway,
    device: Device,
) -> None:
    """A group of relays offers no brightness and sends plain on/off."""
    fake_gateway.add_group(LIVING_ID, "Living", [RELAY_ID])
    await setup_integration(hass)

    state = hass.states.get(LIVING)
    assert state
    assert state.attributes[ATTR_SUPPORTED_COLOR_MODES] == [ColorMode.ONOFF]
    assert ATTR_BRIGHTNESS not in state.attributes

    await _light(hass, SERVICE_TURN_ON)
    await _light(hass, SERVICE_TURN_OFF, **{ATTR_TRANSITION: 1})

    assert _group_writes(command_store, LIVING_ID) == [{"5850": 1}, {"5850": 0}]


@pytest.mark.parametrize("device", ["bulb_w"], indirect=True)
async def test_mixed_group_dims(
    hass: HomeAssistant,
    command_store: CommandStore,
    fake_gateway: FakeGateway,
    device: Device,
    relay: str,
) -> None:
    """One dimmable member is enough for the group to dim."""
    _add_device(command_store, relay)
    fake_gateway.add_group(LIVING_ID, "Living", [BULB_ID, RELAY_ID])
    await setup_integration(hass)

    state = hass.states.get(LIVING)
    assert state
    assert state.attributes[ATTR_SUPPORTED_COLOR_MODES] == [ColorMode.BRIGHTNESS]


@pytest.mark.parametrize("device", ["bulb_w"], indirect=True)
async def test_group_observe_failure_shuts_factory(
    hass: HomeAssistant, mock_api_factory: Any, fake_gateway: FakeGateway, device: Device
) -> None:
    """A group gone between listing and observing retries setup, cleanly."""
    api = fake_gateway.api

    async def deleted_meanwhile(command: Any, timeout: float | None = None) -> Any:
        if not isinstance(command, list) and command.observe and command.path == [
            "15004",
            str(LIVING_ID),
        ]:
            raise ClientError("Gateway payload: . Error code: 4.04 Not Found.")
        return await api(command, timeout)

    mock_api_factory.request = deleted_meanwhile
    entry = await setup_integration(hass)

    assert entry.state is ConfigEntryState.SETUP_RETRY
    mock_api_factory.shutdown.assert_awaited()


@pytest.mark.parametrize("device", ["bulb_w"], indirect=True)
async def test_group_device_under_gateway(
    hass: HomeAssistant, device_registry: dr.DeviceRegistry, device: Device
) -> None:
    """A group's device entry is linked to the gateway's."""
    await setup_integration(hass)
    (gateway,) = device_registry.async_get_devices(identifiers={(DOMAIN, GATEWAY_ID)})
    (group_device,) = device_registry.async_get_devices(
        identifiers={(DOMAIN, f"{GATEWAY_ID}-group-{LIVING_ID}")}
    )

    assert group_device.via_device_id == gateway.id
