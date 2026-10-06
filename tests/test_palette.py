"""Palette tests: the app's colours on screen, the controller's on the wire."""

import pytest

from custom_components.gemstone_lights.api import RGBW, full_scale_rgbw, rgbw_to_int
from custom_components.gemstone_lights.palette import (
    DEFAULT_FAVORITE_COLORS,
    GEMSTONE_PALETTE,
    to_controller,
    to_display,
)


@pytest.mark.parametrize("entry", GEMSTONE_PALETTE, ids=lambda entry: entry.name)
def test_every_swatch_translates_both_ways(entry) -> None:
    """What the picker holds reaches the controller, and what it reports comes back."""
    assert to_controller(entry.display) == entry.controller
    assert to_display(entry.controller) == entry.display


def test_the_favourites_are_the_palette_in_order() -> None:
    """One source for both, so the picker and the translation cannot drift."""
    assert [name for name, _ in DEFAULT_FAVORITE_COLORS] == [entry.name for entry in GEMSTONE_PALETTE]
    assert [payload["rgbw_color"] for _, payload in DEFAULT_FAVORITE_COLORS] == [
        list(entry.display) for entry in GEMSTONE_PALETTE
    ]


@pytest.mark.parametrize("rgbw", [(1, 2, 3, 4), (18, 52, 86, 120), (35, 123, 192, 0)])
def test_a_colour_of_your_own_is_left_alone(rgbw: RGBW) -> None:
    """Only the palette is translated; anything else means exactly what it says."""
    assert to_controller(rgbw) == rgbw
    assert to_display(rgbw) == rgbw


def test_a_rounded_colour_still_finds_its_swatch() -> None:
    """A dimmed legacy colour scaled back up lands a couple of counts out."""
    dimmed = rgbw_to_int(*(round(channel * 33 / 255) for channel in (255, 87, 0, 0)))
    scaled = full_scale_rgbw(dimmed)
    assert scaled == (255, 85, 0, 0)  # the rounding this tolerance exists for
    assert to_display(scaled) == (255, 255, 0, 0)


def test_a_colour_dimmed_past_recognition_is_shown_as_reported() -> None:
    """At 1/255 a yellow is (1, 0, 0, 0): the channel that identified it is gone."""
    scaled = full_scale_rgbw(rgbw_to_int(*(round(channel * 1 / 255) for channel in (255, 87, 0, 0))))
    assert scaled == (255, 0, 0, 0)  # indistinguishable from red, and reported as red
    assert to_display(scaled) == (255, 0, 0, 0)
