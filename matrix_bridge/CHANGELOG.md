# Changelog

All notable changes to this add-on are documented here, following
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.3.5] - 2026-08-22

### Fixed

- **Coming back from a dropped sync.** A momentary homeserver outage left the add-on in
  a degraded state it never came out of. `connected` was only ever set when the bot first
  logged in, and the sync loop reconnects without logging in again, so once a single sync
  had failed the bridge called itself offline for the life of the process: the panel and
  the Home Assistant entities showed a stale error, and housekeeping — trust maintenance,
  verification timeouts, room-change detection — skips while disconnected and so stopped
  running until the add-on was restarted. A sync that succeeds now clears the error and
  marks the bridge online again.
- **A reconnect delay that only ever grew.** The backoff reset sat on an `else` branch
  that could not be reached, because `sync_forever` exits only by raising. Each failure
  doubled the wait up to the five-minute ceiling and nothing ever brought it back down,
  so a blip weeks into an otherwise healthy run still cost the full five minutes. The
  first good sync resets it.
- **`Sync failed ()`.** nio is configured not to retry timeouts, and a slow homeserver
  arrives as a bare `TimeoutError` whose message is empty — which left both the log line
  and the panel's error field with nothing in them to act on. A failure that carries no
  message is now reported by what it is.

## [0.3.4] - 2026-08-14

### Fixed

- **Sending a file from Home Assistant's `config` directory.** `allowed_upload_paths`
  listed `/config/www` by default, but nothing is mounted at `/config`: Supervisor puts
  Home Assistant's configuration directory at `/homeassistant`. A path that Home
  Assistant itself hands out — an automation's `/config/www/...`, a `camera.snapshot`
  target, anything behind `/local` — passed the allowlist and then failed with
  "No such file". Both spellings are now accepted, on the paths you send and on the
  roots you list, so either reaches the same file. The default names `/homeassistant/www`,
  which is the one that exists; existing configurations keep working untouched.

## [0.3.3] - 2026-08-13

### Added

- **An icon and a logo.** The add-on showed the Supervisor's generic placeholder in
  the store and on its own page. `icon.png` is the Matrix `[m]` in Home Assistant
  blue on a dark rounded tile, drawn to stay readable at the 48px the store list
  uses; `logo.png` is the same mark with a "Matrix Bridge" wordmark, on its own dark
  panel so it holds up under light and dark themes alike. Supervisor picks both up
  by filename, so there is nothing to configure.

## [0.3.2] - 2026-08-13

### Changed

- **Notify entities are now `notify.matrix_bridge_<room>`.** They were
  `notify.matrix_bridge_homeassistant_example_org_<room>`, because entity ids are
  built from the device name and the device carried the bot's Matrix ID — long
  enough to be hard to find in the action picker and unpleasant to type in an
  automation. The account moved to the device's serial number, where it is still
  visible on the device page. Home Assistant keeps the id an entity was first
  registered with, so existing entities have to be renamed or the integration
  removed and re-added.

### Added

- `dev/test_notify.py` boots a real Home Assistant against a stubbed add-on and
  asserts the entity ids, that `notify.send_message` reaches the right room, and
  that a notify entity's state is `unknown` until it has sent something — the last
  being Home Assistant's own behaviour, now written down so it does not get
  "fixed".
- The DOCS explain what the notify entities are, why there is no per-room
  `notify.matrix_<room>` action, and what their state means.

## [0.3.1] - 2026-08-13

### Fixed

- **Setting up the integration no longer dead-ends when the API token is missing or
  wrong.** Both cases now fall through to the manual form, prefilled with the
  hostname and port already discovered through Supervisor, so only the token — which
  is readable in the panel — has to be typed. Previously an add-on whose `api_token`
  Supervisor was not serving aborted with "wait for it to finish starting", which is
  wrong whenever the cause is durable: saving the add-on's *Configuration* tab with
  that optional field switched off clears the token from the stored options, and
  nothing republishes it until the next add-on start. A token that was served but
  rejected was worse, re-showing a form with no fields to correct it.
- Rejected API requests are logged at warning level with the path, peer and the
  *length* of the offered token, never the token itself. A 401 and an unreachable
  add-on were previously indistinguishable to the caller and left no trace here,
  which sent operators looking for a network fault that did not exist.

## [0.3.0] - 2026-08-13

### Changed

