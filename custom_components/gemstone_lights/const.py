"""Constants for the Gemstone Lights integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

# Protocol defaults live with the protocol; api.py and sddp.py own them.
from .animations import CATALOG
from .api import DEFAULT_PORT
from .sddp import QUERY_TIMEOUT, SEARCH_TIMEOUT

DOMAIN: Final = "gemstone_lights"

MANUFACTURER: Final = "Gemstone Lights"


def unique_id_for_serial(serial: str) -> str:
    """Identify one controller for the *config entry* by its serial number.

    The serial arrives two ways that disagree on case: SDDP carries the
    controller's own ``Gemstone-<serial>``, and Home Assistant lower-cases
    every DHCP hostname before matching. Normalised here, once, so the two
    routes cannot make two entries for one controller. (Entity and device
    identity is the entry id -- see entity.py -- which is why the reconfigure
    flow can change the address.)
    """
    return serial.strip().lower()


def unique_id_for(host: str, port: int = DEFAULT_PORT) -> str:
    """Identify a controller by address, for one that has not told us its serial.

    Every entry was keyed this way before 0.6, and an entry added by hand still
    is when the controller does not answer a unicast SDDP query (a firewall,
    or a controller that has not yet connected to the cloud, which is when its
    UDP server opens). On the default port this is the bare host, so old
    entries keep their unique id and need no migration. A non-default port is
    appended, so two controllers reached through different forwards -- or two
    simulators on one machine -- do not collide.
    """
    return host if port == DEFAULT_PORT else f"{host}:{port}"


# The controller serves HTTP off the same firmware that drives the pixels, and
# the API documents a 503 for "system busy" -- so polling is adaptive. For a
# minute after any command or observed change we poll fast, so someone actively
# using the lights sees a live dashboard; an idle controller is barely touched;
# an unreachable one is backed off from. The idle interval is the one the
# options flow exposes.
DEFAULT_SCAN_INTERVAL: Final = 30
FAST_SCAN_INTERVAL: Final = 5
FAST_WINDOW_SECONDS: Final = 60
MAX_BACKOFF_INTERVAL: Final = 120
MIN_SCAN_INTERVAL: Final = 5
MAX_SCAN_INTERVAL: Final = 300

# Hub settings (firmware versions, pixel counts) only change across a reboot.
# They are re-read when the controller comes back from an outage, and on this
# interval as a fallback for a reboot that fell between two polls.
HUB_SETTINGS_REFRESH_SECONDS: Final = 3600

CONF_SCAN_INTERVAL: Final = "scan_interval"
CONF_SERIAL: Final = "serial"

# Discovery. Controllers announce themselves every five minutes anyway
# (sddp.py), so the active search is a slow safety net, not the main path.
DISCOVERY_INTERVAL: Final = timedelta(minutes=15)
SDDP_SEARCH_TIMEOUT: Final = SEARCH_TIMEOUT
SDDP_QUERY_TIMEOUT: Final = QUERY_TIMEOUT

# Entity services registered by the light platform; schemas live in services.py.
SERVICE_PLAY_PATTERN: Final = "play_pattern"
SERVICE_SAVE_PLAYING: Final = "save_playing"
SERVICE_FORGET_PATTERN: Final = "forget_pattern"


def signal_patterns_changed(entry_id: str) -> str:
    """Dispatcher signal: this controller's saved patterns changed.

    The light saves and forgets; the Pattern control hears it and rewrites
    its options, so neither has to hold a callback belonging to the other.
    """
    return f"{DOMAIN}_patterns_changed_{entry_id}"


# Fallback hue when a colour command must be sent but no colour is known
# (fresh install, or the controller was playing a pattern). This is the packed
# integer 12614435, the ecosystem's default blue used by the app and cloud.
DEFAULT_RGBW: Final[tuple[int, int, int, int]] = (35, 123, 192, 0)

# Home Assistant's effect model is a bare string, so speed travels beside it on
# the coordinator: the companion number entity sets it and a pattern the
# controller reports overrides it (see GemstoneCoordinator). Direction is not
# here -- each animation has its own, and thirteen take none at all
# (animations.py). Background colour is only reachable via the play_pattern
# action, and only for the animations that have one.
DEFAULT_SPEED: Final = 128
DEFAULT_BACKGROUND_COLOR: Final = 0


# Animation wire slug -> display name. Derived from the catalog so the picker
# and the per-animation rules that build a pattern cannot drift apart
# (animations.py).
ANIMATIONS: Final[dict[str, str]] = {slug: animation.name for slug, animation in CATALOG.items()}

# Display name -> wire slug, for turning an HA effect selection back into a
# payload.
ANIMATION_SLUGS: Final[dict[str, str]] = {name: slug for slug, name in ANIMATIONS.items()}

EFFECT_LIST: Final[list[str]] = list(ANIMATIONS.values())
