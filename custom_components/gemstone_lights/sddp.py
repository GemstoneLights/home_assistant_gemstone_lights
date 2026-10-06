"""Control4 SDDP as a client, so a controller can be found without typing its address.

The Hub2 already speaks Control4's Simple Device Discovery Protocol for the
Control4 driver: it answers ``SEARCH * SDDP/1.0`` on UDP port 1902 and
multicasts ``NOTIFY ALIVE`` to 239.255.255.250:1902 every five minutes and
whenever it reconnects to the cloud. Every message carries
``Host: "Gemstone-<serial>"`` and ``From: "<ip>:1902"``: an address, plus the
one identifier that survives a DHCP change. Home Assistant has no SDDP support
of its own (SSDP is a different protocol on a different port), and the one SDDP
library on PyPI needs a C extension Home Assistant OS cannot build, so this
module is the whole client. Standard library only, no Home Assistant imports,
like api.py.

Three facts about the firmware shape what is here:

* It replies to a SEARCH by unicast to the address in the request's ``From``
  header, at the port the request came *from*. So a search socket is bound to
  the one source address it advertises, and stays open for the replies.
* Its parser insists on ``From`` (quoted), ``Tran`` and ``Timeout``; a SEARCH
  missing any of them is ignored. `build_search_request` sends exactly that.
* On Ethernet it never joins the multicast group, so its IP stack drops a
  multicast SEARCH while it answers a broadcast one. A search therefore goes
  to the group *and* to every broadcast address, and `SddpListener` catches
  the NOTIFYs a controller sends unprompted, which need no request at all.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import socket
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Final, Literal

_LOGGER = logging.getLogger(__name__)

SDDP_GROUP: Final = "239.255.255.250"
SDDP_PORT: Final = 1902

# The firmware's fixed Type header, and the prefix it gives its hostname (the
# same string is its DHCP hostname, which is what the manifest's dhcp matcher
# keys on).
GEMSTONE_TYPE: Final = "gemstonelights:controller"
HOST_PREFIX: Final = "gemstone-"

# Firmware always serves HTTP on 80. The header is an extension only the
# development simulator sends, so several simulators on one address can be
# told apart.
DEFAULT_HTTP_PORT: Final = 80
HTTP_PORT_HEADER: Final = "http-port"

SEARCH_TIMEOUT: Final = 3.0
QUERY_TIMEOUT: Final = 2.0

Kind = Literal["response", "alive", "offline", "identify"]

_STATUS_KINDS: Final[tuple[tuple[re.Pattern[str], Kind], ...]] = (
    (re.compile(r"^SDDP/1\.\d+\s+200\s+OK$", re.IGNORECASE), "response"),
    (re.compile(r"^NOTIFY\s+ALIVE\s+SDDP/1\.\d+$", re.IGNORECASE), "alive"),
    (re.compile(r"^NOTIFY\s+OFFLINE\s+SDDP/1\.\d+$", re.IGNORECASE), "offline"),
    (re.compile(r"^NOTIFY\s+IDENTIFY\s+SDDP/1\.\d+$", re.IGNORECASE), "identify"),
)


@dataclass(frozen=True)
class SddpDevice:
    """One controller heard on the network.

    ``host`` is the address the datagram came from, which is the address to
    reach the controller on; the ``From`` header repeats it. ``serial`` keeps
    the controller's own capitalisation and ``hostname`` is the full
    ``Gemstone-<serial>`` for display.
    """

    serial: str
    host: str
    hostname: str
    kind: Kind
    tran: int | None
    max_age: int | None
    http_port: int
    raw: dict[str, str]


def serial_from_hostname(hostname: str) -> str | None:
    """Extract the serial from a ``Gemstone-<serial>`` hostname; None if it is not one."""
    if not hostname.lower().startswith(HOST_PREFIX):
        return None
    serial = hostname[len(HOST_PREFIX) :].strip()
    return serial or None


def build_search_request(from_ip: str, tran: int, timeout: int = 1) -> bytes:
    """Build the one SEARCH the firmware's parser accepts.

    ``From`` must be quoted and ``Tran`` and ``Timeout`` must be present and
    numeric, or the controller logs a warning and stays silent. ``Timeout`` is
    the seconds a device may wait before answering (the firmware answers at
    once), so it is kept below the caller's own wait.
    """
    return f'SEARCH * SDDP/1.0\r\nFrom: "{from_ip}"\r\nTran: {tran}\r\nTimeout: {max(1, timeout)}\r\n\r\n'.encode()


def _int_or_none(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value.strip())
    except ValueError:
        return None


def parse_sddp(data: bytes, source: tuple[str, int]) -> SddpDevice | None:
    """Parse one datagram; None for anything that is not a Gemstone controller.

    Tolerant of LF-only lines and a trailing NUL, strict about the two things
    that identify a controller: the ``Type`` the firmware sends and the
    ``Gemstone-`` hostname. SEARCH requests (including this module's own
    echoes) and other vendors' devices parse to None.
    """
    text = data.rstrip(b"\x00").decode("utf-8", errors="replace")
    lines = text.replace("\r\n", "\n").split("\n")
    status = lines[0].strip()
    kind: Kind | None = None
    for pattern, candidate in _STATUS_KINDS:
        if pattern.match(status):
            kind = candidate
            break
    if kind is None:
        return None

    headers: dict[str, str] = {}
    for line in lines[1:]:
        key, sep, value = line.partition(":")
        if not sep:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
            value = value[1:-1]
        headers[key.strip().lower()] = value

    if headers.get("type", "").lower() != GEMSTONE_TYPE:
        return None
    hostname = headers.get("host", "")
    serial = serial_from_hostname(hostname)
    if serial is None:
        return None

    from_ip = headers.get("from", "").partition(":")[0]
    if from_ip and from_ip != source[0]:
        _LOGGER.debug("SDDP %s from %s says its address is %s; using %s", kind, source[0], from_ip, source[0])

    return SddpDevice(
        serial=serial,
        host=source[0],
        hostname=hostname,
        kind=kind,
        tran=_int_or_none(headers.get("tran")),
        max_age=_int_or_none(headers.get("max-age")),
        http_port=_int_or_none(headers.get(HTTP_PORT_HEADER)) or DEFAULT_HTTP_PORT,
        raw=headers,
    )


class _Collector(asyncio.DatagramProtocol):
    """Hand every parsed Gemstone datagram to a callback."""

    def __init__(self, on_device: Callable[[SddpDevice], None]) -> None:
        self._on_device = on_device

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        """Parse and forward; anything else is dropped."""
        device = parse_sddp(data, addr)
        if device is not None:
            self._on_device(device)

    def error_received(self, exc: Exception) -> None:
        """Log ICMP errors (unreachable hosts) rather than let them surface."""
        _LOGGER.debug("SDDP socket error: %s", exc)


def _open_search_socket(source_ip: str) -> socket.socket:
    """Open a socket bound to one source address, able to send broadcast and multicast.

    Bound to the address, not 0.0.0.0, because the controller unicasts its
    reply to the address in ``From`` at this socket's port: the socket must own
    that exact address. ``IP_MULTICAST_IF`` sends the group traffic out of the
    same interface.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setblocking(False)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind((source_ip, 0))
    except OSError:
        sock.close()
        raise
    # Best effort: they steer multicast out of the right interface on a
    # multi-homed host, and a loopback address (the tests) may refuse them.
    for option, value in ((socket.IP_MULTICAST_TTL, 1), (socket.IP_MULTICAST_IF, socket.inet_aton(source_ip))):
        try:
            sock.setsockopt(socket.IPPROTO_IP, option, value)
        except OSError as err:
            _LOGGER.debug("Multicast option %s not set on %s: %s", option, source_ip, err)
    return sock


