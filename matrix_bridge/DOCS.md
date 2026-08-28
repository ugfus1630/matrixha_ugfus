# Matrix Bridge

An end-to-end encrypted Matrix bot for Home Assistant.

## Setup

1. Create a Matrix account for the bot on your homeserver. Do **not** reuse your own
   account — the bot needs its own device identity.
2. Start the add-on. There is nothing to fill in first: the add-on's **Configuration**
   tab is empty on purpose.
3. Open **Matrix** in the Home Assistant sidebar. The panel opens on *Settings* with
   nothing set up yet. Enter the homeserver, the bot's Matrix ID and its password, then
   press **Save & reconnect**. The bridge logs in there and then, and the status card
   above says whether it worked.
4. Add the **Matrix Bridge** integration in Home Assistant. There is nothing to type: it
   finds the add-on through Supervisor and reads the hostname, port and API token itself.
   Just confirm.
5. Invite the bot to a room from any Matrix client.

**Everything is configured in the panel**, including how the bridge presents itself in
Home Assistant. The integration has no settings of its own and no options dialog, and the
add-on's Configuration tab holds only the API token the add-on generates for itself.
Saving in the panel validates the settings, stores them in `/data/settings.json` and
reconnects — no add-on restart.

Upgrading from 0.2.x: whatever was in the Configuration tab is imported into the panel on
first start, so there is nothing to re-enter.

After the first successful login the add-on stores its session in `/data/session.json`
and reuses the same `device_id` on every restart. That is what keeps the bot's device
verified across restarts — deleting that file makes the bot appear as a brand new
unverified device to everyone in your encrypted rooms.

## Settings

All of these are in the panel, under *Settings*. The names below are the keys as they are
stored in `/data/settings.json`; the panel shows them with labels.

### Connection

| Setting | Default | Description |
| --- | --- | --- |
| `homeserver` | *(unset)* | Homeserver base URL. `https://` is assumed if you leave it off. |
| `user_id` | *(unset)* | Full Matrix ID, e.g. `@homeassistant:example.org`. |
| `password` | *(unset)* | Used for the initial login. May be cleared afterwards; the stored session keeps working. |
| `access_token` | *(unset)* | Use instead of a password. |
| `device_name` | `Home Assistant` | Display name shown to other users in device lists. |
| `presence` | `online` | `online`, `unavailable` or `offline`. |

### `autojoin`

| Setting | Default | Description |
| --- | --- | --- |
| `enabled` | `true` | Join rooms the bot is invited to. |
| `allowed_users` | `[]` | Only accept invites from these Matrix IDs. Empty means anyone. |
| `allowed_domains` | `[]` | Only accept invites from these homeserver domains. Empty means any. |
| `max_rooms` | `50` | Refuse new invites beyond this many rooms. |
| `accept_dm_invites` | `true` | Accept one-to-one invites. |
| `leave_if_alone` | `false` | Leave rooms where the bot is the only remaining member. |

Leaving both allowlists empty means **anyone who knows the bot's address can invite it**.
On a public homeserver, set `allowed_users` or `allowed_domains`.

### `e2ee`

| Setting | Default | Description |
| --- | --- | --- |
| `enabled` | `true` | Master switch for encryption support. |
| `send_to_unverified_rooms` | `true` | When `false`, the bot refuses to send to any room containing an unverified device, and the service call fails with the offending device list. |
| `ignore_unverified_devices` | `true` | When `false`, nio itself refuses to share room keys with unverified devices. |
| `blacklist_unverified` | `false` | Actively blacklist unverified devices so they never receive keys. Aggressive — verified devices in the same room keep working, unverified ones permanently cannot decrypt. |
| `auto_trust_own_devices` | `true` | Automatically verify other devices belonging to the bot's own account. |
| `auto_accept_verification` | `false` | Automatically accept inbound verification requests. The emoji still have to be confirmed. |
| `require_verified_sender` | `false` | Ignore inbound messages sent from unverified devices. |
| `verification_timeout` | `300` | Seconds before an unanswered verification is dropped. |

The difference between the first two is worth understanding:

- `send_to_unverified_rooms: false` is **our** policy — nothing is sent at all, and you
  get a clear error naming the untrusted devices.
- `ignore_unverified_devices: false` is **nio's** policy — the send is attempted and
  fails at the crypto layer.

