"""Tests for mDNS advertising: off by default, best effort, never fatal."""

import tomllib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from kidsplay_server import discovery
from kidsplay_server.api.app import create_app
from kidsplay_server.database import (
    configure_conn,
    get_or_create_server_id,
    set_server_setting_value,
)
from kidsplay_server.discovery import (
    SERVICE_TYPE,
    AdvertiseConfig,
    Advertiser,
    AdvertisingSwitch,
    advertise_config_from_env,
)


class TestConfigFromEnv:
    def test_defaults_on(self) -> None:
        assert advertise_config_from_env({}) == AdvertiseConfig(8000, "KidsPlay")

    @pytest.mark.parametrize("off", ["0", "false", "OFF", " no "])
    def test_can_be_switched_off(self, off: str) -> None:
        assert advertise_config_from_env({"KIDSPLAY_MDNS": off}) is None

    def test_port_and_name(self) -> None:
        env = {"KIDSPLAY_MDNS_PORT": "8080", "KIDSPLAY_MDNS_NAME": "Home"}
        assert advertise_config_from_env(env) == AdvertiseConfig(8080, "Home")

    @pytest.mark.parametrize("port", ["abc", "0", "70000"])
    def test_bad_port(self, port: str) -> None:
        with pytest.raises(ValueError, match="KIDSPLAY_MDNS_PORT"):
            advertise_config_from_env({"KIDSPLAY_MDNS_PORT": port})


class FakeZeroconf:
    """Stands in for ``AsyncZeroconf``: records, opens no socket."""

    instances: list["FakeZeroconf"] = []

    def __init__(self, **kwargs: object) -> None:
        self.registered: list[discovery.ServiceInfo] = []
        self.closed = False
        FakeZeroconf.instances.append(self)

    async def async_register_service(self, info: discovery.ServiceInfo) -> None:
        self.registered.append(info)

    async def async_unregister_service(self, info: discovery.ServiceInfo) -> None:
        self.registered.remove(info)

    async def async_close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_zeroconf(monkeypatch: pytest.MonkeyPatch) -> type[FakeZeroconf]:
    FakeZeroconf.instances = []
    monkeypatch.setattr(discovery, "AsyncZeroconf", FakeZeroconf)
    monkeypatch.setattr(discovery, "local_ipv4_addresses", lambda: ["192.168.1.20"])
    return FakeZeroconf


