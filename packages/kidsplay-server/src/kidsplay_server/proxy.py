"""Who is really calling, and over which scheme, behind a reverse proxy.

Behind Caddy, nginx or Traefik every connection comes from the proxy, so the
peer address says nothing about the client. Throttles keyed on it would let one
attacker lock everybody out, and the server could not tell that the browser
used HTTPS. ``ProxyHeadersMiddleware`` fixes both, once, for the whole app: it
rewrites the ASGI ``client`` and ``scheme`` from ``X-Forwarded-For`` and
``X-Forwarded-Proto``, but **only** when the TCP peer is listed in
``KIDSPLAY_TRUSTED_PROXIES``. Everything downstream (the login and pairing
throttles, the logs, the Secure cookie) then just reads ``request.client`` and
``request.url.scheme``.

Rules
-----
* A peer that is not a trusted proxy is never believed: its forwarded headers
  are ignored, so a client cannot pick its own throttle key.
* The client is the **right-most hop that is not itself a trusted proxy**.
  Hops further left were written by the client (or by a hop the client could
  have controlled) and can say anything. If every hop is trusted, the left-most
  one is used.
* Every ``X-Forwarded-For`` header line counts, joined in order, so a second
  header line cannot hide the real hop.
* A value that is not an IP address (after allowing ``ip:port`` and
  ``[ipv6]:port``) is never used; the middleware then keeps the proxy's own
  address rather than trusting garbage.
* ``X-Forwarded-Proto`` only counts from a trusted peer, and only ``http`` or
  ``https`` are accepted (the right-most value, the nearest proxy's view).

Uvicorn has its own ``--proxy-headers`` handling (on by default, trusting
127.0.0.1). Run uvicorn with ``--no-proxy-headers`` so this module is the only
one deciding; the Docker image and the all-in-one service already do.
"""

import ipaddress
from collections.abc import Iterable

from starlette.types import ASGIApp, Message, Receive, Scope, Send

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

_IPV6_THROTTLE_PREFIX = 64
"""IPv6 clients are throttled per /64: one host usually owns a whole /64, so
per-address keys would let it dodge any limit by rotating addresses."""


def parse_trusted_proxies(value: str | Iterable[str]) -> tuple[IPNetwork, ...]:
    """Parse ``KIDSPLAY_TRUSTED_PROXIES``: IP addresses and CIDR networks.

    Args:
        value: Comma- or whitespace-separated text, or a list of entries.

    Returns:
        The networks (a bare address becomes a /32 or /128), in order.

    Raises:
        ValueError: If an entry is not an address or network, or is a
            wildcard. ``*`` and ``0.0.0.0/0`` would let any client claim any
            address, which defeats the throttles, so they are refused.
    """
    if isinstance(value, str):
        entries = value.replace(",", " ").split()
    else:
        entries = [e.strip() for e in value if e.strip()]
    networks: list[IPNetwork] = []
    for entry in entries:
        try:
            network = ipaddress.ip_network(entry, strict=False)
        except ValueError:
            raise ValueError(
                f"KIDSPLAY_TRUSTED_PROXIES entry {entry!r} is not an IP "
                "address or CIDR network (wildcards are not accepted)"
            ) from None
        if network.prefixlen == 0:
            raise ValueError(
                f"KIDSPLAY_TRUSTED_PROXIES entry {entry!r} would trust every "
                "client; list your proxy's address or network instead"
            )
        networks.append(network)
    return tuple(networks)


def parse_ip(text: str) -> IPAddress | None:
    """Parse one forwarded-for hop leniently.

    Accepts ``1.2.3.4``, ``1.2.3.4:5678``, ``::1``, ``[::1]`` and
    ``[::1]:5678``. IPv4-mapped IPv6 addresses become plain IPv4 and a zone
    (``%eth0``) is dropped.

    Args:
        text: One comma-separated element of ``X-Forwarded-For``.

    Returns:
        The address, or None if ``text`` is not an IP address.
    """
    cleaned = text.strip()
    if not cleaned or len(cleaned) > 64:
        return None
    candidates = [cleaned]
    if cleaned.startswith("["):
        host, _, rest = cleaned[1:].partition("]")
        if rest == "" or (rest.startswith(":") and rest[1:].isdecimal()):
            candidates.append(host)
    elif cleaned.count(":") == 1:
        host, _, port = cleaned.partition(":")
        if port.isdecimal():
            candidates.append(host)
    for candidate in candidates:
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        if isinstance(address, ipaddress.IPv6Address):
            if address.ipv4_mapped:
                return address.ipv4_mapped
            if address.scope_id:
                # A link-local peer is reported as ``fe80::1%eth0``. The zone
                # names the server's interface, not the client, so it must not
                # make the same client look like several.
                return ipaddress.IPv6Address(int(address))
        return address
    return None


