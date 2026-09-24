"""Config flow: discovered controllers are confirmed; anything else is added by address.

The controller announces itself with Control4's SDDP and takes its DHCP lease
as ``Gemstone-<serial>`` (sddp.py), so it arrives here three ways: the
integration's own SDDP search and listener (discovery.py), and Home Assistant's
DHCP matcher (manifest.json). All three end in `_async_handle_discovery`, which
keys the entry on the serial number -- the one identifier that survives a DHCP
change -- so a discovered controller whose address moved has its entry updated
in place instead of becoming a second device.

An entry made by hand is keyed on the serial too when the controller answers a
unicast SDDP query, and on its address otherwise, as every entry was before
0.6. Entity and device identity is the entry id (entity.py) either way, so the
reconfigure step can change the address without a DHCP change becoming a
delete-and-re-add.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_DEVICE, CONF_HOST, CONF_NAME, CONF_PORT
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import discovery
from .api import DEFAULT_PORT, GemstoneClient, GemstoneConnectionError, GemstoneError, HubSettings
from .const import (
    CONF_SCAN_INTERVAL,
    CONF_SERIAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
    unique_id_for,
    unique_id_for_serial,
)
from .sddp import SddpDevice, serial_from_hostname

if TYPE_CHECKING:
    # Only a type: importing the dhcp component drags in aiodhcpwatcher, which
    # is not installed everywhere this flow runs, and the object is read
    # duck-typed (ip, hostname, macaddress) either way.
    from homeassistant.components.dhcp import DhcpServiceInfo

# Port is optional because firmware always serves on 80 and cannot be told
# otherwise; it is exposed for the development simulator and for anyone who
# reaches a controller through a port-forward or reverse proxy.
PORT_SELECTOR = vol.All(vol.Coerce(int), vol.Range(min=1, max=65535))

# The pick_device choice that falls through to the address form.
MANUAL = "manual"


def _schema(host: str = "", port: int = DEFAULT_PORT) -> vol.Schema:
    """Build the host/port form, pre-filled for the reconfigure step."""
    return vol.Schema(
        {
            vol.Required(CONF_HOST, default=host) if host else vol.Required(CONF_HOST): str,
            vol.Optional(CONF_PORT, default=port): PORT_SELECTOR,
        }
    )


DATA_SCHEMA = _schema()


def _title(settings: HubSettings | None, fallback: str) -> str:
    """Name the entry after the controller's Bluetooth name when it has one."""
    if settings is not None and (settings.bluetooth_name or "").strip():
        return str(settings.bluetooth_name).strip()
    return fallback


@dataclass(frozen=True)
class _Discovered:
    """What a discovery route learnt, however it learnt it."""

    host: str
    port: int
    serial: str
    name: str


