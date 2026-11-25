# IKEA TRÅDFRI Integration for Home Assistant

A drop-in replacement for the [official IKEA TRÅDFRI integration](https://www.home-assistant.io/integrations/tradfri/)
that handles the gateway management you would otherwise need the IKEA Home smart app for: pairing new devices,
naming them, sorting them into rooms and removing them again.

It uses the same `tradfri` domain, so it overrides the core integration while keeping your existing gateway entry,
devices and entity IDs. It is based on the Home Assistant 2026.11.1 core integration. Lights, plugs, blinds, air
purifiers and sensors work as they do there, with two fixes on top:

- **pytradfri 15.0.0**, so one unreachable device that reports no values no longer breaks the whole gateway setup.
- **No more crashes on network drops.** With aiocoap 0.4.15–0.4.17, losing the network while the gateway connection
  is open can crash the whole Home Assistant process with a segmentation fault. This
  integration installs a fixed aiocoap.

## What it adds

| Feature | How |
|---|---|
| Pair new devices without the app | `tradfri.pair_devices` action |
| Rename devices | `tradfri.rename_device` |
| Rename groups (rooms) | `tradfri.rename_group` |
| Create and delete groups | `tradfri.create_group`, `tradfri.delete_group` |
| Change group members | `tradfri.add_to_group`, `tradfri.remove_from_group` |
| Show groups and members | `tradfri.list_groups` (returns a response) |
| Remove (unpair) devices | `tradfri.remove_device`, or **Delete** on the device page for devices that are gone or unreachable |
| Restart the gateway | **Restart** button on the gateway device (configuration entity) |
| See when a device last talked to the gateway | **Last seen** diagnostic sensor on every device |

The actions change the gateway's setup, so only administrators can run them, like the network management actions of
Zigbee Home Automation (ZHA).

Not covered (yet): moods/scenes, smart tasks (wake-up, on/off timers) and firmware updates.

## Installation

### Marketplace

1. Open **Marketplace** and add `https://github.com/chkuendig/hass-tradfri-beta` as a custom repository,
   category **Integration**. Adding custom repositories needs the Marketplace's GitHub connection.
2. Install **IKEA TRÅDFRI (gateway admin)** and restart Home Assistant.

### Manual

Copy `custom_components/tradfri` into your Home Assistant `config/custom_components/` and restart.

If you don't have a gateway entry yet, set it up as usual under **Settings → Devices & services**. You need the
security code from the bottom of the gateway.

To go back to the core integration, uninstall it from the Marketplace (or remove `custom_components/tradfri`) and
restart. The entry and entities stay.

### Dependencies

On first start Home Assistant installs two forks from GitHub, so that start needs internet access:

- [chkuendig/pytradfri](https://github.com/chkuendig/pytradfri) `15.0.0+chkuendig.2`: pytradfri 15.0.0 plus
  commands to create and delete groups and to remove devices, a target group for the pairing window, the power-on
  behavior of lights and outlets, and a fix that makes error replies without a payload raise instead of passing as
  success.
- [chkuendig/aiocoap](https://github.com/chkuendig/aiocoap) `0.4.17+chkuendig.1`: aiocoap 0.4.17 plus the fix for
  that crash.

DTLSSocket, which pytradfri's async extra would bring, is required directly (uv ignores extras in this
URL-with-fragment form). Later starts only check the installed versions, and work offline while those stay installed;
a new Home Assistant container or image needs internet access again for the first start.

## Usage

All actions can be run from **Developer tools → Actions**, from scripts and from automations by an administrator.
Devices are picked with the normal device selector. Groups are given by name, matched case-insensitively and
ignoring surrounding spaces. With more than one gateway, pass `config_entry_id` to the actions that have no device
to go by.

### Pairing a new device

Reset the device first: for bulbs, switch the power off and on six times. For other devices, press the pairing
or reset button as described in IKEA's manual. Then open the pairing window:

```yaml
action: tradfri.pair_devices
data:
  seconds: 120
response_variable: paired
```

The gateway closes its pairing window after about a minute, however long you ask for, so the integration renews
it every 30 seconds until `seconds` have passed. Only one pairing session can run per gateway. The response lists
the devices that joined, and the integration reloads so they appear in Home Assistant. Then name the device and
put it in a room:

```yaml
action: tradfri.rename_device
data:
  device_id: <device>
  name: Hallway 1
---
action: tradfri.add_to_group
data:
  group: Living
  device_ids: [<device>]
```

`rename_device` renames the device on the gateway. In Home Assistant the new name shows unless you have given the
device your own name there.

### Groups

```yaml
action: tradfri.create_group
data:
  name: Hallway
  device_ids: [<device>, <device>]
```

A device can be in several groups. Group names must be unique. `delete_group` takes the members out first, because
the gateway refuses to delete a group that still has members; the devices stay paired.

The gateway keeps a group of all devices for itself, listed as `SuperGroup`. The actions refuse to rename, delete
or change it.

### Removing a device

`tradfri.remove_device` unpairs a device from the gateway and reloads the integration. The device must be reset
before it can be paired again.

The **Delete** button on a device page works for devices the gateway no longer knows about, and for unreachable
ones (it unpairs those). A device that is still reachable has to be removed with the action, so a click cannot
unpair a working lamp by accident.

### Last seen

The gateway updates a device's last-seen time when the device talks to it. It is not a heartbeat: a mains-powered
lamp that sits idle can show an old time and still work. The gateway only marks a device unreachable after a
command to it fails.

## Gateway API notes (firmware 1.21.57)

Things learned while building this:

- `POST /15004` with a name creates an empty group. Members in the payload are ignored, and the reply (`2.01`) has no
  payload and no Location-Path, so the new id has to be found by listing the groups.
- `DELETE /15004/<id>` only works on an empty group (`4.00 Bad Request` otherwise).
- Right after a delete the gateway still lists the id for a moment, but answers `4.04` for the item itself.
- `DELETE /15001/<id>` removes a device.
- The commissioning (pairing) window closes after about 45–60 s regardless of the requested timeout.
- The SuperGroup has id 131073 here and in pytradfri's endpoint example from another gateway. Its attribute `9108`
  is 2 (0 for rooms), but no client documents what `9108` means.
- The gateway is also a native HomeKit bridge. Rooms in Apple Home are set in the Home app and are not linked to
  the gateway's groups.

## Development

The tests include Home Assistant core's tradfri tests (as of 2026.11.1, pointed at this integration) and use
Home Assistant's own test helpers, `pytest-homeassistant`, which installs the matching Home Assistant:

```bash
pip install -r requirements_test.txt
pytest
```

## License

Apache License 2.0, like the Home Assistant core integration this is based on.