Setting `send_to_unverified_rooms: false` is the stricter, more legible choice.

### `media`

| Setting | Default | Description |
| --- | --- | --- |
| `download_incoming` | `true` | Download and decrypt inbound attachments. |
| `download_path` | `/media/matrix` | Where they are written. Under `/media` they show up in Home Assistant's media browser. |
| `max_download_mb` | `25` | Skip inbound files larger than this. |
| `max_upload_mb` | `50` | Refuse outbound files larger than this. |
| `allowed_upload_paths` | `/media`, `/share`, `/homeassistant/www` | The bot will only read files inside these directories. Symlinks and `..` that escape them are rejected. |

Supervisor mounts Home Assistant's configuration directory into add-ons at
`/homeassistant`, but Home Assistant calls that same directory `/config` — which is what
appears in automations, `camera.snapshot` targets and the `www` folder behind `/local`.
Both spellings are accepted here, on the paths you send and on the roots you list, so
`/config/www/snapshot.jpg` and `/homeassistant/www/snapshot.jpg` reach the same file.
| `retention_days` | `30` | Delete downloads older than this. `0` disables pruning. |

### API token

Not a setting to fill in: the add-on generates a token on first start, stores it in
`/data/api_token` and publishes it into its own Supervisor options as `api_token`, which
is how the integration finds the bridge without anyone typing anything. The panel shows
it under *Settings → API token* for calling the API yourself. The port is fixed at 8099,
the Ingress port.

### `homeassistant`

How the bridge presents itself in Home Assistant. These live here, rather than in an
integration options dialog, so that every setting is in one place.

| Setting | Default | Description |
| --- | --- | --- |
| `create_notify_entities` | `true` | Create a `notify.matrix_bridge_<room>` entity for each discovered room. |
| `fire_events` | `true` | Fire `matrix_bridge_*` events on the Home Assistant event bus for inbound messages. |

Reload the integration after changing these.

### `advanced`

| Setting | Default | Description |
| --- | --- | --- |
| `log_level` | `info` | `trace`, `debug`, `info`, `warning`, `error`. |
| `ignore_own_messages` | `true` | Don't emit events for the bot's own messages. |
| `sync_timeout` | `30000` | Long-poll timeout in milliseconds. |
| `message_history_limit` | `50` | Backlog fetched on first sync. |

## Notify entities

One `notify.matrix_bridge_<room>` entity per joined room, created as the bot is invited
and removed when it leaves.

```yaml
action: notify.send_message
target:
  entity_id: notify.matrix_bridge_alerts
data:
  message: The front door was opened.
  title: Front door        # optional, prepended to the message
```

In the UI: *Add action → Notifications → Send a notification message*, then pick the
room. The entities are also on the **Matrix Bridge** device page.

Two things surprise people, both by design:

- **There is no `notify.matrix_<room>` action.** Those per-target services belonged to
  the legacy notify platform, which Home Assistant is removing. Rooms are entities now,
  and one action — `notify.send_message` — targets any of them. Anything richer than a
  message and a title (images, files, replies, threads, reactions) is a
  `matrix_bridge.*` action, which does appear in the action list under *Matrix Bridge*.
- **A notify entity's state is the last time it sent something**, so it reads `unknown`
  until the first message and then shows a timestamp. It is not a connection indicator;
  `binary_sensor.matrix_bridge_connected` is. `unavailable` does mean something is
  wrong: either the bridge is disconnected or the bot is no longer in that room.

Entity ids created before 0.3.2 contain the bot's Matrix ID
(`notify.matrix_bridge_homeassistant_example_org_alerts`). Home Assistant keeps the id
an entity was first registered with, so rename them under *Settings → Devices &
Services → Entities*, or delete and re-add the integration to have them recreated with
the short names.

## Device verification

Open the **Matrix** panel in the Home Assistant sidebar.

**From Home Assistant:** find the device under *Devices* and press *Verify with emoji*.
Your Matrix client will show a verification request. Accept it, compare the emoji, and
press **They match** in both places.

**From your Matrix client:** start verification against the Home Assistant device. The
request appears in the panel's *Verification* section, and as a persistent notification
in Home Assistant containing the emoji and the exact service call to confirm it.