class GemstoneConfigFlow(ConfigFlow, domain=DOMAIN):
    """Discovered controllers are confirmed; others are added by address."""

    VERSION = 1

    def __init__(self) -> None:
        """Start with nothing found."""
        self._discovered: _Discovered | None = None
        self._found: dict[str, SddpDevice] = {}
        self._searched = False

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> GemstoneOptionsFlow:
        """Expose the idle poll interval as an option."""
        return GemstoneOptionsFlow()

    async def _async_validate(self, host: str, port: int) -> tuple[HubSettings | None, str | None]:
        """Prove the host is a reachable controller by reading its settings."""
        client = GemstoneClient(host, async_get_clientsession(self.hass), port=port)
        try:
            return await client.async_get_hub_settings(), None
        except GemstoneConnectionError:
            return None, "cannot_connect"
        except GemstoneError:
            return None, "unknown"

    @callback
    def _async_conflicting_entry(
        self,
        *,
        unique_id: str | None = None,
        host: str | None = None,
        port: int = DEFAULT_PORT,
        ignore: str | None = None,
    ) -> ConfigEntry | None:
        """Return the existing entry (other than ``ignore``) that owns this unique id or address."""
        for other in self._async_current_entries(include_ignore=True):
            if other.entry_id == ignore:
                continue
            if unique_id is not None and other.unique_id == unique_id:
                return other
            same_host = host is not None and other.data.get(CONF_HOST) == host
            if same_host and other.data.get(CONF_PORT, DEFAULT_PORT) == port:
                return other
        return None

    # --- discovery --------------------------------------------------------

    async def async_step_dhcp(self, discovery_info: DhcpServiceInfo) -> ConfigFlowResult:
        """Handle a controller that took a DHCP lease as Gemstone-<serial>."""
        serial = serial_from_hostname(discovery_info.hostname)
        if serial is None:
            return self.async_abort(reason="not_gemstone")
        return await self._async_handle_discovery(discovery_info.ip, DEFAULT_PORT, serial, discovery_info.hostname)

    async def async_step_integration_discovery(self, discovery_info: dict[str, Any]) -> ConfigFlowResult:
        """Handle a controller that answered an SDDP search or announced itself (discovery.py)."""
        return await self._async_handle_discovery(
            discovery_info[CONF_HOST],
            int(discovery_info.get(CONF_PORT, DEFAULT_PORT)),
            discovery_info[CONF_SERIAL],
            discovery_info[CONF_NAME],
        )

    async def _async_handle_discovery(self, host: str, port: int, serial: str, name: str) -> ConfigFlowResult:
        """Key the flow on the serial, then either update, adopt, or ask.

        A configured controller whose address changed is updated in place and
        reloaded (``updates=``). One added by address before discovery existed
        is re-keyed onto its serial rather than offered as a new device. Only
        a controller nobody has configured reaches the confirm step.
        """
        await self.async_set_unique_id(unique_id_for_serial(serial))
        self._abort_if_unique_id_configured(updates={CONF_HOST: host, CONF_PORT: port})
        for entry in self._async_current_entries(include_ignore=False):
            if CONF_SERIAL in entry.data:
                continue
            if entry.data.get(CONF_HOST) == host and entry.data.get(CONF_PORT, DEFAULT_PORT) == port:
                self.hass.config_entries.async_update_entry(
                    entry, unique_id=self.unique_id, data={**entry.data, CONF_SERIAL: serial}
                )
                return self.async_abort(reason="already_configured")
        self._discovered = _Discovered(host, port, serial, name)
        self.context["title_placeholders"] = {"name": name, "host": host}
        return await self.async_step_discovery_confirm()

    async def _async_try_create(self, found: _Discovered) -> tuple[ConfigFlowResult | None, dict[str, str]]:
        """Prove the discovered controller's HTTP API is on, then create the entry.

        Discovery has already shown the controller is on the network, so a
        connection failure here means Allow Local Commands is off, not that
        the address is wrong; the error says so.
        """
        settings, error = await self._async_validate(found.host, found.port)
        if error == "cannot_connect" or (settings is not None and settings.tcp_enabled is False):
            return None, {"base": "local_api_disabled"}
        if error:
            return None, {"base": error}
        return (
            self.async_create_entry(
                title=_title(settings, found.name),
                data={CONF_HOST: found.host, CONF_PORT: found.port, CONF_SERIAL: found.serial},
            ),
            {},
        )

    async def async_step_discovery_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Let the user add the discovered controller."""
        assert self._discovered is not None
        found = self._discovered
        errors: dict[str, str] = {}
        if user_input is not None:
            result, errors = await self._async_try_create(found)
            if result is not None:
                return result
        self._set_confirm_only()
        return self.async_show_form(
            step_id="discovery_confirm",
            errors=errors,
            description_placeholders={"name": found.name, "host": found.host},
        )

    # --- by hand ----------------------------------------------------------

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Offer the controllers a search finds; otherwise ask for an address."""
        if user_input is None and not self._searched:
            self._searched = True
            configured = {entry.unique_id for entry in self._async_current_entries(include_ignore=True)}
            found = await discovery.async_discover(self.hass)
            self._found = {
                key: device for key, device in found.items() if unique_id_for_serial(device.serial) not in configured
            }
            if self._found:
                return await self.async_step_pick_device()

        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            port = int(user_input.get(CONF_PORT, DEFAULT_PORT))
            if self._async_conflicting_entry(host=host, port=port):
                return self.async_abort(reason="already_configured")
            settings, error = await self._async_validate(host, port)
            if error:
                errors["base"] = error
            else:
                data: dict[str, Any] = {CONF_HOST: host, CONF_PORT: port}
                device = await discovery.async_query(self.hass, host)
                if device is not None:
                    data[CONF_SERIAL] = device.serial
                    await self.async_set_unique_id(unique_id_for_serial(device.serial))
                else:
                    await self.async_set_unique_id(unique_id_for(host, port))
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=_title(settings, host), data=data)
        return self.async_show_form(step_id="user", data_schema=DATA_SCHEMA, errors=errors)

    async def async_step_pick_device(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Choose one of the controllers the search found, or go on to the address form."""
        errors: dict[str, str] = {}
        if user_input is not None:
            choice = user_input[CONF_DEVICE]
            if choice == MANUAL:
                return await self.async_step_user()
            device = self._found[choice]
            # Not raise_on_progress: the same controller may be sitting on a
            # Discovered card, and picking it here must not abort this flow.
            await self.async_set_unique_id(unique_id_for_serial(device.serial), raise_on_progress=False)
            self._abort_if_unique_id_configured(updates={CONF_HOST: device.host, CONF_PORT: device.http_port})
            result, errors = await self._async_try_create(
                _Discovered(device.host, device.http_port, device.serial, device.hostname)
            )
            if result is not None:
                return result
        options = {key: f"{device.hostname} ({device.host})" for key, device in self._found.items()}
        options[MANUAL] = "Enter an address"
        return self.async_show_form(
            step_id="pick_device",
            data_schema=vol.Schema({vol.Required(CONF_DEVICE): vol.In(options)}),
            errors=errors,
        )

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Point an existing entry at the controller's new address."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            port = int(user_input.get(CONF_PORT, DEFAULT_PORT))
            # Checked by hand rather than with _abort_if_unique_id_configured():
            # that would match this very entry when the host is unchanged, and
            # _abort_if_unique_id_mismatch() is wrong here because the unique
            # id is meant to change.
            if self._async_conflicting_entry(
                unique_id=unique_id_for(host, port), host=host, port=port, ignore=entry.entry_id
            ):
                return self.async_abort(reason="already_configured")
            _, error = await self._async_validate(host, port)
            if error:
                errors["base"] = error
            else:
                known = entry.data.get(CONF_SERIAL)
                device = await discovery.async_query(self.hass, host)
                if (
                    device is not None
                    and known is not None
                    and unique_id_for_serial(device.serial) != unique_id_for_serial(known)
                ):
                    return self.async_abort(reason="wrong_device")
                data: dict[str, Any] = {CONF_HOST: host, CONF_PORT: port}
                if device is not None:
                    data[CONF_SERIAL] = device.serial
                    unique_id = unique_id_for_serial(device.serial)
                elif known is not None:
                    data[CONF_SERIAL] = known
                    unique_id = unique_id_for_serial(known)
                else:
                    unique_id = unique_id_for(host, port)
                if self._async_conflicting_entry(unique_id=unique_id, ignore=entry.entry_id):
                    return self.async_abort(reason="already_configured")
                return self.async_update_reload_and_abort(entry, unique_id=unique_id, data=data)
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_schema(entry.data[CONF_HOST], entry.data.get(CONF_PORT, DEFAULT_PORT)),
            errors=errors,
        )


# Home Assistant 2025.8 added OptionsFlowWithReload, which reloads the entry when
# options change and refuses to run beside an update listener. Older releases
# lack it, so __init__.py registers a reload listener only when it is missing.
# The listener cannot simply stay: from 2026.12 the reconfigure step's
# async_update_reload_and_abort stops working while one is registered.
_OptionsFlowBase: type[OptionsFlow] = getattr(config_entries, "OptionsFlowWithReload", OptionsFlow)


class GemstoneOptionsFlow(_OptionsFlowBase):
    """One option: how often to poll an idle controller."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show and save the idle poll interval."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        current = self.config_entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SCAN_INTERVAL, default=current): vol.All(
                        vol.Coerce(int),
                        vol.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL),
                    )
                }
            ),
        )
