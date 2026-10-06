"""Patterns kept by Home Assistant, because the controller cannot keep them.

The local API plays a scene and reports the one playing. It has no route that
saves a pattern, and none that lists what the app has saved (Q6), so a pattern
worth keeping is kept here instead.

What is stored is the controller's own reported object, not a rebuilt one:
replaying it is then the same command the controller last confirmed, including
any field this integration does not model.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import slugify

from .const import DOMAIN

STORAGE_VERSION = 1


@dataclass(frozen=True)
class SavedPattern:
    """One kept pattern: what to call it, and what to send to play it."""

    slug: str
    name: str
    pattern: dict[str, Any]


class SavedPatterns:
    """One controller's saved patterns, surviving restarts.

    Keyed by a slug of the name, so saving under a name already used replaces
    it rather than growing a second entity that looks identical.
    """

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        """Open the store for one config entry; nothing is read until loaded."""
        self._store = Store[dict[str, Any]](hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}.patterns")
        self._patterns: dict[str, SavedPattern] = {}

    async def async_load(self) -> list[SavedPattern]:
        """Read what was saved before, tolerating a store that is not there yet."""
        data = await self._store.async_load() or {}
        saved = data.get("patterns", {})
        self._patterns = {
            slug: SavedPattern(slug, entry["name"], entry["pattern"])
            for slug, entry in saved.items()
            if isinstance(entry, dict) and "name" in entry and isinstance(entry.get("pattern"), dict)
        }
        return list(self._patterns.values())

    async def async_save(self, name: str, pattern: dict[str, Any]) -> SavedPattern:
        """Keep one pattern under a name, replacing any pattern of that name."""
        saved = SavedPattern(slugify(name), name, dict(pattern))
        self._patterns[saved.slug] = saved
        await self._async_write()
        return saved

    async def async_forget(self, slug: str) -> None:
        """Drop one pattern; forgetting one that is already gone is not an error."""
        if self._patterns.pop(slug, None) is not None:
            await self._async_write()

    def all(self) -> list[SavedPattern]:
        """Every saved pattern, in the order they were saved."""
        return list(self._patterns.values())

    async def _async_write(self) -> None:
        await self._store.async_save(
            {
                "patterns": {
                    slug: {"name": saved.name, "pattern": saved.pattern} for slug, saved in self._patterns.items()
                }
            }
        )