Verification can also be driven entirely by services, which is useful for headless
setups:

```yaml
action: matrix_bridge.start_verification
data:
  user_id: "@alice:example.org"

# then, after comparing the emoji from the matrix_bridge_verification event:
action: matrix_bridge.confirm_verification
data:
  transaction_id: "{{ trigger.event.data.transaction_id }}"
```

If you trust a device out of band, you can skip the emoji exchange entirely:

```yaml
action: matrix_bridge.set_device_trust
data:
  user_id: "@alice:example.org"
  device_id: ABCDEFGHIJ
  state: verify
```

## Security notes

- The API port is **not published** by default. Home Assistant reaches the add-on over
  the internal Docker network, so the token never crosses your LAN. Only map a host port
  if you specifically need external access.
- Requests arriving through Ingress skip the token because Supervisor has already
  authenticated the user — but only when the peer really is Supervisor. The
  `X-Ingress-Path` header alone is not trusted, since it is trivially forged.
- The bot only reads files inside `media.allowed_upload_paths`, resolved through the
  real filesystem so symlinks cannot escape.
- `/api/settings` returns the stored credentials, as the add-on Configuration tab it
  replaces did. Reaching it means being an authenticated Home Assistant admin through
  Ingress, or holding the API token; `/data/settings.json` is written `0600`.

## Troubleshooting

**The bridge shows as disconnected, or as not set up.**
Nothing about the configuration can stop the add-on — the panel is where it is fixed, so
the panel always comes up. Open **Matrix** in the sidebar: the status card shows the
failure under the connection pill, and the same text is in `/api/status` as `last_error`
and in `sensor.*` attributes. The usual causes are settings never filled in, a homeserver
that does not resolve, a wrong password, or a homeserver that is down. Correct it under
*Settings* and press **Save & reconnect**; no restart is needed.

**The Configuration tab is empty.**
As intended. Every setting is in the panel, where it can be checked against the
homeserver and applied without restarting the container. The `api_token` shown there is
generated by the add-on for the integration to discover.

**"Could not reach the add-on" when adding the integration.**
Check the add-on is running and that the host is `local-matrix-bridge` (hyphens, not
underscores). If you installed from a GitHub repository rather than locally, the hostname
prefix differs — check the add-on log line `API listening on port …` and use the add-on's
own hostname shown in its info tab.

**Sends fail with "Refusing to send: N unverified device(s)".**
Working as configured — *Send to rooms with unverified devices* is off. Verify the listed
devices in the panel, or turn the setting back on.

**The notify entities read `unknown`, or I cannot find a `notify.matrix_<room>` action.**
Both are expected — see *Notify entities* above. Send with `notify.send_message`
targeting the room's entity; the state becomes a timestamp once something has been sent.

**Messages arrive but pictures don't.**
Check `download_incoming` is on and that `download_path` is writable. In encrypted rooms
pictures arrive as `RoomEncryptedImage` events and are decrypted before writing; a
decryption error is logged rather than silently swallowed.

**"Could not decrypt an event" warnings.**
The sender's client didn't share the room key with the bot's device, usually because the
bot's device is unverified or joined after the message was sent. Verify the bot's device
in your Matrix client.

**The bot appears as a new unverified device after every restart.**
`/data` is not persisting, so the session and crypto store are lost. Check the add-on
has not been reinstalled between restarts.

## End-to-end test checklist

1. Install and start the add-on, fill in the panel's Settings and press *Save &
   reconnect*; confirm the status card shows connected and the log shows
   `Connected to … as @bot:…`.
2. Add the integration; confirm `binary_sensor.*_connected` is on.
3. Invite the bot from a second account → a `notify.matrix_bridge_<room>` entity appears.
4. Call `notify.send_message` against it → the message arrives and the entity's state
   becomes the time it was sent.
5. Call `matrix_bridge.send_image` with a file under `/media` → the picture arrives, and
   is encrypted if the room is.
6. Send a picture to the bot → `matrix_bridge_image` fires and the file lands in
   `/media/matrix`.
7. Run emoji verification from the sidebar panel → the device shows as verified.
8. Turn *Send to rooms with unverified devices* off with an unverified device present →
   sends fail with a clear error naming the device.
9. Restart the add-on → the bot keeps the same device ID, its settings and its
   verification.
