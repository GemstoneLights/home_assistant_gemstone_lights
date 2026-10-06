"""Effect parameters as number entities: speed and direction for the effect picker.

Home Assistant's effect model is a bare string, so the parameters a pattern
carries beside its animation need entities of their own. These follow WLED's
precedent: move one while an animation plays and the animation is replayed with
the new value; pick a new effect and it uses whatever they hold. A pattern the
controller reports (set from the app, say) updates them, so they are real state
rather than write-only knobs.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.components.number import NumberEntityDescription, NumberMode, RestoreNumber
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import build_pattern_replay_command
from .coordinator import GemstoneCoordinator
from .entity import GemstoneEntity

if TYPE_CHECKING:
    from . import GemstoneConfigEntry

# Setting a value can replay the running pattern, which is a controller command.
PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class GemstoneNumberEntityDescription(NumberEntityDescription):
    """Describes one effect parameter held on the coordinator."""

    value_fn: Callable[[GemstoneCoordinator], int]
    set_fn: Callable[[GemstoneCoordinator, int], None]


NUMBERS: tuple[GemstoneNumberEntityDescription, ...] = (
    GemstoneNumberEntityDescription(
        key="effect_speed",
        translation_key="effect_speed",
        entity_category=EntityCategory.CONFIG,
        native_min_value=1,
        native_max_value=255,
        native_step=1,
        mode=NumberMode.SLIDER,
        value_fn=lambda coordinator: coordinator.effect_speed,
        set_fn=lambda coordinator, value: coordinator.async_set_effect_params(speed=value),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GemstoneConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add both effect parameters for the controller."""
    async_add_entities(GemstoneNumber(entry.runtime_data, entry, description) for description in NUMBERS)


class GemstoneNumber(GemstoneEntity, RestoreNumber):
    """One effect parameter, kept on the coordinator and restored across restarts."""

    entity_description: GemstoneNumberEntityDescription

    def __init__(
        self,
        coordinator: GemstoneCoordinator,
        entry: GemstoneConfigEntry,
        description: GemstoneNumberEntityDescription,
    ) -> None:
        """Register as a secondary entity keyed by the parameter."""
        super().__init__(coordinator, entry, key=description.key)
        self.entity_description = description

    async def async_added_to_hass(self) -> None:
        """Restore the last set value, unless the controller has already spoken.

        A reported pattern is real state and beats a remembered slider position;
        only when nothing is playing does the remembered value apply.
        """
        await super().async_added_to_hass()
        if self.coordinator.data.pattern is not None:
            return
        last = await self.async_get_last_number_data()
        if last is not None and last.native_value is not None:
            self.entity_description.set_fn(self.coordinator, int(last.native_value))

    @property
    def native_value(self) -> int:
        """The coordinator's current value for this parameter."""
        return self.entity_description.value_fn(self.coordinator)

    async def async_set_native_value(self, value: float) -> None:
        """Store the value and, if an animation is playing, apply it live.

        Storing notifies every coordinator listener, so state is written either
        way. The ``on_state`` guard matters: a replay sends ``onState: true``,
        and a slider move while the lights are off must not switch them on.
        """
        self.entity_description.set_fn(self.coordinator, int(value))
        playing = self.coordinator.data
        if playing.on_state and playing.pattern is not None:
            await self._async_send(
                self.coordinator.client.async_play(
                    build_pattern_replay_command(playing.pattern, speed=self.coordinator.effect_speed)
                )
            )
