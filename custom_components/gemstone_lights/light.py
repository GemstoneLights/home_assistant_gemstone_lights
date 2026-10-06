"""The light entity: `currentlyPlaying` in, a standard Home Assistant light out.

The component declares capabilities; Home Assistant renders the controls. The
colour wheel and sliders come from implementing this interface, not from any
frontend code. How the modules fit together is in docs/design.md.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Final

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_EFFECT,
    ATTR_RGBW_COLOR,
    EFFECT_OFF,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.components.light import (
    DOMAIN as LIGHT_DOMAIN,
)
from homeassistant.core import HomeAssistant, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_platform
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import (
    RGBW,
    build_architectural_replay_command,
    build_pattern_command,
    build_pattern_replay_command,
    rgbw_to_int,
)
from .const import (
    ANIMATION_SLUGS,
    ANIMATIONS,
    DEFAULT_BACKGROUND_COLOR,
    DEFAULT_RGBW,
    DOMAIN,
    EFFECT_LIST,
    SERVICE_FORGET_PATTERN,
    SERVICE_PLAY_PATTERN,
    SERVICE_SAVE_PLAYING,
    signal_patterns_changed,
)
from .coordinator import GemstoneCoordinator
from .entity import GemstoneEntity
from .palette import DEFAULT_FAVORITE_COLORS, to_controller, to_display
from .services import FORGET_PATTERN_SCHEMA, PLAY_PATTERN_SCHEMA, SAVE_PLAYING_SCHEMA

if TYPE_CHECKING:
    from . import GemstoneConfigEntry

# The controller serves HTTP from the chip that drives the pixels and answers
# 503 when busy; the client already serialises every request through one lock,
# and this declares the same thing to Home Assistant for the action platform.
PARALLEL_UPDATES = 1

# ``off`` first, which is core's convention for "no effect" on a light that has
# them: the controller has no stop command, so picking it plays a plain colour.
EFFECT_OPTIONS: Final = [EFFECT_OFF, *EFFECT_LIST]


def _as_rgbw(value: Sequence[int]) -> RGBW:
    """Narrow a validated four-item sequence to the client's colour type."""
    return (int(value[0]), int(value[1]), int(value[2]), int(value[3]))


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GemstoneConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """One light entity per controller; the API has no per-output control."""
    async_add_entities([GemstoneLight(entry.runtime_data, entry)])

    # Entity services target the light, so they are registered with the
    # platform rather than in async_setup; the platform skips re-registration
    # when a second controller is added.
    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(SERVICE_PLAY_PATTERN, PLAY_PATTERN_SCHEMA, "async_handle_play_pattern")
    platform.async_register_entity_service(
        SERVICE_SAVE_PLAYING,
        SAVE_PLAYING_SCHEMA,
        "async_handle_save_playing",
        supports_response=SupportsResponse.OPTIONAL,
    )
    platform.async_register_entity_service(SERVICE_FORGET_PATTERN, FORGET_PATTERN_SCHEMA, "async_handle_forget_pattern")


