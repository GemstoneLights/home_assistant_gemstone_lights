"""The saved patterns, in one control beside the light's own effect list.

On the wire a ``pattern`` is a palette *and* an animation at once, so the two
combine rather than replace each other. The animations are the light's
``effect_list``, where Home Assistant expects a device's own effects to be; the
patterns someone saved here are this select, which is where core puts a stored,
user-authored bundle (WLED's Preset and Playlist selects are the precedent).
Together they read as what is playing:

    light effect   Fireworks    <- what the pixels are doing
    Pattern        Canada Day   <- whose colours they are doing it in

Picking a pattern replays it whole, animation included. Picking an effect on the
light changes only that, so the palette carries. See 4.7.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import build_pattern_replay_command
from .const import DOMAIN, signal_patterns_changed
from .coordinator import GemstoneCoordinator
from .entity import GemstoneEntity

if TYPE_CHECKING:
    from . import GemstoneConfigEntry

# The controller answers 503 when busy; one command at a time, as everywhere.
PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GemstoneConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add the saved-pattern picker, one per controller."""
    async_add_entities([GemstonePattern(entry.runtime_data, entry)])


class GemstonePattern(GemstoneEntity, SelectEntity):
    """The patterns kept for this controller."""

    _attr_translation_key = "pattern"

    def __init__(self, coordinator: GemstoneCoordinator, entry: GemstoneConfigEntry) -> None:
        """Register as a secondary entity of the controller's device.

        The key stays ``saved_pattern``, which it has always been: changing it
        would orphan the entity on every install that has one.
        """
        super().__init__(coordinator, entry, key="saved_pattern")

    async def async_added_to_hass(self) -> None:
        """Follow the saved list, which changes outside the coordinator's polling."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                signal_patterns_changed(self.coordinator.config_entry.entry_id),
                self._async_patterns_changed,
            )
        )

    @callback
    def _async_patterns_changed(self) -> None:
        """Rewrite the options after a save or a forget."""
        self.async_write_ha_state()

    @property
    def options(self) -> list[str]:
        """Every saved pattern, in the order they were saved."""
        return [pattern.name for pattern in self.coordinator.saved_patterns.all()]

    @property
    def current_option(self) -> str | None:
        """The saved pattern whose colours are on the lights.

        By name while it is playing untouched. After the animation is changed
        the controller reports the new animation's name instead, so the palette
        is what identifies it -- which is the point: the colours are still this
        pattern's, and the two controls together say so. Two patterns saved with
        identical colours cannot be told apart here; the first one wins.
        """
        playing = self.coordinator.data.pattern
        if playing is None:
            return None
        saved = self.coordinator.saved_patterns.all()
        name = playing.get("name")
        if isinstance(name, str) and any(pattern.name == name for pattern in saved):
            return name
        colors = playing.get("colors")
        if not isinstance(colors, list):
            return None
        return next((pattern.name for pattern in saved if pattern.pattern.get("colors") == colors), None)

    async def async_select_option(self, option: str) -> None:
        """Replay that pattern exactly as the controller reported it when saved."""
        saved = next((entry for entry in self.coordinator.saved_patterns.all() if entry.name == option), None)
        if saved is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_pattern",
                translation_placeholders={"name": option},
            )
        await self._async_send(self.coordinator.client.async_play(build_pattern_replay_command(saved.pattern)))
