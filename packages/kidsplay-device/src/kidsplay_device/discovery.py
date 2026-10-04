"""Find KidsPlay servers on the local network (mDNS / DNS-SD).

The server advertises ``_kidsplay._tcp`` (see ``kidsplay_server.discovery``);
:class:`ServerDiscovery` browses for it so the pairing screen can offer "found
server at http://...". Everything here is best effort: with no network, or a
network that filters multicast, it simply finds nothing, and the address can
be typed instead.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

from zeroconf import IPVersion, ServiceBrowser, ServiceInfo, ServiceListener, Zeroconf

from kidsplay_models import short_server_id

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

SERVICE_TYPE = "_kidsplay._tcp.local."

_LOOKUP_TIMEOUT_MS = 3000


@dataclass(frozen=True)
class FoundServer:
    """A server seen on the network.

    Attributes:
        name: Instance name, e.g. ``KidsPlay``.
        url: Base URL to pair with, e.g. ``http://192.168.1.20:8000``.
        server_id: The id the server announces (``AB12-CD34``), or "" if it
            announces none. Anyone can announce any name or id; this is
            something to compare with the parent's approval page, not proof.
    """

    name: str
    url: str
    server_id: str = ""


def server_from_info(info: ServiceInfo) -> FoundServer | None:
    """Turn a resolved service into a server to offer.

    Args:
        info: The resolved DNS-SD service.

    Returns:
        The server, or None if it has no IPv4 address or port.
    """
    addresses = info.parsed_addresses(IPVersion.V4Only)
    if not addresses or not info.port:
        return None
    instance = info.name.removesuffix(f".{SERVICE_TYPE}").removesuffix(SERVICE_TYPE)
    raw_id = (info.properties or {}).get(b"id")
    announced = raw_id.decode("ascii", "ignore") if isinstance(raw_id, bytes) else ""
    return FoundServer(
        name=instance or "KidsPlay",
        url=f"http://{addresses[0]}:{info.port}",
        server_id=short_server_id(announced),
    )


class _Listener(ServiceListener):
    """Resolves services on worker threads and reports the result."""

    def __init__(
        self, zc: Zeroconf, found: Callable[[str, FoundServer | None], None]
    ) -> None:
        self._zc = zc
        self._found = found

    def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        threading.Thread(target=self._resolve, args=(type_, name), daemon=True).start()

    def update_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        self.add_service(zc, type_, name)

    def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        self._found(name, None)

    def _resolve(self, type_: str, name: str) -> None:
        try:
            info = self._zc.get_service_info(type_, name, timeout=_LOOKUP_TIMEOUT_MS)
        except Exception as exc:  # zeroconf raises OSError and its own errors
            logger.debug("Could not resolve %s: %s", name, exc)
            return
        if info is not None:
            self._found(name, server_from_info(info))


class ServerDiscovery:
    """Browse for servers in the background.

    Args:
        zeroconf_factory: Builds the ``Zeroconf`` instance (tests pass a fake).
    """

    def __init__(self, zeroconf_factory: Callable[[], Zeroconf] | None = None) -> None:
        self._factory = zeroconf_factory or (
            lambda: Zeroconf(ip_version=IPVersion.V4Only)
        )
        self._zc: Zeroconf | None = None
        self._browser: ServiceBrowser | None = None
        self._lock = threading.Lock()
        self._servers: dict[str, FoundServer] = {}

    def start(self) -> bool:
        """Start browsing.

        Returns:
            True if browsing started; False if the network refused (logged).
        """
        try:
            self._zc = self._factory()
            self._browser = ServiceBrowser(
                self._zc, SERVICE_TYPE, listener=_Listener(self._zc, self._note)
            )
        except Exception as exc:  # no interface, no multicast, no permission
            logger.info("Server discovery unavailable: %s", exc)
            self.stop()
            return False
        return True

    def _note(self, name: str, server: FoundServer | None) -> None:
        with self._lock:
            if server is None:
                self._servers.pop(name, None)
            else:
                self._servers[name] = server

    @property
    def servers(self) -> list[FoundServer]:
        """The servers seen so far, by name."""
        with self._lock:
            return sorted(self._servers.values(), key=lambda s: s.name)

    def stop(self) -> None:
        """Stop browsing and release the sockets."""
        browser, zc = self._browser, self._zc
        self._browser = self._zc = None
        try:
            if browser is not None:
                browser.cancel()
            if zc is not None:
                zc.close()
        except Exception as exc:  # shutting down; nothing to recover
            logger.debug("Discovery shutdown: %s", exc)
