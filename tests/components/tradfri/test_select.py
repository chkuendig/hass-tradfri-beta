"""Tradfri power-on behavior select tests."""

from typing import Any

import pytest
from pytest_homeassistant.common import load_fixture
from pytradfri.command import Command
from pytradfri.const import ATTR_REACHABLE_STATE
from pytradfri.device import Device
from pytradfri.error import ClientError

from custom_components.tradfri.const import DOMAIN
from homeassistant.components.select import (
    ATTR_OPTION,
    ATTR_OPTIONS,
    DOMAIN as SELECT_DOMAIN,
    SERVICE_SELECT_OPTION,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    EntityCategory,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er

from .common import CommandStore, setup_integration

ENTITY_ID = "select.test_power_on_behavior"


@pytest.fixture(scope="module")
def relay() -> str:
    """Return a third-party relay that reports its power-on behavior."""
    return load_fixture("relay.json", DOMAIN)


@pytest.fixture(scope="module")
def bulb_w() -> str:
    """Return a bulb W response, without a power-on behavior."""
    return load_fixture("bulb_w.json", DOMAIN)


@pytest.fixture(scope="module")
def outlet() -> str:
    """Return an outlet response."""
    return load_fixture("outlet.json", DOMAIN)


@pytest.fixture(scope="module")
def remote_control() -> str:
    """Return a remote control response."""
    return load_fixture("remote_control.json", DOMAIN)


async def _select(hass: HomeAssistant, option: str) -> None:
    await hass.services.async_call(
        SELECT_DOMAIN,
        SERVICE_SELECT_OPTION,
        {ATTR_ENTITY_ID: ENTITY_ID, ATTR_OPTION: option},
        blocking=True,
    )


def _writes(command_store: CommandStore) -> list[dict[str, Any] | None]:
    return [c.data for c in command_store.sent_commands if c.method == "put"]


@pytest.mark.parametrize("device", ["relay"], indirect=True)
async def test_state_from_device(
    hass: HomeAssistant, entity_registry: er.EntityRegistry, device: Device
) -> None:
    """The reported value is shown, as a configuration entity."""
    await setup_integration(hass)

    state = hass.states.get(ENTITY_ID)
    assert state
    assert state.state == "toggle"
    assert state.attributes[ATTR_OPTIONS] == ["off", "on", "toggle", "previous"]
    entry = entity_registry.async_get(ENTITY_ID)
    assert entry
    assert entry.entity_category is EntityCategory.CONFIG
    assert entry.unique_id == "mock-gateway-id-65567-power_on_behavior"


@pytest.mark.parametrize("device", ["relay"], indirect=True)
@pytest.mark.parametrize(
    ("value", "expected"),
    [(1, "off"), (2, "on"), (4, "previous"), (0, STATE_UNKNOWN), (5, STATE_UNKNOWN)],
)
async def test_observed_values(
    hass: HomeAssistant,
    command_store: CommandStore,
    device: Device,
    value: int,
    expected: str,
) -> None:
    """Values arriving by observation; 0 and 5 mean the gateway does not know."""
    await setup_integration(hass)

    await command_store.trigger_observe_callback(
        hass, device, {"3311": [{"5849": value}]}
    )

    state = hass.states.get(ENTITY_ID)
    assert state
    assert state.state == expected


@pytest.mark.parametrize("device", ["bulb_w"], indirect=True)
async def test_unknown_when_not_reported(
    hass: HomeAssistant, device: Device
) -> None:
    """A light without a value still gets the setting, as unknown."""
    await setup_integration(hass)

    state = hass.states.get("select.test_w_power_on_behavior")
    assert state
    assert state.state == STATE_UNKNOWN


@pytest.mark.parametrize("device", ["remote_control"], indirect=True)
async def test_no_select_for_controllers(
    hass: HomeAssistant, device: Device
) -> None:
    """Remotes have no output, so no power-on behavior."""
    await setup_integration(hass)

    assert not hass.states.async_entity_ids(SELECT_DOMAIN)


@pytest.mark.parametrize("device", ["relay"], indirect=True)
@pytest.mark.parametrize(
    ("option", "value"), [("off", 1), ("on", 2), ("toggle", 3), ("previous", 4)]
)
async def test_select_light(
    hass: HomeAssistant,
    command_store: CommandStore,
    device: Device,
    option: str,
    value: int,
) -> None:
    """Selecting writes the gateway value to the light, then shows the echo."""
    await setup_integration(hass)

    await _select(hass, option)

    assert _writes(command_store) == [{"3311": [{"5849": value}]}]
    assert command_store.sent_commands[-1].path == ["15001", "65567"]
    await command_store.trigger_observe_callback(hass, device)
    state = hass.states.get(ENTITY_ID)
    assert state
    assert state.state == option


@pytest.mark.parametrize("device", ["outlet"], indirect=True)
async def test_select_outlet(
    hass: HomeAssistant, command_store: CommandStore, device: Device
) -> None:
    """Outlets carry the setting in their socket resource."""
    await setup_integration(hass)
    state = hass.states.get(ENTITY_ID)
    assert state
    assert state.state == STATE_UNKNOWN

    await _select(hass, "previous")

    assert _writes(command_store) == [{"3312": [{"5849": 4}]}]
    await command_store.trigger_observe_callback(hass, device)
    state = hass.states.get(ENTITY_ID)
    assert state
    assert state.state == "previous"


@pytest.mark.parametrize("device", ["relay"], indirect=True)
async def test_rejected_option(
    hass: HomeAssistant, command_store: CommandStore, device: Device
) -> None:
    """Options outside the list are refused before anything is sent."""
    await setup_integration(hass)

    with pytest.raises(ServiceValidationError):
        await _select(hass, "always")

    assert _writes(command_store) == []


@pytest.mark.parametrize("device", ["relay"], indirect=True)
async def test_gateway_error(
    hass: HomeAssistant, mock_api_factory: Any, device: Device
) -> None:
    """A refused write is reported to the caller, and the state is kept."""
    api = mock_api_factory.request

    async def failing(command: Command | list[Command], timeout: float | None = None):
        if not isinstance(command, list) and command.method == "put":
            raise ClientError("4.00 Bad Request")
        return await api(command, timeout)

    mock_api_factory.request = failing
    await setup_integration(hass)

    with pytest.raises(HomeAssistantError, match="did not accept"):
        await _select(hass, "previous")

    state = hass.states.get(ENTITY_ID)
    assert state
    assert state.state == "toggle"


@pytest.mark.parametrize("device", ["relay"], indirect=True)
async def test_unavailable_when_unreachable(
    hass: HomeAssistant, command_store: CommandStore, device: Device
) -> None:
    """The setting follows the device's reachability."""
    await setup_integration(hass)

    await command_store.trigger_observe_callback(
        hass, device, {ATTR_REACHABLE_STATE: 0}
    )

    state = hass.states.get(ENTITY_ID)
    assert state
    assert state.state == STATE_UNAVAILABLE
