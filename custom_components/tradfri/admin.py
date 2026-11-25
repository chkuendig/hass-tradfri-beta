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
- Membership changes are two-phase: the CoAP request returns 2.04 once the
  change is queued, but an add only lands when the device itself answers a
  ZCL Add Group Response. A device that is off the network (powered off,
  unplugged) never confirms and the add silently never applies, so both
  member services verify the committed member list before reporting success.
  Removals, in contrast, update the gateway's list without waiting for the
  device.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from pytradfri import Gateway
from pytradfri.api.aiocoap_api import APIRequestProtocol
from pytradfri.command import Command
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

# A membership add only commits once the device answers a ZCL Add Group
# Response, which takes up to about 10 s.
MEMBER_TIMEOUT_SECONDS = 15


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

    async def _request[T](
        self, command: Command[T], timeout: float = REQUEST_TIMEOUT
    ) -> T:
        return await self._api(command, timeout=timeout)

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
        await self._request(Gateway.delete_group(group.id))

    async def add_members(self, group_id: int, member_ids: list[int]) -> None:
        """Add devices to a group.

        Raises GatewayError if the gateway does not confirm the change, which
        happens when a device is off the network (powered off or unplugged).
        """
        group = await self._group_or_gone(group_id)
        for member in member_ids:
            if member in group.member_ids:
                continue
            await self._request(group.add_member(member))
        await self._wait_for_members(group_id, present=member_ids)

    async def remove_members(self, group_id: int, member_ids: list[int]) -> None:
        """Remove devices from a group."""
        group = await self._get_group(group_id)
        if group is None:
            return
        for member in member_ids:
            if member not in group.member_ids:
                continue
            await self._request(group.remove_member(member))
        await self._wait_for_members(group_id, absent=member_ids)

    async def _get_group(
        self, group_id: int, timeout: float = REQUEST_TIMEOUT
    ) -> Group | None:
        """The group straight from the gateway, or None once it is gone."""
        try:
            return await self._request(
                self._gateway.get_group(group_id), timeout=timeout
            )
        except ClientError as err:
            if "4.04" in str(err):
                return None
            raise

    async def _group_or_gone(self, group_id: int) -> Group:
        """The group, or an error once the gateway no longer lists it."""
        if (group := await self._get_group(group_id)) is not None:
            return group
        raise GatewayError(f"The gateway no longer lists group {group_id}")

    async def _wait_for_members(
        self,
        group_id: int,
        *,
        present: list[int] | None = None,
        absent: list[int] | None = None,
    ) -> None:
        """Wait until the gateway's committed member list shows the change.

        An add only commits once the device answers a ZCL Add Group Response,
        so the list is re-read until a deadline; a timeout means the change
        was not confirmed, e.g. by an unreachable (powered off) device.
        """
        present, absent = set(present or []), set(absent or [])
        members: set[int] = set()
        end = time.monotonic() + MEMBER_TIMEOUT_SECONDS
        while True:
            # Bound each read by the time left, so one hung request cannot
            # push the whole check far past the deadline.
            budget = max(1.0, min(REQUEST_TIMEOUT, end - time.monotonic()))
            if (group := await self._get_group(group_id, timeout=budget)) is None:
                if not present:
                    return  # The group is gone; its members are too.
                break  # An add cannot commit into a group that is gone.
            members = set(group.member_ids)
            if present <= members and not absent & members:
                return
            if time.monotonic() >= end:
                break
            await asyncio.sleep(_POLL_INTERVAL)
        if present:
            raise GatewayError(
                f"The gateway did not confirm adding device(s)"
                f" {sorted(present - members)} to group {group_id}: the device(s)"
                " may be unreachable (powered off)"
            )
        raise GatewayError(
            f"The gateway did not confirm removing device(s)"
            f" {sorted(absent & members)} from group {group_id}"
        )

    async def remove_device(self, device_id: int) -> None:
        """Remove (unpair) a device from the gateway."""
        await self._request(Gateway.remove_device(device_id))

    async def open_pairing(
        self, group_id: int | None = None, group_name: str | None = None
    ) -> None:
        """Open or renew the gateway's pairing window.

        A joining controller is placed in group group_id, or in a new group
        named group_name; without either, the gateway keeps its default.
        """
        await self._request(
            Gateway.set_commissioning_timeout(
                PAIRING_WINDOW_SECONDS, group_id=group_id, group_name=group_name
            )
        )

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
