"use strict";

// All URLs are relative so the page works unchanged under the Ingress path
// prefix that Supervisor assigns at runtime.
const state = { rooms: [], devices: [], verifications: [], settingsShown: false };

const $ = (id) => document.getElementById(id);

function toast(message, isError) {
  const el = $("toast");
  el.textContent = message;
  el.style.background = isError ? "var(--err)" : "";
  el.style.color = isError ? "#fff" : "";
  el.classList.add("show");
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => el.classList.remove("show"), 4000);
}

async function api(path, options) {
  const response = await fetch("api/" + path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const text = await response.text();
  const body = text ? JSON.parse(text) : {};
  if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
  return body;
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

// ---------------------------------------------------------------- rendering

function renderStatus(status) {
  const pill = $("conn");
  if (status.connected) {
    pill.textContent = "connected";
    pill.className = "pill pill--ok";
  } else if (status.configured === false) {
    pill.textContent = "not set up";
    pill.className = "pill pill--muted";
  } else {
    pill.textContent = "disconnected";
    pill.className = "pill pill--err";
  }

  // Open the settings on arrival when there is nothing to show yet, but only
  // once: closing them again must stick while events keep arriving.
  if (status.configured === false && !state.settingsShown) {
    state.settingsShown = true;
    $("settings-details").open = true;
  }

  const rows = [
    ["Account", status.user_id || "—"],
    ["Device", status.device_id || "—"],
    ["Homeserver", status.homeserver || "—"],
    ["Encryption", status.encryption_enabled ? "enabled" : "disabled"],
    ["Rooms", status.room_count],
    ["Unverified devices", status.unverified_device_count],
    ["Send to unverified rooms", status.send_to_unverified_rooms ? "allowed" : "blocked"],
    ["Ignore unverified devices", status.ignore_unverified_devices ? "yes" : "no"],
  ];

  const list = $("status-list");
  list.replaceChildren();
  for (const [key, value] of rows) {
    list.append(el("dt", null, key), el("dd", null, String(value)));
  }

  const error = $("status-error");
  error.hidden = !status.last_error;
  error.textContent = status.last_error || "";
}

function renderRooms(rooms) {
  state.rooms = rooms;
  $("room-count").textContent = rooms.length ? `(${rooms.length})` : "";

  const list = $("room-list");
  list.replaceChildren();
  if (!rooms.length) {
    list.className = "muted";
    list.textContent = "Not in any rooms yet. Invite the bot from your Matrix client.";
    return;
  }
  list.className = "";

  for (const room of rooms) {
    const row = el("div", "row");
    const main = el("div", "row__main");
    main.append(el("div", "row__title", room.name));
    main.append(el("div", "row__sub", room.alias || room.room_id));

    const tags = el("div", "tags");
    tags.append(el("span", "tag", `${room.member_count} members`));
    if (room.encrypted) tags.append(el("span", "tag tag--ok", "encrypted"));
    if (room.unverified_device_count > 0) {
      tags.append(
        el("span", "tag tag--warn", `${room.unverified_device_count} unverified`)
      );
    }
    main.append(tags);

    const actions = el("div", "actions");
    const test = el("button", null, "Send test");
    test.onclick = () => sendTest(room);
    const leave = el("button", "danger", "Leave");
    leave.onclick = () => leaveRoom(room);
    actions.append(test, leave);

    row.append(main, actions);
    list.append(row);
  }
}

function renderDevices(devices) {
  state.devices = devices;
  $("device-count").textContent = devices.length ? `(${devices.length})` : "";

  const list = $("device-list");
  list.replaceChildren();
  if (!devices.length) {
    list.className = "muted";
    list.textContent = "No devices known yet.";
    return;
  }
  list.className = "";

  for (const device of devices) {
    const row = el("div", "row");
    const main = el("div", "row__main");
    main.append(el("div", "row__title", device.display_name || device.device_id));
    main.append(el("div", "row__sub", `${device.user_id} · ${device.device_id}`));

    const tags = el("div", "tags");
    if (device.verified) tags.append(el("span", "tag tag--ok", "verified"));
    else tags.append(el("span", "tag tag--warn", "unverified"));
    if (device.blacklisted) tags.append(el("span", "tag tag--warn", "blacklisted"));
    main.append(tags);

    const actions = el("div", "actions");
    const start = el("button", "primary", "Verify with emoji");
    start.onclick = () => startVerification(device);
    actions.append(start);

    for (const action of ["verify", "unverify", "blacklist"]) {
      const button = el("button", action === "blacklist" ? "danger" : null, action);
      button.onclick = () => setTrust(device, action);
      actions.append(button);
    }

    row.append(main, actions);
    list.append(row);
  }
}

function renderVerifications(list) {
  state.verifications = list;
  const container = $("verify-list");
  container.replaceChildren();
  $("verify-empty").hidden = list.length > 0;

  for (const item of list) {
    const card = el("div", "verify");
    card.append(el("div", "row__title", `${item.user_id} · ${item.device_id}`));
    card.append(
      el("div", "row__sub", `${item.stage}${item.we_started ? " (we started)" : ""}`)
    );

    if (item.emoji && item.emoji.length) {
      const strip = el("div", "emoji");
      for (const entry of item.emoji) {
        const figure = document.createElement("figure");
        figure.append(el("div", "glyph", entry.emoji));
        const caption = document.createElement("figcaption");
        caption.textContent = entry.name;
        figure.append(caption);
        strip.append(figure);
      }
      card.append(strip);
    }

    const actions = el("div", "actions");
    if (item.stage === "requested" && !item.we_started) {
      const accept = el("button", "primary", "Accept");
      accept.onclick = () => verificationAction(item, "accept");
      actions.append(accept);
    }
    if (item.emoji && item.emoji.length) {
      const confirm = el("button", "primary", "They match");
      confirm.onclick = () => verificationAction(item, "confirm");
      actions.append(confirm);
    }
    const cancel = el("button", "danger", "Cancel");
    cancel.onclick = () => verificationAction(item, "cancel");
    actions.append(cancel);

    card.append(actions);
    container.append(card);
  }
}

// ----------------------------------------------------------------- settings

// The form is generated from the field definitions the add-on serves, so a
// setting added in settings.py appears here without touching this file.

function readPath(data, path) {
  return path.split(".").reduce((node, key) => (node == null ? node : node[key]), data);
}

function writePath(data, path, value) {
  const parts = path.split(".");
  let node = data;
  for (const part of parts.slice(0, -1)) node = node[part] ??= {};
  node[parts.at(-1)] = value;
}

function renderField(field, value) {
  const wrap = el("div", `field field--${field.kind}`);
  const label = el("label", null, field.label);
  label.htmlFor = `f:${field.path}`;

  let input;
  if (field.kind === "select") {
    input = document.createElement("select");
    for (const choice of field.choices || []) {
      const option = document.createElement("option");
      option.value = choice;
      option.textContent = choice;
      input.append(option);
    }
    input.value = value ?? "";
  } else if (field.kind === "list") {
    input = document.createElement("textarea");
    input.rows = Math.max(2, (value || []).length + 1);
    input.value = (value || []).join("\n");
  } else {
    input = document.createElement("input");
    if (field.kind === "bool") {
      input.type = "checkbox";
      input.checked = Boolean(value);
    } else if (field.kind === "int") {
      input.type = "number";
      if (field.minimum !== undefined) input.min = field.minimum;
      if (field.maximum !== undefined) input.max = field.maximum;
      input.value = value ?? "";
    } else if (field.kind === "password") {
      input.type = "password";
      input.value = value ?? "";
      // Marked rather than found by type: revealing changes the type, and the
      // toggle has to be able to find them again to hide them.
      input.dataset.secret = "1";
    } else {
      input.type = "text";
      input.value = value ?? "";
    }
  }

  if (field.placeholder && input.tagName !== "SELECT") {
    input.placeholder = field.placeholder;
  }
  input.id = `f:${field.path}`;
  input.dataset.path = field.path;
  input.dataset.kind = field.kind;

  if (field.kind === "bool") wrap.append(input, label);
  else wrap.append(label, input);
  if (field.help) wrap.append(el("p", "hint", field.help));
  return wrap;
}

function renderSettings(payload) {
  const form = $("settings-form");
  form.replaceChildren();

  for (const section of payload.form.sections) {
    const fieldset = document.createElement("fieldset");
    fieldset.append(el("legend", null, section.title));
    if (section.help) fieldset.append(el("p", "hint", section.help));
    for (const field of payload.form.fields) {
      if (field.section === section.key) {
        fieldset.append(renderField(field, readPath(payload.settings, field.path)));
      }
    }
    form.append(fieldset);
  }

  if (payload.api_token) {
    const fieldset = document.createElement("fieldset");
    fieldset.append(el("legend", null, "API token"));
    fieldset.append(
      el(
        "p",
        "hint",
        "Generated by the add-on. The integration reads it through Supervisor; " +
          "you only need it to call the API yourself."
      )
    );
    const token = el("input");
    token.type = "password";
    token.readOnly = true;
    token.value = payload.api_token;
    token.dataset.secret = "1";
    fieldset.append(token);
    form.append(fieldset);
  }

  applyReveal();
  $("settings-summary").textContent = payload.configured
    ? "Everything is configured here"
    : "Not set up yet — start here";
}

function collectSettings() {
  const data = {};
  for (const input of document.querySelectorAll("#settings-form [data-path]")) {
    let value;
    if (input.dataset.kind === "bool") value = input.checked;
    else if (input.dataset.kind === "list") value = input.value;
    else value = input.value;
    writePath(data, input.dataset.path, value);
  }
  return data;
}

function applyReveal() {
  const show = $("reveal-secrets").checked;
  for (const input of document.querySelectorAll("#settings-form [data-secret]")) {
    input.type = show ? "text" : "password";
  }
}

async function loadSettings() {
  try {
    renderSettings(await api("settings"));
  } catch (err) {
    $("settings-form").replaceChildren(el("p", "error", err.message));
  }
}

async function saveSettings() {
  const button = $("settings-save");
  const note = $("settings-note");
  button.disabled = true;
  note.className = "muted";
  note.textContent = "Saving and reconnecting…";

  try {
    const result = await api("settings", {
      method: "POST",
      body: JSON.stringify(collectSettings()),
    });
    renderSettings(result);
    if (result.status) renderStatus(result.status);

    const failure = result.status && result.status.last_error;
    note.textContent = "";
    toast(failure ? `Saved, but: ${failure}` : "Saved and reconnected", Boolean(failure));
    refresh();
  } catch (err) {
    note.className = "error";
    note.textContent = err.message;
    toast(err.message, true);
  } finally {
    button.disabled = false;
  }
}

// ------------------------------------------------------------------ actions

async function sendTest(room) {
  try {
    await api(`rooms/${encodeURIComponent(room.room_id)}/message`, {
      method: "POST",
      body: JSON.stringify({ body: "Test message from Home Assistant." }),
    });
    toast(`Sent to ${room.name}`);
  } catch (err) {
    toast(err.message, true);
  }
}

async function leaveRoom(room) {
  if (!window.confirm(`Leave ${room.name}?`)) return;
  try {
    await api(`rooms/${encodeURIComponent(room.room_id)}/leave`, { method: "POST" });
    toast(`Left ${room.name}`);
    refresh();
  } catch (err) {
    toast(err.message, true);
  }
}

async function startVerification(device) {
  try {
    await api("verifications/start", {
      method: "POST",
      body: JSON.stringify({ user_id: device.user_id, device_id: device.device_id }),
    });
    toast("Verification started — confirm the emoji in your Matrix client");
  } catch (err) {
    toast(err.message, true);
  }
}

async function setTrust(device, action) {
  try {
    await api(
      `devices/${encodeURIComponent(device.user_id)}/${encodeURIComponent(
        device.device_id
      )}/${action}`,
      { method: "POST" }
    );
    toast(`${device.device_id}: ${action}`);
    refresh();
  } catch (err) {
    toast(err.message, true);
  }
}

async function verificationAction(item, action) {
  try {
    await api(`verifications/${encodeURIComponent(item.transaction_id)}/${action}`, {
      method: "POST",
      body: JSON.stringify({}),
    });
    toast(`Verification ${action}ed`);
  } catch (err) {
    toast(err.message, true);
  }
}

// -------------------------------------------------------------- data plumbing

async function refresh() {
  // Settled rather than all: the status card carries `last_error`, which is
  // exactly what is worth reading when one of the other reads is failing.
  const results = await Promise.allSettled([
    api("status"),
    api("rooms"),
    api("devices"),
    api("verifications"),
  ]);
  const [status, rooms, devices, verifications] = results;

  if (status.status === "fulfilled") renderStatus(status.value);
  if (rooms.status === "fulfilled") renderRooms(rooms.value.rooms || []);
  if (devices.status === "fulfilled") renderDevices(devices.value.devices || []);
  if (verifications.status === "fulfilled") {
    renderVerifications(verifications.value.verifications || []);
  }

  const failed = results.find((result) => result.status === "rejected");
  if (failed) toast(failed.reason.message, true);
}

function connect() {
  const url = new URL("api/ws", window.location.href);
  url.protocol = url.protocol.replace("http", "ws");

  const socket = new WebSocket(url);

  socket.onmessage = (message) => {
    const event = JSON.parse(message.data);
    if (event.type === "status") renderStatus(event.data);
    else if (event.type === "rooms_changed") {
      if (event.data.rooms) renderRooms(event.data.rooms);
      else refresh();
    } else if (event.type === "verification") {
      // The bus sends one verification at a time; re-read the full set so the
      // panel always reflects server-side truth.
      api("verifications")
        .then((data) => renderVerifications(data.verifications || []))
        .catch(() => {});
      refresh();
    } else if (event.type === "message" || event.type === "image") {
      toast(`${event.data.sender_name}: ${event.data.body || "(attachment)"}`);
    } else if (event.type === "error") {
      toast(event.data.message, true);
    }
  };

  socket.onclose = () => {
    $("conn").textContent = "reconnecting…";
    $("conn").className = "pill pill--muted";
    setTimeout(connect, 3000);
  };
}

$("settings-save").addEventListener("click", saveSettings);
$("reveal-secrets").addEventListener("change", applyReveal);

refresh();
loadSettings();
connect();
