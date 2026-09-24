"""Base entity: identity, device registration, and the one way commands are sent.

Every platform's entity inherits this, so they all hang off one device in the
registry and share the command path that adopts a controller reply as state.
"""

from __future__ import annotations

from collections.abc import Awaitable
from typing import TYPE_CHECKING

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import RGBW, CurrentlyPlaying, GemstoneError, parse_rgbw_int
from .const import CONF_SERIAL, DEFAULT_RGBW, DOMAIN, MANUFACTURER
from .coordinator import GemstoneCoordinator

if TYPE_CHECKING:
    from . import GemstoneConfigEntry


class GemstoneEntity(CoordinatorEntity[GemstoneCoordinator]):
    """Common identity for every entity of one controller.

    Identity is keyed on the config entry id, not the controller's address.
    The address is exactly what the reconfigure flow exists to change, and
    keying on it made every address change register a second entity and
    device while orphaning the first along with its history. The HTTP API
    exposes nothing more stable; the serial number, when discovery has learnt
    it, is shown on the device page but is not the identity, because an entry
    made by hand may never learn it. The entry id lives precisely as long as
    the entry does.
    """

    _attr_has_entity_name = True

    def __init__(self, coordinator: GemstoneCoordinator, entry: GemstoneConfigEntry, key: str | None = None) -> None:
        """Register under the entry's device; ``key`` distinguishes secondary entities."""
        super().__init__(coordinator)
        self._attr_unique_id = entry.entry_id if key is None else f"{entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            manufacturer=MANUFACTURER,
            model="Hub2",
            name=entry.title,
            serial_number=entry.data.get(CONF_SERIAL),
            sw_version=coordinator.hub_settings.firmware,
            # The client already knows how to address the controller; building a
            # second URL here is how the two drift apart.
            configuration_url=coordinator.client.base_url,
        )

    def _animation_colors(self, override: RGBW | None = None) -> list[RGBW]:
        """Colours for a new animation: the caller's, else what is already playing."""
        if override is not None:
            return [override]
        playing = self.coordinator.data
        if playing.pattern is not None:
            raw = playing.pattern.get("colors")
            if isinstance(raw, list):
                colors = [parse_rgbw_int(int(color)) for color in raw if isinstance(color, (int, float))]
                if colors:
                    return colors
        return [playing.rgbw or DEFAULT_RGBW]

    async def _async_play_animation(
        self,
        slug: str,
        display_name: str,
        *,
        colors: RGBW | None = None,
        brightness: int | None = None,
    ) -> None:
        """Play one of the controller's animations, keeping everything else as it is.

        Shared so the Pattern control and the play_pattern action send the same
        command: the same colours, the same brightness, and the speed and
        direction the companion number entities hold (a reported pattern keeps
        those current, so this is also "whatever is playing now").
        """
        playing = self.coordinator.data
        level = brightness if brightness is not None else playing.brightness
        await self._async_send(
            self.coordinator.client.async_play_pattern(
                slug,
                display_name,
                self._animation_colors(colors),
                255 if level is None else level,
                speed=self.coordinator.effect_speed,
            )
        )

    async def _async_send(self, command: Awaitable[CurrentlyPlaying]) -> None:
        """Send one command and adopt its reply as the new state."""
        try:
            result = await command
        except GemstoneError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        self.coordinator.async_apply_result(result)
