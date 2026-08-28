"""Sensors describing the bridge and the most recent inbound message."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import MatrixBridgeConfigEntry
from .coordinator import MatrixBridgeCoordinator
from .entity import MatrixBridgeEntity


@dataclass(frozen=True, kw_only=True)
class MatrixSensorDescription(SensorEntityDescription):
    """Describes a Matrix Bridge sensor."""

    value_fn: Callable[[MatrixBridgeCoordinator], Any]
    attributes_fn: Callable[[MatrixBridgeCoordinator], dict[str, Any]] | None = None


def _last_message_value(coordinator: MatrixBridgeCoordinator) -> str | None:
    message = coordinator.last_message
    return message.get("sender") if message else None


def _last_message_attributes(
    coordinator: MatrixBridgeCoordinator,
) -> dict[str, Any]:
    message = coordinator.last_message or {}
    return {
        "body": message.get("body"),
        "room_id": message.get("room_id"),
        "room_name": message.get("room_name"),
        "sender_name": message.get("sender_name"),
        "event_id": message.get("event_id"),
        "local_path": message.get("local_path"),
        "encrypted": message.get("encrypted"),
    }


SENSORS: tuple[MatrixSensorDescription, ...] = (
    MatrixSensorDescription(
        key="rooms",
        translation_key="rooms",
        icon="mdi:forum",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="rooms",
        value_fn=lambda c: len(c.rooms),
    ),
    MatrixSensorDescription(
        key="unverified_devices",
        translation_key="unverified_devices",
        icon="mdi:shield-alert",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="devices",
        value_fn=lambda c: c.status.get("unverified_device_count", 0),
    ),
    MatrixSensorDescription(
        key="pending_verifications",
        translation_key="pending_verifications",
        icon="mdi:account-check",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda c: c.status.get("pending_verifications", 0),
    ),
    MatrixSensorDescription(
        key="last_message",
        translation_key="last_message",
        icon="mdi:message-text",
        value_fn=_last_message_value,
        attributes_fn=_last_message_attributes,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MatrixBridgeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Matrix Bridge sensors."""
    coordinator = entry.runtime_data
    async_add_entities(
        MatrixBridgeSensor(coordinator, description) for description in SENSORS
    )


class MatrixBridgeSensor(MatrixBridgeEntity, SensorEntity):
    """A single derived value from the bridge status."""

    entity_description: MatrixSensorDescription

    def __init__(
        self,
        coordinator: MatrixBridgeCoordinator,
        description: MatrixSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{description.key}"

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attributes_fn is None:
            return None
        return self.entity_description.attributes_fn(self.coordinator)
