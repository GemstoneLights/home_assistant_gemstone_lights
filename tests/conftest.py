"""Shared fixtures: load the Home Assistant test harness and this repo's component."""

from collections.abc import Generator
from contextlib import ExitStack
from unittest.mock import patch

import aiohttp.resolver
import pytest
from homeassistant.helpers import aiohttp_client

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Allow Home Assistant to load custom_components from this repository."""
    return


@pytest.fixture
def entity_registry_enabled_by_default() -> Generator[None]:
    """Enable every entity in the registry, including ones that default to disabled.

    Home Assistant core ships this fixture in its own conftest; the custom
    component test plugin does not, so this is the same implementation.
    """
    with patch("homeassistant.helpers.entity.Entity.entity_registry_enabled_default", return_value=True):
        yield


@pytest.fixture(autouse=True)
def discovery_off_the_wire() -> Generator[None]:
    """Keep the SDDP search, query and listener off real sockets.

    `async_setup` searches the network and opens a listener, and the config
    flow searches on its first form and queries a typed address. Every test
    would otherwise touch the LAN. Tests that exercise discovery patch these
    again with what they want found; the socket code itself is tested in
    test_sddp.py against the simulator on loopback.
    """
    with (
        patch("custom_components.gemstone_lights.discovery.async_discover", return_value={}),
        patch("custom_components.gemstone_lights.discovery.async_query", return_value=None),
        patch("custom_components.gemstone_lights.discovery.async_start_listener"),
    ):
        yield


@pytest.fixture(autouse=True)
def use_threaded_resolver() -> Generator[None]:
    """Keep pycares out of the test process.

    Two session constructors would otherwise load it: Home Assistant's shared
    client session builds an ``AsyncResolver`` explicitly, and the harness's
    own mock session builds a default ``TCPConnector`` whose default resolver
    is the aiodns one whenever aiodns is installed (which Home Assistant
    guarantees). pycares spawns a helper thread that outlives the test and
    trips the harness's lingering-thread check. Every request in these tests
    is mocked, so no resolver is ever actually exercised.

    From Home Assistant 2025.2 the shared session gets its resolver from
    ``_async_make_resolver`` instead, which the harness already replaces with
    one resolver for the whole run, so there is no ``AsyncResolver`` to patch.
    """
    threaded = aiohttp.resolver.ThreadedResolver
    with ExitStack() as stack:
        stack.enter_context(patch("aiohttp.connector.DefaultResolver", threaded))
        if hasattr(aiohttp_client, "AsyncResolver"):
            stack.enter_context(patch.object(aiohttp_client, "AsyncResolver", threaded))
        yield
