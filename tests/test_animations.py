"""The animation catalog, and the per-animation shape of the commands it drives.

A pattern is not one shape for all 29. These tests hold the builder to the
vendor's specification animation by animation, because getting this wrong does
not raise -- it just makes the lights look wrong.
"""

import pytest

from custom_components.gemstone_lights.animations import CATALOG, DIRECTIONS, Animation
from custom_components.gemstone_lights.api import build_pattern_command
from custom_components.gemstone_lights.const import ANIMATIONS, EFFECT_LIST

RED = (255, 0, 0, 0)
EVERY = pytest.mark.parametrize("animation", CATALOG.values(), ids=lambda a: a.slug)


def _pattern(animation: Animation) -> dict:
    return build_pattern_command(animation.slug, animation.name, [RED], 200)["pattern"]


def test_the_catalog_is_the_whole_catalogue() -> None:
    """29 animations, and the picker is built from the same source."""
    assert len(CATALOG) == 29
    assert ANIMATIONS == {slug: animation.name for slug, animation in CATALOG.items()}
    assert EFFECT_LIST == [animation.name for animation in CATALOG.values()]


def test_the_counts_match_the_specification() -> None:
    """A transcription slip would show up here before it showed up on a roofline."""
    assert sum(1 for a in CATALOG.values() if not a.directions) == 13
    assert sum(1 for a in CATALOG.values() if a.background_color) == 15
    assert sum(1 for a in CATALOG.values() if a.extras) == 12
    assert sum(len(a.extras) for a in CATALOG.values()) == 28
    assert sum(1 for a in CATALOG.values() if a.speed_type == 0) == 3


def test_no_animation_uses_a_direction_outside_the_six() -> None:
    assert {d for a in CATALOG.values() for d in a.directions} <= set(DIRECTIONS)


@EVERY
def test_direction_is_sent_only_when_the_animation_takes_one(animation: Animation) -> None:
    """The 13 that do not travel must not carry the key at all."""
    pattern = _pattern(animation)
    if animation.directions:
        assert pattern["direction"] in animation.directions
    else:
        assert "direction" not in pattern


@EVERY
def test_background_colour_is_sent_only_when_the_animation_has_one(animation: Animation) -> None:
    pattern = _pattern(animation)
    assert ("backgroundColor" in pattern) is animation.background_color


@EVERY
def test_extra_parameters_are_complete_or_absent(animation: Animation) -> None:
    """Every index the animation defines, or the key left out entirely.

    A partial object is the dangerous case: the indexes left out keep whatever
    firmware holds, which is the bug this catalog exists to fix.
    """
    pattern = _pattern(animation)
    if not animation.extras:
        assert "extraParameters" not in pattern
        return
    extras = pattern["extraParameters"]
    assert extras["version"] == animation.extras_version
    assert set(extras) - {"version"} == {str(p.index) for p in animation.extras}
    for parameter in animation.extras:
        assert extras[str(parameter.index)] == {"value": parameter.default, "name": parameter.name}


@EVERY
def test_speed_is_never_zero(animation: Animation) -> None:
    """Speed is 1-255; zero is outside what the animations accept."""
    assert build_pattern_command(animation.slug, "T", [RED], 200, speed=0)["pattern"]["speed"] == 1


@EVERY
def test_every_animation_builds_a_command(animation: Animation) -> None:
    pattern = _pattern(animation)
    assert pattern["animation"] == animation.slug
    assert pattern["colors"] == [255]
    assert pattern["brightness"] == 200


# --- the examples the specification calls out by name ------------------------


def test_motionless_carries_only_index_one() -> None:
    """Its single parameter is index 1, with no index 0. Not a transcription slip."""
    extras = _pattern(CATALOG["motionless"])["extraParameters"]
    assert extras == {"version": 0, "1": {"value": 1, "name": "Length"}}


def test_eyeball_carries_all_six_including_the_colours() -> None:
    extras = _pattern(CATALOG["eyeball"])["extraParameters"]
    assert extras["0"] == {"value": 16777215, "name": "Eye White Color"}
    assert extras["3"] == {"value": 0, "name": "Pupil Color"}
    assert len(extras) == 7  # six parameters plus the version


def test_wave_carries_its_documented_defaults() -> None:
    extras = _pattern(CATALOG["wave"])["extraParameters"]
    assert extras["0"] == {"value": 20, "name": "Length"}
    assert extras["1"] == {"value": 70, "name": "Tail Brightness"}


def test_accent_has_no_background_despite_its_help_text() -> None:
    """Its description mentions background pixels; the flag says otherwise."""
    assert CATALOG["accent"].background_color is False
    assert "backgroundColor" not in _pattern(CATALOG["accent"])


def test_an_override_is_clamped_to_the_parameter_s_range() -> None:
    wave = CATALOG["wave"]
    extras = build_pattern_command("wave", "W", [RED], 200, extra_parameters={0: 9999})["pattern"]
    assert extras["extraParameters"]["0"]["value"] == wave.extras[0].bounds[1]


def test_music_animations_are_not_in_the_catalog() -> None:
    """They need a UDP audio stream this integration does not implement."""
    assert not [slug for slug in CATALOG if slug.startswith("music")]
