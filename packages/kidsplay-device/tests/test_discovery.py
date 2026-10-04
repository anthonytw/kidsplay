"""Tests for finding the server over mDNS (no real network)."""

import socket
import threading
from typing import NoReturn
from unittest.mock import MagicMock

import pytest
from zeroconf import ServiceInfo

from kidsplay_device import discovery
from kidsplay_device.discovery import (
    SERVICE_TYPE,
    FoundServer,
    ServerDiscovery,
    server_from_info,
)


def info(
    name: str = "KidsPlay", address: str = "192.168.1.20", port: int = 8000
) -> ServiceInfo:
    return ServiceInfo(
        SERVICE_TYPE,
        f"{name}.{SERVICE_TYPE}",
        port=port,
        addresses=[socket.inet_aton(address)],
    )


class TestServerFromInfo:
    def test_builds_the_url(self) -> None:
        assert server_from_info(info()) == FoundServer(
            "KidsPlay", "http://192.168.1.20:8000"
        )

    def test_uses_the_advertised_port(self) -> None:
        found = server_from_info(info(port=8123))
        assert found is not None
        assert found.url == "http://192.168.1.20:8123"

    def test_no_address(self) -> None:
        bare = ServiceInfo(SERVICE_TYPE, f"X.{SERVICE_TYPE}", port=8000, addresses=[])
        assert server_from_info(bare) is None


class TestDiscovery:
    def test_service_type_matches_the_server(self) -> None:
        from kidsplay_server.discovery import SERVICE_TYPE as SERVER_TYPE

        assert SERVICE_TYPE == SERVER_TYPE

    def test_listener_reports_found_and_removed(self) -> None:
        zc = MagicMock()
        zc.get_service_info.return_value = info()
        seen: dict[str, FoundServer | None] = {}
        done = threading.Event()

        def note(name: str, server: FoundServer | None) -> None:
            seen[name] = server
            done.set()

        listener = discovery._Listener(zc, note)
        listener.add_service(zc, SERVICE_TYPE, "KidsPlay._kidsplay._tcp.local.")
        assert done.wait(5)
        assert seen["KidsPlay._kidsplay._tcp.local."] == FoundServer(
            "KidsPlay", "http://192.168.1.20:8000"
        )
        listener.remove_service(zc, SERVICE_TYPE, "KidsPlay._kidsplay._tcp.local.")
        assert seen["KidsPlay._kidsplay._tcp.local."] is None

    def test_unresolvable_service_is_ignored(self) -> None:
        zc = MagicMock()
        zc.get_service_info.side_effect = OSError("network down")
        listener = discovery._Listener(zc, lambda n, s: pytest.fail("no result"))
        listener._resolve(SERVICE_TYPE, "x")

    def test_servers_are_listed_by_name(self) -> None:
        d = ServerDiscovery()
        d._note("b", FoundServer("B", "http://b:1"))
        d._note("a", FoundServer("A", "http://a:1"))
        assert [s.name for s in d.servers] == ["A", "B"]
        d._note("a", None)
        assert [s.name for s in d.servers] == ["B"]

    def test_no_network_is_not_fatal(self) -> None:
        def broken() -> NoReturn:
            raise OSError("no interface")

        d = ServerDiscovery(zeroconf_factory=broken)
        assert d.start() is False
        assert d.servers == []
        d.stop()

    def test_start_and_stop_release_the_sockets(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        zc = MagicMock()
        browsers: list[MagicMock] = []

        def make_browser(*args: object, **kwargs: object) -> MagicMock:
            browsers.append(MagicMock())
            return browsers[-1]

        monkeypatch.setattr(discovery, "ServiceBrowser", make_browser)
        d = ServerDiscovery(zeroconf_factory=lambda: zc)
        assert d.start() is True
        d.stop()
        browsers[0].cancel.assert_called_once()
        zc.close.assert_called_once()