async def async_search(
    source_ips: Iterable[str],
    targets: Iterable[tuple[str, int]],
    *,
    wait: float = SEARCH_TIMEOUT,
) -> dict[str, SddpDevice]:
    """Send a SEARCH from every source address to every target; collect the replies.

    Returns the controllers that answered, keyed by lower-cased serial (the
    first reply for a serial wins, so a controller reachable on two interfaces
    is listed once). ``targets`` are ``(address, port)`` pairs: the multicast
    group, the broadcast addresses, or a single controller.
    """
    loop = asyncio.get_running_loop()
    found: dict[str, SddpDevice] = {}
    transports: list[asyncio.DatagramTransport] = []
    tran = random.randint(1, 0xFFFF)  # a transaction tag the reply echoes, not a secret
    targets = list(targets)

    def collect(device: SddpDevice) -> None:
        if device.kind == "response":
            found.setdefault(device.serial.lower(), device)

    for source_ip in source_ips:
        try:
            sock = _open_search_socket(source_ip)
        except OSError as err:
            _LOGGER.debug("Cannot search for controllers from %s: %s", source_ip, err)
            continue
        transport, _ = await loop.create_datagram_endpoint(lambda: _Collector(collect), sock=sock)
        transports.append(transport)
        request = build_search_request(source_ip, tran, timeout=max(1, int(wait) - 1))
        for target in targets:
            try:
                transport.sendto(request, target)
            except OSError as err:
                _LOGGER.debug("Cannot send SDDP search from %s to %s: %s", source_ip, target, err)

    if not transports:
        return found
    try:
        await asyncio.sleep(wait)
    finally:
        for transport in transports:
            transport.close()
    return found