def throttle_key(client_host: str | None) -> str:
    """Turn a client address into the key its throttle budget is kept under.

    IPv6 addresses are grouped by /64 and IPv4-mapped ones by their IPv4
    address, so one machine cannot get a fresh budget by changing address.

    Args:
        client_host: The client address (``request.client.host``), if known.

    Returns:
        A stable key; ``"unknown"`` if there is no usable address.
    """
    if not client_host:
        return "unknown"
    address = parse_ip(client_host)
    if address is None:
        return client_host
    if isinstance(address, ipaddress.IPv6Address):
        network = ipaddress.ip_network(
            f"{address}/{_IPV6_THROTTLE_PREFIX}", strict=False
        )
        return str(network)
    return str(address)


def _is_trusted(address: IPAddress, trusted: tuple[IPNetwork, ...]) -> bool:
    return any(address.version == net.version and address in net for net in trusted)


def client_from_forwarded_for(
    forwarded_for: str, trusted: tuple[IPNetwork, ...]
) -> IPAddress | None:
    """Pick the real client out of an ``X-Forwarded-For`` chain.

    Args:
        forwarded_for: All ``X-Forwarded-For`` values joined with commas.
        trusted: The trusted proxy networks.

    Returns:
        The right-most hop that is not a trusted proxy (the left-most hop if
        all are trusted), or None if that hop is not a valid address, in
        which case the caller keeps the peer.
    """
    hops = [hop for hop in forwarded_for.split(",") if hop.strip()]
    if not hops:
        return None
    for hop in reversed(hops):
        address = parse_ip(hop)
        if address is None:
            return None
        if not _is_trusted(address, trusted):
            return address
    return parse_ip(hops[0])


class ProxyHeadersMiddleware:
    """ASGI middleware applying ``X-Forwarded-*`` from trusted proxies only.

    With no trusted proxies it does nothing at all.

    Args:
        app: The wrapped ASGI application.
        trusted_proxies: Networks whose forwarded headers are believed.
    """

    def __init__(self, app: ASGIApp, trusted_proxies: tuple[IPNetwork, ...]) -> None:
        self.app = app
        self._trusted = trusted_proxies

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Rewrite ``client`` and ``scheme`` in ``scope`` when appropriate."""
        if self._trusted and scope["type"] in ("http", "websocket"):
            self._apply(scope)
        await self.app(scope, receive, send)

    def _apply(self, scope: Scope) -> None:
        client = scope.get("client")
        if not client or not client[0]:
            return
        peer = parse_ip(str(client[0]))
        if peer is None or not _is_trusted(peer, self._trusted):
            return

        forwarded_for: list[str] = []
        forwarded_proto: list[str] = []
        for name, value in scope.get("headers", []):
            if name == b"x-forwarded-for":
                forwarded_for.append(value.decode("latin-1"))
            elif name == b"x-forwarded-proto":
                forwarded_proto.append(value.decode("latin-1"))

        if forwarded_for:
            real = client_from_forwarded_for(",".join(forwarded_for), self._trusted)
            if real is not None:
                # The remote port is unknown behind a proxy.
                scope["client"] = (str(real), 0)

        proto = ",".join(forwarded_proto).rsplit(",", 1)[-1].strip().lower()
        if proto in ("http", "https"):
            secure = proto == "https"
            if scope["type"] == "websocket":
                scope["scheme"] = "wss" if secure else "ws"
            else:
                scope["scheme"] = proto


def is_secure_scope(scope: Scope) -> bool:
    """Return whether the request arrived over HTTPS (directly or via a proxy).

    Args:
        scope: ASGI scope, after ``ProxyHeadersMiddleware``.

    Returns:
        True for the ``https`` and ``wss`` schemes.
    """
    return scope.get("scheme") in ("https", "wss")


class SecureCookieMiddleware:
    """Add ``Secure`` to the session cookie when the request came over HTTPS.

    Starlette's ``SessionMiddleware`` can only be told ``https_only`` once, for
    the whole app. This wraps it (it must sit outside it, and inside
    ``ProxyHeadersMiddleware`` so the scheme is already corrected) and adds the
    attribute per request, so one install works over HTTP on the LAN and over
    HTTPS through a proxy with no configuration.

    Args:
        app: The wrapped ASGI application.
        cookie_name: Name of the session cookie.
    """

    def __init__(self, app: ASGIApp, cookie_name: str) -> None:
        self.app = app
        self._prefix = f"{cookie_name}=".encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Wrap ``send`` so the cookie header is completed on the way out."""
        if scope["type"] != "http" or not is_secure_scope(scope):
            await self.app(scope, receive, send)
            return

        async def secure_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = []
                for name, value in message.get("headers", []):
                    if (
                        name.lower() == b"set-cookie"
                        and value.startswith(self._prefix)
                        and b"secure" not in value.lower().split(b"; ")
                    ):
                        value += b"; secure"
                    headers.append((name, value))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, secure_send)
