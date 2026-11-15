"""Tests for deleting devices from the device page."""

import asyncio
from typing import Any
from unittest.mock import patch

import pytest
from pytest_homeassistant.common import load_fixture
from pytradfri.const import ATTR_REACHABLE_STATE
from pytradfri.device import Device
from pytradfri.gateway import Gateway

from custom_components.tradfri import async_remove_config_entry_device
from custom_components.tradfri.const import DOMAIN
from custom_components.tradfri.services import gateway_operation
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr

from . import GATEWAY_ID
from .common import CommandStore, setup_integration
from .fake_gateway import FakeGateway

OUTLET_ID = 65548


@pytest.fixture(scope="module")
def outlet() -> str:
    """Return an outlet response."""
    return load_fixture("outlet.json", DOMAIN)


@pytest.fixture(name="fake_gateway")
def fake_gateway_fixture(command_store: CommandStore) -> FakeGateway:
    """Return a gateway that records removed devices."""
    return FakeGateway(command_store)


@pytest.fixture(name="mock_api")
def mock_api_fixture(fake_gateway: FakeGateway) -> Any:
    """Answer the integration's commands with the fake gateway."""
    return fake_gateway.api


def _device(hass: HomeAssistant, identifier: str) -> dr.DeviceEntry:
    (device,) = dr.async_get(hass).async_get_devices(identifiers={(DOMAIN, identifier)})
    return device


@pytest.mark.parametrize("device", ["outlet"], indirect=True)
async def test_gateway_cannot_be_deleted(hass: HomeAssistant, device: Device) -> None:
    """The gateway device itself is not deleted."""
    entry = await setup_integration(hass)

    assert not await async_remove_config_entry_device(
        hass, entry, _device(hass, GATEWAY_ID)
    )


@pytest.mark.parametrize("device", ["outlet"], indirect=True)
async def test_reachable_device_is_kept(
    hass: HomeAssistant, fake_gateway: FakeGateway, device: Device
) -> None:
    """A reachable device must be removed with the action."""
    entry = await setup_integration(hass)

    with pytest.raises(HomeAssistantError, match="remove_device"):
        await async_remove_config_entry_device(
            hass, entry, _device(hass, f"{GATEWAY_ID}-{OUTLET_ID}")
        )

    assert not fake_gateway.removed_devices


@pytest.mark.parametrize("device", ["outlet"], indirect=True)
async def test_unreachable_device_is_unpaired(
    hass: HomeAssistant,
    command_store: CommandStore,
    mock_gateway: Gateway,
    fake_gateway: FakeGateway,
    device: Device,
) -> None:
    """An unreachable device is unpaired on the gateway and deleted."""
    entry = await setup_integration(hass)
    response = device.raw.dict(by_alias=True)
    response[ATTR_REACHABLE_STATE] = 0
    command_store.register_response(mock_gateway.get_device(OUTLET_ID), response)

    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        assert await async_remove_config_entry_device(
            hass, entry, _device(hass, f"{GATEWAY_ID}-{OUTLET_ID}")
        )

    assert fake_gateway.removed_devices == [OUTLET_ID]
    reload.assert_called_once_with(entry.entry_id)


@pytest.mark.parametrize("device", ["outlet"], indirect=True)
async def test_device_gone_from_gateway(
    hass: HomeAssistant,
    command_store: CommandStore,
    mock_gateway: Gateway,
    fake_gateway: FakeGateway,
    device: Device,
) -> None:
    """A device the gateway no longer lists is deleted without a request."""
    entry = await setup_integration(hass)
    command_store.register_response(mock_gateway.get_devices(), [])

    assert await async_remove_config_entry_device(
        hass, entry, _device(hass, f"{GATEWAY_ID}-{OUTLET_ID}")
    )
    assert not fake_gateway.removed_devices


@pytest.mark.parametrize("device", ["outlet"], indirect=True)
async def test_gateway_not_loaded(
    hass: HomeAssistant, fake_gateway: FakeGateway, device: Device
) -> None:
    """Without a connection the device cannot be checked."""
    entry = await setup_integration(hass)
    device_entry = _device(hass, f"{GATEWAY_ID}-{OUTLET_ID}")
    await hass.config_entries.async_unload(entry.entry_id)

    with pytest.raises(HomeAssistantError, match="not connected"):
        await async_remove_config_entry_device(hass, entry, device_entry)

    assert not fake_gateway.removed_devices


@pytest.mark.parametrize("device", ["outlet"], indirect=True)
async def test_removal_waiting_during_unload(
    hass: HomeAssistant,
    command_store: CommandStore,
    mock_gateway: Gateway,
    fake_gateway: FakeGateway,
    device: Device,
) -> None:
    """A removal still waiting for the gateway when unloading starts is refused."""
    entry = await setup_integration(hass)
    response = device.raw.dict(by_alias=True)
    response[ATTR_REACHABLE_STATE] = 0
    command_store.register_response(mock_gateway.get_device(OUTLET_ID), response)
    device_entry = _device(hass, f"{GATEWAY_ID}-{OUTLET_ID}")

    async with gateway_operation(hass, entry):
        removal = asyncio.ensure_future(
            async_remove_config_entry_device(hass, entry, device_entry)
        )
        await asyncio.sleep(0)
        unload = asyncio.ensure_future(hass.config_entries.async_unload(entry.entry_id))
        for _ in range(50):
            await asyncio.sleep(0)
        assert not unload.done()

    with pytest.raises(HomeAssistantError, match="not connected"):
        await removal
    assert await unload
    assert not fake_gateway.removed_devices
