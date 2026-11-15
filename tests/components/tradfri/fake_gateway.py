"""A stateful stand-in for the gateway's management endpoints.

Behaves like gateway firmware 1.21.57 did when the integration was written:
a new group starts empty whatever members are posted, a group with members
cannot be deleted (4.00), and a deleted group stays listed once but answers
4.04. Everything else is answered by the CommandStore.

Tests can make a new group show up only after some listings, or never, and
can hold every change until they release it.
"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pytradfri.command import Command
from pytradfri.const import (
    ATTR_COMMISSIONING_MODE,
    ATTR_GATEWAY_INFO,
    ATTR_GATEWAY_REBOOT,
    ATTR_GROUP_ID,
    ATTR_ID,
    ATTR_NAME,
    ROOT_DEVICES,
    ROOT_GATEWAY,
    ROOT_GROUPS,
)
from pytradfri.error import ClientError

from .common import CommandStore

SUPERGROUP_ID = 131073


def group_response(group_id: int, name: str, members: list[int]) -> dict[str, Any]:
    """Return a group as the gateway reports it."""
    return {
        "9001": name,
        "9002": 1700000000,
        "9003": group_id,
        "5850": 0,
        "5851": 0,
        "9039": 0,
        "9108": 0,
        "9018": {"15002": {"9003": list(members)}},
    }


@dataclass
class FakeGateway:
    """Group, device removal, pairing and reboot endpoints with state."""

    command_store: CommandStore
    groups: dict[int, dict[str, Any]] = field(default_factory=dict)
    stale: set[int] = field(default_factory=set)
    removed_devices: list[int] = field(default_factory=list)
    commissioning: list[int] = field(default_factory=list)
    reboots: int = 0
    next_id: int = 131100
    reboot_error: Exception | None = None
    on_commissioning: Callable[[int], None] | None = None
    created_visible_after: int = 0
    ignore_creates: bool = False
    hold: asyncio.Event | None = None
    held: asyncio.Event = field(default_factory=asyncio.Event)
    _hidden: dict[int, int] = field(default_factory=dict)

    def add_group(
        self, group_id: int, name: str, members: list[int] | None = None
    ) -> None:
        """Add a group to the gateway."""
        self.groups[group_id] = group_response(group_id, name, members or [])

    def members(self, group_id: int) -> list[int]:
        """Return the member ids of a group."""
        return self.groups[group_id]["9018"]["15002"]["9003"]

    def name(self, group_id: int) -> str:
        """Return the name of a group."""
        return self.groups[group_id]["9001"]

    async def api(
        self,
        command: Command | list[Command],
        timeout: float | None = None,  # noqa: ASYNC109 - pytradfri's request signature
    ) -> Any:
        """Answer a command like the gateway would."""
        if isinstance(command, list):
            return [await self.api(cmd) for cmd in command]
        self.command_store.sent_commands.append(command)
        if self.hold is not None and command.method != "get":
            self.held.set()
            await self.hold.wait()
        path = [str(part) for part in command.path]
        data = command.data or {}
        if path[0] == ROOT_GROUPS:
            return self._group_command(command, path[1:], data)
        if (
            path == [ROOT_GATEWAY, ATTR_GATEWAY_INFO]
            and ATTR_COMMISSIONING_MODE in data
        ):
            self.commissioning.append(data[ATTR_COMMISSIONING_MODE])
            if self.on_commissioning:
                self.on_commissioning(data[ATTR_COMMISSIONING_MODE])
            return None
        if path == [ROOT_GATEWAY, ATTR_GATEWAY_REBOOT]:
            if self.reboot_error:
                raise self.reboot_error
            self.reboots += 1
            return None
        if path[0] == ROOT_DEVICES and command.method == "delete":
            self.removed_devices.append(int(path[1]))
            return None
        return self.command_store.process_command(command)

    def _group_command(
        self, command: Command, path: list[str], data: dict[str, Any]
    ) -> Any:
        if command.method == "get" and not path:
            listed = [g for g in self.groups if self._hidden.get(g, 0) == 0]
            self._hidden = {g: n - 1 for g, n in self._hidden.items() if n > 0}
            assert command.process_result
            return command.process_result([*listed, *self.stale])
        if command.method == "get":
            group_id = int(path[0])
            if group_id in self.stale:
                self.stale.discard(group_id)
                raise ClientError("Gateway payload: . Error code: 4.04 Not Found.")
            assert command.process_result
            return command.process_result(self.groups[group_id])
        if command.method == "post" and not path:
            if self.ignore_creates:
                return None
            self.add_group(self.next_id, data[ATTR_NAME])  # Members are ignored.
            self._hidden[self.next_id] = self.created_visible_after
            self.next_id += 1
            return None
        if command.method == "put" and path in (["add"], ["remove"]):
            members = self.members(data[ATTR_GROUP_ID])
            for member in data[ATTR_ID]:
                if path == ["add"] and member not in members:
                    members.append(member)
                if path == ["remove"] and member in members:
                    members.remove(member)
            return None
        if command.method == "put":
            self.groups[int(path[0])]["9001"] = data[ATTR_NAME]
            return None
        if command.method == "delete":
            group_id = int(path[0])
            if self.members(group_id):
                raise ClientError(
                    'Gateway payload: {"r":"00"}. Error code: 4.00 Bad Request.'
                )
            del self.groups[group_id]
            self.stale.add(group_id)
            return None
        raise AssertionError(f"Unexpected group command {command.method} {path}")
