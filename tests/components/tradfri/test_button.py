"""Tests for the gateway restart button."""

import asyncio
from typing import Any

import pytest
from pytradfri.error import ClientError

from custom_components.tradfri.services import gateway_operation
from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN, SERVICE_PRESS
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from . import GATEWAY_ID
from .common import CommandStore, setup_integration
from .fake_gateway import FakeGateway


@pytest.fixture(name="fake_gateway")
def fake_gateway_fixture(command_store: CommandStore) -> FakeGateway:
    """Return a gateway that counts reboots."""
    return FakeGateway(command_store)


@pytest.fixture(name="mock_api")
def mock_api_fixture(fake_gateway: FakeGateway) -> Any:
    """Answer the integration's commands with the fake gateway."""
    return fake_gateway.api


def _button(entity_registry: er.EntityRegistry) -> str:
    entity_id = entity_registry.async_get_entity_id(
        BUTTON_DOMAIN, "tradfri", f"{GATEWAY_ID}-restart"
    )
    assert entity_id
    return entity_id


async def test_restart(
    hass: HomeAssistant, entity_registry: er.EntityRegistry, fake_gateway: FakeGateway
) -> None:
    """Pressing the button reboots the gateway."""
    await setup_integration(hass)
    entity_id = _button(entity_registry)
    assert entity_registry.async_get(entity_id).entity_category == "config"

    await hass.services.async_call(
        BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: entity_id}, blocking=True
    )

    assert fake_gateway.reboots == 1


async def test_restart_refused(
    hass: HomeAssistant, entity_registry: er.EntityRegistry, fake_gateway: FakeGateway
) -> None:
    """A refused reboot is reported as an error."""
    await setup_integration(hass)
    fake_gateway.reboot_error = ClientError("Gateway payload: . Error code: 4.03.")

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            BUTTON_DOMAIN,
            SERVICE_PRESS,
            {ATTR_ENTITY_ID: _button(entity_registry)},
            blocking=True,
        )


async def test_restart_waiting_during_unload(
    hass: HomeAssistant, entity_registry: er.EntityRegistry, fake_gateway: FakeGateway
) -> None:
    """A restart still waiting for a running change is refused once unloading."""
    entry = await setup_integration(hass)
    entity_id = _button(entity_registry)

    async with gateway_operation(hass, entry):
        press = asyncio.ensure_future(
            hass.services.async_call(
                BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: entity_id}, blocking=True
            )
        )
        for _ in range(50):
            await asyncio.sleep(0)
        unload = asyncio.ensure_future(hass.config_entries.async_unload(entry.entry_id))
        for _ in range(50):
            await asyncio.sleep(0)
        assert not unload.done()

    with pytest.raises(HomeAssistantError, match="not connected"):
        await press
    assert await unload
    assert fake_gateway.reboots == 0
