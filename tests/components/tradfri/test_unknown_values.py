"""Devices whose control reports no values (pytradfri 15.0.0 parses them)."""

import json

import pytest
from pytest_homeassistant.common import load_fixture
from pytradfri.device import Device

from custom_components.tradfri.const import DOMAIN
from homeassistant.components.cover import ATTR_CURRENT_POSITION, CoverEntityFeature
from homeassistant.components.light import ATTR_SUPPORTED_COLOR_MODES, ColorMode
from homeassistant.const import ATTR_SUPPORTED_FEATURES, STATE_ON, STATE_UNKNOWN
from homeassistant.core import HomeAssistant

from .common import CommandStore, setup_integration


def _without_values(fixture: str, control: str) -> str:
    """Return a device fixture whose control reports no values."""
    response = json.loads(load_fixture(fixture, DOMAIN))
    response[control] = [{"9003": 0}]
    return json.dumps(response)


@pytest.fixture(scope="module")
def bulb_ws_without_values() -> str:
    """Return a white spectrum bulb that reports no values."""
    return _without_values("bulb_ws.json", "3311")


@pytest.fixture(scope="module")
def outlet_without_values() -> str:
    """Return an outlet that reports no values."""
    return _without_values("outlet.json", "3312")


@pytest.fixture(scope="module")
def blind_without_values() -> str:
    """Return a blind that reports no values."""
    return _without_values("blind.json", "15015")


@pytest.fixture(scope="module")
def air_purifier_without_values() -> str:
    """Return an air purifier that reports no values."""
    return _without_values("air_purifier.json", "15025")


@pytest.mark.parametrize("device", ["bulb_ws_without_values"], indirect=True)
async def test_light_without_values(
    hass: HomeAssistant, command_store: CommandStore, device: Device
) -> None:
    """The light is unknown, and gets its color modes once it reports values."""
    await setup_integration(hass)

    state = hass.states.get("light.test_ws")
    assert state
    assert state.state == STATE_UNKNOWN
    assert state.attributes[ATTR_SUPPORTED_COLOR_MODES] == [ColorMode.ONOFF]

    await command_store.trigger_observe_callback(
        hass, device, {"3311": [{"5850": 1, "5851": 200, "5711": 370}]}
    )

    state = hass.states.get("light.test_ws")
    assert state
    assert state.state == STATE_ON
    assert state.attributes[ATTR_SUPPORTED_COLOR_MODES] == [ColorMode.COLOR_TEMP]


@pytest.mark.parametrize("device", ["outlet_without_values"], indirect=True)
async def test_switch_without_values(hass: HomeAssistant, device: Device) -> None:
    """The switch state is unknown."""
    await setup_integration(hass)

    state = hass.states.get("switch.test")
    assert state
    assert state.state == STATE_UNKNOWN


@pytest.mark.parametrize("device", ["blind_without_values"], indirect=True)
async def test_cover_without_values(hass: HomeAssistant, device: Device) -> None:
    """The position is unknown, and setting a position stays supported."""
    await setup_integration(hass)

    state = hass.states.get("cover.test")
    assert state
    assert state.state == STATE_UNKNOWN
    assert ATTR_CURRENT_POSITION not in state.attributes
    assert state.attributes[ATTR_SUPPORTED_FEATURES] & CoverEntityFeature.SET_POSITION


@pytest.mark.parametrize("device", ["air_purifier_without_values"], indirect=True)
async def test_air_purifier_without_values(hass: HomeAssistant, device: Device) -> None:
    """Fan state and filter time are unknown."""
    await setup_integration(hass)

    fan = hass.states.get("fan.test")
    assert fan
    assert fan.state == STATE_UNKNOWN
    filter_left = hass.states.get("sensor.test_filter_time_left")
    assert filter_left
    assert filter_left.state == STATE_UNKNOWN
