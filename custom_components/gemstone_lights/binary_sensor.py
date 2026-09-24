"""Diagnostic binary sensor read from hub settings: the local TCP server flag."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.components.binary_sensor import BinarySensorEntity, BinarySensorEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import HubSettings
from .coordinator import GemstoneCoordinator
from .entity import GemstoneEntity

if TYPE_CHECKING:
    from . import GemstoneConfigEntry

# Read-only platform: nothing here sends a command.
PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class GemstoneBinarySensorEntityDescription(BinarySensorEntityDescription):
    """Describes one flag read from hub settings."""

    is_on_fn: Callable[[HubSettings], bool | None]
    exists_fn: Callable[[HubSettings], bool] = lambda _: True


BINARY_SENSORS: tuple[GemstoneBinarySensorEntityDescription, ...] = (
    # No device class on purpose: CONNECTIVITY would render "Connected", which
    # misreads a feature flag as link state. The document never defines the
    # field, so the name stays literal. Almost certainly always on while this
    # integration can read it at all, which is why it starts disabled.
    GemstoneBinarySensorEntityDescription(
        key="tcp_enabled",
        translation_key="tcp_enabled",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        is_on_fn=lambda settings: settings.tcp_enabled,
        exists_fn=lambda settings: settings.tcp_enabled is not None,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GemstoneConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add the diagnostic flags this controller's settings support."""
    coordinator = entry.runtime_data
    async_add_entities(
        GemstoneBinarySensor(coordinator, entry, description)
        for description in BINARY_SENSORS
        if description.exists_fn(coordinator.hub_settings)
    )


class GemstoneBinarySensor(GemstoneEntity, BinarySensorEntity):
    """One flag from hub settings, refreshed whenever the coordinator re-reads them."""

    entity_description: GemstoneBinarySensorEntityDescription

    def __init__(
        self,
        coordinator: GemstoneCoordinator,
        entry: GemstoneConfigEntry,
        description: GemstoneBinarySensorEntityDescription,
    ) -> None:
        """Register as a secondary entity keyed by the flag it reads."""
        super().__init__(coordinator, entry, key=description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        """The flag as of the coordinator's last hub-settings read."""
        return self.entity_description.is_on_fn(self.coordinator.hub_settings)
