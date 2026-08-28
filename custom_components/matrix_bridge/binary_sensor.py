"""Binary sensors for connection health and outstanding trust problems."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import MatrixBridgeConfigEntry
from .coordinator import MatrixBridgeCoordinator
from .entity import MatrixBridgeEntity


@dataclass(frozen=True, kw_only=True)
class MatrixBinarySensorDescription(BinarySensorEntityDescription):
    """Describes a Matrix Bridge binary sensor."""

    value_fn: Callable[[MatrixBridgeCoordinator], bool]
    # Connection health must stay readable while the bridge is down.
    available_when_disconnected: bool = False


BINARY_SENSORS: tuple[MatrixBinarySensorDescription, ...] = (
    MatrixBinarySensorDescription(
        key="connected",
        translation_key="connected",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        value_fn=lambda c: c.connected and bool(c.status.get("connected")),
        available_when_disconnected=True,
    ),
    MatrixBinarySensorDescription(
        key="unverified_devices_present",
        translation_key="unverified_devices_present",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda c: bool(c.status.get("unverified_device_count", 0)),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MatrixBridgeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Matrix Bridge binary sensors."""
    coordinator = entry.runtime_data
    async_add_entities(
        MatrixBridgeBinarySensor(coordinator, description)
        for description in BINARY_SENSORS
    )


class MatrixBridgeBinarySensor(MatrixBridgeEntity, BinarySensorEntity):
    """Boolean view of the bridge state."""

    entity_description: MatrixBinarySensorDescription

    def __init__(
        self,
        coordinator: MatrixBridgeCoordinator,
        description: MatrixBinarySensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{description.key}"

    @property
    def available(self) -> bool:
        if self.entity_description.available_when_disconnected:
            return True
        return super().available

    @property
    def is_on(self) -> bool:
        return self.entity_description.value_fn(self.coordinator)
