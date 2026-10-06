"""Polling coordinator for one Hub2 controller.

The controller cannot push, so this is the only source of state freshness.
Firmware's own tooling polls the same way. The interval
adapts: fast for a minute after activity, slow when idle, backed off while the
controller is unreachable.

It also owns two things that are not poll results but need one home shared by
every platform: the effect parameters the light's effect picker sends, and the
hub settings the diagnostic entities read.
"""

from __future__ import annotations

import logging
import time
from dataclasses import replace as dataclass_replace
from datetime import timedelta
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import CurrentlyPlaying, GemstoneClient, GemstoneError, HubSettings, clamp_speed
from .const import (
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SPEED,
    DOMAIN,
    FAST_SCAN_INTERVAL,
    FAST_WINDOW_SECONDS,
    HUB_SETTINGS_REFRESH_SECONDS,
    MAX_BACKOFF_INTERVAL,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)
from .store import SavedPatterns

if TYPE_CHECKING:
    from . import GemstoneConfigEntry

_LOGGER = logging.getLogger(__name__)


def next_interval(idle_interval: int, failures: int, seconds_since_activity: float) -> int:
    """Pick the next poll interval.

    Pure so it can be tested without a running coordinator: repeated failures
    widen the interval toward the cap, recent activity keeps it fast, and an
    idle controller gets the configured idle interval.
    """
    if failures:
        return min(MAX_BACKOFF_INTERVAL, idle_interval * 2**failures)
    if seconds_since_activity < FAST_WINDOW_SECONDS:
        return FAST_SCAN_INTERVAL
    return idle_interval


class GemstoneCoordinator(DataUpdateCoordinator[CurrentlyPlaying]):
    """Keeps Home Assistant's picture of the lights current."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: GemstoneConfigEntry,
        client: GemstoneClient,
        hub_settings: HubSettings,
        saved_patterns: SavedPatterns,
    ) -> None:
        """Store the client, the hub settings read during setup, and the saved patterns."""
        idle = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        self._idle_interval = max(MIN_SCAN_INTERVAL, min(MAX_SCAN_INTERVAL, int(idle)))
        self._failures = 0
        self._last_activity = time.monotonic()
        self._hub_settings_read_at = time.monotonic()
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            # Titled per controller so two of them are told apart in the log.
            name=f"{DOMAIN} {entry.title}",
            update_interval=timedelta(seconds=FAST_SCAN_INTERVAL),
        )
        self.client = client
        self.hub_settings = hub_settings
        # Patterns kept by Home Assistant, offered by one select (store.py).
        self.saved_patterns = saved_patterns
        # What the effect picker sends with the next animation. Precedence: a
        # pattern the controller reports wins whenever there is one, so the
        # number entities show real state whoever set it; otherwise the last
        # value set here wins and is what the next effect selection uses. A
        # value a number entity restores after a restart applies only when
        # nothing was reported at startup.
        self.effect_speed: int = DEFAULT_SPEED

    def _reschedule(self) -> None:
        """Recompute the interval from failures and recency of activity."""
        seconds = next_interval(self._idle_interval, self._failures, time.monotonic() - self._last_activity)
        self.update_interval = timedelta(seconds=seconds)

    async def _async_update_data(self) -> CurrentlyPlaying:
        """Read currently-playing; any client failure marks entities unavailable."""
        recovering = self._failures > 0
        try:
            result = await self.client.async_get_currently_playing()
        except GemstoneError as err:
            self._failures += 1
            self._reschedule()
            raise UpdateFailed(str(err)) from err
        self._failures = 0
        # Hub settings only change across a reboot, and an outage is what a
        # reboot looks like from here. The timed refresh catches one that fell
        # between two polls.
        if recovering or time.monotonic() - self._hub_settings_read_at >= HUB_SETTINGS_REFRESH_SECONDS:
            await self._async_refresh_hub_settings()
        # A change we did not cause (the app, a timer, Control4) counts as
        # activity: someone is using the lights, so stay responsive.
        if self.data is None or result != self.data:
            self._last_activity = time.monotonic()
        self._adopt_effect_params(result)
        self._reschedule()
        return result

    async def _async_refresh_hub_settings(self) -> None:
        """Re-read hub settings, keeping the old ones if the read fails.

        Settings are static between reboots, so stale beats unavailable, and
        the next window retries. ``DeviceInfo.sw_version`` is captured when the
        entities are created and stays as it was until the entry reloads.
        """
        try:
            self.hub_settings = await self.client.async_get_hub_settings()
        except GemstoneError as err:
            _LOGGER.debug("%s: hub-settings refresh failed, keeping previous: %s", self.client.host, err)
        self._hub_settings_read_at = time.monotonic()

    @callback
    def async_set_effect_params(self, *, speed: int | None = None) -> None:
        """Store what the next effect selection will send, and tell listeners."""
        if speed is not None:
            self.effect_speed = clamp_speed(speed)
        self.async_update_listeners()

    def _adopt_effect_params(self, result: CurrentlyPlaying) -> None:
        """Take the speed from a reported pattern: reported state wins.

        Direction is not carried: each animation has its own, and the ones that
        take none must not be sent one at all (animations.py).
        """
        if result.pattern is None:
            return
        speed = result.pattern.get("speed")
        if isinstance(speed, int) and not isinstance(speed, bool):
            self.effect_speed = clamp_speed(speed)

    @callback
    def async_apply_result(self, result: CurrentlyPlaying) -> None:
        """Adopt a POST reply as the current state.

        Deliberately the only place that trusts a command reply. Firmware has
        not confirmed whether the reply reflects applied or merely accepted
        state (open question Q1 in docs/design.md); if the bench says accepted, change this
        one method to schedule a short-delay refresh instead. Nothing else
        needs to move.

        A power-only reply (API section 3.4) carries no scene. The controller
        did not change the scene, so neither does this: the previous colour,
        brightness and pattern carry forward under the new power state.
        Adopting the bare reply blanked the card until the next poll and let a
        brightness move in that window collapse a running pattern to a colour.
        """
        if result.scene is None and self.data is not None:
            result = dataclass_replace(self.data, on_state=result.on_state, raw={**self.data.raw, **result.raw})
        self._failures = 0
        self._last_activity = time.monotonic()
        self._adopt_effect_params(result)
        self._reschedule()
        self.async_set_updated_data(result)
