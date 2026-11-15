"""Tests for the last seen diagnostic sensor."""

import pytest
from pytest_homeassistant.common import load_fixture
from pytradfri.const import ATTR_REACHABLE_STATE
from pytradfri.device import Device

from custom_components.tradfri.const import DOMAIN
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .common import CommandStore, setup_integration


@pytest.fixture(scope="module")
def outlet() -> str:
    """Return an outlet response."""
    return load_fixture("outlet.json", DOMAIN)


@pytest.mark.parametrize("device", ["outlet"], indirect=True)
async def test_last_seen(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    command_store: CommandStore,
    device: Device,
) -> None:
    """The sensor shows the gateway's last seen time, also while unreachable."""
    await setup_integration(hass)
    entity_id = "sensor.test_last_seen"

    state = hass.states.get(entity_id)
    assert state
    assert state.state == "2018-09-14T23:38:00+00:00"
    entry = entity_registry.async_get(entity_id)
    assert entry
    assert entry.entity_category is EntityCategory.DIAGNOSTIC

    await command_store.trigger_observe_callback(
        hass, device, {ATTR_REACHABLE_STATE: 0, "9020": 1536970000}
    )

    state = hass.states.get(entity_id)
    assert state
    assert state.state == "2018-09-15T00:06:40+00:00"
    assert hass.states.get("switch.test").state == "unavailable"
