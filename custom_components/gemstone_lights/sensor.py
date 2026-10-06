"""Diagnostic sensors read from hub settings: pixel count and firmware versions.

Hub settings are static between reboots; the coordinator re-reads them after an
outage and on a slow timer, and these entities show whatever it last read.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import StateType

from .api import HubSettings
from .coordinator import GemstoneCoordinator
from .entity import GemstoneEntity

if TYPE_CHECKING:
    from . import GemstoneConfigEntry

# Read-only platform: nothing here sends a command.
PARALLEL_UPDATES = 0

# The controller has four data outputs; hub settings report one count per output.
OUTPUTS = 4


@dataclass(frozen=True, kw_only=True)
class GemstoneSensorEntityDescription(SensorEntityDescription):
    """Describes one value read from hub settings."""

    value_fn: Callable[[HubSettings], StateType]
    attributes_fn: Callable[[HubSettings], dict[str, Any]] | None = None
    # Older firmware may omit a field. Then the entity is not created at all,
    # rather than existing and reading "unknown" forever.
    exists_fn: Callable[[HubSettings], bool] = lambda _: True


def _output_counts(settings: HubSettings) -> dict[str, Any]:
    """One attribute per data output, padded so the shape never changes."""
    counts = list(settings.pixel_count[:OUTPUTS])
    counts += [0] * (OUTPUTS - len(counts))
    return {f"output_{index}": count for index, count in enumerate(counts, start=1)}


SENSORS: tuple[GemstoneSensorEntityDescription, ...] = (
    # One sensor with the per-output counts as attributes, not four sensors:
    # the outputs are a fixed four-tuple of static configuration, and the
    # common install populates one, so three of four entities would be noise.
    # The total is the one thing that says how long the run is, which anyone
    # reaching for pixel numbers needs, so this one stays enabled.
    GemstoneSensorEntityDescription(
        key="pixel_count",
        translation_key="pixel_count",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda settings: sum(settings.pixel_count),
        attributes_fn=_output_counts,
        exists_fn=lambda settings: bool(settings.pixel_count),
    ),
    # Secondary firmware versions. The main firmware is the device's
    # sw_version; these are rarely needed, so they start disabled.
    GemstoneSensorEntityDescription(
        key="firmware_spi",
        translation_key="firmware_spi",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda settings: settings.firmware_spi,
        exists_fn=lambda settings: settings.firmware_spi is not None,
    ),
    GemstoneSensorEntityDescription(
        key="firmware_wifi",
        translation_key="firmware_wifi",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda settings: settings.firmware_wifi,
        exists_fn=lambda settings: settings.firmware_wifi is not None,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GemstoneConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add the diagnostic sensors this controller's settings support."""
    coordinator = entry.runtime_data
    async_add_entities(
        GemstoneSensor(coordinator, entry, description)
        for description in SENSORS
        if description.exists_fn(coordinator.hub_settings)
    )


class GemstoneSensor(GemstoneEntity, SensorEntity):
    """One value from hub settings, refreshed whenever the coordinator re-reads them."""

    entity_description: GemstoneSensorEntityDescription

    def __init__(
        self,
        coordinator: GemstoneCoordinator,
        entry: GemstoneConfigEntry,
        description: GemstoneSensorEntityDescription,
    ) -> None:
        """Register as a secondary entity keyed by the value it reads."""
        super().__init__(coordinator, entry, key=description.key)
        self.entity_description = description

    @property
    def native_value(self) -> StateType:
        """The value as of the coordinator's last hub-settings read."""
        return self.entity_description.value_fn(self.coordinator.hub_settings)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Per-output detail, where the description provides it."""
        if self.entity_description.attributes_fn is None:
            return None
        return self.entity_description.attributes_fn(self.coordinator.hub_settings)
