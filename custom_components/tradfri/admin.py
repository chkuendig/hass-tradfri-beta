"""Gateway management that otherwise needs the IKEA Home smart app.

The commands come from pytradfri (the chkuendig fork adds group creation and
deletion and device removal); this module adds what the gateway needs around
them. Observed on gateway firmware 1.21.57:

- A new group is created empty and its id is not returned, so it is found by
  listing the groups, and members are added afterwards.
- A group can only be deleted once it is empty.
- Right after a delete the gateway still lists the id for a moment but
  answers 4.04 for the item itself.
- Group membership changes show up after a short delay.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from pytradfri import Gateway
from pytradfri.api.aiocoap_api import APIRequestProtocol
from pytradfri.command import Command
from pytradfri.const import ATTR_GROUP_ID, ATTR_ID
from pytradfri.device import Device
from pytradfri.error import ClientError
from pytradfri.group import Group

# The gateway closes the pairing window after roughly 45-60 s regardless of
# the requested timeout, so it is renewed periodically.
PAIRING_RENEW_SECONDS = 30
PAIRING_WINDOW_SECONDS = 60

# The group the gateway keeps for itself, holding every device. It is listed
# as "SuperGroup", but that name can be changed. No client documents how to
# recognise it: 131073 is its id on the gateway this was developed with and in
# pytradfri's endpoint example from another gateway (the first id of the
# group range, created at setup). Attribute 9108 is 2 for it and 0 for rooms,
# but its meaning is unknown and pytradfri does not parse it. Whether the
# gateway itself refuses to delete it is untested.
SUPERGROUP_ID = 131073

# Like the integration's setup requests; without one a request to an
# unreachable gateway only fails when CoAP gives up, minutes later.
REQUEST_TIMEOUT = 30

_POLL_ATTEMPTS = 10
_POLL_INTERVAL = 0.5


class GatewayError(Exception):
    """The gateway did not do what it was asked."""


class GroupNotFoundError(Exception):
    """No group has the requested name."""


class AmbiguousGroupError(Exception):
    """Several groups have the requested name."""


@dataclass(frozen=True, slots=True)
class GroupInfo:
    """A gateway group with its member device ids."""

    id: int
    name: str
    member_ids: list[int]


def normalize_name(name: str | None) -> str:
    """Name as compared when looking groups up."""
    return (name or "").strip().casefold()


class TradfriAdmin:
    """Management operations on one gateway."""

    def __init__(self, api: APIRequestProtocol, gateway: Gateway) -> None:
        """Initialize with the entry's API and gateway."""
        self._api = api
        self._gateway = gateway

    async def _request[T](self, command: Command[T]) -> T:
        return await self._api(command, timeout=REQUEST_TIMEOUT)

    async def _fetch_each[T](self, commands: list[Command[T]]) -> list[T]:
        """Fetch each listed item, skipping ones deleted in the meantime."""
        results = await asyncio.gather(
            *(self._request(command) for command in commands), return_exceptions=True
        )
        items: list[T] = []
        for result in results:
            if isinstance(result, ClientError) and "4.04" in str(result):
                continue
            if isinstance(result, BaseException):
                raise result
            items.append(result)
        return items

    async def devices(self) -> list[Device]:
        """Current devices, straight from the gateway."""
        return await self._fetch_each(await self._request(self._gateway.get_devices()))

    async def groups(self) -> list[Group]:
        """Current groups, straight from the gateway."""
        return await self._fetch_each(await self._request(self._gateway.get_groups()))

    async def group_infos(self) -> list[GroupInfo]:
        """Groups with their member ids, sorted by name."""
        return sorted(
            (
                GroupInfo(g.id, g.name or "", list(g.member_ids))
                for g in await self.groups()
            ),
            key=lambda g: normalize_name(g.name),
        )

    async def find_group(self, name: str) -> Group | None:
        """The group with this name (case-insensitive), if any."""
        matches = [
            g
            for g in await self.groups()
            if normalize_name(g.name) == normalize_name(name)
        ]
        if len(matches) > 1:
            raise AmbiguousGroupError(name)
        return matches[0] if matches else None

    async def get_group(self, name: str) -> Group:
        """The group with this name; raise if there is none."""
        if (group := await self.find_group(name)) is None:
            raise GroupNotFoundError(name)
        return group

    async def rename_device(self, device_id: int, name: str) -> None:
        """Rename a device on the gateway."""
        device = await self._request(self._gateway.get_device(device_id))
        await self._request(device.set_name(name))

    async def rename_group(self, group: Group, name: str) -> None:
        """Rename a group on the gateway."""
        await self._request(group.set_name(name))

    async def create_group(self, name: str, member_ids: list[int]) -> int:
        """Create a group with these members and return its id."""
        before = {g.id for g in await self.groups()}
        await self._request(Gateway.create_group(name))

        async def new_group_id() -> int | None:
            return next(
                (
                    g.id
                    for g in await self.groups()
                    if g.id not in before
                    and normalize_name(g.name) == normalize_name(name)
                ),
                None,
            )

        if (created := await self._poll(new_group_id)) is None:
            raise GatewayError(f"The gateway did not create group {name!r}")
        await self.add_members(created, member_ids)
        return created

    async def delete_group(self, group: Group) -> None:
        """Delete a group, removing its members first."""
        if group.member_ids:
            await self.remove_members(group.id, list(group.member_ids))

            async def emptied() -> bool | None:
                current = next(
                    (g for g in await self.groups() if g.id == group.id), None
                )
                return True if current is None or not current.member_ids else None

            if await self._poll(emptied) is None:
                raise GatewayError(f"The gateway did not empty group {group.name!r}")
        await self._request(Gateway.delete_group(group.id))

    async def add_members(self, group_id: int, member_ids: list[int]) -> None:
        """Add devices to a group."""
        for member in member_ids:
            await self._request(
                Gateway.add_group_member({ATTR_GROUP_ID: group_id, ATTR_ID: [member]})
            )

    async def remove_members(self, group_id: int, member_ids: list[int]) -> None:
        """Remove devices from a group."""
        for member in member_ids:
            await self._request(
                Gateway.remove_group_member(
                    {ATTR_GROUP_ID: group_id, ATTR_ID: [member]}
                )
            )

    async def remove_device(self, device_id: int) -> None:
        """Remove (unpair) a device from the gateway."""
        await self._request(Gateway.remove_device(device_id))

    async def open_pairing(self) -> None:
        """Open or renew the gateway's pairing window."""
        await self._request(Gateway.set_commissioning_timeout(PAIRING_WINDOW_SECONDS))

    async def close_pairing(self) -> None:
        """Close the gateway's pairing window."""
        await self._request(Gateway.set_commissioning_timeout(0))

    async def reboot(self) -> None:
        """Reboot the gateway."""
        await self._request(Gateway.reboot())

    @staticmethod
    async def _poll[R](check: Callable[[], Awaitable[R | None]]) -> R | None:
        """Repeat check until it returns a value; the gateway lags a little."""
        for attempt in range(_POLL_ATTEMPTS):
            if (result := await check()) is not None:
                return result
            if attempt < _POLL_ATTEMPTS - 1:
                await asyncio.sleep(_POLL_INTERVAL)
        return None
