#FIX auto accept join requests to rooms
# Matrix Bridge for Home Assistant

[![Buy Me A Coffee](https://img.shields.io/badge/Buy%20Me%20A%20Coffee-support-FFDD00?style=flat-square&logo=buymeacoffee&logoColor=black)](https://buymeacoffee.com/immakinbacon)

An end-to-end encrypted Matrix bot, packaged as a Home Assistant **add-on**, plus a
**custom integration** that surfaces it inside Home Assistant.

Home Assistant's built-in `matrix` integration is YAML-only and cannot participate in
encrypted rooms. This pair fills that gap:

| | Built-in `matrix` | Matrix Bridge |
| --- | --- | --- |
| End-to-end encryption | ✗ | ✓ |
| Device verification (emoji) | ✗ | ✓ |
| Room auto-discovery | ✗ (hand-listed) | ✓ (joins on invite) |
| Configured from the UI | ✗ | ✓ (entirely in the panel) |
| Send images / files | ✓ | ✓ (encrypted too) |
| Receive images / files | ✗ | ✓ (decrypted to disk) |
| Trust policy controls | ✗ | ✓ |

## What you get

- **`notify.matrix_bridge_<room>` entity per room** — created automatically as the bot is
  invited to rooms, removed when it leaves. Send to one with the `notify.send_message`
  action; its state is the time it last sent something, so a fresh entity reads
  `unknown`.
- **Events** — `matrix_bridge_message`, `matrix_bridge_image`, `matrix_bridge_file`,
  `matrix_bridge_reaction`, `matrix_bridge_verification` on the Home Assistant event bus.
- **Services** — send messages (plain, HTML, replies, threads), images and files, add
  reactions, join/leave rooms, and drive device verification.
- **Sensors** — room count, unverified device count, pending verifications, last message;
  plus connectivity and "unverified devices present" binary sensors.
- **An Ingress panel** in the sidebar that is the whole configuration surface — settings,
  status, rooms, comparing verification emoji and managing per-device trust.

## Installation

### 1. Add-on

Settings → Add-ons → Add-on store → ⋮ → **Repositories**, add:

```
https://github.com/immakinbacon/matrixha
```

Install **Matrix Bridge** and start it. There is nothing to fill in first — the add-on's
Configuration tab is empty on purpose.

Open **Matrix** in the sidebar and fill in the panel's *Settings*: the homeserver, the
bot's Matrix ID and its password. **Save & reconnect** validates them, stores them in
`/data/settings.json` and logs in immediately; the status card above reports the result.
Every setting lives there, and none of them need an add-on restart.

On first start the add-on also generates an API token for the integration and publishes
it to Supervisor, so there is nothing to copy.

### 2. Integration

Copy `custom_components/matrix_bridge` into your Home Assistant `config/custom_components/`
directory (or install this repository through HACS as a custom repository), restart Home
Assistant, then Settings → Devices & Services → **Add Integration** → *Matrix Bridge*.

**There is nothing to fill in.** The integration finds the add-on through Supervisor and
reads its hostname, port and API token from Supervisor — you just confirm. Every setting
stays in one place: the add-on's panel.

A manual host/port/token form appears only as a fallback, when Home Assistant is not
running supervised (a Core install) or the add-on lives on another machine.

> Supervisor's `discovery:` mechanism only accepts a fixed list of known services, which
> custom integrations cannot join. The integration therefore reads the add-on's info from
> the Supervisor API directly rather than waiting to be discovered.

### 3. Invite the bot

Invite `@yourbot:example.org` to a room from any Matrix client. If the invite passes the
`autojoin` allowlists the bot joins, and a `notify.matrix_bridge_<room>` entity appears.

## Usage

```yaml
# Simple notification
action: notify.send_message
target:
  entity_id: notify.matrix_bridge_alerts
data:
  message: The front door was opened.

# A picture, encrypted automatically if the room is encrypted
action: matrix_bridge.send_image
data:
  room: "#alerts:example.org"
  path: /media/snapshot.jpg
  caption: Front door camera

# React to an inbound message
triggers:
  - trigger: event
    event_type: matrix_bridge_message
conditions:
  - "{{ 'garage' in trigger.event.data.body|lower }}"
actions:
  - action: matrix_bridge.react
    data:
      room: "{{ trigger.event.data.room_id }}"
      event_id: "{{ trigger.event.data.event_id }}"
      key: "👍"
```

Incoming pictures are decrypted and written to `media.download_path` (default
`/media/matrix`), and the event carries the local path:

```yaml
triggers:
  - trigger: event
    event_type: matrix_bridge_image
actions:
  - action: notify.persistent_notification
    data:
      message: "{{ trigger.event.data.sender }} sent {{ trigger.event.data.local_path }}"
```

## Encryption and trust

Every outbound message passes a single trust gate, controlled from the panel's
*Encryption* section:

| Setting | Effect |
| --- | --- |
| `e2ee.send_to_unverified_rooms` | When off, refuses to send to any room containing an unverified device and reports which ones. |
| `e2ee.ignore_unverified_devices` | When off, nio itself refuses to share keys with unverified devices. |
| `e2ee.blacklist_unverified` | Actively blacklists unverified devices so they never receive keys. |
| `e2ee.auto_trust_own_devices` | Trusts other devices belonging to the bot's own account. |
| `e2ee.auto_accept_verification` | Auto-accepts inbound verification requests (emoji still need confirming). |
| `e2ee.require_verified_sender` | Ignores inbound messages from unverified devices. |

To verify a device, open the **Matrix** panel in the sidebar, find the device and choose
*Verify with emoji* — or start verification from your own Matrix client. Compare the
emoji and press **They match**. The same flow is available headlessly through the
`matrix_bridge.confirm_verification` service and a persistent notification.

## Documentation

Full settings reference, troubleshooting and the end-to-end test checklist are in
[`matrix_bridge/DOCS.md`](matrix_bridge/DOCS.md).

## Development

The add-on runs outside of Supervisor for development:

```bash
pip install 'matrix-nio[e2e]==0.26.0' aiohttp aiofiles
MATRIX_BRIDGE_DATA=/tmp/matrix-bridge-dev \
PYTHONPATH=matrix_bridge python3 -m app.main
# then open http://localhost:8099 and fill in the settings
```

The panel is the configuration surface, so nothing needs to be prepared first.
To skip the form, copy `dev/settings.example.json` to
`/tmp/matrix-bridge-dev/settings.json` (or point `MATRIX_BRIDGE_SETTINGS` at
it) before starting.

Checks that need neither Docker nor a homeserver:

```bash
python3 dev/validate.py                                # config/manifest/contract consistency
PYTHONPATH=matrix_bridge python3 dev/test_media.py     # image parsing + upload allowlist
PYTHONPATH=matrix_bridge python3 dev/smoke.py          # full API surface against a stub
PYTHONPATH=. python3 dev/test_integration.py           # add-on discovery + translations
PYTHONPATH=. python3 dev/test_notify.py                # entities inside a real Home Assistant
```

## License

MIT
