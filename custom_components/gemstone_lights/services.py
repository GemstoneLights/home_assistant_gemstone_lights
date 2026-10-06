"""Schemas for the integration's actions: play_pattern, save_playing and forget_pattern.

Kept apart from light.py, which registers them, so the entity stays readable.
Entity-service schemas are plain dicts rather than ``vol.Schema`` objects:
Home Assistant wraps a dict in its entity-target schema itself and deprecates
anything else. Bounds come from the API document's property table and match
what ``api.py``'s builders enforce, so a value that passes here cannot fail
there.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import VolDictType

from .animations import DIRECTIONS
from .api import MAX_COLORS, MAX_NAME_LENGTH
from .const import ANIMATION_SLUGS, ANIMATIONS

FIELD_ANIMATION = "animation"
FIELD_COLORS = "colors"
FIELD_BRIGHTNESS = "brightness"
FIELD_SPEED = "speed"
FIELD_DIRECTION = "direction"
FIELD_BACKGROUND_COLOR = "background_color"
FIELD_NAME = "name"

BYTE = vol.All(vol.Coerce(int), vol.Range(min=0, max=255))
# Speed starts at 1: 0 is outside what the animations accept.
SPEED = vol.All(vol.Coerce(int), vol.Range(min=1, max=255))

# ``[red, green, blue, warm]`` -- the same shape ``light.turn_on`` takes for
# ``rgbw_color``. Coerced to a tuple so it is the client's RGBW type.
RGBW_SCHEMA = vol.All(vol.ExactSequence([BYTE, BYTE, BYTE, BYTE]), vol.Coerce(tuple))

NAME_SCHEMA = vol.All(cv.string, vol.Length(min=1, max=MAX_NAME_LENGTH))


def animation_slug(value: Any) -> str:
    """Accept a wire slug or the effect picker's display name; return the slug.

    The slug is canonical: it is the stable wire value and what the select
    selector carries. Display names are accepted too, for parity with
    ``light.turn_on effect:``.
    """
    if isinstance(value, str):
        if value in ANIMATIONS:
            return value
        if value in ANIMATION_SLUGS:
            return ANIMATION_SLUGS[value]
    raise vol.Invalid(f"Unknown animation: {value!r}")


def direction_value(value: Any) -> int:
    """Accept a direction number or its name; return the number.

    Only these six exist. Whether the chosen one suits the animation is decided
    when the command is built, because that depends on which animation it is
    (animations.py).
    """
    if isinstance(value, str):
        for number, name in DIRECTIONS.items():
            if value == name:
                return number
        # The UI selector carries its options as strings.
        if value.isdigit():
            value = int(value)
    if isinstance(value, int) and not isinstance(value, bool) and value in DIRECTIONS:
        return value
    raise vol.Invalid(f"Unknown direction: {value!r}")


PLAY_PATTERN_SCHEMA: VolDictType = {
    vol.Required(FIELD_ANIMATION): animation_slug,
    vol.Required(FIELD_COLORS): vol.All(cv.ensure_list, [RGBW_SCHEMA], vol.Length(min=1, max=MAX_COLORS)),
    vol.Optional(FIELD_BRIGHTNESS): BYTE,
    vol.Optional(FIELD_SPEED): SPEED,
    vol.Optional(FIELD_DIRECTION): direction_value,
    vol.Optional(FIELD_BACKGROUND_COLOR): RGBW_SCHEMA,
    vol.Optional(FIELD_NAME): NAME_SCHEMA,
}

# Saving takes only a name, and even that is optional: a pattern the app made
# already carries one.
SAVE_PLAYING_SCHEMA: VolDictType = {vol.Optional(FIELD_NAME): NAME_SCHEMA}
# Forgetting names the pattern to drop: with one control rather than an entity
# each, there is nothing else to point at.
FORGET_PATTERN_SCHEMA: VolDictType = {vol.Required(FIELD_NAME): NAME_SCHEMA}
