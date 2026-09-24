"""Diagnostics download, with the customer's private details redacted.

Diagnostics files get attached to public bug reports. The hub-settings
response includes the home's coordinates, so redaction here is a privacy
gate, not a nicety.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

if TYPE_CHECKING:
    from . import GemstoneConfigEntry

TO_REDACT = {"location", "lat", "long", "localIp", "bluetoothName", "host", "serial"}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: GemstoneConfigEntry) -> dict[str, Any]:
    """Return the controller's settings and state, redacted."""
    coordinator = entry.runtime_data
    return {
        "entry_data": async_redact_data(dict(entry.data), TO_REDACT),
        "entry_options": dict(entry.options),
        "effect_speed": coordinator.effect_speed,
        "hub_settings": async_redact_data(coordinator.hub_settings.raw, TO_REDACT),
        "currently_playing": (
            async_redact_data(coordinator.data.raw, TO_REDACT) if coordinator.data is not None else None
        ),
        "update_interval_seconds": (
            coordinator.update_interval.total_seconds() if coordinator.update_interval else None
        ),
        "last_update_success": coordinator.last_update_success,
    }
