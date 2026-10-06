"""The Gemstone app's palette, and the translation it needs to survive a screen.

Home Assistant paints a favourite from the payload it will send, so a swatch
cannot show one colour and send another: the frontend reads the swatch off the
payload's single colour key, and `light.turn_on` marks every colour key mutually
exclusive. Left alone, the controller's own values draw wrong. Its Orange reads
red on a screen and its Yellow reads orange, because they are tuned for LEDs;
its three whites flatten into one, because a screen has three channels and the
roofline has four.

The app has the same problem and solves it in its picker: `buildFillColor`
paints Yellow #FFFF00, Orange #FFA500, Pink #FF69B4 and the whites as pale
tints, while sending the LED-tuned value underneath. Home Assistant offers no
such hook, so the split lives here: it holds the colour that looks right, and
`light.py` translates at the four places a colour crosses to the controller.

The order is the app's own: `gemstoneManagedSwatches` inserts the whites at
index 0, ahead of Off.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from .api import RGBW

# How far a channel may drift and still be the same colour; see to_display.
_TOLERANCE: Final = 3


@dataclass(frozen=True)
class PaletteEntry:
    """One swatch: what Home Assistant shows, and what the hardware is sent."""

    name: str
    display: RGBW
    controller: RGBW


GEMSTONE_PALETTE: Final[tuple[PaletteEntry, ...]] = (
    PaletteEntry("Warm White", (253, 244, 188, 0), (0, 0, 0, 255)),
    PaletteEntry("Cool White", (244, 253, 255, 0), (255, 255, 255, 0)),
    PaletteEntry("Bright White", (255, 255, 255, 0), (255, 255, 255, 255)),
    PaletteEntry("Off", (0, 0, 0, 0), (0, 0, 0, 0)),
    PaletteEntry("Red", (255, 0, 0, 0), (255, 0, 0, 0)),
    PaletteEntry("Orange", (255, 165, 0, 0), (255, 30, 0, 0)),
    PaletteEntry("Yellow", (255, 255, 0, 0), (255, 87, 0, 0)),
    PaletteEntry("Green", (0, 255, 0, 0), (0, 255, 0, 0)),
    PaletteEntry("Aqua", (0, 217, 255, 0), (0, 217, 255, 0)),
    PaletteEntry("Blue", (0, 0, 255, 0), (0, 0, 255, 0)),
    PaletteEntry("Purple", (132, 0, 255, 0), (132, 0, 255, 0)),
    PaletteEntry("Pink", (255, 105, 180, 0), (255, 0, 122, 0)),
)

# Seeded into the light's colour picker, because Home Assistant's own defaults
# for an RGBW light are four warm whites that have nothing to do with this
# ecosystem. Built from the rows above so the picker and the translation cannot
# drift apart. Each is a turn_on payload, which is what a favourite stores.
DEFAULT_FAVORITE_COLORS: Final[tuple[tuple[str, dict[str, Any]], ...]] = tuple(
    (entry.name, {"rgbw_color": list(entry.display)}) for entry in GEMSTONE_PALETTE
)

_TO_CONTROLLER: Final[dict[RGBW, RGBW]] = {entry.display: entry.controller for entry in GEMSTONE_PALETTE}


def to_controller(rgbw: RGBW) -> RGBW:
    """Translate a colour Home Assistant sent into the one the controller means.

    An exact match, because these arrive back from Home Assistant exactly as the
    favourite stored them. Any other colour is one the user picked themselves
    and goes to the controller untouched.
    """
    return _TO_CONTROLLER.get(rgbw, rgbw)


def to_display(rgbw: RGBW) -> RGBW:
    """Translate a colour the controller reported into the one to show.

    Matched within a few counts per channel, because `api.full_scale_rgbw`
    scales a dimmed legacy colour back up and rounds on the way: a yellow can
    return as (255, 85, 0, 0). The tolerance is far narrower than the gap
    between any two palette colours -- the closest pair, Red and Orange, is 30
    apart -- and it holds down to about 12% brightness. Below that the colour
    that identified the swatch is genuinely gone (a yellow dimmed to 1 is
    (1, 0, 0, 0), which is red), so it is shown as reported and no swatch
    highlights, which is better than guessing.
    """
    for entry in GEMSTONE_PALETTE:
        if all(abs(reported - known) <= _TOLERANCE for reported, known in zip(rgbw, entry.controller, strict=True)):
            return entry.display
    return rgbw