class GemstoneLight(GemstoneEntity, LightEntity):
    """A Hub2 controller presented as an RGBW light."""

    # The primary entity of the device, so it takes the device's name.
    _attr_name = None
    _attr_color_mode = ColorMode.RGBW
    _attr_supported_features = LightEntityFeature.EFFECT
    _attr_effect_list = EFFECT_OPTIONS

    def __init__(self, coordinator: GemstoneCoordinator, entry: GemstoneConfigEntry) -> None:
        """Register as the device's primary entity, so no key suffix."""
        super().__init__(coordinator, entry)
        # Set per instance: the base class declares this as an instance
        # attribute, and a mutable class-level set trips both mypy and ruff.
        self._attr_supported_color_modes = {ColorMode.RGBW}

    async def async_added_to_hass(self) -> None:
        """Seed the colour picker with the Gemstone app's swatches, once.

        Home Assistant's defaults for an RGBW light are four warm whites, which
        are not the colours this ecosystem uses; the app's own palette is a
        better starting point. Written only when the entity has no favourites
        of its own, so an edited list survives, and so does an emptied one.
        """
        await super().async_added_to_hass()
        registry = er.async_get(self.hass)
        entry = registry.async_get(self.entity_id)
        if entry is None:
            return
        options = entry.options.get(LIGHT_DOMAIN, {})
        if "favorite_colors" in options:
            return
        registry.async_update_entity_options(
            self.entity_id,
            LIGHT_DOMAIN,
            {**options, "favorite_colors": [dict(payload) for _, payload in DEFAULT_FAVORITE_COLORS]},
        )

    @property
    def is_on(self) -> bool:
        """Power, straight from `onState`."""
        return self.coordinator.data.on_state

    @property
    def brightness(self) -> int | None:
        """Brightness 0-255; the controller's range matches HA's exactly."""
        return self.coordinator.data.brightness

    @property
    def rgbw_color(self) -> RGBW | None:
        """Full-scale hue; the parser separates it from brightness.

        Reported as the colour Home Assistant draws rather than the one the
        controller sent, so a palette colour highlights its own swatch while it
        plays (palette.py).
        """
        rgbw = self.coordinator.data.rgbw
        return None if rgbw is None else to_display(rgbw)

    @property
    def effect(self) -> str | None:
        """Display name of the running animation, or ``off`` when none is.

        Core asks a light that supports effects to report ``EFFECT_OFF`` when
        nothing is rendered. An animation newer firmware knows and this does not
        reads as ``None`` instead, because "off" would be a lie.
        """
        slug = self.coordinator.data.animation
        if not slug:
            return EFFECT_OFF
        return ANIMATIONS.get(slug)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """What kind of thing is playing, and what it is called.

        Two scalars, deliberately: ``playing`` is what an automation conditions
        on ("only while a design is playing"), and ``playing_name`` is the name
        the app gave it, which the effect name does not tell you -- a pattern
        called "Canada Day Custom" reads as "Chase" there. The full pattern or
        design object stays out of the state machine.

        Not called ``scene``, though that is the API's word for it: in Home
        Assistant a scene is an entity type, so the word is left to mean only
        that.
        """
        playing = self.coordinator.data
        source: dict[str, Any] | None
        if playing.scene == "pattern":
            source = playing.pattern
        elif playing.scene == "architectural":
            source = playing.architectural
        elif playing.scene in ("playlist", "impulse"):
            raw = playing.raw.get(playing.scene)
            source = raw if isinstance(raw, dict) else None
        else:
            source = None
        name = source.get("name") if source else None
        return {"playing": playing.scene, "playing_name": name if isinstance(name, str) else None}

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Map HA's turn_on to the narrowest controller command that fits."""
        playing = self.coordinator.data
        client = self.coordinator.client
        picked: RGBW | None = kwargs.get(ATTR_RGBW_COLOR)
        # Translated once, here: every branch below sends controller values
        # (palette.py).
        rgbw: RGBW | None = None if picked is None else to_controller(_as_rgbw(picked))
        brightness: int | None = kwargs.get(ATTR_BRIGHTNESS)
        effect: str | None = kwargs.get(ATTR_EFFECT)

        if effect == EFFECT_OFF:
            # The API has no "stop": a plain colour is what stops an animation,
            # and the power toggle below would keep it running.
            plain = _as_rgbw(rgbw) if rgbw is not None else (playing.rgbw or DEFAULT_RGBW)
            level = brightness if brightness is not None else playing.brightness
            await self._async_send(client.async_play_color(plain, 255 if level is None else level))
        elif effect is not None:
            slug = ANIMATION_SLUGS.get(effect)
            if slug is None:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="unknown_effect",
                    translation_placeholders={"effect": effect},
                )
            await self._async_play_animation(
                slug, effect, colors=_as_rgbw(rgbw) if rgbw is not None else None, brightness=brightness
            )
        elif rgbw is None and brightness is None:
            # The documented scene-preserving power toggle (API section 3.4).
            await self._async_send(client.async_set_on_state(True))
        elif rgbw is None and playing.pattern is not None:
            # Brightness slider while an animation plays: keep the animation.
            await self._async_send(client.async_play(build_pattern_replay_command(playing.pattern, brightness)))
        elif rgbw is None and playing.architectural is not None:
            # Brightness slider while a design plays: keep every pixel's colour.
            # A colour pick, by contrast, falls through and replaces the design;
            # that is what picking one colour for the whole run means.
            await self._async_send(
                client.async_play(build_architectural_replay_command(playing.architectural, brightness))
            )
        else:
            hue: RGBW = _as_rgbw(rgbw) if rgbw is not None else (playing.rgbw or DEFAULT_RGBW)
            level = brightness if brightness is not None else playing.brightness
            await self._async_send(client.async_play_color(hue, 255 if level is None else level))

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Power off without disturbing the running scene."""
        await self._async_send(self.coordinator.client.async_set_on_state(False))

    async def async_handle_play_pattern(
        self,
        *,
        animation: str,
        colors: list[RGBW],
        brightness: int | None = None,
        speed: int | None = None,
        direction: int | None = None,
        background_color: RGBW | None = None,
        name: str | None = None,
    ) -> None:
        """Play an animation with every parameter the API accepts (the play_pattern action).

        Anything not given falls back to what the Pattern control would send:
        the companion number entities for speed and direction, the current
        brightness, the animation's own display name.
        """
        level = brightness if brightness is not None else self.coordinator.data.brightness
        try:
            command = build_pattern_command(
                animation,
                name if name is not None else ANIMATIONS[animation],
                [to_controller(color) for color in colors],
                255 if level is None else level,
                speed=speed if speed is not None else self.coordinator.effect_speed,
                direction=direction,
                background_color=(
                    rgbw_to_int(*to_controller(background_color))
                    if background_color is not None
                    else DEFAULT_BACKGROUND_COLOR
                ),
            )
        except ValueError as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="invalid_scene",
                translation_placeholders={"reason": str(err)},
            ) from err
        await self._async_send(self.coordinator.client.async_play(command))

    async def async_handle_save_playing(self, *, name: str | None = None) -> ServiceResponse:
        """Keep the pattern that is playing, to replay later (the save_playing action).

        The controller cannot save anything (store.py), so the pattern is kept
        here exactly as it was reported and joins the Pattern control a
        moment later. Saving under a name already used replaces that pattern
        rather than leaving two entries that look alike.
        """
        pattern = self.coordinator.data.pattern
        if pattern is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="no_pattern_playing",
            )
        reported = pattern.get("name")
        chosen = name or (
            reported
            if isinstance(reported, str) and reported
            else ANIMATIONS.get(str(pattern.get("animation")), "Saved pattern")
        )
        saved = await self.coordinator.saved_patterns.async_save(chosen, pattern)
        async_dispatcher_send(self.hass, signal_patterns_changed(self.coordinator.config_entry.entry_id))
        return {"name": saved.name, "slug": saved.slug}

    async def async_handle_forget_pattern(self, *, name: str) -> None:
        """Drop a saved pattern (the forget_pattern action).

        By name, because the patterns live in one control rather than an entity
        each, so there is nothing else to point at. Forgetting one that is not
        there says so rather than passing silently: the name was probably a typo.
        """
        saved = next((entry for entry in self.coordinator.saved_patterns.all() if entry.name == name), None)
        if saved is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_pattern",
                translation_placeholders={"name": name},
            )
        await self.coordinator.saved_patterns.async_forget(saved.slug)
        async_dispatcher_send(self.hass, signal_patterns_changed(self.coordinator.config_entry.entry_id))