async def async_query(
    host: str,
    source_ip: str,
    *,
    port: int = SDDP_PORT,
    wait: float = QUERY_TIMEOUT,
) -> SddpDevice | None:
    """Ask one controller, by unicast, who it is.

    Unicast reaches a controller whatever its interface's multicast handling,
    so this is how an entry added by address learns its serial number.
    """
    loop = asyncio.get_running_loop()
    answered: asyncio.Future[SddpDevice] = loop.create_future()

    def collect(device: SddpDevice) -> None:
        if device.kind == "response" and device.host == host and not answered.done():
            answered.set_result(device)

    try:
        sock = _open_search_socket(source_ip)
    except OSError as err:
        _LOGGER.debug("Cannot query %s from %s: %s", host, source_ip, err)
        return None
    transport, _ = await loop.create_datagram_endpoint(lambda: _Collector(collect), sock=sock)
    try:
        transport.sendto(build_search_request(source_ip, random.randint(1, 0xFFFF)), (host, port))
        return await asyncio.wait_for(answered, wait)
    except (TimeoutError, OSError) as err:
        _LOGGER.debug("No SDDP answer from %s: %s", host, err)
        return None
    finally:
        transport.close()


class SddpListener:
    """Hear the NOTIFYs controllers send on their own.

    A controller announces itself on boot-plus-five-minutes, every five minutes
    after that, each time it reconnects to the cloud, and whenever the Gemstone
    app's Control4 Identify button asks it to, whether or not anyone here
    asked. This is the path that needs nothing from the controller's interface
    beyond the ability to *send* multicast, so it is the one that works on
    every Hub2. The socket is shared (``SO_REUSEADDR``/``SO_REUSEPORT``) so a
    Control4 Director or the development simulator on the same machine keeps
    working.
    """

    def __init__(
        self,
        on_device: Callable[[SddpDevice], None],
        *,
        source_ips: Iterable[str],
        port: int = SDDP_PORT,
        group: str = SDDP_GROUP,
    ) -> None:
        """Remember what to join; nothing is opened until `async_start`."""
        self._on_device = on_device
        self._source_ips = list(source_ips)
        self._port = port
        self._group = group
        self._transport: asyncio.DatagramTransport | None = None

    async def async_start(self) -> bool:
        """Open the socket and join the group on every interface; False if the port is unusable."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setblocking(False)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT") and sys.platform != "win32":
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            sock.bind(("", self._port))
        except OSError as err:
            sock.close()
            _LOGGER.warning("Cannot listen for controller announcements on UDP port %s: %s", self._port, err)
            return False

        joined = 0
        for source_ip in self._source_ips:
            mreq = socket.inet_aton(self._group) + socket.inet_aton(source_ip)
            try:
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
                joined += 1
            except OSError as err:
                _LOGGER.debug("Cannot join %s on %s: %s", self._group, source_ip, err)
        if not joined:
            # Still worth keeping: NOTIFYs sent as broadcast, and any SEARCH
            # reply misdirected here, arrive without a group membership.
            _LOGGER.debug("Listening on UDP %s without a multicast membership", self._port)

        loop = asyncio.get_running_loop()
        self._transport, _ = await loop.create_datagram_endpoint(lambda: _Collector(self._on_device), sock=sock)
        return True

    @property
    def port(self) -> int | None:
        """Return the port the listener is bound to, once started."""
        if self._transport is None:
            return None
        return int(self._transport.get_extra_info("sockname")[1])

    def close(self) -> None:
        """Leave the group and close the socket."""
        if self._transport is not None:
            self._transport.close()
            self._transport = None
