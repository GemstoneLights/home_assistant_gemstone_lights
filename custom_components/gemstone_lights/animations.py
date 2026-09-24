"""What each animation is, and what a pattern must carry for it to look right.

A pattern is not one shape for all 29 animations. Each reads its own data, and
sending the wrong set is how an animation ends up looking broken rather than
failing loudly:

* 13 of the 29 take no ``direction`` at all, and the key must be left out.
* The other 16 accept only a subset of the six directions.
* 15 use a ``backgroundColor``; for the other 14 the key does not belong.
* 12 read ``extraParameters`` -- 28 knobs between them, things like a wave's
  length or the spacing between colour segments. Left out, the animation runs
  on whatever those fields default to in firmware, which is the visible bug
  this table exists to fix.

Transcribed from Gemstone's animation specification for the customer app, which
is the same firmware this integration talks to. Two traps it calls out are worth
repeating here, because both look like typos:

* ``motionless`` defines only extra parameter index ``1``. The indexes are not
  contiguous and do not have to start at zero.
* ``accent`` talks about "background colored pixels" in its help text but has no
  background colour. The flag wins.

Music-mode animations (``music_*``) are deliberately absent: they need a UDP
audio stream this integration does not implement, and without it they show
nothing.

Imports nothing from Home Assistant, for the same reason api.py doesn't (4.1).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

# What a direction value means. The controller accepts a wider range, but these
# six are the only ones any animation acts on.
DIRECTIONS: Final[dict[int, str]] = {
    0: "Right",
    1: "Left",
    2: "Right & Left",
    3: "In",
    4: "Out",
    5: "In & Out",
}

# How an animation treats ``speed``: 0 does not move at all, 1 is stepped by
# changing the frame rate, 2 runs at full rate and reads the value itself.
NOT_MOVING: Final = 0


@dataclass(frozen=True)
class ExtraParameter:
    """One knob an animation reads out of ``extraParameters``."""

    index: int
    name: str
    kind: Literal["int", "bool", "color"]
    default: int
    bounds: tuple[int, int] | None = None

    def clamp(self, value: int) -> int:
        """Coerce a value into what this parameter accepts."""
        if self.kind == "bool":
            return 1 if value else 0
        if self.kind == "color":
            return value & 0xFFFFFFFF
        if self.bounds is not None:
            low, high = self.bounds
            return max(low, min(high, value))
        return value


@dataclass(frozen=True)
class Animation:
    """One animation and the shape of a pattern that drives it."""

    slug: str
    name: str
    background_color: bool
    directions: tuple[int, ...]
    speed_type: int
    min_colors: int = 1
    max_colors: int = 20
    extras: tuple[ExtraParameter, ...] = ()
    extras_version: int = 0

    def direction_for(self, requested: int | None) -> int | None:
        """Return the direction to send, or None when this animation takes none.

        A request this animation does not allow falls back to its first
        direction rather than raising: the caller is choosing for whatever is
        playing next, and every animation has a sensible default.
        """
        if not self.directions:
            return None
        if requested in self.directions:
            return requested
        return self.directions[0]

    def extra_parameters(self, overrides: dict[int, int] | None = None) -> dict[str, object] | None:
        """Build the ``extraParameters`` object, or None when this animation has none.

        Every index the animation defines is sent, because a partial object
        leaves the rest at whatever firmware happens to hold.
        """
        if not self.extras:
            return None
        chosen = overrides or {}
        built: dict[str, object] = {"version": self.extras_version}
        for parameter in self.extras:
            value = chosen.get(parameter.index, parameter.default)
            built[str(parameter.index)] = {"value": parameter.clamp(value), "name": parameter.name}
        return built


def _a(
    slug: str,
    name: str,
    *,
    background_color: bool,
    directions: tuple[int, ...],
    speed_type: int,
    extras: tuple[ExtraParameter, ...] = (),
) -> tuple[str, Animation]:
    """Build one catalog row, keyed by slug."""
    return slug, Animation(
        slug=slug,
        name=name,
        background_color=background_color,
        directions=directions,
        speed_type=speed_type,
        extras=extras,
    )


_ALL = (0, 1, 2, 3, 4, 5)

# Order matters: the effect list Home Assistant shows is built from it, and
# services.yaml mirrors the same order.
CATALOG: Final[dict[str, Animation]] = dict(
    (
        _a(
            "accent",
            "Accent",
            background_color=False,
            directions=(),
            speed_type=0,
            extras=(
                ExtraParameter(0, "On Length", "int", 1, (1, 20)),
                ExtraParameter(1, "Off Length", "int", 5, (1, 20)),
            ),
        ),
        _a("around", "Around", background_color=False, directions=(0, 1, 2), speed_type=1),
        _a("chase", "Chase", background_color=False, directions=_ALL, speed_type=1),
        _a(
            "eyeball",
            "Eyeball",
            background_color=True,
            directions=(),
            speed_type=1,
            extras=(
                ExtraParameter(0, "Eye White Color", "color", 16777215),
                ExtraParameter(1, "Eye White Size", "int", 20, (1, 30)),
                ExtraParameter(2, "Iris Size", "int", 6, (0, 10)),
                ExtraParameter(3, "Pupil Color", "color", 0),
                ExtraParameter(4, "Pupil Size", "int", 4, (0, 20)),
                ExtraParameter(5, "Spacing", "int", 5, (0, 50)),
            ),
        ),
        _a("fade", "Fade", background_color=False, directions=(), speed_type=2),
        _a(
            "fireworks",
            "Fireworks",
            background_color=True,
            directions=(),
            speed_type=2,
            extras=(
                ExtraParameter(0, "Firework Count", "int", 4, (1, 8)),
                ExtraParameter(1, "Rocket Color", "color", 4278190080),
                ExtraParameter(2, "Rocket Travel %", "int", 25, (0, 100)),
            ),
        ),
        _a("flicker", "Flicker", background_color=True, directions=(), speed_type=1),
        _a("flow", "Flow", background_color=True, directions=(0, 1, 2, 3, 4), speed_type=1),
        _a("ghost", "Ghost", background_color=True, directions=(0, 1, 2), speed_type=1),
        _a("glitch", "Glitch", background_color=True, directions=(), speed_type=1),
        _a(
            "glitter",
            "Glitter",
            background_color=True,
            directions=(),
            speed_type=1,
            extras=(
                ExtraParameter(0, "Reverse Fade", "bool", 0),
                ExtraParameter(1, "Active Light %", "int", 60, (0, 100)),
            ),
        ),
        _a(
            "gradient_wave",
            "Gradient Wave",
            background_color=False,
            directions=(0, 1, 3, 4),
            speed_type=1,
            extras=(ExtraParameter(0, "Length", "int", 20, (1, 100)),),
        ),
        _a("gradient", "Gradient", background_color=False, directions=(0, 1, 2), speed_type=2),
        _a(
            "isofade",
            "IsoFade",
            background_color=True,
            directions=(),
            speed_type=1,
            extras=(
                ExtraParameter(0, "On Length", "int", 1, (1, 20)),
                ExtraParameter(1, "Off Length", "int", 3, (0, 20)),
            ),
        ),
        _a("marquee", "Marquee", background_color=False, directions=_ALL, speed_type=1),
        _a(
            "motionless",
            "Motionless",
            background_color=True,
            directions=(),
            speed_type=0,
            # Index 1, with no index 0. Not a transcription slip.
            extras=(ExtraParameter(1, "Length", "int", 1, (1, 20)),),
        ),
        _a("multipulse", "MultiPulse", background_color=True, directions=(0, 1, 2, 3), speed_type=2),
        _a("pacman", "Pacman", background_color=False, directions=(0, 1, 3, 4, 5), speed_type=1),
        _a("pulse", "Pulse", background_color=False, directions=(0, 1), speed_type=2),
        _a(
            "pyramid_chase",
            "Pyramid Chase",
            background_color=False,
            directions=_ALL,
            speed_type=1,
            extras=(
                ExtraParameter(0, "Pyramid Length", "int", 20, (1, 200)),
                ExtraParameter(1, "Color Length", "int", 40, (1, 200)),
                ExtraParameter(2, "Pyramid Base Brightness", "int", 25, (0, 255)),
            ),
        ),
        _a(
            "smooth",
            "Smooth",
            background_color=True,
            directions=_ALL,
            speed_type=2,
            extras=(
                ExtraParameter(0, "Color Length", "int", 10, (1, 20)),
                ExtraParameter(1, "Spacing", "int", 2, (2, 20)),
            ),
        ),
        _a(
            "spectrum",
            "Spectrum",
            background_color=False,
            directions=(),
            speed_type=0,
            extras=(
                ExtraParameter(0, "Gradient Length", "int", 10, (1, 20)),
                ExtraParameter(1, "Solid Length", "int", 0, (0, 50)),
                ExtraParameter(2, "Gradient Mode", "int", 1, (1, 3)),
            ),
        ),
        _a("spotlight", "Spotlight", background_color=True, directions=(), speed_type=1),
        _a("stack", "Stack", background_color=True, directions=(0, 1, 2), speed_type=1),
        _a("starry", "Starry", background_color=True, directions=(), speed_type=1),
        _a("stretch", "Stretch", background_color=False, directions=(0, 1, 2), speed_type=1),
        _a("sway", "Sway", background_color=False, directions=_ALL, speed_type=1),
        _a(
            "tremor",
            "Tremor",
            background_color=True,
            directions=(),
            speed_type=1,
            extras=(ExtraParameter(0, "Intensity %", "int", 70, (1, 100)),),
        ),
        _a(
            "wave",
            "Wave",
            background_color=False,
            directions=(0, 1, 3, 4),
            speed_type=1,
            extras=(
                ExtraParameter(0, "Length", "int", 20, (1, 100)),
                ExtraParameter(1, "Tail Brightness", "int", 70, (0, 255)),
            ),
        ),
    )
)