class TestAdvertiser:
    async def test_registers_and_withdraws(
        self, fake_zeroconf: type[FakeZeroconf]
    ) -> None:
        advertiser = Advertiser(AdvertiseConfig(port=8123, name="Home"))
        assert await advertiser.start() is True
        (zc,) = fake_zeroconf.instances
        (info,) = zc.registered
        assert info.type == SERVICE_TYPE
        assert info.port == 8123
        assert info.parsed_addresses() == ["192.168.1.20"]
        await advertiser.stop()
        assert zc.registered == []
        assert zc.closed

    async def test_no_address_is_not_fatal(
        self, fake_zeroconf: type[FakeZeroconf], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(discovery, "local_ipv4_addresses", lambda: [])
        advertiser = Advertiser(AdvertiseConfig())
        assert await advertiser.start() is False
        await advertiser.stop()
        assert fake_zeroconf.instances == []

    async def test_socket_failure_is_not_fatal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def broken(**kwargs: object) -> None:
            raise OSError("multicast is not permitted")

        monkeypatch.setattr(discovery, "AsyncZeroconf", broken)
        monkeypatch.setattr(discovery, "local_ipv4_addresses", lambda: ["10.0.0.5"])
        advertiser = Advertiser(AdvertiseConfig())
        assert await advertiser.start() is False
        await advertiser.stop()

    def test_local_addresses_skip_loopback(self) -> None:
        assert not any(a.startswith("127.") for a in discovery.local_ipv4_addresses())


class TestAppWiring:
    async def test_off_unless_asked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """create_app never touches the network by default (tests, all-in-one)."""

        def boom(*args: object, **kwargs: object) -> None:
            raise AssertionError("mDNS must be off by default")

        monkeypatch.setattr(discovery, "AsyncZeroconf", boom)
        app = create_app(tmp_path / "db.sqlite", tmp_path / "media")
        async with app.router.lifespan_context(app):
            pass

    async def test_advertises_while_the_app_runs(
        self, tmp_path: Path, fake_zeroconf: type[FakeZeroconf]
    ) -> None:
        app = create_app(
            tmp_path / "db.sqlite",
            tmp_path / "media",
            advertise=AdvertiseConfig(port=8000),
        )
        async with app.router.lifespan_context(app):
            (zc,) = fake_zeroconf.instances
            assert len(zc.registered) == 1
        assert zc.registered == [] and zc.closed


class TestServerIdInTheAnnouncement:
    async def test_txt_record_carries_the_server_id(
        self, fake_zeroconf: type[FakeZeroconf]
    ) -> None:
        advertiser = Advertiser(AdvertiseConfig(server_id="3f2a9c1e-8b47"))
        assert await advertiser.start()
        (info,) = fake_zeroconf.instances[0].registered
        assert info.properties[b"id"] == b"3f2a9c1e-8b47"
        assert info.properties[b"api"] == b"/api/v1"

    async def test_no_id_no_record(self, fake_zeroconf: type[FakeZeroconf]) -> None:
        assert await Advertiser(AdvertiseConfig()).start()
        (info,) = fake_zeroconf.instances[0].registered
        assert b"id" not in info.properties

    async def test_app_announces_its_stored_id(
        self, tmp_path: Path, fake_zeroconf: type[FakeZeroconf]
    ) -> None:
        app = create_app(
            tmp_path / "db.sqlite", tmp_path / "media", advertise=AdvertiseConfig()
        )
        async with app.router.lifespan_context(app):
            (info,) = fake_zeroconf.instances[0].registered
            announced = info.properties[b"id"]
        async with aiosqlite.connect(tmp_path / "db.sqlite") as conn:
            await configure_conn(conn)
            assert announced == (await get_or_create_server_id(conn)).encode()


class FakeAdvertiser:
    """Stands in for ``Advertiser`` in switch tests."""

    def __init__(self, config: AdvertiseConfig, works: bool = True) -> None:
        self.config = config
        self.works = works
        self.started = 0
        self.stopped = 0

    async def start(self) -> bool:
        self.started += 1
        return self.works

    async def stop(self) -> None:
        self.stopped += 1


class TestAdvertisingSwitch:
    async def test_follows_the_setting(self) -> None:
        made: list[FakeAdvertiser] = []

        def factory(config: AdvertiseConfig) -> Advertiser:
            made.append(FakeAdvertiser(config))
            return made[-1]  # ty: ignore[invalid-return-type]  # duck-typed fake

        switch = AdvertisingSwitch(AdvertiseConfig(), factory)
        assert not switch.advertising
        await switch.set_enabled(False)
        assert made == []
        await switch.set_enabled(True)
        await switch.set_enabled(True)  # idempotent
        assert switch.advertising
        assert len(made) == 1 and made[0].started == 1
        await switch.set_enabled(False)
        await switch.set_enabled(False)
        assert not switch.advertising
        assert made[0].stopped == 1
        await switch.set_enabled(True)  # a fresh advertiser after being off
        assert len(made) == 2 and made[1].started == 1

    async def test_switched_off_config_never_starts(self) -> None:
        def factory(config: AdvertiseConfig) -> Advertiser:
            raise AssertionError("KIDSPLAY_MDNS=0 must never advertise")

        switch = AdvertisingSwitch(None, factory)
        await switch.set_enabled(True)
        assert not switch.advertising
        await switch.stop()

    async def test_a_failed_start_can_be_retried(self) -> None:
        results = iter([False, True])
        made: list[FakeAdvertiser] = []

        def factory(config: AdvertiseConfig) -> Advertiser:
            made.append(FakeAdvertiser(config, works=next(results)))
            return made[-1]  # ty: ignore[invalid-return-type]  # duck-typed fake

        switch = AdvertisingSwitch(AdvertiseConfig(), factory)
        await switch.set_enabled(True)
        assert not switch.advertising
        await switch.set_enabled(True)
        assert switch.advertising

    async def test_stop_is_safe_when_idle(self) -> None:
        switch = AdvertisingSwitch(AdvertiseConfig())
        await switch.stop()


class TestAdvertisingFollowsPairing:
    """Handhelds look for the server only to pair (issue #43)."""

    async def test_not_announced_while_pairing_is_off(
        self, tmp_path: Path, fake_zeroconf: type[FakeZeroconf]
    ) -> None:
        app = create_app(
            tmp_path / "db.sqlite", tmp_path / "media", advertise=AdvertiseConfig()
        )
        async with app.router.lifespan_context(app):
            (zc,) = fake_zeroconf.instances
            assert len(zc.registered) == 1
        # Turn pairing off in the database, as the settings page does.
        async with aiosqlite.connect(tmp_path / "db.sqlite") as conn:
            await configure_conn(conn)
            await set_server_setting_value(
                conn, "pairing_enabled", "false", datetime.now(UTC)
            )
            await conn.commit()
        fake_zeroconf.instances = []
        app = create_app(
            tmp_path / "db.sqlite", tmp_path / "media", advertise=AdvertiseConfig()
        )
        async with app.router.lifespan_context(app):
            assert fake_zeroconf.instances == []  # never even opened a socket

    async def test_env_switch_off_is_honoured_too(
        self,
        tmp_path: Path,
        fake_zeroconf: type[FakeZeroconf],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("KIDSPLAY_PAIRING_ENABLED", "false")
        app = create_app(
            tmp_path / "db.sqlite", tmp_path / "media", advertise=AdvertiseConfig()
        )
        async with app.router.lifespan_context(app):
            assert fake_zeroconf.instances == []

    async def test_toggling_the_setting_starts_and_stops_it(
        self,
        tmp_path: Path,
        fake_zeroconf: type[FakeZeroconf],
        admin_headers_for: Callable[[FastAPI], Awaitable[dict[str, str]]],
    ) -> None:
        app = create_app(
            tmp_path / "db.sqlite", tmp_path / "media", advertise=AdvertiseConfig()
        )
        headers = await admin_headers_for(app)
        async with (
            app.router.lifespan_context(app),
            AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
                headers=headers,
            ) as c,
        ):
            (zc,) = fake_zeroconf.instances
            assert len(zc.registered) == 1

            r = await c.put("/api/v1/server-settings", json={"pairing_enabled": False})
            assert r.status_code == 200
            assert zc.registered == [] and zc.closed  # withdrawn at once

            r = await c.put("/api/v1/server-settings", json={"pairing_enabled": True})
            assert r.status_code == 200
            assert len(fake_zeroconf.instances) == 2
            assert len(fake_zeroconf.instances[1].registered) == 1

            # Saving something else does not restart the announcement.
            await c.put("/api/v1/server-settings", json={"webp_quality": 80})
            assert len(fake_zeroconf.instances) == 2
        assert fake_zeroconf.instances[1].registered == []

    async def test_settings_change_without_mdns_is_fine(
        self,
        tmp_path: Path,
        admin_headers_for: Callable[[FastAPI], Awaitable[dict[str, str]]],
    ) -> None:
        app = create_app(tmp_path / "db.sqlite", tmp_path / "media")
        headers = await admin_headers_for(app)
        async with (
            app.router.lifespan_context(app),
            AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
                headers=headers,
            ) as c,
        ):
            r = await c.put("/api/v1/server-settings", json={"pairing_enabled": False})
            assert r.status_code == 200


def test_ifaddr_is_a_declared_dependency() -> None:
    """``discovery`` imports ifaddr directly, so it must not rely on zeroconf."""
    pyproject = Path(discovery.__file__).parents[2] / "pyproject.toml"
    dependencies = tomllib.loads(pyproject.read_text())["project"]["dependencies"]
    assert any(d.split(">")[0].split("=")[0].strip() == "ifaddr" for d in dependencies)
