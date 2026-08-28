"""Shared base entity for the Matrix Bridge."""

from __future__ import annotations

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from .const import DOMAIN, SIGNAL_STATE_UPDATED
from .coordinator import MatrixBridgeCoordinator


class MatrixBridgeEntity(Entity):
    """Base class binding entities to the bridge connection state."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator: MatrixBridgeCoordinator) -> None:
        self.coordinator = coordinator
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.entry.entry_id)},
            # Entity ids are built from the device name, so the bot's Matrix ID
            # stays out of it: `notify.matrix_bridge_alerts` is something you
            # can find in the action picker and type in an automation, while
            # `notify.matrix_bridge_homeassistant_example_org_alerts` is not.
            # The account is on the device page instead.
            name="Matrix Bridge",
            manufacturer="Matrix",
            model="Matrix Bridge add-on",
            serial_number=coordinator.bot_user_id,
            configuration_url=(
                f"http://{coordinator.entry.data['host']}:"
                f"{coordinator.entry.data['port']}"
            ),
        )

    @property
    def available(self) -> bool:
        return self.coordinator.connected

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_STATE_UPDATED, self._handle_update
            )
        )

    # `@callback` is what tells HassJob this is loop-safe. Without it the
    # dispatcher treats the target as blocking and runs it in an executor
    # thread, and `async_write_ha_state` from off-loop is a hard error.
    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()
