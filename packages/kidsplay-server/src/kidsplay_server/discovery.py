"""Advertise the server on the local network (mDNS / DNS-SD).

A handheld that is being paired looks for ``_kidsplay._tcp`` and offers
"found server at http://...", so nobody types an address. Advertising is
optional and best effort: a machine without a usable network interface, or one
that forbids multicast, just logs a warning and the server runs without it.
Manual address entry on the device always works.

Environment variables (read by ``create_app_from_env``)
-------------------------------------------------------
KIDSPLAY_MDNS
    ``0``/``false``/``off`` turns advertising off. Default: on, but only while
    "Allow pairing new devices" is on (Settings); handhelds look for the server
    only to pair, so it is not announced otherwise.

KIDSPLAY_MDNS_PORT
    The port devices should connect to (the server cannot see the port
    ``uvicorn`` was started on, or the reverse proxy in front of it).
    Default: 8000.

KIDSPLAY_MDNS_NAME
    Instance name shown to devices. Default: ``KidsPlay``.
"""

import asyncio
import logging
import os
import socket
from collections.abc import Callable, Mapping
from dataclasses import dataclass

import ifaddr
from zeroconf import IPVersion, ServiceInfo
from zeroconf.asyncio import AsyncZeroconf

logger = logging.getLogger(__name__)

SERVICE_TYPE = "_kidsplay._tcp.local."
"""DNS-SD service type the server registers and devices browse for."""

_OFF = frozenset({"0", "false", "off", "no"})


@dataclass(frozen=True)
class AdvertiseConfig:
    """What to advertise.

    Attributes:
        port: TCP port devices connect to.
        name: Instance name.
        server_id: The server's stable id (``id`` in the TXT record), so a
            handheld can show which server it found. Anyone on the network can
            advertise anything; this is a label, not a proof of identity.
    """

    port: int = 8000
    name: str = "KidsPlay"
    server_id: str = ""


def advertise_config_from_env(
    environ: Mapping[str, str] | None = None,
) -> AdvertiseConfig | None:
    """Read the advertising settings.

    Args:
        environ: Environment to read; ``os.environ`` if None.

    Returns:
        The settings, or None if advertising is switched off.

    Raises:
        ValueError: If ``KIDSPLAY_MDNS_PORT`` is not a port number.
    """
    env = os.environ if environ is None else environ
    if env.get("KIDSPLAY_MDNS", "").strip().lower() in _OFF:
        return None
    raw_port = env.get("KIDSPLAY_MDNS_PORT", "").strip()
    try:
        port = int(raw_port) if raw_port else 8000
    except ValueError:
        port = 0
    if not 0 < port < 65536:
        raise ValueError(f"KIDSPLAY_MDNS_PORT must be a port number, not {raw_port!r}")
    name = env.get("KIDSPLAY_MDNS_NAME", "").strip() or "KidsPlay"
    return AdvertiseConfig(port=port, name=name)


def local_ipv4_addresses() -> list[str]:
    """Return this machine's non-loopback, non-link-local IPv4 addresses."""
    found: list[str] = []
    for adapter in ifaddr.get_adapters():
        for ip in adapter.ips:
            if not ip.is_IPv4 or not isinstance(ip.ip, str):
                continue
            if ip.ip.startswith(("127.", "169.254.")):
                continue
            found.append(ip.ip)
    return found


class Advertiser:
    """Registers and withdraws the ``_kidsplay._tcp`` service.

    Args:
        config: What to advertise.
    """

    def __init__(self, config: AdvertiseConfig) -> None:
        self._config = config
        self._zeroconf: AsyncZeroconf | None = None
        self._info: ServiceInfo | None = None

    async def start(self) -> bool:
        """Register the service.

        Returns:
            True if the service is being advertised; False if it could not be
            (the reason is logged and the server carries on).
        """
        addresses = local_ipv4_addresses()
        if not addresses:
            logger.warning("mDNS: no network address to advertise; skipping")
            return False
        host = socket.gethostname().split(".")[0] or "kidsplay"
        properties = {"api": "/api/v1", "v": "1"}
        if self._config.server_id:
            properties["id"] = self._config.server_id
        info = ServiceInfo(
            SERVICE_TYPE,
            f"{self._config.name}.{SERVICE_TYPE}",
            port=self._config.port,
            addresses=[socket.inet_aton(a) for a in addresses],
            server=f"{host}.local.",
            properties=properties,
        )
        try:
            zc = AsyncZeroconf(ip_version=IPVersion.V4Only)
            await zc.async_register_service(info)
        except Exception as exc:  # best effort: see module docstring
            logger.warning("mDNS advertising failed: %s", exc)
            return False
        self._zeroconf, self._info = zc, info
        logger.info(
            "Advertising %s on port %d (%s)",
            SERVICE_TYPE,
            self._config.port,
            ", ".join(addresses),
        )
        return True

    async def stop(self) -> None:
        """Withdraw the service. Safe to call if ``start`` failed."""
        zc, info = self._zeroconf, self._info
        self._zeroconf = self._info = None
        if zc is None or info is None:
            return
        try:
            await zc.async_unregister_service(info)
            await zc.async_close()
        except Exception as exc:  # best effort: see module docstring
            logger.warning("mDNS shutdown failed: %s", exc)


class AdvertisingSwitch:
    """Keeps advertising in step with the "allow pairing" setting.

    Handhelds only look for the server while pairing, so there is no reason to
    announce it on the network when pairing is turned off. The setting can
    change while the server runs, so this starts and stops the advertiser as
    it changes.

    Args:
        config: What to advertise, or None if advertising is switched off
            (``KIDSPLAY_MDNS=0``); then nothing ever starts.
        advertiser_factory: Builds the advertiser (tests pass a fake).
    """

    def __init__(
        self,
        config: AdvertiseConfig | None,
        advertiser_factory: Callable[[AdvertiseConfig], Advertiser] = Advertiser,
    ) -> None:
        self._config = config
        self._factory = advertiser_factory
        self._advertiser: Advertiser | None = None
        self._lock = asyncio.Lock()

    @property
    def advertising(self) -> bool:
        """Whether an advertiser is currently running."""
        return self._advertiser is not None

    async def set_enabled(self, enabled: bool) -> None:
        """Start or stop advertising to match ``enabled``. Idempotent.

        Args:
            enabled: Whether pairing is allowed right now.
        """
        async with self._lock:
            if self._config is None:
                return
            if enabled and self._advertiser is None:
                advertiser = self._factory(self._config)
                if await advertiser.start():
                    self._advertiser = advertiser
            elif not enabled and self._advertiser is not None:
                await self._stop()
                logger.info("Pairing is off; stopped advertising on the network")

    async def _stop(self) -> None:
        advertiser, self._advertiser = self._advertiser, None
        if advertiser is not None:
            await advertiser.stop()

    async def stop(self) -> None:
        """Stop advertising (server shutdown)."""
        async with self._lock:
            await self._stop()