- **Every setting is now edited in the add-on's own panel**, not in the Supervisor
  *Configuration* tab, which is empty from this version on. The panel opens on a
  Settings form covering the connection, auto-join, encryption, media, Home Assistant
  and advanced options; **Save & reconnect** validates them, stores them in
  `/data/settings.json` and reconnects the bot in place — no add-on restart, and a
  wrong homeserver or password is reported next to the field that caused it rather
  than in the log.
- Existing 0.2.x configurations are imported from the add-on options on first start,
  so there is nothing to re-enter.
- The add-on's only Supervisor option is `api_token`, which the add-on generates and
  publishes itself so the integration can keep discovering it. Nothing there is for
  the operator to fill in.
- The API port is fixed at 8099, the Ingress port, instead of being a setting that
  could be changed to a value Ingress does not use.
- Settings are described once, in `settings.py`, and the panel builds its form from
  that description — a new setting cannot appear in the code without appearing in the
  panel, which `dev/validate.py` now enforces.

## [0.2.2] - 2026-08-13

### Fixed

- **Missing credentials no longer stop the add-on.** Starting without a `password` or an
  `access_token` failed option validation, which exits before the API starts — so the one
  case every new install hits first showed as *not running*, with the reason only in the
  log. It is now treated as what it is, a login failure: the add-on stays up and the
  Ingress panel, `/api/status` and the `sensor.*` attributes say which option to set.
- `password` is optional in the add-on schema. It was required, so the configuration form
  refused to save an `access_token`-only setup, or to clear the password after the first
  login even though the stored session keeps working.

## [0.2.1] - 2026-08-13

### Fixed

- **The add-on no longer stops when it cannot reach the homeserver.** nio is configured
  not to retry (`max_timeouts=0`), so an unresolvable or unreachable `homeserver` raised
  a transport error that escaped the startup path and exited the container — the add-on
  showed as *not running* with only a traceback in the log. Connection failures are now
  reported as configuration errors, and no startup failure of any kind can stop the
  add-on: it stays up with the reason in `last_error`, on the Ingress panel and in
  `/api/status`.
- Read endpoints (`/api/rooms`, `/api/devices`, `/api/verifications`) answer with empty
  collections while the bot is still connecting, instead of failing every request. They
  previously made the integration fail setup with *Retrying setup* whenever the bridge
  was offline, hiding the actual cause. Sending and trust changes still require a live
  connection.
- The Ingress panel renders whatever it can when a read fails, so the status card — which
  carries the error — is no longer blanked by an unrelated failing request.
- `/api/status` reports the configured `user_id` before login completes, keeping the
  integration's unique ID stable when it is set up while the bridge is offline.

## [0.2.0] - 2026-08-13

### Changed

- **All configuration now lives in the add-on UI.** The integration asks for nothing: it
  locates the add-on through the Supervisor API and reads its hostname, port and API
  token from the add-on's own options, so setup is a single confirmation.
- A generated API token is written back into the add-on's own configuration via
  `POST /addons/self/options`, so it is visible and editable under `api -> token`
  instead of only being printed to the log.
- Connection details are re-read from Supervisor on every setup, so changing the port or
  token in the add-on UI is picked up by reloading the integration. Re-authentication is
  automatic rather than prompting for a token.

### Added

- `homeassistant` options section (`create_notify_entities`, `fire_events`), replacing
  the integration's options dialog, which has been removed.

## [0.1.0] - 2026-08-13

### Added

- Matrix bot with end-to-end encryption via `matrix-nio[e2e]` 0.26 (vodozemac; no libolm
  system dependency).
- Automatic room discovery: joins on invite subject to user and domain allowlists, and
  tracks rooms joined from other clients.
- Sending of text, HTML, replies, threads, reactions, images and arbitrary files, with
  transparent encryption in encrypted rooms.
- Receiving of messages, images and files, including decryption of `RoomEncrypted*`
  attachments, written to a configurable download path.
- Interactive SAS (emoji) device verification in both directions, plus direct
  verify/unverify/blacklist trust management.
- Trust policy controls: `send_to_unverified_rooms`, `ignore_unverified_devices`,
  `blacklist_unverified`, `auto_trust_own_devices`, `auto_accept_verification` and
  `require_verified_sender`.
- Token-authenticated HTTP + WebSocket API for the Home Assistant integration.
- Ingress web panel for status, rooms, devices and live emoji verification.
- Session persistence so the bot keeps a stable device identity across restarts.
